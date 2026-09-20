# F20 差分结果（2026-09-19 终）

用归档的 run11 状态库做精确差分：

1. **最小复现未复现**（前缀探针 8 次压缩正常）；
2. **run11 真实前缀重建只有 1 个 journal-reminder、零历史 turn**——"大前缀"假设不成立；
3. **新观察（记为 F22，P2）**：run10 的失败回合（c044db14，terminal=failed）未被 `from_thread` 投影进 run11 的前缀——与"D13-D23-003 记录的 from_thread 含失败项"的描述不符（该描述基于测试构造；真实库上失败 turn 未出现，原因待查：可能 from_thread 只取 completed、或 failed turn 的 user_input/final_text 过滤）。

F20 根因因此仍开放，已知条件更新为：
- 同一 turn 内多轮压缩正常（run10：18 次）；
- 后续 turn（run11，前缀极小）首次压缩即 `compaction_source_range_missing`；
- 区分变量只剩"**非首 turn**"与"**前序 turn 有 18 次压缩事实的 run-execution 流**"——run11 的 recorder 是新 run 新流，不应受 run10 影响；除非 `_source_versions` 播种在新 turn 上因 initial_context 只有 1 项而与 loop 的 context 索引错位（loop context = instructions + prefix + input，recorder 播种同样 3+ 项…）。
- 下窗口：直接在 run11 归档库上重放 run11 turn 的事实流，打印 `_source_versions` 与 compact 批次的 first/last 索引，一锤定音。
