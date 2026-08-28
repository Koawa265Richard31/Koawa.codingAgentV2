# D23：长任务记忆闭环（跨回合结论 / 回合内成组压缩 / 检索 / journal）

> 状态：PLANNED（2026-08-27，设计已收口，实施前仍需单独详细实现文档）。
> 北极星：让同一 coding 任务在长回合、进程重启和多次交互回合后，仍能基于可重放事实继续工作，
> 而不是依赖无限增长的 prompt 或模型“自己记住”。
> 背景案例：DSH 逐字投影会话容量事故（session-b16565ec 导出）。本项目坚持“用记忆换上下文”，
> 但记忆必须有权威来源、可验证压缩、故障恢复和容量上限。

---

## 1. 当前差距与为什么必须同一切片闭环

| 编号 | 现状证据 | 长任务后果 |
|---|---|---|
| G1 失败回合零记忆 | `runtime/session.py` 的 `context_items()` 在 `final_text is None` 时跳过 assistant 回显 | 下一轮不知道刚才做过什么、为何失败，只能重新勘察 |
| G2 无稳定回合结论 | 成功轮主要依赖 final text；D22 summary 只处理最终回复失败且不进入下轮 | final text 不是可靠的任务状态蒸馏层 |
| G3 回合内上下文只增不减 | `AgentLoop` 每轮把 model output 和 tool result 继续 append；D6 reducer也只重放追加事实 | 单个复杂任务最终撞 provider context 或本地字符预算 |
| G4 既有 Compactor 未接执行主线 | D13 `Compactor` 能渲染摘要，但没有安全触发、事件事实或 D6 重建语义 | 内存里压缩即使成功，重启也会恢复成另一份上下文 |
| G5 词法召回过薄 | `_recall_score` 只做 casefold 子串和固定字段权重 | 长会话中稀有错误码、文件和结论召回不稳定 |
| G6 journal 依靠模型自觉 | `SessionJournal.write` 只由手动 `/journal` 触发，默认不回注 | 长任务缺少阶段性人工可读检查点 |
| G7 预算没有联动 | history、active run、summary、journal 各自有上限，没有统一 memory envelope | 局部都“有界”，组合后仍可能超过请求容量 |

只做跨回合结论不能解决一个回合内 20–30 轮工具循环；只做回合内压缩又会在下一交互回合丢失
失败和阶段结论。因此 D23 将两者作为一个闭环切片设计，但实现时按依赖顺序逐块提交。

## 2. 目标、不目标与适用范围

### 2.1 目标

1. 每个成功、失败、中断、超时或 outcome-unknown 的 Turn 都能重建一份有界 `TurnConclusion`。
2. interactive/session 的下一 fresh Turn 自动获得近期结论和有限失败回显。
3. active Run 在安全边界自动把最旧的完整执行组替换为压缩块，并可多次压缩。
4. 压缩前后工具调用配对、当前目标、约束、改动、测试证据、审批、未知副作用和预算事实不丢失。
5. 压缩决定、压缩结果和恢复投影全部可从事件重放；checkpoint 仍只是校验投影。
6. recall 在纯标准库下提升确定性、稀有词区分和时效性。
7. journal 有确定性提醒；任何 journal 写入仍遵循 I7 `JOURNAL_EXPORT` workspace effect。
8. 所有 memory 输入共同服从一个请求容量预算，无法安全缩减时稳定失败，不发送已知超界请求。

### 2.2 不目标

- 不持久化或回传 raw reasoning/reasoning_content/隐藏推理链。
- 不引入 embedding 或向量数据库；接口继续保留。
- 不做跨 Thread、跨 repo 或跨用户共享记忆。
- 不让摘要文本替代 ledger、workspace effect、approval、Run/Turn 或 test evidence 真相。
- 不在 D23 实现 TUI。

### 2.3 模式边界

- `TurnConclusion`、失败回显、recall、journal：仅 interactive/session 路径。
- 回合内成组压缩：task/run 与 interactive/session 都启用，因为它解决单 Run 长任务容量。
- 子 Agent：每个子 Run 独立压缩；父 Agent 只接收既有 durable child result/message，不复制子 Run raw context。

## 3. 统一记忆模型

### 3.1 三层事实

```text
Durable business facts
  Run / Turn / approval / tool ledger / MCP allocation / workspace effect / evidence
                     │ deterministic projection
                     ▼
Authoritative memory projection
  goal / status / errors / files / tests / open obligations / budgets / source refs
                     │ optional one-shot summarization
                     ▼
Untrusted summaries
  turn summary / in-run historical narrative / journal prose
```

