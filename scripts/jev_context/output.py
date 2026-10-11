"""Only reading advice is public; source cards remain local."""

import json


def safe(text, length=100):
    return str(text).replace("\n", " ").replace("\r", " ").replace("`", "'")[:length]


def render(report, fmt="markdown"):
    if fmt == "json":
        return json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n"
    m, b = report["metrics"], report["budget"]
    partial = ' / retrieval degraded' if m.get('retrieval_status') == 'degraded' else ''
    lines = [f"{report['status']}{partial} · 候选 {report['candidate_count']} · {report['token_counting']}",
        f"阅读≈{m['reading_tokens']}/{b['first_round_tokens']}; 预留 {b['reserve_tokens']}; 清单≈{m['list_tokens']}/{b['list_tokens']} tokens。"]
    if report['status'] != 'local_only':
        lines.append(f"评分 {m.get('scoring_status', 'unavailable')}; Jev 提交 {m['jev_calls']}, 成功 {m['jev_completed']}, 摘录裁剪 {m.get('jev_cards_clipped', 'unknown')}, 输入≈{m['jev_input_tokens']} tokens。")
    for row in report["reading_list"]:
        score = "" if row["jev_score"] is None else f" · Jev {row['jev_score']:.2f}"
        if row.get("score_range"):
            score = f" · Jev {row['score_range'][0]:.2f}–{row['score_range'][1]:.2f}"
        flag = " uncertain" if row["uncertain"] else ""
        scope = ""
        if row["incomplete"]:
            bounds = ",".join(f"{a}-{z}" for a, z in row["symbol_ranges"][:2])
            scope = f"; incomplete, 完整范围 {bounds}, deferred 补读"
        pin = "; 指名入口" if row.get("pinned") else ""
        path = json.dumps(row['path'], ensure_ascii=False)
        lines.append(f"{row['priority']}. {path}:{row['start_line']}-{row['end_line']} "
            f"{safe(row['symbol'], 80)}{score}{flag} · ≈{row['estimated_read_tokens']} "
            f"({row['ranking_source']}); {row.get('extraction_mode', 'unknown')}{scope}{pin}")
    if not report["reading_list"]:
        lines.append("无阅读项；先检查诊断，再补充检索词、路径或符号。")
    if report["alternatives"]:
        lines.append("备选: " + ", ".join(json.dumps(p, ensure_ascii=False) for p in report["alternatives"]))
    d = report["deferred"]
    if d["count"]:
        lines.append(f"deferred {d['count']} 项（指名入口 {d['pinned_count']}）: " +
                     ", ".join(f"{json.dumps(x['path'], ensure_ascii=False)}:{x['start_line']}-{x['end_line']} ({x['reason']})" for x in d["examples"]))
    if report["diagnostics"]:
        lines.append("诊断: " + "; ".join(safe(x, 120) for x in report["diagnostics"]))
    lines.append("建议按需读；可在清单外补读。行号为一基闭区间；源码变化须重定位。")
    return "\n".join(lines) + "\n"
