# D11：Durable Multi-Agent Control Plane

## 1. 完成边界

D11 证明的不是“并行调两次模型”，而是有身份、状态、失败传播与持久化的
parent/child Agent 控制面：graph、mailbox、budget、attempt/run fence、orphan
接管全部落在 D1 Event Store 上，重启后从事件流重建。

已实现：

- `spawn_agent` / `send_message` / `followup_task` / `wait_agents` /
  `interrupt_agent` / `list_agents` 等价 API；
- 状态机 CREATED / RUNNING / WAITING / COMPLETED / FAILED / CANCELLED /
  ORPHANED；每个 attempt 有 run_id 与 lease；旧 run 的所有 terminal/message
  提交被 exact-version + run_id fence 拒绝；
- durable mailbox：message_id、per-agent sequence、semantic idempotency key、
  QUEUED/DELIVERED/ACKED/CANCELLED；重启或重复投递不产生两份任务；
- 深度/总数/每父并发预算在 SQLite 中原子 reserve，终态 release；
- 子权限只收窄（scopes 来自父配置，read-only allowlist 硬限制）；
- 失败隔离：一个 worker 失败不阻塞兄弟 worker；父 `wait_agents` 收敛；
- cancel 通过 CANCEL 消息传播；orphan 发现与 takeover 产生新 run；
- 进程重启：新 `AgentControlPlane` 从同一 SQLite 重建 graph/mailbox/budget。

尚未开放：

- 写工具、patch、test、宿主命令、network、MCP（D12 起逐日开放）；
- 每 Agent 独立 worktree/容器（D12）；
- fork context 的字节级快照（当前区分 fresh/fork 模式标记，不复制正文）。

## 2. 事件与状态

~~~text
agent.spawned.v1        -> CREATED
agent.started.v1        -> RUNNING（run_id + lease）
agent.heartbeat.v1      -> RUNNING（续租）
agent.orphaned.v1       -> ORPHANED（abandoned_run_id）
agent.taken-over.v1     -> RUNNING（attempt+1、新 run_id）
agent.completed.v1      -> COMPLETED
agent.failed.v1         -> FAILED
agent.cancelled.v1      -> CANCELLED

message.enqueued.v1     -> QUEUED
message.delivered.v1    -> DELIVERED（绑定 run_id）
message.acked.v1        -> ACKED
message.cancelled.v1    -> CANCELLED

budget.reserved.v1 / budget.released.v1（root 预算流）
~~~

## 3. 预算与 fence

- 每个 root 一条 `agent-budget` 流；spawn 以 expected version append
  `budget.reserved.v1`，并发 spawn 由流 CAS 串行化；超过
  `max_total_agents` 抛 `agent_total_exceeded`。
- 深度与每父并发在 spawn 时按 graph 检查：`agent_depth_exceeded`、
  `agent_concurrency_exceeded`。
- 所有 terminal/message 提交要求 `record.run_id == 提交者 run_id`，否则
  `stale_agent_run_fenced`；orphan 接管后旧 run 的迟到结果被拒绝。

## 4. 稳定错误码

| 类别 | 错误码 |
|---|---|
| spawn | `parent_agent_missing`、`parent_agent_not_active`、`agent_spawn_cycle`、`agent_depth_exceeded`、`agent_concurrency_exceeded`、`agent_total_exceeded`、`invalid_agent_task`、`invalid_agent_principal` |
| attempt | `agent_attempt_state_invalid`、`agent_version_stale`、`stale_agent_run_fenced` |
| mailbox | `invalid_message_idempotency`、`invalid_message_body_ref`、`message_target_missing`、`message_target_terminal`、`message_missing` |
| recovery | `agent_missing`、`agent_clock_must_be_aware`、`corrupt_agent_stream`、`corrupt_mailbox_stream` |

## 5. 测试矩阵

| 文件 | 覆盖 |
|---|---|
| `tests/test_d11_agent_control.py`（7） | spawn 持久化/预算、深度/并发/总数限制、terminal 释放预算、cycle 检测、消息去重与终态目标、stale run fence + orphan takeover、interrupt CANCEL、list/wait |
| `tests/test_d11_agent_scheduler.py`（5） | 两个并行只读 worker 收敛、一个失败父收敛、cancel、写工具禁止（`d11_write_forbidden`）、重启恢复 orphan 且旧 run 被 fence |

聚焦命令：

~~~powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = 'src'
python -W error::ResourceWarning -B -m unittest tests.test_d11_agent_control tests.test_d11_agent_scheduler -v
~~~

## 6. 示例

`examples/day11_multi_agent_readonly.py`：三个并行 worker（两个只读成功、一个
失败），父 `wait_agents` 收敛，预算归零；再演示过期 lease → orphan → 新进程
接管 → attempt=2 完成且旧 run 被 fence。

## 7. 推迟到后续日的风险

- 每 Agent 独立 worktree 与容器（D12）。
- fork context 字节级快照与 artifact 集成（D12）。
- 真实模型 provider 与 token/time/tool 计量（D14 eval 时统一）。