任何摘要都必须同时携带其权威投影与 source range。摘要与事实冲突时，事实胜出；摘要不能授权状态变化、
不能解除审批、不能把 UNKNOWN 改成成功，也不能作为 completion evidence。

### 3.2 MemoryEnvelope

每次模型请求前构造一个 `MemoryEnvelope`，顺序固定：

```text
1. system/developer instructions（原样、永不压缩）
2. 当前 task/user goal 与最新 resume response（原样、永不压缩）
3. active-run compaction blocks（按 epoch 升序）
4. session conclusions block（interactive only）
5. session recent-window turns（interactive only）
6. active-run recent closed groups
7. 当前轮需要消费的其他合法 context items
```

Envelope 构建后进行协议验证、字符/字节预算验证和 tool-pair 验证，成功后才能形成 `ModelRequest`。

## 4. 跨回合 TurnConclusion

### 4.1 数据模型

```python
@dataclass(frozen=True, slots=True)
class TurnConclusion:
    thread_id: UUID
    turn_id: UUID
    run_id: UUID | None
    turn_status: str
    run_status: str | None
    request_summary: str
    error_codes: tuple[str, ...]
    successful_tools: tuple[str, ...]
    changed_files: tuple[str, ...]
    test_evidence_refs: tuple[EvidenceRef, ...]
    open_obligations: tuple[str, ...]
    uncertainty_codes: tuple[str, ...]
    authoritative_digest: str
    untrusted_summary: str | None
    source_heads_digest: str
```

权威字段从 I7 `RuntimeTruthDocument`、D6 execution facts、tool ledger、workspace effect index、approval 和
verification evidence 重建。不能只从旧 `TurnState` 或 final text 推导。`changed_files` 继续取 durable
apply_patch/workspace effect 结果，不使用可能延迟的 `git status`。

### 4.2 持久化决策（已拍板）

- 权威结论可重建，但为了稳定分页、摘要绑定和响应丢失幂等，在独立
  `turn-memory-{turn_id}` stream 追加 `memory.turn-conclusion-recorded.v1`。
- 不向已经 terminal 的 Turn stream 追加记忆事件，避免破坏 Turn reducer 和 terminal head。
- append 使用以下 `StreamPrecondition`：Turn terminal head、显式 Run terminal head、completion/failure
  evidence head、run-effect-index head及所有当前 effect heads。
- event 仅保存有界权威 DTO、摘要、source refs/digests；不保存 raw tool arguments/result、stdout/stderr、
  reasoning、credential 或完整 diff。
- 同一个 `turn_id + source_heads_digest` 产生确定 event/command identity。相同事实重试返回原 receipt；
  source head 已变化则必须重建并生成新 revision，旧 revision 标 stale，不可注入。

### 4.3 可选模型摘要

- 默认关闭；开启时固定使用该 Turn 已绑定的 provider/model，不允许配置另一模型。
- tool definitions 为空，最多一次请求，独立 `summary_request_id`，有单独字符/token/cost 上限。
- 摘要输入只有已脱敏权威 DTO，不输入 raw history。
- 失败、空输出或越界时回落为确定性权威文本，不做第二次模型回退。
- 持久文本标记 `[untrusted-turn-summary]`，必须经过 canonical text、redaction 和 UTF-8 byte 上限。

### 4.4 失败与中断回显

`SessionHistory.context_items()` 不再跳过 `final_text is None` 的 Turn。它生成明确标识的 assistant
投影消息：

```text
[reconstructed-turn-outcome]
status=failed
run_status=failed
errors=max_model_rounds_exceeded
attempted_tools=read_file,apply_patch,run_test
changed_files=src/a.py
uncertainty=none
[/reconstructed-turn-outcome]
```

该消息是系统重建结果，不声称是模型当时的原话。最多注入最近 `failed_echo_max_turns` 条；不含原始
参数、结果、推理、credential 和任意用户控制的未标记文本。

### 4.5 结论块去重规则（已拍板）

- 窗口内成功 Turn：只保留 recent-window 原投影，不进入结论块。
- 窗口内失败/中断/UNKNOWN Turn：只进入失败回显，不再进入结论块。
- 窗口外所有有结论的 Turn：进入 conclusions block。
- 相同 `turn_id + authoritative_digest` 最多出现一次。
- 先按 Turn 顺序选取，再从最旧开始删除直至满足条数和字符预算；不得半截断一条结论。

