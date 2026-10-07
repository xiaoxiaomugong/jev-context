#!/usr/bin/env python3
"""Paired ranking evaluation with frozen inputs and post-hoc gold references.

    python3 scripts/evaluate.py --tasks references/evaluation-tasks.json \
        --offline-responses tests/fixtures/jev-responses.json

    python3 scripts/evaluate.py --repo /path/to/repo --tasks tasks.json \\
        --budgets 256,512,1024,2048 --snapshot-dir /path/to/new-snapshot
    python3 scripts/evaluate.py --repo /path/to/repo --tasks tasks.json \\
        --budgets 256,512,1024,2048 --snapshot-dir /path/to/new-snapshot --replay

Offline fixed responses are mock evidence. Replay validates saved inputs and
never invokes the scorer. Gold ranges are used only after retrieval/scoring.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import inspect
import json
import math
import os
from pathlib import Path, PurePosixPath
import statistics
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def _relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and value != "."


def load_tasks(path: Path) -> list[dict]:
    """Load manually labeled, one-based inclusive source ranges."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("task file must use schema_version 1")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("task file needs a nonempty tasks array")
    seen = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("each task must be an object")
        identifier = task.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("task ids must be nonempty and unique")
        seen.add(identifier)
        if not isinstance(task.get("task"), str) or not task["task"].strip():
            raise ValueError(f"task {identifier} needs a task string")
        for field in ("terms", "paths", "symbols"):
            values = task.get(field, [])
            if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"task {identifier}: {field} must contain nonempty strings")
        keys = task.get("key_ranges")
        if not isinstance(keys, list) or not keys:
            raise ValueError(f"task {identifier} needs key_ranges")
        for key in keys:
            if not isinstance(key, dict) or not _relative_path(key.get("path")):
                raise ValueError(f"task {identifier}: key paths must stay repository-relative")
            start, end = key.get("start_line"), key.get("end_line")
            if type(start) is not int or type(end) is not int or not 1 <= start <= end:
                raise ValueError(f"task {identifier}: invalid inclusive key range")
    return tasks


def coverage(keys: list[dict], ranges: list[dict]) -> dict:
    """Measure full labeled ranges and unioned line coverage within each label.

    Line coverage is a location measure, not proof that the read supplied enough
    semantic context. Overlapping recommendations never earn duplicate credit.
    """
    complete = covered_lines = key_lines = 0
    for key in keys:
        left, right = key["start_line"], key["end_line"]
        width = right - left + 1
        key_lines += width
        matches = sorted(
            (max(left, item["start_line"]), min(right, item["end_line"]))
            for item in ranges
            if item["path"] == key["path"]
            and item["start_line"] <= right and item["end_line"] >= left
        )
        union_width = 0
        union_end = left - 1
        for start, end in matches:
            union_width += max(0, end - max(start, union_end + 1) + 1)
            union_end = max(union_end, end)
        covered_lines += union_width
        complete += union_width == width
    return {
        "complete_ranges": complete,
        "total_ranges": len(keys),
        "complete_fraction": complete / len(keys) if keys else 0,
        "covered_lines": covered_lines,
        "key_lines": key_lines,
        "line_fraction": covered_lines / key_lines if key_lines else 0,
    }


def _key_points(keys: list[dict]) -> set[tuple[str, int]]:
    return {(key["path"], line) for key in keys
            for line in range(key["start_line"], key["end_line"] + 1)}


def _covered_points(points: set[tuple[str, int]], ranges: list[dict]) -> set[tuple[str, int]]:
    by_path = {}
    for row in ranges:
        by_path.setdefault(row["path"], []).append((row["start_line"], row["end_line"]))
    return {(path, line) for path, line in points
            if any(start <= line <= end for start, end in by_path.get(path, []))}


def _point_ranges(points: set[tuple[str, int]]) -> list[dict]:
    result = []
    for path, line in sorted(points):
        if result and result[-1]["path"] == path and result[-1]["end_line"] == line - 1:
            result[-1]["end_line"] = line
        else:
            result.append({"path": path, "start_line": line, "end_line": line})
    return result


def _nonblank_points(points: set[tuple[str, int]], files: dict) -> set[tuple[str, int]]:
    for path, line in points:
        if path not in files or not 1 <= line <= len(files[path]):
            raise ValueError("coverage source is missing or shorter than its label")
    return {(path, line) for path, line in points if files[path][line - 1].strip()}


