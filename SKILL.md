---
name: jev-context
description: Use when locating an unknown entry point in an unfamiliar Python repository or tracing behavior across modules and tests. Produces local reading advice and supports bounded supplemental reads. Read an already identified small file or snippet directly.
---

把需求转成阅读建议，主模型决定读哪些范围和何时补读。目标仓库只读，不执行其代码。默认本地排序，不需要 Jev 或凭证。

1. 使用实际 skill 安装位置运行检查，用少量词、错误字符串或符号定位。中文任务可附你推导的英文术语，无须先读全仓库。

```bash
python3 /path/to/jev-context/scripts/jev-context.py --check
python3 /path/to/jev-context/scripts/jev-context.py \
  --repo /path/to/repo --task '解释发票舍入与测试' \
  --term invoice --term rounding --save-plan /outside/repo/plan.json
```

2. 按路径和一基闭区间选择最有用的范围，通常先读核心实现及对应测试。清单不要求全部读取。可用现有文件工具，或用下列工具校验计划并避免重复：

```bash
python3 /path/to/jev-context/scripts/read-context.py \
  --repo /path/to/repo --range billing/amounts.py 4 7 \
  --plan /outside/repo/plan.json --state /outside/repo/seen.json
```

3. 信息不足时，用同一 state 读清单外范围、继续正常 rg，或追加词、路径、符号重新检索。--repeat 显式重读。源码变化后重新定位并使用新 plan/state。每次读取最多 300 行，超限分次读。

incomplete/deferred 表示未完整推荐；text_fallback 为其他语言或不可解析 Python 的文本窗口。未入选不能证明无关。失败评分不是零分，confidence 不是任务正确率。

仅在需要且允许向配置的 provider 发送任务和代码摘录时加 --mode jev；Jev 只判断相关性，不能补救漏召回。安装、参数、计量及限制按需读 [usage](references/usage.md)。默认不输出源码或候选卡；私有调试仅通过显式 --debug-dir 保存到仓库外。
