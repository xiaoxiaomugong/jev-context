import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from jev_context.selection import select
from jev_context.tokens import TokenCounter


def card(cid, start, end, *, path="app.py", pinned=False, score=3.0):
    return dict(id=cid, path=path, start_line=start, end_line=end,
                symbol=cid, kind="function", signature="", excerpt="",
                retrieval_signals=["term: retry"], relations=[],
                local_score=5, pinned=pinned, anchor_line=start,
                symbol_start_line=start, symbol_end_line=end,
                incomplete=False, extraction_mode="python_ast",
                jev_score=score, confidence=0.8, uncertain=False,
                ranking_source="jev", estimated_read_tokens=0)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.counter = TokenCounter(use_tokenizer=False)
        self.lines = ["x = 1234567890\n"] * 100

    def test_overlap_charged_as_union_and_output_ranges_merged(self):
        result = select([card("c1", 1, 10), card("c2", 5, 15)],
                        {"app.py": self.lines}, 1000, self.counter)
        self.assertEqual(len(result["reading_list"]), 1)
        row = result["reading_list"][0]
        self.assertEqual((row["start_line"], row["end_line"]), (1, 15))
        self.assertEqual(result["reading_tokens"], self.counter.count("".join(self.lines[:15])))

    def test_pinned_long_entry_has_readable_slice_and_deferred_range(self):
        c = card("c1", 1, 100, pinned=True)
        c["anchor_line"] = 70
        result = select([c], {"app.py": self.lines}, 100, self.counter)
        self.assertLessEqual(result["reading_tokens"], 80)
        row = result["reading_list"][0]
        self.assertLessEqual(row["start_line"], 70)
        self.assertGreaterEqual(row["end_line"], 70)
        self.assertTrue(row["incomplete"])
        self.assertTrue(result["deferred"])

    def test_explicit_entry_precedes_jev_score_and_uncertain_is_retained(self):
        low = card("c1", 1, 8, pinned=True, score=0.2)
        low["uncertain"] = True
        result = select([card("c2", 1, 8, path="other.py", score=4), low],
                        {"app.py": self.lines, "other.py": self.lines}, 1000, self.counter)
        self.assertEqual(result["reading_list"][0]["candidate_ids"], ["c1"])
        self.assertTrue(result["reading_list"][0]["uncertain"])

    def test_long_core_beats_short_lower_relevance(self):
        core = card("core", 1, 60, score=4)
        short = card("short", 1, 3, path="short.py", score=2)
        result = select([short, core], {"app.py": self.lines, "short.py": self.lines}, 800, self.counter)
        self.assertEqual(result["reading_list"][0]["candidate_ids"], ["core"])
        self.assertLessEqual(result["reading_tokens"], 640)

    def test_tied_candidates_keep_test_and_dependency_coverage(self):
        same = [card("c" + str(i), i * 5 + 1, i * 5 + 5) for i in range(5)]
        test = card("test", 1, 5, path="test_app.py")
        test["kind"] = "test"
        result = select(same + [test], {"app.py": self.lines, "test_app.py": self.lines}, 125, self.counter)
        self.assertTrue(any(r["path"] == "test_app.py" for r in result["reading_list"]))

    def test_local_only_long_core_still_has_readable_anchor_window(self):
        core = card("core", 1, 100, score=None)
        core.update(anchor_line=90, ranking_source="local_fallback")
        result = select([core], {"app.py": self.lines}, 100, self.counter)
        self.assertTrue(result["reading_list"])
        row = result["reading_list"][0]
        self.assertLessEqual(row["start_line"], 90)
        self.assertGreaterEqual(row["end_line"], 90)
        self.assertTrue(row["incomplete"])
        self.assertLessEqual(result["reading_tokens"], 80)
        self.assertEqual(row["scores"], [None])

    def test_ties_keep_import_dependency_before_more_implementation(self):
        from jev_context.selection import ranked
        a = card("a", 1, 4, path="a.py")
        b = card("b", 1, 4, path="b.py")
        dependency = card("dep", 1, 4, path="z.py")
        dependency["relations"] = [{"type": "local_import"}]
        ordered = ranked([a, b, dependency])
        self.assertEqual([c["id"] for c in ordered[:2]], ["a", "dep"])

    def test_double_truncation_defers_full_symbol_not_extracted_range(self):
        core = card("core", 20, 100, pinned=True)
        core.update(symbol_start_line=1, symbol_end_line=100, incomplete=True, anchor_line=95)
        result = select([core], {"app.py": self.lines}, 100, self.counter)
        self.assertEqual((result["deferred"][0]["start_line"], result["deferred"][0]["end_line"]), (1, 100))

    def test_evaluation_fixed_order_and_clipping_ignore_score_value(self):
        from copy import deepcopy
        original = [card("c1", 1, 100, score=None), card("c2", 1, 3, path="short.py", score=None)]
        scored = deepcopy(original)
        scored[0]["jev_score"] = 0.1
        scored[1]["jev_score"] = 4
        files = {"app.py": self.lines, "short.py": self.lines}
        a = select(original, files, 100, self.counter, order=original, trim_by_rank=True)
        b = select(scored, files, 100, self.counter, order=scored, trim_by_rank=True)
        ranges = lambda result: [(r["path"], r["start_line"], r["end_line"]) for r in result["reading_list"]]
        self.assertEqual(ranges(a), ranges(b))
        self.assertTrue(b["reading_list"][0]["incomplete"])

    def test_evaluation_rejects_non_permutation_order(self):
        c = card("c1", 1, 4)
        with self.assertRaises(ValueError):
            select([c], {"app.py": self.lines}, 100, self.counter, order=[])


if __name__ == "__main__":
    unittest.main()
