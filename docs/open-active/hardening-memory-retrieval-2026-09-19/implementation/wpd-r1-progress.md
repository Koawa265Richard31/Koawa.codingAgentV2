# WP-D R1 收口实施记录（闭环审查 2026-09-25 → 修复）

日期：2026-09-25。基线：`73270c2`。范围：闭环审查报告 R1（流头/CAS/静默吞没）
+ `tests/test_wp_d_projection.py` 夹具修复 + 生产 0 事实根因。R2–R5 未动。

**当前状态（2026-09-25 复验修正后已补齐）**：核心修复完成，且复验指出的
正式恢复入口及重启后状态展示缺口已实施——`resume` 对终态 turn 幂等补发并
在 payload 暴露状态；`status` 逐 turn 读取持久投影状态（翻页读取投影流 +
扫描 run-execution 交叉核对，重启后可用）；`result.projection-unavailable.v1`
事件有了持久读取方（`read_publication_status`）。经真实重启场景测试验证
（注入失败→close 重启→status 可见 pending→resume 补发→status 清零→二次
resume 无重复）。R2（首次回执时序、检索读取接投影流、scan/read 1000 条
上限、来源身份绑定）、R3、R4、R5 仍开放。

## 修复内容

### 1. 流头读取（R1 报告根因，审查已独立复现）

`retrieval/projection.py` 的 `_head_version` 原以 `after_version=-1, limit=2`
读取，而 `read_stream` 按版本升序返回——拿到的是最早两条而非流头；第三次
写入后第四次发布必现 `WrongExpectedVersion`（审查探针：5 发 3 存）。现改为
翻页走到短页（与 `recovery/execution.py` 的 `_stream_head_version` 同型）。

### 2. 守卫追加与冲突语义（`_append_guarded`）

- `WrongExpectedVersion`：重读流头后重试，上限 8 次；耗尽则抛
  `EventStoreError` 交上层登记，绝不静默跳过。
- 同身份重试：发布身份 = turn＋call＋事实 event_id，命令键与指纹确定；
  存储幂等检查先于版本检查，重复发布返回原回执，不写重复事件。
- 同身份不同内容：`IdempotencyConflict` 直接上抛（拒绝），不重试、不落部分写。

### 3. 投影不可用登记（设计 WP-D 事件族）

新增 `result.projection-unavailable.v1`：登记发布失败的事实（call_id、
body_ref、`publication_status: "failed"`、error_code）。error_code 只取异常
类名——异常原文（路径/payload）不进入持久事件。同失败重试幂等；不同错误
码按不同身份分别登记。

### 4. 生产可观察（runtime/app.py）

`_publish_result_projections` 重写：逐条隔离失败——某条失败后其余事实继续
发布；失败事实尽力写 unavailable 事件（该写失败时在状态里记
`registration_error`，不掩盖原始失败）；状态存入
`_projection_publications` 并并入 run/resume 结果 payload 的
`result_projections{published, failed, pending, scan_error}`。投影失败
不改写工具执行结果、不失败 turn、不触发工具重执行。

### 5. 生产 0 事实根因（审查未发现、重写夹具时实测确认）

`scan_test_results` 原只接受 `str` 或精确 `dict` 的 `context_item`，而
StoredEvent payload 冻结为 `MappingProxyType`（非 dict 实例）——生产路径
所有 `tool.result-recorded.v1` 事实全部被跳过，发布循环实际 0 输入。
旧测试靠后半段手工发布掩盖了这一点。现接受任意 `Mapping`。

### 6. 正式恢复入口与重启后状态展示（复验缺口，已实施）

- `retrieval/projection.py` 新增 `read_publication_status(store, turn_id)`：
  翻页读取投影流（无 1000 条硬上限），按 (call_id, body_ref.event_id) 身份
  归并——已发布身份计数；未发布且登记过 unavailable 的列出 error_code；
  未发布且从未尝试（scan 与 publish 之间崩溃）的 error_code 为 None；
  扫描自身失败记入 `scan_error`，绝不冒充“全部已发布”。这是
  `result.projection-unavailable.v1` 的第一个持久读取方。
- `runtime/app.py` `resume`：终态 turn 分支从纯 `turn_already_terminal`
  提前返回改为恢复入口——幂等重跑 `_publish_result_projections`，fresh
  状态并入 payload `result_projections`。
- `runtime/app.py` `status`：turn 文档逐个附加 `result_projections`（持久
  读取方，不依赖进程内字典，重启后可见）；读取方级失败记入 `scan_error`
  而非静默零值。

### 7. 恢复链测试（真实入口，无私有方法调用）

