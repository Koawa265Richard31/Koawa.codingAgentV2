# F20/F21 诊断与状态（2026-09-19 终）

## F21（新，P1）：CheckpointError 逃过 worker fail_turn → turn 卡 RUNNING → 线程阻塞

链条：run11 的压缩失败以 `CheckpointError`（`compaction_source_range_missing`，recovery/execution.py:825-830）抛出——它**不是** `AgentLoopError`，worker 的 `except (AgentLoopError, ModelError)` 分支接不住 → 直接逃出 `execute()`，turn 留在 RUNNING + 过期租约。后果链（全部实测）：
1. 同线程新 turn 被 `create_turn` 拒绝："thread already has active turn"（cli 只给空 `runtime_error`，无 traceback，用户无入口）；
2. `resume` 该 turn → `AutomaticRecoveryBlocked("corrupt execution log")`——压缩序列（intended 落盘、compacted 因映射失败未落/不匹配）使重建器拒绝整条执行日志；
3. **唯一出路是换线程/新库**——同线程无任何用户可用的自救入口。

## 修复项（离线，下窗口）

1. worker.execute 增加 `except CheckpointError` → `fail_turn("d2:compaction_failed")`（turn 到终态，线程可继续）；
2. F20 根因：recorder 版本映射对 SessionHistory 注入前缀的覆盖对齐（loop 上下文索引 ↔ `_source_versions`）；
3. `compact()` 原子序审计：intended 已落、compacted 失败时的回滚/补偿语义（避免 corrupt-log 死局）；
4. CLI 空码异常给出 traceback 或可操作提示（本轮临时 loud 打印已验证有效，待正式化）。

## 状态

预算 4 元未动（全部失败发生在模型调用前/服务商错误，非消耗性）。冒烟目标（Agent 审查报告）仍未达成；恢复后先修 1-3，再以新线程跑报告回合。
