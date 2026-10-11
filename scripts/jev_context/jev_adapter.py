"""Consume Jev's echoing JSONL protocol locally, exposing only validated scores.

The installed Jev 0.3.2 ``run SPEC -l --json`` emits a structured ``input``
plus ``answers`` for success, and raw-string ``input`` plus ``error`` for a
failed line. Any line failure yields exit 2. This module performs no auth,
provider selection, key access, shell invocation, or wrapper retries.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Any

from .retrieval import source_lines


def _canonical(value: Any) -> str:
    # Comparing canonical JSON also distinguishes bool from numeric 1, which
    # Python's dict equality alone does not. Reject non-JSON/non-finite input.
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _number(value: Any, minimum: float, maximum: float) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and minimum <= value <= maximum and math.isfinite(value))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    """Reject ambiguous duplicate fields instead of silently taking the last."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _answer(value: Any) -> dict | None:
    if not isinstance(value, dict) or value.get("type") != "score":
        return None
    if not _number(value.get("score"), 0, 4):
        return None
    if not _number(value.get("confidence"), 0, 1):
        return None
    probabilities = value.get("probabilities")
    if isinstance(probabilities, dict):
        if set(probabilities) != {"0", "1", "2", "3", "4"}:
            return None
        distribution = list(probabilities.values())
    elif isinstance(probabilities, list) and len(probabilities) == 5:
        distribution = probabilities
    else:
        return None
    if not all(_number(probability, 0, 1) for probability in distribution):
        return None
    # Tolerate reported/rounded probabilities while rejecting invalid totals.
    if not math.isclose(sum(distribution), 1, rel_tol=0, abs_tol=.02):
        return None
    return {
        "score": float(value["score"]),
        "confidence": float(value["confidence"]),
        "probabilities": probabilities,
        "uncertain": value["confidence"] < .6,
        "source": "jev",
    }


def _failure_classification(text: str) -> str | None:
    # Never return CLI text, even an exception or API error: it can contain the
    # entire submitted candidate or a response controlled by remote content.
    if re.search(r"HTTP\s+(?:401|403)\b", text, flags=re.IGNORECASE):
        return "jev_auth_unavailable"
    if ("未找到" in text and "API_KEY" in text) or "missing api key" in text.lower():
        return "jev_credentials_unavailable"
    return None


