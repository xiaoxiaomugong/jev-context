import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from jev_context.pipeline import run, public_report
from jev_context.output import render


class PipelineTests(unittest.TestCase):
    def repo(self, path):
        (path / "app.py").write_text('def retry():\n    # CANDIDATE_BODY_SENTINEL\n    raise ValueError("retry exhausted")\n', encoding="utf-8")

    def test_local_json_stdout_excludes_source_and_all_candidate_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo(Path(tmp))
            proc = subprocess.run([sys.executable, str(ROOT / "scripts/jev-context.py"),
                "--repo", tmp, "--task", "修复重试", "--term", "retry exhausted", "--local-only", "--format", "json"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertTrue(report["reading_list"])
            self.assertNotIn("CANDIDATE_BODY_SENTINEL", proc.stdout + proc.stderr)
            self.assertNotIn("excerpt", proc.stdout)
            self.assertNotIn("candidates", report)

    def test_failed_score_has_explicit_local_fallback_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo(Path(tmp))
            with patch("jev_context.pipeline.score_candidates", return_value=dict(
                scores={}, status="timeout", diagnostics=["jev_timeout"], calls=1, completed=0)):
                result = run(Path(tmp), "修复重试", ["retry"], jev_timeout=0.1)
            row = result["reading_list"][0]
            self.assertIsNone(row["jev_score"])
            self.assertEqual(row["ranking_source"], "local_fallback")
            self.assertEqual(result["status"], "local_fallback")

    def test_full_scores_from_partial_or_timeout_do_not_claim_external_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo(Path(tmp))
            for scoring_status in ("partial", "timeout"):
                with self.subTest(scoring_status=scoring_status):
                    def scorer(task, candidates, spec_path, **kwargs):
                        return dict(
                            scores={c["id"]: dict(score=3.25, confidence=.9,
                                                 probabilities=[0, 0, 0, .75, .25],
                                                 uncertain=False, source="jev")
                                    for c in candidates},
                            status=scoring_status, diagnostics=["jev_cli_failed"],
                            calls=len(candidates), completed=len(candidates),
                        )
                    with patch("jev_context.pipeline.score_candidates", scorer):
                        result = run(Path(tmp), "修复重试", ["retry"])
                    self.assertEqual(result["status"], "partial_fallback")
                    self.assertEqual(result["metrics"].get("scoring_status"), scoring_status)
                    self.assertEqual(result["metrics"]["jev_completed"], result["candidate_count"])
                    self.assertEqual(result["reading_list"][0]["jev_score"], 3.25)
                    self.assertEqual(result["reading_list"][0]["ranking_source"], "jev")

    def test_full_scores_with_ok_status_claim_external_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo(Path(tmp))
            def scorer(task, candidates, spec_path, **kwargs):
                return dict(
                    scores={c["id"]: dict(score=3.25, confidence=.9,
                                         probabilities=[0, 0, 0, .75, .25],
                                         uncertain=False, source="jev")
                            for c in candidates},
                    status="ok", diagnostics=[], calls=len(candidates), completed=len(candidates),
                )
            with patch("jev_context.pipeline.score_candidates", scorer):
                result = run(Path(tmp), "修复重试", ["retry"])
            self.assertEqual(result["status"], "jev")
            self.assertEqual(result["metrics"].get("scoring_status"), "ok")

    def test_unrecognized_scoring_status_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo(Path(tmp))
            with patch("jev_context.pipeline.score_candidates", return_value=dict(
                scores={}, status="PRIVATE_SCORER_STATUS_SENTINEL", diagnostics=[], calls=0, completed=0)):
                result = run(Path(tmp), "修复重试", ["retry"])
            self.assertEqual(result["metrics"].get("scoring_status"), "error")
            self.assertNotIn("PRIVATE_SCORER_STATUS_SENTINEL", json.dumps(public_report(result)))

    def test_short_jev_excerpt_keeps_late_hit_and_reports_actual_excerpt_range(self):
        from jev_context.pipeline import _jev_cards
        from jev_context.tokens import TokenCounter
        from test_selection import card
        c = card("core", 1, 100)
        c.update(excerpt="    filler = 1234567890\n" * 50 + "    raise ValueError('SCORE_KEY_AT_LAST_LINE')\n",
                 excerpt_start_line=1, excerpt_end_line=51, anchor_line=51)
        result = _jev_cards([c], TokenCounter(use_tokenizer=False))[0]
        self.assertIn("SCORE_KEY_AT_LAST_LINE", result["excerpt"])
        self.assertEqual(result["excerpt_end_line"], 51)
        self.assertGreater(result["excerpt_start_line"], 1)

    def test_rendered_list_budget_and_real_range_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            for i in range(20):
                (p / f"file{i}.py").write_text(f'def retry_{i}():\n    return "retry"\n', encoding="utf-8")
            for fmt in ["json", "markdown"]:
                result = run(p, "repair retries", ["retry"], read_budget=300,
                             list_budget=700, local_only=True, output_format=fmt)
                text = render(public_report(result), fmt)
                from jev_context.tokens import TokenCounter
                counter = TokenCounter()
                self.assertLessEqual(counter.count(text), 700)
                self.assertLessEqual(result["metrics"]["reading_tokens"], 240)
                self.assertEqual(result["metrics"]["list_tokens"], counter.count(text))

    def test_debug_is_opt_in_and_excluded_from_next_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            self.repo(p)
            debug = p / "debug"
            first = run(p, "retry", ["retry"], local_only=True, debug_dir=debug)
            self.assertTrue(list(debug.glob("run-*/candidates.jsonl")))
            second = run(p, "retry", ["retry"], local_only=True, debug_dir=debug)
            self.assertFalse(any(c["path"].startswith("debug/") for c in second["candidates"]))


if __name__ == "__main__":
    unittest.main()
