---
name: jev-context
description: Use when a coding task spans an unfamiliar repository or multiple modules, or the code location is unclear. Retrieves local code fragments, scores relevance with the existing Jev CLI, and returns a budgeted reading list. For an already specified small file or snippet, read it directly.
---

把任务变成预算内阅读建议，保留随时补读的能力。此 skill 只读目标仓库，不执行其代码。

1. 从需求准备少量检索词、路径、符号或错误字符串；中文需求可附带你推导的英文术语。不要为了准备检索词先通读仓库。脚本不负责翻译。
2. 使用本 skill 的绝对路径运行入口（将下例的 skill 路径替换为实际位置）：

```bash
python3 /path/to/jev-context/scripts/jev-context.py \
  --repo /path/to/target-repository \
  --task '修复发票金额舍入' --term invoice --term rounding
```

可重复 `--term`、`--path`、`--symbol`；支持 `--task-file`、`--read-budget 8000`、`--candidate-limit 60`、`--format json`。`--local-only` 完全跳过 Jev。依赖 Python 3.11+、`rg`，评分复用已安装 `jev`（可 `--jev-bin /absolute/path/jev`）。所有凭证/provider 交给 Jev，勿读取或打印 key。

3. 按输出相对路径和一基闭区间行号，用原有文件工具读取建议范围。`incomplete` 表示只推荐子范围，`deferred` 表示仍待补读；低 confidence 仅标 `uncertain`，失败评分显示本地降级，不记作零分。

清单是阅读建议，不是访问白名单。信息不足时可读取清单外代码、追加词或入口重新运行；不能因未入选断言文件无关。文件变化后须重新定位行号。

默认输出没有源码、候选卡或 Jev input 回显；仅在用户明确要求本地调试材料时加 `--debug-dir /指定目录`。参数、成本语义、限制与验证命令见 [使用说明](references/usage.md)；评分定义见 [relevance spec](references/relevance-spec.json)。