def score_candidates(
    task: str,
    candidates: list[dict],
    spec_path: Path,
    *,
    jev_bin: str = "jev",
    timeout: float = 45,
    jobs: int = 4,
) -> dict:
    """Return safe scores, retaining successes during partial CLI failure.

    ``calls`` counts candidate judgments submitted to a started CLI process,
    not verified HTTP requests; its built-in retries and preflight failures
    make exact network usage unknowable here. ``completed`` counts validated
    successful results. Failed/missing IDs have no entry in ``scores``: callers
    must apply a visibly labelled local-ranking fallback, never score zero.
    """
    result: dict = {"scores": {}, "status": "empty", "diagnostics": [],
                    "calls": 0, "completed": 0}
    diagnostics = result["diagnostics"]

    def diagnose(code: str) -> None:
        if code not in diagnostics and len(diagnostics) < 8:
            diagnostics.append(code)

    if not candidates:
        return result
    if (not isinstance(task, str) or not task.strip()
            or not _number(timeout, .001, 86400)
            or not isinstance(jobs, int) or isinstance(jobs, bool) or not 1 <= jobs <= 16):
        result["status"] = "invalid_input"
        diagnose("jev_invalid_request")
        return result
    expected: dict[str, str] = {}
    rows: list[str] = []
    try:
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError
            candidate_id = candidate.get("id")
            if (not isinstance(candidate_id, str) or not candidate_id
                    or candidate_id in expected):
                raise ValueError
            state = {"id": candidate_id, "task": task, "candidate": candidate}
            canonical = _canonical(state)
            expected[candidate_id] = canonical
            rows.append(canonical)
    except (TypeError, ValueError, OverflowError, RecursionError):
        result["status"] = "invalid_input"
        diagnose("jev_invalid_candidate_ids_or_json")
        return result
    argv = [jev_bin, "run", str(spec_path), "-l", "-j", str(jobs), "--json"]
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=os.name == "posix")
    except (FileNotFoundError, PermissionError, OSError):
        result["status"] = "unavailable"
        diagnose("jev_cli_unavailable")
        return result
    result["calls"] = len(rows)
    timed_out = False
    try:
        stdout_bytes, stderr_bytes = process.communicate(
            input=("\n".join(rows) + "\n").encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
        else:
            process.kill()
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=.5)
        except subprocess.TimeoutExpired as cleanup:
            # An independently detached descendant may still own the pipes.
            # Preserve accumulated output and bound cleanup instead of waiting
            # indefinitely for that process to close its inherited descriptors.
            stdout_bytes, stderr_bytes = cleanup.output or b"", cleanup.stderr or b""
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                pass
            diagnose("jev_timeout_output_cleanup_bounded")
        diagnose("jev_timeout_completed_lines_retained")
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    failure_class = _failure_classification(stderr)
    seen: set[str] = set()
    duplicate_ids: set[str] = set()
    output_lines = 0
    for raw_line in source_lines(stdout):
        if not raw_line.strip():
            continue
        # A timeout can interrupt a write; only complete newline-terminated
        # records qualify as completed CLI output in that case.
        if timed_out and not raw_line.endswith("\n"):
            diagnose("jev_truncated_output")
            continue
        output_lines += 1
        try:
            record = json.loads(raw_line, object_pairs_hook=_unique_object)
        except (ValueError, TypeError, RecursionError):
            diagnose("jev_invalid_json")
            continue
        if not isinstance(record, dict):
            diagnose("jev_invalid_record")
            continue
        state = record.get("input")
        if "error" in record and isinstance(state, str):
            try:
                state = json.loads(state, object_pairs_hook=_unique_object)
            except (ValueError, TypeError, RecursionError):
                diagnose("jev_invalid_error_input")
                continue
        if not isinstance(state, dict):
            diagnose("jev_invalid_input_echo")
            continue
        candidate_id = state.get("id")
        if not isinstance(candidate_id, str) or candidate_id not in expected:
            diagnose("jev_unknown_candidate_id")
            continue
        if candidate_id in seen:
            duplicate_ids.add(candidate_id)
            diagnose("jev_duplicate_candidate_id")
            continue
        seen.add(candidate_id)
        try:
            matched = _canonical(state) == expected[candidate_id]
        except (TypeError, ValueError, OverflowError, RecursionError):
            matched = False
        if not matched:
            diagnose("jev_input_echo_mismatch")
            continue
        if "error" in record:
            diagnose("jev_candidate_failed")
            failure_class = (_failure_classification(str(record["error"]))
                             or failure_class)
            continue
        answers = record.get("answers")
        answer = _answer(answers.get("relevance") if isinstance(answers, dict) else None)
        if answer is None:
            diagnose("jev_invalid_score_fields")
            continue
        result["scores"][candidate_id] = answer
    for candidate_id in duplicate_ids:
        result["scores"].pop(candidate_id, None)
    if output_lines != len(rows):
        diagnose("jev_output_count_mismatch")
    if len(result["scores"]) != len(expected):
        diagnose("jev_missing_valid_results")
    if process.returncode != 0 and not timed_out:
        diagnose("jev_cli_failed")
    if failure_class:
        diagnose(failure_class)
    result["completed"] = len(result["scores"])
    if timed_out:
        result["status"] = "timeout"
    elif result["scores"]:
        result["status"] = "partial" if diagnostics else "ok"
    elif failure_class:
        result["status"] = "unavailable"
    else:
        result["status"] = "error"
    return result