def coverage_details(keys: list[dict], ranges: list[dict], files: dict) -> dict:
    """Keep strict coverage and separately measure nonblank physical lines.

    Labels and their whitespace remain frozen. A whitespace-only label is
    vacuously nonblank-complete; this metric does not assert semantic sufficiency.
    Like coverage(), line totals count overlapping labels once per label.
    """
    result = coverage(keys, ranges)
    nonblank_total = nonblank_covered = nonblank_complete = blank_only = 0
    blank_only_labels = []
    for key in keys:
        points = _key_points([key])
        covered = _covered_points(points, ranges)
        nonblank = _nonblank_points(points, files)
        nonblank_total += len(nonblank)
        nonblank_covered += len(nonblank & covered)
        nonblank_complete += nonblank <= covered
        blank_only += bool(points - covered) and nonblank <= covered
        if points - covered and nonblank <= covered:
            blank_only_labels.append(deepcopy(key))
    return {**result, "nonblank_key_lines": nonblank_total,
            "nonblank_covered_lines": nonblank_covered,
            "nonblank_line_fraction": nonblank_covered / nonblank_total if nonblank_total else 1,
            "nonblank_complete_ranges": nonblank_complete,
            "nonblank_complete_fraction": nonblank_complete / len(keys) if keys else 0,
            "blank_only_incomplete_ranges": blank_only,
            "blank_only_incomplete_range_labels": blank_only_labels}


def coverage_delta(keys: list[dict], before: list[dict], after: list[dict], files: dict) -> dict:
    """Expose both directions of a treatment effect, without overlapping credit.

    Changed lines are a union across all labels, while complete-range changes
    retain each original label. Neither is a task-success measure.
    """
    points = _key_points(keys)
    left, right = _covered_points(points, before), _covered_points(points, after)
    nonblank = _nonblank_points(points, files)
    gained, lost = right - left, left - right
    result = {"recovered_line_count": len(gained), "lost_line_count": len(lost),
              "recovered_nonblank_line_count": len(gained & nonblank),
              "lost_nonblank_line_count": len(lost & nonblank),
              "recovered_blank_line_count": len(gained - nonblank),
              "lost_blank_line_count": len(lost - nonblank),
              "recovered_lines": _point_ranges(gained), "lost_lines": _point_ranges(lost),
              "recovered_complete_ranges": [], "lost_complete_ranges": [],
              "recovered_nonblank_complete_ranges": [], "lost_nonblank_complete_ranges": []}
    for key in keys:
        for name, relevant in (("complete", _key_points([key])),
                               ("nonblank_complete", _nonblank_points(_key_points([key]), files))):
            if not relevant <= left and relevant <= right:
                result[f"recovered_{name}_ranges"].append(deepcopy(key))
            if relevant <= left and not relevant <= right:
                result[f"lost_{name}_ranges"].append(deepcopy(key))
    return result


def evaluate_retrieval_ablation(repo: Path, tasks: list[dict], **settings) -> dict:
    """Evaluation-only R0/R1/R2/R3 entry; never invokes any scoring adapter."""
    from retrieval_ablation import evaluate_ablation
    return evaluate_ablation(repo, tasks, **settings)


def validate_labels(repo: Path, tasks: list[dict]) -> None:
    """Reject mismatched annotations instead of reporting misleading zero recall."""
    from jev_context.retrieval import _discover, _safe_path
    root = repo.resolve()
    if not root.is_dir():
        raise ValueError("evaluation repository must be a directory")
    discovery_trace = {}
    discovered = set(_discover(root, [], [], trace=discovery_trace)) | set(discovery_trace.get("scan_limit_files", []))
    for task in tasks:
        for key in task["key_ranges"]:
            path = _safe_path(root, key["path"], []) if key["path"] in discovered else None
            if path is None:
                raise ValueError(f"task {task['id']}: label does not identify a safely discoverable local file")
            raw = path.read_bytes()
            if b"\0" in raw:
                raise ValueError(f"task {task['id']}: label identifies binary content")
            lines = raw.decode("utf-8").splitlines()
            if key["end_line"] > len(lines):
                raise ValueError(f"task {task['id']}: label exceeds its file line count")


