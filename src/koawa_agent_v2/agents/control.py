"""D11 durable multi-agent control plane (spawn/mailbox/result/budget/fence).

I2 (section 4) adds the mailbox state machine RESULT_RECORDED/UNRESOLVED,
delivery/result accounting, mailbox-head CAS (P0-01), takeover as one atomic
batch of agent + unresolved writes, WAITING/RESUME, operator resolution and
stable fingerprints for every retryable command. All persisted datetimes use
the injected clock which defaults to the Event Store database clock (D2.4).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .graph import (
    AgentError,
    AgentGraph,
    AgentRecord,
    AgentState,
    ContextMode,
)
from .messages import (
    AgentMailbox,
    MessageKind,
    MessageRecord,
    MessageStatus,
    canonicalize_result,
    result_digest_for,
    result_ref_for,
    summarize_outcome,
)
from ..control.event_store import (
    EventMetadata,
    IdempotencyConflict,
    NewEvent,
    StreamId,
    StreamPrecondition,
    StreamWrite,
    WrongExpectedVersion,
)

MAX_CAS_RETRIES = 3

# facts passed to fault hooks never carry task text, result bodies or
# credentials; they only hold stable ids/versions/attempts.
FaultInjector = Callable[[str, Mapping[str, object]], None]


def _no_faults(point: str, facts: Mapping[str, object]) -> None:
    return None


NO_FAULTS: FaultInjector = _no_faults


@dataclass(frozen=True, slots=True)
class AgentBudgetLimits:
    max_depth: int = 4
    max_total_agents: int = 16
    max_concurrent_children: int = 4

    def __post_init__(self) -> None:
        for value in (self.max_depth, self.max_total_agents, self.max_concurrent_children):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("agent budget limits must be positive integers")


@dataclass(frozen=True, slots=True)
class Principal:
    """Operator identity used by requeue/cancel decisions."""

    principal_id: str
    scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.principal_id, str) or not self.principal_id:
            raise ValueError("principal_id must be non-empty text")
        for scope in self.scopes:
            if not isinstance(scope, str) or not scope:
                raise ValueError("scopes must be non-empty lowercase ids")


def _correlation(command_id: UUID) -> UUID:
    return uuid5(command_id, "correlation")


def _fingerprint(document: Mapping) -> str:
    return json.dumps(
        dict(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _facts(
    agent_id: UUID,
    attempt: int,
    *,
    message_ids: tuple[UUID, ...] = (),
    delivery_attempt: int | None = None,
    version: int | None = None,
) -> dict[str, object]:
    facts: dict[str, object] = {"agent_id": str(agent_id), "attempt": attempt}
    if message_ids:
        facts["message_ids"] = [str(item) for item in message_ids]
    if delivery_attempt is not None:
        facts["delivery_attempt"] = delivery_attempt
    if version is not None:
        facts["version"] = version
    return facts


class AgentControlPlane:
    """Durable spawn/message/result/terminal commands with exact-version fences."""

    def __init__(
        self,
        event_store,
        *,
        limits: AgentBudgetLimits | None = None,
        clock: Callable[[], datetime] | None = None,
        faults: FaultInjector = NO_FAULTS,
    ) -> None:
        for method in ("append_batch", "read_stream", "read_all"):
            if not callable(getattr(event_store, method, None)):
                raise TypeError("event_store must implement the EventStore boundary")
        if clock is None:
            if not hasattr(event_store, "database_time"):
                raise TypeError(
                    "event_store must implement database_time unless a clock is injected"
                )
            clock = event_store.database_time
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(faults):
            raise TypeError("faults must be callable")
        self.event_store = event_store
        self.graph = AgentGraph(event_store)
        self.mailbox = AgentMailbox(event_store)
        self.limits = limits or AgentBudgetLimits()
        self._clock = clock
        self._faults = faults

    def now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise AgentError("agent_clock_must_be_aware")
        return value.astimezone(timezone.utc)

    # ------------------------------------------------------------------
    # graph plumbing (unchanged semantics)
    # ------------------------------------------------------------------

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
            occurred_at=observed_at,
            correlation_id=_correlation(spawn_command_id),
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
                occurred_at=observed_at,
                correlation_id=_correlation(spawn_command_id),
                turn_id=turn_id,
                run_id=parent_run_id,
            )
            writes.append(StreamWrite(
                StreamId("agent-budget", root), budget_version, (reserved,),
            ))
        for _ in range(MAX_CAS_RETRIES):
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

    # ------------------------------------------------------------------
    # attempt lifecycle: CREATED start / ORPHANED takeover / WAITING resume
    # ------------------------------------------------------------------

    def start_attempt(
        self,
        agent_id: UUID,
        *,
        expected_version: int,
        lease_seconds: int = 30,
    ) -> AgentRecord:
        record = self.graph.load(agent_id)
        if record is None:
            raise AgentError("agent_missing")
        if record.state is AgentState.CREATED:
            return self._start_fresh(record, lease_seconds=lease_seconds)
        if record.state is AgentState.ORPHANED:
            return self._takeover(record, lease_seconds=lease_seconds)
        if record.state is AgentState.WAITING:
            return self._resume(record, lease_seconds=lease_seconds)
        raise AgentError("agent_attempt_state_invalid")

    def _start_fresh(
        self, record: AgentRecord, *, lease_seconds: int
    ) -> AgentRecord:
        agent_id = record.agent_id
        command_id = uuid5(
            NAMESPACE_URL, f"koawa-v2:start:{agent_id}:1"
        )
        fingerprint = _fingerprint({
            "operation": "start_attempt",
            "agent_id": str(agent_id),
            "attempt": 1,
            "lease_seconds": lease_seconds,
        })
        for _ in range(MAX_CAS_RETRIES):
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return self._rebuilt_agent(agent_id)
            current = self.graph.load(agent_id)
            if current is None or current.version != record.version:
                raise AgentError("agent_version_stale")
            if current.state is not AgentState.CREATED:
                raise AgentError("agent_attempt_state_invalid")
            run_id = uuid4()
            observed_at = self.now()
            lease = observed_at + timedelta(seconds=lease_seconds)
            started = _event(
                command_id,
                "agent-start",
                "agent.started.v1",
                {
                    "agent_id": str(agent_id),
                    "run_id": str(run_id),
                    "attempt": 1,
                    "lease_expires_at": lease.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            try:
                self.event_store.append_batch(
                    (StreamWrite(StreamId("agent", agent_id), current.version, (started,)),),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                )
                return self._rebuilt_agent(agent_id)
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return self._rebuilt_agent(agent_id)
        raise AgentError("agent_spawn_retry_exhausted")

    def _takeover(
        self, orphan: AgentRecord, *, lease_seconds: int
    ) -> AgentRecord:
        """Atomic ORPHANED takeover: taken-over.v2 + one unresolved per message.

        The agent write and every unresolved write share one append_batch, so
        a crash can never leave a partial batch (P0-02/P0-03 oracle).
        """

        agent_id = orphan.agent_id
        abandoned_run_id = orphan.abandoned_run_id
        new_attempt = orphan.attempt + 1
        command_id = uuid5(
            NAMESPACE_URL,
            "koawa-v2:takeover:"
            + str(agent_id)
            + ":"
            + str(abandoned_run_id)
            + ":"
            + str(new_attempt),
        )
        new_run_id = uuid5(command_id, "run")
        for _ in range(MAX_CAS_RETRIES):
            current = self.graph.load(agent_id)
            if current is None:
                raise AgentError("agent_missing")
            if current.state is AgentState.RUNNING and current.run_id == new_run_id:
                return current
            if current.state is not AgentState.ORPHANED:
                if current.state is AgentState.RUNNING:
                    raise AgentError("agent_takeover_conflict")
                raise AgentError("agent_attempt_state_invalid")
            if current.abandoned_run_id != abandoned_run_id:
                raise AgentError("agent_takeover_conflict")
            snapshot = self.mailbox.snapshot(agent_id)
            candidates = sorted(
                (
                    message
                    for message in snapshot.messages
                    if message.status is MessageStatus.DELIVERED
                    and message.delivered_run_id == abandoned_run_id
                    and message.result_ref is None
                ),
                key=lambda item: item.sequence,
            )
            observed_at = self.now()
            lease = observed_at + timedelta(seconds=lease_seconds)
            taken_over = _event(
                command_id,
                "agent-taken-over",
                "agent.taken-over.v2",
                {
                    "agent_id": str(agent_id),
                    "abandoned_run_id": str(abandoned_run_id),
                    "run_id": str(new_run_id),
                    "attempt": new_attempt,
                    "lease_expires_at": lease.isoformat(),
                    "taken_over_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            unresolved_events = tuple(
                _event(
                    command_id,
                    "unresolved:" + str(message.message_id) + ":" + str(message.delivery_attempt),
                    "message.unresolved.v1",
                    {
                        "agent_id": str(agent_id),
                        "message_id": str(message.message_id),
                        "abandoned_run_id": str(abandoned_run_id),
                        "delivery_attempt": message.delivery_attempt,
                        "reason": "provider_outcome_not_recorded",
                        "observed_at": observed_at.isoformat(),
                    },
                    occurred_at=observed_at,
                    correlation_id=_correlation(command_id),
                )
                for message in candidates
            )
            writes = [
                StreamWrite(StreamId("agent", agent_id), current.version, (taken_over,)),
            ]
            if candidates:
                writes.append(
                    StreamWrite(
                        StreamId("mailbox", agent_id),
                        snapshot.stream_version,
                        unresolved_events,
                    )
                )
            fingerprint = _fingerprint({
                "operation": "takeover",
                "agent_id": str(agent_id),
                "abandoned_run_id": str(abandoned_run_id),
                "attempt": new_attempt,
                "lease_seconds": lease_seconds,
                "messages": [
                    [str(message.message_id), message.delivery_attempt]
                    for message in candidates
                ],
            })
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return self._rebuilt_agent(agent_id)
            try:
                self.event_store.append_batch(
                    tuple(writes),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return self._rebuilt_agent(agent_id)
            if candidates:
                self._fault(
                    "d11.unresolved.after_commit",
                    _facts(
                        agent_id,
                        new_attempt,
                        message_ids=tuple(item.message_id for item in candidates),
                    ),
                )
            return self._rebuilt_agent(agent_id)
        raise AgentError("mailbox_stream_conflict")

    def _resume(
        self, waiting: AgentRecord, *, lease_seconds: int
    ) -> AgentRecord:
        """Resume a WAITING agent once every blocker is resolved (all QUEUED
        or all CANCELLED; mixing or lingering UNRESOLVED/DELIVERED is refused).
        """

        agent_id = waiting.agent_id
        previous_run_id = waiting.waiting_run_id
        if previous_run_id is None:
            raise AgentError("corrupt_agent_stream")
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self.mailbox.snapshot(agent_id)
            by_id = {message.message_id: message for message in snapshot.messages}
            blockers: list[MessageRecord] = []
            for message_id in waiting.blocking_message_ids:
                message = by_id.get(message_id)
                if message is None:
                    raise AgentError("message_missing")
                blockers.append(message)
            for message in blockers:
                if message.status in (
                    MessageStatus.UNRESOLVED,
                    MessageStatus.DELIVERED,
                ):
                    raise AgentError("message_outcome_unresolved")
            statuses = {message.status for message in blockers}
            if len(statuses) > 1:
                raise AgentError("message_resolution_mixed")
            resolved_kind = next(iter(statuses)) if statuses else None
            if resolved_kind is None or (
                resolved_kind is not MessageStatus.QUEUED
                and resolved_kind is not MessageStatus.CANCELLED
            ):
                raise AgentError("message_resolution_mixed")
            new_attempt = waiting.attempt + 1
            mailbox_version = snapshot.stream_version
            command_id = uuid5(
                NAMESPACE_URL,
                "koawa-v2:resume:"
                + str(agent_id)
                + ":"
                + str(previous_run_id)
                + ":"
                + str(new_attempt)
                + ":"
                + str(mailbox_version),
            )
            fingerprint = _fingerprint({
                "operation": "resume",
                "agent_id": str(agent_id),
                "previous_run_id": str(previous_run_id),
                "attempt": new_attempt,
                "resolved_message_ids": [
                    str(message.message_id) for message in blockers
                ],
            })
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return self._rebuilt_agent(agent_id)
            current = self.graph.load(agent_id)
            if (
                current is None
                or current.state is not AgentState.WAITING
                or current.waiting_run_id != previous_run_id
            ):
                raise AgentError("stale_agent_run_fenced")
            new_run_id = uuid5(command_id, "run")
            observed_at = self.now()
            lease = observed_at + timedelta(seconds=lease_seconds)
            resumed = _event(
                command_id,
                "agent-resumed",
                "agent.resumed.v1",
                {
                    "agent_id": str(agent_id),
                    "previous_run_id": str(previous_run_id),
                    "run_id": str(new_run_id),
                    "attempt": new_attempt,
                    "resolved_message_ids": [
                        str(message.message_id) for message in blockers
                    ],
                    "mailbox_stream_version": mailbox_version,
                    "lease_expires_at": lease.isoformat(),
                    "resumed_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("agent", agent_id),
                            current.version,
                            (resumed,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("mailbox", agent_id),
                            mailbox_version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return self._rebuilt_agent(agent_id)
            self._fault(
                "d11.resume.after_commit",
                _facts(
                    agent_id,
                    new_attempt,
                    message_ids=tuple(message.message_id for message in blockers),
                ),
            )
            return self._rebuilt_agent(agent_id)
        raise AgentError("mailbox_stream_conflict")

    def heartbeat(
        self,
        agent_id: UUID,
        *,
        run_id: UUID,
        lease_seconds: int = 30,
        max_cas_retries: int = 3,
        beat_number: int = 0,
    ) -> AgentRecord:
        command_id = uuid5(
            NAMESPACE_URL,
            "koawa-v2:heartbeat:"
            + str(agent_id)
            + ":"
            + str(run_id)
            + ":"
            + str(beat_number),
        )
        fingerprint = _fingerprint({
            "operation": "heartbeat",
            "agent_id": str(agent_id),
            "run_id": str(run_id),
            "lease_seconds": lease_seconds,
            "beat_number": beat_number,
        })
        for _ in range(max(1, max_cas_retries)):
            record = self.graph.load(agent_id)
            if record is None or record.run_id != run_id:
                raise AgentError("agent_lease_lost")
            observed_at = self.now()
            lease = observed_at + timedelta(seconds=lease_seconds)
            try:
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
                                    occurred_at=observed_at,
                                    correlation_id=_correlation(command_id),
                                ),
                            ),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return self._rebuilt_agent(agent_id)
            updated = self.graph.load(agent_id)
            if updated is None:
                raise AgentError("heartbeat_agent_missing")
            return updated
        raise AgentError("agent_heartbeat_retry_exhausted")

    # ------------------------------------------------------------------
    # mailbox transitions
    # ------------------------------------------------------------------

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
            occurred_at=observed_at,
            correlation_id=_correlation(enqueue_command_id),
            turn_id=None,
            run_id=None,
        )
        for _ in range(MAX_CAS_RETRIES):
            mailbox_events = self._read_all(StreamId("mailbox", to_agent_id))
            mailbox_version = -1 if not mailbox_events else mailbox_events[-1].stream_version
            self._fault(
                "d11.enqueue.before_append",
                _facts(to_agent_id, target.attempt, version=mailbox_version),
            )
            try:
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
            except WrongExpectedVersion:
                rebuilt = self.mailbox.load(to_agent_id)
                if any(item.idempotency_key == idempotency_key for item in rebuilt):
                    return next(
                        item for item in rebuilt
                        if item.idempotency_key == idempotency_key
                    )
                continue
            self._fault(
                "d11.enqueue.after_commit",
                _facts(to_agent_id, target.attempt, message_ids=(message_id,)),
            )
            rebuilt = self.mailbox.load(to_agent_id)
            return next(item for item in rebuilt if item.message_id == message_id)
        raise AgentError("mailbox_stream_conflict")

    def deliver_message(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        run_id: UUID,
        lease_seconds: int = 30,
    ) -> MessageRecord:
        for _ in range(MAX_CAS_RETRIES):
            record = self.graph.load(agent_id)
            if record is None or record.run_id != run_id:
                raise AgentError("stale_agent_run_fenced")
            snapshot = self.mailbox.snapshot(agent_id)
            message = self._message_in(snapshot, message_id)
            if message is None:
                raise AgentError("message_missing")
            if (
                message.status is MessageStatus.DELIVERED
                and message.delivered_run_id == run_id
            ):
                return message
            if message.status is not MessageStatus.QUEUED:
                raise AgentError("message_transition_invalid")
            delivery_attempt = message.delivery_attempt + 1
            command_id = uuid5(
                NAMESPACE_URL,
                "koawa-v2:deliver:"
                + str(agent_id)
                + ":"
                + str(message_id)
                + ":"
                + str(delivery_attempt),
            )
            observed_at = self.now()
            lease = observed_at + timedelta(seconds=lease_seconds)
            delivered = _event(
                command_id,
                "message-delivered",
                "message.delivered.v2",
                {
                    "agent_id": str(agent_id),
                    "message_id": str(message_id),
                    "run_id": str(run_id),
                    "agent_attempt": record.attempt,
                    "delivery_attempt": delivery_attempt,
                    "lease_expires_at": lease.isoformat(),
                    "delivered_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            fingerprint = _fingerprint({
                "operation": "deliver",
                "agent_id": str(agent_id),
                "message_id": str(message_id),
                "run_id": str(run_id),
                "agent_attempt": record.attempt,
                "delivery_attempt": delivery_attempt,
                "lease_seconds": lease_seconds,
            })
            self._fault(
                "d11.deliver.before_append",
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=delivery_attempt,
                    version=snapshot.stream_version,
                ),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                            (delivered,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("agent", agent_id),
                            record.version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return message
            self._fault(
                "d11.deliver.after_commit",
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=delivery_attempt,
                ),
            )
            return self._message_in(self.mailbox.snapshot(agent_id), message_id)
        raise AgentError("mailbox_stream_conflict")

    def record_message_result(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        run_id: UUID,
        expected_delivery_attempt: int,
        outcome: str | None = None,
        error_code: str | None = None,
    ) -> MessageRecord:
        canonical_outcome, is_error, error_code = canonicalize_result(
            outcome, error_code
        )
        command_id = uuid5(
            NAMESPACE_URL,
            "koawa-v2:result:"
            + str(agent_id)
            + ":"
            + str(message_id)
            + ":"
            + str(expected_delivery_attempt),
        )
        result_ref = result_ref_for(
            agent_id, message_id, expected_delivery_attempt
        )
        result_digest = result_digest_for(
            agent_id,
            message_id,
            expected_delivery_attempt,
            canonical_outcome,
            is_error,
            error_code,
        )
        fingerprint = _fingerprint({
            "operation": "record_message_result",
            "agent_id": str(agent_id),
            "message_id": str(message_id),
            "run_id": str(run_id),
            "delivery_attempt": expected_delivery_attempt,
            "result_ref": result_ref,
            "result_digest": result_digest,
            "is_error": is_error,
            "error_code": error_code,
        })
        for _ in range(MAX_CAS_RETRIES):
            record = self.graph.load(agent_id)
            if record is None or record.run_id != run_id:
                raise AgentError("stale_agent_run_fenced")
            snapshot = self.mailbox.snapshot(agent_id)
            message = self._message_in(snapshot, message_id)
            if message is None:
                raise AgentError("message_missing")
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                if receipt == "committed":
                    raise AgentError("message_result_identity_conflict")
                return message
            if (
                message.status is MessageStatus.RESULT_RECORDED
                and message.delivery_attempt == expected_delivery_attempt
                and message.result_digest == result_digest
            ):
                return message
            if message.legacy_delivery:
                raise AgentError("legacy_delivery_requires_resolution")
            if message.status is not MessageStatus.DELIVERED:
                raise AgentError("message_transition_invalid")
            if message.delivery_attempt != expected_delivery_attempt:
                raise AgentError("stale_message_delivery_attempt")
            if message.delivered_run_id != run_id:
                raise AgentError("stale_agent_run_fenced")
            summary = summarize_outcome(canonical_outcome)
            observed_at = self.now()
            recorded = _event(
                command_id,
                "message-result-recorded",
                "message.result-recorded.v1",
                {
                    "agent_id": str(agent_id),
                    "message_id": str(message_id),
                    "delivery_run_id": str(message.delivered_run_id),
                    "recording_run_id": str(run_id),
                    "agent_attempt": record.attempt,
                    "delivery_attempt": expected_delivery_attempt,
                    "result_ref": result_ref,
                    "result_digest": result_digest,
                    "result_summary": summary,
                    "is_error": is_error,
                    "error_code": error_code,
                    "recorded_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            self._fault(
                "d11.result.before_append",
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=expected_delivery_attempt,
                    version=snapshot.stream_version,
                ),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                            (recorded,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("agent", agent_id),
                            record.version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                raise AgentError("message_result_identity_conflict") from None
            self._fault(
                "d11.result.after_commit",
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=expected_delivery_attempt,
                ),
            )
            return self._message_in(self.mailbox.snapshot(agent_id), message_id)
        raise AgentError("mailbox_stream_conflict")

    def ack_message(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        run_id: UUID,
    ) -> MessageRecord:
        for _ in range(MAX_CAS_RETRIES):
            record = self.graph.load(agent_id)
            if record is None or record.run_id != run_id:
                raise AgentError("stale_agent_run_fenced")
            snapshot = self.mailbox.snapshot(agent_id)
            message = self._message_in(snapshot, message_id)
            if message is None:
                raise AgentError("message_missing")
            command_id = uuid5(
                NAMESPACE_URL,
                "koawa-v2:ack:"
                + str(agent_id)
                + ":"
                + str(message_id)
                + ":"
                + str(message.delivery_attempt),
            )
            fingerprint = _fingerprint({
                "operation": "ack_message",
                "agent_id": str(agent_id),
                "message_id": str(message_id),
                "run_id": str(run_id),
                "delivery_attempt": message.delivery_attempt,
                "result_ref": message.result_ref,
                "result_digest": message.result_digest,
            })
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return message
            if message.status is MessageStatus.ACKED:
                return message
            if message.status is not MessageStatus.RESULT_RECORDED:
                if message.legacy_delivery:
                    raise AgentError("legacy_delivery_requires_resolution")
                raise AgentError("message_transition_invalid")
            if message.result_ref is None or message.result_digest is None:
                raise AgentError("message_result_required")
            observed_at = self.now()
            acked = _event(
                command_id,
                "message-acked",
                "message.acked.v2",
                {
                    "agent_id": str(agent_id),
                    "message_id": str(message_id),
                    "ack_run_id": str(run_id),
                    "delivery_attempt": message.delivery_attempt,
                    "result_ref": message.result_ref,
                    "result_digest": message.result_digest,
                    "acked_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            self._fault(
                "d11.ack.before_append",
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=message.delivery_attempt,
                    version=snapshot.stream_version,
                ),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                            (acked,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("agent", agent_id),
                            record.version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return message
            self._fault(
                "d11.ack.after_commit",
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=message.delivery_attempt,
                ),
            )
            return self._message_in(self.mailbox.snapshot(agent_id), message_id)
        raise AgentError("mailbox_stream_conflict")

    def mark_message_unresolved(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        abandoned_run_id: UUID,
        takeover_run_id: UUID,
        expected_delivery_attempt: int,
        reason: str,
    ) -> MessageRecord:
        """Internal primitive; the real takeover uses the atomic batch in the
        private _takeover method. Only usable when the agent is currently owned
        by takeover_run_id.
        """

        for _ in range(MAX_CAS_RETRIES):
            record = self.graph.load(agent_id)
            if record is None or record.run_id != takeover_run_id:
                raise AgentError("stale_agent_run_fenced")
            snapshot = self.mailbox.snapshot(agent_id)
            message = self._message_in(snapshot, message_id)
            if message is None:
                raise AgentError("message_missing")
            command_id = uuid5(
                NAMESPACE_URL,
                "koawa-v2:unresolved:"
                + str(agent_id)
                + ":"
                + str(message_id)
                + ":"
                + str(expected_delivery_attempt),
            )
            fingerprint = _fingerprint({
                "operation": "mark_message_unresolved",
                "agent_id": str(agent_id),
                "message_id": str(message_id),
                "abandoned_run_id": str(abandoned_run_id),
                "takeover_run_id": str(takeover_run_id),
                "delivery_attempt": expected_delivery_attempt,
                "reason": reason,
            })
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return message
            if (
                message.status is MessageStatus.UNRESOLVED
                and message.delivery_attempt == expected_delivery_attempt
            ):
                return message
            if message.legacy_delivery:
                raise AgentError("legacy_delivery_requires_resolution")
            if message.status is not MessageStatus.DELIVERED:
                raise AgentError("message_transition_invalid")
            if message.delivery_attempt != expected_delivery_attempt:
                raise AgentError("stale_message_delivery_attempt")
            if message.delivered_run_id != abandoned_run_id:
                raise AgentError("stale_agent_run_fenced")
            observed_at = self.now()
            unresolved = _event(
                command_id,
                "message-unresolved",
                "message.unresolved.v1",
                {
                    "agent_id": str(agent_id),
                    "message_id": str(message_id),
                    "abandoned_run_id": str(abandoned_run_id),
                    "delivery_attempt": expected_delivery_attempt,
                    "reason": reason,
                    "observed_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                            (unresolved,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("agent", agent_id),
                            record.version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return message
            self._fault(
                "d11.unresolved.after_commit",
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=expected_delivery_attempt,
                ),
            )
            return self._message_in(self.mailbox.snapshot(agent_id), message_id)
        raise AgentError("mailbox_stream_conflict")

    def requeue_message(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        expected_delivery_attempt: int,
        decision_id: UUID,
        actor: Principal,
        approval_id: UUID | None,
        resolution_kind: Literal[
            "proven_not_started", "idempotent_read", "operator_retry"
        ],
        reason: str,
    ) -> MessageRecord:
        record = self.graph.load(agent_id)
        if record is None:
            raise AgentError("agent_missing")
        self._require_operator_authority(
            record,
            actor,
            approval_id=approval_id,
            resolution_kind=resolution_kind,
            error_code="message_requeue_not_authorized",
        )
        command_id = uuid5(
            NAMESPACE_URL,
            "koawa-v2:requeue:" + str(decision_id),
        )
        fingerprint = _fingerprint({
            "operation": "requeue_message",
            "agent_id": str(agent_id),
            "message_id": str(message_id),
            "decision_id": str(decision_id),
            "actor": actor.principal_id,
            "approval_id": None if approval_id is None else str(approval_id),
            "resolution_kind": resolution_kind,
            "reason": reason,
            "expected_delivery_attempt": expected_delivery_attempt,
        })
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self.mailbox.snapshot(agent_id)
            message = self._message_in(snapshot, message_id)
            if message is None:
                raise AgentError("message_missing")
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return message
            if (
                message.status is MessageStatus.QUEUED
                and message.delivery_attempt == expected_delivery_attempt
            ):
                return message
            if message.status is not MessageStatus.UNRESOLVED:
                raise AgentError("message_transition_invalid")
            if message.delivery_attempt != expected_delivery_attempt:
                raise AgentError("stale_message_delivery_attempt")
            observed_at = self.now()
            requeued = _event(
                command_id,
                "message-requeued",
                "message.requeued.v1",
                {
                    "agent_id": str(agent_id),
                    "message_id": str(message_id),
                    "previous_delivery_attempt": message.delivery_attempt,
                    "reason": reason,
                    "resolution_kind": resolution_kind,
                    "decision_id": str(decision_id),
                    "actor_principal_id": actor.principal_id,
                    "approval_id": None if approval_id is None else str(approval_id),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                            (requeued,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("agent", agent_id),
                            record.version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                record = self.graph.load(agent_id)
                if record is None:
                    raise AgentError("agent_missing") from None
                continue
            except IdempotencyConflict:
                return message
            return self._message_in(self.mailbox.snapshot(agent_id), message_id)
        raise AgentError("mailbox_stream_conflict")

    def cancel_message(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        expected_delivery_attempt: int,
        decision_id: UUID,
        actor: Principal,
        approval_id: UUID | None,
        reason: str,
    ) -> MessageRecord:
        record = self.graph.load(agent_id)
        if record is None:
            raise AgentError("agent_missing")
        self._require_operator_authority(
            record,
            actor,
            approval_id=approval_id,
            resolution_kind="operator_retry" if approval_id is not None else "proven_not_started",
            error_code="message_resolution_not_authorized",
        )
        command_id = uuid5(
            NAMESPACE_URL,
            "koawa-v2:cancel:" + str(decision_id),
        )
        fingerprint = _fingerprint({
            "operation": "cancel_message",
            "agent_id": str(agent_id),
            "message_id": str(message_id),
            "decision_id": str(decision_id),
            "actor": actor.principal_id,
            "approval_id": None if approval_id is None else str(approval_id),
            "reason": reason,
            "expected_delivery_attempt": expected_delivery_attempt,
        })
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self.mailbox.snapshot(agent_id)
            message = self._message_in(snapshot, message_id)
            if message is None:
                raise AgentError("message_missing")
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return message
            if message.status in (MessageStatus.ACKED, MessageStatus.CANCELLED):
                raise AgentError("message_transition_invalid")
            observed_at = self.now()
            if message.status in (
                MessageStatus.DELIVERED,
                MessageStatus.RESULT_RECORDED,
            ):
                if message.legacy_delivery:
                    raise AgentError("legacy_delivery_requires_resolution")
                if message.delivery_attempt != expected_delivery_attempt:
                    raise AgentError("stale_message_delivery_attempt")
                if message.cancel_requested:
                    return message
                requested = _event(
                    command_id,
                    "message-cancel-requested",
                    "message.cancel-requested.v1",
                    {
                        "agent_id": str(agent_id),
                        "message_id": str(message_id),
                        "delivery_run_id": str(message.delivered_run_id),
                        "delivery_attempt": message.delivery_attempt,
                        "decision_id": str(decision_id),
                        "actor_principal_id": actor.principal_id,
                        "approval_id": None if approval_id is None else str(approval_id),
                        "reason": reason,
                        "requested_at": observed_at.isoformat(),
                    },
                    occurred_at=observed_at,
                    correlation_id=_correlation(command_id),
                )
                self._fault(
                    "d11.cancel.before_append",
                    _facts(
                        agent_id,
                        record.attempt,
                        message_ids=(message_id,),
                        delivery_attempt=message.delivery_attempt,
                        version=snapshot.stream_version,
                    ),
                )
                try:
                    self.event_store.append_batch(
                        (
                            StreamWrite(
                                StreamId("mailbox", agent_id),
                                snapshot.stream_version,
                                (requested,),
                            ),
                        ),
                        idempotency_key=command_id,
                        request_fingerprint=fingerprint,
                        preconditions=(
                            StreamPrecondition(
                                StreamId("agent", agent_id),
                                record.version,
                            ),
                        ),
                    )
                except WrongExpectedVersion:
                    record = self.graph.load(agent_id)
                    if record is None:
                        raise AgentError("agent_missing") from None
                    continue
                except IdempotencyConflict:
                    return message
                self._fault(
                    "d11.cancel.after_commit",
                    _facts(
                        agent_id,
                        record.attempt,
                        message_ids=(message_id,),
                        delivery_attempt=message.delivery_attempt,
                    ),
                )
                return self._message_in(self.mailbox.snapshot(agent_id), message_id)
            if message.status is MessageStatus.QUEUED:
                if expected_delivery_attempt != 0:
                    raise AgentError("stale_message_delivery_attempt")
                delivery_attempt_wire = 0
                previous_status = "queued"
            elif message.status is MessageStatus.UNRESOLVED:
                if message.delivery_attempt != expected_delivery_attempt:
                    raise AgentError("stale_message_delivery_attempt")
                delivery_attempt_wire = message.delivery_attempt
                previous_status = "unresolved"
            else:
                raise AgentError("message_transition_invalid")
            cancelled = _event(
                command_id,
                "message-cancelled",
                "message.cancelled.v2",
                {
                    "agent_id": str(agent_id),
                    "message_id": str(message_id),
                    "previous_status": previous_status,
                    "delivery_attempt": delivery_attempt_wire,
                    "decision_id": str(decision_id),
                    "actor_principal_id": actor.principal_id,
                    "approval_id": None if approval_id is None else str(approval_id),
                    "reason": reason,
                    "cancelled_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            self._fault(
                "d11.cancel.before_append",
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=delivery_attempt_wire,
                    version=snapshot.stream_version,
                ),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                            (cancelled,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("agent", agent_id),
                            record.version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                record = self.graph.load(agent_id)
                if record is None:
                    raise AgentError("agent_missing") from None
                continue
            except IdempotencyConflict:
                return message
            self._fault(
                "d11.cancel.after_commit",
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=delivery_attempt_wire,
                ),
            )
            return self._message_in(self.mailbox.snapshot(agent_id), message_id)
        raise AgentError("mailbox_stream_conflict")

    def enter_waiting_for_resolution(
        self,
        agent_id: UUID,
        *,
        run_id: UUID,
        attempt: int,
    ) -> AgentRecord:
        """RUNNING -> WAITING with every UNRESOLVED message as a blocker."""

        for _ in range(MAX_CAS_RETRIES):
            record = self.graph.load(agent_id)
            if record is None or record.run_id != run_id:
                raise AgentError("stale_agent_run_fenced")
            if record.attempt != attempt:
                raise AgentError("stale_agent_run_fenced")
            if record.state is not AgentState.RUNNING:
                raise AgentError("agent_attempt_state_invalid")
            snapshot = self.mailbox.snapshot(agent_id)
            blockers = [
                message
                for message in snapshot.messages
                if message.status is MessageStatus.UNRESOLVED
            ]
            if not blockers:
                raise AgentError("message_outcome_unresolved")
            command_id = uuid5(
                NAMESPACE_URL,
                "koawa-v2:waiting:" + str(agent_id) + ":" + str(run_id),
            )
            fingerprint = _fingerprint({
                "operation": "enter_waiting_for_resolution",
                "agent_id": str(agent_id),
                "run_id": str(run_id),
                "attempt": attempt,
                "blocking_message_ids": [
                    str(message.message_id) for message in blockers
                ],
            })
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return self._rebuilt_agent(agent_id)
            observed_at = self.now()
            waiting = _event(
                command_id,
                "agent-waiting-for-resolution",
                "agent.waiting-for-message-resolution.v1",
                {
                    "agent_id": str(agent_id),
                    "run_id": str(run_id),
                    "attempt": attempt,
                    "blocking_message_ids": [
                        str(message.message_id) for message in blockers
                    ],
                    "mailbox_stream_version": snapshot.stream_version,
                    "waiting_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("agent", agent_id),
                            record.version,
                            (waiting,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return self._rebuilt_agent(agent_id)
            self._fault(
                "d11.waiting.after_commit",
                _facts(
                    agent_id,
                    attempt,
                    message_ids=tuple(message.message_id for message in blockers),
                ),
            )
            return self._rebuilt_agent(agent_id)
        raise AgentError("mailbox_stream_conflict")

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
        command_id = uuid5(
            NAMESPACE_URL,
            "koawa-v2:terminal:"
            + str(agent_id)
            + ":"
            + str(run_id)
            + ":"
            + state.value,
        )
        fingerprint = _fingerprint({
            "operation": "terminal",
            "agent_id": str(agent_id),
            "run_id": str(run_id),
            "state": state.value,
            "reason": reason,
            "outcome": outcome,
        })
        receipt = self._read_receipt(command_id, fingerprint)
        if receipt is not None:
            return self._rebuilt_agent(agent_id)
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
        observed_at = self.now()
        for _ in range(MAX_CAS_RETRIES):
            current = self.graph.load(agent_id)
            if current is None or current.run_id != run_id:
                raise AgentError("stale_agent_run_fenced")
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("agent", agent_id),
                            current.version,
                            (_event(
                                command_id,
                                "agent-terminal",
                                event_type,
                                payload,
                                occurred_at=observed_at,
                                correlation_id=_correlation(command_id),
                            ),),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                )
                break
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return self._rebuilt_agent(agent_id)
        else:
            raise AgentError("mailbox_stream_conflict")
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
                observed_at = self.now()
                try:
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
                                        occurred_at=observed_at,
                                        correlation_id=_correlation(command_id),
                                    ),
                                ),
                            ),
                        ),
                        idempotency_key=command_id,
                    )
                except WrongExpectedVersion:
                    continue
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

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_operator_authority(
        self,
        record: AgentRecord,
        actor: Principal,
        *,
        approval_id: UUID | None,
        resolution_kind: str,
        error_code: str,
    ) -> None:
        scopes = set(actor.scopes)
        authorized = "agents.resolve:any" in scopes or (
            actor.principal_id == record.principal_id
            and "agents.resolve" in scopes
        )
        if not authorized:
            raise AgentError(error_code)
        if resolution_kind == "operator_retry" and approval_id is None:
            raise AgentError(error_code)

    def _read_receipt(self, command_id: UUID, fingerprint: str):
        """Return the persisted receipt, or the string sentinel "committed"
        when the same command was already committed under a different semantic
        fingerprint (IdempotencyConflict). Callers treat both as idempotent
        success; record_message_result maps the sentinel to
        message_result_identity_conflict.
        """

        read_idempotency = getattr(self.event_store, "read_idempotency", None)
        if read_idempotency is None:
            return None
        try:
            return read_idempotency(command_id, request_fingerprint=fingerprint)
        except IdempotencyConflict:
            return "committed"

    def _rebuilt_agent(self, agent_id: UUID) -> AgentRecord:
        record = self.graph.load(agent_id)
        if record is None:
            raise AgentError("agent_missing")
        return record

    def _message_in(
        self, snapshot: MailboxSnapshot, message_id: UUID
    ) -> MessageRecord | None:
        for message in snapshot.messages:
            if message.message_id == message_id:
                return message
        return None

    def _fault(self, point: str, facts: Mapping[str, object]) -> None:
        self._faults(point, facts)

    def _release_budget(self, agent_id: UUID) -> None:
        record = self.graph.load(agent_id)
        if record is None or record.parent_agent_id is None:
            return
        root = self.root_for(agent_id)
        for _ in range(MAX_CAS_RETRIES):
            budget = self._budget(root)
            budget_events = self._read_all(StreamId("agent-budget", root))
            budget_version = (
                -1 if not budget_events else budget_events[-1].stream_version
            )
            command_id = uuid4()
            observed_at = self.now()
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
                                    occurred_at=observed_at,
                                    correlation_id=_correlation(command_id),
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
    occurred_at: datetime,
    correlation_id: UUID,
    turn_id: UUID | None = None,
    run_id: UUID | None = None,
) -> NewEvent:
    return NewEvent(
        uuid5(command_id, "event:" + slot),
        event_type,
        int(event_type.rsplit(".v", 1)[1]),
        occurred_at,
        dict(payload),
        EventMetadata(
            command_id,
            correlation_id,
            thread_id=None,
            turn_id=turn_id,
            run_id=run_id,
            actor="agent-control",
        ),
    )
