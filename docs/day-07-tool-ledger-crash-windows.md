# D7 Tool Ledger：崩溃窗口、认领栅栏与不确定结果

## 1. 交付结论

D7 在 D6 的持久执行事实之外增加一条独立的 `tool-execution` 事件流。D6 回答
“模型已经产生了什么、还剩哪些调用”，D7 回答“某个逻辑工具调用是否获得过执行权、
由哪个物理 Run 认领、结果是否确定”。两者不能互相替代。

持久化 Worker 只要配置了工具和 CheckpointStore，就必须使用 `LedgerExecutor`；
直接把 Registry 或其他 ToolExecutor 接到该路径会在启动前被拒绝。历史 D1–D5 的
无 checkpoint 演示仍可使用进程内 executor，但不具备 D7 的恢复声明。

## 2. 稳定身份与物理认领

逻辑 `execution_id` 由以下持久事实派生：

```text
turn_id + model_turn_id + call_id
```

它不包含 `run_id`。tool name、规范化 arguments SHA-256、参数字节数、
side-effect class 和 recovery mode 是该 execution 的不可变语义字段。相同 key
再次出现时：

- 语义相同：返回原 ledger 事实；
- tool、参数或恢复分类变化：`tool_execution_identity_conflict`；
- 新 attempt/new run：仍使用相同 execution，不生成第二份副作用身份。

`claimant_run_id`、`claim_epoch`、`claim_token` 只描述一次物理认领。
每次 reclaim 增加 epoch 并换 token。SUCCEEDED、FAILED、OUTCOME_UNKNOWN 及人工/
查询解析都要求 ledger exact version 与原 claim token；旧 claimant 不能覆盖新决策。

对于支持幂等键的外部系统，handler 从 `ToolExecutionContext.execution_id` 取得稳定
key，并应把它传给外部 API。仅在本地记录 execution_id 不能自动赋予第三方系统
exactly-once。

## 3. 写前日志与状态机

```text
PREPARED --active Turn exact-version/current-run CAS--> CLAIMED
CLAIMED  --definite result---------------------------> SUCCEEDED | FAILED
CLAIMED  --cannot prove outcome----------------------> OUTCOME_UNKNOWN
CLAIMED  --terminal NOT_APPLIED query----------------> PREPARED
OUTCOME_UNKNOWN --authoritative/operator result------> SUCCEEDED | FAILED
OUTCOME_UNKNOWN --terminal NOT_APPLIED decision------> PREPARED
```

只有 CLAIMED 能进入 handler。PREPARED 与 CLAIMED append 都在同一 SQLite 事务里用
D6 `StreamPrecondition` 校验 Turn exact version、最后事件 `turn.started.v1` 和
current `run_id`。cancel 与 claim 谁先提交，谁就赢得该 CAS。

事件均为版本化 JSON：

- `tool.execution-prepared.v1`
- `tool.execution-claimed.v1`
- `tool.execution-reclaimed.v1`
- `tool.execution-succeeded.v1`
- `tool.execution-failed.v1`
- `tool.execution-not-applied.v1`
- `tool.execution-outcome-unknown.v1`

结果事实保存脱敏内容、原内容摘要、UTF-8 字节数和 error flag；不保存 handler、
client、subprocess handle、凭据或 Python object。每个 ledger command 都提供排除
`occurred_at` 的稳定语义 fingerprint；相同 PREPARED 并发/重试返回同一事实，
不同 claimant 则由 ledger exact version 决出唯一赢家。

## 4. 四类恢复策略

| side-effect class | recovery mode | orphaned CLAIMED |
|---|---|---|
| READ_ONLY | RETRY | 新 Run 可 reclaim 后重复读取 |
| IDEMPOTENT_WRITE | RETRY | 使用相同 execution_id 收敛 |
| NON_IDEMPOTENT_WRITE | AUTHORITATIVE_QUERY | 先查外部系统，再提交 result、NOT_APPLIED 或 UNKNOWN |
| NON_IDEMPOTENT_WRITE | MANUAL | 转为/保持 OUTCOME_UNKNOWN，等待人工证据 |