def _load_offline(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("offline responses must use schema_version 1")
    if not isinstance(data.get("responses"), list) or not isinstance(data.get("default"), dict):
        raise ValueError("offline responses need responses and default")
    seen = set()
    for row in [data["default"], *data["responses"]]:
        if not isinstance(row, dict):
            raise ValueError("each offline response must be an object")
        for field, upper in (("score", 4), ("confidence", 1)):
            value = row.get(field)
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= upper:
                raise ValueError(f"invalid offline {field}")
        probabilities = row.get("probabilities")
        if not isinstance(probabilities, (list, dict)):
            raise ValueError("offline probabilities must be a list or object")
        if row is not data["default"]:
            key = (row.get("task_id"), row.get("path"), row.get("symbol"))
            if not isinstance(key[0], str) or not _relative_path(key[1]) or key in seen:
                raise ValueError("offline response needs a unique task_id/path/symbol")
            seen.add(key)
    return data


def _fixed_scorer(task_id: str, data: dict):
    mapping = {
        (row["path"], row.get("symbol")): row
        for row in data["responses"] if row["task_id"] == task_id
    }

    def score_candidates(task, candidates, spec_path, *, jev_bin="jev", timeout=45, jobs=4):
        scores = {}
        for candidate in candidates:
            row = mapping.get((candidate["path"], candidate.get("symbol")))
            if row is None:
                row = mapping.get((candidate["path"], None), data["default"])
            scores[candidate["id"]] = {
                "score": float(row["score"]),
                "confidence": float(row["confidence"]),
                "probabilities": row["probabilities"],
                "uncertain": row["confidence"] < 0.6,
                "source": "jev",
            }
        return {"scores": scores, "status": "jev", "diagnostics": [], "calls": 0}

    return score_candidates


def _integration(status: str, offline: bool) -> str:
    if offline:
        return "mock_not_external"
    if status == "jev":
        return "external_jev"
    if status == "partial_fallback":
        return "partial_external_jev_with_local_fallback"
    if status == "partial_external":
        return "incomplete_external_jev"
    if status in ("empty", "no_hits"):
        return "no_external_request"
    return "local_fallback_external_not_verified"


def _fingerprint(value) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _task_input(task: dict) -> dict:
    return {"id": task["id"], "task": task["task"],
            **{field: task.get(field, []) for field in ("terms", "paths", "symbols")}}


def _snapshot_directory(directory: Path, *, create=False) -> Path:
    directory = Path(directory).absolute()
    for alias, canonical in ((Path("/var"), Path("/private/var")), (Path("/tmp"), Path("/private/tmp"))):
        if directory.is_relative_to(alias) and alias.resolve() == canonical:
            directory = canonical / directory.relative_to(alias)
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError("snapshot directory must not traverse symlinks")
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def _repository_identity(repo: Path, excluded: list[Path]) -> dict:
    from jev_context.retrieval import _discover, _safe_path
    files = []
    for relative in _discover(repo, excluded, []):
        path = _safe_path(repo, relative, excluded)
        if path is None:
            raise ValueError("repository changed during evaluation fingerprinting")
        files.append([relative, hashlib.sha256(path.read_bytes()).hexdigest()])
    def git_value(arguments):
        try:
            result = subprocess.run(["git", "-C", str(repo), *arguments], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=10, check=False)
            return result.stdout.decode("utf-8").strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    commit = git_value(["rev-parse", "HEAD"])
    origin = git_value(["remote", "get-url", "origin"]) if commit else None
    return {"git_commit": commit, "git_origin_fingerprint": _fingerprint(origin) if origin else None,
            "safe_discovered_file_count": len(files), "safe_tree_fingerprint": _fingerprint(files),
            "safe_file_fingerprints": dict(files)}


def _replay_context(repo, tasks, candidate_limit, counter, excluded):
    from jev_context import pipeline, retrieval
    from jev_context.tokens import TokenCounter
    modules = ("retrieval.py",)
    code = {name: hashlib.sha256((ROOT / "scripts/jev_context" / name).read_bytes()).hexdigest()
            for name in modules}
    code["card_builder"] = _fingerprint(inspect.getsource(pipeline._jev_cards) + inspect.getsource(pipeline._short_excerpt))
    code["token_counter"] = _fingerprint(inspect.getsource(TokenCounter))
    settings = {"candidate_limit": candidate_limit, "paired": {"expand": True, "ast_enabled": True},
                "A": {"expand": False, "ast_enabled": False}, "trace": True,
                "counter": counter.method, "implementation": code,
                "bounds": {name: getattr(retrieval, name) for name in (
                    "MAX_FILES", "MAX_TOTAL_BYTES", "MAX_FILE_BYTES", "MAX_TERMS", "MAX_HITS_PER_FILE_TERM",
                    "MAX_CANDIDATES_PER_FILE", "MAX_EXPLICIT_DIRECTORY_FILES", "MAX_RANGE_LINES", "MAX_EXCERPT_BYTES")}}
    task_inputs = [_task_input(task) for task in tasks]
    return {"repository": _repository_identity(repo, excluded), "task_inputs": task_inputs,
            "task_fingerprint": _fingerprint(task_inputs), "retrieval_settings": settings,
            "retrieval_fingerprint": _fingerprint(settings),
            "score_spec_fingerprint": hashlib.sha256(pipeline.SPEC.read_bytes()).hexdigest()}


def _pool_fingerprints(pool, cards=None):
    result = {"candidate_fingerprint": _fingerprint(pool["candidates"]),
              "file_content_fingerprint": _fingerprint(pool["files"]),
              "retrieval_trace_fingerprint": _fingerprint(pool.get("retrieval_trace", {}))}
    if cards is not None:
        result["card_fingerprint"] = _fingerprint(cards)
    return result


def _verify_frozen_inputs(repo, excluded, context, snapshots):
    if _repository_identity(repo, excluded) != context["repository"]:
        raise ValueError("repository changed during frozen input collection or scoring")
    expected = context["repository"]["safe_file_fingerprints"]
    for snapshot in snapshots:
        for name in ("A", "paired"):
            for path, lines in snapshot[name]["files"].items():
                digest = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
                if digest != expected.get(path):
                    raise ValueError("frozen cached file content mismatch with initial repository")


def _write_private_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _save_snapshots(directory, context, snapshots, mode):
    directory = _snapshot_directory(directory, create=True)
    if (directory / "manifest.json").exists():
        raise ValueError("snapshot directory already contains a manifest; use --replay or a new directory")
    descriptors = []
    for index, snapshot in enumerate(snapshots, 1):
        name = f"task-{index:04d}.json"
        _write_private_json(directory / name, snapshot)
        descriptors.append({"id": snapshot["task_input"]["id"], "file": name,
                            "fingerprint": _fingerprint(snapshot)})
    _write_private_json(directory / "manifest.json", {
        "schema_version": 1, "protocol": "paired_rank_only_v1", "mode": mode,
        "context": context, "context_fingerprint": _fingerprint(context), "tasks": descriptors,
        "gold_policy": "gold labels are excluded from all snapshot inputs"})


def _load_snapshots(directory, context, counter):
    from jev_context import pipeline
    directory = _snapshot_directory(directory)
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("replay snapshot manifest symlink is refused")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != 1 or manifest.get("protocol") != "paired_rank_only_v1"
            or manifest.get("context") != context
            or manifest.get("context_fingerprint") != _fingerprint(context)):
        raise ValueError("replay snapshot input mismatch (repository, task, retrieval, counter or scoring spec)")
    snapshots = []
    descriptors = manifest.get("tasks", [])
    if len(descriptors) != len(context["task_inputs"]):
        raise ValueError("replay snapshot task count mismatch")
    for index, descriptor in enumerate(descriptors, 1):
        if descriptor.get("file") != f"task-{index:04d}.json":
            raise ValueError("replay snapshot filename mismatch")
        source = directory / descriptor["file"]
        if source.is_symlink():
            raise ValueError("replay snapshot file mismatch: symlinks are refused")
        snapshot = json.loads(source.read_text(encoding="utf-8"))
        if (_fingerprint(snapshot) != descriptor.get("fingerprint")
                or snapshot.get("task_input") != context["task_inputs"][index - 1]
                or descriptor.get("id") != snapshot["task_input"]["id"]):
            raise ValueError("replay snapshot candidate, file content or score mismatch")
        if (snapshot["paired_fingerprints"] != _pool_fingerprints(snapshot["paired"], snapshot["cards"])
                or snapshot["A_fingerprints"] != _pool_fingerprints(snapshot["A"])
                or snapshot["cards"] != pipeline._jev_cards(snapshot["paired"]["candidates"], counter)):
            raise ValueError("replay snapshot candidate cards or file content mismatch")
        snapshots.append(snapshot)
    return snapshots, manifest["mode"]


