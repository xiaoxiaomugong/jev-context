# 检索标签修订提案 v1（2026-10-06）

公开发布说明：原始实验输出、评分/代码快照和读者日志保留在作者本地，未随公开仓库发布；相关路径仅说明历史证据来源。公开范围见 [实验记录说明](runs/README.md)。

`status=proposed`；`human_reviewed=false`；`eligible_for_scoring=false`。以下语义判断均为模型暂定提案，等待人工裁决。本文件和 [结构化 v1 提案](label-proposals-2026-10-06-retrieval-v1.json) 不替代原标签，也未用于重算覆盖率、答案分数或方法比较。

依据是 [冻结任务与标签](real-evaluation-tasks.json)、[原人工清单](human-review-2026-10-06.md)、范围缺口诊断（本地记录：`runs/2026-10-06/range-gap-diagnostic.json`） 和 命中边界诊断（本地记录：`runs/2026-10-06/hit-bound-diagnostic.json`）。独立读取 `/private/tmp/jev-eval-click`，确认 HEAD 为 `934813e4d421071a1b3db3973c02fe2721359a6e`；涉及六题的 23 个原标注范围源码哈希全部匹配。未发现适用 AGENTS.md。没有调用 Jev、重排候选、重读完整日志或重评实验。

## 1. 跨函数范围：先拆证据单元，保留原非空内容

原题分别要求 Choice 转换与测试、整数边界/失败测试、嵌套上下文与清理测试、空字符串/无默认/空 multiple 的区别。跨函数分隔空行是否是“必要上下文”需要人判断；它与代码缺失应分别记账。

