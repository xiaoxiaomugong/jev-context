"""Generic regressions for the frozen rejected trial; production does not import it."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_retrieval as baseline_tests

TRIAL = Path(__file__).resolve().parents[1] / "references/runs/2026-10-06/retrieval-ablation/trial-v2/retrieval.py"


class RetrievalExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location("jev_rejected_retrieval_trial", TRIAL)
        self.module = importlib.util.module_from_spec(spec)
        exec(compile(TRIAL.read_bytes(), str(TRIAL), "exec"), self.module.__dict__)

    tearDown = baseline_tests.RetrievalTests.tearDown
    write = baseline_tests.RetrievalTests.write
    collect = baseline_tests.RetrievalTests.collect

    def test_saturated_repeated_prefix_leaves_room_for_later_symbol(self):
        self.write("app.py", "def noise():\n" + "    # token\n" * 20 +
                   "    return 0\n\ndef refresh():\n    return 'expired token'\n")
        result = self.collect(terms=["token"], expand=False, resample_hits=True, trace=True)
        self.assertEqual({c["symbol"] for c in result["candidates"]}, {"noise", "refresh"})
        self.assertLessEqual(result["retrieval_trace"]["resampling"]["anchors"], 12)

    def test_saturated_selection_is_stable_when_functions_move(self):
        functions = [f"def weak_{i:02d}():\n    return 'token'\n\n" for i in range(16)]
        relevant = "def token_refresh():\n    return 'token'\n\n"
        for blocks in [functions + [relevant], [relevant] + list(reversed(functions))]:
            self.write("app.py", "".join(blocks))
            result = self.collect(terms=["token"], expand=False, resample_hits=True)
            symbols = {c["symbol"] for c in result["candidates"]}
            self.assertIn("token_refresh", symbols)
            self.assertLessEqual(len(symbols), 12)
            if 'previous' in locals():
                self.assertEqual(previous, symbols)
            previous = symbols

    def test_repeated_hits_cannot_keep_inflating_evidence_score(self):
        scores = []
        for repeats in (1, 10):
            self.write("app.py", "def noise():\n" + "    # token\n" * repeats + "    return 0\n")
            result = self.collect(terms=["token"], expand=False, evidence_coverage=True)
            self.assertEqual(len(result["candidates"]), 1)
            scores.append(result["candidates"][0]["local_score"])
        self.assertEqual(scores[0], scores[1])

    def test_evidence_coverage_preserves_suffix_test_term_witness(self):
        self.write("app.py", "def token_core():\n    return 'token'\n\n"
                   "def token_other():\n    return 'token'\n")
        self.write("app_test.py", "def test_case():\n    assert 'token'\n")
        result = self.collect(terms=["token"], expand=False, candidate_limit=2,
                              evidence_coverage=True)
        self.assertEqual({c["path"] for c in result["candidates"]}, {"app.py", "app_test.py"})

    def test_test_witness_preserves_existing_enclosing_test_assertions(self):
        self.write("app.py", "def token_core():\n    return 'token'\n")
        self.write("tests/test_app.py", "def test_workflow():\n"
                   "    def cli():\n        return 'token weak'\n"
                   "    observed = cli()\n    assert observed.startswith('token')\n\n" +
                   "".join(f"def test_noise_{i}():\n    assert 'token weak'\n\n" for i in range(15)))
        result = self.collect(terms=["token", "weak"], expand=False, candidate_limit=2,
                              evidence_coverage=True)
        found = {(c["path"], c["symbol"]): c for c in result["candidates"]}
        self.assertIn(("app.py", "token_core"), found)
        self.assertIn(("tests/test_app.py", "test_workflow"), found)
        parent = found[("tests/test_app.py", "test_workflow")]
        self.assertEqual((parent["start_line"], parent["end_line"]), (1, 5))
        self.assertIn("assert observed.startswith('token')", parent["excerpt"])
        self.assertNotIn(("tests/test_app.py", "test_workflow.cli"), found)

    def test_saturated_resampling_preserves_rg_unicode_literal_semantics(self):
        # The source scan must agree with the primary literal matcher, including
        # simple Unicode case pairs without Turkish-I or multi-character folds.
        cases = [("item", "\u0131tem", False), ("item", "\u0130tem", False),
                 ("item", "ITEM", True), ("stra\u00dfe", "STRA\u1e9eE", True),
                 ("stra\u00dfe", "STRASSE", False), ("seek", "\u017feek", True),
                 ("keep", "\u212aeep", True)]
        for term, text, expected in cases:
            with self.subTest(term=term, text=text):
                self.write("app.py", "def early():\n" + f"    # {term}\n" * 12 +
                           "    return 0\n\ndef late():\n" + f"    return '{text}'\n")
                self.write("probe.txt", text + "\n")
                primary = self.module._rg_hits(self.repo, ["probe.txt"], [term], [])
                self.assertEqual(bool(primary), expected)
                result = self.collect(terms=[term], expand=False, resample_hits=True)
                self.assertEqual("late" in {c["symbol"] for c in result["candidates"]}, expected)

    def test_file_and_global_limits_preserve_direct_dependency_and_test(self):
        self.write("app.py", "def entry():\n    # token\n    return helper()\n\n" +
                   "".join(f"def noise_{i}():\n    # token weak\n    return 0\n\n" for i in range(15)) +
                   "def helper():\n    return 1\n")
        self.write("tests/test_app.py", "from app import entry\n\ndef test_entry():\n    assert entry() == 1\n")
        result = self.collect(terms=["token", "weak"], symbols=["entry"], candidate_limit=4,
                              evidence_coverage=True, resample_hits=True, trace=True)
        found = {(c["path"], c["symbol"]) for c in result["candidates"]}
        self.assertIn(("app.py", "entry"), found)
        self.assertIn(("app.py", "helper"), found)
        self.assertIn(("tests/test_app.py", "test_entry"), found)
        self.assertLessEqual(len(result["candidates"]), 4)
        self.assertLessEqual(sum(not c["pinned"] for c in result["retrieval_trace"]["post_per_file_candidates"] if c["path"] == "app.py"), 12)

    def test_saturated_single_symbol_stays_bounded_without_new_subprocesses(self):
        self.write("app.py", "def noise():\n" + "    # token\n" * 5000 + "    return 0\n")
        import subprocess
        original = subprocess.run
        with patch.object(subprocess, "run", wraps=original) as run:
            result = self.collect(terms=["token"], expand=False, resample_hits=True, trace=True)
        self.assertEqual(run.call_count, 2)  # discovery and the original bounded rg search
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["retrieval_trace"]["resampling"]["anchors"], 1)
        self.assertLessEqual(result["retrieval_trace"]["resampling"]["scanned_bytes"], self.module.MAX_TOTAL_BYTES)
        self.assertTrue(result["candidates"][0]["incomplete"])

    def test_text_resampling_spreads_anchors_without_expanding_access(self):
        self.write("notes.txt", "token\n" * 600)
        self.write(".env", "DO_NOT_ECHO token\n" * 50)
        self.write("ignored/notes.txt", "DO_NOT_ECHO token\n" * 50)
        result = self.collect(terms=["token"], expand=False, resample_hits=True,
                              excluded_dirs=[self.repo / "ignored"], trace=True)
        self.assertEqual(set(result["files"]), {"notes.txt"})
        self.assertLessEqual(len(result["candidates"]), 12)
        self.assertGreater(max(c["anchor_line"] for c in result["candidates"]), 500)
        self.assertNotIn("DO_NOT_ECHO", str(result["diagnostics"]))

    def test_resampling_deadline_preserves_bounded_original_hits(self):
        source = self.module._Source("app.py", "token\n" * 100, [])
        hits = {"app.py": {i: ["token"] for i in range(1, 13)}}
        diagnostics, trace = [], {}
        result = self.module._resample_hits(hits, lambda path: source, ["token"], diagnostics,
                                            deadline=0, trace=trace)
        self.assertEqual(result, hits)
        self.assertTrue(trace["interrupted"])
        self.assertTrue(any("time limit" in d for d in diagnostics))

    def test_resampling_interruption_never_commits_partial_file_anchors(self):
        source = self.module._Source("app.py", "token\n" * 200, [])
        hits = {"app.py": {i: ["token"] for i in range(1, 13)}}
        diagnostics, trace = [], {}
        with patch.object(self.module.time, "monotonic", side_effect=[0, 0, 0, 2, 2]):
            result = self.module._resample_hits(hits, lambda path: source, ["token"],
                                                diagnostics, deadline=1, trace=trace)
        self.assertEqual(result, hits)
        self.assertTrue(trace["interrupted"])
        self.assertEqual(trace["anchors"], 0)
        self.assertLess(trace["scanned_bytes"], len(source.text.encode("utf-8")))

    def test_saturated_resampling_cannot_cross_scan_and_file_size_limits(self):
        text = "token\n" * 100
        self.write("first.txt", text)
        self.write("second.txt", text)
        self.write("oversize.txt", text * 2)
        with patch.object(self.module, "MAX_TOTAL_BYTES", len(text.encode("utf-8"))), \
             patch.object(self.module, "MAX_FILE_BYTES", len(text.encode("utf-8"))):
            result = self.collect(terms=["token"], resample_hits=True, expand=False, trace=True)
        self.assertEqual(set(result["files"]), {"first.txt"})
        self.assertEqual(result["retrieval_trace"]["scan"]["scan_limit_skipped"], 1)
        self.assertEqual(result["retrieval_trace"]["resampling"]["scanned_bytes"], 600)