## 5. 回合内成组压缩

### 5.1 为什么必须按组

模型协议中的一次工具执行不是两个可独立删除的文本条目，而是：

```text
ModelTurn(assistant text + tool_call A/B/...)
  + ToolResult(A)
  + ToolResult(B)
  + ...全部对应结果
```

这个集合称为 `ClosedExecutionGroup`。只有所有 call_ref 唯一配对且结果已经 durable，组才是 closed。
压缩、删除、移动都以完整组为单位。任何 pending/in-progress/approval-waiting/UNKNOWN 工具调用都禁止
进入压缩源范围。

### 5.2 永不压缩的 anchors

- system/developer instructions；
- 当前 Run 的原始 user goal；
- 最新 typed resume response；
- 未解决 tool call、approval、unknown side effect 与 recovery obligation；
- 最新 authoritative execution state；
- 最近 `in_run_keep_groups` 个 closed groups；
- completion gate 所需 test/effect/evidence refs。

压缩块只替换更早的 closed groups，不改写 durable 原事件。

### 5.3 安全触发点

只允许在下一次 `ModelRequest` 构造前且同时满足：

1. D6 phase=`READY_FOR_MODEL`；
2. `pending_tool_calls == ()`；
3. 没有 tool ledger CLAIMED/PREPARED、MCP STARTED/UNKNOWN 或 workspace effect open/UNKNOWN 被误认为
   “已经总结完”；这些仍保留在 authoritative state；
4. 上一 ModelTurn 及其所有 tool result 已经持久化；
5. worker 仍持有当前 Run lease/fence；
6. cancellation 尚未触发。

禁止在 `READY_FOR_TOOL`、`TOOL_IN_PROGRESS`、模型流中途、工具结果写入中途或 terminal commit 中途压缩。

### 5.4 容量触发与目标

标准库阶段使用 canonical context UTF-8 bytes + chars 双计数，不伪装成精确 tokenizer：

- `request_context_soft_chars`：超过即尝试压缩；
- `request_context_hard_chars`：超过则禁止发送请求；
- `request_context_reserve_chars`：为下一轮 tool result、final answer 和 provider framing 保留；
- `compaction_target_chars`：压缩后必须低于该目标；
- `max_compaction_epochs_per_run`：阻止无界重复摘要；
- `max_compaction_source_groups`：一次最多处理的组数。

触发条件：`projected_chars + reserve > soft`。选择最旧 closed groups，直到预测结果不高于 target；若没有
足够安全组可压缩或压缩后仍超过 hard，写 typed failure `run.context-capacity-exhausted.v1`，Run/Turn 按
I7 原子收口为 FAILED 或 PAUSED（配置固定一种，D23 选择 FAILED，错误码
`context_capacity_exhausted`），绝不发送已知超界请求。

provider 明确返回 `context_length_exceeded` 且没有产生任何 output item 时，允许在安全点执行一次
emergency compaction，再以同一逻辑 model round identity 重试；transport unknown、部分流输出或响应
丢失不走该重试。

### 5.5 压缩产物

```python
@dataclass(frozen=True, slots=True)
class RunCompaction:
    run_id: UUID
    epoch: int
    source_first_version: int
    source_last_version: int
    source_event_ids_digest: str
    source_context_digest: str
    authoritative_projection: Mapping[str, JsonValue]
    untrusted_summary: str | None
    replacement_item: Mapping[str, JsonValue]
    replacement_digest: str
    resulting_context_digest: str
```

replacement item 是一个合法、有界、已标记的 `UserMessage` 投影：

```text
[run-history-compaction epoch=N]
[untrusted-history-summary]...[/untrusted-history-summary]
[authoritative-execution-state]...[/authoritative-execution-state]
[source-range first=... last=... digest=...]
[/run-history-compaction]
```

system/developer 指令永远不复制进该文本，避免把低优先级摘要伪装成高优先级指令。

### 5.6 摘要生成和降级

- 默认采用确定性 summary：工具名、稳定结果码、文件、测试、已完成/未完成 obligation。
- 可选模型 summary 与 TurnConclusion 相同：同一绑定模型、无工具、一次调用、独立预算、输入仅权威 DTO。
- 模型 summary 失败不阻止压缩；使用确定性 summary。
- 确定性结果仍无法达到 target 时，不继续截断权威字段；改为 capacity exhausted。

### 5.7 Durable event 与 D6 重建（闭环关键）

