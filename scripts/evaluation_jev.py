"""Explicit evaluation-only, one-attempt Jev scoring and shared item budget.

No credentials, CLI output, card bodies, retries, or score replay are stored.
``calls`` remains the adapter's submitted candidate count, never HTTP usage.
"""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import stat
import threading

from jev_context.jev_adapter import _answer, _canonical, _unique_object, score_candidates
from jev_context.safe_io import open_directory_nofollow
from jev_context.tokens import TokenCounter

MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_BATCH_CANDIDATES = 60
GLOBAL_SUBMISSION_LIMIT = 1200
_PIPELINE_LOCK = threading.RLock()
_STATUSES = {"empty", "invalid_input", "unavailable", "error", "ok", "partial", "timeout"}
_DIAGNOSTICS = {
    "jev_invalid_request", "jev_invalid_candidate_ids_or_json", "jev_cli_unavailable",
    "jev_timeout_output_cleanup_bounded", "jev_timeout_completed_lines_retained",
    "jev_truncated_output", "jev_invalid_json", "jev_invalid_record", "jev_invalid_input_echo",
    "jev_invalid_error_input", "jev_unknown_candidate_id", "jev_duplicate_candidate_id",
    "jev_input_echo_mismatch", "jev_candidate_failed", "jev_invalid_score_fields",
    "jev_output_count_mismatch", "jev_missing_valid_results", "jev_cli_failed",
    "jev_auth_unavailable", "jev_credentials_unavailable",
}


class ScoringFailure(ValueError):
    """Fixed, source-free evaluation failure."""


def _path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ScoringFailure("submission ledger requires an absolute safe path")
    for alias, canonical in ((Path("/tmp"), Path("/private/tmp")),
                             (Path("/var"), Path("/private/var"))):
        if path.is_relative_to(alias) and alias.resolve() == canonical:
            path = canonical / path.relative_to(alias)
    return path


def _read_bytes(parent, name, maximum=MAX_LEDGER_BYTES):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ScoringFailure("scoring metadata must be bounded regular files without links")
        raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ScoringFailure("scoring metadata exceeds its bounded size")
    return raw


def _read_json(parent, name):
    return json.loads(_read_bytes(parent, name).decode("utf-8"), object_pairs_hook=_unique_object)


def _write_json(parent, name, value):
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode('utf-8')
    if len(encoded) > MAX_LEDGER_BYTES:
        raise ScoringFailure("scoring metadata exceeds its bounded size")
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ScoringFailure("scoring metadata must be regular files without links")
    temporary = ".scoring-pending-" + secrets.token_hex(12)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _reserved(value):
    total = 0
    for batch in value["batches"]:
        if not isinstance(batch, dict):
            raise ScoringFailure("submission ledger has invalid batches")
        amount = batch.get("reserved_remaining", batch.get("reserved_candidates", 0)
                           if batch.get("status") in ("reserved", "reserved_unknown") else 0)
        if type(amount) is not int or amount < 0:
            raise ScoringFailure("submission ledger has invalid reservations")
        total += amount
    return total


def _validate(value):
    if (not isinstance(value, dict) or not isinstance(value.get("scope"), str)
            or not value["scope"].strip() or len(value["scope"].encode()) > 1024
            or type(value.get("candidate_submission_limit")) is not int
            or value["candidate_submission_limit"] < 1
            or type(value.get("submitted_total")) is not int or value["submitted_total"] < 0
            or not isinstance(value.get("batches"), list)):
        raise ScoringFailure("submission ledger has invalid required fields")
    reserved = _reserved(value)
    if "reserved_total" in value and (type(value["reserved_total"]) is not int
                                      or value["reserved_total"] != reserved):
        raise ScoringFailure("submission ledger has inconsistent reservations")
    if value["submitted_total"] + reserved > min(GLOBAL_SUBMISSION_LIMIT, value["candidate_submission_limit"]):
        raise ScoringFailure("submission ledger already exceeds candidate budget")
    return value


