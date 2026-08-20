"""D11 durable multi-agent control plane (spawn/mailbox/budget/fence)."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .graph import (
    AgentError,
    AgentGraph,
    AgentRecord,
    AgentState,
    ContextMode,
    rebuild_agent,
)
from .messages import (
    AgentMailbox,
    MessageKind,
    MessageRecord,
    MessageStatus,
    rebuild_mailbox,
)
from ..control.event_store import (
    EventMetadata,
    NewEvent,
    StreamId,
    StreamPrecondition,
    StreamWrite,
    WrongExpectedVersion,
)


@dataclass(frozen=True, slots=True)
class AgentBudgetLimits:
    max_depth: int = 4
    max_total_agents: int = 16
    max_concurrent_children: int = 4

    def __post_init__(self) -> None:
        for value in (self.max_depth, self.max_total_agents, self.max_concurrent_children):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("agent budget limits must be positive integers")


class AgentControlPlane:
    """Durable spawn/message/terminal commands with exact-version fences."""

    def __init__(
        self,
        event_store,
        *,
        limits: AgentBudgetLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for method in ("append_batch", "read_stream", "read_all"):
            if not callable(getattr(event_store, method, None)):
                raise TypeError("event_store must implement the EventStore boundary")
        self.event_store = event_store
        self.graph = AgentGraph(event_store)
        self.mailbox = AgentMailbox(event_store)
        self.limits = limits or AgentBudgetLimits()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise AgentError("agent_clock_must_be_aware")
        return value.astimezone(timezone.utc)

    def root_for(self, agent_id: UUID) -> UUID:
        current: UUID | None = agent_id
        while current is not None:
            record = self.graph.load(current)
            if record is None:
                raise AgentError("agent_missing")
            if record.parent_agent_id is None:
                return current
            current = record.parent_agent_id
        raise AgentError("agent_missing")

    def spawn_agent(
        self,
        *,
        parent_agent_id: UUID | None,
        task_id: str,
        principal_id: str,
        scopes: tuple[str, ...],
        context_mode: ContextMode = ContextMode.FRESH,
        turn_id: UUID | None = None,
        parent_run_id: UUID | None = None,
    ) -> AgentRecord:
        if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
            raise AgentError("invalid_agent_task")
        if not isinstance(principal_id, str) or not principal_id or len(principal_id) > 128:
            raise AgentError("invalid_agent_principal")
        scopes = tuple(sorted(set(scopes)))
        agent_id = uuid4()
        if parent_agent_id is not None:
            parent = self.graph.load(parent_agent_id)
            if parent is None:
                raise AgentError("parent_agent_missing")
            if parent.state in (
                AgentState.COMPLETED,
                AgentState.FAILED,
                AgentState.CANCELLED,
            ):
                raise AgentError("parent_agent_not_active")
            if self.graph.has_cycle(parent_agent_id, agent_id):
                raise AgentError("agent_spawn_cycle")
            depth = self._depth(parent_agent_id) + 1
            if depth > self.limits.max_depth:
                raise AgentError("agent_depth_exceeded")
            active_children = self._active_children(parent_agent_id)
            if len(active_children) >= self.limits.max_concurrent_children:
                raise AgentError("agent_concurrency_exceeded")
            root = self.root_for(parent_agent_id)
            budget = self._budget(root)
            if budget + 1 > self.limits.max_total_agents:
                raise AgentError("agent_total_exceeded")
        else:
            depth = 0
            root = agent_id
        observed_at = self.now()
        spawn_command_id = uuid5(
            NAMESPACE_URL,
            f"koawa-d11:spawn:{agent_id}:{task_id}",
        )
        spawned = _event(
            spawn_command_id,
            "agent-spawned",
            "agent.spawned.v1",
            {
                "agent_id": str(agent_id),
                "parent_agent_id": (
                    None if parent_agent_id is None else str(parent_agent_id)
                ),
                "task_id": task_id,
                "attempt": 1,
                "run_id": None,
                "principal_id": principal_id,
                "scopes": list(scopes),
                "context_mode": context_mode.value,
                "created_at": observed_at.isoformat(),
            },
            turn_id=turn_id,
            run_id=parent_run_id,
        )
        writes = [
            StreamWrite(StreamId("agent", agent_id), -1, (spawned,)),
        ]
        if parent_agent_id is not None:
            budget_events = self._read_all(StreamId("agent-budget", root))
            budget_version = -1 if not budget_events else budget_events[-1].stream_version
            reserved = _event(
                spawn_command_id,
                "budget-reserved",
                "budget.reserved.v1",
                {
                    "root_agent_id": str(root),
                    "child_agent_id": str(agent_id),
                    "total_agents": budget + 1,
                },
                turn_id=turn_id,
                run_id=parent_run_id,
            )
            writes.append(StreamWrite(
                StreamId("agent-budget", root), budget_version, (reserved,),
            ))
        for _ in range(3):
            try:
                self.event_store.append_batch(
                    tuple(writes),
                    idempotency_key=spawn_command_id,
                )
                break
            except WrongExpectedVersion:
                if parent_agent_id is None:
                    raise
                budget = self._budget(root)
                if budget + 1 > self.limits.max_total_agents:
                    raise AgentError("agent_total_exceeded") from None
                budget_events = self._read_all(StreamId("agent-budget", root))
                budget_version = (
                    -1 if not budget_events else budget_events[-1].stream_version
                )
                writes[-1] = StreamWrite(
                    StreamId("agent-budget", root),
                    budget_version,
                    (reserved,),
                )
        else:
            raise AgentError("agent_spawn_retry_exhausted")
        record = self.graph.load(agent_id)
        if record is None:
            raise AgentError("spawned_agent_missing")
        return record

    def start_attempt(
        self,
        agent_id: UUID,
        *,
        expected_version: int,
        lease_seconds: int = 30,
    ) -> AgentRecord:
        record = self.graph.load(agent_id)
        if record is None or record.version != expected_version:
            raise AgentError("agent_version_stale")
        if record.state not in (AgentState.CREATED, AgentState.ORPHANED):
            raise AgentError("agent_attempt_state_invalid")
        run_id = uuid4()
        attempt = record.attempt + (0 if record.state is AgentState.CREATED else 1)
        lease = self.now() + timedelta(seconds=lease_seconds)
        event_type = (
            "agent.started.v1" if record.state is AgentState.CREATED
            else "agent.taken-over.v1"
        )
        payload: dict = {
            "agent_id": str(agent_id),
            "run_id": str(run_id),
            "attempt": attempt,
            "lease_expires_at": lease.isoformat(),
        }
        if record.state is AgentState.ORPHANED:
            payload["abandoned_run_id"] = str(record.abandoned_run_id)
        command_id = uuid4()
        self.event_store.append_batch(
            (
                StreamWrite(
                    StreamId("agent", agent_id),
                    record.version,
                    (_event(command_id, "agent-start", event_type, payload),),
                ),
            ),
            idempotency_key=command_id,
        )
        updated = self.graph.load(agent_id)
        if updated is None:
            raise AgentError("started_agent_missing")
        return updated

    def heartbeat(
        self,
        agent_id: UUID,
        *,
        run_id: UUID,
        lease_seconds: int = 30,
    ) -> AgentRecord:
        record = self.graph.load(agent_id)
        if record is None or record.run_id != run_id:
            raise AgentError("stale_agent_run_fenced")
        lease = self.now() + timedelta(seconds=lease_seconds)
        command_id = uuid4()
        self.event_store.append_batch(
            (
                StreamWrite(
                    StreamId("agent", agent_id),
                    record.version,
                    (
                        _event(
                            command_id,
                            "agent-heartbeat",
                            "agent.heartbeat.v1",
                            {
                                "agent_id": str(agent_id),
                                "run_id": str(run_id),
                                "lease_expires_at": lease.isoformat(),
                            },
                        ),
                    ),
                ),
            ),
            idempotency_key=command_id,
        )
        updated = self.graph.load(agent_id)
        if updated is None:
            raise AgentError("heartbeat_agent_missing")
        return updated

    def send_message(
        self,
        to_agent_id: UUID,
        *,
        from_agent_id: UUID | None,
        kind: MessageKind,
        body_ref: str | None,
        idempotency_key: str,
    ) -> MessageRecord:
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 256:
            raise AgentError("invalid_message_idempotency")
        if body_ref is not None and (
            not isinstance(body_ref, str) or not body_ref or len(body_ref) > 2048
        ):
            raise AgentError("invalid_message_body_ref")
        target = self.graph.load(to_agent_id)
        if target is None:
            raise AgentError("message_target_missing")
        if target.state in (
            AgentState.COMPLETED,
            AgentState.FAILED,
            AgentState.CANCELLED,
        ):
            raise AgentError("message_target_terminal")
        existing_messages = self.mailbox.load(to_agent_id)
        for message in existing_messages:
            if message.idempotency_key == idempotency_key:
                return message
        message_id = uuid5(
            NAMESPACE_URL,
            f"koawa-d11:message:{to_agent_id}:{idempotency_key}",
        )
        sequence = len(existing_messages)
        observed_at = self.now()
        enqueue_command_id = uuid5(
            NAMESPACE_URL,
            f"koawa-d11:enqueue:{message_id}",
        )
        enqueued = _event(
            enqueue_command_id,
            "message-enqueued",
            "message.enqueued.v1",
            {
                "agent_id": str(to_agent_id),
                "message_id": str(message_id),
                "from_agent_id": (
                    None if from_agent_id is None else str(from_agent_id)
                ),
                "sequence": sequence,
                "kind": kind.value,
                "body_ref": body_ref,
                "idempotency_key": idempotency_key,
                "status": MessageStatus.QUEUED.value,
            },
            turn_id=None,
            run_id=None,
        )
        mailbox_events = self._read_all(StreamId("mailbox", to_agent_id))
        mailbox_version = -1 if not mailbox_events else mailbox_events[-1].stream_version
        self.event_store.append_batch(
            (
                StreamWrite(
                    StreamId("mailbox", to_agent_id),
                    mailbox_version,
                    (enqueued,),
                ),
            ),
            idempotency_key=enqueue_command_id,
        )
        rebuilt = rebuild_mailbox(
            to_agent_id, self._read_all(StreamId("mailbox", to_agent_id))
        )
        return next(item for item in rebuilt if item.message_id == message_id)

    def deliver_message(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        run_id: UUID,
    ) -> MessageRecord:
        return self._transition_message(
            agent_id,
            message_id,
            run_id,
            "message.delivered.v1",
            "message-delivered",
            MessageStatus.DELIVERED,
        )

    def ack_message(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        run_id: UUID,
    ) -> MessageRecord:
        return self._transition_message(
            agent_id,
            message_id,
            run_id,
            "message.acked.v1",
            "message-acked",
            MessageStatus.ACKED,
        )

    def _transition_message(
        self,
        agent_id: UUID,
        message_id: UUID,
        run_id: UUID,
        event_type: str,
        slot: str,
        status: MessageStatus,
    ) -> MessageRecord:
        record = self.graph.load(agent_id)
        if record is None or record.run_id != run_id:
            raise AgentError("stale_agent_run_fenced")
        messages = self.mailbox.load(agent_id)
        message = next((item for item in messages if item.message_id == message_id), None)
        if message is None:
            raise AgentError("message_missing")
        transition_command_id = uuid5(
            NAMESPACE_URL,
            f"koawa-d11:{event_type}:{agent_id}:{message_id}",
        )
        self.event_store.append_batch(
            (
                StreamWrite(
                    StreamId("mailbox", agent_id),
                    message.version,
                    (
                        _event(
                            transition_command_id,
                            slot,
                            event_type,
                            {
                                "agent_id": str(agent_id),
                                "message_id": str(message_id),
                                "run_id": str(run_id),
                            },
                        ),
                    ),
                ),
            ),
            idempotency_key=transition_command_id,
        )
        rebuilt = rebuild_mailbox(
            agent_id, self._read_all(StreamId("mailbox", agent_id))
        )
        return next(item for item in rebuilt if item.message_id == message_id)

    def interrupt_agent(self, agent_id: UUID, *, run_id: UUID) -> MessageRecord:
        return self.send_message(
            agent_id,
            from_agent_id=None,
            kind=MessageKind.CANCEL,
            body_ref="cancel",
            idempotency_key=f"cancel:{run_id}",
        )

    def terminal(
        self,
        agent_id: UUID,
        *,
        run_id: UUID,
        state: AgentState,
        reason: str,
        outcome: str | None = None,
    ) -> AgentRecord:
        record = self.graph.load(agent_id)
        if record is None or record.run_id != run_id:
            raise AgentError("stale_agent_run_fenced")
        event_type = {
            AgentState.COMPLETED: "agent.completed.v1",
            AgentState.FAILED: "agent.failed.v1",
            AgentState.CANCELLED: "agent.cancelled.v1",
        }[state]
        payload: dict = {
            "agent_id": str(agent_id),
            "run_id": str(run_id),
        }
        if state is AgentState.COMPLETED:
            payload["outcome"] = outcome or reason
        else:
            payload["reason"] = reason
        command_id = uuid4()
        self.event_store.append_batch(
            (
                StreamWrite(
                    StreamId("agent", agent_id),
                    record.version,
                    (_event(command_id, "agent-terminal", event_type, payload),),
                ),
            ),
            idempotency_key=command_id,
        )
        self._release_budget(agent_id)
        updated = self.graph.load(agent_id)
        if updated is None:
            raise AgentError("terminal_agent_missing")
        return updated

    def discover_orphans(self) -> list[AgentRecord]:
        now = self.now()
        orphans: list[AgentRecord] = []
        for event in self._scan_agent_spawns():
            raw_agent = event.payload.get("agent_id")
            if not isinstance(raw_agent, str):
                continue
            try:
                record = self.graph.load(UUID(raw_agent))
            except (ValueError, AgentError):
                continue
            if (
                record is not None
                and record.state is AgentState.RUNNING
                and record.lease_expires_at is not None
                and record.lease_expires_at <= now
            ):
                command_id = uuid4()
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("agent", record.agent_id),
                            record.version,
                            (
                                _event(
                                    command_id,
                                    "agent-orphaned",
                                    "agent.orphaned.v1",
                                    {
                                        "agent_id": str(record.agent_id),
                                        "abandoned_run_id": str(record.run_id),
                                    },
                                ),
                            ),
                        ),
                    ),
                    idempotency_key=command_id,
                )
                orphans.append(self.graph.load(record.agent_id))
        return [item for item in orphans if item is not None]

    def wait_agents(
        self,
        parent_agent_id: UUID,
        *,
        timeout_seconds: float = 5.0,
    ) -> list[dict]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            children = self.graph.children(parent_agent_id)
            if children and all(
                child.state
                in (
                    AgentState.COMPLETED,
                    AgentState.FAILED,
                    AgentState.CANCELLED,
                )
                for child in children
            ):
                return [child.to_document() for child in children]
            if time.monotonic() >= deadline:
                return [child.to_document() for child in children]
            time.sleep(0.05)

    def list_agents(self, parent_agent_id: UUID) -> list[dict]:
        return [
            child.to_document()
            for child in self.graph.children(parent_agent_id)
        ]

    def _release_budget(self, agent_id: UUID) -> None:
        record = self.graph.load(agent_id)
        if record is None or record.parent_agent_id is None:
            return
        root = self.root_for(agent_id)
        for _ in range(3):
            budget = self._budget(root)
            budget_events = self._read_all(StreamId("agent-budget", root))
            budget_version = (
                -1 if not budget_events else budget_events[-1].stream_version
            )
            command_id = uuid4()
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("agent-budget", root),
                            budget_version,
                            (
                                _event(
                                    command_id,
                                    "budget-released",
                                    "budget.released.v1",
                                    {
                                        "root_agent_id": str(root),
                                        "child_agent_id": str(agent_id),
                                        "total_agents": max(budget - 1, 0),
                                    },
                                ),
                            ),
                        ),
                    ),
                    idempotency_key=command_id,
                )
                return
            except WrongExpectedVersion:
                continue
        raise AgentError("agent_budget_release_retry_exhausted")

    def _depth(self, agent_id: UUID) -> int:
        depth = 0
        current: UUID | None = agent_id
        while current is not None:
            record = self.graph.load(current)
            if record is None:
                break
            depth += 1
            current = record.parent_agent_id
        return depth

    def _active_children(self, parent_agent_id: UUID) -> list[AgentRecord]:
        return [
            child
            for child in self.graph.children(parent_agent_id)
            if child.state
            in (
                AgentState.CREATED,
                AgentState.RUNNING,
                AgentState.WAITING,
                AgentState.ORPHANED,
            )
        ]

    def _budget(self, root_agent_id: UUID) -> int:
        events = self._read_all(StreamId("agent-budget", root_agent_id))
        reserved = sum(
            1
            for event in events
            if event.event_type == "budget.reserved.v1"
        )
        released = sum(
            1
            for event in events
            if event.event_type == "budget.released.v1"
        )
        return reserved - released

    def _read_all(self, stream: StreamId) -> tuple:
        values = []
        cursor = -1
        while True:
            page = self.event_store.read_stream(stream, after_version=cursor, limit=500)
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version

    def _scan_agent_spawns(self) -> list:
        events = []
        cursor = 0
        while True:
            page = self.event_store.read_all(after_position=cursor, limit=500)
            events.extend(
                event
                for event in page
                if event.event_type == "agent.spawned.v1"
            )
            if len(page) < 500:
                return events
            cursor = page[-1].global_position


def _event(
    command_id: UUID,
    slot: str,
    event_type: str,
    payload: Mapping,
    *,
    turn_id: UUID | None = None,
    run_id: UUID | None = None,
) -> NewEvent:
    return NewEvent(
        uuid5(command_id, "event:" + slot),
        event_type,
        1,
        datetime.now(timezone.utc),
        dict(payload),
        EventMetadata(
            command_id,
            uuid4(),
            thread_id=None,
            turn_id=turn_id,
            run_id=run_id,
            actor="agent-control",
        ),
    )
