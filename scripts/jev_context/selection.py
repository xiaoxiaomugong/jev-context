"""Deterministic ranking and union-of-source-ranges budget accounting."""

from collections import Counter
from copy import deepcopy


def role(c):
    if c["kind"] == "test" or "/tests/" in "/" + c["path"] or c["path"].split("/")[-1].startswith("test_"):
        return "test"
    if any(r.get("type") == "configuration_literal" for r in c["relations"]):
        return "interface"
    if any(r.get("type", "") in ("local_import", "local_symbol_reference")
           or r.get("type", "").startswith(("import", "dependency", "reference")) for r in c["relations"]):
        return "dependency"
    if c["kind"] in ("class", "import", "configuration"):
        return "interface"
    return "implementation"


def ranked(candidates):
    """Scores dominate. Within half a level, prefer new files and roles.

    Failed decisions use an explicit local-rank proxy ONLY for ordering;
    that proxy is never reported as a Jev score or probability.
    """
    local = sorted(candidates, key=lambda c: (-c["local_score"], c["path"], c["start_line"]))
    proxy = {c["id"]: 3 - 2 * i / max(1, len(local) - 1) for i, c in enumerate(local)}
    remaining = list(candidates)
    files, roles = Counter(), Counter()
    result = []
    while remaining:
        pinned = [c for c in remaining if c.get("pinned")]
        pool = pinned or remaining
        priority = lambda c: c["jev_score"] if c.get("jev_score") is not None else proxy[c["id"]]
        top = max(map(priority, pool))
        near = [c for c in pool if priority(c) >= top - 0.5]
        choice = min(near, key=lambda c: (files[c["path"]], roles[role(c)],
                     -priority(c), -c["local_score"], c["path"], c["start_line"], c["id"]))
        result.append(choice)
        remaining.remove(choice)
        files[choice["path"]] += 1
        roles[role(choice)] += 1
    return result


def merged_rows(parts, files, counter):
    groups = {}
    for priority, c in parts:
        groups.setdefault(c["path"], []).append((priority, c))
    rows = []
    for path, group in groups.items():
        active = []
        for priority, c in sorted(group, key=lambda pair: pair[1]["start_line"]):
            if active and c["start_line"] <= active[-1]["end_line"]:
                row = active[-1]
                row["end_line"] = max(row["end_line"], c["end_line"])
                row["priority"] = min(priority, row["priority"])
                row["candidate_ids"].append(c["id"])
                row["symbols"].append(c["symbol"] or c["kind"])
                row["uncertain"] |= c.get("uncertain", False)
                row["incomplete"] |= c.get("incomplete", False)
                row["pinned"] |= c.get("pinned", False)
                row["scores"].append(c.get("jev_score"))
                row["ranking_sources"].append(c["ranking_source"])
                row["evidence"].extend(c["retrieval_signals"])
                row["symbol_ranges"].append([c["symbol_start_line"], c["symbol_end_line"]])
            else:
                active.append(dict(priority=priority, path=path, start_line=c["start_line"],
                    end_line=c["end_line"], candidate_ids=[c["id"]], symbols=[c["symbol"] or c["kind"]],
                    kind=c["kind"], uncertain=c.get("uncertain", False), incomplete=c.get("incomplete", False),
                    pinned=c.get("pinned", False), scores=[c.get("jev_score")],
                    ranking_sources=[c["ranking_source"]], evidence=list(c["retrieval_signals"]),
                    symbol_ranges=[[c["symbol_start_line"], c["symbol_end_line"]]],
                    extraction_mode=c["extraction_mode"]))
        for row in active:
            row["estimated_read_tokens"] = counter.count("".join(files[path][row["start_line"] - 1:row["end_line"]]))
            row["evidence"] = list(dict.fromkeys(row["evidence"]))[:3]
            row["symbols"] = list(dict.fromkeys(row["symbols"]))
            rows.append(row)
    rows.sort(key=lambda r: (r["priority"], r["path"], r["start_line"]))
    for i, row in enumerate(rows, 1):
        row["priority"] = i
    return rows


def total(rows):
    return sum(r["estimated_read_tokens"] for r in rows)


def select(candidates, files, read_budget, counter, *, order=None, trim_by_rank=False):
    """Budget advice, with evaluation-only fixed order and score-neutral clipping.

    Defaults retain CLI behavior. Paired ranking experiments must pass the same
    clipping policy so score magnitude does not become a second treatment.
    """
    if order is not None:
        expected = {c["id"]: c for c in candidates}
        if (len(order) != len(candidates) or len({c["id"] for c in order}) != len(order)
                or any(c != expected.get(c["id"]) for c in order)):
            raise ValueError("order must be a permutation of the candidate snapshot")
    capacity = int(read_budget * 0.8)
    parts, deferred = [], []
    for priority, original in enumerate(ranked(candidates) if order is None else order, 1):
        c = deepcopy(original)
        proposed = merged_rows(parts + [(priority, c)], files, counter)
        if total(proposed) > capacity:
            # Important entries get a readable bounded window, never silent overflow.
            eligible = (priority <= 3 if trim_by_rank else
                        (c.get("jev_score") or 0) >= 3 or
                        (c.get("jev_score") is None and priority <= 3))
            if c.get("pinned") or eligible:
                anchor = min(c["end_line"], max(c["start_line"], c.get("anchor_line", c["start_line"])))
                full_start, full_end = c["start_line"], c["end_line"]
                lo, hi, best = 1, full_end - full_start + 1, None
                while lo <= hi:
                    width = (lo + hi) // 2
                    start = max(full_start, min(anchor - width // 2, full_end - width + 1))
                    c["start_line"], c["end_line"] = start, start + width - 1
                    trial = merged_rows(parts + [(priority, c)], files, counter)
                    if total(trial) <= capacity:
                        best = (c["start_line"], c["end_line"])
                        lo = width + 1
                    else:
                        hi = width - 1
                if best:
                    c["start_line"], c["end_line"] = best
                    c["incomplete"] = True
                    parts.append((priority, c))
                    deferred.append(dict(id=c["id"], path=c["path"], start_line=c["symbol_start_line"],
                        end_line=c["symbol_end_line"], pinned=c.get("pinned", False), reason="remaining_symbol_range"))
                    continue
            deferred.append(dict(id=c["id"], path=c["path"], start_line=original["start_line"],
                end_line=original["end_line"], pinned=c.get("pinned", False), reason="read_budget"))
            continue
        parts.append((priority, c))
        if c.get("incomplete"):
            deferred.append(dict(id=c["id"], path=c["path"], start_line=c["symbol_start_line"],
                end_line=c["symbol_end_line"], pinned=c.get("pinned", False), reason="remaining_symbol_range"))
    rows = merged_rows(parts, files, counter)
    return dict(reading_list=rows, reading_tokens=total(rows), deferred=deferred,
                first_round_budget=capacity, reserve_tokens=read_budget - capacity)
