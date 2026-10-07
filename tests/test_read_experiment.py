"""Logged reader checks use synthetic local files and never call Jev."""

import contextlib
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from jev_context.tokens import TokenCounter


class ExperimentReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        self.run_dir = base / "run"
        self.run_dir.mkdir()
        (self.repo / "entry.py").write_text("def entry():\n    return 7\n", encoding="utf-8")
        self.config = dict(repo=str(self.repo), task_id="synthetic", method="B",
                           initial_ranges=[dict(path="entry.py", start_line=1, end_line=2)])
        (self.run_dir / "config.json").write_text(json.dumps(self.config), encoding="utf-8")

    def invoke(self, *command):
        try:
            helper = importlib.import_module("read_experiment")
        except ModuleNotFoundError:
            self.fail("scripts/read_experiment.py must provide the logged reader")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = helper.main(["--run-dir", str(self.run_dir), *map(str, command)])
        self.assertEqual(err.getvalue(), "")
        events = [json.loads(line) for line in (self.run_dir / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["text"], out.getvalue())
        self.assertEqual(events[-1]["tokens"], TokenCounter(use_tokenizer=False).count(out.getvalue()))
        return status, out.getvalue(), events

    def test_first_configured_read_and_repeat_have_distinct_cost_phases(self):
        status, text, events = self.invoke("read", "entry.py", 1, 2)
        self.assertEqual(status, 0)
        self.assertIn("entry.py:1:def entry():", text)
        self.assertEqual(events[-1]["kind"], "initial_code")
        self.assertEqual(events[-1]["source_lines"], ["def entry():\n", "    return 7\n"])
        status, repeated, events = self.invoke("read", "entry.py", 1, 2)
        self.assertEqual(status, 0)
        self.assertEqual(repeated, text)
        self.assertEqual(events[-1]["kind"], "supplemental_code")
        status, output, events = self.invoke("summary")
        summary = json.loads(output)
        self.assertEqual(summary["visible_text_tokens"], events[0]["tokens"] + events[1]["tokens"])
        self.assertEqual(summary["repeat_output_tokens"], events[1]["tokens"])
        self.assertEqual(summary["code_range_union_tokens"], 9)
        self.assertEqual(events[-1]["kind"], "summary_output")

    def test_supplemental_read_can_use_safe_file_absent_from_initial_list(self):
        (self.repo / "helper.py").write_text("constant = 1\n", encoding="utf-8")
        status, text, events = self.invoke("read", "helper.py", 1, 1)
        self.assertEqual(status, 0)
        self.assertIn("constant = 1", text)
        self.assertEqual(events[-1]["kind"], "supplemental_code")

    def test_supplemental_range_limit_is_enforced_before_source_delivery(self):
        (self.repo / "long.py").write_text("source_line = 1\n" * 268, encoding="utf-8")
        status, text, events = self.invoke("read", "long.py", 1, 268)
        self.assertEqual(status, 1)
        self.assertNotIn("source_line", text)
        self.assertEqual(events[-1]["kind"], "read_failure")

    def test_ignored_file_is_denied_even_when_its_path_looks_safe(self):
        (self.repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
        (self.repo / "ignored.py").write_text("IGNORED_BODY_SENTINEL = 1\n", encoding="utf-8")
        status, text, events = self.invoke("read", "ignored.py", 1, 1)
        self.assertEqual(status, 1)
        self.assertEqual(events[-1]["kind"], "read_failure")
        self.assertNotIn("IGNORED_BODY_SENTINEL", text)

    def test_no_match_is_charged_as_failed_retrieval(self):
        status, text, events = self.invoke("search", "absent_literal")
        self.assertEqual(status, 0)
        self.assertIn("No matches", text)
        self.assertEqual(events[-1]["kind"], "search_failure")

    def test_search_then_read_deduplicates_raw_source_lines(self):
        status, text, events = self.invoke("search", "return 7", "--path", "entry.py")
        self.assertEqual(status, 0)
        self.assertEqual(events[-1].get("source_ranges"), [dict(
            path="entry.py", start_line=2, source_lines=["    return 7\n"])])
        self.invoke("read", "entry.py", 1, 2)
        status, output, events = self.invoke("summary")
        summary = json.loads(output)
        self.assertEqual(summary["code_range_union_tokens"], 9)
        self.assertGreater(summary["visible_text_tokens"], summary["code_range_union_tokens"])

    def test_unsafe_or_invalid_reads_emit_logged_failures_without_source(self):
        (self.repo / ".env").write_text("PRIVATE_VALUE_DO_NOT_SHOW\n", encoding="utf-8")
        (self.repo / "alias.py").symlink_to(self.repo / "entry.py")
        for path, start, end in (("../entry.py", 1, 1), (".env", 1, 1),
                                 ("alias.py", 1, 1), ("entry.py", 0, 1),
                                 ("entry.py", 1, 9), ("entry.py", "bad", 1)):
            with self.subTest(path=path, start=start, end=end):
                status, text, events = self.invoke("read", path, start, end)
                self.assertEqual(status, 1)
                self.assertIn("failure:", text)
                self.assertNotIn("PRIVATE_VALUE", text)
                self.assertNotIn("Traceback", text)
                self.assertEqual(events[-1]["kind"], "read_failure")
                self.assertNotIn("source_lines", events[-1])

    def test_search_has_at_most_thirty_source_hits_and_charges_repeat(self):
        (self.repo / "many.py").write_text("needle = 1\n" * 45, encoding="utf-8")
        status, text, events = self.invoke("search", "needle", "--path", "many.py")
        self.assertEqual(status, 0)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("many.py:")]), 30)
        self.assertIn("bounded", text)
        self.assertEqual(events[-1]["kind"], "search_output")
        self.assertNotIn("source_lines", events[-1])
        status, repeat, events = self.invoke("search", "needle", "--path", "many.py")
        self.assertEqual(status, 0)
        self.assertEqual(repeat, text)
        status, summary, events = self.invoke("summary")
        self.assertEqual(json.loads(summary)["repeat_output_tokens"], events[0]["tokens"])

    def test_search_unavailable_and_missing_config_are_logged_failures(self):
        with patch("jev_context.retrieval.shutil.which", return_value=None):
            status, text, events = self.invoke("search", "entry")
        self.assertEqual(status, 1)
        self.assertEqual(events[-1]["kind"], "search_failure")
        (self.run_dir / "config.json").unlink()
        status, text, events = self.invoke("read", "entry.py", 1, 1)
        self.assertEqual(status, 1)
        self.assertEqual(events[-1]["kind"], "read_failure")


if __name__ == "__main__":
    unittest.main()
