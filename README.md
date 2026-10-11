# jev-context

将编码任务变成预算内的代码阅读建议，再按需读取实现、测试和依赖。适合不熟悉的 Python 仓库、未知入口和跨模块问题；已知的小文件可以直接读。

当前版本 **0.3.2**。默认完全本地运行，Jev 评分需要显式启用。目标仓库只读，不导入或执行目标代码。

## 当前能力

- 使用 `rg` 检索任务词、路径和符号；Python 通过 AST 定位函数、方法或类，并补充有界的一跳依赖和测试线索。
- 搜索与解析共用安全源码快照；评分前和返回前核验候选文件指纹，源码变化后拒绝旧建议。
- 保存包含建议与源码指纹的阅读计划，通过 `read-context.py` 校验范围、补读清单外代码，并按 state 去除重复读取。
- 可选调用外部 Jev CLI 评分；缺 CLI、鉴权失败、超时或部分错误时明确使用本地降级，保留有效评分。

清单是阅读建议，未入选不代表无关，一跳关系不等于准确调用图。行号为一基闭区间，源码变化后应重新定位。

## 环境要求

| 项目 | 要求与验证范围 |
| --- | --- |
| Python | 3.12+；已验证 3.12.14 和 3.14.6 |
| ripgrep | `rg` 必需，需在 PATH 中可用 |
| 平台 | 需要 POSIX directory-fd / `O_NOFOLLOW` 安全接口；实测 macOS arm64，Linux 未运行验证 |
| Windows | 未验证；缺少必需安全接口时明确拒绝 |
| Jev / tiktoken | 均可选；默认不需要安装或配置 |

核心实现只有 Python 标准库，直接运行随附脚本，暂不提供 `pip install .`。Python 3.11 未验证，本版不声明支持。详细记录见 [公开验证摘要](references/validation-0.3.2.md)。

## 快速开始

首次克隆并检查环境；已有仓库时直接进入其根目录：

```bash
git clone https://github.com/xiaoxiaomugong/jev-context.git
cd jev-context
python3 scripts/jev-context.py --check
```

下面每次使用新的临时目录保存计划和已读状态，可重复执行：

```bash
demo_dir="$(mktemp -d /tmp/jev-context-demo.XXXXXX)"

# 默认本地检索，不调用 Jev
python3 scripts/jev-context.py --repo tests/fixtures/shop \
  --task '解释发票舍入及测试' --term invoice --term rounding \
  --save-plan "$demo_dir/plan.json"

# 根据建议读代码；plan 校验源码指纹，state 记录已读区间
python3 scripts/read-context.py --repo tests/fixtures/shop \
  --range billing/amounts.py 4 7 \
  --plan "$demo_dir/plan.json" --state "$demo_dir/seen.json"

# 信息不足时可继续补读；清单外的安全文件也允许读取
python3 scripts/read-context.py --repo tests/fixtures/shop \
  --range billing/entry.py 1 9 --state "$demo_dir/seen.json"

# 相同 state 下再次请求已读区间，stdout 为空
python3 scripts/read-context.py --repo tests/fixtures/shop \
  --range billing/amounts.py 4 7 --state "$demo_dir/seen.json"
```

将 `--repo` 换成自己的目标仓库即可。中文任务建议附带英文术语，脚本不自动翻译；`--term`、`--path`、`--symbol` 都可重复传入，长任务可用 `--task-file`。

plan 必须是目标仓库外的新文件，父目录须已存在；state 同样放在目标外，只保存指纹与已读范围，不包含源码正文。reader 每次合并请求最多 300 行；需要更长内容时分次读取，显式 `--repeat` 可重读。源码变化后重新生成 plan 并使用新 state。

`--check` 不需要 repo/task，也不启动评分。低版本 Python、缺 `rg` 或检索 degraded 时退出 2；正常零命中退出 0，不能据此判断代码无关。

## 作为 agent skill 使用

可直接让 agent 阅读根目录的 [SKILL.md](SKILL.md)。安装时保留完整仓库目录，不能只复制 `SKILL.md` 或单个入口。

