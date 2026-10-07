# 人工核对清单：2026-10-06

公开发布说明：原始实验输出、评分/代码快照和读者日志保留在作者本地，未随公开仓库发布；相关路径仅说明历史证据来源。公开范围见 [实验记录说明](runs/README.md)。

只需要判断答案和片段必要性，无需运行脚本。所有任务与标签目前是模型暂定，开发/留出划分和原始结果保持冻结。若调整标签，应另记人工修订版，不改写本轮原始证据。

| 任务 / 集合 | 需要你判断 | 可直接核对的主要片段 |
|---|---|---|
| choice-case / dev | 确认Apple/Orange原拼写、grape拒绝及normalizer→casefold。测试gold 530–571是否应拆分，避免只因空行判未覆盖？ | [Choice:227–248](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/types.py#L227-L248)；[Choice.convert:269–305](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/types.py#L269-L305) |
| int-range-clamp / dev | 确认整数开边界clamp、无clamp失败、float开边界拒绝；tests/test_types.py:12–51应否拆分去掉跨测试空行？ | [_NumberRangeBase.convert:449–478](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/types.py#L449-L478)；[IntRange:517–540](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/types.py#L517-L540) |
| eager-version / dev | 确认version先于普通required校验、expose_value=False及resilient guard；两段core处理上下文是否必要？ | [version_option:476–522](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/decorators.py#L476-L522)；[iter_params_for_processing:116–140](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L116-L140) |
| command-forward / dev | 确认forward=当前7、显式invoke=42、默认invoke=1；tests/test_commands.py:235–252是否必读？ | [Context.invoke:737–788](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L737-L788)；[Context.forward:790–807](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L790-L807) |
| unknown-option-wrapper / dev | 确认--foo=bar原样与-va拆出-a；wrapper配置、长/短选项parser和测试是否都必要？ | [OptionParser.__init__:269–286](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/parser.py#L269-L286)；[OptionParser._match_short_opt:423–461](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/parser.py#L423-L461) |
| atomic-output / dev | 确认flush不发布、close→replace、同目录临时文件、拒绝a/x/非w、标准输出例外；是否需读File/LazyFile/open_stream/_AtomicFile全部四层？ | [File.convert:695–743](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/types.py#L695-L743)；[LazyFile.open:148–176](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/utils.py#L148-L176) |
| environment-split / holdout | 确认文本空白/路径平台分隔符、nargs=2重复值成对；环境取值与Option分组两个实现是否都必要？ | [ParamType.split_envvar_value:120–128](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/types.py#L120-L128)；[Path:799–804](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/types.py#L799-L804) |
| parameter-precedence / holdout | 确认命令行→环境→配置default_map→声明默认的顺序，以及相同值也能报告来源；是否缺prompt限制？ | [Parameter.consume_value:2278–2296](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L2278-L2296)；[Parameter.handle_parse_result:2395–2413](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L2395-L2413) |
| cleanup-order / holdout | 确认嵌套上下文不提前关闭、清理时current context可见；跨方法/跨测试范围中只有空行未覆盖应如何标？ | [Context.__exit__:461–476](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L461-L476)；[Context.with_resource:558–606](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L558-L606) |
| resilient-help / holdout | 确认eager层优先、同eager按CLI顺序、help对象重建失去index而缓存复用；测试371–437是否覆盖所需所有顺序？ | [iter_params_for_processing:116–140](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L116-L140)；[Command.get_help_option:1296–1319](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L1296-L1319) |
| chain-result / holdout | 确认返回顺序、group kwargs、空链[]与非链组返回值；compose/replace是否超出原始需求、应否保留为验收条件？ | [MultiCommand.result_callback:1575–1618](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L1575-L1618)；[MultiCommand.invoke:1663–1729](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L1663-L1729) |
| default-help-empty / holdout | 确认空字符串、None和空multiple默认的help区别；get_help_record的两段是否都是必要上下文？ | [Option.get_help_record:2785–2828](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L2785-L2828)；[Option.get_help_record:2843–2847](https://github.com/pallets/click/blob/934813e4d421071a1b3db3973c02fe2721359a6e/src/click/core.py#L2843-L2847) |

四个阅读任务的答案已通过模型盲评。请重点确认下面每对答案是否正确、依据够用、是否漏了你认为必要的约束；不用检查调用日志。

| 阅读任务 | B答案 | C答案 | 额外判断点 |
|---|---|---|---|
| choice-case | B答案（本地记录：`runs/2026-10-06/readers/r01/answer.json`） | C答案（本地记录：`runs/2026-10-06/readers/r02/answer.json`） | 尚未人工复核。 |
| atomic-output | B答案（本地记录：`runs/2026-10-06/readers/r03/answer.json`） | C答案（本地记录：`runs/2026-10-06/readers/r04/answer.json`） | 尚未人工复核。 |
| resilient-help | B答案（本地记录：`runs/2026-10-06/readers/r05/answer.json`） | C答案（本地记录：`runs/2026-10-06/readers/r06/answer.json`） | 尚未人工复核。 |
| chain-result | B答案（本地记录：`runs/2026-10-06/readers/r07/answer.json`） | C答案（本地记录：`runs/2026-10-06/readers/r08/answer.json`） | C有268行补读，超过250行协议；已保留，不当作合规配对。 |