压缩不是仅修改内存 list。`DurableExecutionRecorder` 在原 `run-execution` stream 追加：

```text
run.context-compaction-intended.v1
run.context-compacted.v1
```

`intended` 绑定 epoch、source versions/event digest、prior context digest、目标预算；`compacted` 绑定
intended event、replacement、authoritative digest、resulting context digest和可选模型 receipt digest。
两者都用 exact expected stream version，响应丢失重试使用确定 command identity。

D6 `reduce_execution/reconstruct_execution` 必须：

1. 先按现有 model/tool facts 重建 source context；
2. 验证 compaction source 是连续、完整、closed 的 group 集合；
3. 验证 prior/source/replacement/resulting digest；
4. 用 replacement 原子替换 source groups；
5. 继续重放后续 model/tool facts；
6. 拒绝 epoch 跳号、重叠 source、压缩 anchors、悬空 call、伪造 digest和 compacted-without-intended。

Checkpoint 只与上述 reducer 输出比较，不可直接把 checkpoint 内的压缩 context 当真相。重启前后形成的
下一 `ModelRequest.input_items` 必须 byte-equivalent。

### 5.8 并发、取消与 crash windows

- intended 后进程退出：重启时没有 compacted，继续使用原 context，可重新 claim 同 epoch。
- summary 模型调用后、compacted commit 前退出：无 durable receipt 就不采用摘要；有 receipt 时按 exact
  request identity恢复，不能重复付费调用。
- compacted commit 后响应丢失：幂等重试返回原 receipt。
- cancellation 与摘要并发：传播 cancellation；已提交的 compacted fact保持有效，不反向改业务状态。
- old worker 使用旧 run-execution head 提交 compaction：CAS 失败且不能覆盖新 worker projection。

## 6. Recall 增强

接口保持 `recall(thread_id, query, limit)`，公式在设计阶段固定为：

```text
tokenize = Unicode casefold → 字母/数字/下划线 token → 去停用词 → 去重保序
idf(t) = log((1 + N) / (1 + df(t))) + 1
field_weight = request/final/conclusion 2.0, error 1.5, tool/file 1.0
recency = 0.5 + 0.5 * ((turn_position + 1) / N)
score = Σ(idf(token) * matched_field_weight) * recency
```

- `N` 只统计同 Thread 可召回 Turn，最多扫描 `recall_scan_max_turns` 个最新 Turn；
- query token 为空时返回空；没有 token 命中时不返回；
- error code 按 `._:-` 边界额外保留完整 token；
- 同分按新 Turn 优先，再按 `turn_id` 字符串升序，保证确定性；
- 召回结果只返回有界权威 DTO和标记后的 untrusted summary，不返回 raw event/tool body。

## 7. Journal 触发与写入安全

### 7.1 提醒

以下任一条件使下一 fresh interactive Turn 注入一条低优先级 memory reminder：

- 距最近一次成功 journal effect 已达到 `journal_remind_turns`；
- 最近 Turn 为 FAILED/TIMED_OUT/OUTCOME_UNKNOWN；
- 已发生 `journal_remind_changed_files` 个新 changed files。

提醒进入 `[session-memory-reminder]` UserMessage，不修改 system/developer instructions。

### 7.2 写入

- `/journal` 和未来任何自动 journal 都必须走 I7 `WorkspaceEffectKind.JOURNAL_EXPORT`：
  INTENDED → CLAIMED → APPLIED/FAILED_BEFORE_EFFECT/UNKNOWN。
- temp + fsync + replace，输入/pre/post digest 和 controller-relative ref 入 effect，正文不入 event。
- UNKNOWN 必须 reconcile，不允许盲重写。
- journal prose 一律标记 untrusted；默认不自动回注。
- 可选回注只读取最近一个 APPLIED journal ref，核对 digest、byte 上限和 workspace identity后注入。

D23 不自动替模型执行 `/journal`，只提供确定性提醒；因此没有未经授权的额外 workspace 写入。

## 8. 统一配置与硬上限

避免继续扩散 RuntimeConfig 顶层字段，新增：

