"""Orchestrate retrieval, external scoring and deterministic reading advice."""

import json
import hashlib
import os
import tempfile
import time
from copy import deepcopy
from pathlib import Path

from .jev_adapter import score_candidates
from .output import render, safe
from .retrieval import collect, source_lines, _safe_path
from .selection import select
from .tokens import TokenCounter
from .safe_io import read_source_bytes, open_directory_nofollow

SPEC = Path(__file__).resolve().parents[2] / "references/relevance-spec.json"


def _validate_sources(repo, hashes, excluded):
    bytes_read = 0
    for path, expected in hashes.items():
        try:
            raw = read_source_bytes(repo, path, excluded)
        except ValueError:
            raise ValueError('source changed or unavailable; rerun retrieval before reading') from None
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('source changed; rerun retrieval before reading')
        bytes_read += len(raw)
    return bytes_read


def _public_row(row):
    scores = [s for s in row["scores"] if s is not None]
    sources = list(dict.fromkeys(row["ranking_sources"]))
    result = dict(priority=row["priority"], path=row["path"], start_line=row["start_line"],
        end_line=row["end_line"], symbol=safe(" / ".join(row["symbols"]), 90), kind=row["kind"],
        jev_score=max(scores) if scores else None, uncertain=row["uncertain"],
        estimated_read_tokens=row["estimated_read_tokens"], evidence=[safe(x, 90) for x in row["evidence"][:2]],
        ranking_source="+".join(sources), incomplete=row["incomplete"], pinned=row["pinned"],
        extraction_mode=row["extraction_mode"])
    if len(set(scores)) > 1:
        result["score_range"] = [min(scores), max(scores)]
    if row["incomplete"]:
        result["symbol_ranges"] = row["symbol_ranges"]
    return result


def public_report(result):
    # Deliberately whitelist fields: never return candidate excerpt/signature/input.
    return {key: result[key] for key in ("status", "candidate_count", "token_counting", "reading_list",
            "alternatives", "deferred", "diagnostics", "budget", "metrics")}


def _measure_list(result, counter, fmt):
    # list_tokens is itself displayed, so find the small integer fixed point.
    for _ in range(6):
        value = counter.count(render(public_report(result), fmt))
        if value == result["metrics"]["list_tokens"]:
            break
        result["metrics"]["list_tokens"] = value


def _short_excerpt(candidate, counter, limit):
    lines = source_lines(candidate["excerpt"])
    first = candidate.get("excerpt_start_line", candidate["start_line"])
    if not lines:
        return "", first, first
    anchor = min(len(lines) - 1, max(0, candidate.get("anchor_line", first) - first))
    left = right = anchor
    text = counter.truncate(lines[anchor], limit)
    if text == lines[anchor]:
        for distance in range(1, len(lines)):
            for idx in (anchor - distance, anchor + distance):
                if 0 <= idx < len(lines):
                    start, end = min(left, idx), max(right, idx)
                    trial = "".join(lines[start:end + 1])
                    if counter.count(trial) <= limit:
                        left, right, text = start, end, trial
    return text.rstrip(), first + left, first + right


def _jev_cards(candidates, counter):
    cards = []
    for c in candidates:
        keys = ("id", "path", "start_line", "end_line", "symbol", "kind", "signature", "excerpt",
                "retrieval_signals", "relations", "estimated_read_tokens", "extraction_mode",
                "incomplete", "symbol_start_line", "symbol_end_line")
        card = {key: c[key] for key in keys}
        card.update({key: c[key] for key in ("excerpt_start_line", "excerpt_end_line") if key in c})
        card["signature"] = counter.truncate(card["signature"], 80)
        card["retrieval_signals"] = [safe(s, 80) for s in card["retrieval_signals"][:4]]
        card["relations"] = card["relations"][:3]
        # Envelope and task are measured separately. Shrink source first.
        card["excerpt"], card["excerpt_start_line"], card["excerpt_end_line"] = _short_excerpt(c, counter, 300)
        while counter.count(json.dumps(card, ensure_ascii=False)) > 500 and card["excerpt"]:
            limit = counter.count(card["excerpt"]) // 2
            card["excerpt"], card["excerpt_start_line"], card["excerpt_end_line"] = _short_excerpt(c, counter, limit)
        while counter.count(json.dumps(card, ensure_ascii=False)) > 500 and card["relations"]:
            card["relations"].pop()
        card["excerpt_truncated"] = c.get('excerpt_incomplete', False) or card["excerpt"] != c["excerpt"]
        cards.append(card)
    return cards


