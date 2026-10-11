"""Exercise the actual subprocess/JSONL boundary with an offline executable CLI."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    from jev_context.jev_adapter import score_candidates
except ModuleNotFoundError:
    score_candidates = None


class JevAdapterTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(score_candidates), "Jev subprocess adapter is not implemented")
        self.temp = tempfile.TemporaryDirectory(prefix="jev-adapter-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake_count = 0
        self.spec = Path(__file__).resolve().parents[1] / "references" / "relevance-spec.json"
        self.candidates = [
            {"id": "c1", "path": "engine.py", "start_line": 3, "end_line": 14,
             "symbol": "Engine.run", "kind": "method", "signature": "def run(self, task):",
             "excerpt": "CONFIDENTIAL_BODY_SENTINEL", "retrieval_signals": ["task"],
             "relations": [], "estimated_read_tokens": 33, "extraction_mode": "python_ast"},
            {"id": "c2", "path": "test_engine.py", "start_line": 2, "end_line": 9,
             "symbol": "test_run", "kind": "function", "signature": "def test_run():",
             "excerpt": "PRIVATE_TEST_SENTINEL", "retrieval_signals": ["task"],
             "relations": [{"kind": "test", "source": "engine.py"}],
             "estimated_read_tokens": 25, "extraction_mode": "python_ast"},
        ]

    def fake_cli(self, body: str) -> str:
        # No shell invocation: the adapter must execute this filename even with spaces.
        self.fake_count += 1
        path = self.root / f"fake jev {self.fake_count}"
        prefix = """
import json
import sys
import time
from pathlib import Path
rows = [json.loads(line) for line in sys.stdin if line.strip()]
with Path(__file__).with_suffix('.invocations').open('a') as handle:
    handle.write('one invocation\\n')
def success(row, score=3.25, confidence=.9, probabilities=None):
    if probabilities is None:
        probabilities = {'0': .0, '1': .0, '2': .0, '3': .75, '4': .25}
    return {'input': row, 'answers': {'relevance': {
        'type': 'score', 'score': score, 'confidence': confidence,
        'probabilities': probabilities, 'legend': {'0': 'none', '1': 'near',
        '2': 'background', '3': 'direct', '4': 'core'}, 'label': 'direct'}}}
def emit(row):
    print(json.dumps(row, ensure_ascii=False), flush=True)
"""
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(prefix)
                        + "\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def score(self, executable: str, **kwargs) -> dict:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = score_candidates("修复 task 入口", self.candidates, self.spec,
                                      jev_bin=executable, **kwargs)
        self.assertEqual(out.getvalue(), "", "Adapter leaked captured CLI stdout")
        self.assertEqual(err.getvalue(), "", "Adapter leaked captured CLI stderr")
        public_result = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("CONFIDENTIAL_BODY_SENTINEL", public_result)
        self.assertNotIn("PRIVATE_TEST_SENTINEL", public_result)
        self.assertNotIn("PRIVATE_ERROR_SENTINEL", public_result)
        self.assertLessEqual(len(result["diagnostics"]), 8)
        self.assertTrue(all(len(item) <= 160 for item in result["diagnostics"]))
        return result

    def test_passes_whole_task_and_card_stdin_without_shell_or_field(self):
        cli = self.fake_cli("""
assert sys.argv[1:] == ['run', str(Path(sys.argv[2])), '-l', '-j', '4', '--json']
spec = json.loads(Path(sys.argv[2]).read_text())
assert list(spec['questions']) == ['relevance']
assert spec['questions']['relevance']['type'] == 'score'
assert len(spec['questions']['relevance']['criteria']) == 5
assert all(row['task'] == '修复 task 入口' for row in rows)
assert all(row['id'] == row['candidate']['id'] for row in rows)
assert rows[0]['candidate']['excerpt'] == 'CONFIDENTIAL_BODY_SENTINEL'
for row in reversed(rows):
    emit(success(row, score=3.25 if row['id'] == 'c1' else 1.75,
                 confidence=.9 if row['id'] == 'c1' else .5))
print('PRIVATE_ERROR_SENTINEL', file=sys.stderr)
""")
        result = self.score(cli)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scores"]["c1"]["score"], 3.25)
        self.assertEqual(result["scores"]["c2"]["score"], 1.75)
        self.assertFalse(result["scores"]["c1"]["uncertain"])
        self.assertTrue(result["scores"]["c2"]["uncertain"])
        self.assertEqual(result["scores"]["c1"]["source"], "jev")
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["completed"], 2)

    def test_partial_failure_keeps_success_and_never_assigns_zero(self):
        cli = self.fake_cli("""