```python
@dataclass(frozen=True, slots=True)
class MemoryConfig:
    conclusions_enabled: bool = True
    conclusion_max_chars: int = 512
    conclusion_recent_limit: int = 8
    conclusion_model_summary: bool = False
    failed_echo_max_turns: int = 3
    in_run_compaction_enabled: bool = True
    in_run_keep_groups: int = 4
    request_context_soft_chars: int = 48_000
    request_context_hard_chars: int = 64_000
    request_context_reserve_chars: int = 8_000
    compaction_target_chars: int = 36_000
    max_compaction_epochs_per_run: int = 16
    max_compaction_source_groups: int = 32
    compaction_summary_max_chars: int = 2_048
    recall_scan_max_turns: int = 256
    journal_remind_turns: int = 10
    journal_remind_changed_files: int = 20
    journal_inject_latest: bool = False
```

所有值有 schema allowlist、类型检查、上下界和关系校验：

```text
target < soft < hard
reserve < hard - target
keep_groups >= 1
conclusion/summary limits <= hard
```

旧 config 缺少 `memory` 时使用上述默认值；未知 key fail closed。配置 identity进入 execution seed，恢复时
沿用 seed 绑定值，不受后来 config 修改影响。

## 9. 安全与可信边界

1. 所有持久 memory 文本先 canonicalize/redact/size-check，JSON only，禁止 pickle。
2. credential canary、完整环境、raw diff、tool arguments/result、stdout/stderr、reasoning 不进入 conclusion、
   compaction event、recall index或journal提醒。
3. 模型摘要和 journal 统一用 `[untrusted-*]` 标记；其中出现的“已测试/已完成/已批准”不能改变权威字段。
4. replacement context 重新经过 `ModelContextItem` 构造与 tool pairing validator，不直接信任 JSON。
5. 压缩不会降低 policy scope、修改 tool catalog snapshot或生成新的 approval。
6. trace 只记录 compaction epoch、字符数和稳定结果码；trace 失败不能破坏已提交 memory/business fact。

## 10. 文件与实施顺序

### 10.1 文件

| 文件 | 变更 |
|---|---|
| `runtime/turn_conclusion.py`（新增） | 结论 DTO、权威构建、event store、source-head verifier |
| `runtime/session.py` | 失败回显、结论块、去重、recall、journal reminder |
| `runtime/memory.py`（新增） | `MemoryConfig`、MemoryEnvelope、统一预算 |
| `execution/compaction.py`（新增） | group parser/selector、projection、replacement validator |
| `execution/loop.py` | 每轮请求前 safe-point preflight/trigger；接收 durable compaction coordinator |
| `recovery/execution.py` | recorder 写 intended/compacted facts |
| `recovery/context.py` | D6 compaction reducer和重启重建 |
| `runtime/config.py` | nested memory config parse/validation/seed identity |
| `runtime/app.py` / `runtime/cli.py` | terminal conclusion hook、journal reminder/injection wiring |
| `workspace/effects.py` / session journal | JOURNAL_EXPORT 接线与 reconcile |
| `tests/test_d23_turn_conclusion.py` | 结论、失败回显、去重、安全 |
| `tests/test_d23_in_run_compaction.py` | group、触发、恢复、预算、crash windows |
| `tests/test_d23_memory_recall_journal.py` | recall公式、提醒、effect写入 |
| `tests/test_d23_long_task_e2e.py` | 多 epoch 长任务与重启 golden |

### 10.2 串行实施切块

1. D23-A：TurnConclusion event + source verifier，不接 session prompt。
2. D23-B：session 失败回显、结论块、去重和统一 MemoryConfig。
3. D23-C：ClosedExecutionGroup parser/validator，先只做纯函数红绿测试。
4. D23-D：run compaction events + D6 reducer；证明 restart byte-equivalent 后再接 loop。
5. D23-E：AgentLoop safe trigger、预算和 deterministic fallback。
6. D23-F：recall公式、journal reminder/JOURNAL_EXPORT。
7. D23-G：全量、故障矩阵、soak和真实 provider 验证。

每块必须全量绿后才进入下一块；不预建后续空模块。

## 11. 测试与故障矩阵

### 11.1 TurnConclusion/session

- 成功、失败、取消、超时、等待、Run UNKNOWN 都生成诚实权威结论；
- source head/evidence/effect index变化使旧结论 stale；
- response loss幂等，WrongExpectedVersion不产生半结论；
- 失败回显不含 raw参数/结果/reasoning/credential；
- 窗口内成功、失败回显、窗口外结论严格不重复；
- summary失败/空/超界回落确定性文本；
- TIMED_OUT在所有过滤器中都是terminal。

### 11.2 Group 与协议