def _save_debug(directory, candidates, scoring, result):
    # Explicit debug only, isolated private run directory; never save keys/stderr.
    directory = Path(directory).absolute()
    # macOS exposes /private/var and /private/tmp through trusted OS aliases.
    # Canonicalize only those two aliases; user-created symlinks still fail.
    for alias, canonical in ((Path("/var"), Path("/private/var")), (Path("/tmp"), Path("/private/tmp"))):
        if directory.is_relative_to(alias) and alias.resolve() == canonical:
            directory = canonical / directory.relative_to(alias)
    if any(p.is_symlink() for p in [directory, *directory.parents]):
        raise ValueError("debug directory must not traverse symlinks")
    directory.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=directory))
    for name, content in (
        ("candidates.jsonl", "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in candidates)),
        ("scoring.json", json.dumps(scoring, ensure_ascii=False, indent=2)),
        ("report.json", json.dumps(public_report(result), ensure_ascii=False, indent=2))):
        with open(run_dir / name, "x", encoding="utf-8") as stream:
            os.chmod(run_dir / name, 0o600)
            stream.write(content)


def run(repo: Path, task: str, terms: list[str], *, paths=None, symbols=None,
        read_budget=1200, list_budget=600, candidate_limit=60, local_only=True,
        expand=True, ast_enabled=True, jev_bin="jev", jev_timeout=45,
        debug_dir=None, output_format="markdown", tokenizer=False):
    started = time.monotonic()
    if read_budget < 1 or list_budget < 256 or not 1 <= candidate_limit <= 500:
        raise ValueError("read budget >=1, list budget >=256, candidate limit 1..500 required")
    if jev_timeout <= 0:
        raise ValueError("Jev timeout must be positive")
    if debug_dir is not None and Path(debug_dir).resolve().is_relative_to(Path(repo).resolve()):
        raise ValueError('debug directory must be outside the read-only target repository')
    counter = TokenCounter(use_tokenizer=tokenizer, readonly_repo=repo)
    recalled = collect(Path(repo), task, terms, paths=paths or [], symbols=symbols or [],
        candidate_limit=candidate_limit, expand=expand, ast_enabled=ast_enabled,
        excluded_dirs=[Path(debug_dir)] if debug_dir else [])
    candidates = recalled["candidates"]
    if counter.diagnostic:
        recalled['diagnostics'].insert(0, counter.diagnostic)
    files = recalled["files"]
    repo = Path(repo).resolve()
    excluded = [Path(debug_dir).resolve()] if debug_dir else []
    source_hashes = {path: hashlib.sha256(''.join(files[path]).encode('utf-8')).hexdigest()
                     for path in {c['path'] for c in candidates}}
    first_fingerprint_bytes = _validate_sources(repo, source_hashes, excluded)
    for c in candidates:
        c["estimated_read_tokens"] = counter.count("".join(files[c["path"]][c["start_line"] - 1:c["end_line"]]))
    cards = _jev_cards(candidates, counter) if not local_only else []
    scoring = dict(scores={}, status="local_only" if local_only else "empty",
                   diagnostics=[], calls=0, completed=0)
    input_tokens = 0
    if not local_only and cards:
        question = json.loads(SPEC.read_text(encoding="utf-8"))["questions"]
        input_tokens = sum(counter.count(json.dumps({"id": c["id"], "task": task, "candidate": c}, ensure_ascii=False))
                           + counter.count(json.dumps(question, ensure_ascii=False)) for c in cards)
        scoring = score_candidates(task, cards, SPEC, jev_bin=jev_bin, timeout=jev_timeout, jobs=4)
        if any(c['excerpt_truncated'] for c in cards):
            scoring['diagnostics'].append('Jev cards clipped; relevance sees only bounded excerpts')
    scores = scoring["scores"]
    # Only expose fixed adapter states, never an unchecked scorer string.
    scoring_status = scoring.get("status")
    if not isinstance(scoring_status, str) or scoring_status not in (
            "ok", "partial", "timeout", "unavailable", "error",
            "invalid_input", "empty", "local_only", "jev"):
        scoring_status = "error"
    for c in candidates:
        answer = scores.get(c["id"])
        c.update(jev_score=answer["score"] if answer else None,
                 confidence=answer["confidence"] if answer else None,
                 uncertain=answer["uncertain"] if answer else False,
                 ranking_source="jev" if answer else ("local" if local_only else "local_fallback"))
    selected = select(candidates, files, read_budget, counter)
    deferred = selected["deferred"]
    status = "local_only" if local_only else ("jev" if len(scores) == len(candidates)
             and scoring_status in ("ok", "jev") else
             "partial_fallback" if scores else "local_fallback")
    if not candidates:
        status = recalled["status"]
    rows = [_public_row(r) for r in selected["reading_list"]]
    selected_paths = {r["path"] for r in rows}
    result = dict(status=status, candidate_count=len(candidates), token_counting=counter.method,
        candidates=candidates, reading_list=rows,
        alternatives=list(dict.fromkeys(c["path"] for c in candidates if c["path"] not in selected_paths))[:3],
        deferred=dict(count=len(deferred), pinned_count=sum(d["pinned"] for d in deferred), examples=deferred[:3]),
        diagnostics=[safe(d, 100) for d in (recalled["diagnostics"] + scoring["diagnostics"])[:5]],
        budget=dict(read_tokens=read_budget, first_round_tokens=selected["first_round_budget"],
                    reserve_tokens=selected["reserve_tokens"], list_tokens=list_budget),
        metrics=dict(reading_tokens=selected["reading_tokens"], list_tokens=0,
            scoring_status=scoring_status,
            jev_calls=scoring["calls"], jev_completed=len(scores), jev_input_tokens=input_tokens if scoring["calls"] else 0,
            jev_cards_clipped=sum(c['excerpt_truncated'] for c in cards),
            latency_seconds=round(time.monotonic() - started, 3)))
    result['metrics']['retrieval_status'] = recalled['status'] if recalled['status'] in ('ok', 'empty', 'no_hits', 'degraded') else 'degraded'
    for key in ('source_read_bytes', 'source_read_lookahead_bytes', 'source_capture_bytes',
                'snapshot_written_bytes', 'source_captured_files', 'source_skipped_files',
                'source_capture_limit_reached'):
        if key in recalled.get('io_metrics', {}):
            result['metrics'][key] = recalled['io_metrics'][key]
    capture_read_bytes = recalled.get('io_metrics', {}).get('source_read_bytes', 0)
    # Successful final validation must read the same fingerprinted bytes again.
    # Size the output for both checks now, then confirm the actual count below.
    result['metrics']['source_capture_read_bytes'] = capture_read_bytes
    result['metrics']['source_fingerprint_read_bytes'] = 2 * first_fingerprint_bytes
    result['metrics']['source_read_bytes'] = capture_read_bytes + 2 * first_fingerprint_bytes
    # Bound the exact public representation, not just its headings. Removed ranges
    # become deferred and their code cost is removed as well.
    _measure_list(result, counter, output_format)
    while result["metrics"]["list_tokens"] > list_budget:
        if result["alternatives"]:
            result["alternatives"].pop()
        elif result["deferred"]["examples"]:
            result["deferred"]["examples"].pop()
        elif any(r["evidence"] for r in rows):
            next(r for r in reversed(rows) if r["evidence"])["evidence"] = []
        elif rows:
            # Keep explicitly named rows as long as possible; report any deferral.
            idx = next((i for i in range(len(rows) - 1, -1, -1) if not rows[i]["pinned"]), len(rows) - 1)
            removed = rows.pop(idx)
            result["metrics"]["reading_tokens"] -= removed["estimated_read_tokens"]
            result["deferred"]["count"] += 1
            result["deferred"]["pinned_count"] += int(removed["pinned"])
        elif result["diagnostics"]:
            result["diagnostics"].pop()
        else:
            raise ValueError("list budget cannot fit summary; increase --list-budget")
        _measure_list(result, counter, output_format)
    for i, r in enumerate(rows, 1):
        r["priority"] = i
    _measure_list(result, counter, output_format)
    if debug_dir is not None:
        _save_debug(debug_dir, candidates, scoring, result)
    final_fingerprint_bytes = _validate_sources(repo, source_hashes, excluded)
    if final_fingerprint_bytes != first_fingerprint_bytes:
        raise ValueError('source changed; rerun retrieval before reading')
    result['metrics']['source_fingerprint_read_bytes'] = first_fingerprint_bytes + final_fingerprint_bytes
    result['metrics']['source_read_bytes'] = capture_read_bytes + first_fingerprint_bytes + final_fingerprint_bytes
    result['repo'] = str(repo)
    result['source_hashes'] = {r['path']: source_hashes[r['path']] for r in rows}
    return result


def save_plan(result, destination):
    directory = Path(destination).absolute()
    for alias, canonical in ((Path('/var'), Path('/private/var')), (Path('/tmp'), Path('/private/tmp'))):
        if directory.is_relative_to(alias) and alias.resolve() == canonical:
            directory = canonical / directory.relative_to(alias)
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError('plan path must not traverse symlinks')
    if directory.resolve().is_relative_to(Path(result['repo'])):
        raise ValueError('plan must be outside the read-only target repository')
    content = dict(repo=result['repo'], source_hashes=result['source_hashes'], **public_report(result))
    parent = open_directory_nofollow(directory.parent)
    try:
        descriptor = os.open(directory.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(content, stream, ensure_ascii=False, indent=2)
    finally:
        os.close(parent)
