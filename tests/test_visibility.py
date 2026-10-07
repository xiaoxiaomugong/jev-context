import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from jev_context.tokens import TokenCounter


class VisibilityTests(unittest.TestCase):
    def test_costs_separate_lists_initial_supplement_failure_and_repeats(self):
        from jev_context.tokens import VisibilityLedger
        counter = TokenCounter(use_tokenizer=False)
        ledger = VisibilityLedger(counter)
        ledger.record("list", "advice\n")
        ledger.record("initial_code", "a.py:1\n1: x = 1\n", path="a.py", start_line=1, source_lines=["x = 1\n"])
        ledger.record("supplemental_code", "a.py:1\n1: x = 1\n", path="a.py", start_line=1, source_lines=["x = 1\n"])
        ledger.record("search_failure", "No matches\n")
        summary = ledger.summary()
        self.assertEqual(summary["by_kind"]["list"], counter.count("advice\n"))
        self.assertEqual(summary["by_kind"]["initial_code"], summary["by_kind"]["supplemental_code"])
        self.assertEqual(summary["code_range_union_tokens"], counter.count("x = 1\n"))
        self.assertEqual(summary["repeat_output_tokens"], counter.count("a.py:1\n1: x = 1\n"))
        self.assertEqual(summary["visible_text_tokens"], sum(summary["by_kind"].values()))

    def test_changed_source_line_is_rejected_for_deduplication(self):
        from jev_context.tokens import VisibilityLedger
        ledger = VisibilityLedger(TokenCounter(use_tokenizer=False))
        ledger.record("initial_code", "old", path="a.py", start_line=1, source_lines=["old\n"])
        with self.assertRaises(ValueError):
            ledger.record("supplemental_code", "new", path="a.py", start_line=1, source_lines=["new\n"])

    def test_search_ranges_join_file_read_union_without_dropping_output_cost(self):
        from jev_context.tokens import VisibilityLedger
        counter = TokenCounter(use_tokenizer=False)
        ledger = VisibilityLedger(counter)
        ledger.record("search_output", "a.py:1:x = 1\nb.py:3:y = 2\n", source_ranges=[
            dict(path="a.py", start_line=1, source_lines=["x = 1\n"]),
            dict(path="b.py", start_line=3, source_lines=["y = 2\n"])])
        ledger.record("supplemental_code", "a.py:1:x = 1\n", path="a.py", start_line=1, source_lines=["x = 1\n"])
        summary = ledger.summary()
        self.assertEqual(summary["code_range_union_tokens"], counter.count("x = 1\n") + counter.count("y = 2\n"))
        self.assertGreater(summary["visible_text_tokens"], summary["code_range_union_tokens"])

    def test_partial_overlapping_code_outputs_have_separate_repeat_metric(self):
        from jev_context.tokens import VisibilityLedger
        counter = TokenCounter(use_tokenizer=False)
        ledger = VisibilityLedger(counter)
        ledger.record("initial_code", "first output", path="a.py", start_line=1, source_lines=["one\n", "two\n"])
        ledger.record("supplemental_code", "second output", path="a.py", start_line=2, source_lines=["two\n", "three\n"])
        summary = ledger.summary()
        self.assertEqual(summary["repeat_output_tokens"], 0)
        self.assertEqual(summary["repeated_source_tokens"], counter.count("two\n"))
        self.assertEqual(summary["returned_source_tokens"], counter.count("one\ntwo\n") + counter.count("two\nthree\n"))