def _score_status(pool, scoring, offline):
    if not pool["candidates"]:
        return pool["status"]
    if offline:
        return "offline_mock"
    completed = len(scoring["scores"])
    if (completed == len(pool["candidates"]) and scoring["status"] in ("ok", "jev")
            and not scoring["diagnostics"]):
        return "jev"
    if completed == len(pool["candidates"]):
        return "partial_external"
    return "partial_fallback" if completed else "local_fallback"


def _rank_order(pool, scores, local):
    from jev_context.selection import ranked
    candidates = deepcopy(pool["candidates"])
    for candidate in candidates:
        answer = scores.get(candidate["id"])
        candidate.update(jev_score=answer["score"] if answer else None,
                         confidence=answer["confidence"] if answer else None,
                         uncertain=answer.get("uncertain", answer["confidence"] < .6) if answer else False,
                         ranking_source="jev" if answer else "local" if local else "local_fallback")
    return candidates, ranked(candidates)


def _public_result(pool, selected, read_budget, counter, status, measurement, *, local):
    from jev_context import pipeline
    from jev_context.output import safe
    rows = [pipeline._public_row(row) for row in selected["reading_list"]]
    selected_paths = {row["path"] for row in rows}
    deferred = selected["deferred"]
    diagnostics = pool["diagnostics"] + ([] if local else measurement["diagnostics"])
    result = {"status": status, "candidate_count": len(pool["candidates"]), "token_counting": counter.method,
              "reading_list": rows,
              "alternatives": list(dict.fromkeys(c["path"] for c in pool["candidates"] if c["path"] not in selected_paths))[:3],
              "deferred": {"count": len(deferred), "pinned_count": sum(row["pinned"] for row in deferred), "examples": deferred[:3]},
              "diagnostics": [safe(item, 100) for item in diagnostics[:5]],
              "budget": {"read_tokens": read_budget, "first_round_tokens": int(read_budget * .8),
                         "reserve_tokens": read_budget - int(read_budget * .8), "list_tokens": "unbounded"},
              "metrics": {"reading_tokens": selected["reading_tokens"], "list_tokens": 0,
                          "jev_calls": 0 if local else measurement["submitted_candidates"],
                          "jev_completed": 0 if local else measurement["validated_successful_candidates"],
                          "jev_input_tokens": 0 if local else measurement["jev_input_tokens"],
                          "latency_seconds": measurement["retrieval_A_seconds"] if status == "A_reference" else
                              measurement["retrieval_paired_seconds"] + (0 if local else measurement["latency_seconds"])} }
    if status == "A_reference":
        result["status"] = "local_only" if pool["candidates"] else pool["status"]
    # Use the production renderer and exact public representation, with no row removal.
    pipeline._measure_list(result, counter, "markdown")
    return result


