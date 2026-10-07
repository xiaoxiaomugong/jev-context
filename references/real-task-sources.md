# Click 8.1.8 源码评估任务来源

10 条是官方文档场景派生的理解题，2 条是历史维护问题的回顾题，并非十二条真实用户工单。所有 gold 是模型暂定标注，`human_reviewed=false`；来源可追溯不等于答案已经独立人审。

- 仓库：[pallets/click](https://github.com/pallets/click)，[发布版 8.1.8](https://github.com/pallets/click/releases/tag/8.1.8)。
- 固定提交：`934813e4d421071a1b3db3973c02fe2721359a6e`；本地副本 `/private/tmp/jev-eval-click`，detached HEAD，未改源码、安装或执行目标项目。
- 版本依据为 Git 固定提交中的源码、文档和已有测试；公开链接均可按提交复核。已浏览发布页、8.1.8 标签的 options/advanced 文档、issue #2500 和 PR #2811。部分按提交 blob 的网页抓取缓存缺失，仍由 Git checkout 直接读取核验。旧文档站链接可能重定向当前版本，因此不以当前站内容替代旧版内容。
- 先固定 6 dev / 6 holdout 以及四个阅读任务，再进行候选生成与评分。保留 candidate_limit=60、read_budget=8000、list_budget=1000；B/C 配置一致，主 C 最多 720 候选。
- 4 个已知公开入口任务保留题面显式符号；8 个入口未知任务无路径/符号提示。查询词均为题面字面子串。来源、gold 和答案不可进入评分/阅读输入。
- 阅读任务：choice-case、atomic-output（dev），resilient-help、chain-result（holdout）。答案和评分标准只在 manifest 的 gold 中。
- key_ranges 保留现有 schema_version=1 兼容结构；完整理由、来源和范围 SHA-256 在 gold.necessary_context。字面行覆盖不证明语义足够或节约人工阅读。

| ID / split | 原始来源与场景依据（转述） | 入口提示 |
| --- | --- | --- |
| choice-case / dev | [documentation_derived_understanding](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/options.rst#L368-L395)：Insensitive matching preserves the configured choice rather than the raw input. | Choice |
| int-range-clamp / dev | [documentation_derived_understanding](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/options.rst#L812-L843)：Bounds may be open, closed or absent; clamping chooses an accepted boundary. | IntRange |
| eager-version / dev | [documentation_derived_understanding](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/options.rst#L542-L588)：Version reporting needs early evaluation, hidden values and a resilient-parsing guard. | version_option |
| command-forward / dev | [documentation_derived_understanding](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/advanced.rst#L158-L185)：Forward reuses current arguments; invoke accepts caller-supplied values. | Context.forward, Context.invoke |
| unknown-option-wrapper / dev | [documentation_derived_understanding](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/advanced.rst#L258-L335)：Wrapper parsing preserves unknown options; mixed short flags can be partially consumed and reassembled. | unknown entry |
| atomic-output / dev | [documentation_derived_understanding](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/handling-files.rst#L78-L90)：Atomic output writes a sibling temporary file and moves it over the destination on completion. | unknown entry |
| environment-split / holdout | [documentation_derived_understanding](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/options.rst#L736-L771)：Multi-value environment input delegates splitting to its type, with path-specific separators. | unknown entry |
| parameter-precedence / holdout | [documentation_derived_understanding](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/advanced.rst#L388-L413)：A command can inspect parameter origin independently of its value. | unknown entry |
| cleanup-order / holdout | [documentation_derived_understanding](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/advanced.rst#L417-L475)：Group resources can be registered with context-owned cleanup. | unknown entry |
| resilient-help / holdout | [real_maintenance_pr_retrospective](https://github.com/pallets/click/pull/2811)：PR #2811 reports regenerated help-option objects losing positional identity; the release caches them. | unknown entry |
| chain-result / holdout | [documentation_derived_understanding](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/commands.rst#L335-L410)：A chained pipeline gathers subcommand returns for the group result callback. | unknown entry |
| default-help-empty / holdout | [real_issue_retrospective](https://github.com/pallets/click/issues/2500)：Issue #2500 reports missing empty-string defaults; release 8.1.8 corrects that behavior. | unknown entry |

必要范围均由固定版本的公开接口、实现与已有测试直接检查提出，未根据补丁修改行、候选排名或模型分数反推。逐项范围理由和期望行为见同目录 real-evaluation-tasks.json；所有边界题由本次评估作者提出，不声称是文档原文逐字问题。

特殊限制：eager-version 的已有测试只直接验证通用 eager 顺序，未发现 version 专属运行时回归测试。resilient-help 的 ID 已预先冻结，但其内容是自定义帮助选项的 eagerness/identity。default-help-empty 以发布版行为作答；原 issue 报告的是 8.0.3。atomic-output 不主张异常回滚：当前实现的 close 会忽略 delete 参数并替换文件。

只新增本文件和 real-evaluation-tasks.json；没有调用 Jev 或查看其排名，没有修改旧评估报告与目标源码。
