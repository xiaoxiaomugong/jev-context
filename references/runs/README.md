# 实验记录的公开范围

公开仓库不包含原始评分快照、缓存的第三方源码、reader 对话与事件日志、完整实验输出或基线压缩归档。这些材料保留在作者本地；历史汇总报告中的“本地记录”路径用于说明证据来源，不能在公开仓库中直接打开或重放。

仅保留本项目自己的两份冻结检索实现，供测试与重新运行本地消融使用：

- [原始基线](2026-10-06/retrieval-ablation/baseline/scripts/jev_context/retrieval.py)
- [已撤回的试验实现](2026-10-06/retrieval-ablation/trial-v2/retrieval.py)

这两份文件不包含评估目标仓库的源码。生产入口使用 `scripts/jev_context/retrieval.py`，不导入试验实现。

汇总报告和标注任务位于上级 `references/` 目录。可按 [使用说明](../usage.md) 在新的目标仓库和输出目录重新采集；fixture 离线评估不需要原始记录，也不会调用 Jev。