Codex 的用户级 skill 目录是 `~/.agents/skills`，项目级目录是 `.agents/skills`，见 [官方安装与发现说明](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。用户级安装示例：

```bash
mkdir -p "$HOME/.agents/skills"
git clone https://github.com/xiaoxiaomugong/jev-context.git \
  "$HOME/.agents/skills/jev-context"
```

目标目录非空时 `git clone` 会拒绝。已有安装应先检查本地改动，再按升级步骤更新；其他 agent 工具按其 skill 目录规则安装。运行脚本时使用实际安装位置的绝对路径，并让 `--repo` 指向目标项目。

升级前检查安装目录的 `git status`，备份自定义改动；工作区干净时可在安装目录执行 `git pull --ff-only`。卸载仅移除确认属于本 skill 的目录。本项目没有后台服务，不自动注册 skill 或修改全局配置。

## 显式启用 Jev

先独立安装并配置兼容的 Jev CLI，再添加 `--mode jev`：

```bash
python3 scripts/jev-context.py --repo tests/fixtures/shop \
  --task '解释过期令牌处理' --term 'expired token' \
  --path auth/entry.py --mode jev
```

适配层按 **Jev CLI 0.3.2** 的 JSONL 契约实现，该版本号属于外部 CLI。可以用 `--jev-bin /path/to/jev` 指定已有可执行文件；本项目不读取凭证或管理 provider。

启用评分会将任务、路径、符号和代码摘录经 Jev 发送给其配置的 provider，使用前确认目标代码允许发送。失败明确降级，不把失败当作不相关；confidence 不是任务正确率。`--jev-timeout` 默认 45 秒，适配层不额外重试。

## 参数、预算与计量

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--mode` | `local` | 本地排序；`jev` 显式启用外部评分 |
| `--read-budget` | `1200` | 首轮推荐源码 960 tokens，预留补读 240 tokens |
| `--list-budget` | `600` | 控制清单序列化输出成本；JSON 较长时可提高 |
| `--candidate-limit` | `60` | 控制候选数，可能影响召回与评分提交量 |
| `--format` | `markdown` | 可选 `json` |
| `--save-plan` | 不保存 | 显式保存建议和指纹到目标外新文件 |
| `--tokenizer` | 关闭 | 显式使用可选 tiktoken / `cl100k_base` |

`--local-only` 保留为 `--mode local` 的兼容别名。阅读预算约束推荐范围，实际读取由用户或模型决定；清单外补读仍允许，其成本不能视为免费。

默认计数采用 `ceil(UTF-8 bytes/3)`。可选 tokenizer 需要安装 `tiktoken>=0.7,<1`，首次可能下载公开编码数据；可显式配置目标外的安全 `TIKTOKEN_CACHE_DIR`，该目录允许尚未创建。未显式配置时，需已有安全的外部默认缓存位置；默认位置未建立、路径不安全或包不可用时明确回退。

清单、推荐源码和 Jev 输入分别计量。源码捕获预算 32 MiB，单文件上限 1 MiB；两次指纹核验另计读取，成功命令累计目标源码读取可达约 96 MiB。代理 tokens、推荐源码成本和提交项数都不等于主模型上下文用量、实际 HTTP 请求数或账单；服务端 usage 和费用未知。边界及完整参数见 [使用说明](references/usage.md)。

## 当前验证与实验结果

0.3.2 发布前，207 项测试和公开副本的环境检查、本地检索、计划保存、补读去重、fixture 离线评估均通过。该结果验证工程行为，尚未证明总体任务成本节省。

| 评估 | 已观察结果 | 适用边界 |
| --- | --- | --- |
| [新题 v1](evidence/new-evaluation-v1/report.md) | 8 题本地模式配对中位数多 5.62% 检索文本；首轮与代表复跑答案模型盲评 4/4 | 未达到降低 15% 的成本目标 |
| [已见开发 Jev 试验](evidence/jev-pilot-v1/reader-costs.json) | 一题固定查询与候选池下少约 26.8% 检索文本 | 局部结果，不能推广为新题总体收益 |
| 新题 v2 | 22/30 初轮已保存；全题比较与代表复跑未完成 | 使用冻结的旧 0.3.0 实现，不代表当前版本效果 |

标签和答案均为模型审查，`human_reviewed=false`。历史评估累计真实 Jev 提交 90 项，见 [账本](evidence/jev-submissions.json)；实际 HTTP 数、usage 与费用未知。0.3.1 的日志硬链接、指纹读取计量、深 JSON 修复，以及 0.3.2 的临时目录和缓存修复见 [验证与修复摘要](references/validation-0.3.2.md)。

## 运行验证

```bash
python3 -m unittest discover -s tests -q

# 固定 mock 响应，不调用真实 Jev
python3 scripts/evaluate.py --offline-responses tests/fixtures/jev-responses.json
```

标准测试与 fixture 离线演示不依赖私有历史材料。生产入口不导入已撤回试验实现；实验专用测试与生产回归边界见 [使用说明](references/usage.md)。

## 文档与公开范围

- [Agent 使用入口](SKILL.md)
- [参数、安全边界与成本口径](references/usage.md)
- [0.3.2 验证与验收摘要](references/validation-0.3.2.md)
- [旧排序报告](references/evaluation-2026-10-06.md)与[已撤回检索消融](references/retrieval-ablation-2026-10-07.md)

公开仓库保留实现、测试和选定的评估摘要；原始读者日志、代码摘录、运行快照和内部续接记录留在作者本地。历史报告中的本地记录路径不能在公开仓库中直接打开或重放，范围见 [原始记录说明](references/runs/README.md)。

许可证：[MIT](LICENSE)。