def _gold_prefixes(order, pool, keys, counter):
    from jev_context.selection import merged_rows, total
    targets = {"full_key_ranges": ("complete_fraction", 1), "line_50_percent": ("line_fraction", .5),
               "line_75_percent": ("line_fraction", .75), "line_100_percent": ("line_fraction", 1)}
    found = {}
    for count in range(len(order) + 1):
        rows = merged_rows(list(enumerate(order[:count], 1)), pool["files"], counter)
        measured = coverage(keys, rows)
        for name, (field, threshold) in targets.items():
            if name not in found and measured[field] >= threshold:
                found[name] = {"reachable": True, "prefix_candidate_count": count,
                               "union_read_tokens": total(rows), "gold_coverage": measured, "rows": rows}
        if len(found) == len(targets):
            break
    for name in targets:
        found.setdefault(name, {"reachable": False, "prefix_candidate_count": None,
                               "union_read_tokens": None, "gold_coverage": None, "rows": []})
    return found


def _omissions(keys, pool):
    trace = pool.get("retrieval_trace", {})
    discovered = set(trace.get("discovered_files", pool["files"]))
    pre = trace.get("pre_truncation_candidates", pool["candidates"])
    per_file = trace.get("post_per_file_candidates", pre)
    limits = trace.get("possible_hit_limits", [])
    omissions = []
    for key in keys:
        if coverage([key], pool["candidates"])["complete_fraction"] == 1:
            continue
        if key["path"] in trace.get("scan", {}).get("scan_limit_files", []):
            reason = "scan_limit"
        elif key["path"] not in discovered:
            reason = "scan_safety_or_ignore_boundary"
        elif coverage([key], pre)["complete_fraction"] < 1:
            reason = "retrieval_or_hit_bound"
        elif coverage([key], per_file)["complete_fraction"] < 1:
            reason = "per_file_candidate_limit"
        else:
            reason = "global_candidate_limit"
        omissions.append({**key, "reason": reason,
                          "possible_hit_limit": any(item[0] == key["path"] for item in limits)})
    return omissions


def _variant(pool, candidates, order, prefixes, fingerprints, card_ranges, task, read_budget, counter,
             name, measurement, offline):
    from jev_context.selection import select, merged_rows, total
    selected = select(candidates, pool["files"], read_budget, counter, order=order, trim_by_rank=True)
    status = "A_reference" if name == "A" else "local_only" if name == "B" else measurement["status"]
    public = _public_result(pool, selected, read_budget, counter, status, measurement, local=name != "C")
    trace = pool.get("retrieval_trace", {})
    pre = trace.get("pre_truncation_candidates", pool["candidates"])
    per_file = trace.get("post_per_file_candidates", pre)
    full_tokens = total(merged_rows(list(enumerate(order, 1)), pool["files"], counter))
    reference = {"basis": "post_hoc_gold_only_not_a_stopping_policy"}
    for target, prefix in prefixes.items():
        reference[target] = {key: value for key, value in prefix.items() if key != "rows"}
        if prefix["reachable"]:
            prefix_public = _public_result(pool, {"reading_list": prefix["rows"], "reading_tokens": prefix["union_read_tokens"],
                "deferred": []}, read_budget, counter, status, measurement, local=name != "C")
            reference[target]["list_tokens"] = prefix_public["metrics"]["list_tokens"]
            reference[target]["code_plus_list_tokens"] = prefix["union_read_tokens"] + prefix_public["metrics"]["list_tokens"]
    result = {"candidate_count": len(pool["candidates"]), "candidate_count_pre_truncation": len(pre),
              "candidate_recall": coverage(task["key_ranges"], pool["candidates"]),
              "candidate_recall_pre_truncation": coverage(task["key_ranges"], pre),
              "candidate_recall_post_per_file": coverage(task["key_ranges"], per_file),
              "gold_visible_in_scorer_card_excerpts": coverage(task["key_ranges"], card_ranges) if name != "A" else None,
              "candidate_omissions": _omissions(task["key_ranges"], pool),
              "budget_key_coverage": coverage(task["key_ranges"], public["reading_list"]),
              "reading_tokens": selected["reading_tokens"], "list_tokens": public["metrics"]["list_tokens"],
              "code_plus_list_tokens": selected["reading_tokens"] + public["metrics"]["list_tokens"],
              "list_cap_disabled": True, "capacity_tokens": int(read_budget * .8),
              "full_pool_union_read_tokens": full_tokens, "full_pool_fits": full_tokens <= int(read_budget * .8),
              "selection_uninformative": full_tokens <= int(read_budget * .8),
              "latency_seconds": round(public["metrics"]["latency_seconds"], 6),
              "jev_calls": public["metrics"]["jev_calls"], "jev_input_tokens": public["metrics"]["jev_input_tokens"],
              "jev_cost_basis": "offline_mock_no_external_cost" if name == "C" and offline else "once_per_task_measurement_reused_across_budgets",
              "status": public["status"], "diagnostics": public["diagnostics"],
              "jev_integration": _integration(measurement["status"], offline) if name == "C" else "not_used",
              "reading_list": public["reading_list"], "public_report": public,
              "offline_gold_prefix_reference": reference, **fingerprints}
    if name == "C" and offline:
        result["offline_scored_candidates"] = len(candidates)
    return result


