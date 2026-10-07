"""Only reading advice is public; source cards remain local."""

import json


def safe(text, length=100):
    return str(text).replace("\n", " ").replace("\r", " ").replace("`", "'")[:length]


def render(report, fmt="markdown"):
    if fmt == "json":
        return json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n"
    m, b = report["metrics"], report["budget"]
    lines = [f"状态: {report['status']} · 候选 {report['candidate_count']} · 计数 {report['token_counting']}",
        f"阅读 ≈{m['reading_tokens']}/{b['first_round_tokens']} tokens; 总预算 {b['read_tokens']}, 预留 {b['reserve_tokens']}; 清单 ≈{m['list_tokens']}/{b['list_tokens']}。",
        f"Jev 提交 {m['jev_calls']} 项, 成功 {m['jev_completed']}; 输入≈{m['jev_input_tokens']} tokens（含重复任务及评分问题）; 耗时 {m['latency_seconds']:.3f}s。", ""]
    for row in report["reading_list"]:
        score = "—" if row["jev_score"] is None else f"{row['jev_score']:.2f}"
        if row.get("score_range"):
            score = f"{row['score_range'][0]:.2f}–{row['score_range'][1]:.2f}"
        flag = " uncertain" if row["uncertain"] else ""
        scope = ""
        if row["incomplete"]:
            bounds = ",".join(f"{a}-{z}" for a, z in row["symbol_ranges"][:2])
            scope = f"; incomplete, 完整范围 {bounds}, deferred 补读"
        pin = "; 指名入口" if row.get("pinned") else ""
        lines.append(f"{row['priority']}. `{safe(row['path'], 150)}:{row['start_line']}-{row['end_line']}` "
            f"{safe(row['symbol'], 80)} · Jev {score}{flag} · ≈{row['estimated_read_tokens']} tokens "
            f"({row['ranking_source']}); {safe('; '.join(row['evidence']), 150)}{scope}{pin}")
    if not report["reading_list"]:
        lines.append("无阅读项；可补充英文检索词、明确路径或符号后重试。")
    if report["alternatives"]:
        lines.extend(["", "备选: " + ", ".join(safe(p, 120) for p in report["alternatives"])])
    d = report["deferred"]
    if d["count"]:
        lines.append(f"deferred {d['count']} 项（指名入口 {d['pinned_count']}）: " +
                     ", ".join(f"{safe(x['path'], 100)}:{x['start_line']}-{x['end_line']} ({x['reason']})" for x in d["examples"]))
    if report["diagnostics"]:
        lines.append("诊断: " + "; ".join(safe(x, 120) for x in report["diagnostics"]))
    lines.append("清单是建议；信息不足可补读。行号为一基闭区间，文件变化后重新定位。成本为估计，补读未测。")
    return "\n".join(lines) + "\n"