@contextmanager
def _locked_ledger(path):
    parent = open_directory_nofollow(path.parent)
    lock = None
    try:
        lock = os.open(path.name + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=parent)
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ScoringFailure("submission ledger lock must be regular without links")
        os.fchmod(lock, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = _validate(_read_json(parent, path.name))
        yield parent, value
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ScoringFailure("submission ledger could not be safely opened") from None
    finally:
        if lock is not None:
            os.close(lock)
        os.close(parent)


def validate_ledger(value, repo):
    """Validate an existing ledger; only the separate private lock is created."""
    path = _path(value)
    if path.is_relative_to(_path(repo)):
        raise ScoringFailure("submission ledger must be outside the read-only repository")
    with _locked_ledger(path):
        pass
    return path


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class FirstBatchGuard:
    def __init__(self, repo, run_directory, ledger_path, task):
        self.directory = _path(run_directory)
        self.ledger = validate_ledger(ledger_path, repo)
        self.task = task
        self.batch = dict(first_advice_attempted=True, task_sha256=_hash(task),
                          cards_sha256=None, rules_sha256=None, n=None, calls=None,
                          validated_scores=0, scores={}, status="reserved",
                          http_requests=None, provider_usage=None, provider_cost=None)
        self.claimed = False
        self.scored = False

    def _save(self):
        parent = open_directory_nofollow(self.directory)
        try:
            _write_json(parent, "scoring-batch.json", self.batch)
        finally:
            os.close(parent)

    def _unknown_failure(self):
        self.batch.update(status='reserved_unknown', calls=None, validated_scores=0, scores={})
        try:
            self._save()
        except (OSError, ValueError):
            pass
        return ScoringFailure('scoring attempt failed; candidate budget remains reserved')

    def claim(self):
        """Durably consume the run's first advice before retrieval or scoring."""
        parent = open_directory_nofollow(self.directory)
        try:
            try:
                descriptor = os.open("scoring-batch.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=parent)
            except FileExistsError:
                previous = _read_json(parent, "scoring-batch.json")
                if not isinstance(previous, dict) or previous.get("first_advice_attempted") is not True:
                    raise ScoringFailure("initial scoring attempt metadata is invalid")
                status = previous.get('status')
                calls = previous.get('calls')
                if (not isinstance(status, str) or status not in _STATUSES | {
                        'reserved', 'reserved_unknown', 'budget_rejected', 'error_before_scoring'}
                        or (calls is not None and (type(calls) is not int
                            or not 0 <= calls <= MAX_BATCH_CANDIDATES))):
                    raise ScoringFailure('initial scoring attempt metadata is invalid')
                # A subsequent local request needs only these validated fields;
                # old scores are neither read into ranking nor replayed to cards.
                self.batch = dict(status=status, calls=calls)
                return False
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(self.batch, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)
            self.claimed = True
            return True
        except OSError:
            raise ScoringFailure("initial scoring attempt could not be safely recorded") from None
        finally:
            os.close(parent)

    def score_candidates(self, task, cards, spec_path, *, scorer=score_candidates, **kwargs):
        if not self.claimed or self.scored:
            raise ScoringFailure("initial scoring attempt is already consumed")
        self.scored = True
        if task != self.task or not isinstance(cards, list):
            raise ScoringFailure("initial scoring request does not match its task")
        try:
            cards_hash = _hash(cards)
            ids = [card["id"] for card in cards]
            if any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
                raise ValueError
            rules = _path(spec_path)
            parent = open_directory_nofollow(rules.parent)
            try:
                rules_bytes = _read_bytes(parent, rules.name, 65536)
            finally:
                os.close(parent)
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            raise ScoringFailure("initial scoring cards or rules are invalid") from None
        n = len(cards)
        self.batch.update(cards_sha256=cards_hash, rules_sha256=hashlib.sha256(rules_bytes).hexdigest(), n=n)
        batch_id = secrets.token_hex(16)
        with _locked_ledger(self.ledger) as (parent, ledger):
            if (n > MAX_BATCH_CANDIDATES or ledger["submitted_total"] + _reserved(ledger) + n
                    > min(GLOBAL_SUBMISSION_LIMIT, ledger["candidate_submission_limit"])):
                self.batch.update(status="budget_rejected", calls=0)
                self._save()
                raise ScoringFailure("candidate scoring budget preflight rejected this batch")
            ledger["batches"].append(dict(id=batch_id, preflight_candidates=n, reserved_candidates=n,
                reserved_remaining=n, submitted_candidates=0, status="reserved",
                task_sha256=self.batch["task_sha256"], cards_sha256=cards_hash,
                rules_sha256=self.batch["rules_sha256"], calls=None, validated_scores=0,
                http_requests=None, provider_usage=None, provider_cost=None))
            ledger["reserved_total"] = _reserved(ledger)
            _write_json(parent, self.ledger.name, ledger)
        self.batch["reservation_id"] = batch_id
        self._save()
        snapshot = ".scoring-rules-" + secrets.token_hex(12) + ".json"
        run_parent = open_directory_nofollow(self.directory)
        try:
            descriptor = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=run_parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(rules_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            # Never hold the shared ledger lock during the external process.
            try:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    raw = scorer(task, cards, self.directory / snapshot, **kwargs)
                calls = raw.get("calls") if isinstance(raw, dict) else None
                if type(calls) is not int or not 0 <= calls <= n:
                    raise ValueError
            except BaseException:
                raise self._unknown_failure() from None
            reported_status = raw.get('status')
            valid_status = isinstance(reported_status, str) and reported_status in _STATUSES
            status = reported_status if valid_status else 'error'
            scores = {}
            source = raw.get("scores")
            if isinstance(source, dict) and calls and valid_status:
                for candidate_id, value in source.items():
                    if candidate_id not in ids or not isinstance(value, dict):
                        continue
                    try:
                        validated = _answer(dict(type="score", score=value.get("score"),
                            confidence=value.get("confidence"), probabilities=value.get("probabilities")))
                    except (TypeError, ValueError, OverflowError):
                        validated = None
                    if validated is not None:
                        scores[candidate_id] = validated
            if len(scores) > calls:
                scores = {}
                status = "error"
            if status == "ok" and (calls != n or len(scores) != n):
                status = "partial" if scores else "error"
            diagnostics = [item for item in raw.get("diagnostics", [])
                           if isinstance(item, str) and item in _DIAGNOSTICS] if isinstance(raw.get("diagnostics"), list) else []
            if status == "error" and "jev_missing_valid_results" not in diagnostics:
                diagnostics.append("jev_missing_valid_results")
            result = dict(scores=scores, status=status, diagnostics=diagnostics, calls=calls, completed=len(scores))
            # Persist the score evidence before releasing a conservative reserve.
            # A crash between these writes still blocks all further run attempts.
            self.batch.update(calls=calls, status=status, validated_scores=len(scores), scores=scores)
            try:
                self._save()
            except (OSError, ValueError):
                raise self._unknown_failure() from None
            with _locked_ledger(self.ledger) as (parent, ledger):
                matches = [item for item in ledger["batches"] if item.get("id") == batch_id]
                if len(matches) != 1 or matches[0].get("reserved_remaining") != n:
                    raise ScoringFailure("scoring reservation could not be finalized")
                matches[0].update(reserved_remaining=0, submitted_candidates=calls, calls=calls,
                                  status=status, validated_scores=len(scores))
                ledger["submitted_total"] += calls
                ledger["reserved_total"] = _reserved(ledger)
                _write_json(parent, self.ledger.name, ledger)
            return result
        finally:
            try:
                os.unlink(snapshot, dir_fd=run_parent)
            except FileNotFoundError:
                pass
            os.close(run_parent)

    def finish_without_scoring(self, status="empty"):
        if self.claimed and not self.scored:
            self.batch.update(status=status, calls=0, n=0)
            self._save()


def _bound_followup(pipeline, result):
    """Count the added evaluation notice and keep the existing public list bound."""
    counter = TokenCounter(use_tokenizer=False)
    pipeline._measure_list(result, counter, 'markdown')
    rows = result['reading_list']
    while result['metrics']['list_tokens'] > result['budget']['list_tokens']:
        if result['alternatives']:
            result['alternatives'].pop()
        elif result['deferred']['examples']:
            result['deferred']['examples'].pop()
        elif any(row['evidence'] for row in rows):
            next(row for row in reversed(rows) if row['evidence'])['evidence'] = []
        elif rows:
            index = next((i for i in range(len(rows) - 1, -1, -1) if not rows[i]['pinned']), len(rows) - 1)
            removed = rows.pop(index)
            result['metrics']['reading_tokens'] -= removed['estimated_read_tokens']
            result['deferred']['count'] += 1
            result['deferred']['pinned_count'] += int(removed['pinned'])
        elif len(result['diagnostics']) > 1:
            result['diagnostics'].pop()
        else:
            raise ScoringFailure('list budget cannot fit evaluation status; increase list budget')
        pipeline._measure_list(result, counter, 'markdown')
    for priority, row in enumerate(rows, 1):
        row['priority'] = priority
    pipeline._measure_list(result, counter, 'markdown')


def run_advice(pipeline, repo, directory, ledger_path, task, *, metadata=None, **kwargs):
    """Wrap frozen production retrieval with a single explicit scorer attempt."""
    guard = FirstBatchGuard(repo, directory, ledger_path, task)
    first = guard.claim()
    def record_metadata():
        value = dict(first_advice=first, subsequent_local_only=not first,
            batch_status=guard.batch["status"], batch_calls=guard.batch["calls"],
            task_sha256=_hash(task), http_requests=None, provider_usage=None, provider_cost=None)
        if metadata is not None:
            metadata.update(value)
        return value
    record_metadata()
    if first:
        with _PIPELINE_LOCK:
            original = pipeline.score_candidates
            def guarded(task, cards, spec_path, **options):
                return guard.score_candidates(task, cards, spec_path, scorer=original, **options)
            pipeline.score_candidates = guarded
            try:
                result = pipeline.run(repo, task, local_only=False, **kwargs)
                guard.finish_without_scoring()
            except BaseException:
                guard.finish_without_scoring("error_before_scoring")
                raise
            finally:
                pipeline.score_candidates = original
                record_metadata()
    else:
        with _PIPELINE_LOCK:
            result = pipeline.run(repo, task, local_only=True, **kwargs)
        result['diagnostics'].insert(0, 'Evaluation: first Jev advice already attempted; subsequent advice is local only')
        _bound_followup(pipeline, result)
    result["evaluation_scoring"] = record_metadata()
    return result
