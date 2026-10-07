"""Evaluation checks: interval union, explicit labels, and reproducible reports."""

import importlib
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FROZEN_TRIAL = ROOT / "references/runs/2026-10-06/retrieval-ablation/trial-v2/retrieval.py"
FROZEN_BASELINE = ROOT / "references/runs/2026-10-06/retrieval-ablation/baseline/scripts/jev_context/retrieval.py"
sys.path.insert(0, str(ROOT / "scripts"))


class EvaluationTests(unittest.TestCase):
    def evaluator(self):
        try:
            return importlib.import_module("evaluate")
        except ModuleNotFoundError:
            self.fail("scripts/evaluate.py must provide the executable evaluator")

    def test_overlap_coverage_unions_intervals_without_double_credit(self):
        evaluate = self.evaluator()
        keys = [{"path": "entry.py", "start_line": 10, "end_line": 19}]
        ranges = [
            {"path": "entry.py", "start_line": 10, "end_line": 14},
            {"path": "entry.py", "start_line": 12, "end_line": 16},
            {"path": "other.py", "start_line": 10, "end_line": 19},
        ]
        result = evaluate.coverage(keys, ranges)
        self.assertEqual(result["complete_ranges"], 0)
        self.assertEqual(result["total_ranges"], 1)
        self.assertAlmostEqual(result["line_fraction"], 0.7)
        ranges.append({"path": "entry.py", "start_line": 17, "end_line": 19})
        self.assertEqual(evaluate.coverage(keys, ranges)["complete_ranges"], 1)

    def test_task_labels_reject_invalid_ranges_and_unbounded_paths(self):
        evaluate = self.evaluator()
        invalid = {"schema_version": 1, "tasks": [{
            "id": "bad", "task": "问题", "terms": ["error"],
            "key_ranges": [{"path": "../outside.py", "start_line": 0, "end_line": 4}],
        }]}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "tasks.json"
            source.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(ValueError):
                evaluate.load_tasks(source)

    def test_marked_fixture_has_three_chinese_tasks_and_locatable_ranges(self):
        evaluate = self.evaluator()
        tasks = evaluate.load_tasks(ROOT / "references" / "evaluation-tasks.json")
        self.assertGreaterEqual(len(tasks), 3)
        for task in tasks:
            self.assertTrue(task["terms"])
            self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in task["task"]))
            for key in task["key_ranges"]:
                path = ROOT / "tests" / "fixtures" / "shop" / key["path"]
                lines = path.read_text(encoding="utf-8").splitlines()
                self.assertGreaterEqual(key["start_line"], 1)
                self.assertLessEqual(key["end_line"], len(lines))

    def test_evaluation_rejects_labels_missing_from_actual_repo(self):
        evaluate = self.evaluator()
        validate = getattr(evaluate, "validate_labels", None)
        self.assertIsNotNone(validate, "evaluation must validate labels before running variants")
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            tasks = [{"id": "missing", "task": "问题", "terms": ["error"],
                      "key_ranges": [{"path": "absent.py", "start_line": 1, "end_line": 2}]}]
            with self.assertRaisesRegex(ValueError, "label"):
                validate(repo, tasks)

    def test_labels_obey_secret_symlink_and_ignore_boundary_before_read(self):
        evaluate = self.evaluator()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "safe.py").write_text("def safe():\n    return 1\n", encoding="utf-8")
            (repo / ".env").write_text("synthetic placeholder\n", encoding="utf-8")
            (repo / "ignored.py").write_text("ignored = 1\n", encoding="utf-8")
            (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
            (repo / "alias.py").symlink_to(repo / "safe.py")
            original = Path.read_text
            for path in [".env", "alias.py", "ignored.py"]:
                with self.subTest(path=path):
                    reads = []
                    def observed_read(p, *args, **kwargs):
                        reads.append(p)
                        return original(p, *args, **kwargs)
                    tasks = [{"id": "unsafe", "key_ranges": [{"path": path, "start_line": 1, "end_line": 1}]}]
                    with patch.object(Path, "read_text", observed_read):
                        with self.assertRaisesRegex(ValueError, "label"):
                            evaluate.validate_labels(repo, tasks)
                    self.assertNotIn(repo / path, reads)

    def test_offline_report_exercises_pipeline_and_labels_mock(self):
        evaluate = self.evaluator()
        report = evaluate.evaluate(
            ROOT / "tests" / "fixtures" / "shop",
            evaluate.load_tasks(ROOT / "references" / "evaluation-tasks.json"),
            read_budget=800,
            offline_responses=ROOT / "tests" / "fixtures" / "jev-responses.json",
        )
        self.assertEqual(report["supplemental_read_cost"], "not_measured")
        self.assertEqual(report["jev_mode"], "offline_fixed_responses_mock")
        self.assertEqual(len(report["tasks"]), 3)
        for task in report["tasks"]:
            self.assertEqual(set(task["variants"]), {"A", "B", "C"})
            for name, variant in task["variants"].items():
                self.assertGreaterEqual(variant["candidate_recall"]["line_fraction"], 0)
                self.assertLessEqual(variant["budget_key_coverage"]["line_fraction"], 1)
                self.assertLessEqual(variant["reading_tokens"], 640)
                self.assertGreaterEqual(variant["list_tokens"], 0)
                self.assertGreaterEqual(variant["latency_seconds"], 0)
                if name == "C":
                    self.assertEqual(variant["jev_integration"], "mock_not_external")
                    self.assertEqual(variant["jev_calls"], 0)
                    self.assertNotEqual(variant["status"], "jev")

    def test_cli_emits_metrics_without_candidate_source_or_fixture_execution(self):
        evaluate = self.evaluator()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            source = repo / "entry.py"
            source.write_text(
                'raise RuntimeError("SOURCE_BODY_SENTINEL_DO_NOT_ECHO")\n'
                'def invoice_rounding():\n    return "never execute this file"\n',
                encoding="utf-8",
            )
            tasks = base / "tasks.json"
            tasks.write_text(json.dumps({"schema_version": 1, "tasks": [{
                "id": "controlled", "task": "定位发票金额", "terms": ["invoice_rounding"],
                "key_ranges": [{"path": "entry.py", "start_line": 2, "end_line": 3}],
            }]}), encoding="utf-8")
            import io
            output = io.StringIO()
            with patch("sys.stdout", output):
                exit_code = evaluate.main([
                    "--tasks", str(tasks), "--repo", str(repo),
                    "--offline-responses", str(ROOT / "tests" / "fixtures" / "jev-responses.json"),
                ])
            self.assertEqual(exit_code, 0)
            self.assertNotIn("SOURCE_BODY_SENTINEL", output.getvalue())
            report = json.loads(output.getvalue())
            self.assertEqual(report["tasks"][0]["variants"]["B"]["budget_key_coverage"]["complete_ranges"], 1)

    def controlled_repo(self, base):
        repo = base / "repo"
        repo.mkdir()
        (repo / "entry.py").write_text(
            "def invoice_first():\n    return 11\n\n"
            "def invoice_second():\n    return 22\n\n"
            "def invoice_third():\n    return 33\n", encoding="utf-8")
        return repo, [{"id": "controlled", "task": "Locate invoice calculation",
                       "terms": ["invoice"], "paths": [], "symbols": [], "split": "holdout",
                       "key_ranges": [{"path": "entry.py", "start_line": 7, "end_line": 8}]}]

    def test_budget_scan_freezes_paired_snapshot_and_scores_each_task_once(self):
        evaluate = self.evaluator()
        from jev_context import pipeline
        with tempfile.TemporaryDirectory() as directory:
            repo, tasks = self.controlled_repo(Path(directory))
            collect_calls, score_inputs = [], []
            actual_collect = pipeline.collect
            def collect(*args, **kwargs):
                collect_calls.append((args, kwargs))
                return actual_collect(*args, **kwargs)
            def scorer(task, cards, spec_path, **kwargs):
                score_inputs.append((task, deepcopy(cards)))
                return {"scores": {c["id"]: {"score": 4.0 if c["symbol"] == "invoice_third" else 0.0,
                        "confidence": 1.0, "probabilities": [0, 0, 0, 0, 1], "uncertain": False,
                        "source": "jev"} for c in cards}, "calls": len(cards),
                        "completed": len(cards), "status": "ok", "diagnostics": []}
            with patch.object(pipeline, "collect", collect), patch.object(pipeline, "score_candidates", scorer):
                report = evaluate.evaluate(repo, tasks, read_budget=64, budgets=[32, 64, 128, 256])
            self.assertEqual(len(collect_calls), 2, "A recalls separately; paired B/C recall exactly once")
            self.assertEqual(len(score_inputs), 1, "budget scans must reuse one scoring result")
            self.assertNotIn("key_ranges", json.dumps(collect_calls, default=str))
            self.assertNotIn("key_ranges", json.dumps(score_inputs))
            task = report["tasks"][0]
            self.assertEqual(task["variants"]["B"]["candidate_fingerprint"], task["variants"]["C"]["candidate_fingerprint"])
            self.assertEqual(task["variants"]["B"]["file_content_fingerprint"], task["variants"]["C"]["file_content_fingerprint"])
            self.assertEqual(len(task["budget_rows"]), 4)
            self.assertEqual(task["scoring_measurement"]["submitted_candidates"], 3)
            self.assertIsNone(task["scoring_measurement"]["api_request_count"])
            self.assertIsNone(task["scoring_measurement"]["provider_retries"])
            for row in task["budget_rows"]:
                self.assertEqual(row["capacity_tokens"], int(row["read_budget"] * .8))
                for name in ("B", "C"):
                    variant = row["variants"][name]
                    self.assertLessEqual(variant["reading_tokens"], row["capacity_tokens"])
                    self.assertTrue(variant["list_cap_disabled"])
            self.assertEqual(report["submission_preflight"]["planned_submitted_candidates"], 3)

    def test_submission_cap_preflights_entire_batch_before_any_scoring(self):
        evaluate = self.evaluator()
        with tempfile.TemporaryDirectory() as directory:
            repo, tasks = self.controlled_repo(Path(directory))
            second = deepcopy(tasks[0])
            second["id"] = "second"
            tasks.append(second)
            with patch("jev_context.pipeline.score_candidates", side_effect=AssertionError("must preflight first")):
                with self.assertRaisesRegex(ValueError, "submission.*cap|submitted.*limit"):
                    evaluate.evaluate(repo, tasks, budgets=[32, 64], max_submitted=5)

    def test_replay_uses_frozen_scores_and_refuses_changed_inputs(self):
        evaluate = self.evaluator()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, tasks = self.controlled_repo(base)
            snapshots = base / "snapshots"
            first = evaluate.evaluate(repo, tasks, read_budget=64, budgets=[32, 64], snapshot_dir=snapshots,
                offline_responses=ROOT / "tests" / "fixtures" / "jev-responses.json")
            manifest = json.loads((snapshots / "manifest.json").read_text())
            self.assertNotIn("key_ranges", json.dumps(manifest))
            for path in snapshots.glob("task-*.json"):
                self.assertNotIn("key_ranges", path.read_text())
            with patch("jev_context.pipeline.score_candidates", side_effect=AssertionError("no scorer in replay")), \
                 patch("jev_context.pipeline.collect", side_effect=AssertionError("replay frozen collection")):
                replay = evaluate.evaluate(repo, tasks, read_budget=64, budgets=[32, 64], snapshot_dir=snapshots, replay=True)
            self.assertIn("replay", replay["jev_mode"])
            self.assertEqual(first["tasks"][0]["budget_rows"], replay["tasks"][0]["budget_rows"])
            self.assertEqual(replay["submission_preflight"]["submitted_this_run"], 0)
            changed = deepcopy(tasks)
            changed[0]["task"] += " changed"
            with self.assertRaisesRegex(ValueError, "snapshot.*mismatch|replay.*mismatch"):
                evaluate.evaluate(repo, changed, snapshot_dir=snapshots, replay=True)
            with self.assertRaisesRegex(ValueError, "snapshot.*mismatch|replay.*mismatch"):
                evaluate.evaluate(repo, tasks, candidate_limit=2, snapshot_dir=snapshots, replay=True)
            (repo / "entry.py").write_text((repo / "entry.py").read_text() + "# file changed\n")
            with self.assertRaisesRegex(ValueError, "snapshot.*mismatch|replay.*mismatch"):
                evaluate.evaluate(repo, tasks, snapshot_dir=snapshots, replay=True)

    def test_replay_refuses_changed_repository_tree_and_tampered_candidate_snapshot(self):
        evaluate = self.evaluator()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, tasks = self.controlled_repo(base)
            snapshots = base / "snapshots"
            evaluate.evaluate(repo, tasks, snapshot_dir=snapshots,
                offline_responses=ROOT / "tests" / "fixtures" / "jev-responses.json")
            other = base / "other"
            other.mkdir()
            (other / "entry.py").write_bytes((repo / "entry.py").read_bytes())
            portable = evaluate.evaluate(other, tasks, snapshot_dir=snapshots, replay=True)
            self.assertTrue(portable["reproducibility"]["replay_validated"])
            (other / "changed.py").write_text("tree_changed = True\n")
            with self.assertRaisesRegex(ValueError, "snapshot.*mismatch|replay.*mismatch"):
                evaluate.evaluate(other, tasks, snapshot_dir=snapshots, replay=True)
            snapshot = next(snapshots.glob("task-*.json"))
            saved = json.loads(snapshot.read_text())
            saved["paired"]["candidates"][0]["excerpt"] += " altered"
            snapshot.write_text(json.dumps(saved))
            with self.assertRaisesRegex(ValueError, "snapshot.*mismatch|replay.*mismatch"):
                evaluate.evaluate(repo, tasks, snapshot_dir=snapshots, replay=True)

    def test_candidate_recall_before_truncation_and_full_pool_fit_are_reported(self):
        evaluate = self.evaluator()
        with tempfile.TemporaryDirectory() as directory:
            repo, tasks = self.controlled_repo(Path(directory))
            report = evaluate.evaluate(repo, tasks, read_budget=8000, budgets=[32, 8000], candidate_limit=1,
                offline_responses=ROOT / "tests" / "fixtures" / "jev-responses.json")
            variant = report["tasks"][0]["variants"]["B"]
            self.assertEqual(variant["candidate_recall_pre_truncation"]["complete_fraction"], 1)
            self.assertEqual(variant["candidate_recall"]["complete_fraction"], 0)
            self.assertTrue(variant["full_pool_fits"])
            self.assertTrue(variant["selection_uninformative"])
            reference = variant["offline_gold_prefix_reference"]
            self.assertEqual(reference["basis"], "post_hoc_gold_only_not_a_stopping_policy")
            self.assertFalse(reference["full_key_ranges"]["reachable"])
            self.assertIn("holdout", report["paired_summary_by_split"])

    def test_partial_or_timeout_scores_never_claim_complete_external_success(self):
        evaluate = self.evaluator()
        def scorer(task, cards, spec_path, **kwargs):
            return {"scores": {c["id"]: {"score": 3.0, "confidence": .8,
                    "probabilities": [0, 0, 0, 1, 0], "uncertain": False, "source": "jev"}
                    for c in cards}, "calls": len(cards), "completed": len(cards),
                    "status": "timeout", "diagnostics": ["jev_timeout_completed_lines_retained"]}
        with tempfile.TemporaryDirectory() as directory:
            repo, tasks = self.controlled_repo(Path(directory))
            with patch("jev_context.pipeline.score_candidates", scorer):
                report = evaluate.evaluate(repo, tasks, budgets=[32, 64])
            variant = report["tasks"][0]["variants"]["C"]
            self.assertNotEqual(variant["status"], "jev")
            self.assertNotEqual(variant["jev_integration"], "external_jev")
            self.assertEqual(report["tasks"][0]["scoring_measurement"]["validated_successful_candidates"], 3)
            self.assertFalse(report["tasks"][0]["scoring_measurement"]["complete_external_success"])

    def test_existing_partial_snapshot_refuses_before_real_scoring_and_is_preserved(self):
        evaluate = self.evaluator()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, tasks = self.controlled_repo(base)
            snapshots = base / "snapshots"
            snapshots.mkdir()
            artifact = snapshots / "task-0001.json"
            artifact.write_text("preserve incomplete evidence\n")
            with patch("jev_context.pipeline.score_candidates", side_effect=AssertionError("preflight before score")):
                with self.assertRaisesRegex(ValueError, "snapshot.*already|snapshot.*nonempty"):
                    evaluate.evaluate(repo, tasks, snapshot_dir=snapshots)
            self.assertEqual(artifact.read_text(), "preserve incomplete evidence\n")

    def test_replay_refuses_manifest_symlink_and_changed_score_spec(self):
        evaluate = self.evaluator()
        from jev_context import pipeline
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, tasks = self.controlled_repo(base)
            snapshots = base / "snapshots"
            evaluate.evaluate(repo, tasks, snapshot_dir=snapshots,
                offline_responses=ROOT / "tests" / "fixtures" / "jev-responses.json")
            spec = base / "spec.json"
            modified = json.loads(pipeline.SPEC.read_text())
            modified["description"] += " changed scoring rule"
            spec.write_text(json.dumps(modified))
            with patch.object(pipeline, "SPEC", spec):
                with self.assertRaisesRegex(ValueError, "snapshot.*mismatch|replay.*mismatch"):
                    evaluate.evaluate(repo, tasks, snapshot_dir=snapshots, replay=True)
            manifest = snapshots / "manifest.json"
            moved = base / "saved-manifest.json"
            manifest.rename(moved)
            manifest.symlink_to(moved)
            with self.assertRaisesRegex(ValueError, "snapshot.*symlink|manifest.*symlink"):
                evaluate.evaluate(repo, tasks, snapshot_dir=snapshots, replay=True)

    def test_unbounded_public_list_keeps_all_read_ranges_and_counts_exact_render(self):
        evaluate = self.evaluator()
        from jev_context.output import render
        from jev_context.tokens import TokenCounter
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            for part in range(3):
                (repo / f"part{part}.py").write_text("\n".join(
                    f"def invoice_{part}_{item}():\n    return {item}\n" for item in range(10)))
            tasks = [{"id": "many", "task": "Locate invoice entries", "terms": ["invoice"],
                      "key_ranges": [{"path": "part2.py", "start_line": 28, "end_line": 29}]}]
            report = evaluate.evaluate(repo, tasks, read_budget=8000,
                offline_responses=ROOT / "tests" / "fixtures" / "jev-responses.json")
            variant = report["tasks"][0]["variants"]["B"]
            self.assertEqual(len(variant["reading_list"]), 30)
            self.assertGreater(variant["list_tokens"], 1000)
            self.assertEqual(variant["list_tokens"], TokenCounter(use_tokenizer=False).count(render(variant["public_report"])))

    def test_repository_change_during_collection_refuses_before_scoring(self):
        evaluate = self.evaluator()
        from jev_context import pipeline
        with tempfile.TemporaryDirectory() as directory:
            repo, tasks = self.controlled_repo(Path(directory))
            actual_collect = pipeline.collect
            def changing_collect(*args, **kwargs):
                result = actual_collect(*args, **kwargs)
                (repo / "entry.py").write_text((repo / "entry.py").read_text() + "# changed during retrieval\n")
                return result
            with patch.object(pipeline, "collect", changing_collect), \
                 patch.object(pipeline, "score_candidates", side_effect=AssertionError("must reject source drift first")):
                with self.assertRaisesRegex(ValueError, "repository.*changed|frozen.*mismatch"):
                    evaluate.evaluate(repo, tasks)

    def test_cached_content_must_match_original_repository_fingerprints(self):
        evaluate = self.evaluator()
        from jev_context import pipeline
        with tempfile.TemporaryDirectory() as directory:
            repo, tasks = self.controlled_repo(Path(directory))
            actual_collect = pipeline.collect
            def inconsistent_collect(*args, **kwargs):
                result = actual_collect(*args, **kwargs)
                result["files"]["entry.py"][1] = "    return 999\n"
                return result
            with patch.object(pipeline, "collect", inconsistent_collect), \
                 patch.object(pipeline, "score_candidates", side_effect=AssertionError("must reject foreign cache first")):
                with self.assertRaisesRegex(ValueError, "cached.*mismatch|frozen.*mismatch"):
                    evaluate.evaluate(repo, tasks)

    def test_repository_change_during_scoring_preserves_evidence_and_refuses_report(self):
        evaluate = self.evaluator()
        def changing_scorer(task, cards, spec_path, **kwargs):
            (repo / "entry.py").write_text((repo / "entry.py").read_text() + "# changed during scoring\n")
            return {"scores": {}, "calls": len(cards), "completed": 0, "status": "error", "diagnostics": []}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, tasks = self.controlled_repo(base)
            snapshots = base / "snapshots"
            with patch("jev_context.pipeline.score_candidates", changing_scorer):
                with self.assertRaisesRegex(ValueError, "repository.*changed.*scoring|post.scoring"):
                    evaluate.evaluate(repo, tasks, snapshot_dir=snapshots)
            saved = json.loads(next(snapshots.glob("task-*.json")).read_text())
            self.assertTrue(saved["post_scoring_repository_changed"])
            self.assertEqual(saved["scoring_measurement"]["submitted_candidates"], 3)

    def test_gold_beyond_scan_limit_remains_valid_and_reports_scan_omission(self):
        evaluate = self.evaluator()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "a.py").write_text("def invoice_first():\n    return 1\n")
            (repo / "b.py").write_text("def invoice_gold():\n    return 2\n")
            tasks = [{"id": "bounded", "task": "Locate invoice", "terms": ["invoice"],
                      "key_ranges": [{"path": "b.py", "start_line": 1, "end_line": 2}]}]
            with patch("jev_context.retrieval.MAX_FILES", 1):
                report = evaluate.evaluate(repo, tasks,
                    offline_responses=ROOT / "tests" / "fixtures" / "jev-responses.json")
            variant = report["tasks"][0]["variants"]["B"]
            self.assertEqual(variant["candidate_recall_pre_truncation"]["complete_fraction"], 0)
            self.assertEqual(variant["candidate_omissions"][0]["reason"], "scan_limit")

    def test_declared_task_commit_must_match_repository_before_scoring(self):
        evaluate = self.evaluator()
        with tempfile.TemporaryDirectory() as directory:
            repo, tasks = self.controlled_repo(Path(directory))
            tasks[0]["repository_commit"] = "a" * 40
            with patch("jev_context.pipeline.score_candidates", side_effect=AssertionError("wrong version must not score")):
                with self.assertRaisesRegex(ValueError, "declared.*commit|repository.*version"):
                    evaluate.evaluate(repo, tasks)

    def test_nonblank_coverage_preserves_strict_blank_gap_failure(self):
        evaluate = self.evaluator()
        details = getattr(evaluate, "coverage_details", None)
        self.assertIsNotNone(details, "ablation needs separate strict and nonblank coverage")
        keys = [{"path": "entry.py", "start_line": 1, "end_line": 5}]
        ranges = [{"path": "entry.py", "start_line": 1, "end_line": 2},
                  {"path": "entry.py", "start_line": 4, "end_line": 5}]
        files = {"entry.py": ["def first():\n", "    return 1\n", "  \n",
                              "def second():\n", "    return 2\n"]}
        result = details(keys, ranges, files)
        for field, value in evaluate.coverage(keys, ranges).items():
            self.assertEqual(result[field], value)
        self.assertEqual(result["complete_ranges"], 0)
        self.assertEqual(result["nonblank_complete_ranges"], 1)
        self.assertEqual(result["nonblank_line_fraction"], 1)
        self.assertEqual(result["blank_only_incomplete_ranges"], 1)
        self.assertEqual(result.get("blank_only_incomplete_range_labels"), keys)
        ranges.pop()
        self.assertEqual(details(keys, ranges, files)["nonblank_complete_ranges"], 0)

    def test_pairwise_coverage_reports_losses_as_well_as_recoveries(self):
        evaluate = self.evaluator()
        delta = getattr(evaluate, "coverage_delta", None)
        self.assertIsNotNone(delta, "ablation must expose lines lost behind aggregate gains")
        keys = [{"path": "entry.py", "start_line": 1, "end_line": 3},
                {"path": "entry.py", "start_line": 3, "end_line": 4}]
        files = {"entry.py": ["first\n", "\n", "third\n", "fourth\n"]}
        before = [{"path": "entry.py", "start_line": 1, "end_line": 3}]
        after = [{"path": "entry.py", "start_line": 2, "end_line": 4}]
        result = delta(keys, before, after, files)
        self.assertEqual(result["recovered_line_count"], 1)
        self.assertEqual(result["lost_line_count"], 1)
        self.assertEqual(result["recovered_lines"], [{"path": "entry.py", "start_line": 4, "end_line": 4}])
        self.assertEqual(result["lost_lines"], [{"path": "entry.py", "start_line": 1, "end_line": 1}])
        self.assertEqual(result["recovered_complete_ranges"], [keys[1]])
        self.assertEqual(result["lost_complete_ranges"], [keys[0]])
        self.assertEqual(result["recovered_nonblank_line_count"], 1)
        self.assertEqual(result["lost_nonblank_line_count"], 1)

    def test_local_ablation_filters_dev_before_labels_and_never_scores(self):
        evaluate = self.evaluator()
        ablate = getattr(evaluate, "evaluate_retrieval_ablation", None)
        self.assertIsNotNone(ablate, "evaluation needs a local-only ablation entry")
        import retrieval_ablation
        with tempfile.TemporaryDirectory() as directory:
            repo, tasks = self.controlled_repo(Path(directory))
            tasks[0]["split"] = "dev"
            tasks[0]["gold"] = {"human_reviewed": False}
            held = deepcopy(tasks[0])
            held.update(id="never-evaluated", split="holdout",
                        key_ranges=[{"path": "absent.py", "start_line": 1, "end_line": 2}])
            tasks.append(held)
            inputs = []
            actual_load = retrieval_ablation._load_baseline
            def observed_load(path):
                module, source, digest = actual_load(path)
                if source == FROZEN_TRIAL.resolve():
                    actual = module.collect
                    def observed(*args, **kwargs):
                        inputs.append((deepcopy(args[1:]), deepcopy(kwargs)))
                        return actual(*args, **kwargs)
                    module.collect = observed
                return module, source, digest
            with patch.object(retrieval_ablation, "_load_baseline", observed_load), \
                 patch("jev_context.pipeline.score_candidates", side_effect=AssertionError("ablation cannot score")):
                report = ablate(repo, tasks, baseline_retrieval=ROOT / "scripts/jev_context/retrieval.py",
                                trial_retrieval=FROZEN_TRIAL, read_budget=64, budgets=[32, 64], dev_only=True)
            self.assertEqual([row["id"] for row in report["tasks"]], ["controlled"])
            self.assertEqual(set(report["tasks"][0]["variants"]), {"R0", "R1", "R2", "R3"})
            self.assertEqual(report["scoring_totals"]["submitted_candidates"], 0)
            self.assertEqual(report["scoring_totals"]["jev_input_tokens"], 0)
            self.assertFalse(report["tasks"][0]["human_reviewed"])
            self.assertTrue(report["tasks"][0]["baseline_identity_check"]["matches"])
            self.assertNotIn("key_ranges", json.dumps(inputs, default=str))
            self.assertTrue(all(kwargs["trace"] for _, kwargs in inputs))
            self.assertEqual({(kw["resample_hits"], kw["evidence_coverage"]) for _, kw in inputs},
                             {(True, False), (False, True), (True, True)})
            for row in report["tasks"][0]["budget_rows"]:
                self.assertEqual(len(row["pairwise"]), 6)
                for variant in row["variants"].values():
                    self.assertLessEqual(variant["reading_tokens"], int(row["read_budget"] * .8))
                    self.assertEqual(variant["jev_calls"], 0)
                    self.assertIn("nonblank_line_fraction", variant["budget_key_coverage"])

    def test_ablation_entry_preserves_existing_output_and_does_not_echo_source(self):
        entry = None
        try:
            entry = importlib.import_module("ablate_retrieval")
        except ModuleNotFoundError:
            pass
        self.assertIsNotNone(entry, "ablation should have its own evaluation-only entry")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, tasks = self.controlled_repo(base)
            task_file = base / "tasks.json"
            task_file.write_text(json.dumps({"schema_version": 1, "tasks": tasks}))
            output = base / "result.json"
            output.write_text("preserve evidence\n")
            import io
            with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                entry.main(["--repo", str(repo), "--tasks", str(task_file),
                            "--baseline-retrieval", str(ROOT / "scripts/jev_context/retrieval.py"),
                            "--trial-retrieval", str(FROZEN_TRIAL),
                            "--output", str(output)])
            self.assertEqual(output.read_text(), "preserve evidence\n")
            (repo / "entry.py").write_text((repo / "entry.py").read_text() +
                                           '\n# SOURCE_BODY_SENTINEL_DO_NOT_ECHO\n')
            output = base / "fresh.json"
            stdout = io.StringIO()
            with patch("sys.stdout", stdout), \
                 patch("jev_context.pipeline.score_candidates", side_effect=AssertionError("no external scoring")):
                result = entry.main(["--repo", str(repo), "--tasks", str(task_file),
                                     "--baseline-retrieval", str(ROOT / "scripts/jev_context/retrieval.py"),
                                     "--trial-retrieval", str(FROZEN_TRIAL),
                                     "--output", str(output)])
            self.assertEqual(result, 0)
            self.assertNotIn("SOURCE_BODY_SENTINEL", output.read_text())
            self.assertNotIn("SOURCE_BODY_SENTINEL", stdout.getvalue())
            self.assertEqual(json.loads(output.read_text())["scoring_totals"]["submitted_candidates"], 0)

    def test_ablation_line_attribution_locates_each_first_loss_stage(self):
        from retrieval_ablation import _line_attribution
        keys = [{"path": "entry.py", "start_line": 1, "end_line": 5}]
        files = {"entry.py": ["first\n", "second\n", "third\n", "fourth\n", "\n"]}
        stages = {"pre_truncation": [{"path": "entry.py", "start_line": 2, "end_line": 5}],
                  "post_per_file": [{"path": "entry.py", "start_line": 3, "end_line": 5}],
                  "post_global": [{"path": "entry.py", "start_line": 4, "end_line": 5}]}
        selected = [{"path": "entry.py", "start_line": 5, "end_line": 5}]
        result = _line_attribution(keys, stages, files, {}, selected)
        for reason, line in (("hit_expansion_or_range_boundary", 1), ("per_file_candidate_limit", 2),
                             ("global_candidate_limit", 3), ("read_budget", 4)):
            self.assertEqual(result[reason]["lines"], [{"path": "entry.py", "start_line": line, "end_line": line}])
            self.assertEqual(result[reason]["nonblank_line_count"], 1)

    def test_ablation_reports_scan_limited_label_without_expanding_discovery(self):
        evaluate = self.evaluator()
        from jev_context import retrieval
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            (repo / "a.py").write_text("def unrelated():\n    return 0\n")
            (repo / "b.py").write_text("def invoice():\n    return 1\n")
            tasks = [{"id": "bounded", "task": "Locate invoice", "terms": ["invoice"],
                      "key_ranges": [{"path": "b.py", "start_line": 1, "end_line": 2}]}]
            baseline = base / "baseline.py"
            baseline.write_text(Path(retrieval.__file__).read_text().replace("MAX_FILES = 5000", "MAX_FILES = 1"))
            trial = base / "trial.py"
            trial.write_text(FROZEN_TRIAL.read_text().replace("MAX_FILES = 5000", "MAX_FILES = 1"))
            with patch.object(retrieval, "MAX_FILES", 1):
                try:
                    report = evaluate.evaluate_retrieval_ablation(repo, tasks, baseline_retrieval=baseline,
                                                                 trial_retrieval=trial, read_budget=64, budgets=[64])
                except ValueError as error:
                    self.fail(f"safe scan-limited labels must remain measurable: {error}")
            for variant in report["tasks"][0]["variants"].values():
                self.assertEqual(variant["candidate_count"], 0)
                self.assertEqual(variant["scan"]["scanned_count"], 1)
                self.assertEqual(variant["candidate_omissions"][0]["reason"], "scan_limit")
                self.assertEqual(variant["omission_attribution"]["scan_limit"]["nonblank_line_count"], 2)

    def test_loading_archived_retrieval_does_not_write_bytecode_into_baseline(self):
        from retrieval_ablation import _load_baseline
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "retrieval.py"
            source.write_text("baseline_marker = 42\n")
            module, _, _ = _load_baseline(source)
            self.assertEqual(module.baseline_marker, 42)
            self.assertFalse((base / "__pycache__").exists(), "saved baseline must stay read-only")

    def test_archived_trial_isolated_from_legacy_production_collector(self):
        evaluate = self.evaluator()
        from jev_context import retrieval
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, tasks = self.controlled_repo(base)
            trial = base / "trial.py"
            trial.write_text(FROZEN_TRIAL.read_text())
            actual = retrieval.collect
            def legacy(repo, task, terms, *, paths=None, symbols=None, candidate_limit=60,
                       expand=True, ast_enabled=True, trace=False):
                return actual(repo, task, terms, paths=paths, symbols=symbols,
                              candidate_limit=candidate_limit, expand=expand, ast_enabled=ast_enabled, trace=trace)
            with patch.object(retrieval, "collect", legacy):
                try:
                    report = evaluate.evaluate_retrieval_ablation(repo, tasks,
                        baseline_retrieval=ROOT / "scripts/jev_context/retrieval.py",
                        trial_retrieval=trial, read_budget=64, budgets=[64])
                except TypeError as error:
                    self.fail(f"archived trials must run after withdrawing production flags: {error}")
            self.assertTrue(report["tasks"][0]["baseline_identity_check"]["matches"])
            self.assertEqual(report["reproducibility"].get("trial_retrieval_path"), str(trial.resolve()))

    def test_archived_baseline_hit_stage_is_captured_without_changing_candidates(self):
        evaluate = self.evaluator()
        with tempfile.TemporaryDirectory() as directory:
            repo, tasks = self.controlled_repo(Path(directory))
            report = evaluate.evaluate_retrieval_ablation(repo, tasks, baseline_retrieval=FROZEN_BASELINE,
                trial_retrieval=FROZEN_TRIAL, read_budget=64, budgets=[64])
            row = report["tasks"][0]
            self.assertIn("post_hits", row["variants"]["R0"]["stages"])
            self.assertTrue(row["baseline_identity_check"]["matches"])


if __name__ == "__main__":
    unittest.main()
