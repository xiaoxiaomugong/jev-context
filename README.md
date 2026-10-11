# jev-context

将代码任务变成简洁阅读建议，再按需读实现、测试和依赖。适合不熟悉的 Python 仓库、未知入口及跨模块行为；已知小文件直接读更合适。当前源码版本为 **0.3.2**。

默认完全本地，不启动 Jev、不发送评分请求。目标仓库只读，不导入或执行其代码；发现后安全捕获最多32MiB源码，搜索和解析使用同一临时快照。捕获与两次源码指纹核验的读取分开计量，成功命令的目标源码累计读取可达约96MiB；这不同于捕获预算。Python 使用 AST，其他语言和损坏 Python 明确文本降级。清单是建议，模型仍可正常 rg、读清单外文件并补读。

## 快速开始

必需 **Python 3.12+、ripgrep (rg)、POSIX 安全文件接口（macOS/Linux）**。核心只有标准库。实测版本见 [验证](references/validation-0.3.2.md)；Windows 未验证，不支持安全接口时明确拒绝。Python 3.11 未验证，本版不声明支持。

克隆仓库后，在根目录执行；也可复制完整交付文件夹到新位置，不依赖作者路径或私有历史材料：

```bash
git clone https://github.com/xiaoxiaomugong/jev-context.git
cd jev-context

python3 scripts/jev-context.py --check
python3 scripts/jev-context.py --repo tests/fixtures/shop \
  --task '解释发票舍入及测试' --term invoice --term rounding \
  --save-plan /tmp/invoice-plan.json
python3 scripts/read-context.py --repo tests/fixtures/shop \
  --range billing/amounts.py 4 7 \
  --plan /tmp/invoice-plan.json --state /tmp/invoice-seen.json
# 补读；同一 state 仅返回新行
python3 scripts/read-context.py --repo tests/fixtures/shop \
  --range billing/entry.py 1 9 --state /tmp/invoice-seen.json
```

plan 必须是新文件；重复演示请换名字，state 可复用。--check 无须 repo/task、不启动评分。缺 Python 时安装受支持解释器；低版本或缺 rg 退出 2。零命中不等于内容无关。

## 安装、升级、卸载

可直接把 [SKILL.md](SKILL.md) 交给 agent。安装需复制完整目录，不能只复制入口；目标目录存在时停止，不覆盖已有安装或其他 skill。脚本使用实际安装位置。

```bash
# 从交付父目录执行；目标不存在时才复制
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
test ! -e "${CODEX_HOME:-$HOME/.codex}/skills/jev-context" && \
  cp -R jev-context "${CODEX_HOME:-$HOME/.codex}/skills/jev-context"
```

升级先备份旧目录及自定义修改，在另一个新目录验证新版后自行切换名字。Git 安装先检查 git status，只有无本地修改时才 git pull --ff-only。卸载只删除确认属于本 skill 的文件夹，不删除整个 skills 目录。本版没有后台服务，也不修改全局配置。暂不提供 pip install .，直接运行随附脚本。

## 可选能力与预算

--mode jev 显式复用已安装和配置的外部 Jev，可用 --jev-bin /path/to/jev。任务、路径、符号和代码摘录会交给其 provider；本项目不读取凭证或管理 provider。缺 CLI、凭证、超时或部分错误时保留明确本地降级，不把失败当作不相关。Jev 0.3.2 JSONL 契约离线验证；本次累计已提交90项（45项已见开发试验、45项新评估），均获得有效评分；实际HTTP请求数、usage和费用未知。最新状态见[账本](evidence/jev-submissions.json)。

默认 --read-budget 1200（首轮960、预留240）、--list-budget 600、--candidate-limit 60。实际读取由模型决定，补读没有被禁止或计为免费。--local-only 保留为兼容别名。--format json 较长时可提高清单预算。

默认统一采用 ceil(UTF-8 bytes/3) 估算。显式 --tokenizer 使用可选 tiktoken/cl100k_base，首次可能下载公开编码数据；需配置目标外的 `TIKTOKEN_CACHE_DIR`（例如 `/tmp/jev-tokenizer-cache`），或已有安全外部默认缓存。缓存位置未建立或不安全、包不可用时报告回退。两者都不等于主模型 usage 或服务端账单。隔离环境可安装 `python3 -m pip install 'tiktoken>=0.7,<1'`；实际验证版本0.14.0。

**尚未达到优秀标准。** [新题v1](evidence/new-evaluation-v1/report.md) 的8题本地模式配对中位数多5.62%检索文本；所有两臂与代表复跑答案模型盲评4/4。[已见开发Jev试验](evidence/jev-pilot-v1/reader-costs.json) 有一次26.8%的局部收益，不能当作新题泛化证明或自动启用规则。状态与成本见 [公开验收摘要](references/validation-0.3.2.md)。

0.3.1修复了独立审查发现的硬链接日志写入、指纹读取漏计和深层JSON失败隔离问题；0.3.2再修复目标内临时目录与tokenizer缓存写入边界。当前v2评估继续使用已冻结0.3.0字节快照，并保留旧指标的计量限制，不将其改称当前版本性能结果。见[验证与修复摘要](references/validation-0.3.2.md)。

## 验证

```bash
python3 -m unittest discover -s tests -q
# mock 离线演示：不依赖私有历史快照，不调用真实Jev
python3 scripts/evaluate.py --offline-responses tests/fixtures/jev-responses.json
```

实验专用测试和生产回归的边界见 [usage](references/usage.md)。[旧排序报告](references/evaluation-2026-10-06.md)、[已撤回消融](references/retrieval-ablation-2026-10-07.md) 只作已见回归材料。[私有材料范围](references/runs/README.md) 与 [原标签提案](references/label-proposals-2026-10-06-retrieval.md) 的状态保留。许可证：[MIT](LICENSE)。

公开仓库仅保留选定的评估摘要与提交账本；原始读者日志、代码摘录、运行快照和内部续接记录留在作者本地。公开范围见 [验证摘要](references/validation-0.3.2.md)。
