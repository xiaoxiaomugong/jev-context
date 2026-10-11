"""The public paired reader logs every delivery without executing its target."""

import importlib.util
from contextlib import redirect_stdout
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/task_experiment.py"


class TaskExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.run = self.base / "run"
        self.run.mkdir()
        self.config()

    def config(self, method="baseline", repo=None, submission_ledger=None):
        config = {
            "repo": str(repo or self.repo), "task_id": "synthetic", "method": method,
        }
        if submission_ledger is not None:
            config['submission_ledger'] = str(submission_ledger)
        (self.run / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def module(self):
        sys.path.insert(0, str(SCRIPT.parent))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPT.parent)))
        spec = importlib.util.spec_from_file_location('paired_reader_for_test', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def write(self, path, text):
        destination = self.repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        return destination

    def invoke(self, *args, env=None):
        self.assertTrue(SCRIPT.is_file(), "paired task reader entry point is missing")
        result = subprocess.run([sys.executable, str(SCRIPT), "--run-dir", str(self.run),
                                 *map(str, args)], capture_output=True,
                                env=env, timeout=30)
        result.stdout = result.stdout.decode('utf-8')
        result.stderr = result.stderr.decode('utf-8')
        self.assertEqual(result.stderr, "")
        return result

    def events(self):
        return [json.loads(line) for line in (self.run / "events.jsonl").read_text().split('\n') if line]

    def success(self, *args):
        result = self.invoke(*args)
        self.assertEqual(result.returncode, 0, result.stdout)
        event = self.events()[-1]
        self.assertEqual(event["text"], result.stdout)
        self.assertGreaterEqual(event["elapsed_seconds"], 0)
        return result.stdout

    def failure(self, *args, env=None):
        result = self.invoke(*args, env=env)
        self.assertNotEqual(result.returncode, 0)
        return result.stdout

    def test_regex_glob_case_and_fixed_search_are_normal_rg_capabilities(self):
        self.write("src/a.py", "alpha\nBeta\na.b\na0b\n")
        self.write("src/b.js", "alpha\n")
        self.write(".gitignore", "ignored.py\n")
        self.write("ignored.py", "alpha\n")
        self.assertEqual(self.success("files", "--glob", "src/*.py"), "src/a.py\n")
        self.assertEqual(self.success("search", "alpha|Beta", "--glob", "src/*.py"),
                         "src/a.py:1:alpha\nsrc/a.py:2:Beta\n")
        self.assertEqual(self.success("search", "beta", "--ignore-case", "--path", "src"),
                         "src/a.py:2:Beta\n")
        self.assertEqual(self.success("search", "a.b", "--fixed", "--glob", "*.py"),
                         "src/a.py:3:a.b\n")

    def test_search_read_overlap_and_explicit_repeat_have_separate_costs(self):
        self.write("a.py", "one\ntwo\nthree\n")
        self.success("search", "one")
        self.assertEqual(self.success("read", "a.py", 1, 2), "a.py:1-2\n1:one\n2:two\n")
        self.assertEqual(self.success("read", "a.py", 2, 3), "a.py:3-3\n3:three\n")
        self.assertEqual(self.success("read", "a.py", 1, 3), "Already delivered; use --repeat to request it again.\n")
        self.assertEqual(self.events()[-1].get("source_ranges"), [])
        self.success("read", "a.py", 2, 2, "--repeat")
        summary = json.loads(self.success("summary"))
        self.assertEqual(summary["code_range_union_tokens"], 5)
        self.assertEqual(summary["returned_source_tokens"], 9)
        self.assertEqual(summary["repeated_source_tokens"], 4)
        self.assertIsNone(summary["api_usage"])
        self.assertEqual(summary["event_count"], 5)
        self.assertGreater(summary["elapsed_seconds"], 0)

    def test_read_source_hash_change_fails_without_delivering_changed_body(self):
        source = self.write("a.py", "one\ntwo\n")
        self.success("read", "a.py", 1, 1)
        before = (self.run / "state.json").read_bytes()
        source.write_text("CHANGED_PRIVATE_BODY\ntwo\n")
        text = self.failure("read", "a.py", 2, 2)
        self.assertIn("changed", text)
        self.assertNotIn("CHANGED_PRIVATE_BODY", text)
        self.assertEqual((self.run / "state.json").read_bytes(), before)
        self.assertFalse(self.events()[-1].get("source_ranges"))

    def test_failure_text_and_invalid_arguments_are_charged(self):
        self.write("a.py", "one\n")
        text = self.failure("search", "(")
        self.assertNotIn("regex parse error", text)
        self.assertEqual(self.events()[-1]["text"], text)
        self.assertEqual(self.events()[-1].get('io_metadata'), {
            'source_read_bytes': 4, 'snapshot_written_bytes': 4, 'snapshot_files': 1,
        })
        self.failure("read", "a.py", 0, 1)
        self.failure("search", "one", "--limit", 0)
        before = self.events()
        summary = json.loads(self.success("summary"))
        self.assertEqual(summary["visible_text_tokens"],
                         sum(math.ceil(len(item["text"].encode()) / 3) for item in before))
        self.assertEqual(summary["event_count"], 3)
        self.assertFalse(any(item.get("source_ranges") for item in before))

    def test_configurable_limits_deliver_only_counted_lines_and_notice(self):
        for number in range(4):
            self.write(f"p{number}.py", "match\nmatch\n")
        text = self.success("files", "--limit", 2)
        self.assertIn("limit", text.lower())
        self.assertEqual(len([line for line in text.splitlines() if line.endswith(".py")]), 2)
        text = self.success("search", "match", "--limit", 3)
        self.assertIn("limit", text.lower())
        self.assertEqual(len(self.events()[-1]["source_ranges"]), 3)
        self.assertEqual(len([line for line in text.splitlines() if ":match" in line]), 3)
        self.assertEqual(self.success("search", "match", "--path", "p3.py", "--limit", 1000),
                         "p3.py:1:match\np3.py:2:match\n")

    def test_missing_rg_and_no_matches_are_logged_nonzero(self):
        self.write("a.py", "one\n")
        env = dict(os.environ, PATH=str(self.base / "no-tools"))
        self.assertIn("failure", self.failure("files", env=env))
        self.assertIn("No matches", self.failure("search", "absent"))
        self.assertEqual(len(self.events()), 2)

    def test_unsafe_repo_paths_are_refused_without_echoing_body(self):
        self.write(".env", "SYNTHETIC_SECRET\n")
        outside = self.base / "outside.py"
        outside.write_text("OUTSIDE_BODY\n")
        (self.repo / "link.py").symlink_to(outside)
        self.write("a.py", "one\n")
        for path in ("../outside.py", str(outside), "link.py", ".env"):
            text = self.failure("read", path, 1, 1)
            self.assertNotIn("BODY", text)
            self.assertNotIn("SECRET", text)
        self.assertTrue(self.success("files").startswith("a.py\n"))

    def test_run_inside_repo_and_metadata_symlinks_never_write_target(self):
        self.write("a.py", "one\n")
        inside = self.repo / "run"
        inside.mkdir()
        old_run = self.run
        self.run = inside
        self.config()
        self.failure("read", "a.py", 1, 1)
        self.assertFalse((inside / "events.jsonl").exists())
        self.assertFalse((inside / "state.json").exists())
        self.run = old_run
        sink = self.base / "sink"
        sink.write_text("untouched")
        for name in ("config.json", "events.jsonl", "state.json"):
            with self.subTest(name=name):
                path = self.run / name
                if path.exists():
                    path.unlink()
                path.symlink_to(sink)
                self.failure("read", "a.py", 1, 1)
                self.assertEqual(sink.read_text(), "untouched")
                path.unlink()
                self.config()

    def test_hardlinked_event_log_never_writes_the_target_repository(self):
        self.write('a.py', 'one\n')
        target = self.write('audit.jsonl', '')
        original_mode = target.stat().st_mode
        os.link(target, self.run / 'events.jsonl')
        result = self.invoke('files')
        self.assertEqual(target.read_bytes(), b'', 'event logging wrote through a hardlink into the target')
        self.assertEqual(target.stat().st_mode, original_mode)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('failure', result.stdout)

    def test_lf_source_ranges_preserve_unicode_and_colon_path(self):
        self.write("name:part.py", "one\u2028two\vthree\rrest\nname:part.py:1-2\nlast")
        self.assertEqual(self.success("read", "name:part.py", 1, 3),
                         "name:part.py:1-3\n1:one\u2028two\vthree\rrest\n2:name:part.py:1-2\n3:last\n")
        ranges = self.events()[-1]["source_ranges"]
        self.assertEqual(ranges, [{"path": "name:part.py", "start_line": 1,
                                  "source_lines": ["one\u2028two\vthree\rrest\n", "name:part.py:1-2\n", "last\n"]}])
        self.success("search", "last")
        self.assertEqual(self.events()[-1]["source_ranges"][0]["source_lines"], ["last\n"])

    def test_baseline_refuses_advice_and_local_saves_each_plan_without_jev(self):
        self.write("a.py", "def target():\n    return 1\n")
        self.assertIn("local", self.failure("advice", "--task", "target"))
        self.assertEqual(list(self.run.glob("plan-*.json")), [])
        self.config("local")
        executable = self.base / "bin"
        executable.mkdir()
        marker = self.base / "jev-started"
        fake = executable / "jev"
        fake.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        fake.chmod(0o700)
        env = dict(os.environ, PATH=str(executable) + os.pathsep + os.environ["PATH"])
        for _ in range(2):
            result = self.invoke("advice", "--task", "target", "--term", "target", env=env)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("local_only", result.stdout)
            self.assertEqual(self.events()[-1]["text"], result.stdout)
        self.assertFalse(marker.exists())
        plans = sorted(self.run.glob("plan-*.json"))
        self.assertEqual(len(plans), 2)
        for path in plans:
            plan = json.loads(path.read_text())
            self.assertEqual(plan["metrics"]["jev_calls"], 0)
            self.assertIn("a.py", plan["source_hashes"])
            self.assertNotIn("candidates", plan)

    def test_read_300_line_limit_is_identical_for_both_methods(self):
        self.write("a.py", "x\n" * 301)
        for method in ("baseline", "local"):
            self.config(method)
            self.assertIn("300", self.failure("read", "a.py", 1, 301))

    def test_search_snapshots_safely_opened_bytes_with_one_rg_search_process(self):
        self.write("a.py", "hit\n")
        self.write("package/b.py", "miss\n")
        sys.path.insert(0, str(SCRIPT.parent))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPT.parent)))
        spec = importlib.util.spec_from_file_location('paired_reader_for_test', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        with patch.object(module.subprocess, 'run', wraps=subprocess.run) as run, redirect_stdout(output):
            status = module.main(['--run-dir', str(self.run), 'search', 'hit'])
        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue(), 'a.py:1:hit\n')
        search_calls = [call for call in run.call_args_list if '--json' in call.args[0]]
        self.assertEqual(len(search_calls), 1)
        self.assertEqual(self.events()[-1]['io_metadata'], {
            'source_read_bytes': 9, 'snapshot_written_bytes': 9, 'snapshot_files': 2,
        })

    def test_malformed_event_fields_fail_with_fixed_recorded_text(self):
        (self.run / 'events.jsonl').write_text(json.dumps({
            'kind': 'setup', 'text': {'private': 'NOT_FOR_STDERR'},
        }) + '\n')
        text = self.failure('summary')
        self.assertNotIn('NOT_FOR_STDERR', text)
        self.assertEqual(self.events()[-1]['text'], text)

    def test_first_read_after_advice_checks_saved_source_fingerprint(self):
        self.config('local')
        source = self.write('a.py', 'def target():\n    return 1\n')
        self.success('advice', '--task', 'target', '--term', 'target')
        source.write_text('CHANGED_PRIVATE_BODY\n')
        text = self.failure('read', 'a.py', 1, 1)
        self.assertIn('changed', text)
        self.assertNotIn('CHANGED_PRIVATE_BODY', text)
        self.assertFalse((self.run / 'state.json').exists())

    def test_rejected_changed_search_source_never_marks_undelivered_read_seen(self):
        source = self.write('a.py', 'old\n')
        self.success('search', 'old')
        source.write_text('new\n')
        self.assertIn('changed', self.failure('read', 'a.py', 1, 1))
        self.assertFalse((self.run / 'state.json').exists())
        self.failure('read', 'a.py', 1, 1)
        self.assertFalse((self.run / 'state.json').exists())
        self.assertEqual(list(self.run.glob('.pending-state-*')), [])

    def test_extreme_elapsed_metadata_is_a_logged_fixed_failure(self):
        (self.run / 'events.jsonl').write_text(json.dumps({
            'kind': 'setup', 'text': 'synthetic', 'elapsed_seconds': 10**400,
        }) + '\n')
        text = self.failure('summary')
        self.assertIn('failure', text)
        self.assertEqual(self.events()[-1]['text'], text)

    def test_json_expansion_never_writes_an_unreplayable_delivery_or_state(self):
        self.write('a.py', '\v' * 1_000_000 + '\n')
        self.assertIn('limit', self.failure('read', 'a.py', 1, 1))
        self.assertFalse((self.run / 'state.json').exists())
        summary = json.loads(self.success('summary'))
        self.assertEqual(summary['event_count'], 1)
        self.assertEqual(summary['code_range_union_tokens'], 0)

    def test_jev_only_first_advice_scores_and_subsequent_advice_is_explicitly_local(self):
        ledger = self.base / 'submissions.json'
        ledger.write_text(json.dumps({'scope': 'synthetic', 'candidate_submission_limit': 1200,
                                     'submitted_total': 45, 'batches': []}))
        self.config('jev', submission_ledger=ledger)
        self.write('a.py', 'def target():\n    return 1\n')
        task = '完整中文原题\n分析 target 的行为 `quotes` $literal'
        (self.run / 'task.txt').write_text(task)
        module = self.module()
        def fake(task_value, cards, rules, **kwargs):
            self.assertEqual(task_value, task)
            self.assertEqual(json.loads(ledger.read_text())['reserved_total'], len(cards))
            return {'scores': {card['id']: {'score': 3, 'confidence': .8,
                    'probabilities': [0, 0, 0, 1, 0]} for card in cards},
                    'status': 'ok', 'diagnostics': [], 'calls': len(cards), 'completed': len(cards)}
        argv = ['--run-dir', str(self.run), 'advice', '--task-file', str(self.run / 'task.txt'),
                '--term', 'target']
        with patch.object(module.pipeline, 'score_candidates', fake), patch.object(module.pipeline, 'run', wraps=module.pipeline.run) as run:
            for number in range(2):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(module.main(argv), 0, output.getvalue())
                event = self.events()[-1]
                self.assertEqual(event['request_argv'], argv)
                self.assertEqual(event['scoring_metadata']['first_advice'], number == 0)
                self.assertEqual(event['scoring_metadata']['subsequent_local_only'], number == 1)
                self.assertEqual(event['scoring_metadata']['batch_status'], 'ok')
            self.assertEqual([call.kwargs['local_only'] for call in run.call_args_list], [False, True])
        self.assertIn('subsequent advice is local only', self.events()[-1]['text'])
        self.assertEqual(len(json.loads(ledger.read_text())['batches']), 1)
        self.assertEqual(json.loads(ledger.read_text())['submitted_total'], 46)

    def test_first_jev_exception_is_logged_once_followup_local_and_original_adapter_restored(self):
        ledger = self.base / 'submissions.json'
        ledger.write_text(json.dumps({'scope': 'synthetic', 'candidate_submission_limit': 1200,
                                     'submitted_total': 45, 'batches': []}))
        self.config('jev', submission_ledger=ledger)
        self.write('a.py', 'def target():\n    return 1\n')
        module = self.module()
        def fake(*args, **kwargs):
            raise RuntimeError('CREDENTIAL_SECRET PRIVATE_CARD')
        argv = ['--run-dir', str(self.run), 'advice', '--task', 'target', '--term', 'target']
        with patch.object(module.pipeline, 'score_candidates', fake):
            for expected in (1, 0):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(module.main(argv), expected, output.getvalue())
                self.assertNotIn('SECRET', output.getvalue())
                self.assertNotIn('PRIVATE_CARD', output.getvalue())
                self.assertIs(module.pipeline.score_candidates, fake)
        self.assertEqual(self.events()[0]['scoring_metadata']['batch_status'], 'reserved_unknown')
        self.assertEqual(self.events()[0]['request_argv'], argv)
        self.assertEqual(json.loads(ledger.read_text())['reserved_total'], 1)
        self.assertEqual(len(json.loads(ledger.read_text())['batches']), 1)

    def test_jev_config_requires_existing_absolute_ledger_outside_target(self):
        for path in (None, Path('relative.json'), self.repo / 'ledger.json', self.base / 'missing.json'):
            with self.subTest(path=path):
                self.config('jev', submission_ledger=path)
                result = self.invoke('files')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('ledger', result.stdout)

    def test_task_file_is_exact_bounded_utf8_run_file_without_links(self):
        self.config('local')
        self.write('a.py', 'def target():\n    return 1\n')
        task = self.run / 'task.txt'
        for raw in (b'a' * 65537, b'\xff', b'   '):
            task.write_bytes(raw)
            self.assertIn('task', self.failure('advice', '--task-file', task))
        outside = self.base / 'outside-task.txt'
        outside.write_text('PRIVATE_TASK')
        task.unlink()
        task.symlink_to(outside)
        self.assertNotIn('PRIVATE_TASK', self.failure('advice', '--task-file', task))
        task.unlink()
        task.write_text('target')
        self.assertIn('task', self.failure('advice', '--task-file', outside))
        self.assertIn('invalid', self.failure('advice', '--task', 'target', '--task-file', task))
        self.assertIn('local_only', self.success('advice', '--task-file', task, '--term', 'target'))
        task.write_bytes(b'a' * 65536)
        module = self.module()
        args = module._parser().parse_args(['--run-dir', str(self.run), 'advice', '--task-file', str(task)])
        self.assertEqual(len(module._task(self.run, args).encode()), 65536)

    def test_read_clamps_end_to_eof_with_actual_ranges_and_rejects_start_past_eof(self):
        self.write('a.py', 'one\ntwo\nlast')
        text = self.success('read', 'a.py', 2, 100)
        self.assertIn('a.py:2-3\n2:two\n3:last\n', text)
        self.assertIn('Notice:', text)
        self.assertIn('3', text.split('Notice:', 1)[1])
        self.assertEqual(self.events()[-1]['source_ranges'], [
            {'path': 'a.py', 'start_line': 2, 'source_lines': ['two\n', 'last\n']}])
        self.assertIn('failure', self.failure('read', 'a.py', 4, 100))
        self.assertIn('300', self.failure('read', 'a.py', 1, 301))

    def test_overlap_clamp_notice_uses_actual_eof_instead_of_last_newly_delivered_line(self):
        self.write('a.py', 'one\ntwo\nlast\n')
        self.success('read', 'a.py', 3, 3)
        text = self.success('read', 'a.py', 1, 100)
        self.assertIn('a.py:1-2\n1:one\n2:two\n', text)
        self.assertIn('clamped to EOF line 3', text)

    def test_followup_advice_diagnostic_keeps_list_cost_within_budget(self):
        ledger = self.base / 'submissions.json'
        ledger.write_text(json.dumps({'scope': 'synthetic', 'candidate_submission_limit': 1200,
                                     'submitted_total': 45, 'batches': []}))
        self.config('jev', submission_ledger=ledger)
        self.write('a.py', 'def target():\n    return 1\n')
        module = self.module()
        fake = lambda *a, **k: {'scores': {}, 'status': 'unavailable', 'diagnostics': [], 'calls': 0}
        argv = ['--run-dir', str(self.run), 'advice', '--task', 'target', '--term', 'target',
                '--list-budget', '256']
        with patch.object(module.pipeline, 'score_candidates', fake):
            for _ in range(2):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(module.main(argv), 0, output.getvalue())
        plan = json.loads((self.run / 'plan-0002.json').read_text())
        visible_cost = math.ceil(len(self.events()[-1]['text'].encode('utf-8')) / 3)
        self.assertEqual(plan['metrics']['list_tokens'], visible_cost)
        self.assertLessEqual(visible_cost, 256)

    def test_heading_search_delivers_full_lines_with_one_header_per_file(self):
        self.write('a.py', 'alpha and rest\nalpha again\n')
        self.write('b.py', 'other\nalpha final\n')
        text = self.success('search', 'alpha', '--heading')
        self.assertEqual(text, 'a.py\n1:alpha and rest\n2:alpha again\nb.py\n2:alpha final\n')
        self.assertEqual(self.events()[-1]['source_ranges'], [
            {'path': 'a.py', 'start_line': 1, 'source_lines': ['alpha and rest\n']},
            {'path': 'a.py', 'start_line': 2, 'source_lines': ['alpha again\n']},
            {'path': 'b.py', 'start_line': 2, 'source_lines': ['alpha final\n']}])
        self.assertIn('--heading', self.events()[-1]['request_argv'])


if __name__ == "__main__":
    unittest.main()
