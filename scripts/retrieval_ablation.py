"""Local-only retrieval experiments. Not imported by the production CLI."""

from copy import deepcopy
import hashlib
import importlib.util
import inspect
from itertools import combinations
from pathlib import Path
import statistics
import sys
import time

from evaluate import (ROOT, _covered_points, _fingerprint, _key_points,
                      _nonblank_points, _omissions, _point_ranges, _public_result,
                      _rank_order, _repository_identity, _task_input,
                      coverage_delta, coverage_details, validate_labels)


VARIANTS = {"R0": (False, False), "R1": (True, False),
            "R2": (False, True), "R3": (True, True)}
BOUND_NAMES = ("MAX_FILES", "MAX_TOTAL_BYTES", "MAX_FILE_BYTES", "MAX_TERMS",
               "MAX_HITS_PER_FILE_TERM", "MAX_CANDIDATES_PER_FILE",
               "MAX_EXPLICIT_DIRECTORY_FILES", "MAX_RANGE_LINES", "MAX_EXCERPT_BYTES")


def _load_baseline(path):
    path = Path(path).resolve(strict=True)
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    spec = importlib.util.spec_from_file_location(f"_jev_frozen_retrieval_{digest}", path)
    if spec is None or spec.loader is None:
        raise ValueError("baseline retrieval module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # Compile the exact hashed bytes without producing __pycache__ in the archive.
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module, path, digest


def _ranges(rows):
    return [{field: row[field] for field in ("path", "start_line", "end_line")}
            for row in rows]


def _collect_baseline(module, *args, **kwargs):
    """Observe the old hit pool at the unchanged one-hop boundary in memory."""
    original = module._expand
    hit_candidates = None
    expansion_seconds = None
    def observe(*expand_args, **expand_kwargs):
        nonlocal hit_candidates, expansion_seconds
        hit_candidates = deepcopy(list(expand_args[3].values()))
        started = time.perf_counter()
        try:
            return original(*expand_args, **expand_kwargs)
        finally:
            expansion_seconds = round(time.perf_counter() - started, 6)
    module._expand = observe
    try:
        pool = module.collect(*args, **kwargs)
    finally:
        module._expand = original
    trace = pool.get("retrieval_trace", {})
    if "hit_candidates" not in trace:
        trace["hit_candidates"] = hit_candidates if hit_candidates is not None else deepcopy(
            trace.get("pre_truncation_candidates", pool["candidates"]))
        trace["baseline_trace_instrumentation"] = {
            "post_hits": "in-memory observation immediately before unchanged _expand; archived source untouched",
            "pre_truncation_timing": "unchanged _expand invocation only; excludes trace snapshot copying"}
        trace.setdefault("stage_elapsed_seconds", {})["pre_truncation"] = expansion_seconds
    return pool


def _union_tokens(rows, files, counter):
    grouped = {}
    for row in rows:
        grouped.setdefault(row["path"], []).append((row["start_line"], row["end_line"]))
    total = 0
    for path, intervals in grouped.items():
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        total += sum(counter.count("".join(files[path][start - 1:end])) for start, end in merged)
    return total


def _stage_rows(pool):
    trace = pool.get("retrieval_trace", {})
    rows = {}
    if "hit_candidates" in trace:
        rows["post_hits"] = trace["hit_candidates"]
    rows["pre_truncation"] = trace.get("pre_truncation_candidates", pool["candidates"])
    rows["post_per_file"] = trace.get("post_per_file_candidates", rows["pre_truncation"])
    rows["post_global"] = pool["candidates"]
    return rows


def _line_attribution(keys, stage_rows, files, scan, selected=None):
    """Assign a missing physical line to the first stage that removed it."""
    points = _key_points(keys)
    nonblank = _nonblank_points(points, files)
    pre = _covered_points(points, stage_rows["pre_truncation"])
    per_file = _covered_points(points, stage_rows["post_per_file"])
    final = _covered_points(points, stage_rows["post_global"])
    groups = {"scan_limit": {(path, line) for path, line in points
                             if path in scan.get("scan_limit_files", [])} - pre,
              "hit_expansion_or_range_boundary": points - pre,
              "per_file_candidate_limit": pre - per_file,
              "global_candidate_limit": per_file - final}
    groups["hit_expansion_or_range_boundary"] -= groups["scan_limit"]
    if selected is not None:
        groups["read_budget"] = final - _covered_points(points, selected)
    return {reason: {"line_count": len(missing), "nonblank_line_count": len(missing & nonblank),
                     "blank_line_count": len(missing - nonblank), "lines": _point_ranges(missing)}
            for reason, missing in groups.items()}


def _pairwise(keys, variant_ranges, files):
    return {f"{left}_to_{right}": coverage_delta(keys, variant_ranges[left], variant_ranges[right], files)
            for left, right in combinations(VARIANTS, 2)}


def _variant(pool, task, label_files, counter, elapsed, read_budget, budgets):
    from jev_context.selection import select
    stages = _stage_rows(pool)
    trace = pool.get("retrieval_trace", {})
    stage_timings = trace.get("stage_elapsed_seconds", {})
    stage_metrics = {}
    for stage, candidates in stages.items():
        stage_metrics[stage] = {"candidate_count": len(candidates),
            "summed_candidate_read_tokens": sum(counter.count("".join(pool["files"][row["path"]]
                [row["start_line"] - 1:row["end_line"]])) for row in candidates),
            "union_read_tokens": _union_tokens(candidates, pool["files"], counter),
            "coverage": coverage_details(task["key_ranges"], candidates, label_files),
            "elapsed_seconds": stage_timings.get(stage), "ranges": _ranges(candidates)}
    candidates, order = _rank_order(pool, {}, True)
    measurement = {"diagnostics": [], "retrieval_paired_seconds": elapsed}
    budget_results = {}
    for budget in dict.fromkeys([*budgets, read_budget]):
        selected = select(candidates, pool["files"], budget, counter, order=order, trim_by_rank=True)
        public = _public_result(pool, selected, budget, counter, "local_only", measurement, local=True)
        budget_results[budget] = {
            "candidate_recall": stage_metrics["post_global"]["coverage"],
            "budget_key_coverage": coverage_details(task["key_ranges"], public["reading_list"], label_files),
            "reading_tokens": selected["reading_tokens"], "list_tokens": public["metrics"]["list_tokens"],
            "code_plus_list_tokens": selected["reading_tokens"] + public["metrics"]["list_tokens"],
            "capacity_tokens": int(budget * .8), "list_cap_disabled": True,
            "jev_calls": 0, "jev_input_tokens": 0, "reading_list": public["reading_list"],
            "omission_attribution": _line_attribution(task["key_ranges"], stages, label_files,
                                                       trace.get("scan", {}), public["reading_list"])}
    return {"candidate_count": len(pool["candidates"]),
            "candidate_recall": stage_metrics["post_global"]["coverage"],
            "candidate_recall_pre_truncation": stage_metrics["pre_truncation"]["coverage"],
            "candidate_recall_post_per_file": stage_metrics["post_per_file"]["coverage"],
            "stages": stage_metrics, "scan": {**deepcopy(trace.get("scan", {})),
                                               "discovery_elapsed_seconds": stage_timings.get("discovery")},
            "stage_elapsed_seconds": deepcopy(stage_timings),
            "baseline_trace_instrumentation": deepcopy(trace.get("baseline_trace_instrumentation", {})),
            "resampling": deepcopy(trace.get("resampling", {})),
            "trimming": deepcopy(trace.get("trim_decisions", {})),
            "possible_hit_limits": deepcopy(trace.get("possible_hit_limits", [])),
            "collection_elapsed_seconds": elapsed,
            "stage_timing_availability": "reported_only_when_instrumented; null_is_unavailable",
            "candidate_omissions": _omissions(task["key_ranges"], pool),
            "omission_attribution": _line_attribution(task["key_ranges"], stages, label_files, trace.get("scan", {})),
            "candidate_fingerprint": _fingerprint(pool["candidates"]),
            "file_content_fingerprint": _fingerprint(pool["files"]),
            "full_pool_union_read_tokens": stage_metrics["post_global"]["union_read_tokens"],
            "status": pool["status"], "diagnostics": pool["diagnostics"],
            "budget_metrics": budget_results, **budget_results[read_budget]}


def _verify_cached_files(pools, identity):
    for pool in pools:
        for path, lines in pool["files"].items():
            if hashlib.sha256("".join(lines).encode("utf-8")).hexdigest() != identity["safe_file_fingerprints"].get(path):
                raise ValueError("ablation cached source differs from frozen repository")


def _summary(results, budgets):
    summaries = {}
    splits = sorted({row["split"] for row in results})
    for split in ["all", *[value for value in splits if value != "all"]]:
        tasks = results if split == "all" else [row for row in results if row["split"] == split]
        rows = []
        for budget in budgets:
            variants = {}
            for name in VARIANTS:
                metrics = [row["variants"][name]["budget_metrics"][budget] for row in tasks]
                variants[name] = {"mean_candidate_complete_fraction": statistics.mean(row["candidate_recall"]["complete_fraction"] for row in metrics),
                    "mean_candidate_nonblank_complete_fraction": statistics.mean(row["candidate_recall"]["nonblank_complete_fraction"] for row in metrics),
                    "mean_budget_line_fraction": statistics.mean(row["budget_key_coverage"]["line_fraction"] for row in metrics),
                    "mean_budget_nonblank_line_fraction": statistics.mean(row["budget_key_coverage"]["nonblank_line_fraction"] for row in metrics),
                    "mean_budget_complete_fraction": statistics.mean(row["budget_key_coverage"]["complete_fraction"] for row in metrics),
                    "total_reading_tokens": sum(row["reading_tokens"] for row in metrics),
                    "total_list_tokens": sum(row["list_tokens"] for row in metrics)}
            pairwise = {}
            for left, right in combinations(VARIANTS, 2):
                comparisons = [row["budget_pairwise"][budget][f"{left}_to_{right}"] for row in tasks]
                pairwise[f"{left}_to_{right}"] = {
                    field: sum(row[field] for row in comparisons) for field in (
                        "recovered_line_count", "lost_line_count", "recovered_nonblank_line_count", "lost_nonblank_line_count")}
            rows.append({"read_budget": budget, "variants": variants, "pairwise": pairwise})
        summaries[split] = {"task_count": len(tasks), "budgets": rows}
    return summaries


def evaluate_ablation(repo, tasks, *, baseline_retrieval, read_budget=2048,
                     trial_retrieval=None, budgets=None, candidate_limit=60,
                     dev_only=False, token_proxy=True):
    """Run four bounded variants with identical local ranking and clipping.

    R0 always executes the explicitly supplied archived retrieval module. A fifth
    disabled-current collection verifies that changes did not disturb R0. Labels
    are used for measurement only, never supplied to any collect call.
    """
    from jev_context import retrieval
    from jev_context.tokens import TokenCounter
    if type(read_budget) is not int or read_budget < 1:
        raise ValueError("read budget must be positive")
    budgets = list(dict.fromkeys(budgets if budgets is not None else [256, 512, 1024, 2048]))
    if not budgets or any(type(value) is not int or value < 1 for value in budgets):
        raise ValueError("budgets must be positive integers")
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= 500:
        raise ValueError("candidate limit must be 1..500")
    tasks = deepcopy([task for task in tasks if not dev_only or task.get("split") == "dev"])
    if not tasks:
        raise ValueError("ablation needs at least one selected task")
    repo = Path(repo).resolve()
    validate_labels(repo, tasks)
    baseline, baseline_path, baseline_digest = _load_baseline(baseline_retrieval)
    if trial_retrieval is None:
        trial = retrieval
        trial_path = Path(retrieval.__file__).resolve()
        trial_digest = hashlib.sha256(trial_path.read_bytes()).hexdigest()
    else:
        trial, trial_path, trial_digest = _load_baseline(trial_retrieval)
    parameters = inspect.signature(trial.collect).parameters
    if not ({"resample_hits", "evidence_coverage"} <= parameters.keys() or
            any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())):
        raise ValueError("trial collector lacks ablation switches; supply archived --trial-retrieval")
    production_path = Path(retrieval.__file__).resolve()
    production_digest = hashlib.sha256(production_path.read_bytes()).hexdigest()
    bounds = {name: getattr(retrieval, name) for name in BOUND_NAMES}
    if (bounds != {name: getattr(baseline, name) for name in BOUND_NAMES} or
            bounds != {name: getattr(trial, name) for name in BOUND_NAMES}):
        raise ValueError("ablation requires unchanged hard retrieval bounds")
    counter = TokenCounter(use_tokenizer=not token_proxy)
    identity = _repository_identity(repo, [])
    # A valid label can lie beyond the scan cap. Freeze that file separately for
    # metrics without adding it to discovery or any retrieval input.
    label_fingerprints = {}
    for relative in {key["path"] for task in tasks for key in task["key_ranges"]}:
        safe = retrieval._safe_path(repo, relative, [])
        if safe is None:
            raise ValueError("label source no longer safely discoverable")
        label_fingerprints[relative] = hashlib.sha256(safe.read_bytes()).hexdigest()
    for task in tasks:
        if task.get("repository_commit") is not None and task["repository_commit"] != identity["git_commit"]:
            raise ValueError(f"task {task['id']}: declared repository commit does not match repository version")
    frozen = []
    for task in tasks:
        common = {"paths": task.get("paths", []), "symbols": task.get("symbols", []),
                  "candidate_limit": candidate_limit, "expand": True, "ast_enabled": True, "trace": True}
        pools, timings = {}, {}
        for name, (resample, evidence) in VARIANTS.items():
            collector = baseline.collect if name == "R0" else trial.collect
            settings = dict(common)
            if name != "R0" or "resample_hits" in inspect.signature(collector).parameters:
                settings.update(resample_hits=resample, evidence_coverage=evidence)
            started = time.perf_counter()
            pool = (_collect_baseline(baseline, repo, task["task"], task.get("terms", []), **settings)
                    if name == "R0" else collector(repo, task["task"], task.get("terms", []), **settings))
            for candidate in pool["candidates"]:
                candidate["estimated_read_tokens"] = counter.count("".join(pool["files"][candidate["path"]]
                    [candidate["start_line"] - 1:candidate["end_line"]]))
            pools[name] = deepcopy(pool)
            timings[name] = round(time.perf_counter() - started, 6)
        started = time.perf_counter()
        control_settings = dict(common)
        parameters = inspect.signature(retrieval.collect).parameters
        if ({"resample_hits", "evidence_coverage"} <= parameters.keys() or
                any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())):
            control_settings.update(resample_hits=False, evidence_coverage=False)
        control = retrieval.collect(repo, task["task"], task.get("terms", []), **control_settings)
        for candidate in control["candidates"]:
            candidate["estimated_read_tokens"] = counter.count("".join(control["files"][candidate["path"]]
                [candidate["start_line"] - 1:candidate["end_line"]]))
        check = {"matches": pools["R0"]["candidates"] == control["candidates"] and pools["R0"]["files"] == control["files"],
                 "disabled_current_candidate_fingerprint": _fingerprint(control["candidates"]),
                 "control_elapsed_seconds": round(time.perf_counter() - started, 6)}
        if not check["matches"]:
            raise ValueError("R0 archived baseline differs from current disabled retrieval; ablation refused")
        _verify_cached_files([*pools.values(), control], identity)
        frozen.append((task, pools, timings, check))
    if _repository_identity(repo, []) != identity:
        raise ValueError("repository changed during ablation collection")
    if hashlib.sha256(baseline_path.read_bytes()).hexdigest() != baseline_digest:
        raise ValueError("archived baseline changed during ablation")
    if (hashlib.sha256(trial_path.read_bytes()).hexdigest() != trial_digest or
            hashlib.sha256(production_path.read_bytes()).hexdigest() != production_digest):
        raise ValueError("trial or production retrieval code changed during ablation")
    # Only after the whole collection is frozen, read label whitespace for metrics.
    from jev_context.retrieval import _safe_path
    label_files = {}
    for relative in {key["path"] for task in tasks for key in task["key_ranges"]}:
        safe = _safe_path(repo, relative, [])
        if safe is None:
            raise ValueError("label source no longer safely discoverable")
        raw = safe.read_bytes()
        if hashlib.sha256(raw).hexdigest() != label_fingerprints[relative]:
            raise ValueError("label source changed after ablation collection")
        label_files[relative] = raw.decode("utf-8").splitlines(keepends=True)
    results = []
    for task, pools, timings, check in frozen:
        variants = {name: _variant(pools[name], task, label_files, counter, timings[name], read_budget, budgets)
                    for name in VARIANTS}
        pairwise = {budget: _pairwise(task["key_ranges"],
                    {name: variants[name]["budget_metrics"][budget]["reading_list"] for name in VARIANTS}, label_files)
                    for budget in budgets}
        stage_pairwise = {stage: _pairwise(task["key_ranges"],
                          {name: variants[name]["stages"][stage]["ranges"] for name in VARIANTS}, label_files)
                          for stage in ("post_hits", "pre_truncation", "post_per_file", "post_global")}
        results.append({"id": task["id"], "split": task.get("split", "unspecified"),
            "human_reviewed": task.get("gold", {}).get("human_reviewed", False),
            "task_input_fingerprint": _fingerprint(_task_input(task)),
            "baseline_identity_check": check, "variants": variants, "stage_pairwise": stage_pairwise,
            "budget_pairwise": pairwise,
            "budget_rows": [{"read_budget": budget, "capacity_tokens": int(budget * .8),
                "variants": {name: variants[name]["budget_metrics"][budget] for name in VARIANTS},
                "pairwise": pairwise[budget]} for budget in budgets]})
    return {"schema_version": 1, "protocol_version": 1, "repo": str(repo),
        "jev_mode": "local_only_retrieval_ablation", "dev_only": dev_only,
        "evaluation_scope": "development_only" if dev_only else "all_frozen_tasks_regression; original holdout is previously observed",
        "read_budget": read_budget, "budgets": budgets, "first_round_fraction": .8,
        "candidate_limit": candidate_limit, "token_basis": counter.method,
        "variants": {name: {"resample_hits": resample, "evidence_coverage": evidence,
                             "archived_implementation": name == "R0" or trial_retrieval is not None}
                     for name, (resample, evidence) in VARIANTS.items()},
        "controls": "same original task/terms/paths/symbols, hard bounds, local ranking, counter, rank-only clipping and budgets; no scorer or readers",
        "scoring_totals": {"submitted_candidates": 0, "jev_input_tokens": 0, "cli_invocations": 0, "api_request_count": 0},
        "reproducibility": {"repository": identity, "baseline_retrieval_path": str(baseline_path),
            "label_source_sha256": label_fingerprints,
            "baseline_retrieval_sha256": baseline_digest, "bounds": bounds,
            "current_retrieval_sha256": production_digest,
            "production_retrieval_sha256": production_digest,
            "production_retrieval_path": str(production_path),
            "trial_retrieval_path": str(trial_path), "trial_retrieval_sha256": trial_digest,
            "trial_is_archived": trial_retrieval is not None,
            "task_input_fingerprint": _fingerprint([_task_input(task) for task in tasks]),
            "annotation_split_fingerprint": _fingerprint([{field: task.get(field) for field in ("id", "split", "key_ranges", "gold")} for task in tasks]),
            "metric_implementation_fingerprint": _fingerprint({relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                for relative in ("scripts/evaluate.py", "scripts/retrieval_ablation.py", "scripts/jev_context/selection.py",
                                 "scripts/jev_context/tokens.py", "scripts/jev_context/pipeline.py", "scripts/jev_context/output.py")}),
            "retrieval_inputs_exclude_gold": True},
        "coverage_definition": "strict counts every original physical line; nonblank excludes whitespace-only lines without changing labels; neither proves semantic sufficiency",
        "pairwise_line_definition": "changed physical lines are deduplicated across overlapping original labels; complete-range changes retain original labels",
        "list_policy": "production renderer, exactly measured list tokens, cap disabled",
        "supplemental_read_cost": "not_measured", "tasks": results,
        "summary_by_split": _summary(results, budgets)}