| 题目 / 原范围 | 建议范围（同一文件，含端点） | 原始需求对应的证据与理由 |
|---|---|---|
| choice-case，`tests/test_options.py:530–571` | `530–560`；`563–571` | [两个测试](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_options.py#L530-L571)分别覆盖大小写输入和原拼写；561–562 是测试间空行。原诊断只缺 2 个空行，扩大命中上限仍未整段覆盖。 |
| int-range-clamp，`tests/test_types.py:12–51` | `12–32`；`35–51` | [参数化边界测试](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_types.py#L12-L51)分为转换/夹取和失败测试；33–34 是分隔空行。保留装饰器和函数，原诊断只缺这类 2 个空行。 |
| cleanup-order，`src/click/core.py:461–476` | `462–465`；`467–476` | [进入/退出上下文](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L461-L476)保留深度计数、push、close-before-pop；461 是前导空行，466 是方法分隔空行。原 final 缺失 469–476 是非空代码，拆分不能消除此缺口。 |
| cleanup-order，`src/click/core.py:558–606` | `558–585`；`587–597`；`599–606` | [三个注册/关闭方法](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L558-L606)分别是 `with_resource`、`call_on_close`、`close`。只去除 586/598 方法间空行；原诊断该范围仅缺 2 个空行。 |
| cleanup-order，`tests/test_context.py:219–253` | `219–237`；`240–253` | [两个测试](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_context.py#L219-L253)分别证明清理中 current context 可见及资源在 scope 后关闭；238–239 是分隔空行。原诊断只缺 2 个空行。 |
| default-help-empty，`tests/test_options.py:789–812` | `789–794`；`797–802`；`805–812` | [三个对照测试](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_options.py#L789-L812)分别对应原题明确要求的三种默认值；仅去除 795–796/803–804 分隔空行。原始召回还缺 797–802 的非空“无默认”测试，不能把它删去来改善数字。 |

`cleanup-order` 的 [嵌套进入测试 103–120](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_context.py#L103-L120)建议原样保留：它是一个顶层测试，原缺口包含初始化与执行/断言等非空行。`default-help-empty` 的两个实现范围也保留；命中上限诊断恢复实现范围说明存在真实召回因素。

待人工回答：是否把跨函数/跨测试证据拆成以上范围，同时保留所有原非空内容？批准后也应保留原范围统计；拆分会改变“完整范围”分母，不能把新旧数值直接解释成检索或 Jev 的进步。AST 读取单元的实际成本须继续按完整读取单元计量。

## 2. 测试必要性：重复佐证是否必须累计找到

Choice 原题要求解释 `apple`、`oRANGe` 的原拼写并定位转换与测试；原 criterion-4 是引用实现和一个支持测试范围。建议另设一个与上节 Choice 拆分方案互斥的必要性方案：`tests/test_options.py:530–560` 保留必要，`563–571` 降为可选佐证。理由是 [536–542](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_options.py#L536-L542) 已验证 Apple/Orange 原拼写；[563–571](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_options.py#L563-L571)重复相同的 `apple`→`Apple` 场景。两者都没有 `grape` 断言；该失败行为仍需 [Choice.convert 的失败分支](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/types.py#L269-L305)支撑。

待人工回答：第二个 Choice 测试是否因专门的回归意图必须独立找到，还是正确回答引用第一个测试已足够？在确认前，优先只拆范围，不自动降级。

`command-forward` 原题明确要求：当前 count=7 时比较 forward、显式 invoke(count=42)、无参数 invoke 的默认 1，并解释返回传播和定位测试。原 `tests/test_commands.py:235–252` 建议暂时原样保留。[该测试](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_commands.py#L235-L252)确实调用无参数 `ctx.invoke(other_cmd)` 并检查返回值；整体删除会失去直接佐证。字符串默认转整数与空 multiple 变 tuple 是该测试额外覆盖的细节，原题没有要求，建议不要添加为必答验收知识。

待人工回答：引用 [forward/显式 invoke 测试 24–41](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_commands.py#L24-L41)加 [默认填充/返回实现 774–788](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L774-L788)是否已满足“locate tests”，还是必须找到 235–252？这属于证据充分性裁决，目前不建议仅因该测试在全局截断中遗漏就将它去掉。

## 3. chain-result：注册定位不必然要求 compose/replace

冻结原题要求：链组把子命令返回值构成 pipeline，传入 result callback 和组参数；定位注册及调用代码；解释返回顺序、`invoke_without_command` 空调用及非链区别；引用测试。原题没有明确要求重复注册结果回调时的组合或替换规则。

原标签的 `criterion-3` 是 `Explain callback registration and replace.`，占 2/8 分；expected_answer 也要求 `Registration composes callbacks unless replace=True.`。建议将此条件改为“定位 `result_callback` 的注册及 `_result_callback` 的调用路径，说明 result 先传入、随后传组参数”，仍为 2 分；compose/replace 降为补充知识。以下阅读范围暂保持原样，不通过缩短注册方法改善检索分数：

| 原范围 | 建议范围 | 证据及作用 |
|---|---|---|
| `src/click/core.py:1575–1618` | 原样保留 | [注册方法](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L1575-L1618)提供 decorator 和 `_result_callback` 存储路径；其中 [1611–1616](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L1611-L1616)确实实现组合，但代码存在不代表原题要求必答。 |
| `src/click/core.py:1663–1729` | 原样保留 | [调用实现](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L1663-L1729)包含 group kwargs、空链 `[]`、普通组单值和按 context 顺序收集返回列表，均对应原题。 |
| `tests/test_chain.py:88–104` | 原样保留 | [空组对照](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_chain.py#L88-L104)直接验证非链 `1` 与链 `[]`。 |
| `tests/test_chain.py:127–167` | 原样保留 | [pipeline 测试](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_chain.py#L127-L167)提供 processors 和组 file 参数的注册/执行示例。 |

文档来源 [commands.rst:402–406](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/commands.rst#L402-L406)要求返回值列表及组参数，[549–558](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/commands.rst#L549-L558)说明链/非链返回合同。两处支撑冻结原题，没有提出重复注册 compose/replace。命中边界诊断又确认原注册和调用范围可由扩大命中上限恢复；验收范围疑问不能替代这个实际召回证据。

待人工回答：是否批准 compose/replace 降为可选知识，保留注册定位为必答？原 8 份答案的 8/8 模型盲评记录保持不变；本提案没有对原答案追加或撤销分数。

## 最少人工核对清单

- [ ] **跨函数必要范围**：批准或拒绝第 1 节的逐函数拆分；原题的边界、清理、三种默认值职责均保留。直接核对 [Choice 两测试](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_options.py#L530-L571)、[IntRange 两测试](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_types.py#L12-L51)、[cleanup 三方法](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L558-L606)与 [default 三测试](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_options.py#L789-L812)。
- [ ] **测试是否累积必需**：原题要求找到能支持行为的测试。判断 [Choice 重复 Apple 测试](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_options.py#L563-L571)能否可选；判断 [command-forward 默认/返回测试](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/tests/test_commands.py#L235-L252)是否须独立找到。当前只建议前者可选方案，后者暂保留。
- [ ] **chain 验收是否超题**：原题要求注册位置、调用和返回合同。对照 [文档原要求](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/docs/commands.rst#L402-L406)和 [组合实现](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L1611-L1616)，判断 criterion-3 的 compose/replace 是否改为可选。