- 一轮多个 tool call 必须和全部结果一起选择；
- 缺结果、重复call_ref、异轮结果、UNKNOWN结果拒绝压缩；
- instructions、goal、resume、open approval/effect不能进入source；
- replacement后 `ModelRequest` tool pairing合法；
- summary伪造“测试通过”不覆盖 authoritative negative evidence。

### 11.3 Durable/restart/crash

- intended后kill；summary receipt后kill；compacted commit后响应丢失；
- checkpoint发布前后kill；
- restart形成的下一请求与不中断执行 byte-equivalent；
- epoch跳号、source重叠、digest伪造、old worker CAS全部fail closed；
- cancellation、KeyboardInterrupt/SystemExit不被摘要/trace吞掉。

### 11.4 容量

- soft以下不压缩；soft以上压至target；hard以上不发送请求；
- 没有安全组时稳定 `context_capacity_exhausted`；
- 至少连续16个compaction epoch不增长未释放内存/线程/handle；
- 100轮 scripted coding loop中请求context保持有界，事实流仍可完整重放；
- optional model summary调用次数/字符/费用受硬上限，失败不递归调用。

### 11.5 Recall/journal

- tokenize/IDF/recency公式逐项golden，tie-break确定；
- error code、文件、工具、结论可召回，停用词不制造结果；
- scan limit生效，历史再大也有界；
- journal提醒时点确定；写入走effect；response loss幂等；UNKNOWN不盲写；
- journal injection默认关闭，开启时tamper/digest/oversize均拒绝。

## 12. Mandatory E2E 与验收证据

### 12.1 离线 scripted golden（PR mandatory）

构造一个至少100 model rounds、包含多工具调用组、失败工具、文件修改、测试和一次进程重启的任务：

1. 至少发生3次 in-run compaction；
2. 每次请求低于hard并保留reserve；
3. 重启前后下一请求一致；
4. 最终 completion gate读取真实effect/test evidence成功；
5. 下一 interactive Turn问“刚才卡在哪里/做了什么”，能从结论和失败回显获得正确事实；
6. DB canary扫描无secret/raw reasoning/raw tool body；
7. 无悬空tool call、active Run、lease、线程或临时文件。

### 12.2 真实 provider（D23 COMPLETE 必需，人工 opt-in）

- 场景A：故意失败一轮，下一轮询问失败点；回答必须命中权威错误码/工具/文件。
- 场景B：单Run持续到触发至少一次压缩，后续模型仍能引用目标、改动和未完成事项。
- 证据绑定 exact commit/config/provider/model 和 request receipts；内容脱敏后存入 `examples/`。
- provider失败不影响离线正确性，但没有该证据不得把路线图 D23 标为 COMPLETE。

## 13. Definition of Done

同时满足以下条件才可将 D23 标记 COMPLETE：

1. D23-A 至 D23-G 全部实现，没有仅定义未使用的模块；
2. TurnConclusion、失败回显、成组压缩、D6重建、recall、journal和统一预算全部接 production path；
3. §11 专项测试、相关 D1/D2/D4/D6/D13/D16/D19/D21/D22 回归及全量测试全绿；
4. §12.1 scripted golden通过，至少三次compaction且restart byte-equivalent；
5. §12.2真实provider两项证据通过并绑定当前commit；
6. unexpected skip、ResourceWarning、遗留process/thread/handle为零；
7. credential/raw reasoning/raw tool body canary为零；
8. roadmap、README、config example、错误码表和本设计与代码一致。

## 14. 已知风险与控制

| 风险 | 控制 |
|---|---|
| 摘要遗漏关键义务 | 权威 projection + anchors 永不由摘要替代 |
| 工具配对损坏 | ClosedExecutionGroup validator + replacement后二次协议校验 |
| 重启恢复漂移 | durable intended/compacted facts + D6 reducer digest校验 |
| 摘要额外花费 | 默认确定性摘要；模型摘要默认关且一次调用/独立预算 |
| prompt injection | untrusted标记、低优先级UserMessage、权威字段独立 |
| Git/journal副作用不确定 | I7 workspace effect ledger + reconcile |
| 字符估算和provider token不一致 | soft/hard/reserve保守边界 + explicit provider context error单次安全重试 |
| 历史事件持续增长 | 事件事实允许增长但请求投影有界；I8 projection/index/容量基线负责存储侧证据 |

该设计不声称“摘要让模型拥有无限记忆”。它保证的是：历史事实仍完整可审计，发送给模型的投影有界，
每次丢弃都发生在完整、安全、可重放的执行组边界，并且任何无法证明安全的情况都会 fail closed。
