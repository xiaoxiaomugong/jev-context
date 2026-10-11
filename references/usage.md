# 使用、边界与验证

当前源码0.3.2；必需 Python3.12+、rg、POSIX directory-fd/O_NOFOLLOW安全接口。核心标准库；Jev和tiktoken均可选，默认不启动。实测与验收状态见 [公开验证摘要](validation-0.3.2.md)。

## 参数

入口 scripts/jev-context.py；--check 不需要repo/task，不读源码或凭证。低版本Python、缺rg或其他degraded检索退出2；正常零命中退出0、状态no_hits/empty。

| 参数 | 默认 / 含义 |
|---|---|
| --repo | 目标仓库，必需，只读 |
| --task / --task-file | 二选一，非空UTF-8，最多64KiB |
| --term | 可重复单行字面量，rg -F；未给时从需求提词，不自动翻译 |
| --path / --symbol | 可重复；明确入口优先，预算内放不下仍deferred |
| --mode local / jev | local；--local-only兼容别名 |
| --read-budget | 1200：80%首轮推荐源码并集，20%补读预留；补读不被封锁 |
| --list-budget | 600，实际序列化输出；最少256，JSON通常需更多 |
| --candidate-limit | 60，允许1–500；提高会增加本地/可能的评分成本 |
| --format | markdown / json |
| --save-plan | 仓库外新文件，建议和所选文件SHA256，无正文，不覆盖已有文件 |
| --tokenizer | 显式tiktoken/cl100k_base；可能首次下载编码，不可用则回退 |
| --jev-bin / --jev-timeout | jev / 45秒评分期限，另有最多1秒有界清理；不额外重试 |
| --debug-dir | 显式私有目录，应放目标外；含代码的0600调试文件 |

行号一基闭区间，按LF计行并保留CRLF；bare CR Python无法可靠映射AST，文本降级。Python保留装饰器、多行签名并取最小符号；其他语言、配置和损坏Python只取文本窗口。提取超过300行或预算不足，建议命中附近子范围，标incomplete并保留完整符号范围；清单裁剪计deferred。不是访问白名单。

既有本地证据分、候选多样性和半级并列选择暂保留。重复命中、候选池规模可能影响排序代理，尚未证明任务效果；分数不能当相关概率。失败使用明确local_fallback，不显示成Jev分；低confidence(<0.6)标uncertain。relevance、confidence和任务正确率不同。

## 阅读和补读

scripts/read-context.py --repo REPO --range PATH START END；--range可重复，每次合并请求最多300行。超限在交付正文前拒绝，拆成多个请求即可。连续范围只输出一次path头，随后原行号和代码；末行无LF时补一个输出LF。允许清单外安全发现文件。

--plan /outside/plan.json 核对repo和请求涉及文件指纹；清单外可补读。--state /outside/seen.json 只存repo/hash/已返回区间；交集不再返回，完全重复stdout为空且退出0。--repeat 显式重读。源变化后重新检索，用新plan/state；没有state时自行避免重复。

plan/state父目录需已存在。state必须在目标外、无用户symlink，原子0600保存；面向单代理串行，多进程应各用一个state。macOS系统/tmp和/var别名按真实系统路径处理，用户symlink拒绝。

## 安全、限制与变化

尊重仓库.gitignore/.ignore/.rgignore及安全根.git/info/exclude。禁用rg用户配置、祖先/global ignore和外部gitdir exclude自动解析，不执行预处理器。工作树外及嵌套gitdir exclude不加载；ignore元文件或相关目录symlink时安全降级。排除常见秘密、二进制、依赖/生成目录及symlink，不读key或原CLI stderr。源读取逐级directory fd+O_NOFOLLOW并复查regular/大小，防打开期间symlink替换。

5000文件、32MiB源码捕获预算、1MiB单文件、16词、每词每文件12命中、每文件12非指名候选、8种子/每种子6一跳线索、显式目录20文件、最长提取300行；本地共享约60秒期限。限界诊断不代表内容无关。发现后用directory fd安全捕获源码到私有临时快照；首检索、有界扩展、一跳搜索与AST解析都用同一bytes，普通源码变化和搜索期间symlink换位不会变成旧命中搭新源码。评分前/报告前核验全部入选候选涉及文件的hash，变化拒绝旧建议；读时plan再核验一次。

