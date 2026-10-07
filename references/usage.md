# 运行与验证

公开发布说明：原始实验输出、评分/代码快照和读者日志保留在作者本地，未随公开仓库发布；相关路径仅说明历史证据来源。公开范围见 [实验记录说明](runs/README.md)。

此目录本身就是可运行的 skill；不自动注册、不修改全局 skills。标准库实现，外部必需 `rg`。Jev 可选，原有 CLI 负责鉴权/provider；CLI 缺失、凭证缺失、鉴权失败、批量部分错误或超时会留下本地阅读清单。`--local-only` 不启动 Jev。

```bash
python3 scripts/jev-context.py --help
python3 scripts/jev-context.py --repo tests/fixtures/shop \
  --task '修复发票金额舍入' --term invoice --term rounding --local-only
python3 scripts/jev-context.py --repo tests/fixtures/shop \
  --task '修复过期令牌处理' --term 'expired token' --path auth/entry.py --format json
python3 -m unittest discover -s tests -v
python3 scripts/evaluate.py --offline-responses tests/fixtures/jev-responses.json
python3 scripts/evaluate.py
```

任务可用 `--task-file task.txt` 代替 `--task`；词是 `rg -F` 字面量，可重复提供。`--path` 为仓库相对路径，`--symbol` 为 Python 名称。显式定位的候选优先保留；非常多的指名入口也可能在候选或阅读预算内放不下，诊断和 deferred 会提示。直接读很小的已知文件通常更省成本。

默认阅读总预算 8000 tokens，首轮上限 6400，剩余 1600 留给补读；`--list-budget 1000` 单独控制实际输出成本，JSON 较长时可提高。`--candidate-limit 60` 控制提交候选。每张 Jev 卡目标约 500 tokens，正文 excerpt 通常更短，全文推荐范围另行计费。参数是初始工程选择，未经最优性验证。

三类成本分别报告：清单序列化 token 数、推荐代码范围的 token 数、Jev 输入估计和提交判断数。代码成本按读取范围的并集计费，同文件重叠合并。可选 `tiktoken` 使用 `cl100k_base` 并声明编码；否则采用 `ceil(UTF-8 bytes/3)` 估算。即使有 tokenizer，编码也未必与主模型相同，不能称作主模型精确用量。Jev 输入包含每项重复 task、卡片和评分问题，未包含 provider 包装/输出 token；提交数不等于服务端实际请求数，CLI 内置重试次数与实际计费未知。补读成本未测。

Jev relevance 为描述性 0–4 score，可小数。confidence/probabilities 是评分分布信息，不是任务正确率。低 confidence (<0.6) 标 uncertain；没有成功结果的候选使用本地相关性名次映射到排序代理（范围 3 至 1），显示 local_fallback，不伪造 Jev 分。分数主导排序，半级以内结合新文件/片段角色、本地证据处理并列；不按分数/token排序，也不用分数阈值宣称无关。过长的关键片段输出包含命中位置的子范围，保留完整符号范围并 deferred。

已核实 Jev 0.3.2 `jev run --help` 与安装源码：`jev run SPEC -l -j 4 --json` 从 stdin 接收含同一 task 和独立 candidate/id 的 JSONL；不使用 `--field`。成功行为 `{input:对象, answers:{relevance:...}}`；错误行可为 `{input:原始字符串,error:...}`，部分失败退出 2。适配层校验 echoed input、唯一 ID、结果数量与 score/confidence/probabilities 字段，保留成功项，不重复启动批量重试。`--jev-timeout 45` 是整个批量 wall timeout，可调整；退出状态和错误文本不透传。服务/provider 隐含失败细节只作固定有界诊断。

本地发现与搜索尊重仓库内 `.gitignore`、`.ignore`、`.rgignore` 和安全的根 `.git/info/exclude`，并排除常见依赖、生成目录、密钥文件、二进制、`.jev-context` 与此次 `--debug-dir`。关闭 rg 用户配置、祖先/global ignore 和自动外部 gitdir exclude 解析，避免预处理器执行或仓库外读取；工作树外的 info/exclude 及嵌套独立 Git 仓库的 info/exclude 不加载。ignore 元文件或 `.git`/`info` 若为符号链接，安全降级并报告，不跟随其目标。调试目录建议放在目标仓库外；后续运行若仍用自定义仓库内目录，也须带相同 `--debug-dir`，或将它加入仓库 ignore。文件/目录符号链接均不读取。候选数限制前根据本地证据与来源覆盖选择。零命中尝试一次有界扩展，失败明确报告；没有英文术语的中文需求可能召回不足。