emit(success(rows[0]))
emit({'input': json.dumps(rows[1]), 'error': 'PRIVATE_ERROR_SENTINEL HTTP 401'})
print('PRIVATE_ERROR_SENTINEL HTTP 401', file=sys.stderr)
sys.exit(2)
""")
        result = self.score(cli)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(set(result["scores"]), {"c1"})
        self.assertEqual(result["completed"], 1)

    def test_duplicate_output_invalidates_only_duplicate_candidate(self):
        cli = self.fake_cli("""
emit(success(rows[0]))
emit(success(rows[0], score=2.0))
emit(success(rows[1]))
""")
        result = self.score(cli)
        self.assertEqual(set(result["scores"]), {"c2"})
        self.assertEqual(result["status"], "partial")

    def test_unknown_id_cannot_be_matched_by_output_position(self):
        cli = self.fake_cli("""
unknown = dict(rows[0], id='unrecognized')
unknown['candidate'] = dict(unknown['candidate'], id='unrecognized')
emit(success(unknown))
emit(success(rows[1]))
""")
        result = self.score(cli)
        self.assertEqual(set(result["scores"]), {"c2"})
        self.assertEqual(result["status"], "partial")

    def test_complete_input_must_match_task_and_candidate(self):
        for change in ("changed['task'] = 'another task'",
                       "changed['candidate']['excerpt'] = 'changed excerpt'",
                       "changed['candidate']['id'] = 'c2'",
                       "changed['candidate']['start_line'] = True"):
            with self.subTest(change=change):
                cli = self.fake_cli("changed = json.loads(json.dumps(rows[0]))\n"
                                    + change + "\nemit(success(changed))\nemit(success(rows[1]))")
                result = self.score(cli)
                self.assertEqual(set(result["scores"]), {"c2"})

    def test_malformed_json_does_not_hide_other_successes(self):
        cli = self.fake_cli("""
print('{PRIVATE_ERROR_SENTINEL invalid JSON}', flush=True)
emit(success(rows[1]))
sys.exit(2)
""")
        result = self.score(cli)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(set(result["scores"]), {"c2"})

    def test_deep_json_record_keeps_an_earlier_valid_score(self):
        cli = self.fake_cli("""
emit(success(rows[0]))
print('[' * 20000 + '0' + ']' * 20000, flush=True)
""")
        result = self.score(cli)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(set(result['scores']), {'c1'})
        self.assertEqual(result['completed'], 1)
        self.assertTrue({'jev_invalid_json', 'jev_invalid_record'} & set(result['diagnostics']))

    def test_deep_error_input_keeps_other_valid_scores(self):
        cli = self.fake_cli("""
emit(success(rows[0]))
emit({'input': '[' * 20000 + '0' + ']' * 20000,
      'error': 'PRIVATE_ERROR_SENTINEL'})
""")
        result = self.score(cli)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(set(result['scores']), {'c1'})
        self.assertTrue({'jev_invalid_error_input', 'jev_invalid_input_echo'} & set(result['diagnostics']))

    def test_deep_candidate_json_does_not_crash_request_validation(self):
        nested = 0
        for _ in range(20000):
            nested = [nested]
        self.candidates[0]['nested'] = nested
        cli = self.fake_cli('emit(success(rows[0]))')
        result = self.score(cli)
        if result['calls']:
            # Python 3.14 can encode this nesting; valid JSON need not be refused.
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(set(result['scores']), {'c1'})
            self.assertTrue(Path(cli).with_suffix('.invocations').exists())
        else:
            self.assertEqual(result['status'], 'invalid_input')
            self.assertEqual(result['scores'], {})
            self.assertFalse(Path(cli).with_suffix('.invocations').exists())

    def test_missing_line_is_not_an_implicit_zero(self):
        cli = self.fake_cli("emit(success(rows[0]))")
        result = self.score(cli)
        self.assertEqual(set(result["scores"]), {"c1"})
        self.assertEqual(result["status"], "partial")

    def test_timeout_retains_only_complete_successful_lines_without_retry(self):
        cli = self.fake_cli("""
emit(success(rows[0]))
sys.stdout.write('{"input": ')
sys.stdout.flush()
time.sleep(4)
""")
        result = self.score(cli, timeout=1)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(set(result["scores"]), {"c1"})
        self.assertEqual(Path(cli).with_suffix(".invocations").read_text().count("invocation"), 1)

    def test_unicode_line_separators_in_echo_preserve_jsonl_records(self):
        for separator in ("\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                self.candidates[0]["excerpt"] = "CONFIDENTIAL_BODY_SENTINEL" + separator + "tail"
                cli = self.fake_cli("""
print(json.dumps(success(rows[0]), ensure_ascii=False), flush=True)
emit(success(rows[1]))
""")
                result = self.score(cli)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(set(result["scores"]), {"c1", "c2"})

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_timeout_bounds_child_pipe_cleanup_and_keeps_completed_scores(self):
        cli = self.fake_cli("""
import subprocess
emit(success(rows[0]))
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'],
                 stdin=subprocess.DEVNULL)