`RestartRecoveryEntryTest`：注入 test-before 发布失败 → `app.close()` 模拟
重启 → 新 AppRuntime 的 `status()` 持久暴露 pending（1 published +
test-before/EventStoreError）→ `resume(turn_id)` 返回 `turn_already_terminal`
且补发完成（published=2, failed=0）→ `status()` 清零 → 流上恰好 2 published
+ 1 历史 unavailable（无重复）→ 二次 `resume` 幂等零新增。
`DurablePublicationStatusTest`：store 级 never-attempted 分支（error_code
None 的 pending）。

### 8. 夹具与测试重写（tests/test_wp_d_projection.py）

- 工具路径修复：`build_verified_coding_tool_registry` + 脚本化
  CommandRunner，`run_test_profile` 真实执行（原夹具用编辑工具注册表调用
  不存在的 run_test_profile，turn 必 failed）。
- 全部用隔离 `TemporaryDirectory`；删除仓库固定 `FIXED_DIR` 及测试内
  `shutil.rmtree`（审查复验建议）。
- 不再在测试内复制 app 发布逻辑；生产发布由真实 `AppRuntime.run` 入口验证。

## 设计条款 → 生产执行点 → 测试映射

| 条款 | 生产执行点 | 测试 |
| --- | --- | --- |
| 流头正确 + ≥5 事实全发布 | `projection._head_version` | `test_five_facts_idempotent_retry_and_content_conflict`（版本 0–4 连续） |
| 同身份重试不重复 | `publish` 确定性身份+指纹 → 存储幂等回执 | 同上（5 发后重发全量仍 5 事件） |
| 同身份不同内容拒绝 | `IdempotencyConflict` 上抛 | 同上（冲突后流仍 5 事件） |
| 并发 CAS 冲突重试 | `_append_guarded` | `test_concurrent_head_advance_is_retried`（确定性注入一次并发推进） |
| 发布失败登记/重启恢复 | `register_publication_failure` | `test_failure_registration_is_durable_idempotent_and_recovers`（新 store 实例=重启） |
| 生产入口发布 + 状态暴露 | `app._execute → _publish_result_projections → _truth_outcome` | `test_run_publishes_projections_and_reports_status`（真实 run：验证注册表+真实测试 profile 子进程+git 证据+finalize） |
| 失败可观察、不失败 turn、不重执行 | `app._execute → _publish_result_projections → _truth_outcome` | `test_publication_failure_is_observable_and_turn_still_completes`（注入单点失败→后续事实仍发布→**手动**补发幂等无重复，调私有方法，非生产恢复入口） |
| 重启后 pending 可见 + 生产入口补发 | `app.resume` 终态分支 + `read_publication_status` + `app.status` | `test_restart_status_shows_pending_and_resume_republishes`（close 重启→status 持久可见→resume 补发→status 清零→二次 resume 无重复，全程真实入口） |
| never-attempted 事实暴露 | `read_publication_status` 扫描交叉核对 | `test_never_attempted_fact_is_pending_without_error_code` |

## 验证等级（准确区分，2026-09-25 复验修正后）

- **已接线**：发布挂在 turn 终态后的 `app._execute`（发布时序本身是 R2
  待重构项——设计的“首次回执来自已提交投影”仍未满足）。
- **已单测**：store 级五事实/幂等/内容冲突/CAS 竞争/不可用登记/重启补发
  幂等（新 store 实例重发全量不重复）。
- **已生产入口集成验证**：`AppRuntime.run` 全链路（真实工具执行→
  run-execution 事实→扫描→发布→outcome payload），含故障注入下的失败可
  观察。
- **R1 残余（复验确认，当日已补齐）**：正式恢复入口与重启后状态展示
  已实施——`resume` 终态分支幂等补发并在 payload 暴露状态；`status`
  经持久读取方逐 turn 展示；`read_publication_status` 翻页读取投影流并
  与 run-execution 扫描交叉核对（never-attempted 事实以 error_code=None
  的 pending 暴露）。经真实重启场景测试验证（`RestartRecoveryEntryTest`）。
  store 级重启补发幂等（新 store 实例重发全量不重复）继续成立。
- **未做**：真实模型任务端到端验证（需 provider 预算）；R2/R3/R4/R5 全部
  未动。

## 局限与未覆盖（保持可见）

- 未构造 >500 事件流实测 head 翻页（与 recovery 同型代码，代码推断级）。
- unavailable 与 published 事件同流共存；`read_publication_status` 已按
  event_type 区分消费，WP-E 重建索引时沿用同一区分。
- 并发验证为确定性单次注入，非多线程压测。
- `read()`/`scan_test_results` 仍 1000 条上限、无翻页与不完整标记（R2；
  `read_publication_status` 的投影流翻页已实现，run-execution 扫描侧未动）。
- 来源识别仍仅凭回执 `test_output_policy` 字段，未绑定可信调用身份（R2）。
