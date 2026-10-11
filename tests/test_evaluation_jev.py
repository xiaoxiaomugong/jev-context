"""Evaluation guards use synthetic cards and never invoke an installed CLI."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def answer(score=3):
    return {"score": score, "confidence": .8, "probabilities": [0, 0, 0, 1, 0],
            "uncertain": False, "source": "jev"}


class EvaluationJevTests(unittest.TestCase):
    def setUp(self):
        self.module_path = Path(__file__).resolve().parents[1] / "scripts/evaluation_jev.py"
        self.assertTrue(self.module_path.exists(), "evaluation guard has not been implemented")
        self.guard_module = importlib.import_module("evaluation_jev")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.run = self.base / "run"
        self.run.mkdir()
        self.ledger = self.base / "submissions.json"
        self.ledger.write_text(json.dumps({"scope": "synthetic-test", "candidate_submission_limit": 1200,
                                           "submitted_total": 45, "batches": []}))
        self.rules = self.base / "rules.json"
        self.rules.write_text('{"questions":[]}')
        self.task = "完整中文问题\n包含第二行和 `shell` $literal"
        self.cards = [{"id": "a", "excerpt": "PRIVATE_CARD", "path": "a.py"},
                      {"id": "b", "excerpt": "other", "path": "b.py"}]

    def guard(self, run=None):
        guard = self.guard_module.FirstBatchGuard(self.repo, run or self.run, self.ledger, self.task)
        self.assertTrue(guard.claim())
        return guard

    def saved(self):
        return json.loads((self.run / "scoring-batch.json").read_text())

    def score(self, guard, fake, cards=None):
        return guard.score_candidates(self.task, self.cards if cards is None else cards,
                                      self.rules, scorer=fake)

    def test_partial_scores_charge_submitted_items_without_inventing_missing_scores(self):
        guard = self.guard()
        result = self.score(guard, lambda *a, **k: {"scores": {"a": answer()}, "status": "partial",
                               "diagnostics": ["jev_missing_valid_results"], "calls": 2, "completed": 1})
        self.assertEqual(set(result["scores"]), {"a"})
        self.assertEqual(result["scores"]["a"]["score"], 3)
        ledger = json.loads(self.ledger.read_text())
        self.assertEqual(ledger["submitted_total"], 47)
        self.assertEqual(ledger["reserved_total"], 0)
        batch = self.saved()
        self.assertEqual((batch["n"], batch["calls"], batch["validated_scores"], batch["status"]),
                         (2, 2, 1, "partial"))
        self.assertEqual(batch["scores"], result["scores"])
        self.assertIsNone(batch["http_requests"])
        self.assertIsNone(batch["provider_cost"])
        self.assertNotIn("PRIVATE_CARD", json.dumps(batch))
        canonical_task = json.dumps(self.task, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()
        canonical_cards = json.dumps(self.cards, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(batch["task_sha256"], hashlib.sha256(canonical_task).hexdigest())
        self.assertEqual(batch["cards_sha256"], hashlib.sha256(canonical_cards).hexdigest())
        self.assertEqual(batch["rules_sha256"], hashlib.sha256(self.rules.read_bytes()).hexdigest())
        self.assertFalse(self.guard_module.FirstBatchGuard(self.repo, self.run, self.ledger, self.task).claim())

    def test_full_card_body_changes_fingerprint_even_with_identical_ids(self):
        fake = lambda *a, **k: {"scores": {}, "status": "unavailable", "diagnostics": [], "calls": 0}
        self.score(self.guard(), fake)
        first = self.saved()["cards_sha256"]
        second_run = self.base / "second-run"
        second_run.mkdir()
        cards = [dict(card, excerpt="changed") for card in self.cards]
        self.score(self.guard(second_run), fake, cards)
        second = json.loads((second_run / "scoring-batch.json").read_text())["cards_sha256"]
        self.assertNotEqual(first, second)

    def test_candidate_and_global_budget_preflight_reject_before_scorer(self):
        def forbidden(*args, **kwargs):
            self.fail("budget rejection must precede external scoring")
        for cards, submitted, limit in (([{"id": str(i)} for i in range(61)], 45, 1200),
                                         (self.cards, 1199, 1200), (self.cards, 45, 46)):
            with self.subTest(submitted=submitted, limit=limit, n=len(cards)):
                (self.run / "scoring-batch.json").unlink(missing_ok=True)
                self.ledger.write_text(json.dumps({"scope": "test", "candidate_submission_limit": limit,
                                                  "submitted_total": submitted, "batches": []}))
                with self.assertRaisesRegex(ValueError, "budget"):
                    self.score(self.guard(), forbidden, cards)
                self.assertEqual(self.saved()["status"], "budget_rejected")
                self.assertEqual(json.loads(self.ledger.read_text())["submitted_total"], submitted)

    def test_unavailable_with_zero_calls_releases_reservation_and_never_retries(self):
        guard = self.guard()
        def fake(*a, **k):
            ledger = json.loads(self.ledger.read_text())
            self.assertEqual(ledger["reserved_total"], 2)
            self.assertEqual(self.saved()["status"], "reserved")
            return {"scores": {}, "status": "unavailable", "diagnostics": ["jev_cli_unavailable"], "calls": 0}
        result = self.score(guard, fake)
        self.assertEqual(result["calls"], 0)
        self.assertEqual(json.loads(self.ledger.read_text())["reserved_total"], 0)
        with self.assertRaisesRegex(ValueError, "attempt"):
            self.score(guard, fake)

    def test_scorer_exception_retains_reservation_and_hides_outputs_and_exception(self):
        guard = self.guard()
        def fake(*a, **k):
            print("CREDENTIAL_SECRET PRIVATE_CARD")
            print("CREDENTIAL_SECRET", file=sys.stderr)
            raise RuntimeError("CREDENTIAL_SECRET PRIVATE_CARD")
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaisesRegex(ValueError, "reserved") as caught:
                self.score(guard, fake)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        self.assertNotIn("SECRET", str(caught.exception))
        ledger = json.loads(self.ledger.read_text())
        self.assertEqual((ledger["submitted_total"], ledger["reserved_total"]), (45, 2))
        self.assertEqual(self.saved()["status"], "reserved_unknown")
        self.assertIsNone(self.saved()["calls"])
        self.assertNotIn("SECRET", self.ledger.read_text() + json.dumps(self.saved()))

    def test_unchecked_scores_and_diagnostics_do_not_cross_adapter_boundary(self):
        result = self.score(self.guard(), lambda *a, **k: {"scores": {
            "a": dict(answer(), score=99), "unknown": answer()}, "status": "PRIVATE_CARD",
            "diagnostics": ["PRIVATE_CARD", "CREDENTIAL_SECRET"], "calls": 2})
        self.assertEqual(result["scores"], {})
        self.assertEqual(result["status"], "error")
        self.assertNotIn("PRIVATE_CARD", json.dumps(result) + json.dumps(self.saved()))

    def test_unknown_status_discards_claimed_success_and_nonstring_status_is_safe(self):
        for status in ('PRIVATE_CARD', ['PRIVATE_CARD']):
            with self.subTest(status=status):
                (self.run / 'scoring-batch.json').unlink(missing_ok=True)
                result = self.score(self.guard(), lambda *a, **k: {'scores': {'a': answer()},
                                      'status': status, 'diagnostics': [], 'calls': 2})
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['scores'], {})
                self.assertNotIn('PRIVATE_CARD', json.dumps(self.saved()))

    def test_invalid_call_count_preserves_conservative_reservation(self):
        for calls in (True, -1, 3, float('nan'), 10**500):
            with self.subTest(kind=type(calls).__name__):
                (self.run / 'scoring-batch.json').unlink(missing_ok=True)
                self.ledger.write_text(json.dumps({'scope': 'test', 'candidate_submission_limit': 1200,
                                                  'submitted_total': 45, 'batches': []}))
                with self.assertRaisesRegex(ValueError, 'reserved'):
                    self.score(self.guard(), lambda *a, **k: {'scores': {}, 'status': 'error',
                                                             'calls': calls})
                self.assertEqual(json.loads(self.ledger.read_text())['reserved_total'], 2)
                self.assertEqual(self.saved()['status'], 'reserved_unknown')

    def test_recorded_rules_are_the_bytes_consumed_even_if_original_file_changes(self):
        before = self.rules.read_bytes()
        def fake(task, cards, rules_path, **kwargs):
            self.rules.write_text('{"changed": true}')
            self.assertEqual(rules_path.read_bytes(), before)
            self.assertEqual(os.stat(rules_path).st_mode & 0o777, 0o600)
            return {'scores': {}, 'status': 'unavailable', 'diagnostics': [], 'calls': 0}
        self.score(self.guard(), fake)
        self.assertEqual(self.saved()['rules_sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(list(self.run.glob('.scoring-rules-*')), [])

    def test_timeout_retains_successes_and_preserves_timeout_status(self):
        result = self.score(self.guard(), lambda *a, **k: {'scores': {'a': answer(), 'b': answer()},
                              'status': 'timeout', 'diagnostics': [], 'calls': 2})
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(len(result['scores']), 2)
        self.assertEqual(self.saved()['status'], 'timeout')

    def test_failed_score_evidence_save_keeps_reserved_budget(self):
        from unittest.mock import patch
        guard = self.guard()
        actual = guard._save
        def fail_result_save():
            if guard.batch.get('calls') == 2:
                raise OSError('PRIVATE_CARD CREDENTIAL_SECRET')
            return actual()
        with patch.object(guard, '_save', fail_result_save):
            with self.assertRaisesRegex(ValueError, 'reserved'):
                self.score(guard, lambda *a, **k: {'scores': {'a': answer()}, 'status': 'partial',
                                                  'diagnostics': [], 'calls': 2})
        self.assertEqual(json.loads(self.ledger.read_text())['submitted_total'], 45)
        self.assertEqual(json.loads(self.ledger.read_text())['reserved_total'], 2)

    def test_prior_attempt_marker_never_forwards_unchecked_status_or_call_fields(self):
        for status, calls in (('PRIVATE_STATUS_SENTINEL', 0), ('reserved', {'body': 'PRIVATE_BODY_SENTINEL'}),
                              ('ok', True), ('partial', 61)):
            with self.subTest(status=status):
                (self.run / 'scoring-batch.json').write_text(json.dumps({
                    'first_advice_attempted': True, 'status': status, 'calls': calls}))
                guard = self.guard_module.FirstBatchGuard(self.repo, self.run, self.ledger, self.task)
                with self.assertRaisesRegex(ValueError, 'metadata') as caught:
                    guard.claim()
                self.assertNotIn('SENTINEL', str(caught.exception))

    def test_concurrent_reservations_do_not_exceed_shared_limit_and_call_runs_unlocked(self):
        self.ledger.write_text(json.dumps({"scope": "test", "candidate_submission_limit": 47,
                                          "submitted_total": 45, "batches": []}))
        entered, release = threading.Event(), threading.Event()
        failures = []
        first = self.guard()
        def fake(*a, **k):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release missing")
            return {"scores": {}, "status": "error", "diagnostics": [], "calls": 2}
        def work():
            try:
                self.score(first, fake)
            except BaseException as exc:
                failures.append(exc)
        worker = threading.Thread(target=work)
        worker.start()
        self.assertTrue(entered.wait(5))
        try:
            second_run = self.base / "second-run"
            second_run.mkdir()
            with self.assertRaisesRegex(ValueError, "budget"):
                self.score(self.guard(second_run), lambda *a, **k: self.fail("overlapping reserve"))
            ledger = json.loads(self.ledger.read_text())
            self.assertEqual(ledger["submitted_total"] + ledger["reserved_total"], 47)
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])

    def test_symlink_ledger_or_lock_is_refused_without_modifying_sink(self):
        for kind in ("ledger", "lock"):
            with self.subTest(kind=kind):
                sink = self.base / (kind + "-sink")
                sink.write_text(self.ledger.read_text())
                path = self.ledger if kind == "ledger" else Path(str(self.ledger) + ".lock")
                path.unlink(missing_ok=True)
                path.symlink_to(sink)
                before = sink.read_bytes()
                with self.assertRaises(ValueError):
                    self.guard_module.validate_ledger(self.ledger, self.repo)
                self.assertEqual(sink.read_bytes(), before)
                path.unlink()
                if kind == "ledger":
                    self.ledger.write_bytes(before)


if __name__ == "__main__":
    unittest.main()
