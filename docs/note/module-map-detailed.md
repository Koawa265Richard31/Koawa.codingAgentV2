# 模块详细地图(自动生成)

生成时间:2026-11-03 04:51
生成方式:`python scripts/gen_module_map.py`(AST 解析,不导入代码)

**怎么用**:先看每节开头的职责行 → 再看「字段」找契约表 → 再看「方法/函数」找入口。
私有成员(`_x`)不列出;文档字符串截断到 110 字符。

## 目录

- [`agents/`](#agents) — 6 文件 / 23 类 / 14 函数
- [`context/`](#context) — 5 文件 / 9 类 / 2 函数
- [`control/`](#control) — 9 文件 / 46 类 / 20 函数
- [`editing/`](#editing) — 4 文件 / 14 类 / 8 函数
- [`execution/`](#execution) — 4 文件 / 19 类 / 3 函数
- [`ledger/`](#ledger) — 5 文件 / 17 类 / 3 函数
- [`mcp/`](#mcp) — 11 文件 / 55 类 / 20 函数
- [`model/`](#model) — 4 文件 / 39 类 / 2 函数
- [`plan/`](#plan) — 4 文件 / 7 类 / 3 函数
- [`recovery/`](#recovery) — 8 文件 / 24 类 / 23 函数
- [`retrieval/`](#retrieval) — 4 文件 / 5 类 / 24 函数
- [`runtime/`](#runtime) — 17 文件 / 44 类 / 26 函数
- [`sandbox/`](#sandbox) — 5 文件 / 14 类 / 12 函数
- [`security/`](#security) — 3 文件 / 3 类 / 5 函数
- [`telemetry/`](#telemetry) — 3 文件 / 15 类 / 6 函数
- [`tools/`](#tools) — 7 文件 / 22 类 / 7 函数
- [`verification/`](#verification) — 9 文件 / 32 类 / 11 函数
- [`workspace/`](#workspace) — 9 文件 / 31 类 / 7 函数

顶层模块:
- [`approval_service`](#approval_service) — 6 类 / 0 函数
- [`policy`](#policy) — 16 类 / 10 函数

## 关键契约速查

20 个模块里,先从这几张表开始看;其余是能力实现,不是判定边界。

| 文件 | 契约 | 管什么 |
| --- | --- | --- |
| `control/event_store.py` | StreamId / NewEvent / StreamWrite / AppendReceipt | 写入契约:精确版本 + 幂等键 |
| `control/models.py` | ThreadState / TurnState | 聚合状态:重放出来的唯一真相 |
| `policy.py` | Principal / PolicyRule / ResolvedAction | 授权三件套:身份、规则、动作 |
| `execution/loop.py` | ToolExecutionContext | 工具上下文:身份与作用域应从这里取 |
| `ledger/protocol.py` | ToolRecoveryProfile | 恢复语义:重试 / 查询 / 人工 |
| `verification/runner.py` | CommandProfile / CommandResult | 测试执行契约:argv 与结果事实 |
| `verification/release_contracts.py` | ProfileReleaseContract | 披露契约:四档 + 字段白名单 |
| `verification/dependency_preparation.py` | DependencyScope / PreparationDecision | 依赖准备:三态决策 |
| `agents/control.py` | AgentRecord / AgentBudgetLimits | 委派契约:父子、身份、scope、预算 |
| `tools/shell.py` | ShellLimits | 受限 shell:白名单与上限 |

## `agents/`

> D11 durable multi-agent control plane.

### `agents/__init__.py`

> D11 durable multi-agent control plane.


### `agents/control.py`

> D11 durable multi-agent control plane (spawn/mailbox/result/budget/fence).

- **class `AgentBudgetLimits`**
  - 字段:
    - `max_depth: int = 4`
    - `max_total_agents: int = 16`
    - `max_concurrent_children: int = 4`

- **class `Principal`**
  - Operator identity used by requeue/cancel decisions.
  - 字段:
    - `principal_id: str`
    - `scopes: tuple[str, ...] = ()`

- **`terminal_run_result_ref(agent_id: UUID, run_id: UUID) -> str`** — Stable result ref for one agent run (section 5.6).
- **`terminal_result_identity(agent_id: UUID, run_id: UUID, state: AgentState, reason: str | None, messages: Sequence[MessageRecord]) -> tuple[str, str]`** — Rebuild the exact terminal aggregate from mailbox projections.
- **class `ResourceReconcileReceipt`**
  - 字段:
    - `root_agent_id: UUID`
    - `command_id: UUID`
    - `source_global_position: int`
    - `source_digest: str`
    - `released_reservation_ids: tuple[UUID, ...]`
    - `budget_stream_version: int`
    - `changed: bool`

- **class `AgentControlPlane`**
  - Durable spawn/message/result/terminal commands with exact-version fences.
  - 方法:
    - `now(self) -> datetime`
    - `root_for(self, agent_id: UUID) -> UUID`
    - `spawn_agent(self, *, parent_agent_id: UUID | None, task_id: str, principal_id: str, scopes: tuple[str, ...], context_mode: ContextMode=ContextMode.FRESH, turn_id: UUID | None=None, thread_id: UUID | None=None, parent_run_id: UUID | None=None, semantic_idempotency_key: str | None=None) -> AgentRecord`
    - `_spawn_root(self, command_id: UUID, child_id: UUID, *, task_id: str, principal_id: str, scopes: tuple[str, ...], context_mode: ContextMode, turn_id: UUID | None, parent_run_id: UUID | None) -> AgentRecord` — Root spawn: a single spawned.v2 write; no pseudo reserve/release.
    - `_spawn_child(self, command_id: UUID, child_id: UUID, reservation_id: UUID, *, parent_agent_id: UUID, parent_run_id: UUID | None, task_id: str, principal_id: str, scopes: tuple[str, ...], context_mode: ContextMode, turn_id: UUID | None) -> AgentRecord` — Atomic four-stream spawn (section 5.5, P0-03).
    - `start_attempt(self, agent_id: UUID, *, expected_version: int, lease_seconds: int=30) -> AgentRecord`
    - `_start_fresh(self, record: AgentRecord, *, lease_seconds: int) -> AgentRecord`
    - `_takeover(self, orphan: AgentRecord, *, lease_seconds: int) -> AgentRecord` — Atomic ORPHANED takeover: taken-over.v2 + one unresolved per message.
    - `_resume(self, waiting: AgentRecord, *, lease_seconds: int) -> AgentRecord` — Resume a WAITING agent once every blocker is resolved (all QUEUED
    - `heartbeat(self, agent_id: UUID, *, run_id: UUID, attempt: int, lease_seconds: int=30, max_cas_retries: int=3, beat_number: int=0) -> AgentRecord`
    - `send_message(self, to_agent_id: UUID, *, from_agent_id: UUID | None, kind: MessageKind, body_ref: str | None, idempotency_key: str) -> MessageRecord`
    - `deliver_message(self, agent_id: UUID, message_id: UUID, *, run_id: UUID, lease_seconds: int=30) -> MessageRecord`
    - `record_message_result(self, agent_id: UUID, message_id: UUID, *, run_id: UUID, expected_delivery_attempt: int, outcome: str | None=None, error_code: str | None=None) -> MessageRecord`
    - `ack_message(self, agent_id: UUID, message_id: UUID, *, run_id: UUID) -> MessageRecord`
    - `mark_message_unresolved(self, agent_id: UUID, message_id: UUID, *, abandoned_run_id: UUID, takeover_run_id: UUID, expected_delivery_attempt: int, reason: str) -> MessageRecord` — Internal primitive; the real takeover uses the atomic batch in the
    - `requeue_message(self, agent_id: UUID, message_id: UUID, *, expected_delivery_attempt: int, decision_id: UUID, actor: Principal, approval_id: UUID | None, resolution_kind: Literal['proven_not_started', 'idempotent_read', 'operator_retry'], reason: str) -> MessageRecord`
    - `cancel_message(self, agent_id: UUID, message_id: UUID, *, expected_delivery_attempt: int, decision_id: UUID, actor: Principal, approval_id: UUID | None, reason: str) -> MessageRecord`
    - `enter_waiting_for_resolution(self, agent_id: UUID, *, run_id: UUID, attempt: int) -> AgentRecord` — RUNNING -> WAITING with every UNRESOLVED message as a blocker.
    - `interrupt_agent(self, agent_id: UUID, *, run_id: UUID) -> MessageRecord`
    - `terminal(self, agent_id: UUID, *, run_id: UUID, expected_attempt: int, state: Literal[AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELLED], reason: str | None, result_ref: str, result_digest: str, source_message_ids: tuple[UUID, ...]) -> AgentRecord` — Atomic terminal settlement (section 5.6, P0-05).
    - `discover_orphans(self) -> list[AgentRecord]`
    - `import_capacity_baseline(self, parent_agent_id: UUID) -> ParentCapacity` — Standalone first-commit baseline for a legacy v1 parent (5.3).
    - `reconcile_legacy_resources(self, root_agent_id: UUID, *, expected_budget_version: int | None=None) -> ResourceReconcileReceipt` — Release only reservations provably terminal from Agent events.
    - `_baseline_source(self, parent_agent_id: UUID, high_water: int) -> tuple[dict, str]` — Build the exact baseline source document up to the high-water.
    - `_root_from_scan(self, agent_id: UUID, by_stream: Mapping[str, list]) -> UUID | None` — Resolve the root from spawn events; missing/cycle returns None.
    - `_reconcile_source(self, root_agent_id: UUID, high_water: int) -> tuple[dict, str]` — Build the exact reconcile source document up to the high-water.
    - `wait_agents(self, parent_agent_id: UUID, *, timeout_seconds: float=5.0) -> list[dict]`
    - `list_agents(self, parent_agent_id: UUID) -> list[dict]`
    - `_require_operator_authority(self, record: AgentRecord, actor: Principal, *, approval_id: UUID | None, resolution_kind: str, error_code: str) -> None`
    - `_receipt_conflict(self, command_id: UUID, fingerprint: str) -> bool` — True when the same command id was committed under a different
    - `_read_receipt(self, command_id: UUID, fingerprint: str)` — Return the persisted receipt, or the string sentinel "committed"
    - `_rebuilt_agent(self, agent_id: UUID) -> AgentRecord`
    - `_message_in(self, snapshot: MailboxSnapshot, message_id: UUID) -> MessageRecord | None`
    - `_fault(self, point: str, facts: Mapping[str, object]) -> None`
    - `_event(self, command_id: UUID, slot: str, event_type: str, payload: Mapping, *, occurred_at: datetime, correlation_id: UUID, turn_id: UUID | None=None, run_id: UUID | None=None) -> NewEvent`
    - `_depth(self, agent_id: UUID) -> int`
    - `_active_children(self, parent_agent_id: UUID) -> list[AgentRecord]`
    - `_budget(self, root_agent_id: UUID) -> int` — Compatibility count of active non-root reservations under a root.
    - `_validate_session_root(self, thread_id: UUID | None, principal_id: str) -> None` — Issue root authority from the session record (issue #18 F2).
    - `_validate_spawn_parent(self, parent: AgentRecord, parent_run_id: UUID | None, scopes: tuple[str, ...], principal_id: str) -> None`
    - `_narrow_delegated_identity(self, parent: AgentRecord, principal_id: str, scopes: tuple[str, ...]) -> None`
    - `_ancestor_principals(self, parent: AgentRecord) -> set[str]` — Principal names held by this agent and every ancestor above it.
    - `_spawned_event(self, command_id: UUID, child_id: UUID, *, parent: UUID | None, root: UUID, depth: int, task_id: str, principal_id: str, scopes: tuple[str, ...], context_mode: ContextMode, turn_id: UUID | None, parent_run_id: UUID | None, observed_at: datetime, capacity_reservation_id: UUID | None, budget_reservation_id: UUID | None) -> NewEvent`
    - `_parent_capacity(self, parent_agent_id: UUID) -> ParentCapacity`
    - `_root_budget(self, root_agent_id: UUID) -> RootAgentBudget`
    - `_legacy_parent_for(self, child_agent_id: UUID) -> UUID | None`
    - `_current_global_position(self) -> int`
    - `_read_all_through(self, through_position: int) -> tuple`
    - `_read_all(self, stream: StreamId) -> tuple`
    - `_scan_agent_spawns(self) -> list`


### `agents/graph.py`

> D11 durable parent/child Agent graph model (event-sourced).

- **class `AgentError`** (RuntimeError)
  - Stable, content-free D11 control-plane failure.

- **class `AgentState`** (StrEnum)

- **class `ContextMode`** (StrEnum)

- **class `AgentRecord`**
  - 字段:
    - `agent_id: UUID`
    - `parent_agent_id: UUID | None`
    - `task_id: str`
    - `attempt: int`
    - `run_id: UUID | None`
    - `principal_id: str`
    - `scopes: tuple[str, ...]`
    - `context_mode: ContextMode`
    - `state: AgentState`
    - `version: int`
    - `created_at: datetime`
    - `lease_expires_at: datetime | None = None`
    - `outcome: str | None = None`
    - `reason: str | None = None`
    - `abandoned_run_id: UUID | None = None`
    - `waiting_run_id: UUID | None = None`
    - `blocking_message_ids: tuple[UUID, ...] = ()`
    - `root_agent_id: UUID | None = None`
    - `depth: int | None = None`
    - `capacity_reservation_id: UUID | None = None`
    - `budget_reservation_id: UUID | None = None`
    - `result_ref: str | None = None`
    - `result_digest: str | None = None`
    - `legacy_spawn: bool = False`
  - 方法:
    - `to_document(self) -> dict[str, Any]`

- **`rebuild_agent(agent_id: UUID, events: tuple, *, initial: AgentRecord | None=None) -> AgentRecord | None`** — Replay one agent stream into its current durable state.
- **class `AgentGraph`**
  - Read-only graph facade over per-agent event streams.
  - 方法:
    - `load(self, agent_id: UUID) -> AgentRecord | None`
    - `children(self, parent_agent_id: UUID) -> list[AgentRecord]` — Return children whose spawn event names this parent.
    - `has_cycle(self, parent_agent_id: UUID, child_agent_id: UUID) -> bool` — True if child is an ancestor of parent (spawn would create a cycle).
    - `_read_all(self, stream: StreamId, *, after_version: int=-1) -> tuple`


### `agents/messages.py`

> D11 durable mailbox: per-agent sequence, dedupe, delivery state.

- **class `MessageKind`** (StrEnum)

- **class `MessageStatus`** (StrEnum)

- **class `MessageRecord`**
  - Projection of one message inside a mailbox stream.
  - 字段:
    - `agent_id: UUID`
    - `message_id: UUID`
    - `sequence: int`
    - `from_agent_id: UUID | None`
    - `kind: MessageKind`
    - `body_ref: str | None`
    - `idempotency_key: str`
    - `status: MessageStatus`
    - `version: int`
    - `delivered_run_id: UUID | None = None`
    - `delivery_attempt: int = 0`
    - `delivered_agent_attempt: int | None = None`
    - `delivery_lease_expires_at: datetime | None = None`
    - `result_ref: str | None = None`
    - `result_digest: str | None = None`
    - `result_summary: str | None = None`
    - `result_is_error: bool = False`
    - `result_error_code: str | None = None`
    - `unresolved_reason: str | None = None`
    - `cancel_requested: bool = False`
    - `legacy_delivery: bool = False`
  - 方法:
    - `to_document(self) -> dict[str, Any]`

- **`rebuild_mailbox(agent_id: UUID, events: tuple) -> list[MessageRecord]`** — Replay one mailbox stream into ordered message records.
- **class `MailboxSnapshot`**
  - One consistent read of a mailbox stream with its real stream head.
  - 字段:
    - `agent_id: UUID`
    - `stream_version: int`
    - `messages: tuple[MessageRecord, ...]`
  - 方法:
    - `unfinished(self) -> tuple[MessageRecord, ...]`

- **class `AgentMailbox`**
  - 方法:
    - `snapshot(self, agent_id: UUID) -> MailboxSnapshot`
    - `load(self, agent_id: UUID) -> list[MessageRecord]`
    - `queued(self, agent_id: UUID) -> list[MessageRecord]`
    - `result_recorded(self, agent_id: UUID) -> list[MessageRecord]`
    - `_read_all(self, stream: StreamId) -> tuple`

- **`canonicalize_result(outcome: str | None, error_code: str | None) -> tuple[str, bool, str | None]`** — Return (canonical_outcome, is_error, error_code).
- **`result_ref_for(agent_id: UUID, message_id: UUID, delivery_attempt: int) -> str`**
- **`result_digest_for(agent_id: UUID, message_id: UUID, delivery_attempt: int, canonical_outcome: str, is_error: bool, error_code: str | None) -> str`**
- **`summarize_outcome(canonical_outcome: str) -> str`** — Truncate to a bounded, code-point-safe summary (marker included).

- 常量(字符串):
  - `TRUNCATION_MARKER` = `
[truncated]`

### `agents/resources.py`

> I3 durable resource projections: parent capacity and root budget streams.

- **`capacity_stream(parent_agent_id: UUID) -> StreamId`** — Stream that tracks how many of a parent's child slots are active.
- **`budget_stream(root_agent_id: UUID) -> StreamId`** — Stream that tracks total active non-root agents under one root.
- **class `ResourceReservation`**
  - One active child slot: identical identity on capacity and budget.
  - 字段:
    - `reservation_id: UUID`
    - `child_agent_id: UUID`
    - `parent_agent_id: UUID`
    - `root_agent_id: UUID`
  - 方法:
    - `to_document(self) -> dict[str, str]`

- **class `ParentCapacity`**
  - Projection of one agent-capacity-{parent} stream.
  - 字段:
    - `parent_agent_id: UUID`
    - `version: int`
    - `active_reservations: tuple[ResourceReservation, ...]`
  - 方法:
    - `find(self, reservation_id: UUID) -> ResourceReservation | None`
    - `active_count(self) -> int`
    - `exists(self) -> bool` — True when the capacity stream has at least one committed event.

- **class `RootAgentBudget`**
  - Projection of one agent-budget-{root} stream.
  - 字段:
    - `root_agent_id: UUID`
    - `version: int`
    - `active_reservations: tuple[ResourceReservation, ...]`
  - 方法:
    - `find(self, reservation_id: UUID) -> ResourceReservation | None`
    - `active_count(self) -> int`

- **`rebuild_capacity(parent_agent_id: UUID, events: Sequence) -> ParentCapacity`** — Replay an agent-capacity stream into its active reservation set.
- **`rebuild_budget(root_agent_id: UUID, events: Sequence, *, resolve_parent: Callable[[UUID], UUID | None]) -> RootAgentBudget`** — Replay an agent-budget stream into its active reservation set.
- **`canonical_json(value: Any) -> str`** — Deterministic compact JSON used for digests and fingerprints.
- **`sha256_digest(canonical_text: str) -> str`**

### `agents/scheduler.py`

> D11 attempt runner: durable result, no blind retry, minimal LeaseKeeper.

- **class `AgentRunResult`**
  - 字段:
    - `state: AgentState`
    - `summary: str`
    - `result_refs: tuple[str, ...] = ()`

- **class `ScriptedAgentProvider`**
  - Deterministic provider for D11 tests and examples (no real model).
  - 方法:
    - `run(self, task: str, *, tool_allowlist: frozenset[str]) -> str`

- **class `WaitStrategy`** (Protocol)
  - Blocking wait used by the keeper heartbeat loop.
  - 方法:
    - `wait(self, timeout: float) -> bool`
    - `request_stop(self) -> None`

- **class `DefaultWaitStrategy`**
  - Bounded wait on a stop event; returns as soon as stop is requested.
  - 方法:
    - `request_stop(self) -> None`
    - `wait(self, timeout: float) -> bool`

- **class `AgentLeaseKeeper`**
  - Minimal DB-clock LeaseKeeper for one agent run (I2).
  - 方法:
    - `start(self) -> None`
    - `stop(self, *, assert_owned: bool=True) -> None`
    - `assert_owned(self) -> None`
    - `_run(self) -> None`

- **class `AgentScheduler`**
  - Run one durable attempt: fence, deliver, execute, record, terminal.
  - 方法:
    - `run_attempt(self, agent_id: UUID) -> AgentRunResult`
    - `_process_task(self, agent_id: UUID, run_id: UUID, message: MessageRecord, keeper: AgentLeaseKeeper) -> AgentRunResult | None`
    - `_process_cancel(self, agent_id: UUID, run_id: UUID, message: MessageRecord, keeper: AgentLeaseKeeper) -> bool`
    - `_terminate(self, agent_id: UUID, run_id: UUID, snapshot, *, cancelled: bool) -> AgentRunResult`
    - `_terminal(self, agent_id: UUID, run_id: UUID, state: AgentState, reason: str | None, messages) -> AgentRunResult` — Compute the exact aggregate identity and call the atomic terminal.
    - `_first_error_code(messages) -> str | None`
    - `_completion_outcome(messages) -> str | None`
    - `_facts(agent_id: UUID, message: MessageRecord, delivered: MessageRecord) -> dict[str, object]`
    - `_fault(self, point: str, facts: dict[str, object]) -> None`
    - `_stop_keeper_safely(keeper: AgentLeaseKeeper) -> None`


## `context/`

> D13 repository context, budget, retrieval, and compaction.

### `context/__init__.py`

> D13 repository context, budget, retrieval, and compaction.


### `context/budget.py`

> D13 context budget: deterministic truncation and accounting.

- **class `ContextBudget`**
  - 字段:
    - `max_chars: int = 120000`
    - `max_items: int = 200`
    - `max_snippet_chars: int = 8000`

- **`fit_within_budget(items: list[tuple[str, int]], *, budget: ContextBudget) -> list[tuple[str, int]]`** — Deterministically select items until the char budget is exhausted.

### `context/compaction.py`

> D13 compaction: untrusted summary + authoritative projections + pairs.

- **class `ToolCallPair`**
  - 字段:
    - `call_ref: str`
    - `tool_name: str`
    - `has_result: bool`

- **class `AuthoritativeProjection`**
  - Typed safety/execution facts that summary text must never replace.
  - 字段:
    - `user_goal: str`
    - `constraints: tuple[str, ...]`
    - `changed_files: tuple[str, ...]`
    - `test_evidence: str`
    - `pending_approval: str | None`
    - `unknown_outcome: str | None`
    - `active_children: tuple[str, ...]`
    - `budget: str`
  - 方法:
    - `render(self) -> str`

- **class `Compactor`**
  - Build a compacted prompt without dropping obligations or open calls.
  - 方法:
    - `compact(self, *, summary: str, projection: AuthoritativeProjection, pairs: tuple[ToolCallPair, ...]) -> str`

- **`rebuild_after_restart(*, summary: str, projection: AuthoritativeProjection, tail_events: tuple[str, ...]) -> str`** — Deterministic restart reconstruction: projection never derives from text.

### `context/index.py`

> D13 repository index: ignore rules, binary detection, limits, staleness.

- **class `IndexLimits`**
  - 字段:
    - `max_files: int = 10000`
    - `max_file_bytes: int = 1000000`
    - `max_total_bytes: int = 64000000`

- **class `IndexedFile`**
  - 字段:
    - `path: str`
    - `size: int`
    - `sha256: str`

- **class `RepositoryIndex`**
  - Index a Git repo honoring .gitignore via `git ls-files`.
  - 方法:
    - `list_files(self) -> list[IndexedFile]`
    - `is_stale(self, file: IndexedFile) -> bool`
    - `hash_file(self, path: Path) -> str`
    - `_allowed(self, name: str) -> bool`
    - `_git(self, *arguments: str) -> str`


### `context/retrieval.py`

> D13 retrieval: literal scoring, dedupe, untrusted snippets.

- **class `ContextItem`**
  - 字段:
    - `path: str`
    - `sha256: str`
    - `line_range: tuple[int, int]`
    - `source: str`
    - `score: int`
    - `snippet: str`
  - 方法:
    - `to_document(self) -> dict`

- **class `ContextRetriever`**
  - Score and budget repository context; snippets are untrusted text.
  - 方法:
    - `retrieve(self, *, query: str, files: list[IndexedFile], max_snippet_lines: int=40) -> list[ContextItem]`
    - `_score_snippet(self, file: IndexedFile, query: str, max_lines: int) -> tuple[str, int, int]`


## `control/`

> D1 durable Thread/Turn control plane and event storage.

### `control/__init__.py`

> D1 durable Thread/Turn control plane and event storage.


### `control/durable_json.py`

> I4 durable JSON hard boundaries and canonical user text.

- **class `DurableJsonError`** (RuntimeError)
  - Content-free durable JSON failure; the message never echoes user text.

- **class `DurableJsonLimitExceeded`** (DurableJsonError)
  - A durable JSON hard limit was exceeded (limit+1 must fail like this).

- **class `CanonicalTextError`** (RuntimeError)
  - Content-free canonical text failure; carries only a stable code/path.

- **class `DurableJsonLimits`**
  - One durable JSON policy: byte/depth/node/string/member/item/key caps.
  - 字段:
    - `max_utf8_bytes: int`
    - `max_depth: int`
    - `max_nodes: int`
    - `max_string_utf8_bytes: int`
    - `max_object_members: int`
    - `max_array_items: int`
    - `max_key_utf8_bytes: int`

- **`validate_json_document(value: Any, limits: DurableJsonLimits, *, path: str='payload', require_object: bool=True, reject_string_controls: bool=True) -> None`** — Validate one already-parsed JSON-compatible value against the limits.
- **`validate_json_value(value: Any, limits: DurableJsonLimits, *, path: str='payload', reject_string_controls: bool=True) -> None`** — Validate any JSON value (object/array/scalar) against the limits.
- **`effective_payload_limits(ingress: Mapping[str, int] | None) -> DurableJsonLimits`** — Write-time payload policy: min(EVENT_PAYLOAD_READ_V1, runtime ingress).
- **`strict_json_loads_bytes(data: bytes | bytearray | memoryview, limits: DurableJsonLimits, *, path: str='payload', reject_string_controls: bool=True) -> Any`** — Strictly decode UTF-8 bytes and parse strict JSON, then validate.
- **`strict_json_loads_text(text: str, limits: DurableJsonLimits, *, path: str='payload', reject_string_controls: bool=True) -> Any`** — Strict parse of an already decoded (strict UTF-8) JSON text string.
- **class `CanonicalText`**
  - The single canonical value used for model/event/history/resume.
  - 字段:
    - `value: str`
    - `utf8_bytes: int`
    - `digest: str`
    - `redaction_policy_version: int`
    - `redaction_count: int`

- **class `CanonicalTextPolicy`**
  - Byte ceilings per canonical text kind (defaults = section 6.2 table).
  - 字段:
    - `user_input_max_utf8_bytes: int = USER_INPUT_MAX_UTF8_BYTES`
    - `resume_interrupt_max_utf8_bytes: int = RESUME_INTERRUPT_MAX_UTF8_BYTES`
    - `terminal_text_max_utf8_bytes: int = TERMINAL_TEXT_MAX_UTF8_BYTES`
    - `instruction_max_utf8_bytes: int = INSTRUCTION_MAX_UTF8_BYTES`
  - 方法:
    - `from_ingress(cls, ingress: Mapping[str, int] | None) -> 'CanonicalTextPolicy'` — Build the text policy from the exact-key runtime ingress policy.

- **`canonicalize_text(value: Any, max_utf8_bytes: int, *, name: str='text') -> CanonicalText`** — Apply the fixed canonicalization order (section 6.3) to untrusted text.
- **`validate_runtime_ingress(value: Any) -> dict[str, int]`** — Validate an exact-key durable-limits ingress policy.
- **`canonical_json_bytes_v1(value: Any, limits: DurableJsonLimits | None=None, *, path: str='payload') -> bytes`**

- 常量(字符串):
  - `REDACTED` = `[REDACTED]`

### `control/event_store.py`

> Append-only Event Store 的类型契约。

- **class `EventStoreError`** (RuntimeError)
  - 事件存储可预期失败的基类，便于 Runtime 统一捕获存储层错误。

- **class `InvalidEvent`** (EventStoreError)
  - 事件不满足可持久化 JSON 契约时抛出，而不是等待数据库序列化失败。

- **class `WrongExpectedVersion`** (EventStoreError)
  - 命令所依据的聚合版本已过期时抛出，即事件流上的乐观锁冲突。

- **class `IdempotencyConflict`** (EventStoreError)
  - 同一个幂等键被用于不同请求内容时抛出，通常表示调用方错误。

- **class `DuplicateEventId`** (EventStoreError)
  - event_id 已存在于全局事件日志时抛出。

- **class `StreamId`**
  - 一条聚合事件流的稳定身份，例如 ``turn-<uuid>``。
  - 字段:
    - `category: str`
    - `aggregate_id: UUID`
  - 方法:
    - `key(self) -> str` — 返回数据库使用的规范化 stream key。

- **class `EventMetadata`**
  - 事件的审计/链路元数据；业务状态本身应放在 payload 中。
  - 字段:
    - `command_id: UUID`
    - `correlation_id: UUID`
    - `thread_id: UUID | None = None`
    - `turn_id: UUID | None = None`
    - `run_id: UUID | None = None`
    - `actor: str = 'runtime'`
    - `causation_id: UUID | None = None`

- **class `NewEvent`**
  - 尚未提交的领域事实，由 Runtime 创建、交给 EventStore 追加。
  - 字段:
    - `event_id: UUID`
    - `event_type: str`
    - `schema_version: int`
    - `occurred_at: datetime`
    - `payload: Mapping[str, Any]`
    - `metadata: EventMetadata`

- **class `StoredEvent`**
  - EventStore 已确认提交、可用于聚合重放的历史事实。
  - 字段:
    - `event_id: UUID`
    - `stream_id: StreamId`
    - `stream_version: int`
    - `global_position: int`
    - `commit_id: UUID`
    - `commit_index: int`
    - `commit_size: int`
    - `event_type: str`
    - `schema_version: int`
    - `occurred_at: datetime`
    - `recorded_at: datetime`
    - `payload: Mapping[str, Any]`
    - `metadata: EventMetadata`

- **class `StreamWrite`**
  - 对一条流的条件追加请求：版本精确匹配时才写入全部事件。
  - 字段:
    - `stream_id: StreamId`
    - `expected_version: int`
    - `events: tuple[NewEvent, ...]`

- **class `StreamPrecondition`**
  - 只读流栅栏；与 writes 在同一事务中验证但不推进该流。
  - 字段:
    - `stream_id: StreamId`
    - `expected_version: int`
    - `required_event_type: str | None = None`
    - `required_payload: Mapping[str, Any] = MappingProxyType({})`

- **class `StreamAppendReceipt`**
  - 某一条流在原子批次中的实际写入范围。
  - 字段:
    - `stream_id: StreamId`
    - `first_version: int`
    - `last_version: int`
    - `global_positions: tuple[int, ...]`

- **class `AppendReceipt`**
  - 整个 append_batch 的持久化回执，也是幂等重试需要原样返回的结果。
  - 字段:
    - `idempotency_key: UUID`
    - `streams: tuple[StreamAppendReceipt, ...]`

- **class `EventStore`** (Protocol)
  - Runtime 控制面的持久化端口；SQLite/Postgres 实现都应遵守此契约。
  - 方法:
    - `database_time(self) -> datetime` — Return the backend-authoritative UTC clock.
    - `append_batch(self, writes: Sequence[StreamWrite], *, idempotency_key: UUID, request_fingerprint: str | None=None, preconditions: Sequence[StreamPrecondition]=()) -> AppendReceipt` — 以精确版本原子追加一批流，并按幂等键保存/返回固定回执。
    - `read_idempotency(self, idempotency_key: UUID, *, request_fingerprint: str) -> AppendReceipt | None` — 查询命令是否已经成功提交，并验证本次语义指纹与原请求一致。
    - `read_stream(self, stream_id: StreamId, *, after_version: int=-1, limit: int=500) -> tuple[StoredEvent, ...]` — 按 stream_version 读取单个聚合历史，用于 Thread/Turn 状态重建。
    - `current_global_position(self) -> int` — Return the highest committed global position (0 for an empty log).
    - `read_all(self, *, after_position: int=0, through_position: int | None=None, limit: int=500) -> tuple[StoredEvent, ...]` — 按 global_position 读取全局事件日志，用于投影、审计和订阅。


### `control/models.py`

> 由持久化事件重建的不可变 Thread/Turn 领域状态。

- **class `DomainError`** (RuntimeError)
  - 可确定复现的领域失败基类，与数据库/网络故障区分。

- **class `AggregateNotFound`** (DomainError)
  - 事件序列为空、无法重建指定 Thread/Turn 时抛出。

- **class `InvalidTransition`** (DomainError)
  - 应用命令试图执行非法状态迁移时抛出（主要由 Runtime 使用）。

- **class `CorruptEventStream`** (DomainError)
  - 持久化历史存在版本断档、非法 payload 或不可能迁移时抛出。

- **class `ThreadStatus`** (StrEnum)
  - 会话生命周期；归档是不可逆终态。

- **class `TurnStatus`** (StrEnum)
  - 一次用户任务的控制面状态。

- **class `RunStatus`** (StrEnum)
  - Lifecycle of one fenced execution attempt.

- **class `InterruptKind`** (StrEnum)
  - 待处理中断期望的响应类型。

- **class `PendingInterrupt`**
  - 已持久化、即使进程退出也能继续等待的交互点。
  - 字段:
    - `interrupt_id: UUID`
    - `kind: InterruptKind`
    - `prompt: str`
    - `approval_request_id: UUID | None = None`

- **class `ThreadState`**
  - 一个长期会话在指定事件流版本上的不可变快照。
  - 字段:
    - `thread_id: UUID`
    - `workspace_ref: str`
    - `status: ThreadStatus`
    - `active_turn_id: UUID | None`
    - `version: int`
    - `created_at: datetime`
    - `updated_at: datetime`
    - `owner_id: str | None = None`

- **class `TurnState`**
  - 一次用户任务在指定事件流版本上的不可变快照。
  - 字段:
    - `turn_id: UUID`
    - `thread_id: UUID`
    - `user_input: str`
    - `status: TurnStatus`
    - `attempt: int`
    - `current_run_id: UUID | None`
    - `pending_interrupt: PendingInterrupt | None`
    - `last_resume_response: str | bool | None`
    - `last_resume_interrupt_id: UUID | None`
    - `last_resume_version: int | None`
    - `outcome: str | None`
    - `error: str | None`
    - `version: int`
    - `created_at: datetime`
    - `updated_at: datetime`
    - `last_resume_approval_request_id: UUID | None = None`
    - `last_resume_approval_decision: str | None = None`
    - `recovery_claim_token: UUID | None = None`
  - 方法:
    - `is_terminal(self) -> bool` — Turn 是否已经进入不可继续迁移的终态。

- **class `CompletionEvidenceRef`**
  - Exact reference to the typed evidence authorizing terminalization.
  - 字段:
    - `stream_id: StreamId`
    - `stream_version: int`
    - `event_id: UUID`
    - `evidence_digest: str`

- **class `RunState`**
  - Explicit durable truth for one execution attempt.
  - 字段:
    - `run_id: UUID`
    - `thread_id: UUID`
    - `turn_id: UUID`
    - `attempt: int`
    - `status: RunStatus`
    - `version: int`
    - `created_at: datetime`
    - `updated_at: datetime`
    - `detail: str | None = None`
    - `completion_evidence: CompletionEvidenceRef | None = None`
  - 方法:
    - `is_terminal(self) -> bool`

- **class `LegacyRunState`**
  - Read-only Run view synthesized from a pre-I7 Turn stream.
  - 字段:
    - `run_id: UUID`
    - `thread_id: UUID`
    - `turn_id: UUID`
    - `attempt: int`
    - `status: RunStatus`
    - `created_at: datetime`
    - `updated_at: datetime`
    - `detail: str | None = None`
    - `version: int = -1`
  - 方法:
    - `is_terminal(self) -> bool`

- **class `StoredEventLike`** (Protocol)
  - 聚合重放真正需要的最小事件视图，与具体 EventStore 实现解耦。
  - 字段:
    - `event_type: str`
    - `schema_version: int`
    - `stream_version: int`
    - `occurred_at: datetime`
    - `payload: Mapping[str, Any]`

- **`rebuild_thread(thread_id: UUID, events: Iterable[StoredEventLike]) -> ThreadState`** — 从头重放 Thread 事件，并拒绝版本断档或非法历史迁移。
- **`rebuild_turn(turn_id: UUID, events: Iterable[StoredEventLike]) -> TurnState`** — 从头重放 Turn 事件，得到当前运行/等待/终止状态。
- **`rebuild_run(run_id: UUID, events: Iterable[StoredEventLike]) -> RunState`** — Rebuild one explicit Run attempt and reject impossible histories.
- **`reduce_execution_seed(payload: Mapping[str, Any], *, turn: TurnState, turn_stream_version: int) -> dict[str, Any]`** — Validate a v2 execution seed against the authoritative Turn state.

- 常量(字符串):
  - `THREAD_CREATED` = `thread.created.v1`
  - `THREAD_TURN_ATTACHED` = `thread.turn-attached.v1`
  - `THREAD_TURN_DETACHED` = `thread.turn-detached.v1`
  - `THREAD_ARCHIVED` = `thread.archived.v1`
  - `TURN_CREATED` = `turn.created.v1`
  - `TURN_STARTED` = `turn.started.v1`
  - `TURN_WAITING_FOR_INPUT` = `turn.waiting-for-input.v1`
  - `TURN_WAITING_FOR_APPROVAL` = `turn.waiting-for-approval.v1`
  - `TURN_PAUSED` = `turn.paused.v1`
  - `TURN_RECOVERY_QUEUED` = `turn.recovery-queued.v1`
  - `TURN_RUNTIME_OUTCOME_RESOLVED` = `turn.runtime-outcome-resolved.v1`
  - `TURN_STALE_RUN_REQUEUED` = `turn.stale-run-requeued.v1`
  - `TURN_RECOVERY_LEASE_CLAIMED` = `turn.recovery-lease-claimed.v1`
  - `TURN_RECOVERY_LEASE_HEARTBEATED` = `turn.recovery-lease-heartbeated.v1`
  - `TURN_RECOVERY_LEASE_RELEASED` = `turn.recovery-lease-released.v1`
  - `TURN_COMPLETED` = `turn.completed.v1`
  - `TURN_FAILED` = `turn.failed.v1`
  - `TURN_CANCELLED` = `turn.cancelled.v1`
  - `TURN_TIMED_OUT` = `turn.timed-out.v1`
  - `RUN_STARTED` = `run.started.v1`
  - `RUN_INTERRUPTED` = `run.interrupted.v1`
  - `RUN_ABANDONED` = `run.abandoned.v1`
  - `RUN_COMPLETED` = `run.completed.v1`
  - `RUN_FAILED` = `run.failed.v1`
  - `RUN_CANCELLED` = `run.cancelled.v1`
  - `RUN_TIMED_OUT` = `run.timed-out.v1`
  - `RUN_OUTCOME_UNKNOWN` = `run.outcome-unknown.v1`

### `control/read_snapshot.py`

> Bounded, source-byte-preserving SQLite snapshot acquisition.

- **class `ReadSnapshotError`** (RuntimeError)

- **class `ReadSnapshot`**
  - 字段:
    - `image: bytearray = field(repr=False)`
    - `source_digest: str`
  - 方法:
    - `connect(self)`

- **`read_snapshot(path: Path, *, timeout_ms: int=10000) -> ReadSnapshot`**

### `control/run_effects.py`

> I7 per-Run effect index and terminal exact-head verification.

- **class `RunEffectRef`**
  - 字段:
    - `run_id: UUID`
    - `effect_kind: str`
    - `stream_id: StreamId`
    - `identity_digest: str`
    - `first_version: int`
    - `index_version: int`

- **class `RunEffectInspection`**
  - 字段:
    - `refs: tuple[RunEffectRef, ...]`
    - `preconditions: tuple[StreamPrecondition, ...]`
    - `open_effects: tuple[RunEffectRef, ...]`
    - `index_version: int`
  - 方法:
    - `terminal_safe(self) -> bool`

- **class `RunEffectIndex`**
  - Append-only references to effects causally created by one Run.
  - 方法:
    - `link_write(self, *, run_id: UUID, effect_kind: str, effect_stream: StreamId, identity_digest: str, first_version: int, command_id: UUID, actor: str) -> StreamWrite`
    - `inspect(self, run_id: UUID) -> RunEffectInspection`
    - `stream(run_id: UUID) -> StreamId`
    - `_read_all(self, stream: StreamId) -> tuple`

- **`effect_identity_digest(document: Mapping[str, object]) -> str`**

### `control/runtime.py`

> Thread/Turn 的同步应用服务：校验命令，并把状态变化持久化为事件。

- **class `ThreadRuntime`**
  - 持久化控制面的应用服务，负责命令校验、状态栅栏与原子事件提交。
  - 方法:
    - `create_thread(self, workspace_ref: str, *, thread_id: UUID | str | None=None, command_id: UUID | str | None=None) -> ThreadState` — 创建 Thread；相同 command_id 的重试返回首次提交的同一结果。
    - `create_turn(self, thread_id: UUID | str, user_input: str, *, expected_thread_version: int, turn_id: UUID | str | None=None, command_id: UUID | str | None=None) -> TurnState` — 在 Thread 中创建唯一活跃 Turn，并原子更新 Thread/Turn 两条流。
    - `start_turn(self, turn_id: UUID | str, expected_version: int, *, command_id: UUID | str | None=None, execution_seed: ExecutionSeedDTO | None=None, execution_expected_version: int | None=None, lease_owner_id: str | None=None, lease_seconds: int | None=None) -> TurnState` — 把 QUEUED Turn 启动为一次新的 Run，并增加 attempt。
    - `claim_recovery_run(self, turn_id: UUID | str, *, expected_version: int, owner_id: str, lease_seconds: int, command_id: UUID | str | None=None) -> TurnState` — Claim a recovery lease overlay on the active run (typed event).
    - `heartbeat_recovery_run(self, turn_id: UUID | str, *, expected_version: int, run_id: UUID | str, claim_token: UUID | str | None, lease_seconds: int, command_id: UUID | str | None=None, owner_id: str | None=None) -> TurnState` — Renew the recovery lease overlay through a typed event.
    - `release_recovery_run(self, turn_id: UUID | str, *, expected_version: int, run_id: UUID | str, claim_token: UUID | str | None, command_id: UUID | str | None=None, owner_id: str | None=None) -> TurnState` — Release the recovery lease overlay (typed event, no expiry field).
    - `_lease_head(self, turn_id: UUID)`
    - `_require_lease_fence(self, turn_id: UUID, run_id: UUID, claim_token: UUID | None) -> None`
    - `_live_recovery_claim(self, turn_id: UUID) -> bool`
    - `requeue_stale_run(self, turn_id: UUID | str, *, expected_version: int, abandoned_run_id: UUID | str, command_id: UUID | str | None=None) -> TurnState` — Typed stale-run requeue: appends turn.stale-run-requeued.v1.
    - `wait_for_input(self, turn_id: UUID | str, prompt: str, *, expected_version: int, run_id: UUID | str, interrupt_id: UUID | str | None=None, command_id: UUID | str | None=None) -> TurnState` — 让当前 Run 持久化进入“等待用户输入”，并记录可恢复的 interrupt。
    - `wait_for_approval(self, turn_id: UUID | str, prompt: str, *, expected_version: int, run_id: UUID | str, interrupt_id: UUID | str | None=None, approval_request_id: UUID | str | None=None, command_id: UUID | str | None=None) -> TurnState` — 持久化审批等待；带 request ID 的新请求只能由 ApprovalService 结算。
    - `pause_turn(self, turn_id: UUID | str, expected_version: int, reason: str='operator requested pause', *, run_id: UUID | str | None=None, command_id: UUID | str | None=None) -> TurnState` — 暂停 QUEUED/RUNNING Turn；暂停运行中 Turn 时必须通过 run fencing。
    - `request_resume(self, turn_id: UUID | str, expected_version: int, *, interrupt_id: UUID | str | None=None, response: str | bool | None=None, command_id: UUID | str | None=None) -> TurnState` — 确认挂起原因并把 Turn 重新排队，而不是直接伪造 RUNNING 状态。
    - `resolve_runtime_outcome(self, turn_id: UUID | str, *, expected_version: int, run_id: UUID | str, evidence_kind: str, evidence_digest: str, reconciler: str, command_id: UUID | str | None=None) -> TurnState` — Queue a PAUSED Turn only after its uncertain Run effects reconcile.
    - `complete_turn(self, turn_id: UUID | str, summary: str, *, expected_version: int, run_id: UUID | str, evidence_ref: CompletionEvidenceRef | None=None, command_id: UUID | str | None=None) -> TurnState` — 由当前合法 Worker 完成 Turn，并在同一事务中释放 Thread。
    - `fail_turn(self, turn_id: UUID | str, error: str, *, expected_version: int, run_id: UUID | str, command_id: UUID | str | None=None) -> TurnState` — 由当前合法 Worker 标记执行失败，并在同一事务中释放 Thread。
    - `cancel_turn(self, turn_id: UUID | str, reason: str, *, expected_version: int, command_id: UUID | str | None=None) -> TurnState` — 由操作方取消非终态 Turn；不要求 Worker run_id。
    - `timeout_turn(self, turn_id: UUID | str, reason: str, *, expected_version: int, command_id: UUID | str | None=None) -> TurnState` — 由调度/操作方把非终态 Turn 标记为超时。
    - `archive_thread(self, thread_id: UUID | str, *, expected_version: int, command_id: UUID | str | None=None) -> ThreadState` — 归档没有活跃 Turn 的开放 Thread，并使用精确版本防并发写入。
    - `get_thread(self, thread_id: UUID | str) -> ThreadState` — 读取完整 Thread 事件流并重放，得到当前领域状态。
    - `get_turn(self, turn_id: UUID | str) -> TurnState` — 读取完整 Turn 事件流并重放，得到当前领域状态。
    - `get_run(self, run_id: UUID | str) -> RunState | LegacyRunState` — Read explicit Run truth, or synthesize a read-only legacy view.
    - `record_completion_evidence(self, turn_id: UUID | str, *, run_id: UUID | str, final_text: str, command_id: UUID | str | None=None) -> CompletionEvidenceRef` — Persist the exact final output fact before terminalizing a Run.
    - `_legacy_run(self, run_id: UUID) -> LegacyRunState`
    - `_read_stream(self, stream_id: StreamId, *, through_version: int | None=None) -> tuple[Any, ...]` — 分页读完事件流；可在指定版本处截断以重建某次命令的结果。
    - `_wait(self, turn_id: UUID | str, prompt: str, event_type: str, *, expected_version: int, run_id: UUID | str, interrupt_id: UUID | str | None, approval_request_id: UUID | str | None, command_id: UUID | str | None) -> TurnState` — 实现两种等待命令的共享流程，并持久化 interrupt 身份与提示。
    - `_worker_terminate(self, turn_id: UUID | str, value: str, *, value_name: str, expected_version: int, run_id: UUID | str, command_id: UUID | str | None, terminal_status: TurnStatus, event_type: str, evidence_ref: CompletionEvidenceRef | None) -> TurnState` — Worker 终止路径：只允许当前 RUNNING 且 run_id 匹配的执行者。
    - `_operator_terminate(self, turn_id: UUID | str, reason: str, *, expected_version: int, command_id: UUID | str | None, terminal_status: TurnStatus, event_type: str) -> TurnState` — 操作方终止路径：可结束任意非终态 Turn，不冒充某个 Worker Run。
    - `_terminate_turn(self, turn: TurnState, command_id: UUID, fingerprint: str, terminal_status: TurnStatus, event_type: str, payload: Mapping[str, Any], *, event_run_id: UUID | None, evidence_ref: CompletionEvidenceRef | None, effect_preconditions: tuple[StreamPrecondition, ...]=()) -> TurnState` — 原子写入 Turn 终态事件与 Thread 脱离事件。
    - `_append_turn_event(self, turn: TurnState, command_id: UUID, fingerprint: str, event_type: str, payload: Mapping[str, Any], *, event_slot: str, run_id: UUID | None, run_terminal_event_type: str | None=None, run_detail: str | None=None, preconditions: tuple[StreamPrecondition, ...]=()) -> TurnState` — Append a Turn fact and, when requested, close its Run atomically.
    - `_get_explicit_running_run(self, run_id: UUID, turn: TurnState) -> RunState`
    - `_get_turn_at_version(self, turn_id: UUID, expected_version: int) -> TurnState` — 重建 Turn 并执行精确版本校验，拒绝基于旧快照作出的命令。
    - `_canonical_required(self, value: Any, max_utf8_bytes: int, name: str) -> CanonicalText` — Canonicalize one free-text entry before fingerprint/event formation.
    - `_committed_receipt(self, command_id: UUID, fingerprint: str, result_stream: StreamId) -> AppendReceipt | None` — 查询已提交命令回执，并确认它确实包含本命令应返回的流。
    - `_thread_from_receipt(self, thread_id: UUID, receipt: AppendReceipt) -> ThreadState` — 只重放到 receipt 记录的 Thread 版本，返回本命令提交时的状态。
    - `_turn_from_receipt(self, turn_id: UUID, receipt: AppendReceipt) -> TurnState` — 只重放到 receipt 记录的 Turn 版本，隔离随后并发写入的“未来状态”。
    - `_fingerprint(self, action: str, arguments: Mapping[str, Any]) -> str` — 把命令语义规范化为稳定 JSON，供幂等键检测“同键是否同命令”。
    - `_event(self, *, event_type: str, payload: Mapping[str, Any], occurred_at: datetime, command_id: UUID, event_slot: str, thread_id: UUID | None=None, turn_id: UUID | None=None, run_id: UUID | None=None) -> NewEvent` — 构造领域事件，并从 command_id + event_slot 确定性派生 event_id。
    - `_append(self, command_id: UUID, fingerprint: str, *writes: StreamWrite, preconditions: tuple[StreamPrecondition, ...]=()) -> AppendReceipt` — 把一个命令产生的一个或多个流写入作为原子批次交给 EventStore。


### `control/schema.py`

> I5 database schema manager.

- **class `DatabaseSchemaError`** (RuntimeError)
  - Content-free schema failure; carries only a stable code.

- **class `DatabaseState`** (StrEnum)

- **`register_fault_hook(point: str, hook: Callable[[], None] | None) -> None`** — Install/remove one named callable for the deterministic fault points.
- **`inject_fault(point: str) -> None`** — Trigger a registered fault once per call; production default is no-op.
- **class `MigrationStep`**
  - 字段:
    - `from_version: int`
    - `to_version: int`
    - `migration_id: str`
    - `checksum: str`
    - `ddl: str`
    - `apply: Callable[[sqlite3.Connection], None]`
    - `postcheck: Callable[[sqlite3.Connection], None]`

- **`schema_signature(connection) -> str`** — Canonical user-object signature for classification and verification.
- **class `Classification`**
  - 字段:
    - `state: DatabaseState`
    - `user_version: int`

- **`classify_database(database_path: str | Path, *, busy_timeout_ms: int=10000) -> Classification`** — Classify an existing database file with a read-only snapshot.
- **`classify_connection(connection: sqlite3.Connection) -> Classification`** — Classify the caller's already captured snapshot, without reopening media.
- **`ensure_schema(database_path: str | Path, *, busy_timeout_ms: int=10000) -> int`** — Classify then (if needed) migrate the database to the current version.

- 常量(字符串):
  - `DATABASE_LEGACY_EXPORT_REQUIRED` = `database_legacy_export_required`
  - `DATABASE_SCHEMA_TOO_NEW` = `database_schema_too_new`
  - `DATABASE_SCHEMA_UNKNOWN` = `database_schema_unknown`
  - `DATABASE_MIGRATION_FAILED` = `database_migration_failed`
  - `DATABASE_CLASSIFICATION_FAILED` = `database_classification_failed`

### `control/sqlite_store.py`

> 基于 SQLite 的 append-only Event Store 实现。

- **class `SqliteEventStore`**
  - 文件型 Event Store：支持精确版本控制和多流原子追加。
  - 方法:
    - `durable_limits(self) -> dict[str, int]` — Return the normalized exact-key runtime ingress policy.
    - `database_path(self) -> Path` — 返回当前 Event Store 使用的数据库文件路径。
    - `database_time(self) -> datetime` — Return the SQLite database-authoritative UTC clock.
    - `append_batch(self, writes: Sequence[StreamWrite], *, idempotency_key: UUID, request_fingerprint: str | None=None, preconditions: Sequence[StreamPrecondition]=()) -> AppendReceipt` — 把一个命令产生的多个流写入作为一个事务原子追加。
    - `read_idempotency(self, idempotency_key: UUID, *, request_fingerprint: str) -> AppendReceipt | None` — 按命令键和语义指纹读取已提交回执。
    - `read_stream(self, stream_id: StreamId, *, after_version: int=-1, limit: int=500) -> tuple[StoredEvent, ...]` — 按流内版本升序读取某个聚合的事件页。
    - `current_global_position(self) -> int` — Return the highest committed global position (0 for an empty log).
    - `read_all(self, *, after_position: int=0, through_position: int | None=None, limit: int=500) -> tuple[StoredEvent, ...]` — 按全局位置升序读取整个事件库的一页。
    - `_initialize(self) -> None` — 设置持久化策略（表结构由 I5 schema manager 在打开前保证）。
    - `_connect(self) -> sqlite3.Connection` — 创建一个配置一致的短生命周期 SQLite 连接。
    - `_read_connect(self) -> sqlite3.Connection` — Open a short query-only connection without writer-only PRAGMAs.
    - `_project_typed_event(self, connection: sqlite3.Connection, event_document: Mapping[str, Any], stream_version: int, turn_id: str) -> None` — Update the rebuildable recoverable/lease projection from a typed
    - `_project_lease_event(self, connection: sqlite3.Connection, event_document: Mapping[str, Any], stream_version: int, turn_id: str) -> None`
    - `publish_checkpoint_cache(self, record: CheckpointCacheRecord, *, expected_cache_version: int) -> CacheReceipt` — Atomically verify source event + Turn fence and monotonic upsert.
    - `load_checkpoint_cache(self, turn_id: UUID) -> CheckpointCacheRecord | None`
    - `list_recoverable(self, *, expired_before: datetime, after_turn_id: UUID | None=None, limit: int=1000) -> tuple[RecoverableTurn, ...]`
    - `get_active_lease(self, turn_id: UUID, run_id: UUID, owner_id: str) -> RunLease | None`


## `editing/`

> D4 typed patch protocol, atomic workspace transaction, and patch tools.

### `editing/__init__.py`

> D4 typed patch protocol, atomic workspace transaction, and patch tools.


### `editing/protocol.py`

> D4 结构化 Patch 协议与纯内存精确应用。

- **class `PatchError`** (Exception)
  - 可安全跨工具边界传播的稳定 Patch 失败。

- **class `PatchOperation`** (StrEnum)

- **class `NewlineStyle`** (StrEnum)

- **class `PatchLimits`**
  - Patch 文档、内存规划和模型可见结果共用的硬上限。
  - 字段:
    - `max_patch_json_chars: int = 262144`
    - `max_path_chars: int = 1024`
    - `max_files: int = 32`
    - `max_hunks_per_file: int = 128`
    - `max_lines_per_hunk: int = 500`
    - `max_line_chars: int = 10000`
    - `max_file_bytes: int = 1000000`
    - `max_file_lines: int = 100000`
    - `max_total_input_bytes: int = 16 * 1024 * 1024`
    - `max_total_output_bytes: int = 16 * 1024 * 1024`
    - `max_result_chars: int = 100000`

- **class `PatchHunk`**
  - 字段:
    - `old_start: int`
    - `old_lines: tuple[str, ...]`
    - `new_lines: tuple[str, ...]`

- **class `AddFileChange`**
  - 字段:
    - `operation: PatchOperation`
    - `path: str`
    - `content: str`
    - `newline: NewlineStyle`
    - `utf8_bom: bool`

- **class `UpdateFileChange`**
  - 字段:
    - `operation: PatchOperation`
    - `path: str`
    - `base_sha256: str`
    - `hunks: tuple[PatchHunk, ...]`

- **class `DeleteFileChange`**
  - 字段:
    - `operation: PatchOperation`
    - `path: str`
    - `base_sha256: str`

- **class `PatchSet`**
  - 字段:
    - `schema_version: int`
    - `changes: tuple[FileChange, ...]`
    - `document_sha256: str`

- **class `TextDocument`**
  - 已解码文本的格式事实；UPDATE 重建时必须全部保留。
  - 字段:
    - `lines: tuple[str, ...]`
    - `newline: str`
    - `final_newline: bool`
    - `utf8_bom: bool`

- **`parse_patch_document(value: str, limits: PatchLimits | None=None) -> PatchSet`** — 严格解析一个完整 Patch JSON object，不接受重复键或宽松类型。
- **`decode_text_document(data: bytes, *, max_lines: int=1000000) -> TextDocument`** — 只接受 UTF-8 文本，并识别需要由 UPDATE 保留的格式。
- **`encode_text_document(document: TextDocument) -> bytes`**
- **`apply_update(before: bytes, change: UpdateFileChange, limits: PatchLimits) -> tuple[bytes, int, int]`** — 按原始行号精确应用 hunks；返回新字节及增加/删除行数。
- **`encode_add(change: AddFileChange, limits: PatchLimits) -> bytes`** — ADD 的 content 使用 LF 作为协议换行，再按显式声明编码。

### `editing/tools.py`

> D4 ``apply_patch`` ToolSpec、handler 与 D3+D4 组合 Registry。

- **class `ApplyPatchArguments`**
  - 字段:
    - `patch_json: str`

- **`apply_patch_tool_spec(limits: PatchLimits | None=None) -> ToolSpec[ApplyPatchArguments]`** — 外层 schema 保持 D3 子集；嵌套 Patch 文档由版本化 parser 严格校验。
- **`register_patch_tool(registry: RepositoryToolRegistry, workspace_root: str | Path, resolver: WorkspacePathResolver, *, limits: PatchLimits | None=None, fault_injector: FaultInjector | None=None, protected_paths: frozenset[str]=frozenset(), protected_patterns: tuple[str, ...]=(), observer: PatchObserver | None=None) -> None`** — 注册 D4 工具；D5 可附加脏文件保护与成功修改证据。
- **`build_coding_tool_registry(workspace_root: str | Path, *, repository_limits: RepositoryToolLimits | None=None, patch_limits: PatchLimits | None=None, fault_injector: FaultInjector | None=None, protected_patterns: tuple[str, ...]=()) -> RepositoryToolRegistry`** — 构建 D3 read/list/search + D4 apply_patch 的单一、可 seal Registry。

### `editing/transaction.py`

> D4 工作区 Patch 两阶段事务。

- **class `PatchFileResult`**
  - 字段:
    - `operation: PatchOperation`
    - `path: str`
    - `before_sha256: str | None`
    - `after_sha256: str | None`
    - `additions: int`
    - `deletions: int`
    - `diff: str`

- **class `PatchTransactionResult`**
  - 字段:
    - `patch_sha256: str`
    - `files: tuple[PatchFileResult, ...]`
  - 方法:
    - `to_tool_content(self, *, max_chars: int) -> str` — 生成确定性、有界 JSON；超大 diff 被显式截断而非静默丢失。

- **class `AtomicPatchWorkspace`**
  - 绑定一个 workspace 的结构化 Patch 执行器。
  - 方法:
    - `apply(self, patch: PatchSet, *, progress_guard: ProgressGuard | None=None) -> PatchTransactionResult`
    - `_plan(self, patch: PatchSet, guard: ProgressGuard) -> tuple[_Plan, ...]`
    - `_stage(self, plans: tuple[_Plan, ...], guard: ProgressGuard) -> list[_Staged]`
    - `_commit(self, staged: list[_Staged], guard: ProgressGuard) -> None`
    - `_rollback(self, committed: list[_Staged], staged: list[_Staged], guard: ProgressGuard) -> bool`
    - `_revalidate(self, plan: _Plan, guard: ProgressGuard) -> None`
    - `_verify_committed(self, item: _Staged, guard: ProgressGuard) -> None`
    - `_assert_installed(self, item: _Staged, guard: ProgressGuard) -> None`
    - `_assert_parent(self, plan: _Plan, guard: ProgressGuard) -> None`
    - `_directory_identity(self, path: str, guard: ProgressGuard) -> tuple[int, ...]`
    - `_read_existing(self, path: str, guard: ProgressGuard) -> WorkspaceFileRead`
    - `_assert_missing(target: Path, *, initial: bool) -> None`
    - `_backup_name(parent: Path) -> Path`
    - `_fault(self, point: str, path: str) -> None`
    - `_discard_uncommitted(staged: list[_Staged]) -> bool`


## `execution/`

> D2 bounded agent-loop orchestration and durable Turn worker.

### `execution/__init__.py`

> D2 bounded agent-loop orchestration and durable Turn worker.


### `execution/compaction.py`

> D23-C: turn-internal closed execution-group parsing and selection (pure functions).

- **class `CompactionError`** (RuntimeError)
  - Stable, content-free compaction failure safe to print to an operator.

- **class `ClosedExecutionGroup`**
  - One ``ModelTurn`` plus its tool results — the atomic compaction unit.
  - 字段:
    - `model_turn_id: UUID`
    - `assistant_text: str`
    - `calls: tuple[ToolCallEcho, ...]`
    - `results: tuple[ToolResultMessage, ...]`
    - `first_context_index: int`
    - `last_context_index: int`
    - `closed: bool`

- **`parse_closed_groups(context: Sequence[ModelContextItem]) -> tuple[ClosedExecutionGroup, ...]`** — Split a model-context sequence into ``ClosedExecutionGroup`` records.
- **`select_compressible(groups: Sequence[ClosedExecutionGroup], *, keep_recent: int) -> tuple[ClosedExecutionGroup, ...]`** — Return the older closed groups, keeping the most recent ``keep_recent``.
- **`anchors_are_preserved(groups: Sequence[ClosedExecutionGroup], selected: Sequence[ClosedExecutionGroup]) -> bool`** — Verify a selection leaves every anchor untouched (pure-function level).

### `execution/loop.py`

> D2 的有界模型—工具 Agent Loop。

- **class `AgentLoopError`** (Exception)
  - 可以安全写入 D1 failure outcome 的稳定 Loop 错误。

- **class `AgentLoopCancelled`** (AgentLoopError)
  - 协作式取消边界；partial 模型输出不能成为成功结果。

- **class `AgentLoopRecoveryBlocked`** (AgentLoopError)
  - A durable tool claim needs recovery/query/operator action.

- **class `AgentLoopApprovalWaiting`** (AgentLoopError)
  - Policy durably suspended the Turn before any tool claim or side effect.

- **class `ModelClient`** (Protocol)
  - Provider adapter 需要实现的同步 typed-stream 端口。
  - 方法:
    - `stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]` — 返回一条以 typed terminal 结束的 canonical event stream。

- **class `ToolExecutionResult`**
  - D3 Tool Registry 返回给 Loop 的最小模型可见结果。
  - 字段:
    - `content: str`
    - `is_error: bool = False`

- **class `ToolExecutionContext`**
  - 工具调用的本地执行身份；D7 ledger 会扩展其副作用语义。
  - 字段:
    - `run_id: UUID`
    - `model_turn_id: UUID`
    - `model_round: int`
    - `call_ref: ModelCallRef`
    - `turn_id: UUID | None = None`
    - `turn_version: int | None = None`
    - `execution_id: UUID | None = None`
    - `ancestor_turn_ids: tuple[UUID, ...] = ()`
    - `recovered_call: bool = False`
    - `progress_guard: Callable[[], None] | None = field(default=None, repr=False, compare=False)`
  - 方法:
    - `check_progress(self) -> None` — 长工具在自己的阻塞循环中复查取消与当前 Run ownership。

- **class `ToolExecutor`** (Protocol)
  - D3 Tool Registry 实现的定义与执行单一入口。
  - 方法:
    - `definitions(self) -> tuple[ToolDefinition, ...]` — 返回模型可见工具定义的冻结快照。
    - `execute(self, call: ToolCallItem, *, context: ToolExecutionContext) -> ToolExecutionResult` — 执行一个完整调用；普通工具失败用 ``is_error=True`` 返回。

- **class `CompletionGate`** (Protocol)
  - D5 验证闸门：模型文本不能替代真实测试与 diff 证据。
  - 方法:
    - `assert_complete(self, run_id: UUID) -> None` — 证据不足时抛出带稳定 code 的 ``AgentLoopError``。

- **class `DurableExecutionSink`** (Protocol)
  - D6 typed-fact sink; called only for complete canonical values.
  - 方法:
    - `model_completed(self, turn: ModelTurn, projected: Sequence[ModelContextItem], model_round: int, output_chars: int, has_tools: bool) -> None`
    - `tool_started(self, call_id: str, tool_name: str) -> None`
    - `tool_completed(self, result: ToolResultMessage, tool_count: int) -> None`

- **class `CancellationToken`**
  - 线程安全的协作式取消标记。
  - 方法:
    - `cancel(self) -> None`
    - `cancelled(self) -> bool`
    - `raise_if_cancelled(self) -> None`

- **class `AgentLoopLimits`**
  - 约束一个 Turn 内的模型轮数、工具数和累计文本。
  - 字段:
    - `max_model_rounds: int = 32`
    - `max_tool_calls: int = 128`
    - `max_total_output_chars: int = 8000000`

- **class `AgentLoopResult`**
  - 一次 Loop 的结果；D6 recorder 可从 typed facts 跨进程重建。
  - 字段:
    - `final_text: str`
    - `context: tuple[ModelContextItem, ...]`
    - `model_turns: tuple[ModelTurn, ...]`
    - `model_rounds: int`
    - `tool_calls: int`

- **class `AgentLoop`**
  - 完整验证模型回合后，确定性执行工具或返回最终答案。
  - 方法:
    - `bind_compaction_sink(self, sink) -> None` — Audit F12: attach this RUN's durable recorder as the compaction sink.
    - `clear_compaction_sink(self) -> None`
    - `context_chars(self, context: Sequence[ModelContextItem]) -> int` — Canonical UTF-8 byte+char budget of the request projection (D23 §5.4).
    - `_maybe_compact(self, context: list[ModelContextItem]) -> None` — D23 §5.3/§5.4 safe-point preflight before the next model request.
    - `_tool_count_known(self) -> int`
    - `_definitions_chars(self, definitions=None) -> int` — E1/E2 + R4: pinned tool definitions ride every request and must
    - `_request_estimate(self, context, definitions=None) -> int` — R4: conservative full-request char estimate.
    - `_assert_request_fits(self, context, definitions, max_output_tokens) -> None` — R4: independent final-send gate.
    - `_deliver_result(self, call, result_message: ToolResultMessage, execution_context, durable_sink) -> ToolResultMessage` — Plan B delivery gate (spec v4).
    - `_pending_tool_calls(self) -> bool`
    - `durable_tool_execution(self) -> bool`
    - `has_tools(self) -> bool`
    - `last_snapshot(self)` — The frozen ToolCatalogSnapshot bound to the last model round.
    - `_take_snapshot(self)` — Atomically snapshot the catalog for THIS model round (§8.8).
    - `_trace(self, stream: str, kind: str, fields: dict[str, object]) -> None`
    - `run(self, *, run_id: UUID, turn_id: UUID | None=None, turn_version: int | None=None, input_items: Sequence[ModelContextItem], provider: str, model: str, max_output_tokens: int=4096, cancellation: CancellationToken | None=None, event_sink: ModelEventSink | None=None, ownership_guard: OwnershipGuard | None=None, durable_sink: DurableExecutionSink | None=None, initial_model_rounds: int=0, initial_tool_calls: int=0, initial_output_chars: int=0, resume_tool_calls: Sequence[ToolCallEcho]=()) -> AgentLoopResult` — 运行有界模型—工具循环，直到产生合法 final 或明确失败。
    - `_collect_turn(self, request: ModelRequest, cancellation: CancellationToken, event_sink: ModelEventSink | None, ownership_guard: OwnershipGuard | None) -> ModelTurn` — 完整消费 Provider iterable；wire progress 处同时检查取消与 ownership。


### `execution/worker.py`

> 把 D2 ``AgentLoop`` 接到 D1 durable Thread/Turn 生命周期。

- **class `ContextUnavailable`** (Exception)
  - 续跑需要 D6 CheckpointStore，但当前 Worker 未配置或阶段不安全。

- **class `TurnWorkerResult`**
  - D1 终态和可选 D2 进程内结果的组合。
  - 字段:
    - `turn: TurnState`
    - `loop_result: AgentLoopResult | None`

- **class `TurnWorker`**
  - 以 D1 run_id 作为 fence，安全完成或失败一个 D2 Agent Loop。
  - 方法:
    - `execute(self, turn_id: UUID | str, expected_version: int, *, cancellation: CancellationToken | None=None, event_sink: ModelEventSink | None=None) -> TurnWorkerResult` — 从 QUEUED 启动新 Run，并用同一 fence 提交最终状态。
    - `_build_resume_block(self, queued: TurnState, context: Sequence[ModelContextItem]) -> dict` — Build the v2 seed resume block for a typed resume response.


## `ledger/`

> D7 Tool Ledger public boundary.

### `ledger/__init__.py`

> D7 Tool Ledger public boundary.


### `ledger/executor.py`

> The only durable Runtime path from a canonical ToolCall to a handler.

- **class `AuthorizedToolCall`**
  - Process-local, non-persisted ticket issued only after the D9 policy gate.
  - 字段:
    - `_executor: 'LedgerExecutor'`
    - `_token: object`
    - `_issuance: object`
    - `call: ToolCallItem`
    - `context: ToolExecutionContext`
    - `prepared_invocation: object | None`
    - `record: ToolExecutionRecord | None`
    - `early_result: ToolExecutionResult | None`
    - `action: ResolvedAction | None`
    - `verdict: PolicyVerdict | None`
    - `approval: ApprovalRecord | None`

- **class `LedgerExecutor`**
  - Write-ahead claim, execute once per claim, and durably record the result.
  - 方法:
    - `ledger(self) -> ToolLedgerStore`
    - `catalog_snapshot(self)` — I6 §8.8: one frozen snapshot (definitions + profiles + resolvers +
    - `definitions(self) -> tuple[ToolDefinition, ...]`
    - `execute(self, call: ToolCallItem, *, context: ToolExecutionContext) -> ToolExecutionResult` — Compatibility entry; AgentLoop uses authorize before D6 tool_started.
    - `authorize(self, call: ToolCallItem, *, context: ToolExecutionContext) -> AuthorizedToolCall` — Schema-check, authorize and claim without invoking the handler.
    - `execute_authorized(self, authorization: AuthorizedToolCall) -> ToolExecutionResult` — Consume one executor-issued ticket and invoke at most one handler.
    - `_ticket(self, call: ToolCallItem, context: ToolExecutionContext, *, prepared: object | None, record: ToolExecutionRecord | None, early_result: ToolExecutionResult | None=None, action: ResolvedAction | None=None, verdict: PolicyVerdict | None=None, approval: ApprovalRecord | None=None) -> AuthorizedToolCall`
    - `_release_active_claim(self, ticket: AuthorizedToolCall) -> None`
    - `_discard_prepared(self, ticket: AuthorizedToolCall) -> None`
    - `_discard_prepared_invocation(self, prepared: object | None) -> None`
    - `_load_existing(self, context: ToolExecutionContext, binding_digest: str | None) -> ToolExecutionRecord | None`
    - `_authorize_terminal(self, call: ToolCallItem, context: ToolExecutionContext, profile: ToolRecoveryProfile, prepared: object | None, record: ToolExecutionRecord) -> AuthorizedToolCall`
    - `_claim_authority(self, record: ToolExecutionRecord) -> tuple[str, str, str] | None`
    - `_resolve(resolver: ActionResolver, call: ToolCallItem, context: ToolExecutionContext, profile: ToolRecoveryProfile, previous: ResolvedAction | None) -> ResolvedAction`
    - `_j2_check(self, record, action, context) -> None` — J2: escalate ALLOW-verdict actions on an exact session-canary hit.
    - `_require_grant(self, record: ToolExecutionRecord, action: ResolvedAction, verdict: PolicyVerdict, *, context: ToolExecutionContext) -> ApprovalRecord | None`
    - `_fault(self, point: str, record: ToolExecutionRecord) -> None`
    - `_trace(self, stream: str, kind: str, fields: Mapping[str, object]) -> None`


### `ledger/protocol.py`

> D7 durable tool-execution identity and recovery protocol.

- **class `ToolLedgerError`** (RuntimeError)
  - Stable, content-free D7 contract failure.

- **class `ToolLedgerConflict`** (ToolLedgerError)
  - The same logical invocation was observed with different semantics.

- **class `ToolOutcomeBlocked`** (ToolLedgerError)
  - Automatic execution is forbidden because the prior outcome is uncertain.

- **class `ToolExecutionState`** (Enum)

- **class `SideEffectClass`** (Enum)

- **class `RecoveryMode`** (Enum)

- **class `ToolRecoveryProfile`**
  - Deterministic Runtime declaration; the model cannot lower this risk.
  - 字段:
    - `side_effect_class: SideEffectClass`
    - `recovery_mode: RecoveryMode`

- **class `LogicalExecutionIdentity`**
  - I6 §8.8 / plan §10.3: ONE typed key for prepare/claim/load/recovery.
  - 字段:
    - `turn_id: UUID`
    - `model_turn_id: UUID`
    - `call_id: str`
    - `binding_digest: str | None = None`
  - 方法:
    - `derive(self) -> UUID` — The one stable execution key for this logical identity.

- **class `DurableToolResult`**
  - A bounded, redacted result that may be reused after process restart.
  - 字段:
    - `content: str`
    - `is_error: bool = False`

- **class `ToolExecutionRecord`**
  - 字段:
    - `execution_id: UUID`
    - `turn_id: UUID`
    - `model_turn_id: UUID`
    - `call_id: str`
    - `tool_name: str`
    - `arguments_sha256: str`
    - `arguments_bytes: int`
    - `profile: ToolRecoveryProfile`
    - `state: ToolExecutionState`
    - `version: int`
    - `binding_digest: str | None = None`
    - `claim_epoch: int = 0`
    - `claimant_run_id: UUID | None = None`
    - `claim_token: UUID | None = None`
    - `result: DurableToolResult | None = None`
    - `result_sha256: str | None = None`
    - `result_bytes: int | None = None`
    - `unknown_reason: str | None = None`

- **class `LookupOutcome`** (Enum)

- **class `LookupResult`**
  - 字段:
    - `outcome: LookupOutcome`
    - `result: DurableToolResult | None = None`

- **class `AuthoritativeLookup`** (Protocol)

- **`logical_execution_id(turn_id: UUID, model_turn_id: UUID, call_id: str, binding_digest: str | None=None) -> UUID`** — Derive the stable logical key without claimant ``run_id``.
- **`canonical_arguments_digest(arguments_json: str) -> tuple[str, int]`** — Hash a strict, versioned canonical JSON object without persisting it.
- **`result_digest(content: str) -> tuple[str, int]`**

### `ledger/recovery.py`

> D7 recovery decisions for orphaned durable tool claims.

- **class `ToolRecoveryManager`**
  - Reconcile D6 pending calls without ever blindly replaying risky writes.
  - 方法:
    - `reconcile_pending(self, turn_id: UUID, pending_tool_calls: Sequence[Mapping[str, Any]]) -> bool` — Return true only when a new Run can safely consume every pending call.
    - `_reconcile_query(self, record: ToolExecutionRecord) -> bool`
    - `_mark_unknown(self, record: ToolExecutionRecord, reason: str) -> None`


### `ledger/store.py`

> Append-only D7 Tool Ledger built on the existing transactional Event Store.

- **class `ToolLedgerStore`**
  - Persist logical calls and physical claims as versioned JSON facts.
  - 方法:
    - `load(self, execution_id: UUID) -> ToolExecutionRecord | None`
    - `load_for_call(self, turn_id: UUID, model_turn_id: UUID, call_id: str, binding_digest: str | None=None) -> ToolExecutionRecord | None` — Load with the EXACT typed identity (plan §10.3).
    - `prepare(self, *, turn_id: UUID, turn_version: int, run_id: UUID, model_turn_id: UUID, call_id: str, tool_name: str, arguments_json: str, profile: ToolRecoveryProfile, binding_digest: str | None=None) -> ToolExecutionRecord`
    - `claim(self, record: ToolExecutionRecord, *, turn_version: int, run_id: UUID) -> ToolExecutionRecord`
    - `commit_result(self, record: ToolExecutionRecord, result: DurableToolResult, *, actor: str='ledger') -> ToolExecutionRecord`
    - `commit_prepared_failure(self, record: ToolExecutionRecord, result: DurableToolResult, *, turn_version: int, run_id: UUID, actor: str='policy') -> ToolExecutionRecord` — Close a call rejected before claim; no external effect was entered.
    - `release_not_applied(self, record: ToolExecutionRecord, *, actor: str='recovery') -> ToolExecutionRecord`
    - `resolve_unknown_result(self, record: ToolExecutionRecord, result: DurableToolResult, *, actor: str='operator') -> ToolExecutionRecord` — Resolve an UNKNOWN claim from authoritative query or operator evidence.
    - `resolve_unknown_not_applied(self, record: ToolExecutionRecord, *, actor: str='operator') -> ToolExecutionRecord` — Return UNKNOWN to PREPARED only after a terminal negative lookup.
    - `mark_outcome_unknown(self, record: ToolExecutionRecord, reason: str, *, actor: str='recovery') -> ToolExecutionRecord`
    - `_require_current(self, execution_id: UUID) -> ToolExecutionRecord`
    - `_require_claim(self, record: ToolExecutionRecord) -> ToolExecutionRecord`
    - `_require_unknown(self, record: ToolExecutionRecord) -> ToolExecutionRecord`
    - `_append(self, execution_id: UUID, expected_version: int, event_type: str, payload: Mapping[str, Any], *, turn_id: UUID, run_id: UUID | None, actor: str, turn_version: int | None=None, fence_turn: bool=False) -> None`
    - `_read_all(self, stream: StreamId) -> tuple`


## `mcp/`

> D10 MCP lifecycle: stdio transport, sessions, and tool bindings.

### `mcp/__init__.py`

> D10 MCP lifecycle: stdio transport, sessions, and tool bindings.


### `mcp/activation.py`

> I6 MCP activation: launch identity, durable admin grants, allocation ledger.

- **class `McpActivationError`** (RuntimeError)

- **class `ExecutableIdentity`**
  - 字段:
    - `canonical_path: str`
    - `platform_file_id: str`
    - `size: int`
    - `content_sha256: str`

- **class `CodeArtifactIdentity`**
  - 字段:
    - `role: str`
    - `argv_index: int`
    - `source: ExecutableIdentity`
    - `staged: ExecutableIdentity`

- **class `EnvironmentIdentity`**
  - 字段:
    - `name: str`
    - `value_digest: str`

- **class `ReadOnlyMountIdentity`**
  - 字段:
    - `container_path: str`
    - `source_file_id: str`
    - `manifest_digest: str`

- **class `McpLaunchIdentity`**
  - 字段:
    - `server_id: str`
    - `execution_profile: str`
    - `code_artifacts: tuple[CodeArtifactIdentity, ...]`
    - `argv_digest: str`
    - `cwd_identity_digest: str | None`
    - `environment: tuple[EnvironmentIdentity, ...]`
    - `image_digest: str | None`
    - `read_only_mounts: tuple[ReadOnlyMountIdentity, ...]`
    - `resource_digest: str`
    - `deadline_limit_digest: str`
    - `config_digest: str`
  - 方法:
    - `to_audit_document(self) -> dict[str, object]`

- **`resolve_launch_identity(config, *, base_dir: Path) -> McpLaunchIdentity`**
- **class `StagedLaunchPlan`**
  - 字段:
    - `config: object`
    - `identity: McpLaunchIdentity`
    - `staged_argv: tuple[str, ...]`
    - `code_artifacts: tuple[CodeArtifactIdentity, ...]`
    - `staged_dir: Path`
  - 方法:
    - `revalidate_sources(self) -> None`

- **`stage_code_artifacts(config, *, base_dir: Path, staging_root: Path) -> StagedLaunchPlan`**
- **class `ActivationView`**
  - 字段:
    - `request_id: UUID`
    - `server_id: str`
    - `launch_identity_digest: str`
    - `execution_profile: str`
    - `principal_id: str`
    - `scope: str`
    - `status: str`
    - `version: int`
    - `expires_at: datetime | None`
  - 方法:
    - `request_id_str(self) -> str`

- **class `AllocationIntent`**
  - 字段:
    - `request_id: UUID`
    - `allocation_id: UUID`
    - `attempt: int`
    - `activation_stream_version: int`

- **class `AllocationView`**
  - 字段:
    - `request_id: UUID`
    - `allocation_id: UUID`
    - `status: str`
    - `version: int`
    - `attempt: int`
    - `grant_version: int`
    - `launch_identity_digest: str`
    - `principal_id: str`
    - `claim_epoch: int`
    - `claim_token: UUID | None`

- **class `AuthorizedLaunchTicket`**
  - 字段:
    - `request_id: UUID`
    - `grant_stream_version: int`
    - `grant_digest: str`
    - `allocation_id: UUID`
    - `allocation_stream_version: int`
    - `claim_epoch: int`
    - `claim_token: UUID`
    - `nonce: UUID`
    - `launch_identity_digest: str`
    - `not_after: datetime`

- **`process_start_scope(profile) -> str`**
- **class `ActivationService`**
  - 方法:
    - `plan_start(self, identity: McpLaunchIdentity, *, principal_id: str, scope: str, decision: str) -> ActivationView`
    - `resolve_activation(self, request_id: UUID, approved: bool, *, expected_version: int, approver_principal_id: str) -> ActivationView`
    - `get_activation(self, request_id: UUID) -> ActivationView | None`
    - `pending_requests(self) -> tuple[ActivationView, ...]`
    - `intend(self, view: ActivationView) -> AllocationIntent`
    - `get_allocation(self, allocation_id: UUID) -> AllocationView | None`
    - `reconcile_allocation(self, allocation_id: UUID, *, expected_version: int, outcome: str, evidence_kind: str, evidence_digest: str, reconciler_principal_id: str) -> AllocationView`
    - `claim(self, intent: AllocationIntent, view: ActivationView, *, principal_id: str) -> AuthorizedLaunchTicket`
    - `consume_ticket(self, ticket: AuthorizedLaunchTicket) -> None`
    - `_append_allocation(self, ticket: AuthorizedLaunchTicket, event_type: str, extra: Mapping[str, Any], *, actor: str) -> None`
    - `record_started(self, ticket: AuthorizedLaunchTicket) -> None`
    - `record_ready(self, ticket: AuthorizedLaunchTicket) -> None`
    - `record_stopped(self, ticket: AuthorizedLaunchTicket, *, reason: str='closed') -> None`
    - `record_failed_before_start(self, ticket: AuthorizedLaunchTicket, *, reason: str) -> None`
    - `record_outcome_unknown(self, ticket: AuthorizedLaunchTicket, *, reason: str) -> None`
    - `record_start_observed(self, ticket: AuthorizedLaunchTicket, *, evidence_digest: str) -> None`
    - `record_outcome_resolved(self, ticket: AuthorizedLaunchTicket, *, resolved_state: str, evidence_digest: str, reconciler_principal_id: str) -> None`
    - `_request_id(self, server_id: str, launch_digest: str, principal_id: str) -> UUID`
    - `_activation_stream(self, request_id: UUID) -> StreamId`
    - `_allocation_stream(self, allocation_id: UUID) -> StreamId`
    - `_expired(self, view: ActivationView) -> bool`
    - `_allocations_for_request(self, request_id: UUID) -> tuple[AllocationView, ...]`
    - `_reconstruct_allocation(self, allocation_id: UUID) -> AllocationView | None`
    - `_is_allocation_recovery_receipt(self, allocation_id: UUID, expected_version: int, outcome: str, evidence_kind: str, evidence_digest: str, reconciler_principal_id: str) -> bool`
    - `_require_view(self, request_id: UUID) -> ActivationView`
    - `_is_decision_receipt(self, request_id: UUID, approved: bool, expected_version: int, approver_principal_id: str) -> bool`
    - `_reconstruct(self, request_id: UUID) -> ActivationView | None`
    - `_append_activation(self, request_id: UUID, event_type: str, payload: Mapping[str, Any], expected_version: int) -> None`
    - `_event(self, slot: str, event_type: str, payload: Mapping[str, Any], request_id: UUID, command_id: UUID | None=None) -> NewEvent`
    - `_read_all(self, stream: StreamId) -> tuple`
    - `_now(self) -> datetime`


- 常量(字符串):
  - `_ACTIVATION_NAME` = `koawa-mcp-activation-v1`

### `mcp/connection_manager.py`

> Durable MCP session lifecycle: initialize, catalog, call, refresh, close.

- **class `McpSessionError`** (RuntimeError)
  - Stable, content-free MCP session failure.

- **class `McpOutcomeUncertain`** (Exception)
  - The server may have applied the call; the Runtime must not retry blindly.

- **class `McpCallResult`**
  - 字段:
    - `content: str`
    - `is_error: bool`
    - `uncertain: bool`

- **class `McpSession`**
  - One MCP stdio session with an immutable per-generation tool catalog.
  - 方法:
    - `server_id(self) -> str`
    - `session_instance_id(self) -> UUID` — Physical instance token; fences live handles only (§8.8).
    - `launch_identity(self)`
    - `launch_identity_digest(self) -> str | None`
    - `state(self) -> str`
    - `generation(self) -> int`
    - `catalog(self) -> McpCatalog | None`
    - `pending_refresh(self) -> bool`
    - `unknown_response_count(self) -> int`
    - `connect(self) -> McpCatalog`
    - `refresh(self) -> McpCatalog`
    - `call(self, binding: McpBinding, arguments_json: str, *, timeout: float | None=None) -> McpCallResult`
    - `close(self) -> None`
    - `_shutdown_transport(self) -> None` — Terminal teardown for connect-failure paths.
    - `handler(self, binding: McpBinding)` — Return the D3-style typed handler bound to this session/binding.
    - `_request(self, method: str, params: Mapping[str, Any], *, timeout: float | None=None) -> Any`
    - `_list_tools(self, generation: int) -> McpCatalog`
    - `_extract_result(self, result: Any) -> McpCallResult`
    - `_notification_loop(self) -> None`
    - `_fail_pending(self, error: McpSessionError) -> None`
    - `_trace(self, stream: str, kind: str, fields: Mapping[str, Any]) -> None`
    - `_throttle_notify(self) -> bool` — I1: merge notification storms; False means this notification is dropped.
    - `_spawn_refresh_worker(self) -> None` — I1: single-flight refresh worker; concurrent notifications merge.
    - `_refresh_worker_run(self) -> None`
    - `_safe_refresh(self) -> None`

- **`configure_release_rules(rules) -> None`** — Install the operator-side per-tool release rules (assembly calls this).
- **`bind_tool_handler(session: McpSession, binding: McpBinding)`** — Build a D3 typed handler that routes one binding to the live session.

### `mcp/docker_endpoint.py`

> D25 W2: long-lived Docker endpoint for one sandboxed stdio MCP server.

- **class `DockerEndpointError`** (RuntimeError)
  - Stable, content-free container endpoint failure.

- **class `DockerMcpEndpoint`**
  - McpProcessEndpoint over one attach client + one container.
  - 方法:
    - `pid(self) -> int`
    - `stdin(self) -> BinaryIO | None`
    - `stdout(self) -> BinaryIO | None`
    - `stderr(self) -> BinaryIO | None`
    - `container_id(self) -> str`
    - `poll(self) -> int | None`
    - `wait(self, *, deadline: float) -> int`
    - `terminate_tree(self, *, deadline: float) -> None` — Graceful path: stdin EOF, bounded stop, exact remove, collect.
    - `kill_tree(self, *, deadline: float) -> None`
    - `close_handles(self) -> None`
    - `external_identity(self) -> dict[str, object]` — Digest-only identity; never argv/env/stderr (§4.7).
    - `_terminate(self, *, deadline: float, kill_attach: bool) -> None`

- **class `DockerAdapter`**
  - Thin trusted wrapper over the Docker CLI (fakes replace this).
  - 方法:
    - `create(self, docker: Path, arguments: tuple[str, ...], *, timeout: float) -> str`
    - `inspect(self, docker: Path, container_id: str, *, timeout: float) -> dict`
    - `start_attach(self, docker: Path, container_id: str) -> '_AttachProcess'`
    - `list_by_labels(self, docker: Path, labels: tuple[tuple[str, str], ...]) -> list[str]` — All containers (running or exited) carrying every given label.
    - `stop_and_remove(self, docker: Path, container_id: str, *, stop_timeout_seconds: float, cli_timeout_seconds: float) -> None`

- **`launch_container_endpoint(spec: ContainerSpec, *, docker_executable: str, process_start_timeout_seconds: float, adapter: DockerAdapter | None=None) -> DockerMcpEndpoint`** — Create, verify and attach one sandboxed MCP container.

### `mcp/fixture_server.py`

> Real local MCP stdio fixture server with env-driven fault injection.

- **`main() -> int`**

- 常量(字符串):
  - `DEFAULT_PROTOCOL_VERSION` = `2025-06-18`

### `mcp/launcher.py`

> I6 MCP process launchers: the ONLY path from ticket to OS create (§8.6).

- **class `McpProcessEndpoint`** (Protocol)
  - A spawned process tree with a verifiable external identity.
  - 方法:
    - `pid(self) -> int`
    - `stdin(self) -> BinaryIO | None`
    - `stdout(self) -> BinaryIO | None`
    - `stderr(self) -> BinaryIO | None`
    - `poll(self) -> int | None`
    - `terminate_tree(self, *, deadline: float) -> None`
    - `kill_tree(self, *, deadline: float) -> None`
    - `wait(self, *, deadline: float) -> int`
    - `close_handles(self) -> None`
    - `external_identity(self) -> dict[str, object]` — Digest-only external identity for recovery evidence.

- **class `McpProcessLauncher`** (Protocol)
  - 方法:
    - `launch(self, ticket: AuthorizedLaunchTicket) -> McpProcessEndpoint` — Consume the ticket and create at most one OS process tree.

- **class `HostLimitEnforcer`** (Protocol)
  - Enforces the declared resource envelope on one owned process tree.
  - 方法:
    - `precheck(self, limits: McpResourceLimits) -> None`
    - `enforce(self, process: OwnedProcess, limits: McpResourceLimits) -> OwnedProcess`

- **class `WindowsJobObjectEnforcer`**
  - Real Windows Job Object enforcement (memory + active-process limits).
  - 方法:
    - `precheck(self, limits: McpResourceLimits) -> None`
    - `enforce(self, process: OwnedProcess, limits: McpResourceLimits) -> OwnedProcess`

- **class `HostTrustedLauncher`**
  - host_trusted stdio launcher over the I1 spawner port.
  - 方法:
    - `launch(self, ticket: AuthorizedLaunchTicket) -> McpProcessEndpoint` — Consume the one-time ticket, re-validate, enforce, then create.
    - `_collect(self, owned: OwnedProcess) -> None`

- **class `SandboxedLauncher`**
  - sandboxed profile: real container boundary, never a host fallback.
  - 方法:
    - `launch(self, ticket: AuthorizedLaunchTicket) -> McpProcessEndpoint`


### `mcp/protocol.py`

> MCP 2025-06-18 JSON-RPC 2.0 message boundary (stdlib only).

- **class `McpProtocolError`** (ValueError)
  - Stable, content-free MCP protocol failure.

- **class `ErrorObject`**
  - 字段:
    - `code: int`
    - `message: str`
    - `data: Any | None = None`

- **class `JsonRpcRequest`**
  - 字段:
    - `jsonrpc: str`
    - `id: int`
    - `method: str`
    - `params: dict[str, Any] | None = None`

- **class `JsonRpcResponse`**
  - 字段:
    - `jsonrpc: str`
    - `id: int`
    - `result: Any | None = None`
    - `error: ErrorObject | None = None`

- **class `JsonRpcNotification`**
  - 字段:
    - `jsonrpc: str`
    - `method: str`
    - `params: dict[str, Any] | None = None`

- **`parse_message(raw: str) -> JsonRpcRequest | JsonRpcResponse | JsonRpcNotification`** — Parse one strict JSON-RPC 2.0 message from an MCP peer.
- **`request_payload(request_id: int, method: str, params: Mapping[str, Any] | None=None) -> str`**
- **`notification_payload(method: str, params: Mapping[str, Any] | None=None) -> str`**
- **`response_payload(request_id: int, result: Any) -> str`**
- **`error_payload(request_id: int, code: int, message: str) -> str`**

- 常量(字符串):
  - `MCP_PROTOCOL_VERSION` = `2025-06-18`
  - `INITIALIZE` = `initialize`
  - `INITIALIZED_NOTIFICATION` = `notifications/initialized`
  - `TOOLS_LIST` = `tools/list`
  - `TOOLS_CALL` = `tools/call`
  - `TOOLS_LIST_CHANGED_NOTIFICATION` = `notifications/tools/list_changed`
  - `JSONRPC_VERSION` = `2.0`

### `mcp/release_rules.py`

> A2 (WP-C §8): per server/tool MCP release rules.

- **class `McpReleaseRuleError`** (RuntimeError)
  - Content-free, stable code for a malformed rule.

- **class `McpReleaseRule`**
  - One operator-side release contract for a server/tool pair.
  - 字段:
    - `server_id: str`
    - `tool_name: str`
    - `fields: tuple[str, ...] | None = None`
    - `excerpt_chars: int | None = None`
    - `audience_policy_ref: str | None = None`
  - 方法:
    - `key(self) -> tuple[str, str]`

- **`build_envelope(*, server_id: str, tool_name: str, redacted_body: str, is_error: bool, rule: McpReleaseRule | None) -> dict`** — Build the model-facing envelope for one MCP call result.

### `mcp/sandbox_reconcile.py`

> D25 W3: dual-ledger binding and crash-window reconciliation.

- **`zero_mount_digest() -> str`**
- **`ensure_sandbox_intent(sandbox_store: SandboxAllocationStore, ticket, *, image_id: str, launch_identity_digest: str, deadline_at: datetime, owner_nonce: str | None=None) -> None`** — Write the sandbox intent before Docker create is legal (§4.2).
- **class `ReconcileFacts`**
  - 字段:
    - `allocation_id: UUID`
    - `image_id: str`
    - `launch_identity_digest: str`
    - `evidence_kind: str`
    - `evidence_digest: str`
    - `reconciler_principal_id: str`
    - `deadline_at: datetime`

- **class `DualLedgerReconciler`**
  - Cross-ledger recovery for sandboxed MCP launches (§W3 window table).
  - 方法:
    - `_from_path(self)`
    - `_find_managed_container(self, image_id: str) -> str | None` — Probe for one managed container by exact labels + image match.
    - `_container_running(self, container_id: str) -> bool | None`
    - `_stop_remove(self, container_id: str) -> bool`
    - `_mcp_recover(self, facts: ReconcileFacts, *, outcome: str, view) -> None`
    - `_evidence_digest(self, container_id: str | None, detail: str) -> str`
    - `reconcile(self, facts: ReconcileFacts) -> str` — Evaluate the crash-window table and drive recovery to a fact.

- **`default_deadline(seconds: float=300.0) -> datetime`**

- 常量(字符串):
  - `ZERO_MOUNT_DIGEST_SOURCE` = `koawa-d25-zero-mount:v1`

### `mcp/tool_binding.py`

> Immutable MCP tool catalogs bound to a session generation.

- **class `McpBindingError`** (RuntimeError)
  - Stable, content-free catalog binding failure.

- **`validate_server_tool(server_id: str, tool: Mapping[str, Any]) -> dict[str, Any]`** — Validate one MCP tool declaration into a binding-ready document.
- **class `SemanticMcpBinding`**
  - Cross-process, recovery-stable binding identity.
  - 字段:
    - `server_id: str`
    - `launch_identity_digest: str`
    - `catalog_digest: str`
    - `catalog_epoch_id: UUID`
    - `tool_name: str`
    - `schema_hash: str`
    - `binding_digest: str`
  - 方法:
    - `to_document(self) -> dict[str, object]`

- **class `PhysicalSessionFence`**
  - Live-handle guard; never enters ledger/approval/recovery identity.
  - 字段:
    - `session_instance_id: UUID`
    - `connection_epoch: int = 0`
    - `catalog_generation: int = 1`

- **class `PreparedMcpInvocation`**
  - Immutable prepared handle + semantic binding + physical fence.
  - 字段:
    - `prepared_call: object`
    - `semantic_binding: SemanticMcpBinding`
    - `physical_fence: PhysicalSessionFence`
  - 方法:
    - `early_result(self)`
    - `binding_digest(self) -> str | None`

- **class `McpBinding`**
  - One immutable (server, generation, tool, schema) binding.
  - 字段:
    - `server_id: str`
    - `session_generation: int`
    - `tool_name: str`
    - `registry_name: str`
    - `schema_hash: str`
    - `spec: ToolSpec[Any]`
    - `arguments_type: type`
  - 方法:
    - `binding_digest(self) -> str`

- **class `McpCatalog`**
  - 字段:
    - `generation: int`
    - `bindings: Mapping[str, McpBinding]`
    - `definitions: tuple[ToolDefinition, ...]`
    - `catalog_digest: str = ''`
    - `catalog_epoch_id: UUID | None = None`
    - `semantic_bindings: Mapping[str, SemanticMcpBinding | None] = MappingProxyType({})`

- **`bind_catalog(server_id: str, generation: int, tools: list[Mapping[str, Any]], *, launch_identity_digest: str | None=None, tool_allowlist: frozenset[str] | None=None) -> McpCatalog`** — Build the immutable generation catalog; any invalid tool fails closed.
- **class `McpRegistryAdapter`**
  - D3 ToolExecutor facade exposing semantic + physical binding identity.
  - 方法:
    - `definitions(self) -> tuple[ToolDefinition, ...]`
    - `prepare(self, call)` — Return an immutable prepared + semantic/physical binding (I6 §8.8).
    - `invoke_prepared(self, prepared, *, context, authority)`
    - `bind_policy_authority(self, authority) -> None`
    - `discard_prepared(self, prepared, *, authority) -> bool`
    - `execute(self, call, *, context)`
    - `binding_digest(self, tool_name: str) -> str | None`
    - `semantic_bindings(self) -> Mapping[str, SemanticMcpBinding | None]`
    - `set_catalog_epoch_id(self, epoch_id: UUID | None) -> None`
    - `catalog_epoch_id(self) -> UUID | None`

- **`build_mcp_registry(session, catalog: McpCatalog) -> McpRegistryAdapter`** — Register one generation catalog into a D3 registry with live handlers.

### `mcp/transport.py`

> MCP stdio transport with strict Content-Length framing (stdlib only).

- **class `TransportError`** (RuntimeError)
  - Stable, content-free MCP transport failure.

- **class `TransportClosed`** (TransportError)

- **class `TransportTimeout`** (TransportError)

- **class `TransportMalformedFrame`** (TransportError)

- **class `TransportPayloadInvalid`** (TransportError)
  - Outbound payload rejected before any byte reached the pipe (NOT_SENT).

- **class `TransportSendUncertain`** (TransportError)
  - A write partially failed and delivery cannot be proven (UNKNOWN).

- **class `TransportOverflow`** (TransportError)
  - Inbound messages exceeded the bounded queue; transport failed closed.

- **class `SpawnSpec`**
  - One immutable spawn request: already-validated command + env + cwd.
  - 字段:
    - `command: tuple[str, ...]`
    - `env: Mapping[str, str]`
    - `cwd: str | None = None`

- **class `CloseReport`**
  - What the terminal close actually proved (§8.7).
  - 字段:
    - `process_exited: bool | None`
    - `process_terminated: bool`
    - `uncertain: bool`
    - `stderr_truncated: bool`
    - `stderr_bytes: int`
    - `cleanup_error: str | None`

- **class `OwnedProcess`** (Protocol)
  - Process tree owned by the transport, with deadline-bounded control.
  - 方法:
    - `pid(self) -> int`
    - `stdin(self) -> BinaryIO`
    - `stdout(self) -> BinaryIO`
    - `stderr(self) -> BinaryIO`
    - `poll(self) -> int | None`
    - `terminate_tree(self, *, deadline: float) -> None`
    - `kill_tree(self, *, deadline: float) -> None`
    - `wait(self, *, deadline: float) -> int`
    - `close_handles(self) -> None`

- **class `ProcessSpawner`** (Protocol)
  - Injectable spawn port; ``deadline`` is an absolute monotonic deadline.
  - 方法:
    - `spawn(self, spec: SpawnSpec, *, deadline: float) -> OwnedProcess`

- **class `SystemProcessSpawner`**
  - Production spawner: controlled spawn + exact tree termination.
  - 方法:
    - `spawn(self, spec: SpawnSpec, *, deadline: float) -> OwnedProcess`

- **class `OwnedSystemProcess`**
  - Production OwnedProcess backed by subprocess.Popen (see spawner doc).
  - 方法:
    - `pid(self) -> int`
    - `stdin(self) -> BinaryIO`
    - `stdout(self) -> BinaryIO`
    - `stderr(self) -> BinaryIO`
    - `poll(self) -> int | None`
    - `terminate_tree(self, *, deadline: float) -> None`
    - `kill_tree(self, *, deadline: float) -> None`
    - `wait(self, *, deadline: float) -> int`
    - `close_handles(self) -> None`
    - `_signal_tree(self, force: bool, *, deadline: float) -> None`
    - `_wait_or_raise(self, deadline: float) -> None`

- **class `StdioTransport`**
  - Spawn one stdio MCP process and frame JSON-RPC messages over it.
  - 方法:
    - `stderr_truncated(self) -> bool`
    - `stderr_bytes(self) -> int`
    - `send_state(self) -> str` — NOT_SENT / SENT / UNKNOWN for the most recent send().
    - `state(self) -> str`
    - `closed(self) -> bool`
    - `pid(self) -> int | None`
    - `close_report(self) -> CloseReport | None` — Terminal CloseReport of the last close (None while open).
    - `_report_allocation(self, state: str) -> None`
    - `open(self) -> None`
    - `send(self, payload: str) -> None`
    - `read(self, timeout: float) -> JsonRpcRequest | JsonRpcResponse | JsonRpcNotification`
    - `close(self) -> None`
    - `_fail_open(self, code: str) -> None`
    - `_mark_terminal(self) -> None`
    - `_enter_closing(self) -> bool`
    - `_is_terminal(self) -> bool`
    - `_is_open(self) -> bool`
    - `_process_exited(self, owned: OwnedProcess) -> bool`
    - `_request_cleanup(self) -> None` — Fail-closed teardown initiated from a background thread.
    - `_perform_teardown(self) -> None`
    - `_wait_bounded(self, owned: OwnedProcess, *, deadline: float, cap: float | None) -> None`
    - `_collect_owned(self, owned: OwnedProcess) -> None` — Force-collect a started tree whose ACK was lost (bounded).
    - `_enqueue(self, item: object) -> bool`
    - `_read_loop(self, process: OwnedProcess) -> None`
    - `_stderr_loop(self, process: OwnedProcess) -> None`

- **`spawn_fixture_command(extra_env: Mapping[str, str] | None=None) -> list[str]`** — Return the stdio command for the local MCP fixture server.

## `model/`

> D2 provider-neutral model protocol, stream assembly, and transport.

### `model/__init__.py`

> D2 provider-neutral model protocol, stream assembly, and transport.


### `model/openai_client.py`

> OpenAI-compatible Chat Completions 的标准库流式适配器。

- **class `OpenAICompatibleClientError`** (ModelError)
  - 尚未建立 response identity 时产生的安全适配器错误。

- **class `ReasoningEffort`** (StrEnum)
  - User-facing reasoning intensity knob, translated per provider/model family.

- **`reasoning_family(provider: str, model: str) -> str | None`** — Return the known reasoning-control family for a provider+model, or None.
- **class `OpenAICompatibleChatClient`**
  - OpenAI-compatible Chat Completions SSE adapter (D2 canonical stream).
  - 方法:
    - `stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]` — 无外部控制器的直接调用；仍受 transport 与整流 deadline 约束。
    - `stream_controlled(self, request: ModelRequest, *, progress_guard: Callable[[], None]) -> Iterator[ModelStreamEvent]` — 让 Agent Loop 在每条 wire line 处检查取消和 D1 ownership。
    - `_stream(self, request: ModelRequest, *, progress_guard: Callable[[], None] | None) -> Iterator[ModelStreamEvent]` — 发送一次请求并把完整 SSE 映射为严格 canonical 事件流。
    - `_attempt(self, request: ModelRequest, http_request: urllib.request.Request, *, progress_guard: Callable[[], None] | None, deadline: float) -> Iterator[ModelStreamEvent]` — 一次 HTTP+解码尝试；首个内容事件出现前缓冲 TurnStarted。


### `model/protocol.py`

> D2 的 Provider-neutral 模型协议。

- **class `ModelError`** (Exception)
  - 所有可安全跨 Agent Loop 边界传播的模型错误。

- **class `ModelProtocolError`** (ModelError)
  - Canonical 模型事件违反本地协议。

- **class `ModelStreamFailure`** (ModelError)
  - Provider 用 typed terminal 明确报告模型流失败。

- **class `InstructionRole`** (StrEnum)
  - 模型请求中可信指令的角色。

- **class `FinishReason`** (StrEnum)
  - 一次完整 Provider 回合的本地终止语义。

- **class `OutputKind`** (StrEnum)
  - 完成态 OutputItem 的封闭类型集合。

- **class `ContentKind`** (StrEnum)
  - 允许通过 delta 预览的文本目标。

- **class `BlockedKind`** (StrEnum)
  - 不能进入模型上下文或工具执行面的输出类型。

- **class `StreamFailureKind`** (StrEnum)
  - Canonical 流失败分类；正文错误不进入对象。

- **class `ModelCallRef`**
  - 把 call_id 约束在生成它的本地 model_turn_id 内。
  - 字段:
    - `model_turn_id: UUID`
    - `call_id: str`

- **class `ModelUsage`**
  - Provider 明确报告的 token 计数；缺失 total 时不自行合成。
  - 字段:
    - `input_tokens: int`
    - `output_tokens: int`
    - `total_tokens: int | None = None`
    - `cached_input_tokens: int | None = None`

- **class `ToolDefinition`**
  - 发送给 Provider 的工具说明；D3 的 Registry 将产生这些值。
  - 字段:
    - `name: str`
    - `description: str | None`
    - `input_schema_json: str`
  - 方法:
    - `input_schema(self) -> dict[str, Any]` — 每次返回独立 JSON 对象，避免调用方修改协议值。

- **class `AssistantTextItem`**
  - 一次模型回合中已经完成的 assistant 文本项。
  - 字段:
    - `canonical_index: int`
    - `item_id: str`
    - `text: str`
  - 方法:
    - `kind(self) -> OutputKind`

- **class `PublicReasoningSummaryItem`**
  - Provider 明确公开的推理摘要；不表示隐藏 chain-of-thought。
  - 字段:
    - `canonical_index: int`
    - `item_id: str`
    - `summary: str`
  - 方法:
    - `kind(self) -> OutputKind`

- **class `ToolCallItem`**
  - 只有 Provider item done 后才可构造的完整工具调用。
  - 字段:
    - `canonical_index: int`
    - `item_id: str`
    - `call_id: str`
    - `name: str`
    - `arguments_json: str`
  - 方法:
    - `kind(self) -> OutputKind`
    - `arguments(self) -> dict[str, Any]` — 返回防御性解析后的参数，不暴露内部可变映射。

- **class `BlockedItem`**
  - 未知或畸形输出的安全墓碑，不保留原始正文。
  - 字段:
    - `canonical_index: int`
    - `item_id: str`
    - `blocked_kind: BlockedKind`
    - `payload_length: int`
    - `sha256: str`
  - 方法:
    - `kind(self) -> OutputKind`

- **class `InstructionMessage`**
  - 可信 system/developer 指令。
  - 字段:
    - `role: InstructionRole`
    - `content: str`

- **class `UserMessage`**
  - 带稳定输入身份的用户消息。
  - 字段:
    - `input_id: str`
    - `content: str`
    - `source_interrupt_id: str | None = None`

- **class `AssistantMessage`**
  - 完成 assistant 文本进入下一轮模型上下文的白名单包装。
  - 字段:
    - `source_provider: str`
    - `model_turn_id: UUID`
    - `item: AssistantTextItem`

- **class `ReasoningSummaryEcho`**
  - 公开推理摘要进入下一轮上下文的显式包装。
  - 字段:
    - `source_provider: str`
    - `model_turn_id: UUID`
    - `item: PublicReasoningSummaryItem`

- **class `ToolCallEcho`**
  - 完整 ToolCall 进入下一轮上下文的显式包装。
  - 字段:
    - `source_provider: str`
    - `call_ref: ModelCallRef`
    - `item: ToolCallItem`

- **class `ToolResultMessage`**
  - 按 ModelCallRef 与先前调用关联的模型可见工具结果。
  - 字段:
    - `call_ref: ModelCallRef`
    - `content: str`
    - `is_error: bool = False`

- **class `ModelRequest`**
  - Provider-neutral 请求；只接受经过白名单定义的上下文类型。
  - 字段:
    - `model_turn_id: UUID`
    - `provider: str`
    - `model: str`
    - `input_items: tuple[ModelContextItem, ...]`
    - `tool_definitions: tuple[ToolDefinition, ...] = ()`
    - `max_output_tokens: int = 4096`
    - `protocol_version: int = MODEL_PROTOCOL_VERSION`

- **class `ModelTurn`**
  - 完整 Provider response 的 canonical 完成态。
  - 字段:
    - `model_turn_id: UUID`
    - `provider: str`
    - `model: str`
    - `provider_response_id: str`
    - `output_items: tuple[OutputItem, ...]`
    - `finish_reason: FinishReason`
    - `usage: ModelUsage | None = None`
    - `protocol_version: int = MODEL_PROTOCOL_VERSION`
  - 方法:
    - `final_text(self) -> str` — 按 canonical 顺序连接该回合的 assistant 文本。

- **class `StreamHeader`**
  - 每个 canonical stream event 共享的顺序与响应身份。
  - 字段:
    - `model_turn_id: UUID`
    - `provider: str`
    - `provider_response_id: str`
    - `sequence: int`
    - `provider_sequence: int | None = None`

- **class `TurnStarted`**
  - Canonical 流的唯一首事件。
  - 字段:
    - `header: StreamHeader`
    - `model: str`

- **class `ItemStarted`**
  - 建立一个可接收 delta 的 output target。
  - 字段:
    - `header: StreamHeader`
    - `canonical_index: int`
    - `item_id: str`
    - `output_kind: OutputKind`
    - `call_id: str | None = None`
    - `tool_name: str | None = None`

- **class `ContentDelta`**
  - 文本或公开推理摘要的非权威预览分片。
  - 字段:
    - `header: StreamHeader`
    - `canonical_index: int`
    - `item_id: str`
    - `content_kind: ContentKind`
    - `delta: str`

- **class `ToolArgumentsDelta`**
  - 工具参数 JSON 的非权威预览分片。
  - 字段:
    - `header: StreamHeader`
    - `canonical_index: int`
    - `item_id: str`
    - `call_id: str`
    - `delta: str`

- **class `ItemCompleted`**
  - 关闭 target 的权威完整 OutputItem。
  - 字段:
    - `header: StreamHeader`
    - `item: OutputItem`

- **class `UsageReported`**
  - Provider 明确报告的一次 response usage。
  - 字段:
    - `header: StreamHeader`
    - `usage: ModelUsage`

- **class `TurnCompleted`**
  - 完整 response 的唯一成功 terminal。
  - 字段:
    - `header: StreamHeader`
    - `turn: ModelTurn`

- **class `StreamFailed`**
  - 无有效 ModelTurn 时的 typed failure terminal。
  - 字段:
    - `header: StreamHeader`
    - `failure_kind: StreamFailureKind`
    - `stable_code: str`
    - `retryable: bool = False`

- **class `UnknownEvent`**
  - 未知 Provider 事件的无正文墓碑；聚合器收到后立即 fail closed。
  - 字段:
    - `header: StreamHeader`
    - `type_digest: str`
    - `payload_length: int`
    - `sha256: str`


### `model/stream.py`

> 把 D2 typed model stream 严格聚合成完整 ``ModelTurn``。

- **class `StreamLimits`**
  - 限制不可信 Provider 流在内存中的占用。
  - 字段:
    - `max_events: int = 100000`
    - `max_items: int = 128`
    - `max_text_chars: int = 2000000`
    - `max_argument_chars: int = 1000000`
    - `max_total_chars: int = 4000000`

- **class `ModelStreamAssembler`**
  - 验证单条 canonical stream，并构造唯一完成态 ``ModelTurn``。
  - 方法:
    - `accept(self, event: ModelStreamEvent) -> None` — 应用一个 typed event；任何非法顺序立即 fail closed。
    - `finish(self) -> ModelTurn` — 在 EOF 处取得完成回合；没有 typed terminal 时拒绝部分结果。
    - `_require_sequence(self, header: StreamHeader) -> None`
    - `_require_identity(self, header: StreamHeader) -> None`
    - `_start_item(self, event: ItemStarted) -> None`
    - `_append_content(self, event: ContentDelta) -> None`
    - `_append_arguments(self, event: ToolArgumentsDelta) -> None`
    - `_append_fragment(self, slot: _ItemSlot, fragment: str, *, argument: bool) -> None`
    - `_open_slot(self, index: int, item_id: str) -> _ItemSlot`
    - `_complete_item(self, event: ItemCompleted) -> None`
    - `_require_completed_identity(started: ItemStarted, item: OutputItem) -> None`
    - `_complete_turn(self, event: TurnCompleted) -> None`

- **`assemble_model_stream(events: Iterable[ModelStreamEvent], *, limits: StreamLimits | None=None) -> ModelTurn`** — 消费完整 iterable 并返回唯一合法 ``ModelTurn``。

## `plan/`

> D24 W1: plan board 与 update_plan 工具核心。

### `plan/__init__.py`

> D24 W1: plan board 与 update_plan 工具核心。


### `plan/board.py`

> Plan board: bounded task-decomposition facts with an authoritative projection.

- **class `PlanError`** (RuntimeError)
  - Plan 边界的稳定错误码；不泄露内部表示。

- **class `PlanLimits`**
  - 字段:
    - `max_items: int = 32`
    - `max_text_chars: int = 400`
    - `max_total_chars: int = 8000`

- **class `PlanItem`**
  - 字段:
    - `item_id: int`
    - `text: str`
    - `status: str`

- **class `PlanBoard`**
  - 单线程会话拥有的有界计划状态；变更经可选 write-ahead 钩子后提交。
  - 方法:
    - `limits(self) -> PlanLimits`
    - `snapshot(self) -> tuple[PlanItem, ...]`
    - `replace(self, texts: Sequence[str], statuses: Sequence[str]) -> tuple[PlanItem, ...]` — 全量替换（TodoWrite 语义）：每次调用给出完整计划状态。
    - `set_status(self, item_id: int, status: str) -> tuple[PlanItem, ...]`
    - `authoritative_projection(self) -> str` — 确定性文本投影；头部显式声明无授权语义（治理锚点）。
    - `_commit(self, items: tuple[PlanItem, ...]) -> None`
    - `bind_journal(self, hook: Callable[[tuple[PlanItem, ...]], None]) -> None` — 绑定持久化 write-ahead 钩子；只允许绑定一次。
    - `restore(self, items: Sequence[PlanItem]) -> None` — 直接重放持久化状态（不触发钩子）；条目已由 PlanItem 校验。


- 常量(字符串):
  - `_PROJECTION_HEADER` = `Authoritative plan (facts only; confers no authority):`
  - `_PROJECTION_EMPTY` = `Authoritative plan: (none)`

### `plan/durable.py`

> Durable plan journal: ``memory.plan-updated.v1`` on a thread-keyed stream.

- **class `PlanDurableJournal`**
  - 把 PlanBoard 的 on_change 钩子接到线程键控的持久事件流。
  - 方法:
    - `stream(self) -> StreamId`
    - `_events(self)`
    - `current_version(self) -> int`
    - `append(self, items: Sequence[PlanItem]) -> None`
    - `load(self) -> tuple[PlanItem, ...]`


- 常量(字符串):
  - `PLAN_EVENT_TYPE` = `memory.plan-updated.v1`

### `plan/tools.py`

> update_plan 工具：全量替换权威计划（D24 W1）。

- **class `UpdatePlanArguments`**
  - 字段:
    - `texts: tuple[str, ...]`
    - `statuses: tuple[str, ...]`

- **`plan_tool_spec(limits: PlanLimits | None=None) -> ToolSpec[UpdatePlanArguments]`** — 从同一份硬限额生成 update_plan 的 definition 与 decoder。
- **class `PlanToolRegistry`** (ToolRegistry)
  - 持有 PlanBoard 的最小 registry；注册后即可被 sealed 装配复用。
  - 方法:
    - `_update_plan(self, arguments: UpdatePlanArguments, *, context: ToolExecutionContext) -> ToolExecutionResult`

- **`update_plan_handler(board: PlanBoard, arguments: UpdatePlanArguments) -> ToolExecutionResult`**
- **`register_plan_tool(registry: ToolRegistry, board: PlanBoard) -> None`** — 把 update_plan 并入既有 sealed 装配（如 verified coding registry）。

## `recovery/`

> D6 durable execution, checkpoint v2, lease, and recovery boundary.

### `recovery/__init__.py`

> D6 durable execution, checkpoint v2, lease, and recovery boundary.


### `recovery/context.py`

> Rebuild canonical execution projection solely from typed run-execution facts.

- **class `ReconstructionError`** (RuntimeError)
  - Content-free corruption failure; carries no user text.

- **class `ExecutionProjection`**
  - The canonical projection of one execution fact stream.
  - 字段:
    - `context: tuple[Mapping[str, Any], ...]`
    - `model_round: int`
    - `tool_count: int`
    - `output_chars: int`
    - `input_tokens: int`
    - `output_tokens: int`
    - `phase: RunPhase`
    - `execution_version: int`
    - `last_run_id: UUID`
    - `pending_tool_calls: tuple[Mapping[str, Any], ...] = ()`
    - `final_text: str | None = None`

- **`projection_document(projection: ExecutionProjection) -> dict[str, Any]`** — The exact canonical 10-key projection document (section 7.4).
- **`projection_digest(projection: ExecutionProjection) -> str`** — lower_hex(SHA256(canonical_json_bytes_v1(projection_document))).
- **`delivery_content_digest_text(delivered: str) -> str`** — SPEC-2 补-3: SHA-256 over the delivered string's UTF-8 bytes.
- **`pending_delivery_calls(events: Sequence[StoredEvent]) -> dict`** — Scan a run-execution segment for delivery state (plan B, SPEC-3).
- **`reduce_execution(events: Sequence[StoredEvent], initial: ExecutionProjection | None=None) -> ExecutionProjection`** — Reduce run-execution facts into the canonical projection.

- 常量(字符串):
  - `SEED_EVENT_TYPE` = `run.context-seeded.v2`
  - `LEGACY_SEED_EVENT_TYPE` = `run.context-seeded.v1`
  - `_MODEL_TURN_EVENT` = `model.turn-completed.v1`
  - `_TOOL_RESULT_EVENT` = `tool.result-recorded.v1`
  - `_DELIVERY_DECIDED_EVENT` = `result.delivery-decided.v1`
  - `_DELIVERY_TEST_TOOL` = `run_test_profile`
  - `_PHASE_ADVANCE_EVENT` = `run.phase-advanced.v1`
  - `COMPACTION_INTENDED_EVENT` = `run.context-compaction-intended.v1`
  - `COMPACTION_COMPACTED_EVENT` = `run.context-compacted.v1`

### `recovery/coordinator.py`

> Discovery and safe stale-run takeover orchestration (section 7.6).

- **class `AutomaticRecoveryBlocked`** (RuntimeError)
  - Recovery requires a D7 ledger or an operator decision.

- **class `ToolRecoveryPort`** (Protocol)
  - 方法:
    - `reconcile_pending(self, turn_id: UUID, pending_tool_calls: Sequence[Mapping[str, Any]]) -> bool`

- **class `RecoveryClaim`**
  - 字段:
    - `turn: object`
    - `lease: object | None`
    - `context: ExecutionProjection`

- **class `RecoveryCoordinator`**
  - 方法:
    - `list_recoverable_turns(self) -> tuple[RecoverableTurn, ...]`
    - `reconstruct(self, item: RecoverableTurn) -> ExecutionProjection` — Rebuild the canonical projection, using the cache only when every
    - `_valid_cache(self, checkpoint, item: RecoverableTurn, events, full) -> bool`
    - `claim_stale(self, item: RecoverableTurn, *, force: bool=False) -> RecoveryClaim`
    - `_live_recovery_claim(self, turn_id: UUID) -> bool`
    - `_read_events(self, stream: StreamId, after_version: int) -> tuple`


### `recovery/delivery.py`

> Plan B recovery: delivery-decision backfill (spec v4, SPEC-3).

- **class `DeliveryRecoveryError`** (Exception)
  - Base for the three explicitly separated delivery recovery states.

- **class `DeliveryRecoveryPaused`** (DeliveryRecoveryError)
  - delivery_paused - backfill could not verify; nothing persisted.

- **class `DeliveryProtocolMismatch`** (DeliveryRecoveryError)
  - protocol_version_mismatch - facts predate the delivery protocol.

- **class `DeliveryLogCorruption`** (DeliveryRecoveryError)
  - log_corruption - durable conflicting decisions / broken identity.

- **`backfill_delivery_decisions(store, *, turn_id: UUID, thread_id: UUID, run_id, events: Sequence, turn_fence: tuple[int, str, Mapping | None] | None=None) -> dict`** — Backfill missing first-delivery decisions for one turn's facts.

### `recovery/execution.py`

> Typed execution facts, v2 execution seed DTO and durable recorder.

- **`context_document(item: ModelContextItem) -> dict[str, Any]`** — Serialize only the durable, redacted model-context contract.
- **`context_from_document(d: Mapping[str, Any]) -> ModelContextItem`**
- **`model_turn_document(turn: ModelTurn) -> dict[str, Any]`**
- **`tool_definition_document(tool: ToolDefinition) -> dict[str, Any]`** — Exact per-tool document for the seed request semantics.
- **`tool_catalog_digest(definitions: Sequence[ToolDefinition]) -> str`** — lower_hex(SHA256(canonical bytes of the sorted definition documents)).
- **class `ExecutionSeedDTO`**
  - The immutable v2 seed exchanged with ThreadRuntime.start_turn.
  - 字段:
    - `request_semantics: Mapping[str, Any]`
    - `projection: Mapping[str, Any]`
    - `resume: Mapping[str, Any] | None`
    - `thread_id: UUID | None = None`
    - `turn_id: UUID | None = None`
    - `run_id: UUID | None = None`
    - `attempt: int | None = None`
    - `turn_stream_version: int | None = None`
  - 方法:
    - `with_identity(self, *, thread_id: UUID, turn_id: UUID, run_id: UUID, attempt: int, turn_stream_version: int) -> 'ExecutionSeedDTO'`
    - `to_document_partial(self) -> dict[str, Any]` — Wire minus the runtime-owned identity fields (for fingerprints).
    - `to_document(self) -> dict[str, Any]`

- **`resume_document(turn_event_id: UUID, turn_event_type: str, context_item: Mapping[str, Any]) -> dict[str, Any]`** — Build the seed resume block for one typed Turn resume event.
- **`execution_seed(initial_context: Sequence[ModelContextItem], *, model_round: int=0, tool_count: int=0, output_chars: int=0, input_tokens: int=0, output_tokens: int=0, phase: RunPhase=RunPhase.READY_FOR_MODEL, pending_calls: Sequence[Mapping[str, Any]]=(), final_text: str | None=None, provider: str | None=None, model: str | None=None, max_output_tokens: int | None=None, tool_definitions: Sequence[ToolDefinition]=(), resume: Mapping[str, Any] | None=None) -> ExecutionSeedDTO`** — Build the full v2 seed: canonical context, pinned semantics, catalog.
- **`resolve_seed_semantics(events: Sequence[Any]) -> dict[str, Any]`** — Recover request semantics pinned by the latest v2 seed in the facts.
- **`validate_execution_segments(events: Sequence[Any]) -> None`** — Verify per-run segment integrity of the execution fact stream.
- **`live_projection_from_state(*, context: Sequence[Mapping[str, Any]], model_round: int, tool_count: int, output_chars: int, input_tokens: int, output_tokens: int, phase: RunPhase, pending_calls: Sequence[Mapping[str, Any]], final_text: str | None, execution_version: int, last_run_id: UUID) -> ExecutionProjection`**
- **class `DurableExecutionRecorder`**
  - Appends canonical facts with a live Turn fence and a verified cache.
  - 方法:
    - `_append_seed(self, document: Mapping[str, Any]) -> None`
    - `model_completed(self, turn: ModelTurn, projected: Sequence[ModelContextItem], model_round: int, output_chars: int, has_tools: bool) -> None`
    - `tool_started(self, call_id: str, tool_name: str) -> None`
    - `tool_completed(self, result: ToolResultMessage, tool_count: int) -> None`
    - `delivery_decided(self, result_message, decision: dict) -> None` — Plan B: persist the FIRST delivery decision for one test call.
    - `_placeholder_view(self, doc: dict) -> dict` — Model-facing placeholder view of one test-receipt fact.
    - `compact(self, *, epoch: int, source_first_version: int, source_last_version: int, replacement: Mapping[str, Any], resulting_context_digest: str, target_chars: int, summary_receipt_digest: str | None=None, source_event_ids_digest: str | None=None) -> None` — D23 §5.7: record an in-run compaction (intended + compacted).
    - `source_versions_for(self, first: int, last: int) -> tuple[int, int]` — Map projection indices to the recorder's own stream versions.
    - `synced_context(self)` — The authoritative projection context as ModelContextItems.
    - `_range_identity_digest(self, source_first_version: int, source_last_version: int) -> str` — Identity digest of the source-range context items (D23 §5.7).
    - `_range_resulting_digest(self, source_first_version: int, source_last_version: int, replacement: Mapping[str, Any]) -> str` — Resulting-context digest after replacing the source range (D23 §5.7).
    - `_stream_head_version(self, stream: StreamId) -> int`
    - `_append_fenced(self, stream: StreamId, expected_version: int, events: tuple[NewEvent, ...], command_id: UUID) -> None`
    - `_append_typed(self, event_type: str, payload: Mapping[str, Any]) -> None`
    - `_turn_head_version(self) -> int` — Current Turn stream head version (fence for typed-heartbeat turns).
    - `_turn_head_fence(self)` — Live head (version, event_type, run_id) of the Turn stream.


- 常量(字符串):
  - `SEED_EVENT_TYPE` = `run.context-seeded.v2`
  - `LEGACY_SEED_EVENT_TYPE` = `run.context-seeded.v1`
  - `_MODEL_TURN_EVENT` = `model.turn-completed.v1`
  - `_TOOL_RESULT_EVENT` = `tool.result-recorded.v1`
  - `_PHASE_ADVANCE_EVENT` = `run.phase-advanced.v1`
  - `_TEST_TOOL_NAME` = `run_test_profile`

### `recovery/protocol.py`

> D6 checkpoint v2 wire contract (section 7.4).

- **class `RunPhase`** (StrEnum)

- **class `CheckpointError`** (RuntimeError)
  - Content-free checkpoint failure carrying a stable code.

- **`stored_event_hash_v2_from_document(document: Mapping[str, Any]) -> str`** — SHA-256 (lower hex) of the canonical stored-event v2 document.
- **`stored_event_hash_v2(event: StoredEvent) -> str`** — Build the exact stored-event v2 document for a committed event.
- **`checkpoint_identity_document(*, reducer_name: str, reducer_version: int, source_category: str, source_aggregate_id: UUID, covered_stream_version: int, covered_event_id: UUID, covered_global_position: int, covered_commit_id: UUID, covered_event_hash: str, projection_digest: str) -> dict[str, Any]`** — Exact checkpoint identity document (section 7.4).
- **`checkpoint_id_for_identity(identity: Mapping[str, Any]) -> UUID`** — checkpoint_id = UUIDv5(CHECKPOINT_ID_NAMESPACE, canonical identity bytes).
- **class `Checkpoint`**
  - Validated v2 checkpoint: reducer + source + fence + projection digest.
  - 字段:
    - `checkpoint_id: UUID`
    - `reducer_name: str`
    - `reducer_version: int`
    - `source_category: str`
    - `source_aggregate_id: UUID`
    - `covered_stream_version: int`
    - `covered_event_id: UUID`
    - `covered_global_position: int`
    - `covered_commit_id: UUID`
    - `covered_event_hash: str`
    - `thread_id: UUID`
    - `turn_id: UUID`
    - `run_id: UUID`
    - `turn_stream_version: int`
    - `projection: Mapping[str, Any]`
    - `projection_digest: str`
    - `previous: None`
    - `created_at: datetime`
  - 方法:
    - `build(cls, *, reducer_name: str='run-execution', reducer_version: int=2, source_category: str, source_aggregate_id: UUID, covered_stream_version: int, covered_event_id: UUID, covered_global_position: int, covered_commit_id: UUID, covered_event_hash: str, thread_id: UUID, turn_id: UUID, run_id: UUID, turn_stream_version: int, projection: Mapping[str, Any], projection_digest: str, created_at: datetime) -> 'Checkpoint'`
    - `document(self) -> dict[str, Any]` — Exact canonical wire document of this checkpoint.
    - `wire_bytes(self) -> bytes` — Canonical UTF-8 bytes of the wire document for the cache BLOB.
    - `parse(cls, value: bytes | str | bytearray) -> 'Checkpoint'` — Strict bounded parse + exact key set + identity re-computation.
    - `from_document(cls, document: Mapping[str, Any]) -> 'Checkpoint'`


- 常量(字符串):
  - `REDUCER_NAME` = `run-execution`

### `recovery/redaction.py`

> Deterministic redaction for D6 persisted model context and tool data.

- **`redact_text(value: str) -> str`** — Remove common credential shapes while preserving useful surrounding text.
- **`redact_json_value(value: Any, *, key: str | None=None) -> Any`** — Recursively redact sensitive fields and strings from a JSON-compatible value.
- **`redact_arguments_json(value: str) -> str`** — Redact a tool arguments document without changing whether it is valid JSON.

- 常量(字符串):
  - `REDACTED` = `[REDACTED]`

### `recovery/store.py`

> Recovery projection port and protocol-only CheckpointStore.

- **class `LeaseConflict`** (RuntimeError)
  - Recovery fencing failure: lease missing, expired or ownership lost.

- **class `RunLease`**
  - 字段:
    - `turn_id: UUID`
    - `run_id: UUID`
    - `owner_id: str`
    - `generation: int`
    - `version: int`
    - `expires_at: str`

- **class `CheckpointCacheRecord`**
  - 字段:
    - `turn_id: UUID`
    - `cache_version: int`
    - `checkpoint_id: UUID`
    - `run_id: UUID`
    - `turn_version: int`
    - `execution_version: int`
    - `reducer_name: str`
    - `reducer_version: int`
    - `source_event_id: UUID`
    - `source_global_position: int`
    - `projection_digest: str`
    - `checkpoint_json: bytes`
    - `updated_at: datetime`

- **class `RecoverableTurn`**
  - 字段:
    - `turn_id: UUID`
    - `turn_version: int`
    - `run_id: UUID`
    - `lease_expires_at: datetime`

- **class `CacheReceipt`**
  - 字段:
    - `turn_id: UUID`
    - `cache_version: int`
    - `checkpoint_id: UUID`
    - `changed: bool`

- **class `RecoveryEventSource`** (Protocol)
  - The minimal read surface CheckpointStore needs from an event store.
  - 方法:
    - `read_stream(self, stream_id: StreamId, *, after_version: int=-1, limit: int=500) -> tuple[StoredEvent, ...]`

- **class `RecoveryProjectionPort`** (Protocol)
  - Control-layer projection adapter; mutators are forbidden on the port.
  - 方法:
    - `database_time(self) -> datetime`
    - `publish_checkpoint_cache(self, record: CheckpointCacheRecord, *, expected_cache_version: int) -> CacheReceipt`
    - `load_checkpoint_cache(self, turn_id: UUID) -> CheckpointCacheRecord | None`
    - `list_recoverable(self, *, expired_before: datetime, after_turn_id: UUID | None, limit: int) -> tuple[RecoverableTurn, ...]`
    - `get_active_lease(self, turn_id: UUID, run_id: UUID, owner_id: str) -> RunLease | None`

- **class `CheckpointStore`**
  - Protocol-only recovery facade.
  - 方法:
    - `database_time(self) -> datetime`
    - `publish_from_source(self, *, thread_id: UUID, turn_id: UUID, run_id: UUID, turn_version: int, source_event: StoredEvent, projection: ExecutionProjection) -> CacheReceipt`
    - `load(self, turn_id: UUID) -> Checkpoint | None`
    - `list_recoverable(self, *, expired_before: datetime | None=None, after_turn_id: UUID | None=None, limit: int=1000) -> tuple[RecoverableTurn, ...]`
    - `list_recoverable_turns(self) -> tuple[RecoverableTurn, ...]` — Compatibility alias for pre-I5 callers.
    - `get_active_lease(self, turn_id: UUID, run_id: UUID, owner_id: str) -> RunLease`
    - `_read_covered(self, source_event: StoredEvent) -> tuple[StoredEvent, ...]`

- **class `LeaseKeeper`**
  - Renews one exact run lease while a Worker may block.
  - 方法:
    - `start(self) -> None`
    - `_run(self) -> None`
    - `assert_owned(self) -> None`
    - `stop(self) -> None`


## `retrieval/`

> Bounded-result retrieval (hardening 2026-09-19): metadata-only history

### `retrieval/__init__.py`

> Bounded-result retrieval (hardening 2026-09-19): metadata-only history


### `retrieval/projection.py`

> Hardening 2026-09-19 (WP-D): durable metadata-only result projections.

- **`canonical_text(document: Any) -> str`** — Canonical JSON text of a document (single wire rule for deliveries).
- **`source_digest(receipt: Any) -> str`** — source digest - binds the SOURCE execution receipt (SPEC-2).
- **`delivery_content_digest(delivered_text: str) -> str`** — delivery digest - SHA-256 over the delivered string's UTF-8 bytes
- **`projection_payload_digest(payload: Mapping) -> str`** — immutable projection digest - binds one stored projection payload.
- **`test_diagnostics(receipt: Mapping[str, Any]) -> dict`** — Metadata-only diagnostics copied from a test receipt.
- **`unavailable_placeholder(*, turn_id, model_turn_id, call_id: str, source_sha256: str) -> dict`** — The fixed projection_unavailable placeholder (spec v4, verbatim).
- **`build_published_payload(*, source_kind: str, call_id: str, diagnostics: dict, body_ref: dict) -> dict`** — The exact stored payload of a published projection event (the
- **class `TestFactScan`**
  - Trusted test facts of one turn plus the untrusted/unverified counts.
  - 字段:
    - `facts: tuple[dict, ...]`
    - `untrusted: int`
    - `unverified: int`

- **class `ResultProjectionStore`**
  - Publish and read metadata-only result projections (WP-D).
  - 方法:
    - `stream(self, turn_id: UUID) -> StreamId`
    - `_head_version(self, turn_id: UUID) -> int`
    - `_append_guarded(self, *, turn_id: UUID, thread_id: UUID, run_id, event: NewEvent, command: UUID, fingerprint: str) -> None` — Append one projection event at the current head, retrying CAS.
    - `_fingerprint(payload: dict) -> str`
    - `_append_event(self, *, turn_id: UUID, thread_id: UUID, run_id, event_type: str, payload: dict, identity: str) -> str`
    - `revoke(self, *, turn_id: UUID, thread_id: UUID, run_id, call_id: str, model_turn_id, reason: str) -> str` — Withdraw one fact's projection (design event family member).
    - `expire(self, *, turn_id: UUID, thread_id: UUID, run_id, call_id: str, model_turn_id, expires_at: datetime) -> str` — Schedule one fact's read expiry (design event family member).
    - `publish(self, *, turn_id: UUID, thread_id: UUID, run_id: UUID, call_id: str, source_kind: str, diagnostics: dict, body_ref: dict) -> str` — Append one projection event; idempotent per identity inputs.
    - `register_publication_failure(self, *, turn_id: UUID, thread_id: UUID, run_id, call_id: str | None, body_ref: dict | None, error_code: str) -> str` — Durable pending marker for a fact whose publication failed.
    - `read(self, turn_id: UUID) -> list[dict]` — All projection payloads of one turn, page-walked in order (R2).

- **`read_publication_status(store, turn_id: UUID) -> dict`** — Rebuild one turn's publication status from durable events.
- **`scan_test_results(store, turn_id: UUID) -> TestFactScan`** — Extract trusted test-result facts from one turn's run-execution stream.
- **`build_test_fact_payload(fact) -> dict`** — The exact published-payload document for one trusted test fact.
- **`publish_test_fact(store, *, turn_id, thread_id, run_id, fact) -> str`** — Single publish path for one trusted test fact (R2/plan B).
- **`lookup_projection(store, turn_id: UUID, call_id: str, model_turn_id: str | None=None, *, now: datetime | None=None) -> dict`** — Read one call's published projection by reference (R2 slice b).
- **`delivery_command_id(turn_id, model_turn_id, call_id: str) -> UUID`** — SPEC-1: the delivery key NEVER carries a content dimension.
- **`build_delivery_payload(*, call_id: str, model_turn_id, delivery: str, source_sha256: str, delivered_content: str, projection_ref: dict | None=None, error_code: str | None=None) -> dict`** — Delivery payload = SPEC-1 fingerprint fields + SPEC-2 digests + the
- **`decide_delivery(store, *, turn_id: UUID, thread_id: UUID, run_id, call_id: str, model_turn_id, payload: dict, turn_fence: tuple[int, str, dict | None] | None=None) -> str`** — Append one ``result.delivery-decided.v1`` on the run-execution stream.
- **`make_test_receipt_publisher(store, ledger, runtime) -> Callable`** — Build the in-loop publication + delivery hook (plan B).
- **`encode_search_cursor(turn_index: int, stream_version: int) -> str`** — Opaque pagination cursor: base64 of its canonical JSON document.
- **`decode_search_cursor(cursor: str) -> dict`**
- **`search_projections(store, *, thread_id: UUID, profile_id: str | None=None, cursor: str | None=None, contract_resolver=None) -> dict`** — N1 (WP-E): search published projections scoped to the CURRENT thread.

- 常量(字符串):
  - `PROJECTION_PUBLISHED_EVENT` = `result.projection-published.v1`
  - `PROJECTION_UNAVAILABLE_EVENT` = `result.projection-unavailable.v1`
  - `PROJECTION_REVOKED_EVENT` = `result.projection-revoked.v1`
  - `PROJECTION_EXPIRED_EVENT` = `result.projection-expired.v1`
  - `DELIVERY_DECIDED_EVENT` = `result.delivery-decided.v1`
  - `PROJECTION_STREAM_CATEGORY` = `result-projection`
  - `TEST_SOURCE_KIND` = `test`
  - `TEST_TOOL_NAME` = `run_test_profile`
  - `DELIVERY_RECEIPT` = `receipt`
  - `DELIVERY_UNAVAILABLE` = `projection_unavailable`
  - `PLACEHOLDER_MESSAGE` = `本次调用的可读结果暂不可用。此状态不表示工具执行失败，也不表示未执行。不要仅因结果不可用重试原工具；请使用结果引用查询，或等待恢复处理。`

### `retrieval/recall_tool.py`

> Hardening 2026-09-19 (WP-E slice): model-facing history recall tool.

- **class `RecallQueryArguments`**
  - 字段:
    - `query: str`

- **`recall_tool_spec() -> ToolSpec[RecallQueryArguments]`**
- **`register_recall_tool(registry: ToolRegistry, *, store, runtime, memory_factory: Callable[[], Any] | None=None) -> None`** — Register ``recall_history`` on a not-yet-sealed registry.

- 常量(字符串):
  - `CONTENT_RELEASE_STATE` = `unavailable_without_release_rule`

### `retrieval/result_read_tool.py`

> Hardening 2026-09-19 (WP-D/E slice, R2 closure): model-facing read of

- **class `ResultReadArguments`**
  - 字段:
    - `turn_id: str`
    - `call_id: str`
    - `model_turn_id: str = ''`

- **class `ResultSearchArguments`**
  - 字段:
    - `profile_id: str = ''`
    - `cursor: str = ''`

- **`result_search_tool_spec() -> ToolSpec[ResultSearchArguments]`**
- **`result_read_tool_spec() -> ToolSpec[ResultReadArguments]`**
- **`register_result_read_tool(registry: ToolRegistry, *, store, runtime, contract_resolver=None) -> None`** — Register both read tools on a not-yet-sealed registry.

## `runtime/`

> D15 unified runtime, real P0 runtime, and CLI.

### `runtime/__init__.py`

> D15 unified runtime, real P0 runtime, and CLI.


### `runtime/app.py`

> P0 application layer: real run/resume/status/cancel/doctor commands.

- **class `CommandOutcome`**
  - 字段:
    - `ok: bool`
    - `code: str`
    - `payload: Mapping[str, Any]`
  - 方法:
    - `to_json(self) -> str`

- **class `AppRuntime`**
  - 方法:
    - `_ensure_execution_plane(self) -> AssembledRuntime`
    - `from_config_file(cls, path: str | Path, *, repo_override: str | Path | None=None, model_client: object | None=None, api_key: str | None=None, reasoning_sink: Callable[[str], None] | None=None, launcher_builder=None) -> 'AppRuntime'`
    - `close(self) -> None`
    - `run(self, task: str, *, event_sink: EventSink | None=None) -> CommandOutcome`
    - `chat(self, message: str, *, thread_id: str | UUID | None=None, history: SessionHistory | None=None, event_sink: EventSink | None=None) -> CommandOutcome`
    - `resume(self, turn_id: str | UUID, *, event_sink: EventSink | None=None) -> CommandOutcome`
    - `_backfill_terminal_delivery(self, turn) -> dict` — Best-effort delivery backfill for a terminal turn (recovery
    - `cancel(self, turn_id: str | UUID) -> CommandOutcome`
    - `status(self) -> CommandOutcome`
    - `_turn_document_with_projections(self, turn_id: UUID) -> dict`
    - `_durable_projection_status(self, turn_id: UUID) -> dict` — Durable, restart-safe projection status (WP-D/R1 residual).
    - `pending_approvals(self) -> CommandOutcome`
    - `_pending_approval_documents(self) -> list[dict[str, Any]]`
    - `resolve_approval(self, request_id: str | UUID, approved: bool, *, expected_version: int | None=None, resume_after: bool=True) -> CommandOutcome`
    - `_pending_approval_records(self) -> tuple[ApprovalRecord, ...]`
    - `doctor(self) -> CommandOutcome`
    - `_execute(self, turn_id: UUID, version: int, event_sink: EventSink | None) -> TurnWorkerResult`
    - `_publish_result_projections(self, turn) -> dict` — Hardening WP-D/R1: publish metadata-only projections for the run's
    - `_register_projection_failure(self, turn, *, projection_store, call_id, body_ref, error_code: str, status: dict) -> None`
    - `_record_turn_conclusion(self, turn) -> None` — Audit F13: persist a TurnConclusion for every terminal Turn.
    - `_truth_outcome(self, result: TurnWorkerResult) -> CommandOutcome`
    - `_claim_stale(self, turn_id: UUID)`


### `runtime/assembly.py`

> P0 runtime assembly: one real model/tool/policy/ledger/recovery chain.

- **class `RuntimeAssemblyError`** (RuntimeError)
  - Stable, content-free assembly failure for operator-facing CLI output.

- **class `AssembledRuntime`**
  - 字段:
    - `config: RuntimeConfig`
    - `store: SqliteEventStore`
    - `runtime: ThreadRuntime`
    - `ledger: ToolLedgerStore`
    - `approvals: ApprovalService`
    - `trace: TraceStore`
    - `trace_sink: TraceSink`
    - `registry: object`
    - `executor: LedgerExecutor`
    - `client: object`
    - `worker: TurnWorker`
    - `checkpoint_store: CheckpointStore`
    - `correlation_id: object`
    - `loop: AgentLoop`
    - `mcp_sessions: tuple[tuple[McpServerConfig, McpSession, McpCatalog], ...] = ()`
    - `plan_board: object | None = None`
    - `_closed: bool = field(default=False, init=False, repr=False, compare=False)`
  - 方法:
    - `build_worker(self, initial_context: Sequence[ModelContextItem]=(), *, task_mode: bool=True, claim_gate: bool=False) -> TurnWorker` — Build a TurnWorker over the same loop, optionally seeded with history.
    - `close(self) -> None` — Idempotent teardown in reverse assembly order (doc 3.6).

- **class `ControlPlaneRuntime`**
  - I6 §8.9: control-plane resources only; never spawns MCP processes.
  - 字段:
    - `config: RuntimeConfig`
    - `config_base_dir: Path`
    - `store: SqliteEventStore`
    - `runtime: ThreadRuntime`
    - `ledger: ToolLedgerStore`
    - `approvals: ApprovalService`
    - `trace: TraceStore`
    - `trace_sink: TraceSink`
    - `checkpoint_store: CheckpointStore`
    - `correlation_id: object`
    - `activation: ActivationService`
    - `staging_root: Path`
    - `git: GitFacade`
    - `_execution: object | None = field(default=None, init=False, repr=False, compare=False)`
    - `_closed: bool = field(default=False, init=False, repr=False, compare=False)`
  - 方法:
    - `attach_execution(self, execution: object) -> None`
    - `client(self)`
    - `loop(self)`
    - `worker(self)`
    - `executor(self)`
    - `mcp_sessions(self) -> tuple`
    - `build_worker(self, initial_context: Sequence[ModelContextItem]=(), *, task_mode: bool=True, claim_gate: bool=False) -> TurnWorker`
    - `close(self) -> None` — Close the execution plane (if any) and the control staging dir.

- **class `GrantedExecutionPlan`**
  - Preflight outcome: every activated server has an effective grant.
  - 字段:
    - `activated_plans: Mapping[str, StagedLaunchPlan]`
    - `activated_views: Mapping[str, ActivationView]`
    - `legacy_servers: tuple[McpServerConfig, ...] = ()`

- **class `ActivationPending`**
  - Preflight outcome: durable mcp_process_start ASKs exist; no Turn yet.
  - 字段:
    - `requests: Mapping[str, ActivationView]`
  - 方法:
    - `request_ids(self) -> tuple[str, ...]`

- **`assemble_control_plane(config: RuntimeConfig, *, config_base_dir: Path | None=None) -> ControlPlaneRuntime`** — Control plane only: no model client, no MCP process spawn (I6 §8.9).
- **`preflight_execution_activation(control: ControlPlaneRuntime, *, command_context: str) -> GrantedExecutionPlan | ActivationPending`** — Resolve activated MCP launch identity + policy (I6 §8.5).
- **`assemble_execution_plane(control: ControlPlaneRuntime, granted_plan: GrantedExecutionPlan, *, model_client: object | None=None, api_key: str | None=None, reasoning_sink: Callable[[str], None] | None=None, launcher_builder=None, post_build_registrars: Callable | tuple=()) -> AssembledRuntime`** — Build the lazy execution plane: verified registry + MCP + client.
- **`assemble_runtime(config: RuntimeConfig, *, model_client: object | None=None, api_key: str | None=None, reasoning_sink: Callable[[str], None] | None=None, config_base_dir: Path | None=None) -> AssembledRuntime`** — Eager composition for deterministic tests and legacy callers.

### `runtime/claim_gate.py`

> D22 F1: 交互完成门（防无工具幻觉完成）。

- **`claims_workspace_change(text: str) -> bool`** — 最终文本是否包含写入声明（白名单词）。
- **`claim_gate_allows(final_text: str, successful_write_tools: frozenset[str], *, write_tools: frozenset[str]=_WRITE_TOOLS) -> bool`** — False 当回合声称改文件却没有任何成功写工具。

### `runtime/cli.py`

> D15 minimal durable CLI: run / resume / status / cancel / doctor.

- **class `PatchArguments`**
  - 字段:
    - `path: str`
    - `content: str`

- **class `FakeProvider`**
  - Deterministic provider: one patch call, then a final answer.
  - 方法:
    - `stream(self, request: ModelRequest)`

- **`run_command(db: Path, repo: Path) -> dict`**
- **`resume_command(db: Path, turn_id: str | UUID, repo: Path) -> dict`**
- **`cancel_command(db: Path, turn_id: str | UUID) -> dict`**
- **`status_command(db: Path) -> dict`**
- **`doctor_command(db: Path) -> dict`**
- **`main(argv: list[str] | None=None) -> int`** — Deterministic legacy entry plus the P0 configured real entry.

### `runtime/composite_registry.py`

> Composite prepared tool registry for built-in + MCP delegates.

- **class `ToolCatalogSnapshot`**
  - I6 §8.8 frozen catalog consumed atomically by one model round.
  - 字段:
    - `catalog_epoch_id: Any`
    - `definitions: tuple[ToolDefinition, ...]`
    - `profiles: Mapping[str, Any]`
    - `resolvers: Mapping[str, Any]`
    - `semantic_bindings: Mapping[str, Any]`
    - `delegates: tuple[Any, ...]`
  - 方法:
    - `tool_names(self) -> frozenset[str]`
    - `prepare(self, call: ToolCallItem) -> _PreparedCall`

- **class `CompositeToolRegistry`**
  - A read-only composition of already-built prepared registries.
  - 方法:
    - `definitions(self) -> tuple[ToolDefinition, ...]`
    - `bind_policy_authority(self, authority: object) -> None`
    - `prepare(self, call: ToolCallItem) -> _PreparedCall`
    - `invoke_prepared(self, prepared: _PreparedCall, *, context: ToolExecutionContext, authority: object) -> ToolExecutionResult`
    - `discard_prepared(self, prepared: _PreparedCall, *, authority: object) -> bool`
    - `execute(self, call: ToolCallItem, *, context: ToolExecutionContext) -> ToolExecutionResult`
    - `binding_digest(self, tool_name: str) -> str | None`
    - `assert_complete(self, run_id: object) -> None` — D5 CompletionGate facade (§8.9 E-stage gap).
    - `current_snapshot(self, *, profiles: Mapping[str, object] | None=None, resolvers: Mapping[str, object] | None=None) -> ToolCatalogSnapshot` — One frozen snapshot over CURRENT delegate maps (I6 §8.8).


### `runtime/config.py`

> P0 runtime configuration: strict JSON config for the real model runtime.

- **class `RuntimeConfigError`** (RuntimeError)
  - Stable, content-free configuration failure safe to print to an operator.

- **class `SandboxRunner`** (StrEnum)

- **class `McpExecutionProfile`** (StrEnum)

- **class `McpCodeArtifact`**
  - One declared code-bearing argv entry (role + position).
  - 字段:
    - `role: str`
    - `argv_index: int`

- **class `McpResourceLimits`**
  - I6 §8.3 declarative resource envelope for one MCP process tree.
  - 字段:
    - `cpus: float = 1.0`
    - `memory_bytes: int = 512 * 1024 * 1024`
    - `pids: int = 256`
    - `tmpfs_bytes: int = 64 * 1024 * 1024`
    - `process_count: int = 1`
  - 方法:
    - `to_document(self) -> dict[str, object]`

- **class `RepositoryTrustMode`** (StrEnum)

- **class `ProviderConfig`**
  - 字段:
    - `base_url: str`
    - `api_key_env: str`
    - `model: str`
    - `provider: str = 'siliconflow'`
    - `timeout_seconds: float = 120.0`
    - `max_stream_seconds: float = 600.0`
    - `max_output_tokens: int = 4096`
    - `max_request_bytes: int = 2 * 1024 * 1024`
    - `max_response_bytes: int = 16 * 1024 * 1024`
    - `max_sse_event_bytes: int = 2 * 1024 * 1024`
    - `provider_options: tuple[tuple[str, object], ...] = ()`
    - `reasoning_effort: str | None = None`

- **class `TestProfileConfig`**
  - 字段:
    - `profile_id: str`
    - `argv: tuple[str, ...]`
    - `timeout_seconds: float = 120.0`
    - `max_stdout_bytes: int = 256000`
    - `max_stderr_bytes: int = 256000`
    - `environment: tuple[tuple[str, str], ...] = ()`
    - `release_fields: tuple[str, ...] | None = None`
    - `sensitive: bool = False`
    - `profile_version: str = ''`
    - `audience_policy_ref: str | None = None`
    - `input_policy_ref: str | None = None`
    - `candidate_admission_ref: str | None = None`
    - `environment_ref: str | None = None`
    - `release_mode: str | None = None`
    - `producer_rules: tuple[str, ...] = ()`
    - `scratch_policy_ref: str | None = None`
    - `budget_policy_ref: str | None = None`
    - `workspace_manifest: tuple[str, ...] = ()`

- **class `McpServerConfig`**
  - 字段:
    - `server_id: str`
    - `command: tuple[str, ...]`
    - `cwd: Path | None = None`
    - `environment: tuple[tuple[str, str], ...] = ()`
    - `request_timeout_seconds: float = 15.0`
    - `process_start_timeout_seconds: float = 30.0`
    - `initialize_timeout_seconds: float = 30.0`
    - `tools_list_timeout_seconds: float = 30.0`
    - `tool_call_timeout_seconds: float = 15.0`
    - `io_poll_timeout_seconds: float = 0.25`
    - `shutdown_timeout_seconds: float = 5.0`
    - `max_pending_requests: int = 64`
    - `max_inbound_messages: int = 1024`
    - `max_list_pages: int = 32`
    - `max_tools: int = 512`
    - `max_notifications_per_window: int = 64`
    - `max_cursor_bytes: int = 4096`
    - `max_frame_bytes: int = 1048576`
    - `max_stderr_bytes: int = 262144`
    - `max_result_bytes: int = 1048576`
    - `request_timeout_deprecation: bool = False`
    - `decision: Decision = Decision.ASK`
    - `side_effect_class: str = 'read_only'`
    - `recovery_mode: str = 'retry'`
    - `execution_profile: McpExecutionProfile | None = None`
    - `image_id: str | None = None`
    - `resource_limits: McpResourceLimits | None = None`
    - `read_only_mounts: tuple[tuple[str, str], ...] = ()`
    - `code_artifacts: tuple[McpCodeArtifact, ...] = ()`
    - `container_working_directory: str | None = None`
    - `tool_allowlist: tuple[str, ...] | None = None`
    - `legacy_fixture: bool = False`

- **class `SandboxConfig`**
  - 字段:
    - `runner: SandboxRunner = SandboxRunner.DOCKER`
    - `image_id: str | None = None`
    - `docker_executable: str = 'docker'`
    - `host_trust: RepositoryTrustMode = RepositoryTrustMode.USER_CONFIRMED`

- **class `PolicyConfig`**
  - 字段:
    - `policy_version: str = 'policy-v1'`
    - `read_decision: Decision = Decision.ALLOW`
    - `patch_decision: Decision = Decision.ALLOW`
    - `test_decision: Decision = Decision.ALLOW`
    - `principal_scopes: tuple[str, ...] = ('workspace.read', 'workspace.write', 'sandbox.test', 'mcp.use')`
    - `protected_paths: tuple[str, ...] = ()`

- **class `RuntimeConfig`**
  - 字段:
    - `repo: Path`
    - `db: Path`
    - `provider: ProviderConfig`
    - `sandbox: SandboxConfig`
    - `test_profiles: tuple[TestProfileConfig, ...]`
    - `policy: PolicyConfig`
    - `system_prompt: str`
    - `required_test_profiles: tuple[str, ...] | None = None`
    - `mcp_servers: tuple[McpServerConfig, ...] = ()`
    - `owner_id: str = 'runtime-cli'`
    - `lease_seconds: int = 30`
    - `model_rounds: int = 32`
    - `max_tool_calls: int = 128`
    - `verification_max_test_runs: int | None = None`
    - `history_max_turns: int = 16`
    - `history_max_chars: int = 32000`
    - `compact_min_turns: int = 4`
    - `memory: MemoryConfig = MemoryConfig()`
    - `fallback_summary_model: str | None = None`
    - `budget_action_limits: tuple[tuple[str, int], ...] = (('root', 20),)`
    - `canary_key_env: str | None = None`
    - `durable_limits: Mapping[str, int] | None = None`
    - `config_schema_version: int = 1`

- **`load_runtime_config(path: str | Path) -> RuntimeConfig`** — Load and strictly validate a P0 runtime JSON config (I4 strict loader).
- **`resolve_canary_key(config: 'RuntimeConfig') -> bytes | None`** — RT/J J2 / PSEC semantics-C: HMAC canary key from the configured env var.
- **`resolve_api_key(provider: ProviderConfig) -> str`** — Read the configured provider key from the process environment only.

### `runtime/mcp_sandbox_labels.py`

> D25 W4: shared trusted labels for sandboxed MCP containers.


### `runtime/memory.py`

> D23-B unified memory configuration and hard limits.

- **class `MemoryConfigError`** (RuntimeError)
  - Stable, content-free memory-configuration failure safe to print.

- **class `MemoryConfig`**
  - 字段:
    - `conclusions_enabled: bool = True`
    - `conclusion_max_chars: int = 512`
    - `conclusion_recent_limit: int = 8`
    - `conclusion_model_summary: bool = False`
    - `failed_echo_max_turns: int = 3`
    - `in_run_compaction_enabled: bool = True`
    - `in_run_keep_groups: int = 4`
    - `request_context_soft_chars: int = 48000`
    - `request_context_hard_chars: int = 64000`
    - `request_context_reserve_chars: int = 8000`
    - `compaction_target_chars: int = 36000`
    - `max_compaction_epochs_per_run: int = 16`
    - `max_compaction_source_groups: int = 32`
    - `compaction_summary_max_chars: int = 2048`
    - `recall_scan_max_turns: int = 256`
    - `journal_remind_turns: int = 10`
    - `journal_remind_changed_files: int = 20`
    - `journal_inject_latest: bool = False`
  - 方法:
    - `from_mapping(cls, raw: Mapping[str, Any] | None) -> 'MemoryConfig'` — Build from a JSON-decoded mapping; missing keys fall back to defaults.
    - `to_document(self) -> dict[str, Any]` — Normalized, JSON-serializable document (all keys, correct types).


### `runtime/release_audit.py`

> Read-only durable runtime truth audit for the I9 release lane.

- **class `AuditError`** (RuntimeError)
  - Stable, content-free failure for an unreadable durable store.

- **`canonical_bytes(value: object) -> bytes`**
- **`audit_database(path: Path) -> dict`**
- **`write_report(path: Path, document: dict) -> str`**

### `runtime/session.py`

> D16 interactive session: bounded conversation history projection + compaction.

- **class `SessionHistoryError`** (RuntimeError)
  - Stable, content-free session-history failure safe to print to an operator.

- **class `SessionTurn`**
  - One completed conversational turn, projected under the whitelist.
  - 字段:
    - `user_input: str`
    - `final_text: str | None = None`
    - `turn_id: UUID | None = None`
    - `status: str | None = None`
    - `error: str | None = None`
    - `changed_files: tuple[str, ...] = ()`

- **class `SessionHistoryLimits`**
  - 字段:
    - `max_turns: int = 16`
    - `max_chars: int = 32000`
    - `compact_min_turns: int = 4`

- **class `CompactionResult`**
  - Authoritative projection plus an optional untrusted model summary.
  - 字段:
    - `authoritative: str`
    - `summary: str | None = None`
    - `dropped: tuple[SessionTurn, ...] = ()`

- **class `SessionHistory`**
  - In-memory session memory with bounded projection and compaction.
  - 方法:
    - `plan_projection(self) -> Callable[[], str] | None`
    - `plan_projection(self, value: Callable[[], str] | None) -> None`
    - `project_note(self) -> str | None`
    - `project_note(self, value: str | None) -> None`
    - `limits(self) -> SessionHistoryLimits`
    - `turn_count(self) -> int`
    - `turns(self) -> tuple[SessionTurn, ...]`
    - `preload_summaries(self, summaries: Sequence[str]) -> None` — D13-D23-005: seed persisted cross-turn summaries (oldest first).
    - `export_summaries(self) -> tuple[str, ...]` — Persisted-summary export for the session marker (D13-D23-005).
    - `append(self, turn: SessionTurn) -> None` — Append one turn whose user text is canonicalized at the entry point.
    - `_bounded_recent(self) -> list[SessionTurn]`
    - `maybe_compact(self) -> tuple[CompactionResult, ...]` — Compact newly dropped turns when their count hits the threshold.
    - `context_items(self) -> tuple[ModelContextItem, ...]` — Bounded projection: compaction blocks, conclusion blocks, recent turns.
    - `_conclusion_items(self, window_out: Sequence[SessionTurn], offset: int) -> list[ModelContextItem]` — Out-of-window conclusions as a bounded, deduped block (D23 §4.5).
    - `_load_conclusion(self, turn: SessionTurn)` — Best-effort recorded conclusion for one turn; None on any failure.
    - `_journal_reminder(self) -> UserMessage | None` — D23 §7.1 deterministic reminder for the next fresh interactive turn.
    - `mark_journal_written(self) -> None` — Record a successful journal export so reminders reset (§7.1).
    - `from_thread(cls, store, runtime, thread_id: UUID | str, *, provider: str, limits: SessionHistoryLimits | None=None, summarize: Callable[[str], str] | None=None, conclusions: object | None=None, memory: MemoryConfig | None=None) -> 'SessionHistory'` — Rebuild session history from the durable thread records.

- **`load_project_note(repo: str | Path) -> str | None`** — Load the repo-root AGENTS.md as untrusted project data (D24 W2).
- **`summarize_via_client(client, *, provider: str, model: str, text: str, max_output_tokens: int=512) -> str`** — Summarize dropped history through the configured model client.
- **class `RecallHit`**
  - One retrieved turn summary from the event store.
  - 字段:
    - `turn_id: UUID`
    - `user_input: str`
    - `final_text: str | None = None`
    - `tools: tuple[str, ...] = ()`
    - `files: tuple[str, ...] = ()`
    - `score: float = 0.0`

- **class `SessionMemory`**
  - Lexical retrieval over the durable turn records of one thread (D19-4).
  - 方法:
    - `recall(self, thread_id: UUID | str, query: str, limit: int=5) -> tuple[RecallHit, ...]` — Rank completed turns of a thread by D23 §6 IDF/recency scoring.

- **class `SessionJournal`**
  - Ledgered, crash-aware human-readable session artifact.
  - 方法:
    - `write(self, repo: Path, turns: Sequence[SessionTurn], *, path: str=DEFAULT_PATH, run_id: UUID, semantic_command_id: UUID, repository_identity_digest: str) -> Path` — Write deterministic markdown through a JOURNAL_EXPORT effect.


- 常量(字符串):
  - `_UNTRUSTED_MARKER` = `[untrusted-session-summary]`

### `runtime/store_migration.py`

> Offline legacy-store export to a fresh sanitized store (section 7.7).

- **class `LegacyExportError`** (RuntimeError)
  - Content-free export failure carrying a stable code.

- **`export_legacy_store(source: str | Path, destination: str | Path, *, busy_timeout_ms: int=10000, canaries: Sequence[str]=()) -> dict`** — Export one legacy store to a fresh sanitized destination.

- 常量(字符串):
  - `LEGACY_EXPORT_DESTINATION_EXISTS` = `legacy_export_destination_exists`
  - `LEGACY_EXPORT_SOURCE_INVALID` = `legacy_export_source_invalid`
  - `LEGACY_EXPORT_VERIFICATION_FAILED` = `legacy_export_verification_failed`

### `runtime/subprocess_env.py`

> "I1 shared minimal subprocess environment builder (S4-A).

- **class `SubprocessEnvError`** (RuntimeError)
  - Stable content-free environment contract failure.

- **`environment_identities(explicit: Mapping[str, str]) -> tuple[tuple[str, str], ...]`** — I6 §8.3/§8.4: digest of the configured env (sorted name, sha256(value)).
- **`environment_identity_digest(explicit: Mapping[str, str]) -> str`** — Stable digest over the typed (name, value_digest) identities.
- **`build_minimal_environment(explicit: Mapping[str, str], *, allowed_names: frozenset[str], private_temp: Path) -> Mapping[str, str]`** — Build the immutable minimal environment for one child process.

### `runtime/truth.py`

> I7 reconstructed business truth for App and Unified responses.

- **class `RuntimeTruthDocument`**
  - 字段:
    - `thread: ThreadState`
    - `turn: TurnState`
    - `run: RunState | LegacyRunState | None`
    - `completion_evidence: CompletionEvidenceRef | None`
    - `ledger_uncertain: bool = False`
    - `workspace_uncertain: bool = False`
    - `projection_stale: bool = False`
  - 方法:
    - `outcome_code(self) -> str`

- **class `RuntimeTruthVerifier`**
  - 方法:
    - `read(self, turn_id: UUID) -> RuntimeTruthDocument`
    - `verify(self, document: RuntimeTruthDocument) -> None`
    - `_verify_evidence(self, run_id: UUID, turn_id: UUID, reference: CompletionEvidenceRef) -> None`
    - `_last_run_id(self, turn_id: UUID) -> UUID | None`


### `runtime/turn_conclusion.py`

> D23-A: authoritative per-turn conclusion event and source-head verifier.

- **class `TurnConclusionError`** (RuntimeError)
  - Stable, content-free conclusion failure; carries a code only.

- **class `EvidenceRef`**
  - Exact reference to one piece of test/completion evidence.
  - 字段:
    - `stream_id: StreamId`
    - `stream_version: int`
    - `event_id: UUID`
    - `evidence_digest: str`

- **class `TurnConclusion`**
  - Bounded authoritative conclusion of one terminal Turn (D23 §4.1).
  - 字段:
    - `thread_id: UUID`
    - `turn_id: UUID`
    - `run_id: UUID | None`
    - `turn_status: str`
    - `run_status: str | None`
    - `request_summary: str`
    - `error_codes: tuple[str, ...]`
    - `successful_tools: tuple[str, ...]`
    - `changed_files: tuple[str, ...]`
    - `test_evidence_refs: tuple[EvidenceRef, ...]`
    - `open_obligations: tuple[str, ...]`
    - `uncertainty_codes: tuple[str, ...]`
    - `authoritative_digest: str`
    - `untrusted_summary: str | None`
    - `source_heads_digest: str`
  - 方法:
    - `document(self) -> dict[str, Any]` — Canonical bounded payload (never raw args/results/reasoning).
    - `from_document(cls, payload: Mapping[str, Any]) -> 'TurnConclusion'`

- **class `TurnConclusionStore`**
  - Build, persist and verify TurnConclusion facts (D23-A).
  - 方法:
    - `stream(self, turn_id: UUID) -> StreamId`
    - `_memory_version(self, turn_id: UUID) -> int`
    - `source_heads(self, turn: TurnState, run: RunState | None, inspection: RunEffectInspection | None) -> tuple[StreamPrecondition, ...]` — Fences for one conclusion: turn head, run head, evidence head and
    - `source_heads_digest(preconditions: Sequence[StreamPrecondition]) -> str`
    - `_last_run_id(self, turn: TurnState) -> UUID | None`
    - `_successful_tools(self, turn_id: UUID) -> tuple[str, ...]` — Tool names with a durable non-error result, from D6 facts.
    - `_changed_files(self, turn_id: UUID, successful_tools: tuple[str, ...]) -> tuple[str, ...]` — Authoritative changed files: apply_patch success results only.
    - `build(self, turn_id: UUID | str) -> TurnConclusion` — Rebuild the authoritative conclusion for a terminal Turn.
    - `persist(self, conclusion: TurnConclusion, *, command_id: UUID | None=None) -> AppendReceipt` — Append the conclusion to the turn-memory stream.
    - `load(self, turn_id: UUID | str) -> tuple[TurnConclusion, ...]` — All recorded conclusions for the Turn, newest last.
    - `is_stale(self, conclusion: TurnConclusion) -> bool` — True when the conclusion's source heads no longer match current.


- 常量(字符串):
  - `MEMORY_STREAM_CATEGORY` = `turn-memory`
  - `CONCLUSION_EVENT_TYPE` = `memory.turn-conclusion-recorded.v1`

### `runtime/turn_summary.py`

> D22 F6b: 收尾摘要——回合主体完成但最终回复失败时的可见输出。

- **`build_turn_summary(ok_tools: list[str], changed_files: tuple[str, ...]) -> str`** — 确定性收尾摘要；素材全部来自事件库/会话记录。
- **`summarize_with_model(client: Any, *, provider: str, model: str, text: str) -> tuple[str | None, bool]`** — 用指定模型生成一次收尾摘要（request-scoped，仅一次请求）。

- 常量(字符串):
  - `_SUMMARY_SYSTEM_PROMPT` = `你是收尾摘要助手：用一句简洁中文总结 agent 刚完成的工作，不要编造任何工具执行之外的事实。`

### `runtime/unified.py`

> D15 unified runtime: turn + subagents + context + compaction + trace.

- **class `UnifiedResult`**
  - 字段:
    - `turn_id: UUID`
    - `turn_status: str`
    - `context_items: tuple[str, ...]`
    - `child_states: tuple[str, ...]`
    - `compacted: str`
    - `trace_streams: tuple[str, ...]`
    - `thread_id: UUID | None = None`
    - `run_id: UUID | None = None`
    - `evidence_digest: str | None = None`

- **class `UnifiedAgentRuntime`**
  - Compose the D1–D15 slices into one runnable durable task flow.
  - 方法:
    - `execute(self, goal: str) -> UnifiedResult`
    - `_durable_turn(self, goal: str) -> RuntimeTruthDocument`


- 常量(字符串):
  - `_OWNER_ID` = `unified`

## `sandbox/`

> Public D8 Docker-sandbox protocol.

### `sandbox/__init__.py`

> Public D8 Docker-sandbox protocol.


### `sandbox/docker_primitives.py`

> D25 W2: trusted Docker primitives shared by long-lived MCP containers.

- **class `ContainerSpec`**
  - Everything needed to create one long-lived MCP container.
  - 字段:
    - `image_id: str`
    - `argv: tuple[str, ...]`
    - `container_working_directory: str`
    - `environment: tuple[tuple[str, str], ...]`
    - `cpus: float`
    - `memory_bytes: int`
    - `pids_limit: int`
    - `tmpfs_bytes: int`
    - `container_name: str`
    - `labels: tuple[tuple[str, str], ...]`

- **`create_arguments(spec: ContainerSpec) -> tuple[str, ...]`** — Trusted create argv: zero mounts, no TTY, interactive stdin, non-root.
- **`inspect_and_validate(document: Mapping[str, Any], spec: ContainerSpec) -> None`** — Exact-contract comparison of one ``docker container inspect`` document.
- **`stop_and_remove(docker: Path, container_id: str, *, stop_timeout_seconds: float, cli_timeout_seconds: float) -> None`** — Bounded stop + exact remove; failure raises (never silently ignored).

- 常量(字符串):
  - `_USER` = `65532:65532`
  - `_TMPFS_MOUNT` = `/tmp`

### `sandbox/protocol.py`

> D8 immutable Docker-sandbox protocol and persisted allocation identity.

- **class `SandboxError`** (RuntimeError)
  - Stable, content-free sandbox failure safe to persist or return.

- **class `AllocationState`** (StrEnum)

- **class `SandboxLimits`**
  - Trusted host policy for one container; values are never model supplied.
  - 字段:
    - `cpus: float = 1.0`
    - `memory_bytes: int = 256 * 1024 * 1024`
    - `pids_limit: int = 64`
    - `tmpfs_bytes: int = 64 * 1024 * 1024`
    - `cleanup_grace_seconds: float = 15.0`
    - `max_workspace_entries: int = 100000`
  - 方法:
    - `to_document(self) -> dict[str, JsonValue]`
    - `digest(self) -> str`

- **class `SandboxCommandProfile`**
  - Immutable command selected by ID; argv and environment are host-defined.
  - 字段:
    - `profile_id: str`
    - `argv: tuple[str, ...]`
    - `working_directory: str = '/workspace'`
    - `timeout_seconds: float = 60.0`
    - `max_stdout_bytes: int = 256000`
    - `max_stderr_bytes: int = 256000`
    - `environment: tuple[tuple[str, str], ...] = ()`
    - `release_fields: tuple[str, ...] | None = None`
    - `sensitive: bool = False`
    - `workspace_manifest: tuple[str, ...] = ()`
    - `profile_version: str = ''`
    - `audience_policy_ref: str | None = None`
    - `input_policy_ref: str | None = None`
    - `candidate_admission_ref: str | None = None`
    - `environment_ref: str | None = None`
    - `release_mode: str | None = None`
    - `producer_rules: tuple[str, ...] = ()`
    - `scratch_policy_ref: str | None = None`
    - `budget_policy_ref: str | None = None`
  - 方法:
    - `cwd(self) -> str`
    - `command_document(self) -> dict[str, JsonValue]`
    - `to_document(self) -> dict[str, JsonValue]`
    - `canonical_json(self) -> str`
    - `command_digest(self) -> str`
    - `profile_digest(self) -> str`

- **class `DockerDoctorReport`**
  - 字段:
    - `ready: bool`
    - `client_version: str | None = None`
    - `server_version: str | None = None`
    - `server_os: str | None = None`
    - `server_architecture: str | None = None`
    - `image_id: str | None = None`
    - `error_code: str | None = None`
  - 方法:
    - `to_document(self) -> dict[str, JsonValue]`

- **class `SandboxAllocation`**
  - JSON-projectable allocation aggregate; no live Docker object is retained.
  - 字段:
    - `allocation_id: UUID`
    - `owner_execution_id: UUID`
    - `owner_nonce: str`
    - `image_id: str`
    - `mount_digest: str`
    - `profile_digest: str`
    - `command_digest: str`
    - `deadline_at: datetime`
    - `state: AllocationState = AllocationState.INTENDED`
    - `version: int = 0`
    - `container_id: str | None = None`
    - `outcome: str | None = None`
    - `exit_code: int | None = None`
    - `oom_killed: bool | None = None`
    - `finished_at: datetime | None = None`
  - 方法:
    - `expected_labels(self) -> tuple[tuple[str, str], ...]`
    - `to_document(self) -> dict[str, JsonValue]`

- **class `ReapReport`**
  - 字段:
    - `removed: tuple[str, ...] = ()`
    - `refused: tuple[str, ...] = ()`
    - `released: tuple[UUID, ...] = ()`
    - `errors: tuple[str, ...] = ()`
  - 方法:
    - `removed_count(self) -> int`
    - `refused_count(self) -> int`
    - `released_count(self) -> int`
    - `to_document(self) -> dict[str, JsonValue]`

- **`validate_immutable_image_id(value: str) -> str`** — Accept only a complete lowercase Docker content-addressed image ID.
- **`validate_sha256_digest(value: str) -> str`**
- **`canonical_sha256(document: Mapping[str, JsonValue]) -> str`**

- 常量(字符串):
  - `MANAGED_LABEL` = `io.koawa.v2.managed`
  - `ALLOCATION_ID_LABEL` = `io.koawa.v2.allocation`
  - `OWNER_EXECUTION_ID_LABEL` = `io.koawa.v2.owner`
  - `OWNER_NONCE_LABEL` = `io.koawa.v2.owner-nonce`
  - `IMAGE_ID_LABEL` = `io.koawa.v2.image-id`
  - `MOUNT_DIGEST_LABEL` = `io.koawa.v2.mount-digest`
  - `PROFILE_DIGEST_LABEL` = `io.koawa.v2.profile-digest`
  - `COMMAND_DIGEST_LABEL` = `io.koawa.v2.command-digest`
  - `MANAGED_LABEL_VALUE` = `true`

### `sandbox/runtime.py`

> Trusted Docker control plane, allocation ledger, and exact container reaper.

- **class `SandboxAllocationStore`**
  - Append-only allocation aggregate using exact EventStore versions.
  - 方法:
    - `intent(self, *, owner_execution_id: UUID, image_id: str, mount_digest: str, profile_digest: str, command_digest: str, deadline_at: datetime, allocation_id: UUID | None=None, owner_nonce: str | None=None) -> SandboxAllocation` — Persist identity and expected labels before Docker create is legal.
    - `load(self, allocation_id: UUID) -> SandboxAllocation | None`
    - `list_open(self) -> tuple[SandboxAllocation, ...]` — Discover all non-released allocations from the global append log.
    - `bind(self, allocation: SandboxAllocation | UUID, container_id: str, *, actor: str='sandbox') -> SandboxAllocation`
    - `start(self, allocation: SandboxAllocation | UUID, *, actor: str='sandbox') -> SandboxAllocation`
    - `finish(self, allocation: SandboxAllocation | UUID, *, outcome: str, exit_code: int | None, oom_killed: bool | None, finished_at: datetime | None=None, actor: str='sandbox') -> SandboxAllocation`
    - `release(self, allocation: SandboxAllocation | UUID, *, reason: str='removed', actor: str='sandbox') -> SandboxAllocation`
    - `_current(self, allocation: SandboxAllocation | UUID) -> SandboxAllocation`
    - `_require(self, allocation_id: UUID) -> SandboxAllocation`
    - `_append(self, allocation: SandboxAllocation, *, expected_version: int, event_type: str, payload: Mapping[str, Any], actor: str) -> None`
    - `_read_stream(self, stream_id: StreamId) -> tuple[Any, ...]`

- **class `DockerSandboxDoctor`**
  - Read-only daemon and immutable-image compatibility check.
  - 方法:
    - `check(self, image_id: str) -> DockerDoctorReport`

- **`resolve_workspace_mount(workspace_root: str | os.PathLike[str], *, max_entries: int=100000) -> tuple[Path, str]`** — Resolve a bind source and reject descendant link/reparse escapes.
- **class `ContainerReaper`**
  - Reconcile only containers whose durable intent and exact labels agree.
  - 方法:
    - `reap(self, *, allocation_id: UUID | None=None, owner_execution_id: UUID | None=None, force: bool=False, now: datetime | None=None) -> ReapReport`
    - `_reconcile(self, record: SandboxAllocation, inspected: Mapping[str, Any]) -> tuple[str | None, str | None, UUID | None, str | None]`
    - `_collect_outcome(outcome: tuple[str | None, str | None, UUID | None, str | None], removed: list[str], refused: list[str], released: list[UUID], errors: list[str]) -> None`

- **class `DockerCommandRunner`**
  - D8 real-container backend for administrator-defined command profiles.
  - 方法:
    - `release_rule(self, profile_id: str)` — R3: this profile's receipt release contract, if configured.
    - `profile_ids(self) -> tuple[str, ...]`
    - `allocation_store(self) -> SandboxAllocationStore`
    - `reaper(self) -> ContainerReaper`
    - `validate_profile(self, profile_id: str) -> None`
    - `run(self, profile_id: str, *, progress_guard: Callable[[], None] | None=None, execution_id: UUID | None=None) -> CommandResult`
    - `run_profile_object(self, profile: SandboxCommandProfile, *, progress_guard: Callable[[], None] | None=None, execution_id: UUID | None=None) -> CommandResult` — Run one host-defined profile object without pre-registration.
    - `_run_profile(self, profile: SandboxCommandProfile, execution_id: UUID, progress_guard: Callable[[], None] | None) -> CommandResult`
    - `_run_manifest_profile(self, profile: SandboxCommandProfile, execution_id: UUID, progress_guard: Callable[[], None] | None) -> CommandResult` — B1: run one profile inside the isolated manifest workspace.
    - `_create_arguments(self, profile: SandboxCommandProfile, allocation: SandboxAllocation, *, workspace: object | None=None) -> tuple[str, ...]` — Container create argv.
    - `_result(self, profile: SandboxCommandProfile, allocation: SandboxAllocation, *, outcome: CommandOutcome, exit_code: int | None, stdout: bytes, stderr: bytes, stdout_bytes: int, stderr_bytes: int, duration_ms: int) -> CommandResult`
    - `_fault(self, point: str, allocation: SandboxAllocation, container_id: str | None) -> None`


- 常量(字符串):
  - `_ALLOCATION_CATEGORY` = `sandbox-allocation`

### `sandbox/test_workspace.py`

> WP-C v1 (closure review R5): isolated test workspace + safe diagnostics.

- **class `WorkspaceError`** (RuntimeError)
  - Content-free workspace failure carrying a stable code.

- **`validate_manifest(root: Path, manifest: Sequence[str]) -> tuple[Path, ...]`** — Verify a fixed input manifest: relative, in-tree, real files only.
- **`manifest_snapshot(root: Path, entries: Sequence[Path]) -> dict[str, str]`** — Content snapshot of the frozen inputs (relative path -> sha256).
- **`inputs_unchanged(root: Path, snapshot: Mapping[str, str]) -> bool`** — True when every frozen input still has its snapshot content.
- **class `PreparedTestWorkspace`**
  - One run's ephemeral workspace: read-only candidate + private scratch.
  - 方法:
    - `cleanup(self) -> None` — Best-effort removal; failures quarantine, never reuse.

- **`prepare_test_workspace(source_root: Path, manifest: Sequence[str], *, label: str) -> Iterator[PreparedTestWorkspace]`** — Materialize the isolated workspace for one profile run.
- **`extract_diagnostics(stdout_text: str) -> dict`** — Bounded structured excerpt of an isolated run's output.

## `security/`

> RT/J J2 security package (see state.py module contract).

### `security/__init__.py`

> RT/J J2 security package (see state.py module contract).


### `security/gate.py`

> RT/J J2 gate: canary detection wired into the executor authorize path.

- **class `SecurityGate`**
  - Exact session-canary gate consulted after first/final resolve.
  - 字段:
    - `event_store: EventStore`
    - `key: bytes`
  - 方法:
    - `token_for_turn(self, turn_id: Any) -> str`
    - `hit(self, arguments_json: str, turn_id: Any) -> bool`
    - `hit_multi(self, arguments_json: str, turn_id: Any, ancestor_turn_ids: Any=()) -> tuple[str, Any] | None` — PSEC semantics-C (T7-b): exact scan against the own turn token plus
    - `sticky_status(self, execution_id) -> str | None`
    - `escalate(self, record, action, *, context, approval_service, turn_id: Any, matched: tuple[str, Any] | None=None) -> dict` — Escalate an ALLOW verdict to a durably suspended approval.
    - `pending_status(self, execution_id) -> str | None`


### `security/state.py`

> RT/J J2: session-canary exact detection and security-state store.

- **`derive_canary_token(key: bytes, turn_id: UUID) -> str`** — High-entropy session canary token (HMAC-SHA256, 32 hex chars).
- **`scan_exact_token(text: str, token: str) -> bool`** — Byte-exact occurrence check.  No encoding or variant claims (§5.3).
- **`security_aggregate_id(execution_id: UUID) -> UUID`**
- **`security_stream(execution_id: UUID) -> StreamId`**
- **`escalation_id(execution_id: UUID, action_digest: str, policy_version: str) -> UUID`**
- **class `SecurityEscalation`**
  - 字段:
    - `execution_id: UUID`
    - `status: str`
    - `version: int`
    - `action_digest: str`
    - `policy_version: str`
    - `reason_code: str`

- **class `SecurityStateStore`**
  - Execution-scoped sticky escalation state with exact-version CAS.
  - 方法:
    - `_events(self, execution_id: UUID)`
    - `head(self, execution_id: UUID) -> int`
    - `status(self, execution_id: UUID) -> tuple[str | None, int]` — Sticky status: latest status-bearing event wins (§3 J2).
    - `propose(self, execution_id: UUID, *, signal_payload: Mapping[str, Any], escalated_payload: Mapping[str, Any], expected_head: int | None=None) -> int` — Append signal + escalated atomically (exact CAS, two CAS tries).


- 常量(字符串):
  - `SECURITY_SIGNAL_EVENT` = `security.signal.v1`
  - `POLICY_ESCALATED_EVENT` = `policy.escalated.v1`
  - `ESCALATION_PENDING` = `PENDING`
  - `ESCALATION_DENIED` = `DENIED`
  - `ESCALATION_GRANTED` = `GRANTED`
  - `ESCALATION_CONSUMED` = `CONSUMED`
  - `ESCALATION_EXPIRED` = `EXPIRED`

## `telemetry/`

> Lazy telemetry exports keep fault contracts independent of runtime imports.

### `telemetry/__init__.py`

> Lazy telemetry exports keep fault contracts independent of runtime imports.


### `telemetry/faults.py`

> I8 production fault-port contract and the single stable point registry.

- **class `FaultPointClass`** (StrEnum)

- **class `FaultEventDelta`**
  - 字段:
    - `counters: Mapping[str, int]`
    - `stream_categories: tuple[str, ...] = ()`
    - `per_fact: Mapping[str, int] = field(default_factory=dict)`
    - `variants: Mapping[str, Mapping[str, int]] = field(default_factory=dict)`

- **class `FaultSpec`**
  - 字段:
    - `name: str`
    - `point_class: FaultPointClass`
    - `marker_timing: str`
    - `expected_event_delta: FaultEventDelta`
    - `recovery_oracle: str`

- **class `FaultPort`** (Protocol)
  - 方法:
    - `hit(self, point: str, facts: Mapping[str, JsonPrimitive]) -> None`

- **class `NoOpFaultPort`**
  - 方法:
    - `hit(self, point: str, facts: Mapping[str, JsonPrimitive]) -> None`

- **class `InjectedFault`** (RuntimeError)

- **class `RecordingFaultPort`**
  - 字段:
    - `raise_at: frozenset[str] = frozenset()`
    - `hits: list[tuple[str, Mapping[str, JsonPrimitive]]] = field(default_factory=list)`
  - 方法:
    - `hit(self, point: str, facts: Mapping[str, JsonPrimitive]) -> None`

- **`using_fault_port(port: FaultPort)`** — Explicit test-only injection; no config/env switch can enable faults.
- **`emit_fault(point: str, facts: Mapping[str, JsonPrimitive], *, port=None) -> None`**
- **`require_fault_point(point: str) -> FaultSpec`**
- **`validate_fault_facts(facts: Mapping[str, JsonPrimitive]) -> Mapping[str, JsonPrimitive]`**
- **class `LegacyFaultAdapter`**
  - Validate old D11 metadata; new ports receive only bounded primitives.

- **`adapt_fault_callback(callback, port=None)`**
- **class `FaultInjector`**
  - D14 deterministic selector retained for compatibility and evals.
  - 字段:
    - `seed: str`
    - `script: tuple[str, ...] = ()`
    - `triggered: list[str] = field(default_factory=list, compare=False)`
  - 方法:
    - `should_fail(self, point: str) -> bool`
    - `to_document(self) -> dict`

- **`classify_failure(code: str) -> str`**

### `telemetry/trace.py`

> D14 event trace with field allowlist and pre-persist redaction.

- **class `TraceRecord`**
  - 字段:
    - `correlation_id: UUID`
    - `stream: str`
    - `kind: str`
    - `sequence: int`
    - `occurred_at: datetime`
    - `fields: Mapping[str, Any]`

- **class `TraceProbe`**
  - Small, already-classified diagnostic DTO accepted by production sinks.
  - 字段:
    - `correlation_id: UUID`
    - `stream: str`
    - `kind: str`
    - `fields: Mapping[str, Any]`

- **class `TraceSink`** (Protocol)
  - Failure-isolated trace boundary used by production components.
  - 方法:
    - `emit(self, probe: TraceProbe) -> None`

- **class `TraceDiagnostics`**
  - 字段:
    - `dropped_since_start: int`
    - `last_error_code: str | None`
    - `last_failure_at: datetime | None`

- **class `TraceStore`**
  - Persist bounded trace facts; raw bodies/credentials never enter.
  - 方法:
    - `append(self, *, correlation_id: UUID, stream: str, kind: str, fields: Mapping[str, Any]) -> TraceRecord`
    - `read(self, correlation_id: UUID) -> list[TraceRecord]`
    - `_read_all(self, stream: StreamId) -> tuple`

- **class `BestEffortTraceSink`**
  - Serialize local emits and never let diagnostic storage break business work.
  - 方法:
    - `emit(self, probe: TraceProbe) -> None`
    - `diagnostics(self) -> TraceDiagnostics`
    - `_correlation_lock(self, correlation_id: UUID)`
    - `_record_drop(self, code: str) -> None`


## `tools/`

> D3 typed tool schemas, registry, workspace safety, and repository reads.

### `tools/__init__.py`

> D3 typed tool schemas, registry, workspace safety, and repository reads.


### `tools/errors.py`

> D3 工具层可以安全跨边界传播的稳定错误。

- **class `ToolConfigurationError`** (ValueError)
  - 启动期工具配置无效；异常正文只包含稳定分类。

- **class `ToolRegistryError`** (RuntimeError)
  - Registry 或 handler 违反本地执行合同。

- **class `ToolArgumentError`** (Exception)
  - 模型可修正的参数错误；不保存导致失败的原始值。

- **`tool_error_result(code: str, *, reason: str | None=None, field: str | None=None, expected: str | None=None, example: str | None=None, detail: str | None=None) -> ToolExecutionResult`** — 构造模型可见的稳定 JSON 错误，不回显工具名、参数值或异常正文.
- **`argument_error_result(error: ToolArgumentError, *, example: str | None=None) -> ToolExecutionResult`** — 把内部参数拒绝转换成 Registry 的模型可见结果。

### `tools/registry.py`

> D3 的确定性 Tool Registry 与唯一 handler 分发入口。

- **class `ToolHandler`** (Protocol[ArgumentsT])
  - Registry 在 schema gate 后调用的 typed handler。

- **class `PreparedToolInvocation`**
  - Schema-gated invocation token whose repr never exposes arguments.
  - 字段:
    - `_registry: 'ToolRegistry'`
    - `_entry: _Entry[Any] | None`
    - `_call: ToolCallItem`
    - `_arguments: Any | None`
    - `_early_result: ToolExecutionResult | None`
    - `_issuance_token: object`
  - 方法:
    - `call(self) -> ToolCallItem`
    - `spec(self) -> ToolSpec[Any] | None`
    - `arguments(self) -> Any | None`
    - `early_result(self) -> ToolExecutionResult | None`

- **class `ToolRegistry`**
  - 注册期可变、首次模型快照后不可变的本地工具目录。
  - 方法:
    - `sealed(self) -> bool`
    - `register(self, spec: ToolSpec[ArgumentsT], handler: ToolHandler[ArgumentsT]) -> None` — 注册同源 spec/handler；重名与运行期变更在启动边界失败。
    - `definitions(self) -> tuple[ToolDefinition, ...]` — 返回按名称排序的固定 D2 definitions；第一次调用原子地 seal。
    - `bind_policy_authority(self, authority: object) -> None` — Bind the sole D9 policy authority; rebinding cannot replace it.
    - `prepare(self, call: ToolCallItem) -> PreparedToolInvocation` — Seal, look up and schema-decode a call without invoking its handler.
    - `invoke_prepared(self, prepared: PreparedToolInvocation, *, context: ToolExecutionContext, authority: object) -> ToolExecutionResult` — Invoke one registry-issued preparation under the bound authority.
    - `discard_prepared(self, prepared: PreparedToolInvocation, *, authority: object) -> bool` — Consume an issued preparation; False if it was already consumed.
    - `execute(self, call: ToolCallItem, *, context: ToolExecutionContext) -> ToolExecutionResult` — 完成 name/schema gate 后调用 typed handler；预期参数错误模型可见。
    - `_validate_prepared_locked(self, prepared: PreparedToolInvocation) -> None`
    - `_invoke_prepared(prepared: PreparedToolInvocation, *, context: ToolExecutionContext) -> ToolExecutionResult`


### `tools/repo_map.py`

> D24 W3: repo_map —— 有界目录树 + Python 符号大纲（低信任元数据）。

- **class `RepoMapError`** (RuntimeError)

- **class `RepoMapLimits`**
  - 字段:
    - `max_entries: int = 256`
    - `max_depth: int = 6`
    - `max_scan_entries: int = 2000`
    - `max_files: int = 200`
    - `max_symbol_probe_bytes: int = 32768`
    - `max_symbols_per_file: int = 12`
    - `max_result_chars: int = 16000`

- **class `RepoMapArguments`**
  - 字段:
    - `path: str`
    - `max_depth: int`

- **`repo_map_tool_spec(limits: RepoMapLimits | None=None) -> ToolSpec[RepoMapArguments]`**
- **`register_repo_map_tool(registry: ToolRegistry, resolver: WorkspacePathResolver, *, limits: RepoMapLimits | None=None) -> None`**

### `tools/repository.py`

> D3 只读仓库工具：同源 schema、有界执行与稳定 JSON 结果。

- **class `RepositoryToolLimits`**
  - Provider schema 与 handler 共用的硬限额。
  - 字段:
    - `max_path_chars: int = 1024`
    - `max_query_chars: int = 256`
    - `max_glob_patterns: int = 16`
    - `max_glob_pattern_chars: int = 256`
    - `max_file_bytes: int = 1000000`
    - `max_start_line: int = 2147483647`
    - `max_read_lines: int = 500`
    - `max_directory_entries: int = 1000`
    - `max_directory_scan_entries: int = 10000`
    - `max_search_files: int = 1000`
    - `max_search_total_bytes: int = 10000000`
    - `max_depth: int = 12`
    - `max_matches: int = 500`
    - `max_match_chars: int = 400`
    - `max_output_chars: int = 100000`

- **class `ReadFileArguments`**
  - 字段:
    - `path: str`
    - `start_line: int`
    - `max_lines: int`

- **class `ListFilesArguments`**
  - 字段:
    - `path: str`
    - `max_depth: int`
    - `max_entries: int`

- **class `SearchTextArguments`**
  - 字段:
    - `query: str`
    - `path: str`
    - `max_depth: int`
    - `max_files: int`
    - `max_matches: int`
    - `case_sensitive: bool = True`
    - `include: tuple[str, ...] = ()`
    - `exclude: tuple[str, ...] = ()`

- **`repository_tool_specs(limits: RepositoryToolLimits | None=None) -> tuple[ToolSpec[ReadFileArguments], ToolSpec[ListFilesArguments], ToolSpec[SearchTextArguments]]`** — 从同一份硬限额生成模型 definition 和运行时 decoder。
- **`build_repository_tool_registry(workspace_root: str | Path, *, limits: RepositoryToolLimits | None=None) -> RepositoryToolRegistry`** — 为一个已存在的 workspace root 构建只读、未 seal 的 Registry。
- **`register_repository_tools(registry: ToolRegistry, resolver: WorkspacePathResolver, *, limits: RepositoryToolLimits | None=None) -> None`** — 把 D3 三个工具接到调用方 Registry，供 D4 组合完整 Coding catalog。
- **class `RepositoryToolRegistry`** (ToolRegistry)
  - ToolRegistry 加 resolver 生命周期；长运行进程可显式释放 root handle。
  - 方法:
    - `close(self) -> None`


### `tools/schema.py`

> D3 ToolSpec 与受限 JSON Schema 编译器。

- **class `ToolSpec`** (Generic[ArgumentsT])
  - 一份同时生成 Provider definition、validator 和 typed decoder 的工具规格。
  - 字段:
    - `name: str`
    - `description: str | None`
    - `arguments_type: type[ArgumentsT]`
    - `_definition: ToolDefinition`
    - `_compiled: _CompiledObject[ArgumentsT]`
  - 方法:
    - `input_schema_json(self) -> str`
    - `input_schema(self) -> dict[str, Any]`
    - `definition(self) -> ToolDefinition` — 返回由已编译 schema 产生的 immutable D2 定义。
    - `decode(self, arguments_json: str) -> ArgumentsT` — 严格验证 JSON 后构造 handler 专用 typed arguments dataclass。


### `tools/workspace.py`

> D3 工作区内、有界、只读的文件系统访问边界。

- **class `WorkspacePathError`** (Exception)
  - 稳定、可交给模型的错误；正文永不嵌入宿主绝对路径。

- **class `WorkspaceEntryKind`** (StrEnum)

- **class `WorkspaceFileRead`**
  - 字段:
    - `path: str`
    - `data: bytes`
    - `byte_length: int`
    - `sha256: str`
    - `identity: tuple[int, ...]`

- **class `WorkspaceDirectoryEntry`**
  - 字段:
    - `path: str`
    - `name: str`
    - `kind: WorkspaceEntryKind`
    - `size: int | None`

- **class `WorkspaceDirectoryListing`**
  - 字段:
    - `path: str`
    - `entries: tuple[WorkspaceDirectoryEntry, ...]`
    - `total_entries: int`
    - `truncated: bool`

- **class `WorkspaceDirectoryIdentity`**
  - D4 写事务使用的已打开目录身份；不暴露宿主绝对路径。
  - 字段:
    - `path: str`
    - `identity: tuple[int, ...]`

- **class `WorkspacePathResolver`**
  - 绑定一个 canonical workspace root，只暴露有界只读 API。
  - 方法:
    - `close(self) -> None`
    - `read_bytes(self, path: str, *, max_bytes: int, progress_guard: ProgressGuard | None=None) -> WorkspaceFileRead` — 从已验证的打开对象读取一个普通文件，绝不返回路径让调用者重开。
    - `list_directory(self, path: str='.', *, max_entries: int, max_scan_entries: int, progress_guard: ProgressGuard | None=None) -> WorkspaceDirectoryListing` — 不跟随链接，返回确定性排序的直接子项。
    - `inspect_directory(self, path: str='.', *, progress_guard: ProgressGuard | None=None) -> WorkspaceDirectoryIdentity` — 打开并验证一个目录，供写事务绑定 parent identity。
    - `_ensure_open(self) -> None`
    - `_preflight(self, parts: tuple[str, ...]) -> None`
    - `_read_posix(self, parts: tuple[str, ...], display: str, limit: int, guard: ProgressGuard) -> WorkspaceFileRead`
    - `_read_windows(self, parts: tuple[str, ...], display: str, limit: int, guard: ProgressGuard) -> WorkspaceFileRead`
    - `_list_posix(self, parts: tuple[str, ...], scan_limit: int, guard: ProgressGuard) -> list[WorkspaceDirectoryEntry]`
    - `_list_windows(self, parts: tuple[str, ...], scan_limit: int, guard: ProgressGuard) -> list[WorkspaceDirectoryEntry]`
    - `_open_posix(self, parts: tuple[str, ...], *, require_directory: bool) -> int`
    - `_assert_windows_contained(self, handle: int) -> None`
    - `_assert_root_unchanged(self) -> None`


## `verification/`

> D5 trusted commands, Git evidence, verification ledger, and finalization.

### `verification/__init__.py`

> D5 trusted commands, Git evidence, verification ledger, and finalization.


### `verification/admission.py`

> A3 (WP-C): candidate admission identity and invalidation.

- **class `AdmissionError`** (RuntimeError)
  - Content-free candidate-admission failure.

- **class `CandidateIdentity`**
  - The bound facts of one candidate; every field participates.
  - 字段:
    - `code_digest: str`
    - `test_digest: str`
    - `dependency_digest: str`
    - `input_digest: str`
    - `environment_digest: str`
    - `profile_id: str`
    - `profile_version: str`
  - 方法:
    - `document(self) -> dict`
    - `candidate_id(self) -> str` — Opaque, stable identity; any bound change yields a new id.

- **class `AdmissionPolicy`**
  - Which candidates may run without local review.
  - 字段:
    - `approved_candidate_ids: tuple[str, ...] = ()`
    - `require_review: bool = True`
    - `max_candidate_changes: int = 1`

- **class `AdmissionTracker`**
  - Per-turn admission state: distinct candidates tried and their verdict.
  - 字段:
    - `policy: AdmissionPolicy`
    - `seen: list[str] | None = None`
  - 方法:
    - `evaluate(self, identity: CandidateIdentity) -> str` — APPROVED / REVIEW_REQUIRED for this candidate.

- **`candidate_identity_from_parts(*, code_digests: tuple[str, ...], test_digests: tuple[str, ...], dependency_digest: str, input_digest: str, environment_digest: str, profile_id: str, profile_version: str) -> CandidateIdentity`** — Fold ordered component digests into one candidate identity.

- 常量(字符串):
  - `ADMISSION_APPROVED` = `approved`
  - `ADMISSION_REVIEW_REQUIRED` = `local_review_required`
  - `ADMISSION_REFUSED` = `admission_refused`

### `verification/dependency_preparation.py`

> C1 (WP-C): controlled dependency preparation.

- **class `DependencyPreparationError`** (RuntimeError)
  - Content-free preparation failure with a stable code.

- **class `DependencyScope`**
  - The maintainer-authorised preparation scope.
  - 字段:
    - `allowed_packages: tuple[str, ...] = ()`
    - `allow_install: bool = False`
    - `max_packages: int = 8`
    - `index_urls: tuple[str, ...] = ()`

- **class `PreparationDecision`**
  - 字段:
    - `status: str`
    - `install: tuple[str, ...] = ()`
    - `requires_authorization: tuple[str, ...] = ()`
    - `refused: tuple[str, ...] = ()`
    - `reason: str | None = None`

- **`plan_preparation(requested: tuple[str, ...], scope: DependencyScope, *, missing: tuple[str, ...] | None=None) -> PreparationDecision`** — Classify a preparation request; never installs anything.

- 常量(字符串):
  - `PREP_READY` = `ready`
  - `PREP_REQUIRES_AUTHORIZATION` = `requires_authorization`
  - `PREP_REFUSED` = `refused`

### `verification/finalization.py`

> D5 修改/测试/Git 证据账本与最终完成闸门。

- **class `VerificationError`** (Exception)

- **class `VerificationLimits`**
  - 字段:
    - `max_test_runs: int = 4`
    - `max_report_chars: int = 100000`

- **class `TestEvidence`**
  - 字段:
    - `generation: int`
    - `result: CommandResult`

- **class `VerificationLedger`**
  - 按 D1 run_id 隔离 D5 证据，并用 mutation generation 防止陈旧测试。
  - 方法:
    - `record_patch(self, context: ToolExecutionContext, result: PatchTransactionResult) -> None`
    - `reserve_test(self, run_id: UUID) -> int`
    - `record_test(self, run_id: UUID, generation: int, result: CommandResult) -> None`
    - `record_status(self, run_id: UUID, status: GitStatusSnapshot) -> None`
    - `record_diff(self, run_id: UUID, diff: GitDiffSnapshot) -> None`
    - `finalize(self, run_id: UUID) -> str`
    - `assert_complete(self, run_id: UUID) -> None`
    - `_state(self, run_id: UUID) -> _RunEvidence`
    - `_invalidate_final(state: _RunEvidence) -> None`


### `verification/git.py`

> D5 安全只读 Git 门面。

- **class `GitFacadeError`** (Exception)

- **class `GitLimits`**
  - 字段:
    - `timeout_seconds: float = 15.0`
    - `max_status_bytes: int = 1000000`
    - `max_diff_bytes: int = 1000000`
    - `max_paths: int = 10000`
    - `max_untracked_file_bytes: int = 1000000`

- **class `GitStatusEntry`**
  - 字段:
    - `status: str`
    - `path: str`

- **class `GitStatusSnapshot`**
  - 字段:
    - `entries: tuple[GitStatusEntry, ...]`
    - `digest: str`
  - 方法:
    - `paths(self) -> tuple[str, ...]`

- **class `GitDiffSnapshot`**
  - 字段:
    - `changed_paths: tuple[str, ...]`
    - `diff: str`
    - `diff_sha256: str`
    - `truncated: bool`
    - `status_digest: str`

- **class `GitFacade`**
  - 绑定一个仓库和创建时 dirty baseline 的只读视图。
  - 方法:
    - `baseline(self) -> GitStatusSnapshot`
    - `protected_paths(self) -> frozenset[str]`
    - `_content_verified_protection(self, baseline: GitStatusSnapshot) -> frozenset[str]` — Startup protection set with content-truth dirty classification.
    - `status(self, *, progress_guard: Callable[[], None] | None=None) -> GitStatusSnapshot`
    - `agent_changed_paths(self, status: GitStatusSnapshot | None=None) -> tuple[str, ...]`
    - `baseline_unchanged(self) -> bool`
    - `diff(self, *, progress_guard: Callable[[], None] | None=None) -> GitDiffSnapshot`
    - `_git_prefix(self) -> tuple[str, ...]`
    - `_git_environment() -> dict[str, str]`
    - `_git_command(self, arguments: tuple[str, ...], *, max_bytes: int, progress_guard: Callable[[], None] | None=None, allow_truncation: bool=False) -> bytes`
    - `_diff_has_changes(self, relative: str) -> bool` — Content-anchored dirty test for a tracked path.
    - `_fingerprint(self, relative: str) -> str`


### `verification/output_policy.py`

> Hardening 2026-09-19: model-visible test output policy (single authority).

- **class `ReleaseRule`**
  - One profile's field-release contract (deployment input).
  - 方法:
    - `withheld(self) -> bool`

- **`apply_release_rule(payload: dict, rule: 'ReleaseRule | None') -> dict`** — Filter a receipt payload by the profile's release contract.
- **`withheld_receipt(profile_id: str) -> dict`** — The fixed sensitive-without-contract state (distinct from failure).
- **`safe_diagnostics(result) -> dict`** — Fixed structured diagnostic fields for one CommandResult.

- 常量(字符串):
  - `POLICY_VERSION` = `test-output-policy-v1`
  - `BODY_VISIBILITY` = `metadata_only`
  - `MCP_POLICY_VERSION` = `mcp-output-policy-v1`
  - `MCP_BODY_VISIBILITY` = `metadata_only`
  - `RESULT_WITHHELD` = `result_withheld`
  - `HUMAN_REQUIRED` = `human_required`

### `verification/release_contracts.py`

> Release-contract surface (contracts.md §2/§4, first protocol version).

- **`parse_producer_rules(entries) -> tuple`** — Parse/validate §2 producer_rules entries; raise on unknown fields,
- **`apply_producer_rules(diagnostic_body: dict, rules) -> dict`** — §2 execution point: keep only fields whose rule names the trusted
- **class `ReleaseMode`** (StrEnum)

- **class `ProfileContractError`** (RuntimeError)
  - Content-free contract failure carrying a stable code.

- **class `ProfileReleaseContract`**
  - §2 per-profile release contract instance (trusted config only).
  - 字段:
    - `profile_id: str`
    - `profile_version: str`
    - `audience_policy_ref: str | None = None`
    - `input_policy_ref: str | None = None`
    - `candidate_admission_ref: str | None = None`
    - `environment_ref: str | None = None`
    - `release_mode: ReleaseMode = ReleaseMode.LOCAL_ONLY`
    - `release_fields: tuple[str, ...] | None = None`
    - `producer_rules: tuple[str, ...] = ()`
    - `scratch_policy_ref: str | None = None`
    - `budget_policy_ref: str | None = None`
    - `sensitive: bool = False`
  - 方法:
    - `audience_proven(self) -> bool` — detailed may only claim model-readable bodies with this proof.
    - `environment_proven(self) -> bool`
    - `sensitive_validation_open(self) -> bool`
    - `scratch_allowed(self) -> bool`
    - `effective_mode_allows_body(self) -> bool` — Whether the (downgraded) mode may carry ANY diagnostic body.

- **`effective_release_mode(mode: ReleaseMode, contract: ProfileReleaseContract) -> ReleaseMode`** — §2: detailed alone authorizes nothing — it downgrades without proofs.
- **`build_receipt(contract: ProfileReleaseContract, *, execution_status: str, verdict: str, diagnostic: dict | None, capture_truncated: bool=False, projection_reduced: bool=False, read_truncated: bool=False, receipt_ref: str | None=None) -> dict`** — §4 receipt: generated FROM the release contract, never a serialization

- 常量(字符串):
  - `RECEIPT_SCHEMA_VERSION` = `result-receipt-v1`

### `verification/runner.py`

> D5 受信测试配置与有界宿主进程执行。

- **class `CommandRunnerError`** (Exception)
  - 可安全跨工具边界传播的稳定命令错误。

- **class `RepositoryTrust`** (StrEnum)

- **class `CommandOutcome`** (StrEnum)

- **class `CommandProfile`**
  - 可信调用方注册的固定命令；模型永远不能修改 argv/env/cwd。
  - 字段:
    - `profile_id: str`
    - `argv: tuple[str, ...]`
    - `timeout_seconds: float = 60.0`
    - `max_stdout_bytes: int = 256000`
    - `max_stderr_bytes: int = 256000`
    - `environment: tuple[tuple[str, str], ...] = ()`
    - `release_fields: tuple[str, ...] | None = None`
    - `sensitive: bool = False`
    - `profile_version: str = ''`
    - `audience_policy_ref: str | None = None`
    - `input_policy_ref: str | None = None`
    - `candidate_admission_ref: str | None = None`
    - `environment_ref: str | None = None`
    - `release_mode: str | None = None`
    - `producer_rules: tuple[str, ...] = ()`
    - `scratch_policy_ref: str | None = None`
    - `budget_policy_ref: str | None = None`
    - `workspace_manifest: tuple[str, ...] = ()`

- **class `CommandResult`**
  - 字段:
    - `profile_id: str`
    - `outcome: CommandOutcome`
    - `exit_code: int | None`
    - `stdout: str`
    - `stderr: str`
    - `stdout_bytes: int`
    - `stderr_bytes: int`
    - `stdout_truncated: bool`
    - `stderr_truncated: bool`
    - `duration_ms: int`
    - `argv: tuple[str, ...]`
    - `timeout_seconds: float`
    - `backend: str = 'host'`
    - `immutable_image_id: str | None = None`
    - `profile_digest: str | None = None`
    - `allocation_id: UUID | None = None`
    - `container_id: str | None = None`
    - `isolated_workspace: bool = False`
    - `inputs_changed: bool = False`
  - 方法:
    - `passed(self) -> bool`

- **class `CommandRunner`** (Protocol)
  - D5/D8 共用的结构化命令执行端口。
  - 方法:
    - `profile_ids(self) -> tuple[str, ...]` — 返回可信配置中可供模型选择的 profile ID。
    - `validate_profile(self, profile_id: str) -> None` — 在占用验证预算前确定性校验 profile。
    - `run(self, profile_id: str, *, progress_guard: Callable[[], None] | None=None, execution_id: UUID | None=None) -> CommandResult` — 按固定 profile 执行，并返回带 backend 身份的类型化结果。

- **class `TrustedCommandRunner`**
  - 只按 ``profile_id`` 运行不可变测试命令。
  - 方法:
    - `profile_ids(self) -> tuple[str, ...]`
    - `release_rule(self, profile_id: str)` — R3: this profile's receipt release contract, if configured.
    - `release_contract(self, profile_id: str)` — contracts.md §2: the profile's release contract instance.
    - `validate_profile(self, profile_id: str) -> None` — 在占用测试预算前验证 trust gate 与固定配置是否存在。
    - `run(self, profile_id: str, *, progress_guard: Callable[[], None] | None=None, execution_id: UUID | None=None) -> CommandResult`

- **`run_bounded_process(cwd: str | Path, argv: Sequence[str], *, timeout_seconds: float, max_stdout_bytes: int, max_stderr_bytes: int, environment: Mapping[str, str] | None=None, progress_guard: Callable[[], None] | None=None) -> _ProcessResult`** — 固定 argv 的底层执行器；并行排空输出，超时/取消时终止进程树。

### `verification/tools.py`

> D5 测试、Git 证据、Finalizer 工具及完整 Coding Registry。

- **class `RunTestArguments`**
  - 字段:
    - `profile_id: str`

- **class `EmptyArguments`**

- **class `D5ToolLimits`**
  - 字段:
    - `max_tool_result_chars: int = 262144`

- **class `CodingToolRegistry`** (RepositoryToolRegistry)
  - D3+D4+D5 工具目录，同时实现 AgentLoop CompletionGate。
  - 方法:
    - `assert_complete(self, run_id: UUID) -> None`

- **`build_verified_coding_tool_registry(workspace_root: str | Path, *, command_profiles: Sequence[CommandProfile] | None=None, command_runner: CommandRunner | None=None, required_test_profiles: Sequence[str] | None=None, repository_trust: RepositoryTrust=RepositoryTrust.UNTRUSTED, repository_limits: RepositoryToolLimits | None=None, patch_limits: PatchLimits | None=None, git_limits: GitLimits | None=None, verification_limits: VerificationLimits | None=None, tool_limits: D5ToolLimits | None=None, fault_injector: FaultInjector | None=None, git_facade: GitFacade | None=None, plan_board: PlanBoard | None=None, post_build_registrars: Sequence=(), protected_patterns: tuple[str, ...]=()) -> CodingToolRegistry`** — 构建 D3 read/search + D4 patch + D5 test/git/finalize 的完整目录。

## `workspace/`

> D12 per-agent worktree, container, and artifact integration.

### `workspace/__init__.py`

> D12 per-agent worktree, container, and artifact integration.


### `workspace/artifacts.py`

> I7 content-addressed, JSON-only artifact packages and durable references.

- **class `TestEvidenceRef`**
  - 字段:
    - `stream_id: StreamId`
    - `stream_version: int`
    - `event_id: UUID`
    - `evidence_digest: str`

- **class `ArtifactPackageRef`**
  - 字段:
    - `package_ref: str`
    - `package_digest: str`
    - `package_json_bytes: int`

- **class `ArtifactPackageEntry`**
  - 字段:
    - `path_bytes: bytes`
    - `kind: str`
    - `mode: int`
    - `content_or_target: bytes`
    - `content_sha256: str`
  - 方法:
    - `create(cls, *, path_bytes: bytes, kind: str, mode: int, content_or_target: bytes) -> 'ArtifactPackageEntry'`
    - `document(self) -> dict[str, object]`

- **class `ArtifactPackageV2`**
  - 字段:
    - `repository_identity_digest: str`
    - `base_commit: str`
    - `tracked_binary_patch: bytes`
    - `entries: tuple[ArtifactPackageEntry, ...]`
    - `prestate_digest: str`
    - `poststate_manifest_digest: str`
  - 方法:
    - `document(self) -> dict[str, object]`

- **class `ArtifactV2`**
  - 字段:
    - `artifact_id: UUID`
    - `agent_id: UUID`
    - `run_id: UUID`
    - `repository_identity_digest: str`
    - `base_commit: str`
    - `repo_prestate_digest: str`
    - `package: ArtifactPackageRef`
    - `diff_digest: str`
    - `working_tree_content_digest: str`
    - `test_evidence_ref: TestEvidenceRef`
    - `sandbox_image_digest: str`
    - `sandbox_profile_digest: str`
    - `created_at: datetime`
  - 方法:
    - `create(cls, *, agent_id: UUID, run_id: UUID, repository_identity_digest: str, base_commit: str, repo_prestate_digest: str, package: ArtifactPackageRef, diff_digest: str, working_tree_content_digest: str, test_evidence_ref: TestEvidenceRef, sandbox_image_digest: str, sandbox_profile_digest: str, created_at: datetime | None=None) -> 'ArtifactV2'`

- **class `ArtifactPackageStore`**
  - Atomic package storage with optional event-backed pin/release fences.
  - 方法:
    - `put(self, package: ArtifactPackageV2) -> ArtifactPackageRef`
    - `load(self, reference: ArtifactPackageRef) -> ArtifactPackageV2`
    - `pin(self, *, artifact_id: UUID, package: ArtifactPackageRef, command_id: UUID) -> None`
    - `release(self, *, artifact_id: UUID, expected_version: int, command_id: UUID) -> None`
    - `collect_unpinned(self) -> tuple[str, ...]` — Delete only packages proven to have no active durable pin.

- **`package_from_snapshot(*, repository_identity_digest: str, base_commit: str, tracked_binary_patch: bytes, entries: tuple[ArtifactPackageEntry, ...], prestate_digest: str, poststate_manifest_digest: str) -> ArtifactPackageV2`**
- **`apply_package(package: ArtifactPackageV2, *, worktree: Path, git_binary: str) -> None`** — Apply one verified package without allowing paths outside the worktree.

### `workspace/container.py`

> D12 container boundary: injectable runner (real Docker is env-gated).

- **class `ContainerResult`**
  - 字段:
    - `exit_code: int | None`
    - `stdout: str`
    - `stderr: str`
    - `image_digest: str`

- **class `ContainerRunner`** (Protocol)
  - 方法:
    - `run(self, worktree: Path, argv: Sequence[str], *, timeout: float=60.0) -> ContainerResult`

- **class `InjectedContainerRunner`**
  - Host-bootstrap runner for deterministic tests (D8 real runner later).
  - 方法:
    - `run(self, worktree: Path, argv: Sequence[str], *, timeout: float=60.0) -> ContainerResult`

- **class `DockerContainerRunner`**
  - Real D8 Docker runner bound to one immutable image.
  - 方法:
    - `run(self, worktree: Path, argv: Sequence[str], *, timeout: float=60.0) -> ContainerResult`


### `workspace/content.py`

> I7 stable repository prestate and content identity snapshots.

- **class `ManifestEntry`**
  - 字段:
    - `path_bytes: bytes`
    - `kind: str`
    - `mode: int`
    - `size: int`
    - `content_or_target_sha256: str`
  - 方法:
    - `document(self) -> dict[str, object]`

- **class `RepoPrestate`**
  - 字段:
    - `head_commit: str`
    - `index_identity_digest: str`
    - `status_raw_digest: str`
    - `working_tree_manifest_digest: str`
    - `prestate_digest: str`

- **class `ContentSnapshot`**
  - 字段:
    - `prestate: RepoPrestate`
    - `diff_bytes: bytes`
    - `diff_digest: str`
    - `entries: tuple[ManifestEntry, ...]`
    - `working_tree_content_digest: str`

- **`capture_repository(repo_root: Path, *, base_commit: str | None=None, git_binary: str | None=None) -> ContentSnapshot`** — Capture one coherent repository state or fail with a stable race code.
- **`repository_identity(repo_root: Path, *, git_binary: str | None=None) -> str`**

### `workspace/effects.py`

> I7 durable ledger for workspace side effects.

- **class `WorkspaceEffectError`** (RuntimeError)
  - Stable, content-free workspace-effect failure.

- **class `WorkspaceEffectConflict`** (WorkspaceEffectError)
  - One stable command identity was reused for different semantics.

- **class `WorkspaceEffectKind`** (StrEnum)

- **class `WorkspaceEffectState`** (StrEnum)

- **class `WorkspaceEffectResultKind`** (StrEnum)

- **class `WorkspaceEffectResolvedState`** (StrEnum)

- **class `WorkspaceEffectRecord`**
  - 字段:
    - `effect_id: UUID`
    - `semantic_command_id: UUID`
    - `kind: WorkspaceEffectKind`
    - `repository_identity_digest: str`
    - `agent_id: UUID | None`
    - `run_id: UUID`
    - `resource_nonce: UUID`
    - `resource_ref: str`
    - `base_digest: str | None`
    - `input_digest: str`
    - `precondition_digest: str`
    - `expected_postcondition_digest: str`
    - `intended_at: datetime`
    - `state: WorkspaceEffectState`
    - `version: int`
    - `last_event_id: UUID`
    - `claim_epoch: int = 0`
    - `claim_token: UUID | None = None`
    - `owner_id: str | None = None`
    - `result_kind: WorkspaceEffectResultKind | None = None`
    - `result_code: str | None = None`
    - `exit_code: int | None = None`
    - `postcondition_digest: str | None = None`
    - `evidence_digest: str | None = None`
    - `error_code: str | None = None`
    - `uncertainty_code: str | None = None`
    - `unknown_event_id: UUID | None = None`
    - `resolved_by_event_id: UUID | None = None`
    - `result: Mapping[str, Any] | None = None`

- **class `WorkspaceEffectWrite`**
  - A transition result including the durable idempotency receipt.
  - 字段:
    - `record: WorkspaceEffectRecord`
    - `receipt: AppendReceipt`

- **`workspace_effect_id(kind: WorkspaceEffectKind, semantic_command_id: UUID) -> UUID`**
- **`workspace_resource_nonce(effect_id: UUID) -> UUID`**
- **class `WorkspaceEffectStore`**
  - Event-sourced workspace-effect state machine with exact CAS fences.
  - 方法:
    - `intend(self, *, semantic_command_id: UUID, kind: WorkspaceEffectKind, repository_identity_digest: str, agent_id: UUID | None, run_id: UUID, resource_ref: str, base_digest: str | None, input_digest: str, precondition_digest: str, expected_postcondition_digest: str) -> WorkspaceEffectWrite`
    - `claim(self, effect_id: UUID, *, expected_version: int, owner_id: str) -> WorkspaceEffectWrite`
    - `record_applied(self, effect_id: UUID, *, expected_version: int, claim_epoch: int, claim_token: UUID, result_kind: WorkspaceEffectResultKind, result_code: str, exit_code: int | None, postcondition_digest: str, evidence_digest: str, result: Mapping[str, Any] | None=None) -> WorkspaceEffectWrite`
    - `record_failed_before_effect(self, effect_id: UUID, *, expected_version: int, claim_epoch: int, claim_token: UUID, error_code: str, evidence_digest: str) -> WorkspaceEffectWrite`
    - `record_outcome_unknown(self, effect_id: UUID, *, expected_version: int, claim_epoch: int, claim_token: UUID, uncertainty_code: str, evidence_digest: str | None) -> WorkspaceEffectWrite`
    - `resolve_unknown(self, effect_id: UUID, *, expected_version: int, claim_epoch: int, claim_token: UUID, unknown_event_id: UUID, resolved_state: WorkspaceEffectResolvedState, result_kind: WorkspaceEffectResultKind | None, reconciler_principal: str, evidence_kind: str, evidence_digest: str) -> WorkspaceEffectWrite`
    - `load(self, effect_id: UUID) -> WorkspaceEffectRecord | None`
    - `_claimed_terminal(self, current: WorkspaceEffectRecord, expected_version: int, claim_epoch: int, claim_token: UUID, command_id: UUID, action: str, event_type: str, schema_version: int, payload: Mapping[str, Any]) -> WorkspaceEffectWrite`
    - `_transition(self, current: WorkspaceEffectRecord, *, expected_version: int, command_id: UUID, action: str, event_type: str, payload: Mapping[str, Any], required_state: WorkspaceEffectState) -> WorkspaceEffectWrite`
    - `_append(self, effect_id: UUID, *, expected_version: int, command_id: UUID, action: str, event_type: str, schema_version: int, payload: Mapping[str, Any], fingerprint: str | None=None) -> WorkspaceEffectWrite`
    - `_idempotent(self, command_id: UUID, fingerprint: str, effect_id: UUID) -> WorkspaceEffectWrite | None`
    - `_require(self, effect_id: UUID) -> WorkspaceEffectRecord`
    - `_require_expected(current: WorkspaceEffectRecord, expected: int) -> None`
    - `_require_claim(current: WorkspaceEffectRecord, claim_epoch: int, claim_token: UUID) -> None`
    - `_stream(effect_id: UUID) -> StreamId`
    - `_read_all(self, stream: StreamId) -> tuple[StoredEvent, ...]`
    - `_now(self) -> datetime`


### `workspace/integration.py`

> D12 artifact acceptance, integration worktree, and gated delivery.

- **class `Artifact`**
  - 字段:
    - `agent_id: UUID`
    - `run_id: UUID`
    - `base_commit: str`
    - `head_commit: str`
    - `diff: str`
    - `test_evidence: str`
    - `image_digest: str`
  - 方法:
    - `digest(self) -> str`

- **class `ArtifactIntegrator`**
  - Integrate accepted artifacts, retest, then gate delivery on HEAD hash.
  - 方法:
    - `accept(self, artifact: Artifact, *, expected_run_id: UUID, expected_base_commit: str) -> str`
    - `integrate(self, artifacts: list[Artifact], *, test_argv: list[str], timeout: float=60.0) -> tuple[ContainerResult, str]` — Apply diffs serially in one integration worktree; conflicts raise.
    - `deliver(self, artifacts: list[Artifact], *, user_base_commit: str) -> None` — Apply the integrated diff set to the user workspace after HEAD gate.
    - `_apply_diff(self, worktree: Path, diff: str) -> None`
    - `_git(self, *arguments: str) -> str`

- **class `IntegrationReceiptRef`**
  - 字段:
    - `receipt_id: UUID`
    - `stream_version: int`
    - `event_id: UUID`
    - `receipt_digest: str`

- **class `DurableIntegrationResult`**
  - 字段:
    - `receipt: IntegrationReceiptRef`
    - `test_exit_code: int`
    - `test_result_kind: str`

- **class `DurableArtifactIntegrator`**
  - I7 crash-resumable package integration and receipt-only delivery.
  - 方法:
    - `integrate(self, artifacts: list[ArtifactV2], *, test_argv: list[str], timeout: float=60.0, command_id: UUID) -> DurableIntegrationResult`
    - `deliver(self, receipt_ref: IntegrationReceiptRef, *, command_id: UUID) -> None`
    - `_acquire_delivery_lease(self, repository_digest: str, command_id: UUID) -> tuple[int, UUID]`
    - `_release_matching_delivery_lease(self, repository_digest: str, command_id: UUID) -> None`
    - `_release_delivery_lease(self, repository_digest: str, expected_version: int, token: UUID, command_id: UUID) -> None`
    - `_run_apply_effect(self, semantic: UUID, artifact: ArtifactV2)`
    - `_verify_test_evidence(self, artifact: ArtifactV2) -> None`
    - `_run_retest_effect(self, semantic, artifacts, argv, timeout, post)`
    - `_common_git_dir(self, root: Path) -> Path`
    - `_record_receipt(self, receipt_id: UUID, payload: dict, command_id: UUID) -> IntegrationReceiptRef`
    - `_load_receipt_head(self, receipt_id: UUID) -> IntegrationReceiptRef | None`
    - `_load_receipt(self, reference: IntegrationReceiptRef) -> dict`
    - `_git(self, *arguments: str) -> bytes`


### `workspace/store.py`

> I7 projection-only inventory for deterministic detached worktrees.

- **class `WorkspaceRecord`**
  - 字段:
    - `agent_id: UUID`
    - `run_id: UUID`
    - `worktree_path: str`
    - `base_commit: str`
    - `branch: str | None`
    - `state: str`
    - `version: int`
    - `allocation_id: UUID`
    - `resource_nonce: UUID`
    - `effect_id: UUID | None = None`
    - `legacy_unverified: bool = False`

- **class `AgentWorkspaceStore`**
  - Typed inventory projection; physical Git/filesystem work belongs to manager.
  - 方法:
    - `allocate(self, agent_id: UUID, *, run_id: UUID, worktree_path: Path, base_commit: str, branch: str | None=None, allocation_id: UUID | None=None, resource_nonce: UUID | None=None, effect_id: UUID | None=None, command_id: UUID | None=None) -> WorkspaceRecord`
    - `mark_reap_pending(self, agent_id: UUID, *, run_id: UUID, effect_id: UUID, command_id: UUID) -> WorkspaceRecord`
    - `mark_reaped(self, agent_id: UUID, *, run_id: UUID, effect_id: UUID, command_id: UUID) -> WorkspaceRecord`
    - `mark_unknown(self, agent_id: UUID, *, run_id: UUID, effect_id: UUID, command_id: UUID) -> WorkspaceRecord`
    - `reap(self, agent_id: UUID, *, run_id: UUID, reason: str) -> WorkspaceRecord` — Legacy projection transition; no physical deletion occurs here.
    - `load(self, agent_id: UUID, *, run_id: UUID | None=None) -> WorkspaceRecord | None`
    - `list(self, agent_id: UUID) -> tuple[WorkspaceRecord, ...]`
    - `_transition(self, agent_id: UUID, run_id: UUID, state: str, effect_id: UUID, command_id: UUID) -> WorkspaceRecord`
    - `_inside_managed(self, path: Path) -> bool`
    - `_read_all(self, stream: StreamId) -> tuple`


### `workspace/subprocesses.py`

> Bounded subprocess pipe transport for workspace Git/container helpers.

- **class `BoundedProcessResult`**
  - 字段:
    - `returncode: int`
    - `stdout: bytes`
    - `stderr: bytes`

- **`run_bounded(arguments: Sequence[str], *, cwd: Path, environment: Mapping[str, str] | None, timeout: float, output_limit: int, failure_code: str, output_limit_code: str, input_bytes: bytes | None=None) -> BoundedProcessResult`**

### `workspace/worktree.py`

> I7 effect-ledgered detached Git worktree orchestration.

- **class `WorktreeManager`**
  - Brackets every physical add/remove with an exact workspace effect.
  - 方法:
    - `user_worktree_dirty(self) -> bool`
    - `create(self, agent_id: UUID, *, run_id: UUID, base_commit: str, branch: str | None=None, write_agent: bool, semantic_command_id: UUID | None=None) -> Path`
    - `_create(self, agent_id: UUID, *, run_id: UUID, base_commit: str, branch: str | None=None, write_agent: bool, semantic_command_id: UUID | None=None) -> Path` — Create a detached worktree; ``branch`` is ignored legacy input.
    - `reap(self, agent_id: UUID, *, run_id: UUID, reason: str='completed', semantic_command_id: UUID | None=None)`
    - `_reap(self, agent_id: UUID, *, run_id: UUID, reason: str='completed', semantic_command_id: UUID | None=None)`
    - `reconcile(self, effect_id: UUID, *, expected_version: int, base_commit: str)` — Inspect one interrupted effect; never rerun an uncertain Git command.
    - `_reconcile(self, record, base_commit)`
    - `_safe_target(self, resource_ref: str) -> Path`
    - `_operation_lock(self)`
    - `diff(self, agent_id: UUID, *, base_commit: str, run_id: UUID | None=None) -> str`
    - `_record_create_failure(self, claimed, target: Path) -> None`
    - `_verify_active(self, target: Path, base_commit: str) -> None`
    - `_registered(self, target: Path) -> bool`
    - `_metadata_present(self, target: Path) -> bool` — Do not mistake an incomplete/prunable Git admin entry for absence.
    - `_metadata_pointer(directory: Path) -> Path`
    - `_git(self, *arguments: str) -> bytes`
    - `_run_bounded(self, arguments: list[str], *, environment: dict[str, str]) -> bytes`


## `approval_service`

### `approval_service.py`

> D9 durable approvals and atomic authorization-to-ledger claims.

- **class `ApprovalStatus`** (StrEnum)

- **class `ApprovalError`** (PolicyError)
  - Stable, content-free approval failure.

- **class `ApprovalWaiting`** (ApprovalError)
  - The Turn was durably suspended before a tool claim.

- **class `ApprovalDenied`** (ApprovalError)
  - The exact request was durably denied.

- **class `ApprovalRecord`**
  - 字段:
    - `subject_id: UUID`
    - `request_id: UUID`
    - `thread_id: UUID`
    - `turn_id: UUID`
    - `execution_id: UUID`
    - `model_turn_id: UUID`
    - `call_id: str`
    - `interrupt_id: UUID`
    - `action_digest: str`
    - `principal_id: str`
    - `policy_version: str`
    - `capability_scope: tuple[str, ...]`
    - `status: ApprovalStatus`
    - `expires_at: datetime`
    - `version: int`
    - `single_use: bool = True`
    - `consumed_run_id: UUID | None = None`
    - `claim_token: UUID | None = None`

- **class `ApprovalService`**
  - Coordinates approval, durable budget and D7 ledger streams.
  - 方法:
    - `load(self, subject_id: UUID) -> ApprovalRecord | None`
    - `require_grant(self, record: ToolExecutionRecord, action: ResolvedAction, verdict: PolicyVerdict, *, context: ToolExecutionContext, prompt: str, now: datetime | None=None) -> ApprovalRecord | None` — Return a matching grant, deny, or durably suspend the running Turn.
    - `resolve(self, pending: ApprovalRecord, approved: bool, *, expected_approval_version: int, expected_turn_version: int, interrupt_id: UUID, approver_principal_id: str, command_id: UUID | None=None, now: datetime | None=None) -> ApprovalRecord` — Resolve one exact request and queue its Turn in the same transaction.
    - `claim(self, record: ToolExecutionRecord, action: ResolvedAction, verdict: PolicyVerdict, approval: ApprovalRecord | None, *, context: ToolExecutionContext, now: datetime | None=None) -> ToolExecutionRecord` — Reserve budget, consume a grant and claim D7 in one append batch.
    - `begin_execution(self, record: ToolExecutionRecord, action: ResolvedAction, *, context: ToolExecutionContext) -> None` — Persist a one-shot gate for one exact claim before handler entry.
    - `_observed_at(self, explicit: datetime | None) -> datetime`
    - `_request(self, record: ToolExecutionRecord, action: ResolvedAction, *, context: ToolExecutionContext, prompt: str, previous: ApprovalRecord | None, now: datetime) -> None`
    - `require_escalated_grant(self, record, action: ResolvedAction, *, context: ToolExecutionContext, signal_payload: Mapping[str, Any], escalated_payload: Mapping[str, Any], prompt: str, security_head: int | None=None) -> dict` — RT/J J2: five-event atomic escalation command (plan §3 J2).
    - `_validate(self, record: ToolExecutionRecord, action: ResolvedAction, verdict: PolicyVerdict, context: ToolExecutionContext) -> None`
    - `_load_turn(self, turn_id: UUID) -> TurnState`
    - `_claim_matches(self, record: ToolExecutionRecord, action: ResolvedAction) -> bool`
    - `_read_all(self, stream: StreamId) -> tuple`


## `policy`

### `policy.py`

> D9 policy-domain types, canonical action identity, and safe preflight gates.

- **class `PolicyError`** (RuntimeError)
  - Content-free D9 contract failure with a stable machine-readable code.

- **class `Decision`** (StrEnum)

- **class `ActionKind`** (StrEnum)

- **class `SideEffectClass`** (StrEnum)
  - Policy-local risk classification, kept independent of ledger imports.

- **class `Principal`**
  - Local policy identity and its already-granted capability scopes.
  - 字段:
    - `principal_id: str`
    - `scopes: tuple[str, ...] = ()`
    - `parent_principal_id: str | None = None`
  - 方法:
    - `narrow(self, child_principal_id: str, requested_scopes: Sequence[str]) -> 'Principal'` — Create a child whose authority is the parent/request intersection.
    - `to_document(self) -> dict[str, JsonValue]`

- **`narrow_child_principal(parent: Principal, *, child_principal_id: str, requested_scopes: Sequence[str]) -> Principal`**
- **class `ResolvedResource`**
  - A caller-resolved resource identity included verbatim in action identity.
  - 字段:
    - `resource_kind: str`
    - `requested: str`
    - `resolved: str`
    - `identity: str`
    - `metadata: tuple[tuple[str, str], ...] = ()`
  - 方法:
    - `to_document(self) -> dict[str, JsonValue]`

- **class `Origin`**
  - 字段:
    - `scheme: str`
    - `host: str`
    - `port: int`
  - 方法:
    - `authority(self) -> str`
    - `to_document(self) -> dict[str, JsonValue]`

- **class `NetworkTarget`**
  - Normalized HTTPS destination pinned to an all-global DNS answer set.
  - 字段:
    - `normalized_url: str`
    - `origin: Origin`
    - `resolved_addresses: tuple[str, ...]`
    - `via_proxy: bool`
  - 方法:
    - `resolution_digest(self) -> str`
    - `to_document(self) -> dict[str, JsonValue]`

- **class `CredentialScope`**
  - Credential identifiers and approved recipients; never credential values.
  - 字段:
    - `scope_id: str`
    - `origins: tuple[Origin, ...] = ()`
    - `server_ids: tuple[str, ...] = ()`
  - 方法:
    - `permits_origin(self, origin: Origin) -> bool`
    - `permits_server(self, server_id: str) -> bool`
    - `to_document(self) -> dict[str, JsonValue]`

- **`normalize_https_url(url: str) -> str`** — Return a strict ASCII-only canonical HTTPS URL without doing DNS I/O.
- **`origin_from_url(url: str) -> Origin`**
- **`resolve_network_target(url: str, resolver: DNSResolver, *, previous: NetworkTarget | None=None, via_proxy: bool=False) -> NetworkTarget`** — Resolve through an injected resolver and fail closed on unsafe/drifted DNS.
- **`resolve_redirect(current: NetworkTarget, location: str, resolver: DNSResolver, *, credential_scope: CredentialScope | None=None, via_proxy: bool=True) -> NetworkTarget`** — Resolve one redirect and forbid sending credentials to another origin.
- **class `ResourceBudgetLimits`**
  - Trusted preflight ceilings; Docker must independently enforce runtime limits.
  - 字段:
    - `max_cpus: float = 1.0`
    - `max_memory_bytes: int = 256 * 1024 * 1024`
    - `max_pids: int = 64`
    - `max_tmpfs_bytes: int = 64 * 1024 * 1024`
    - `max_duration_seconds: float = 60.0`
    - `max_stdout_bytes: int = 256000`
    - `max_stderr_bytes: int = 256000`
    - `max_network_bytes: int = 0`
    - `max_tool_calls: int = 1`
    - `max_subagents: int = 0`
  - 方法:
    - `preflight(self, request: 'ResourceRequest') -> None`
    - `to_document(self) -> dict[str, JsonValue]`

- **class `ResourceRequest`**
  - 字段:
    - `cpus: float = 1.0`
    - `memory_bytes: int = 256 * 1024 * 1024`
    - `pids: int = 64`
    - `tmpfs_bytes: int = 64 * 1024 * 1024`
    - `duration_seconds: float = 60.0`
    - `stdout_bytes: int = 256000`
    - `stderr_bytes: int = 256000`
    - `network_bytes: int = 0`
    - `tool_calls: int = 1`
    - `subagents: int = 0`
  - 方法:
    - `to_document(self) -> dict[str, JsonValue]`

- **`preflight_resource_request(request: ResourceRequest, limits: ResourceBudgetLimits) -> None`**
- **class `ActionRequest`**
  - Unresolved local action proposal; it grants no authority by itself.
  - 字段:
    - `kind: ActionKind`
    - `tool_name: str`
    - `arguments_json: str`
    - `principal: Principal`
    - `side_effect_class: SideEffectClass`
    - `sandbox_profile_id: str`
    - `resource_references: tuple[str, ...] = ()`
    - `resolved_resources: tuple[ResolvedResource, ...] = ()`
    - `network_url: str | None = None`
    - `credential_scope: CredentialScope | None = None`
    - `resource_request: ResourceRequest | None = None`
    - `mcp_claimed_capabilities: tuple[str, ...] = ()`
    - `mcp_server_id: str | None = None`
    - `mcp_session_generation: int | None = None`
    - `mcp_schema_hash: str | None = None`
    - `mcp_binding_digest: str | None = None`
    - `mcp_server_identity_digest: str | None = None`

- **class `ResolvedAction`**
  - Immutable, fully canonical action evaluated and bound by an approval.
  - 字段:
    - `kind: ActionKind`
    - `tool_name: str`
    - `canonical_arguments_json: str`
    - `principal: Principal`
    - `side_effect_class: SideEffectClass`
    - `sandbox_profile_id: str`
    - `policy_version: str`
    - `resources: tuple[ResolvedResource, ...] = ()`
    - `network_target: NetworkTarget | None = None`
    - `credential_scope: CredentialScope | None = None`
    - `resource_request: ResourceRequest | None = None`
    - `mcp_claimed_capabilities: tuple[str, ...] = ()`
    - `mcp_server_id: str | None = None`
    - `mcp_session_generation: int | None = None`
    - `mcp_schema_hash: str | None = None`
    - `mcp_binding_digest: str | None = None`
    - `mcp_server_identity_digest: str | None = None`
  - 方法:
    - `action_digest(self) -> str`
    - `to_document(self) -> dict[str, JsonValue]`

- **`action_request_from_tool_call(*, kind: ActionKind, tool_name: str, arguments_json: str, principal: Principal, side_effect_class: SideEffectClass, sandbox_profile_id: str, resource_references: Sequence[str]=(), network_url: str | None=None, credential_scope: CredentialScope | None=None, resource_request: ResourceRequest | None=None, mcp_claimed_capabilities: Sequence[str]=(), mcp_server_id: str | None=None, mcp_session_generation: int | None=None, mcp_schema_hash: str | None=None, mcp_binding_digest: str | None=None, mcp_server_identity_digest: str | None=None) -> ActionRequest`** — Adapter-friendly constructor without importing Provider/Registry types.
- **`resolve_action_request(request: ActionRequest, *, policy_version: str, resource_resolver: ResourceResolver | None=None, dns_resolver: DNSResolver | None=None, previous_network_target: NetworkTarget | None=None, via_proxy: bool=False) -> ResolvedAction`** — Resolve an action with injected pure boundaries and compute canonical identity.
- **`canonical_arguments(value: str | Mapping[str, Any]) -> str`** — Canonicalize a strict JSON object with duplicate/NaN/surrogate rejection.
- **`compute_action_digest(action: ResolvedAction) -> str`**
- **class `PolicyRule`**
  - A local administrator rule. Empty selectors mean wildcard.
  - 字段:
    - `rule_id: str`
    - `decision: Decision`
    - `action_kinds: tuple[ActionKind, ...] = ()`
    - `tool_names: tuple[str, ...] = ()`
    - `principal_ids: tuple[str, ...] = ()`
    - `required_scopes: tuple[str, ...] = ()`
    - `side_effect_classes: tuple[SideEffectClass, ...] = ()`
    - `sandbox_profile_ids: tuple[str, ...] = ()`
    - `allowed_origins: tuple[Origin, ...] = ()`
  - 方法:
    - `matches(self, action: ResolvedAction) -> bool`

- **class `PolicyVerdict`**
  - 字段:
    - `decision: Decision`
    - `code: str`
    - `policy_version: str`
    - `action_digest: str`
    - `matched_rule_ids: tuple[str, ...] = ()`

- **class `PolicyEngine`**
  - Deterministic local authority: DENY > ASK > ALLOW, otherwise DENY.
  - 字段:
    - `policy_version: str`
    - `rules: tuple[PolicyRule, ...] = ()`
    - `network_enabled: bool = False`
    - `proxy_required: bool = True`
    - `allowed_origins: tuple[Origin, ...] = ()`
    - `resource_limits: ResourceBudgetLimits | None = None`
  - 方法:
    - `evaluate(self, action: ResolvedAction) -> PolicyVerdict`
    - `_verdict(self, decision: Decision, code: str, digest: str, rules: tuple[str, ...]=()) -> PolicyVerdict`