检索工程上限：5000 文件、32 MiB 总量、单文件 1 MiB、16 个词、每词每文件 12 命中、每文件 12 个非指名候选；最多 8 个种子、每种子 6 个一跳补充；显式目录最多 20 文件；完整 Python 范围超过 300 行会截取并 deferred。本地搜索共享约 60 秒期限。超过限额会给诊断，不能据此断言未召回内容无关。本地排序的前三个候选放不下时，也给命中位置子范围和完整符号 deferred，不把本地代理显示为 Jev 分数。

Python 用 `ast` 选最小完整函数、方法或类，保留装饰器和多行签名；超过提取上限时标 incomplete。语法损坏、配置/文档及其他语言取有界文本窗口并标 extraction_mode。局部导入和同文件定义关系来自 AST；对应测试与跨文件同名引用仍是检索线索，潜在关系不代表准确调用图。

显式 `--debug-dir /path` 才保存私有 run 子目录中的候选卡、评分和报告，不保存 key、原始 CLI stderr 或 API 回显。默认 stdout 只含建议和有界诊断。输入内容是待评价数据，不能改变 relevance 规则；主模型仍应核实重要行为与约束。

评估入口在同一组标注任务上比较 A（文本窗口+本地排序）、B（AST/一跳扩展+本地排序）、C（同B+Jev）。关键范围用一基闭区间标注；报告候选完整覆盖率与行覆盖率、固定预算推荐完整/行覆盖率、清单/推荐阅读成本、延迟和提交量。离线固定响应只验证流程，明确标 mock；真实 C 如降级不会称作集成成功。小 fixture 的覆盖率无法证明生产仓库的节省比例，也未衡量主模型完成任务的正确率。

本次真实评估指标见 [evaluation-results.json](evaluation-results.json)，离线固定响应报告见 [evaluation-offline-results.json](evaluation-offline-results.json)。2026-10-06 的三任务实测首轮各限 640 tokens：A 平均关键完整覆盖 2/3，B/C 为 1；累计推荐阅读 575/806/806，累计清单 680/880/891。真实 C 提交 12 项，三任务均为 jev 成功状态，累计约 2.90 秒；结果未证明 C 比 B 节省阅读成本。默认受限网络里 DNS 失败时降级也已实测，真实调用在允许联网的环境完成。补读成本与生产规模收益尚未测量；当前验证采用 UTF-8 估算，可选 tokenizer 分支和 Python 3.11 本体未运行（本机 Python 3.14.6）。

方法参考：[Aider repo map](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/repomap.md) 的签名、依赖关系及预算选择思想；未复刻其全仓库图排名。Jev 字段语义依据本机 CLI 与其 [官方 primitives](https://docs.typesafe.ai/primitives)、[confidence](https://docs.typesafe.ai/confidence) 文档；首次实现核实以本机契约为准。

本轮扩大评估见 [evaluation-2026-10-06.md](evaluation-2026-10-06.md)：Click 8.1.8 固定 commit、12题、6开发/6留出，B/C复用同一候选快照；四档预算只评分一次。默认评估用 UTF-8 代理计数，关闭清单裁剪、统一按名次截取；生产 CLI 参数和默认策略保持。实际隔离读者的清单、代码、补读、搜索及重复返回另有账本，不能把推荐裸源码成本等同于主模型可见成本。

```bash
python3 scripts/evaluate.py --repo /private/tmp/jev-eval-click \
  --tasks references/real-evaluation-tasks.json --read-budget 1024 \
  --budgets 256,512,1024,2048 --candidate-limit 60 \
  --snapshot-dir references/runs/2026-10-06/real/snapshots --replay
```

捕获需换成新空目录并移除 `--replay`；这会产生新的真实提交。`--max-submitted 1000` 在全批采集后、首个评分前检查数量；它不是跨运行配额守卫。快照私有保存候选、缓存代码和已验证分数，不保存 key/CLI stderr；评分前核对仓库版本和缓存内容。重放验证任务、仓库/源码、候选/卡、检索边界、计数器和评分规则指纹，不启动评分。报告的重放成本是原始测量，新增提交见 `submitted_this_run`。

离线固定响应是 mock，真实捕获是 external_attempt，重放是 replay_external_attempt；不能把它们混为真实调用。gold只在采集/评分后计算覆盖，后验最短gold前缀只作离线参考。所有完整范围/行覆盖都是物理位置指标，人工未复核时不等同于语义充分性。人工需判断的内容集中在 [human-review-2026-10-06.md](human-review-2026-10-06.md)。