def _summary(rows):
    return {"mean_candidate_complete_fraction": statistics.mean(row["candidate_recall"]["complete_fraction"] for row in rows),
            "mean_budget_complete_fraction": statistics.mean(row["budget_key_coverage"]["complete_fraction"] for row in rows),
            "mean_budget_line_fraction": statistics.mean(row["budget_key_coverage"]["line_fraction"] for row in rows),
            "total_reading_tokens": sum(row["reading_tokens"] for row in rows),
            "total_list_tokens": sum(row["list_tokens"] for row in rows),
            "total_latency_seconds": round(sum(row["latency_seconds"] for row in rows), 6),
            "total_jev_calls": sum(row["jev_calls"] for row in rows),
            "total_jev_input_tokens": sum(row["jev_input_tokens"] for row in rows)}


def _paired_summary(results, budgets):
    splits = sorted({task["split"] for task in results})
    summaries = {}
    for split in ["all", *[value for value in splits if value != "all"]]:
        tasks = results if split == "all" else [task for task in results if task["split"] == split]
        comparisons = []
        for budget in budgets:
            rows = [next(row for row in task["budget_rows"] if row["read_budget"] == budget) for task in tasks]
            deltas = [row["variants"]["C"]["budget_key_coverage"]["line_fraction"] -
                      row["variants"]["B"]["budget_key_coverage"]["line_fraction"] for row in rows]
            savings = []
            for row in rows:
                left = row["variants"]["B"]["offline_gold_prefix_reference"]["full_key_ranges"]
                right = row["variants"]["C"]["offline_gold_prefix_reference"]["full_key_ranges"]
                if left["reachable"] and right["reachable"]:
                    savings.append(left["code_plus_list_tokens"] - right["code_plus_list_tokens"])
            comparisons.append({"read_budget": budget, "capacity_tokens": int(budget * .8),
                "task_count": len(rows), "mean_C_minus_B_line_fraction": statistics.mean(deltas),
                "C_wins": sum(delta > 0 for delta in deltas), "ties": sum(delta == 0 for delta in deltas),
                "B_wins": sum(delta < 0 for delta in deltas),
                "selection_informative_tasks": sum(not row["variants"]["B"]["full_pool_fits"] for row in rows),
                "paired_full_gold_reachable_tasks": len(savings),
                "mean_offline_full_gold_code_plus_list_saving_B_minus_C": statistics.mean(savings) if savings else None})
        summaries[split] = {"task_count": len(tasks), "budgets": comparisons}
    return summaries