0.3.1的source_capture_read_bytes是捕获阶段实际读取（含已拒绝片段），source_fingerprint_read_bytes是两次核验实际读取，source_read_bytes是两者之和；source_capture_bytes是成功捕获内容，snapshot_written_bytes仅计源码快照写入。捕获阶段最多32MiB+1读取、32MiB写入，单次增长拒绝最多1字节lookahead，累计lookahead可多于1；成功命令的两次指纹重读最多额外64MiB，因此目标源码累计读取最多96MiB+1。失败后未返回完整报告时的累计读取未知；rg对快照的读取/临时JSON输出、ignore元数据、debug文件及解释器IO不包含在这些目标源码内容指标中，整机总IO未知。旧0.3.0 source_read_bytes仅计捕获，不能用来声称整次读取上限。latency_seconds记录初次报告生成时点，不含之后的清单整理/debug/最后核验，完整命令时间应由调用方另计。

原file-discovery的ignore元数据是活读取，静态symlink预检与配置隔离存在；运行期间应保持ignore元数据稳定，不把本工具称为对抗同UID恶意并发进程的文件系统沙箱。不是全仓事务快照。不宣称这些安全核验具有相同本地IO成本。

0.3.2在创建任何内部临时文件前选择目标外目录，显式传dir，不先调用会写探测文件的gettempdir。按缓存提示、TMPDIR/TEMP/TMP及POSIX常见临时目录顺序检查，跳过目标内位置；没有可用外部目录时失败。搜索临时输出放在已经隔离的私有快照中。可选tokenizer需设置目标外TIKTOKEN_CACHE_DIR/DATA_GYM_CACHE_DIR，或已有安全的默认tempdir缓存；未建立缓存位置或路径进入目标时，在加载编码前报告UTF-8回退。这样库不能因外部目录探测失败而写入cwd；显式空缓存值禁用磁盘缓存，仍可能下载公开编码数据。不修改用户的全局环境变量。最终Git干净或源码hash不变不能单独证明整个运行期间只读。

一跳关系和测试是线索，不是精确调用图。极长命中行的Jev摘录可能只含行首；不能对评分器未见内容假定准确判断。秘密文件名排除不是任意秘密识别；Jev显式启用前确认代码允许送provider。

## Jev与成本

Jev0.3.2 JSONL：jev run SPEC -l -j 4 --json。校验完整echo、唯一id、数量、score/confidence/probabilities；保留成功项，失败不变零分，不回显input/error/stderr。POSIX超时终止评分进程组、清理有界；独立脱离session的子进程不保证被杀，但不会无限等待pipe。

生产无评分缓存。实验重放绑定任务、内容/卡、repo、规则和计数器指纹，不能把旧c1/c2分套到新代码。mock/external_attempt/replay_external_attempt区分。Jev是判断器，不解释、不提取、不补召回；既有报告未证明总体阅读成本收益，保持可选。新增真实提交上限1200，见 [账本](../evidence/jev-submissions.json)，每批评分前预检；不是HTTP请求/费用预算。

推荐成本为范围并集的裸源码估计，清单为序列化输出估计；主模型实际还接收搜索/行号/补读/重复。UTF-8代理和cl100k_base都不是服务端usage。Jev输入含重复task/卡/问题，未含provider封装/输出；提交项不是HTTP请求数，重试/账单未知。未知不估作费用。

## 验证与历史材料

python3 -m unittest discover -s tests -q；python3 scripts/evaluate.py --offline-responses tests/fixtures/jev-responses.json。

标准测试依赖的fixture和两份冻结检索均随交付，私有旧快照缺失不导致标准测试失败。生产不导入试验。实验专用：test_retrieval_experiment.py及test_evaluate.py的ablation/replay；核心：retrieval/selection/pipeline/reader/jev_adapter/tokens；read_experiment/visibility是计量工具。测试数不替代任务验收。

旧Click已见，原提案仍proposed/human_reviewed=false/eligible_for_scoring=false。重新采集只用新目录，不覆盖旧证据。evaluate.py --snapshot-dir NEW默认会外部评分，显式offline响应才保证零提交；--max-submitted只单批预检，不是跨运行守卫。原始公开范围见 [runs说明](runs/README.md)，旧报告结论保持。

评估专用 task_experiment.py 的baseline/local/jev方法拥有相同files/regex search/heading/EOF-clamped reads；jev每run只首批评分，前置共享账本预留，至多60项，后续advice本地。此预算控制用于本次1200项工程上限，不是生产自动启用规则；生产CLI默认仍local。task-file只读本run/task.txt，stdout与完整argv元数据分别留存，真实HTTP/usage/bill未知。
