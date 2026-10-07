# jev-context

把编码任务变成预算内的代码阅读清单。适合不熟悉的仓库、跨模块问题或入口不明确的任务；输出仓库相对路径、行号和补读提示，供 agent 或开发者继续读取与核实。

项目只读目标仓库，不执行目标代码。已知的小文件或代码片段可以直接阅读，无须先运行检索。

## 能力

- 使用 `rg` 检索字面量、相对路径和 Python 符号，结合本地证据选择候选。
- 对 Python 使用 AST 提取函数、方法或类，并补充有界的一跳依赖和测试线索；其他语言、文档和语法损坏文件使用文本窗口。
- 可调用外部 Jev CLI 为候选评分，再生成预算内阅读清单；也可完全使用本地排序。
- 报告清单、推荐源码和 Jev 输入的独立成本估计，并标记 `incomplete`、`deferred` 和低置信度的 `uncertain`。

清单是阅读建议。没有入选不代表文件无关，潜在依赖也不等于准确调用图；信息不足时应补读或重新检索。行号为一基闭区间，文件变化后须重新定位。

## 环境与快速开始

需要 **Python 3.11+** 和 **ripgrep (`rg`)**。核心实现只使用 Python 标准库，可直接运行脚本。Jev 是独立安装、独立配置的可选外部 CLI，本项目不管理它的安装、鉴权或 provider。

```bash
git clone https://github.com/xiaoxiaomugong/jev-context.git
cd jev-context

python3 scripts/jev-context.py --help
python3 scripts/jev-context.py \
  --repo tests/fixtures/shop \
  --task '修复发票金额舍入' \
  --term invoice --term rounding \
  --local-only
```

`--local-only` 不启动 Jev，也不向外部服务提交评分。将 `--repo` 换成目标仓库即可用于自己的项目；中文任务建议附带推导出的英文术语，脚本不负责翻译。

如果已安装并配置兼容的 Jev CLI，去掉 `--local-only` 即会尝试外部评分：

```bash
python3 scripts/jev-context.py \
  --repo tests/fixtures/shop \
  --task '修复过期令牌处理' \
  --term 'expired token' --path auth/entry.py \
  --format json
```

适配层按 Jev 0.3.2 的 JSONL 契约实现。可以用 `--jev-bin /path/to/jev` 指定已有可执行文件。启用评分时，**任务与候选卡（包含代码摘录、路径、符号等）会经 Jev 发给其配置的 provider**。只对允许发送给该 provider 的代码启用此功能。

## 用作 skill

仓库根目录的 [SKILL.md](SKILL.md) 是 agent 使用入口。可以让 agent 读取该文件，也可以将完整仓库放入支持此格式的工具的 skills 目录。例如 Codex 的本地 skills 目录：

```bash
git clone https://github.com/xiaoxiaomugong/jev-context.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/jev-context"
```

运行时使用实际安装位置下 `scripts/jev-context.py` 的绝对路径，并通过 `--repo` 指向目标项目。脚本不会自动注册 skill 或修改全局配置。

## 预算、降级与数据

默认 `--read-budget 8000`，其中首轮 6400、补读预留 1600；`--list-budget 1000` 单独限制清单输出，`--candidate-limit 60` 限制候选数。`--term`、`--path`、`--symbol` 可重复，长任务可用 `--task-file`。JSON 清单较长时可提高 `--list-budget`。

CLI 默认尝试 Jev。缺失 CLI、鉴权失败、超时或部分结果错误时保留成功评分，并对其余候选使用明确标记的本地降级；失败不会伪装成零分。`--jev-timeout 45` 控制整批超时，适配层不额外重试。

未安装可选 `tiktoken` 时，用 `ceil(UTF-8 bytes/3)` 估算 tokens；可用时使用 `cl100k_base`。这些数值不等于主模型精确用量或服务端账单。提交候选数也不等于实际 HTTP 请求数，provider 包装、输出与 Jev 内置重试成本未知。

检索尊重仓库 ignore 规则，排除常见密钥文件、二进制、生成目录和符号链接，并受扫描与候选上限约束。默认输出不含候选源码或 CLI 原始回显；只有显式设置 `--debug-dir` 才保存本地候选与评分材料，这些材料仍可能含私有代码。

详细参数、边界与成本口径见 [使用说明](references/usage.md)；评分定义见 [relevance spec](references/relevance-spec.json)。

## 验证与实验

在仓库根目录运行：

```bash
python3 -m unittest discover -s tests -v

# 固定 mock 响应，只验证评估流程，不调用真实 Jev
python3 scripts/evaluate.py \
  --offline-responses tests/fixtures/jev-responses.json
```

[Jev 排序评估](references/evaluation-2026-10-06.md) 包含小 fixture 与 Click 8.1.8 单仓库样本：部分预算下排序后覆盖提高，少量实际阅读未证明整体上下文成本节省。标签及答案尚需人工复核，不能据此宣称普遍 token 节省或任务正确率提升。

[检索消融报告](references/retrieval-ablation-2026-10-07.md) 保留了覆盖改善但阅读预算下退化的试验；两项试验策略均已撤回，没有进入生产检索逻辑。评估材料用于复核方法与失败案例，不构成稳定收益承诺。

公开仓库保留实现、测试与汇总报告；原始评分、代码快照及 reader 日志未公开，历史报告中的“本地记录”路径对应作者保留的材料，无法直接重放这些原快照；公开范围见 [实验记录说明](references/runs/README.md)。可以运行上述 fixture 离线评估，或按使用说明准备指定版本的目标仓库，在新的空快照目录重新采集；真实评分会重新向 provider 提交候选。

## 许可证

[MIT](LICENSE)。