time.sleep(4)
""")
        started = time.monotonic()
        result = self.score(cli, timeout=1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5, "descendant pipes extended the whole-batch timeout")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(set(result["scores"]), {"c1"})
        self.assertEqual(Path(cli).with_suffix(".invocations").read_text().count("invocation"), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_timeout_cleanup_is_bounded_when_child_leaves_process_group(self):
        cli = self.fake_cli("""
import subprocess
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'],
                         stdin=subprocess.DEVNULL, start_new_session=True)
Path(__file__).with_suffix('.child-pid').write_text(str(child.pid))
emit(success(rows[0]))
time.sleep(4)
""")
        started = time.monotonic()
        try:
            result = self.score(cli, timeout=1)
        finally:
            marker = Path(cli).with_suffix(".child-pid")
            if marker.exists():
                try:
                    os.kill(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(set(result["scores"]), {"c1"})

    def test_missing_cli_has_no_calls_and_safe_fallback(self):
        result = self.score(str(self.root / "missing jev"))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["scores"], {})
        self.assertEqual(result["calls"], 0)

    def test_missing_credentials_or_auth_failure_sanitizes_diagnostic(self):
        for failure in ("未找到 TYPESAFE_API_KEY PRIVATE_ERROR_SENTINEL",
                        "API 错误 (HTTP 401): PRIVATE_ERROR_SENTINEL",
                        "API 错误 (HTTP 403): PRIVATE_ERROR_SENTINEL"):
            with self.subTest(failure=failure):
                cli = self.fake_cli("print(" + repr(failure)
                                    + ", file=sys.stderr)\nsys.exit(2)")
                result = self.score(cli)
                self.assertEqual(result["scores"], {})
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(Path(cli).with_suffix(".invocations").read_text().count("invocation"), 1)

    def test_invalid_score_confidence_or_probability_fields_are_rejected(self):
        patches = [
            "answer['score'] = -1", "answer['score'] = 4.1",
            "answer['score'] = float('nan')", "answer['score'] = True",
            "answer['score'] = 10 ** 500",
            "answer['confidence'] = float('inf')", "answer['confidence'] = -.1",
            "answer['confidence'] = 1.1", "answer['confidence'] = '0.9'",
            "answer['type'] = 'noul'", "answer.pop('score')",
            "answer.pop('probabilities')", "answer['probabilities'] = {'0': -1}",
            "answer['probabilities']['3'] = float('nan')",
            "answer['probabilities'] = [1, 0]",
            "answer['probabilities'] = {'wrong': 1}",
            "answer['probabilities'] = {'0': 1, '1': 1, '2': 1, '3': 1, '4': 1}",
            "answer['probabilities']['4'] = 10 ** 500",
        ]
        for patch in patches:
            with self.subTest(patch=patch):
                cli = self.fake_cli("out = success(rows[0])\n"
                                    "answer = out['answers']['relevance']\n" + patch
                                    + "\nemit(out)\nemit(success(rows[1]))")
                result = self.score(cli)
                self.assertEqual(set(result["scores"]), {"c2"})

    def test_ambiguous_duplicate_json_fields_do_not_become_valid_scores(self):
        cli = self.fake_cli("""
raw = json.dumps(success(rows[0]))
print(raw.replace('"score": 3.25', '"score": 0, "score": 3.25'), flush=True)
emit(success(rows[1]))
""")
        result = self.score(cli)
        self.assertEqual(set(result["scores"]), {"c2"})

    def test_score_distribution_is_preserved_as_distribution_not_correct_probability(self):
        cli = self.fake_cli("""
emit(success(rows[0], score=0.4, confidence=.95,
             probabilities=[.6, .4, 0.0, 0.0, 0.0]))
emit(success(rows[1]))
""")
        result = self.score(cli)
        self.assertEqual(result["scores"]["c1"]["score"], .4)
        self.assertEqual(result["scores"]["c1"]["confidence"], .95)
        self.assertEqual(result["scores"]["c1"]["probabilities"], [.6, .4, 0, 0, 0])

    def test_duplicate_candidate_inputs_are_rejected_before_cli(self):
        cli = self.fake_cli("for row in rows: emit(success(row))")
        result = score_candidates("task", [self.candidates[0], self.candidates[0]],
                                  self.spec, jev_bin=cli)
        self.assertEqual(result["scores"], {})
        self.assertEqual(result["status"], "invalid_input")
        self.assertEqual(result["calls"], 0)
        self.assertFalse(Path(cli).with_suffix(".invocations").exists())

    def test_empty_candidates_skip_subprocess(self):
        result = score_candidates("task", [], self.spec, jev_bin="missing jev")
        self.assertEqual(result["scores"], {})
        self.assertEqual(result["calls"], 0)
        self.assertEqual(result["status"], "empty")


if __name__ == "__main__":
    unittest.main()
