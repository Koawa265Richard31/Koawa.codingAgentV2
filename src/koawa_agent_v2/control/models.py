"""由持久化事件重建的不可变 Thread/Turn 领域状态。

本文件是 D1 的纯领域层：它不执行 SQL，也不主动保存状态，而是把按版本排序的
事件依次交给 reducer，确定性地算出当前 ``ThreadState``/``TurnState``。
重放时仍严格校验事件顺序和状态迁移，因此损坏或不可能出现的历史不会被静默接受。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping, Protocol
from uuid import UUID


# Thread 事件：创建会话、占用/释放当前 Turn、归档会话。
THREAD_CREATED = "thread.created.v1"
THREAD_TURN_ATTACHED = "thread.turn-attached.v1"
THREAD_TURN_DETACHED = "thread.turn-detached.v1"
THREAD_ARCHIVED = "thread.archived.v1"

# Turn 事件：覆盖排队、运行、等待、恢复和全部终态。
TURN_CREATED = "turn.created.v1"
TURN_STARTED = "turn.started.v1"
TURN_WAITING_FOR_INPUT = "turn.waiting-for-input.v1"
TURN_WAITING_FOR_APPROVAL = "turn.waiting-for-approval.v1"
TURN_PAUSED = "turn.paused.v1"
TURN_RECOVERY_QUEUED = "turn.recovery-queued.v1"
TURN_STALE_RUN_REQUEUED = "turn.stale-run-requeued.v1"
TURN_COMPLETED = "turn.completed.v1"
TURN_FAILED = "turn.failed.v1"
TURN_CANCELLED = "turn.cancelled.v1"
TURN_TIMED_OUT = "turn.timed-out.v1"


class DomainError(RuntimeError):
    """可确定复现的领域失败基类，与数据库/网络故障区分。"""


class AggregateNotFound(DomainError):
    """事件序列为空、无法重建指定 Thread/Turn 时抛出。"""


class InvalidTransition(DomainError):
    """应用命令试图执行非法状态迁移时抛出（主要由 Runtime 使用）。"""


class CorruptEventStream(DomainError):
    """持久化历史存在版本断档、非法 payload 或不可能迁移时抛出。"""


class ThreadStatus(StrEnum):
    """会话生命周期；归档是不可逆终态。"""

    OPEN = "open"
    ARCHIVED = "archived"


class TurnStatus(StrEnum):
    """一次用户任务的控制面状态。"""

    # 已入队，等待某个 Worker 以新 run_id 启动。
    QUEUED = "queued"
    # 当前存在合法执行者；Worker 写终态时还要通过 run_id fencing。
    RUNNING = "running"
    # 两种可持久恢复的 interrupt：分别等待文本输入和布尔审批。
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    # 人工/系统暂停；与 interrupt 不同，它没有待回答的问题。
    PAUSED = "paused"
    # 以下四种都是不可继续迁移的终态。
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class InterruptKind(StrEnum):
    """待处理中断期望的响应类型。"""

    INPUT = "input"
    APPROVAL = "approval"


# 集中定义终态，保证 ``is_terminal``、detach 校验等位置使用同一规则。
TERMINAL_TURN_STATUSES = frozenset(
    {
        TurnStatus.COMPLETED,
        TurnStatus.FAILED,
        TurnStatus.CANCELLED,
        TurnStatus.TIMED_OUT,
    }
)
# 只有等待输入、等待审批和暂停状态可通过 recovery-queued 回到队列。
RESUMABLE_TURN_STATUSES = frozenset(
    {
        TurnStatus.WAITING_FOR_INPUT,
        TurnStatus.WAITING_FOR_APPROVAL,
        TurnStatus.PAUSED,
    }
)


@dataclass(frozen=True, slots=True)
class PendingInterrupt:
    """已持久化、即使进程退出也能继续等待的交互点。"""

    # 恢复请求必须回传并匹配此 ID，避免回答了已经过期的提问。
    interrupt_id: UUID
    # 决定 response 应为非空字符串还是布尔值。
    kind: InterruptKind
    # 展示给用户/审批者的问题；不包含响应。
    prompt: str
    # D9 durable approval 的请求身份；None 表示可重放的 D1 legacy interrupt。
    approval_request_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ThreadState:
    """一个长期会话在指定事件流版本上的不可变快照。"""

    thread_id: UUID
    # 工作区引用（通常是仓库路径），Thread 的任务都围绕它执行。
    workspace_ref: str
    status: ThreadStatus
    # D1 强制同一 Thread 同时最多有一个未终止 Turn。
    active_turn_id: UUID | None
    # 已应用的最后一个 stream_version；也是下一命令的 expected_version 基准。
    version: int
    created_at: datetime
    # 最后一条已应用事件的 occurred_at。
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TurnState:
    """一次用户任务在指定事件流版本上的不可变快照。"""

    turn_id: UUID
    thread_id: UUID
    # 用户原始目标；D1 把它作为创建事件的一部分保存，而非仅存内存。
    user_input: str
    status: TurnStatus
    # 真正开始执行的次数；每次 QUEUED -> RUNNING 必须恰好加一。
    attempt: int
    # 最近一次启动 Run 的 fencing token；等待/暂停时保留，恢复入队或终止时清空。
    current_run_id: UUID | None
    # 等待输入/审批时存在；恢复成功后清空。
    pending_interrupt: PendingInterrupt | None
    # 最近一次 interrupt 的答复，供下一次 Run 恢复上下文；暂停恢复时为空。
    last_resume_response: str | bool | None
    # 响应来源和事件版本形成稳定幂等身份，避免重启时把同一答复重复注入上下文。
    last_resume_interrupt_id: UUID | None
    last_resume_version: int | None
    # 成功摘要与失败原因分字段保存，避免把不同终态语义混在一起。
    outcome: str | None
    error: str | None
    # 已重放的最后一个流版本。
    version: int
    created_at: datetime
    updated_at: datetime
    # D9 字段追加在旧构造签名末尾并给默认值，保持外部旧代码兼容。
    # Durable approval 不把 bool 混入 response，而是保存独立身份与结论。
    last_resume_approval_request_id: UUID | None = None
    last_resume_approval_decision: str | None = None

    @property
    def is_terminal(self) -> bool:
        """Turn 是否已经进入不可继续迁移的终态。"""

        return self.status in TERMINAL_TURN_STATUSES


class StoredEventLike(Protocol):
    """聚合重放真正需要的最小事件视图，与具体 EventStore 实现解耦。"""

    # reducer 不关心 global_position、commit_id 等存储细节。
    event_type: str
    schema_version: int
    stream_version: int
    occurred_at: datetime
    payload: Mapping[str, Any]


def rebuild_thread(thread_id: UUID, events: Iterable[StoredEventLike]) -> ThreadState:
    """从头重放 Thread 事件，并拒绝版本断档或非法历史迁移。

    reducer 没有外部副作用：同一组有序事件总会生成同一个状态，这是进程重启后
    能可靠恢复的基础。空流不是“默认新对象”，而是明确的不存在错误。
    """

    state: ThreadState | None = None
    for event in events:
        # stream_version 必须从 0 开始逐个递增；否则重放得到的状态不可信。
        _require_next_version(state.version if state else -1, event.stream_version)
        _require_schema_version(event)
        state = _apply_thread_event(thread_id, state, event)
    if state is None:
        raise AggregateNotFound(f"thread {thread_id} does not exist")
    return state


def rebuild_turn(turn_id: UUID, events: Iterable[StoredEventLike]) -> TurnState:
    """从头重放 Turn 事件，得到当前运行/等待/终止状态。

    这里恢复的是业务状态而非 Python 调用栈；可恢复 Turn 会先回到 QUEUED，
    再由新 Worker 产生新 run_id 和新 attempt。
    """

    state: TurnState | None = None
    for event in events:
        # 每一条事件都必须紧接前一版本，避免漏事件后仍计算出“看似合理”的状态。
        _require_next_version(state.version if state else -1, event.stream_version)
        _require_schema_version(event)
        state = _apply_turn_event(turn_id, state, event)
    if state is None:
        raise AggregateNotFound(f"turn {turn_id} does not exist")
    return state


def _apply_thread_event(
    thread_id: UUID,
    state: ThreadState | None,
    event: StoredEventLike,
) -> ThreadState:
    """把一条 Thread 事件归约到旧状态，返回新的不可变状态。"""

    if event.event_type == THREAD_CREATED:
        # 创建事件既是身份锚点，也必须是 stream version 0 的第一条事实。
        if state is not None:
            raise CorruptEventStream("thread creation must be the first event")
        _require_uuid(event.payload, "thread_id", thread_id)
        workspace_ref = _require_text(event.payload, "workspace_ref")
        return ThreadState(
            thread_id=thread_id,
            workspace_ref=workspace_ref,
            status=ThreadStatus.OPEN,
            active_turn_id=None,
            version=event.stream_version,
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
        )

    if state is None:
        raise CorruptEventStream("thread stream must begin with thread.created.v1")
    # 归档后禁止任何变化，让归档成为稳定的会话终态。
    if state.status is ThreadStatus.ARCHIVED:
        raise CorruptEventStream("an archived thread cannot transition")

    if event.event_type == THREAD_TURN_ATTACHED:
        # active_turn_id 是会话级互斥锁：一个 Thread 不并行占用两个前台 Turn。
        if state.active_turn_id is not None:
            raise CorruptEventStream(
                f"thread already has active turn {state.active_turn_id}"
            )
        return replace(
            state,
            active_turn_id=_require_uuid(event.payload, "turn_id"),
            version=event.stream_version,
            updated_at=event.occurred_at,
        )

    if event.event_type == THREAD_TURN_DETACHED:
        # 只能释放当前占用者，且只能在该 Turn 已进入终态时释放。
        turn_id = _require_uuid(event.payload, "turn_id")
        if state.active_turn_id != turn_id:
            raise CorruptEventStream(
                f"cannot detach {turn_id}; active turn is {state.active_turn_id}"
            )
        terminal_status = _require_enum(event.payload, "terminal_status", TurnStatus)
        if terminal_status not in TERMINAL_TURN_STATUSES:
            raise CorruptEventStream("a detached turn must have a terminal status")
        return replace(
            state,
            active_turn_id=None,
            version=event.stream_version,
            updated_at=event.occurred_at,
        )

    if event.event_type == THREAD_ARCHIVED:
        # 有活跃任务时归档会使任务失去归属，因此历史中不允许出现这种组合。
        if state.active_turn_id is not None:
            raise CorruptEventStream("a thread with an active turn cannot be archived")
        return replace(
            state,
            status=ThreadStatus.ARCHIVED,
            version=event.stream_version,
            updated_at=event.occurred_at,
        )

    raise CorruptEventStream(f"unknown thread event type: {event.event_type}")


def _apply_turn_event(
    turn_id: UUID,
    state: TurnState | None,
    event: StoredEventLike,
) -> TurnState:
    """把一条 Turn 事件归约到旧状态，并校验完整状态机不变量。"""

    if event.event_type == TURN_CREATED:
        # 新 Turn 从 QUEUED/attempt=0 开始，还没有合法 Worker 或 run_id。
        if state is not None:
            raise CorruptEventStream("turn creation must be the first event")
        _require_uuid(event.payload, "turn_id", turn_id)
        return TurnState(
            turn_id=turn_id,
            thread_id=_require_uuid(event.payload, "thread_id"),
            user_input=_require_text(event.payload, "user_input"),
            status=TurnStatus.QUEUED,
            attempt=0,
            current_run_id=None,
            pending_interrupt=None,
            last_resume_response=None,
            last_resume_interrupt_id=None,
            last_resume_approval_request_id=None,
            last_resume_approval_decision=None,
            last_resume_version=None,
            outcome=None,
            error=None,
            version=event.stream_version,
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
        )

    if state is None:
        raise CorruptEventStream("turn stream must begin with turn.created.v1")
    # 所有终态都不可逆；重放若发现后续事件，说明日志本身已损坏。
    if state.is_terminal:
        raise CorruptEventStream(f"terminal turn {turn_id} cannot transition")

    if event.event_type == TURN_STARTED:
        # 只有队列中的 Turn 能启动；每一次启动都形成新的执行世代。
        _require_status(state, TurnStatus.QUEUED)
        attempt = _require_int(event.payload, "attempt")
        if attempt != state.attempt + 1:
            raise CorruptEventStream(
                f"turn attempt must increment by one; got {attempt} after {state.attempt}"
            )
        return replace(
            state,
            status=TurnStatus.RUNNING,
            attempt=attempt,
            current_run_id=_require_uuid(event.payload, "run_id"),
            pending_interrupt=None,
            last_resume_response=None,
            last_resume_interrupt_id=None,
            last_resume_approval_request_id=None,
            last_resume_approval_decision=None,
            last_resume_version=None,
            version=event.stream_version,
            updated_at=event.occurred_at,
        )

    if event.event_type == TURN_STALE_RUN_REQUEUED:
        _require_status(state, TurnStatus.RUNNING)
        if _require_uuid(event.payload, "abandoned_run_id") != state.current_run_id:
            raise CorruptEventStream("stale requeue run mismatch")
        return replace(state, status=TurnStatus.QUEUED, current_run_id=None,
                       pending_interrupt=None, version=event.stream_version,
                       updated_at=event.occurred_at)

    if event.event_type in (TURN_WAITING_FOR_INPUT, TURN_WAITING_FOR_APPROVAL):
        # 只有当前正在运行的 Worker 才能把任务挂起为一个持久 interrupt。
        _require_status(state, TurnStatus.RUNNING)
        kind = (
            InterruptKind.INPUT
            if event.event_type == TURN_WAITING_FOR_INPUT
            else InterruptKind.APPROVAL
        )
        approval_request_id = None
        if event.event_type == TURN_WAITING_FOR_APPROVAL:
            raw_approval_request_id = event.payload.get("approval_request_id")
            if raw_approval_request_id is not None:
                approval_request_id = _require_uuid(
                    event.payload,
                    "approval_request_id",
                )
        interrupt = PendingInterrupt(
            interrupt_id=_require_uuid(event.payload, "interrupt_id"),
            kind=kind,
            prompt=_require_text(event.payload, "prompt"),
            approval_request_id=approval_request_id,
        )
        status = (
            TurnStatus.WAITING_FOR_INPUT
            if kind is InterruptKind.INPUT
            else TurnStatus.WAITING_FOR_APPROVAL
        )
        return replace(
            state,
            status=status,
            pending_interrupt=interrupt,
            last_resume_response=None,
            last_resume_interrupt_id=None,
            last_resume_approval_request_id=None,
            last_resume_approval_decision=None,
            last_resume_version=None,
            version=event.stream_version,
            updated_at=event.occurred_at,
        )

    if event.event_type == TURN_PAUSED:
        # pause 可以发生在尚未启动或正在运行时，但不会产生待回答的 interrupt。
        if state.status not in (TurnStatus.QUEUED, TurnStatus.RUNNING):
            raise CorruptEventStream(f"cannot pause a turn in {state.status.value}")
        return replace(
            state,
            status=TurnStatus.PAUSED,
            pending_interrupt=None,
            last_resume_response=None,
            last_resume_interrupt_id=None,
            last_resume_approval_request_id=None,
            last_resume_approval_decision=None,
            last_resume_version=None,
            version=event.stream_version,
            updated_at=event.occurred_at,
        )

    if event.event_type == TURN_RECOVERY_QUEUED:
        # Resume 不直接伪造 RUNNING：先进入队列，之后由 start 产生新 run_id。
        if state.status not in RESUMABLE_TURN_STATUSES:
            raise CorruptEventStream(f"cannot resume a turn in {state.status.value}")
        response = event.payload.get("response")
        approval_request_id = None
        approval_decision = None
        if state.pending_interrupt is not None:
            # interrupt_id 防止迟到响应误答了新一轮问题；响应类型由 kind 决定。
            supplied = _require_uuid(event.payload, "interrupt_id")
            if supplied != state.pending_interrupt.interrupt_id:
                raise CorruptEventStream("resume interrupt does not match pending interrupt")
            if state.pending_interrupt.kind is InterruptKind.INPUT:
                if not isinstance(response, str) or not response.strip():
                    raise CorruptEventStream(
                        "an input interrupt must be resumed with non-empty text"
                    )
                if (
                    event.payload.get("approval_request_id") is not None
                    or event.payload.get("approval_decision") is not None
                ):
                    raise CorruptEventStream(
                        "an input interrupt cannot carry durable approval fields"
                    )
            elif state.pending_interrupt.approval_request_id is not None:
                # D9 durable approval 由 ApprovalService 结算：request 身份必须与
                # interrupt 精确绑定，且结论不再伪装成 D1 的 bool response。
                approval_request_id = _require_uuid(
                    event.payload,
                    "approval_request_id",
                )
                if approval_request_id != state.pending_interrupt.approval_request_id:
                    raise CorruptEventStream(
                        "resume approval request does not match pending approval request"
                    )
                approval_decision = event.payload.get("approval_decision")
                if approval_decision not in ("granted", "denied"):
                    raise CorruptEventStream(
                        "a durable approval decision must be 'granted' or 'denied'"
                    )
                if response is not None:
                    raise CorruptEventStream(
                        "a durable approval resolution cannot carry a legacy response"
                    )
            else:
                # 旧 v1 approval 事件仍可重放；bool（包括 True）只保留为 legacy
                # response，不会被提升为 durable grant。
                if not isinstance(response, bool):
                    raise CorruptEventStream(
                        "a legacy approval interrupt must be resumed with a boolean decision"
                    )
                if (
                    event.payload.get("approval_request_id") is not None
                    or event.payload.get("approval_decision") is not None
                ):
                    raise CorruptEventStream(
                        "a legacy approval interrupt cannot carry durable approval fields"
                    )
        elif event.payload.get("interrupt_id") is not None:
            # 普通 pause 没有要确认的中断，因此不能携带虚构的响应信息。
            raise CorruptEventStream("a paused turn has no interrupt to acknowledge")
        elif response is not None:
            raise CorruptEventStream("a paused turn has no interrupt response")
        elif (
            event.payload.get("approval_request_id") is not None
            or event.payload.get("approval_decision") is not None
        ):
            raise CorruptEventStream("a paused turn has no durable approval resolution")
        return replace(
            state,
            status=TurnStatus.QUEUED,
            current_run_id=None,
            pending_interrupt=None,
            last_resume_response=response,
            last_resume_interrupt_id=(
                state.pending_interrupt.interrupt_id
                if state.pending_interrupt is not None
                else None
            ),
            last_resume_approval_request_id=approval_request_id,
            last_resume_approval_decision=approval_decision,
            last_resume_version=event.stream_version,
            version=event.stream_version,
            updated_at=event.occurred_at,
        )

    if event.event_type == TURN_COMPLETED:
        # 正常成功/失败必须由 RUNNING 执行结束；run_id 的执行者校验在 Runtime 层做。
        _require_status(state, TurnStatus.RUNNING)
        return _terminal_state(
            state,
            event,
            TurnStatus.COMPLETED,
            outcome=_require_text(event.payload, "summary"),
        )

    if event.event_type == TURN_FAILED:
        _require_status(state, TurnStatus.RUNNING)
        return _terminal_state(
            state,
            event,
            TurnStatus.FAILED,
            error=_require_text(event.payload, "error"),
        )

    if event.event_type == TURN_CANCELLED:
        # 取消和超时允许从任意非终态收口，用于外部控制面强制终止任务。
        return _terminal_state(
            state,
            event,
            TurnStatus.CANCELLED,
            error=_require_text(event.payload, "reason"),
        )

    if event.event_type == TURN_TIMED_OUT:
        return _terminal_state(
            state,
            event,
            TurnStatus.TIMED_OUT,
            error=_require_text(event.payload, "reason"),
        )

    raise CorruptEventStream(f"unknown turn event type: {event.event_type}")


def _terminal_state(
    state: TurnState,
    event: StoredEventLike,
    status: TurnStatus,
    *,
    outcome: str | None = None,
    error: str | None = None,
) -> TurnState:
    """统一生成终态快照，并清除不再合法的 Worker 和 interrupt 身份。"""

    return replace(
        state,
        status=status,
        current_run_id=None,
        pending_interrupt=None,
        last_resume_response=None,
        last_resume_interrupt_id=None,
        last_resume_approval_request_id=None,
        last_resume_approval_decision=None,
        last_resume_version=None,
        outcome=outcome,
        error=error,
        version=event.stream_version,
        updated_at=event.occurred_at,
    )


def _require_next_version(previous: int, current: int) -> None:
    """验证事件流版本从 0 开始连续递增，既不能跳号也不能重复。"""

    if current != previous + 1:
        raise CorruptEventStream(
            f"non-contiguous stream version: expected {previous + 1}, got {current}"
        )


def _require_schema_version(event: StoredEventLike) -> None:
    """拒绝当前 reducer 尚不会解释的事件 schema，避免错误兼容。"""

    if event.schema_version != 1:
        raise CorruptEventStream(
            f"unsupported schema version {event.schema_version} for {event.event_type}"
        )


def _require_status(state: TurnState, expected: TurnStatus) -> None:
    """声明并检查某事件的唯一合法前置状态。"""

    if state.status is not expected:
        raise CorruptEventStream(
            f"expected turn status {expected.value}, got {state.status.value}"
        )


def _require_text(payload: Mapping[str, Any], key: str) -> str:
    """从事件 payload 读取非空文本，否则把历史标记为损坏。"""

    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CorruptEventStream(f"event payload field {key!r} must be non-empty text")
    return value


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    """读取严格整数；显式排除 Python 中属于 int 子类的 bool。"""

    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CorruptEventStream(f"event payload field {key!r} must be an integer")
    return value


def _require_uuid(
    payload: Mapping[str, Any],
    key: str,
    expected: UUID | None = None,
) -> UUID:
    """从 payload 读取 UUID，并可额外验证它与聚合路径中的 ID 一致。"""

    raw = payload.get(key)
    try:
        value = raw if isinstance(raw, UUID) else UUID(str(raw))
    except (TypeError, ValueError, AttributeError) as exc:
        raise CorruptEventStream(f"event payload field {key!r} must be a UUID") from exc
    if expected is not None and value != expected:
        raise CorruptEventStream(f"event payload {key}={value} does not match {expected}")
    return value


def _require_enum(
    payload: Mapping[str, Any],
    key: str,
    enum_type: type[StrEnum],
) -> StrEnum:
    """把 payload 字符串解析为指定 StrEnum，非法值视为事件流损坏。"""

    try:
        return enum_type(payload.get(key))
    except (TypeError, ValueError) as exc:
        raise CorruptEventStream(
            f"event payload field {key!r} is not a valid {enum_type.__name__}"
        ) from exc