def evaluate(repo: Path, tasks: list[dict], *, read_budget: int = 8000,
             offline_responses: Path | None = None, budgets: list[int] | None = None,
             candidate_limit: int = 60, snapshot_dir: Path | None = None, replay: bool = False,
             token_proxy: bool = True, max_submitted: int = 1000) -> dict:
    """Freeze A and paired B/C once, score once per task, then scan budgets.

    The legacy ``variants`` and ``summary`` select ``read_budget``. New budget
    rows reuse their saved task measurement and must never be summed as API cost.
    """
    from jev_context import pipeline
    from jev_context.tokens import TokenCounter
    if type(read_budget) is not int or read_budget < 1:
        raise ValueError("read budget must be positive")
    budgets = [read_budget] if budgets is None else list(dict.fromkeys(budgets))
    if not budgets or any(type(value) is not int or value < 1 for value in budgets):
        raise ValueError("budgets must be positive integers")
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= 500:
        raise ValueError("candidate limit must be 1..500")
    if type(max_submitted) is not int or not 1 <= max_submitted <= 1000:
        raise ValueError("submitted limit must be 1..1000")
    if replay and (snapshot_dir is None or offline_responses is not None):
        raise ValueError("--replay needs --snapshot-dir and cannot use --offline-responses")
    repo = Path(repo).resolve()
    validate_labels(repo, tasks)
    counter = TokenCounter(use_tokenizer=not token_proxy)
    directory = _snapshot_directory(snapshot_dir) if snapshot_dir else None
    excluded = [directory] if directory and directory.is_relative_to(repo) else []
    context = _replay_context(repo, tasks, candidate_limit, counter, excluded)
    for task in tasks:
        declared = task.get("repository_commit")
        if declared is not None and declared != context["repository"]["git_commit"]:
            raise ValueError(f"task {task['id']}: declared repository commit does not match repository version")
    offline_data = _load_offline(offline_responses) if offline_responses else None
    mode = "offline_fixed_responses_mock" if offline_data else "external_attempt"
    if replay:
        snapshots, mode = _load_snapshots(directory, context, counter)
        _verify_frozen_inputs(repo, excluded, context, snapshots)
    else:
        if directory and directory.exists() and any(directory.iterdir()):
            raise ValueError("snapshot directory is already nonempty; preserve it and use --replay or a new directory")
        snapshots = []
        # Entire-batch preflight precedes the first scorer invocation.
        for task in tasks:
            pools, timings = {}, {}
            for name, expanded in (("A", False), ("paired", True)):
                started = time.perf_counter()
                pool = pipeline.collect(repo, task["task"], task.get("terms", []),
                    paths=task.get("paths", []), symbols=task.get("symbols", []), candidate_limit=candidate_limit,
                    expand=expanded, ast_enabled=expanded, excluded_dirs=excluded, trace=True)
                for candidate in pool["candidates"]:
                    candidate["estimated_read_tokens"] = counter.count("".join(pool["files"][candidate["path"]]
                        [candidate["start_line"] - 1:candidate["end_line"]]))
                pools[name] = deepcopy(pool)
                timings[name] = round(time.perf_counter() - started, 6)
            cards = pipeline._jev_cards(pools["paired"]["candidates"], counter)
            snapshots.append({"task_input": _task_input(task), **pools, "cards": cards,
                "A_fingerprints": _pool_fingerprints(pools["A"]),
                "paired_fingerprints": _pool_fingerprints(pools["paired"], cards), "retrieval_timings": timings})
        planned = sum(len(snapshot["cards"]) for snapshot in snapshots)
        if offline_data is None and planned > max_submitted:
            raise ValueError(f"submission preflight exceeds submitted limit: {planned} > {max_submitted}; no scoring started")
        _verify_frozen_inputs(repo, excluded, context, snapshots)
        from jev_context.jev_adapter import _canonical
        questions = json.loads(pipeline.SPEC.read_text(encoding="utf-8"))["questions"]
        for snapshot in snapshots:
            cards, task_input = snapshot["cards"], snapshot["task_input"]
            scorer = _fixed_scorer(task_input["id"], offline_data) if offline_data else pipeline.score_candidates
            started = time.perf_counter()
            scoring = scorer(task_input["task"], deepcopy(cards), pipeline.SPEC) if cards else {
                "scores": {}, "status": "empty", "diagnostics": [], "calls": 0, "completed": 0}
            elapsed = round(time.perf_counter() - started, 6)
            status = _score_status(snapshot["paired"], scoring, offline_data is not None)
            input_proxy = sum(counter.count(_canonical({"id": card["id"], "task": task_input["task"], "candidate": card}))
                              + counter.count(_canonical(questions)) for card in cards)
            snapshot["scoring"] = scoring
            snapshot["scoring_measurement"] = {"mode": mode, "status": status, "adapter_status": scoring["status"],
                "diagnostics": scoring["diagnostics"], "planned_submitted_candidates": len(cards),
                "submitted_candidates": scoring["calls"], "validated_successful_candidates": len(scoring["scores"]),
                "local_fallback_candidates": len(cards) - len(scoring["scores"]),
                "cli_invocations": int(scoring["calls"] > 0), "api_request_count": None, "provider_retries": None,
                "billed_input_tokens": None, "billed_output_tokens": None, "billed_cost": None,
                "jev_input_tokens": input_proxy if scoring["calls"] else 0, "planned_input_token_proxy": input_proxy,
                "input_token_basis": "serialized submitted task/card plus repeated JSON scoring questions; proxy, not provider usage",
                "latency_seconds": elapsed, "retrieval_A_seconds": snapshot["retrieval_timings"]["A"],
                "retrieval_paired_seconds": snapshot["retrieval_timings"]["paired"],
                "complete_external_success": status == "jev", "measurement_scope": "once_per_task_reused_across_budgets"}
        post_scoring_changed = False
        try:
            _verify_frozen_inputs(repo, excluded, context, snapshots)
        except ValueError:
            post_scoring_changed = True
        for snapshot in snapshots:
            snapshot["post_scoring_repository_changed"] = post_scoring_changed
        if directory:
            _save_snapshots(directory, context, snapshots, mode)
        if post_scoring_changed:
            submitted = sum(snapshot["scoring_measurement"]["submitted_candidates"] for snapshot in snapshots)
            raise ValueError(f"repository changed during scoring; normal report refused; {submitted} submitted judgments"
                             + (f" preserved in {directory}" if directory else "; no snapshot directory was supplied"))
    offline = mode == "offline_fixed_responses_mock"
    results = []
    for task, snapshot in zip(tasks, snapshots):
        measurement = snapshot["scoring_measurement"]
        prepared = {}
        for name in ("A", "B", "C"):
            pool = snapshot["A"] if name == "A" else snapshot["paired"]
            candidates, order = _rank_order(pool, snapshot["scoring"]["scores"] if name == "C" else {}, name != "C")
            card_ranges = [{"path": card["path"], "start_line": card.get("excerpt_start_line", card["start_line"]),
                            "end_line": card.get("excerpt_end_line", card["end_line"])}
                           for card in snapshot["cards"] if card["excerpt"]] if name != "A" else []
            prepared[name] = (pool, candidates, order, _gold_prefixes(order, pool, task["key_ranges"], counter),
                              snapshot["A_fingerprints"] if name == "A" else snapshot["paired_fingerprints"], card_ranges)
        def variants_for(budget):
            return {name: _variant(*prepared[name], task, budget, counter, name, measurement, offline) for name in ("A", "B", "C")}
        rows = [{"read_budget": budget, "capacity_tokens": int(budget * .8), "variants": variants_for(budget)} for budget in budgets]
        variants = next((row["variants"] for row in rows if row["read_budget"] == read_budget), None) or variants_for(read_budget)
        results.append({"id": task["id"], "split": task.get("split", "unspecified"), "variants": variants,
                        "budget_rows": rows, "scoring_measurement": measurement,
                        "task_input_fingerprint": _fingerprint(snapshot["task_input"])})
    measurements = [task["scoring_measurement"] for task in results]
    submitted = sum(item["submitted_candidates"] for item in measurements)
    return {"schema_version": 1, "protocol_version": 2, "repo": str(repo), "read_budget": read_budget,
        "budgets": budgets, "first_round_fraction": .8, "candidate_limit": candidate_limit,
        "variants": {"A": "separate rg/text recall reference + local ranking",
                     "B": "frozen rg + AST/one-hop candidates + local ranking",
                     "C": "same frozen B candidates/cards/files + Jev ranking"},
        "paired_controls": "identical task, hints, candidates, cards, files, ranges, counter, capacity and rank-only clipping; only ranking changes",
        "jev_mode": f"replay_{mode}" if replay else mode,
        "submission_preflight": {"cap": max_submitted, "planned_submitted_candidates": sum(len(s["cards"]) for s in snapshots),
            "submitted_this_run": 0 if replay else submitted, "saved_original_submitted_candidates": submitted if replay else None,
            "all_tasks_collected_before_scoring": True, "submitted_is_not_api_requests": True},
        "scoring_totals": {"submitted_candidates": submitted, "validated_successful_candidates": sum(item["validated_successful_candidates"] for item in measurements),
            "local_fallback_candidates": sum(item["local_fallback_candidates"] for item in measurements),
            "jev_input_tokens": sum(item["jev_input_tokens"] for item in measurements),
            "latency_seconds": round(sum(item["latency_seconds"] for item in measurements), 6),
            "api_request_count": None, "provider_retries": None, "billed_cost": None,
            "scope": "saved_original_run_once_per_task" if replay else "current_run_once_per_task"},
        "reproducibility": {**context, "repository_location_informational": str(repo),
                            "metric_implementation_fingerprint": _fingerprint({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                for name in ("scripts/evaluate.py", "scripts/jev_context/selection.py", "scripts/jev_context/output.py")}),
                            "annotation_split_fingerprint": _fingerprint([
                                {"id": task["id"], "split": task.get("split", "unspecified"), "key_ranges": task["key_ranges"]}
                                for task in tasks]),
                            "snapshot_dir": str(directory) if directory else None,
                            "replay_validated": replay, "snapshot_inputs_exclude_gold": True},
        "coverage_definition": "complete_fraction = all lines of a labeled range covered; line_fraction = unioned covered lines / labeled lines; neither proves semantic sufficiency",
        "supplemental_read_cost": "not_measured", "token_basis": counter.method,
        "list_policy": "production public renderer, exact measured list tokens, cap disabled; no rows removed",
        "tasks": results, "summary": {name: _summary([task["variants"][name] for task in results]) for name in ("A", "B", "C")},
        "paired_summary_by_split": _paired_summary(results, budgets)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=ROOT / "references/evaluation-tasks.json", help="schema_version 1 labeled tasks JSON (defaults to fixture annotations)")
    parser.add_argument("--repo", type=Path, default=ROOT / "tests" / "fixtures" / "shop")
    parser.add_argument("--read-budget", type=int, help="legacy displayed budget; defaults to last scan budget")
    parser.add_argument("--budgets", nargs="+", help="scan budgets, comma-separated or space-separated (default: 256,512,1024,2048)")
    parser.add_argument("--candidate-limit", type=int, default=60)
    parser.add_argument("--snapshot-dir", type=Path, help="new private snapshot directory; required for --replay")
    parser.add_argument("--replay", action="store_true", help="validate and reuse frozen candidates/scores; never invokes scorer")
    parser.add_argument("--token-proxy", action="store_true", default=True, help="use deterministic UTF-8 bytes / 3 proxy (default)")
    parser.add_argument("--tokenizer", dest="token_proxy", action="store_false", help="use installed tokenizer; capture/replay must match")
    parser.add_argument("--max-submitted", type=int, default=1000, help="whole-batch real submitted judgment cap, never above 1000")
    parser.add_argument("--offline-responses", type=Path, help="fixed local mock responses; makes no external Jev request")
    args = parser.parse_args(argv)
    try:
        budgets = [int(value) for group in args.budgets for value in group.split(",")] if args.budgets else (
            [args.read_budget] if args.read_budget is not None else [256, 512, 1024, 2048])
        read_budget = args.read_budget if args.read_budget is not None else budgets[-1]
        report = evaluate(args.repo, load_tasks(args.tasks), read_budget=read_budget, budgets=budgets,
                          offline_responses=args.offline_responses, candidate_limit=args.candidate_limit,
                          snapshot_dir=args.snapshot_dir, replay=args.replay, token_proxy=args.token_proxy,
                          max_submitted=args.max_submitted)
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error)[:240])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
