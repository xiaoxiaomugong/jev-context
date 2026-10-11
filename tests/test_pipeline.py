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
    def test_default_never_starts_external_scorer(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo(Path(tmp))
            with patch('jev_context.pipeline.score_candidates', side_effect=AssertionError('network scoring')):
                result = run(Path(tmp), 'retry', ['retry'])
            self.assertEqual(result['status'], 'local_only')
            self.assertEqual(result['metrics']['jev_calls'], 0)

    def test_snapshot_io_and_partial_retrieval_state_are_visible(self):
        from jev_context.retrieval import collect
        with tempfile.TemporaryDirectory() as tmp:
            self.repo(Path(tmp))
            pool = collect(Path(tmp), 'retry', ['retry'])
            pool['status'] = 'degraded'
            with patch('jev_context.pipeline.collect', return_value=pool):
                result = run(Path(tmp), 'retry', ['retry'])
            self.assertEqual(result['metrics']['retrieval_status'], 'degraded')
            self.assertGreater(result['metrics']['source_capture_bytes'], 0)
            self.assertEqual(result['metrics']['source_capture_bytes'], result['metrics']['snapshot_written_bytes'])
            self.assertIn('degraded', render(public_report(result)))

    def test_short_cards_preserve_lf_line_numbers_with_unicode_separators(self):
        from jev_context.pipeline import _jev_cards
        from jev_context.tokens import TokenCounter
        from test_selection import card
        c = card('core', 1, 2)
        c.update(excerpt='# ' + '\u2028' * 40 + '\ndef retry(): return 7\n',
                 excerpt_start_line=1, excerpt_end_line=2, anchor_line=2)
        result = _jev_cards([c], TokenCounter(use_tokenizer=False))[0]
        self.assertIn('def retry()', result['excerpt'])
        self.assertEqual(result['excerpt_end_line'], 2)

    def test_long_line_excerpt_clipping_is_declared_to_scorer_and_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'app.py').write_text('# ' + 'x' * 2000 + ' retry\n')
            def scorer(task, candidates, spec_path, **kwargs):
                self.assertTrue(candidates[0]['excerpt_truncated'])
                return dict(scores={}, status='error', diagnostics=[], calls=len(candidates), completed=0)
            with patch('jev_context.pipeline.score_candidates', scorer):
                result = run(Path(tmp), 'retry', ['retry'], local_only=False)
            self.assertTrue(any('clipped' in d for d in result['diagnostics']))

    def test_check_needs_no_task_and_reports_required_and_optional_tools(self):
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts/jev-context.py'), '--check'],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report['ready'])
        self.assertIn('rg', report)
        self.assertEqual(report['default_mode'], 'local')
        self.assertIn('jev_optional', report)

    def test_source_changed_during_scoring_rejects_stale_advice(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo(Path(tmp))
            def scorer(task, candidates, spec_path, **kwargs):
                (Path(tmp) / 'app.py').write_text('unrelated = 1\n')
                return dict(scores={}, status='error', diagnostics=[], calls=len(candidates), completed=0)
            with patch('jev_context.pipeline.score_candidates', scorer):
                with self.assertRaisesRegex(ValueError, 'source changed'):
                    run(Path(tmp), 'retry', ['retry'], local_only=False)

    def test_save_plan_binds_repo_and_source_without_code_and_refuses_repo_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = base / 'repo'
            p.mkdir()
            self.repo(p)
            plan = base / 'plan.json'
            args = [sys.executable, str(ROOT / 'scripts/jev-context.py'), '--repo', str(p),
                    '--task', 'retry', '--term', 'retry', '--save-plan', str(plan)]
            proc = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            saved = json.loads(plan.read_text())
            self.assertEqual(saved['repo'], str(p.resolve()))
            self.assertEqual(len(saved['source_hashes']['app.py']), 64)
            self.assertNotIn('CANDIDATE_BODY_SENTINEL', plan.read_text())
            args[-1] = str(p / 'plan.json')
            proc = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse((p / 'plan.json').exists())

    def test_markdown_preserves_long_path_and_reports_text_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            path = p / ('directory' * 15)
            path.mkdir()
            name = 'module' * 10 + '.js'
            (path / name).write_text('function retry() { return 1; }\n')
            result = run(p, 'retry', ['retry'], local_only=True)
            text = render(public_report(result))
            self.assertIn((path / name).relative_to(p).as_posix(), text)
            self.assertIn('text_fallback', text)

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
                result = run(Path(tmp), "修复重试", ["retry"], jev_timeout=0.1, local_only=False)
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
                        result = run(Path(tmp), "修复重试", ["retry"], local_only=False)
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
                result = run(Path(tmp), "修复重试", ["retry"], local_only=False)
            self.assertEqual(result["status"], "jev")
            self.assertEqual(result["metrics"].get("scoring_status"), "ok")

    def test_unrecognized_scoring_status_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo(Path(tmp))
            with patch("jev_context.pipeline.score_candidates", return_value=dict(
                scores={}, status="PRIVATE_SCORER_STATUS_SENTINEL", diagnostics=[], calls=0, completed=0)):
                result = run(Path(tmp), "修复重试", ["retry"], local_only=False)
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
            debug = p.parent / (p.name + '-debug')
            self.addCleanup(__import__('shutil').rmtree, debug, True)
            first = run(p, "retry", ["retry"], local_only=True, debug_dir=debug)
            self.assertTrue(list(debug.glob("run-*/candidates.jsonl")))
            second = run(p, "retry", ["retry"], local_only=True, debug_dir=debug)
            self.assertFalse(any(c["path"].startswith("debug/") for c in second["candidates"]))

    def test_debug_never_mutates_target_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            self.repo(p)
            with self.assertRaisesRegex(ValueError, 'outside'):
                run(p, 'retry', ['retry'], debug_dir=p / 'debug')
            self.assertFalse((p / 'debug').exists())

    def test_missing_rg_has_nonzero_retrieval_and_check_exit(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            self.repo(Path(tmp))
            environment = dict(os.environ, PATH=tmp)
            for args in (['--check'], ['--repo', tmp, '--task', 'retry']):
                proc = subprocess.run([sys.executable, str(ROOT / 'scripts/jev-context.py'), *args],
                    capture_output=True, text=True, env=environment)
                self.assertEqual(proc.returncode, 2)
                self.assertIn('rg', proc.stdout)

    def test_check_refuses_unavailable_safe_file_interfaces(self):
        code = ('import os,runpy,sys; del os.O_NOFOLLOW; '
                'sys.path.insert(0,sys.argv[1]); sys.argv=[sys.argv[2],"--check"]; '
                'runpy.run_path(sys.argv[0],run_name="__main__")')
        proc = subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts'),
                              str(ROOT / 'scripts/jev-context.py')], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertFalse(json.loads(proc.stdout)['safe_reading_available'])


if __name__ == "__main__":
    unittest.main()