`LookupOutcome.NOT_APPLIED` 是强契约：查询方必须证明旧请求没有应用，并且以后也
不可能再应用；普通的“暂时没查到”只能返回 UNKNOWN。恢复协调器先把旧 Turn/Run
fence 为 QUEUED，再允许查询释放 claim，避免旧 claimant 仍有提交权时回到 PREPARED。

权威查询暂时失败或返回 UNKNOWN 后，记录可继续被查询；它不是不可逆死状态。
`resolve_unknown_result` 和 `resolve_unknown_not_applied` 为人工/查询方提供带
claim-token fence 的收敛入口。

普通 handler Exception 不泄漏原异常。read-only/idempotent profile 保持 CLAIMED 并
抛出 typed `tool_retry_required`；高风险 profile 转为 OUTCOME_UNKNOWN。AgentLoop/
TurnWorker 保留该 recovery-blocked 控制流，不把仍需查询的 Turn 错写成普通 FAILED。

## 5. 六个崩溃窗口

1. **claim 前 kill**：最多留下 PREPARED，副作用计数为 0；新 Run 可 claim。
2. **claim commit 后、handler 前 kill**：留下 CLAIMED，副作用计数为 0；按 profile
   retry/query/manual。
3. **handler 执行中 kill**：留下 CLAIMED，外部副作用可能发生；非幂等不可查询路径
   转 OUTCOME_UNKNOWN。
4. **handler 返回后、result commit 前 kill**：同样不能从缺失 result 推断未成功；
   queryable/manual 路径不盲重试。
5. **result commit 后、D6 checkpoint 前 kill**：ledger 已是 SUCCEEDED/FAILED；
   新 Run 复用持久结果，handler 调用次数不增加。
6. **cancel 与 claim 并发**：cancel 先提交，PREPARED/CLAIM append 的 Turn CAS 失败；
   claim 先提交，允许该 claim 用 token 收敛结果，但 ownership guard 会阻止开始下一调用。

父测试进程会在前五个边界收到子进程 durable marker 后真实 `process.kill`，再用新
SQLite/EventStore 实例重建 ledger；第六个窗口以同一 Turn stream 上的两个提交顺序
分别验证。

## 6. 脱敏参数的恢复边界

D6 不持久化凭据，pending ToolCall 中的敏感参数会变成 `[REDACTED]`。因此：

- ledger 已有 SUCCEEDED/FAILED 时，可以按 ModelCallRef 复用脱敏后的持久结果；
- queryable execution 可以只按稳定 execution_id 查询；
- 需要再次调用 handler、但原参数已经被脱敏时，返回
  `recovered_tool_arguments_unavailable`，不得把占位符当真实参数执行；
- 用户必须重新提供凭据/参数，或由人工/权威查询解析原 execution。

这是“不持久化 credential”和“自动重放”之间刻意选择的 fail-closed 边界。

## 7. Public API 与稳定错误

公共入口位于 `koawa_agent_v2.ledger`：

- `ToolLedgerStore`、`LedgerExecutor`、`ToolRecoveryManager`
- `ToolExecutionRecord`、`ToolExecutionState`、`DurableToolResult`
- `ToolRecoveryProfile`、`SideEffectClass`、`RecoveryMode`
- 四个内置 profile、`LookupResult`/`LookupOutcome`
- `logical_execution_id`、`canonical_arguments_digest`

关键稳定错误包括 `durable_turn_identity_required`、
`tool_execution_identity_conflict`、`tool_claim_already_active`、
`tool_claim_requires_recovery`、`stale_tool_claim`、
`tool_retry_required`、`tool_outcome_unknown` 和
`recovered_tool_arguments_unavailable`。

## 8. 验证命令与明确不声明

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B -m unittest discover -s tests -v
python -B examples/day07_tool_ledger_recovery.py
```

2026-08-20 的实际验收使用 Python 3.14（满足项目 3.12+ 下限）：D7 专项
14 tests / OK；全量 191 tests / OK，3 个既有平台能力 skip；D7 示例退出码为 0。

D7 证明的是 write-ahead claim、old-claim fencing、按恢复分类收敛，以及面对未知结果
时不盲目重复。它不声称任意文件系统、HTTP/MCP 服务或第三方 API 自动 exactly-once。
D8 的容器隔离、D9 的 durable approval/policy、D10 的 MCP binding/idempotency
仍是后续切片。
