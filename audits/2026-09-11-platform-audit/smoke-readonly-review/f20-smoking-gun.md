# F20 决定性证据（2026-09-19 终）

run11 turn（f37fe3da）事实流（34 facts）：
- `run.context-seeded.v2`：**`context: []`——零播种项**；
- `run.context-compaction-intended.v1`：source_first_version=0、source_last_version=18、epoch=1；
- `run.context-compacted.v1`：**已落盘**（首次压缩事实层"成功"）；
- 随后轮次的下一次压缩触发 `CheckpointError(compaction_source_range_missing)` → CheckpointError 逃过 fail_turn（F21，已修）→ corrupt-log。

根因判定：**loop 跟踪 19 项上下文，recorder 播种却是 0 项**——即 f37fe3da 的 recorder 构造收到了空 `initial_context`（chat 路径 execute() 的 context 构造与 recorder 播种之间出现分叉：resume/fresh 分支判定、instructions 传递、或 build_worker(task_mode=False) 的 initial_context 未达 recorder）。首次压缩之所以能"成功"，是因为空表下映射走了意外通路；错位在第二轮压缩必然爆炸。

下窗口修复入口（按序）：
1. 查 f37fe3da 为何 recorder.initial_context 为空：worker.execute 的 fresh/resume 分支 + build_worker(task_mode=False) 的 instructions/initial_context 传递（对照 d16 交互测试为何未拦住）；
2. 不变量补丁：recorder 播种数 == loop 初始 context 数，为 0 时 fail-fast（拒绝 durable start 而非埋雷）；
3. 修复后以同线程任务2回归（4 元内可完成）。
