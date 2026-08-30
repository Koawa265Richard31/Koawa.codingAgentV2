"""D11 durable multi-agent control plane (spawn/mailbox/result/budget/fence).

I2 (section 4) adds the mailbox state machine RESULT_RECORDED/UNRESOLVED,
delivery/result accounting, mailbox-head CAS (P0-01), takeover as one atomic
batch of agent + unresolved writes, WAITING/RESUME, operator resolution and
stable fingerprints for every retryable command.

I3 (section 5) adds the resource projections (parent capacity and root
budget), the semantic four-stream spawn transaction (P0-03), the atomic
terminal settlement that releases capacity + budget + parent result in one
batch (P0-05) and the complete LeaseKeeper. All persisted datetimes use the
injected clock which defaults to the Event Store database clock (D2.4).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from ..telemetry.faults import FaultPoint, adapt_fault_callback

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
    MailboxSnapshot,
    MessageKind,
    MessageRecord,
    MessageStatus,
    canonicalize_result,
    result_digest_for,
    result_ref_for,
    summarize_outcome,
)
from .resources import (
    ParentCapacity,
    RootAgentBudget,
    budget_stream,
    capacity_stream,
    canonical_json,
    rebuild_budget,
    rebuild_capacity,
    sha256_digest,
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


def _limits_document(limits: AgentBudgetLimits) -> dict[str, int]:
    return {
        "max_depth": limits.max_depth,
        "max_total_agents": limits.max_total_agents,
        "max_concurrent_children": limits.max_concurrent_children,
    }


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


_STABLE_REASON = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")


def terminal_run_result_ref(agent_id: UUID, run_id: UUID) -> str:
    """Stable result ref for one agent run (section 5.6)."""

    result_id = uuid5(
        NAMESPACE_URL,
        "koawa-v2:agent-run-result:" + str(agent_id) + ":" + str(run_id),
    )
    return "agent-run-result:" + str(result_id)


def terminal_result_identity(
    agent_id: UUID,
    run_id: UUID,
    state: AgentState,
    reason: str | None,
    messages: Sequence[MessageRecord],
) -> tuple[str, str]:
    """Rebuild the exact terminal aggregate from mailbox projections.

    Every processed non-cancelled message must be ACKED and contributes one
    results entry ordered by message sequence. completed/failed require at
    least one result; only CANCELLED (cancelled_before_dispatch) allows an
    empty results list. Returns (result_ref, result_digest); identity
    violations fail closed as agent_result_identity_conflict.
    """

    if state is AgentState.COMPLETED and reason is not None:
        raise AgentError("agent_result_identity_conflict")
    if state in (AgentState.FAILED, AgentState.CANCELLED):
        if not isinstance(reason, str) or not _STABLE_REASON.fullmatch(reason):
            raise AgentError("agent_result_identity_conflict")
    acked = [
        message
        for message in messages
        if message.status is MessageStatus.ACKED
    ]
    for message in messages:
        if (
            message.status is not MessageStatus.ACKED
            and message.status is not MessageStatus.CANCELLED
        ):
            raise AgentError("agent_result_identity_conflict")
    results: list[dict[str, object]] = []
    for message in sorted(acked, key=lambda item: item.sequence):
        if message.result_ref is None or message.result_digest is None:
            raise AgentError("agent_result_identity_conflict")
        results.append(
            {
                "message_id": str(message.message_id),
                "delivery_attempt": message.delivery_attempt,
                "result_ref": message.result_ref,
                "result_digest": message.result_digest,
                "is_error": message.result_is_error,
                "error_code": message.result_error_code,
            }
        )
    if not results and state is not AgentState.CANCELLED:
        raise AgentError("agent_result_identity_conflict")
    aggregate = {
        "schema_version": 1,
        "agent_id": str(agent_id),
        "run_id": str(run_id),
        "terminal_state": state.value,
        "reason": reason,
        "results": results,
    }
    digest = sha256_digest(canonical_json(aggregate))
    return terminal_run_result_ref(agent_id, run_id), digest


@dataclass(frozen=True, slots=True)
class ResourceReconcileReceipt:
    root_agent_id: UUID
    command_id: UUID
    source_global_position: int
    source_digest: str
    released_reservation_ids: tuple[UUID, ...]
    budget_stream_version: int
    changed: bool


class AgentControlPlane:
    """Durable spawn/message/result/terminal commands with exact-version fences."""

    def __init__(
        self,
        event_store,
        *,
        limits: AgentBudgetLimits | None = None,
        clock: Callable[[], datetime] | None = None,
        faults: FaultInjector = NO_FAULTS,
        fault_port=None,
    ) -> None:
        for method in ("append_batch", "read_stream", "read_all", "current_global_position"):
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
        self._faults = adapt_fault_callback(faults, fault_port)
        # Incremental, process-local index over the append-only global log.
        # It never asserts truth: each scan consumes the durable tail after the
        # last observed global position before returning the accumulated spawns.
        self._spawn_scan_cursor = 0
        self._spawn_scan_events: list = []
        self._spawn_scan_lock = RLock()

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
        semantic_idempotency_key: str | None = None,
    ) -> AgentRecord:
        if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
            raise AgentError("invalid_agent_task")
        if not isinstance(principal_id, str) or not principal_id or len(principal_id) > 128:
            raise AgentError("invalid_agent_principal")
        scopes = tuple(sorted(set(scopes)))
        if semantic_idempotency_key is not None:
            if (
                not isinstance(semantic_idempotency_key, str)
                or not semantic_idempotency_key
                or len(semantic_idempotency_key) > 256
            ):
                raise AgentError("invalid_agent_idempotency")
            namespace = "root" if parent_agent_id is None else str(parent_agent_id)
            command_id = uuid5(
                NAMESPACE_URL,
                "koawa-v2:spawn:" + namespace + ":" + semantic_idempotency_key,
            )
        else:
            # One-shot local command: no semantic key means the caller must not
            # auto-retry; the id, child and reservation remain derived from this
            # one command (deprecated path, section 5.5).
            command_id = uuid4()
        child_id = uuid5(command_id, "child")
        reservation_id = uuid5(command_id, "resource-reservation")
        if parent_agent_id is None:
            return self._spawn_root(
                command_id,
                child_id,
                task_id=task_id,
                principal_id=principal_id,
                scopes=scopes,
                context_mode=context_mode,
                turn_id=turn_id,
                parent_run_id=parent_run_id,
            )
        return self._spawn_child(
            command_id,
            child_id,
            reservation_id,
            parent_agent_id=parent_agent_id,
            parent_run_id=parent_run_id,
            task_id=task_id,
            principal_id=principal_id,
            scopes=scopes,
            context_mode=context_mode,
            turn_id=turn_id,
        )

    def _spawn_root(
        self,
        command_id: UUID,
        child_id: UUID,
        *,
        task_id: str,
        principal_id: str,
        scopes: tuple[str, ...],
        context_mode: ContextMode,
        turn_id: UUID | None,
        parent_run_id: UUID | None,
    ) -> AgentRecord:
        """Root spawn: a single spawned.v2 write; no pseudo reserve/release."""
        fingerprint = _fingerprint({
            "operation": "spawn",
            "parent": None,
            "task_id": task_id,
            "principal_id": principal_id,
            "scopes": list(scopes),
            "context_mode": context_mode.value,
            "turn_id": None if turn_id is None else str(turn_id),
            "limits": _limits_document(self.limits),
        })
        for _ in range(MAX_CAS_RETRIES):
            if self._receipt_conflict(command_id, fingerprint):
                raise AgentError("agent_spawn_idempotency_conflict")
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return self._rebuilt_agent(child_id)
            observed_at = self.now()
            spawned = self._spawned_event(
                command_id,
                child_id,
                parent=None,
                root=child_id,
                depth=0,
                task_id=task_id,
                principal_id=principal_id,
                scopes=scopes,
                context_mode=context_mode,
                turn_id=turn_id,
                parent_run_id=parent_run_id,
                observed_at=observed_at,
                capacity_reservation_id=None,
                budget_reservation_id=None,
            )
            try:
                self.event_store.append_batch(
                    (StreamWrite(StreamId("agent", child_id), -1, (spawned,)),),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                raise AgentError("agent_spawn_idempotency_conflict") from None
            self._fault(
                FaultPoint.D11_SPAWN_AFTER_COMMIT,
                _facts(child_id, 1, version=0),
            )
            return self._rebuilt_agent(child_id)
        raise AgentError("agent_spawn_retry_exhausted")

    def _spawn_child(
        self,
        command_id: UUID,
        child_id: UUID,
        reservation_id: UUID,
        *,
        parent_agent_id: UUID,
        parent_run_id: UUID | None,
        task_id: str,
        principal_id: str,
        scopes: tuple[str, ...],
        context_mode: ContextMode,
        turn_id: UUID | None,
    ) -> AgentRecord:
        """Atomic four-stream spawn (section 5.5, P0-03).

        parent child-spawn-authorized + capacity-reserved + budget-reserved.v2
        + child spawned.v2 share one append_batch; any CAS conflict re-reads
        every projection, never just the budget.
        """
        parent = self.graph.load(parent_agent_id)
        if parent is None:
            raise AgentError("parent_agent_missing")
        fingerprint = _fingerprint({
            "operation": "spawn",
            "parent": str(parent_agent_id),
            "parent_run_id": (
                None if parent_run_id is None else str(parent_run_id)
            ),
            "parent_attempt": parent.attempt,
            "task_id": task_id,
            "principal_id": principal_id,
            "scopes": list(scopes),
            "context_mode": context_mode.value,
            "turn_id": None if turn_id is None else str(turn_id),
            "limits": _limits_document(self.limits),
        })
        for _ in range(MAX_CAS_RETRIES):
            if self._receipt_conflict(command_id, fingerprint):
                raise AgentError("agent_spawn_idempotency_conflict")
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return self._rebuilt_agent(child_id)
            parent = self.graph.load(parent_agent_id)
            if parent is None:
                raise AgentError("parent_agent_missing")
            self._validate_spawn_parent(parent, parent_run_id, scopes)
            if self.graph.has_cycle(parent_agent_id, child_id):
                raise AgentError("agent_spawn_cycle")
            depth = (
                parent.depth + 1
                if parent.depth is not None
                else self._depth(parent_agent_id) + 1
            )
            if depth > self.limits.max_depth:
                raise AgentError("agent_depth_exceeded")
            capacity = self._parent_capacity(parent_agent_id)
            if not capacity.exists and parent.legacy_spawn:
                # Legacy v1 parent: the capacity stream must first be
                # bootstrapped by the standalone baseline import.
                self.import_capacity_baseline(parent_agent_id)
                continue
            root = parent.root_agent_id or self.root_for(parent_agent_id)
            budget = self._root_budget(root)
            if capacity.active_count >= self.limits.max_concurrent_children:
                raise AgentError("agent_concurrency_exceeded")
            if budget.active_count + 1 > self.limits.max_total_agents:
                raise AgentError("agent_total_exceeded")
            observed_at = self.now()
            parent_run_wire = (
                None if parent.state is not AgentState.RUNNING else str(parent.run_id)
            )
            authorized = self._event(
                command_id,
                "child-spawn-authorized",
                "agent.child-spawn-authorized.v1",
                {
                    "parent_agent_id": str(parent_agent_id),
                    "parent_run_id": parent_run_wire,
                    "parent_attempt": parent.attempt,
                    "child_agent_id": str(child_id),
                    "root_agent_id": str(root),
                    "depth": depth,
                    "reservation_id": str(reservation_id),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
                turn_id=turn_id,
                run_id=parent_run_id,
            )
            reserved_payload = {
                "parent_agent_id": str(parent_agent_id),
                "root_agent_id": str(root),
                "child_agent_id": str(child_id),
                "reservation_id": str(reservation_id),
            }
            reserved_capacity = self._event(
                command_id,
                "capacity-reserved",
                "agent.capacity-reserved.v1",
                reserved_payload,
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
                turn_id=turn_id,
                run_id=parent_run_id,
            )
            reserved_budget = self._event(
                command_id,
                "budget-reserved",
                "budget.reserved.v2",
                reserved_payload,
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
                turn_id=turn_id,
                run_id=parent_run_id,
            )
            spawned = self._spawned_event(
                command_id,
                child_id,
                parent=parent_agent_id,
                root=root,
                depth=depth,
                task_id=task_id,
                principal_id=principal_id,
                scopes=scopes,
                context_mode=context_mode,
                turn_id=turn_id,
                parent_run_id=parent_run_id,
                observed_at=observed_at,
                capacity_reservation_id=reservation_id,
                budget_reservation_id=reservation_id,
            )
            writes = (
                StreamWrite(
                    StreamId("agent", parent_agent_id),
                    parent.version,
                    (authorized,),
                ),
                StreamWrite(
                    capacity_stream(parent_agent_id),
                    capacity.version,
                    (reserved_capacity,),
                ),
                StreamWrite(
                    budget_stream(root),
                    budget.version,
                    (reserved_budget,),
                ),
                StreamWrite(
                    StreamId("agent", child_id),
                    -1,
                    (spawned,),
                ),
            )
            self._fault(
                FaultPoint.D11_SPAWN_AFTER_READ,
                _facts(parent_agent_id, parent.attempt, version=parent.version),
            )
            self._fault(
                FaultPoint.D11_SPAWN_BEFORE_APPEND,
                _facts(parent_agent_id, parent.attempt, version=parent.version),
            )
            try:
                self.event_store.append_batch(
                    writes,
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                raise AgentError("agent_spawn_idempotency_conflict") from None
            self._fault(
                FaultPoint.D11_SPAWN_AFTER_COMMIT,
                _facts(parent_agent_id, parent.attempt),
            )
            return self._rebuilt_agent(child_id)
        raise AgentError("agent_spawn_retry_exhausted")

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
        if record.state is AgentState.RUNNING:
            # A concurrent winner already holds the agent: this caller lost the
            # start/takeover race.  Report the stable conflict code (identical
            # to the takeover CAS loop) instead of a state-shape error, so
            # concurrent losers can retry/fail consistently.
            raise AgentError("agent_takeover_conflict")
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
            self._fault(
                FaultPoint.D11_TAKEOVER_BEFORE_APPEND,
                _facts(
                    agent_id,
                    new_attempt,
                    message_ids=tuple(item.message_id for item in candidates),
                    version=current.version,
                ),
            )
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
            self._fault(
                FaultPoint.D11_TAKEOVER_AFTER_COMMIT,
                _facts(
                    agent_id,
                    new_attempt,
                    message_ids=tuple(item.message_id for item in candidates),
                ),
            )
            if candidates:
                self._fault(
                    FaultPoint.D11_UNRESOLVED_AFTER_COMMIT,
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
            self._fault(
                FaultPoint.D11_RESUME_BEFORE_APPEND,
                _facts(
                    agent_id,
                    new_attempt,
                    message_ids=tuple(message.message_id for message in blockers),
                    version=current.version,
                ),
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
                FaultPoint.D11_RESUME_AFTER_COMMIT,
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
        attempt: int,
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
            + str(attempt)
            + ":"
            + str(beat_number),
        )
        fingerprint = _fingerprint({
            "operation": "heartbeat",
            "agent_id": str(agent_id),
            "run_id": str(run_id),
            "attempt": attempt,
            "lease_seconds": lease_seconds,
            "beat_number": beat_number,
        })
        for _ in range(max(1, max_cas_retries)):
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return self._rebuilt_agent(agent_id)
            record = self.graph.load(agent_id)
            if record is None or record.run_id != run_id or record.attempt != attempt:
                raise AgentError("agent_lease_lost")
            observed_at = self.now()
            lease = observed_at + timedelta(seconds=lease_seconds)
            self._fault(
                FaultPoint.D11_HEARTBEAT_BEFORE_APPEND,
                _facts(agent_id, attempt, version=record.version),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("agent", agent_id),
                            record.version,
                            (
                                self._event(
                                    command_id,
                                    "agent-heartbeat",
                                    "agent.heartbeat.v2",
                                    {
                                        "agent_id": str(agent_id),
                                        "run_id": str(run_id),
                                        "attempt": attempt,
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
            self._fault(
                FaultPoint.D11_HEARTBEAT_AFTER_COMMIT,
                _facts(agent_id, attempt),
            )
            return self._rebuilt_agent(agent_id)
        raise AgentError("agent_heartbeat_retry_exhausted")

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
                FaultPoint.D11_ENQUEUE_BEFORE_APPEND,
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
                FaultPoint.D11_ENQUEUE_AFTER_COMMIT,
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
                FaultPoint.D11_DELIVER_BEFORE_APPEND,
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
                FaultPoint.D11_DELIVER_AFTER_COMMIT,
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
                FaultPoint.D11_RESULT_BEFORE_APPEND,
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
                FaultPoint.D11_RESULT_AFTER_COMMIT,
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
                FaultPoint.D11_ACK_BEFORE_APPEND,
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
                FaultPoint.D11_ACK_AFTER_COMMIT,
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
                FaultPoint.D11_UNRESOLVED_AFTER_COMMIT,
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
                    FaultPoint.D11_CANCEL_BEFORE_APPEND,
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
                    FaultPoint.D11_CANCEL_AFTER_COMMIT,
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
                FaultPoint.D11_CANCEL_BEFORE_APPEND,
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
                FaultPoint.D11_CANCEL_AFTER_COMMIT,
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
                FaultPoint.D11_WAITING_AFTER_COMMIT,
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
        expected_attempt: int,
        state: Literal[
            AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELLED
        ],
        reason: str | None,
        result_ref: str,
        result_digest: str,
        source_message_ids: tuple[UUID, ...],
    ) -> AgentRecord:
        """Atomic terminal settlement (section 5.6, P0-05).

        One append_batch writes the agent terminal v2 event, the parent
        capacity release, the root budget release.v2 and (when the parent is
        still non-terminal) the parent result message. The agent's own
        capacity head is a StreamPrecondition in every branch, proving the
        active-children check and the settlement share one linearization point.
        """
        if state is AgentState.COMPLETED:
            if reason is not None:
                raise AgentError("agent_result_identity_conflict")
        else:
            if not isinstance(reason, str) or not _STABLE_REASON.fullmatch(reason):
                raise AgentError("agent_result_identity_conflict")
        command_id = uuid5(agent_id, "terminal:" + str(run_id) + ":" + state.value)
        fingerprint = _fingerprint({
            "operation": "terminal",
            "agent_id": str(agent_id),
            "run_id": str(run_id),
            "attempt": expected_attempt,
            "state": state.value,
            "reason": reason,
            "result_ref": result_ref,
            "result_digest": result_digest,
            "policy": "enqueue_parent_if_nonterminal",
        })
        for _ in range(MAX_CAS_RETRIES):
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return self._rebuilt_agent(agent_id)
            record = self.graph.load(agent_id)
            if record is None:
                raise AgentError("agent_missing")
            if record.run_id != run_id or record.attempt != expected_attempt:
                raise AgentError("stale_agent_run_fenced")
            if record.state is not AgentState.RUNNING:
                raise AgentError("agent_attempt_state_invalid")
            own_capacity = self._parent_capacity(agent_id)
            if own_capacity.active_count:
                raise AgentError("agent_children_active")
            observed_at = self.now()
            terminal_event = self._event(
                command_id,
                "agent-terminal",
                {
                    AgentState.COMPLETED: "agent.completed.v2",
                    AgentState.FAILED: "agent.failed.v2",
                    AgentState.CANCELLED: "agent.cancelled.v2",
                }[state],
                {
                    "agent_id": str(agent_id),
                    "run_id": str(run_id),
                    "attempt": expected_attempt,
                    "result_ref": result_ref,
                    "result_digest": result_digest,
                    "terminal_at": observed_at.isoformat(),
                    **(
                        {}
                        if state is AgentState.COMPLETED
                        else {"reason": reason}
                    ),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            writes = [
                StreamWrite(
                    StreamId("agent", agent_id),
                    record.version,
                    (terminal_event,),
                ),
            ]
            preconditions = [
                StreamPrecondition(
                    capacity_stream(agent_id),
                    own_capacity.version,
                ),
            ]
            if record.parent_agent_id is not None:
                parent = self.graph.load(record.parent_agent_id)
                if parent is None:
                    raise AgentError("parent_agent_missing")
                root = (
                    record.root_agent_id
                    if record.root_agent_id is not None
                    else self.root_for(agent_id)
                )
                capacity_reservation_id = (
                    record.capacity_reservation_id
                    if record.capacity_reservation_id is not None
                    else agent_id
                )
                budget_reservation_id = (
                    record.budget_reservation_id
                    if record.budget_reservation_id is not None
                    else agent_id
                )
                parent_capacity = self._parent_capacity(record.parent_agent_id)
                root_budget = self._root_budget(root)
                cap_reservation = parent_capacity.find(capacity_reservation_id)
                bud_reservation = root_budget.find(budget_reservation_id)
                if (
                    cap_reservation is None
                    or bud_reservation is None
                    or capacity_reservation_id != budget_reservation_id
                ):
                    raise AgentError("agent_resource_reconciliation_required")
                for reservation in (cap_reservation, bud_reservation):
                    if (
                        reservation.child_agent_id != agent_id
                        or reservation.parent_agent_id != record.parent_agent_id
                        or reservation.root_agent_id != root
                    ):
                        raise AgentError("agent_resource_reconciliation_required")
                release_payload = {
                    "parent_agent_id": str(record.parent_agent_id),
                    "root_agent_id": str(root),
                    "child_agent_id": str(agent_id),
                    "reservation_id": str(capacity_reservation_id),
                    "terminal_state": state.value,
                    "terminal_run_id": str(run_id),
                    "released_at": observed_at.isoformat(),
                }
                writes.append(
                    StreamWrite(
                        capacity_stream(record.parent_agent_id),
                        parent_capacity.version,
                        (
                            self._event(
                                command_id,
                                "capacity-released",
                                "agent.capacity-released.v1",
                                release_payload,
                                occurred_at=observed_at,
                                correlation_id=_correlation(command_id),
                            ),
                        ),
                    )
                )
                writes.append(
                    StreamWrite(
                        budget_stream(root),
                        root_budget.version,
                        (
                            self._event(
                                command_id,
                                "budget-released",
                                "budget.released.v2",
                                release_payload,
                                occurred_at=observed_at,
                                correlation_id=_correlation(command_id),
                            ),
                        ),
                    )
                )
                parent_terminal = parent.state in (
                    AgentState.COMPLETED,
                    AgentState.FAILED,
                    AgentState.CANCELLED,
                )
                if not parent_terminal:
                    parent_snapshot = self.mailbox.snapshot(
                        record.parent_agent_id
                    )
                    sequence = (
                        parent_snapshot.messages[-1].sequence + 1
                        if parent_snapshot.messages
                        else 0
                    )
                    result_message_id = uuid5(
                        command_id, "parent-result-message"
                    )
                    enqueued = self._event(
                        command_id,
                        "parent-result-enqueued",
                        "message.enqueued.v1",
                        {
                            "agent_id": str(record.parent_agent_id),
                            "message_id": str(result_message_id),
                            "from_agent_id": str(agent_id),
                            "sequence": sequence,
                            "kind": MessageKind.RESULT.value,
                            "body_ref": result_ref,
                            "idempotency_key": "child-result:"
                            + str(agent_id)
                            + ":"
                            + str(run_id),
                            "status": MessageStatus.QUEUED.value,
                        },
                        occurred_at=observed_at,
                        correlation_id=_correlation(command_id),
                    )
                    writes.append(
                        StreamWrite(
                            StreamId("mailbox", record.parent_agent_id),
                            parent_snapshot.stream_version,
                            (enqueued,),
                        )
                    )
                    preconditions.append(
                        StreamPrecondition(
                            StreamId("agent", record.parent_agent_id),
                            parent.version,
                        )
                    )
            # Rebuild identity from the mailbox and compare byte-for-byte with
            # the caller-supplied ref/digest (section 5.6).
            snapshot = self.mailbox.snapshot(agent_id)
            rebuilt_ref, rebuilt_digest = terminal_result_identity(
                agent_id,
                run_id,
                state,
                reason,
                snapshot.messages,
            )
            acked_ids = {
                message.message_id
                for message in snapshot.messages
                if message.status is MessageStatus.ACKED
            }
            if set(source_message_ids) != acked_ids:
                raise AgentError("agent_result_identity_conflict")
            if (
                rebuilt_ref != result_ref
                or not isinstance(result_digest, str)
                or rebuilt_digest != result_digest
            ):
                raise AgentError("agent_result_identity_conflict")
            self._fault(
                FaultPoint.D11_TERMINAL_AFTER_READ,
                _facts(agent_id, expected_attempt, version=record.version),
            )
            self._fault(
                FaultPoint.D11_TERMINAL_BEFORE_APPEND,
                _facts(agent_id, expected_attempt, version=record.version),
            )
            try:
                self.event_store.append_batch(
                    tuple(writes),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=tuple(preconditions),
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return self._rebuilt_agent(agent_id)
            self._fault(
                FaultPoint.D11_TERMINAL_AFTER_COMMIT,
                _facts(agent_id, expected_attempt, version=record.version),
            )
            return self._rebuilt_agent(agent_id)
        raise AgentError("agent_terminal_retry_exhausted")

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
                abandoned_run_id = record.run_id
                if abandoned_run_id is None:
                    continue
                command_id = uuid5(
                    NAMESPACE_URL,
                    "koawa-v2:orphan:"
                    + str(record.agent_id)
                    + ":"
                    + str(abandoned_run_id),
                )
                observed_at = self.now()
                self._fault(
                    FaultPoint.D11_ORPHAN_BEFORE_APPEND,
                    _facts(
                        record.agent_id,
                        record.attempt,
                        version=record.version,
                    ),
                )
                try:
                    self.event_store.append_batch(
                        (
                            StreamWrite(
                                StreamId("agent", record.agent_id),
                                record.version,
                                (
                                    self._event(
                                        command_id,
                                        "agent-orphaned",
                                        "agent.orphaned.v1",
                                        {
                                            "agent_id": str(record.agent_id),
                                            "abandoned_run_id": str(abandoned_run_id),
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
                self._fault(
                    FaultPoint.D11_ORPHAN_AFTER_COMMIT,
                    _facts(record.agent_id, record.attempt),
                )
                orphans.append(self.graph.load(record.agent_id))
        return [item for item in orphans if item is not None]

    def import_capacity_baseline(self, parent_agent_id: UUID) -> ParentCapacity:
        """Standalone first-commit baseline for a legacy v1 parent (5.3).

        Captures a global high-water, scans only events up to that boundary and
        commits agent.capacity-baseline-imported.v1 with expected_version=-1.
        Concurrent initializers lose via WEV and re-read the receipt.
        """
        for _ in range(MAX_CAS_RETRIES):
            high_water = self._current_global_position()
            source, digest = self._baseline_source(parent_agent_id, high_water)
            command_id = uuid5(
                parent_agent_id,
                "capacity-baseline:" + str(high_water) + ":" + digest,
            )
            fingerprint = _fingerprint({
                "operation": "import_capacity_baseline",
                "parent_agent_id": str(parent_agent_id),
                "source_global_position": high_water,
                "source_digest": digest,
            })
            self._read_receipt(command_id, fingerprint)
            capacity = self._parent_capacity(parent_agent_id)
            if capacity.exists:
                # either we or a concurrent initializer already committed:
                # a non-baseline first event means corruption.
                first_events = self._read_all(capacity_stream(parent_agent_id))
                if (
                    not first_events
                    or first_events[0].event_type
                    != "agent.capacity-baseline-imported.v1"
                ):
                    raise AgentError("agent_capacity_projection_corrupt")
                return capacity
            observed_at = self.now()
            baseline = self._event(
                command_id,
                "capacity-baseline-imported",
                "agent.capacity-baseline-imported.v1",
                {
                    "parent_agent_id": str(parent_agent_id),
                    "reservations": [
                        {
                            "reservation_id": item["child_agent_id"],
                            "child_agent_id": item["child_agent_id"],
                            "root_agent_id": item["root_agent_id"],
                        }
                        for item in source["active_children"]
                    ],
                    "source_global_position": high_water,
                    "source_digest": digest,
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            self._fault(
                FaultPoint.D11_RESOURCES_BASELINE_BEFORE_APPEND,
                _facts(parent_agent_id, 1, version=high_water),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            capacity_stream(parent_agent_id),
                            -1,
                            (baseline,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                )
            except WrongExpectedVersion:
                capacity = self._parent_capacity(parent_agent_id)
                if capacity.exists:
                    first_events = self._read_all(
                        capacity_stream(parent_agent_id)
                    )
                    if (
                        not first_events
                        or first_events[0].event_type
                        != "agent.capacity-baseline-imported.v1"
                    ):
                        raise AgentError("agent_capacity_projection_corrupt")
                    return capacity
                continue
            except IdempotencyConflict:
                raise AgentError("agent_capacity_projection_corrupt") from None
            self._fault(
                FaultPoint.D11_RESOURCES_BASELINE_AFTER_COMMIT,
                _facts(parent_agent_id, 1),
            )
            return self._parent_capacity(parent_agent_id)
        raise AgentError("agent_resource_reconciliation_required")

    def reconcile_legacy_resources(
        self,
        root_agent_id: UUID,
        *,
        expected_budget_version: int | None = None,
    ) -> ResourceReconcileReceipt:
        """Release only reservations provably terminal from Agent events.

        Empty releases return changed=false with zero events and zero receipt
        writes; otherwise one typed budget.legacy-reconciled.v1 is appended at
        the exact budget version. History is never modified; unknown,
        non-terminal or identity-mismatched reservations fail closed.
        """
        for _ in range(MAX_CAS_RETRIES):
            high_water = self._current_global_position()
            source, digest = self._reconcile_source(root_agent_id, high_water)
            command_id = uuid5(
                root_agent_id,
                "legacy-resource-reconcile:"
                + str(high_water)
                + ":"
                + digest,
            )
            released_ids = tuple(
                UUID(item["reservation_id"]) for item in source["releases"]
            )
            budget = self._root_budget(root_agent_id)
            if not source["releases"]:
                return ResourceReconcileReceipt(
                    root_agent_id,
                    command_id,
                    high_water,
                    digest,
                    (),
                    budget.version,
                    False,
                )
            version = (
                expected_budget_version
                if expected_budget_version is not None
                else budget.version
            )
            fingerprint = _fingerprint({
                "operation": "reconcile_legacy_resources",
                "root_agent_id": str(root_agent_id),
                "source_global_position": high_water,
                "source_digest": digest,
                "releases": [str(item) for item in released_ids],
            })
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                budget_after = self._root_budget(root_agent_id)
                return ResourceReconcileReceipt(
                    root_agent_id,
                    command_id,
                    high_water,
                    digest,
                    released_ids,
                    budget_after.version,
                    True,
                )
            observed_at = self.now()
            event = self._event(
                command_id,
                "budget-legacy-reconciled",
                "budget.legacy-reconciled.v1",
                {
                    "root_agent_id": str(root_agent_id),
                    "releases": source["releases"],
                    "source_global_position": high_water,
                    "source_digest": digest,
                    "reconciled_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            budget_stream(root_agent_id),
                            version,
                            (event,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                budget_after = self._root_budget(root_agent_id)
                return ResourceReconcileReceipt(
                    root_agent_id,
                    command_id,
                    high_water,
                    digest,
                    released_ids,
                    budget_after.version,
                    True,
                )
            budget_after = self._root_budget(root_agent_id)
            return ResourceReconcileReceipt(
                root_agent_id,
                command_id,
                high_water,
                digest,
                released_ids,
                budget_after.version,
                True,
            )
        raise AgentError("agent_resource_reconciliation_required")

    def _baseline_source(
        self, parent_agent_id: UUID, high_water: int
    ) -> tuple[dict, str]:
        """Build the exact baseline source document up to the high-water."""
        all_events = self._read_all_through(high_water)
        by_stream: dict[str, list] = {}
        for event in all_events:
            by_stream.setdefault(event.stream_id.key, []).append(event)
        children: list[dict] = []
        for event in all_events:
            if event.event_type not in (
                "agent.spawned.v1",
                "agent.spawned.v2",
            ):
                continue
            if event.payload.get("parent_agent_id") != str(parent_agent_id):
                continue
            child_text = event.payload.get("agent_id")
            if not isinstance(child_text, str):
                raise AgentError("agent_resource_reconciliation_required")
            child_id = UUID(child_text)
            if event.event_type == "agent.spawned.v2":
                # a v2 child under a legacy parent cannot predate the baseline
                raise AgentError("agent_resource_reconciliation_required")
            stream_events = by_stream.get(
                StreamId("agent", child_id).key, ()
            )
            try:
                record = rebuild_agent(child_id, tuple(stream_events))
            except AgentError:
                raise AgentError("agent_resource_reconciliation_required") from None
            if record is None:
                raise AgentError("agent_resource_reconciliation_required")
            if record.state not in (
                AgentState.CREATED,
                AgentState.RUNNING,
                AgentState.WAITING,
                AgentState.ORPHANED,
            ):
                continue
            root = self._root_from_scan(child_id, by_stream)
            if root is None:
                raise AgentError("agent_resource_reconciliation_required")
            children.append(
                {
                    "child_agent_id": str(child_id),
                    "root_agent_id": str(root),
                    "reservation_id": str(child_id),
                    "agent_state": record.state.value,
                    "agent_stream_version": record.version,
                }
            )
        children.sort(key=lambda item: item["child_agent_id"])
        source = {
            "schema_version": 1,
            "source_global_position": high_water,
            "parent_agent_id": str(parent_agent_id),
            "active_children": children,
        }
        digest = sha256_digest(canonical_json(source))
        return source, digest

    def _root_from_scan(
        self, agent_id: UUID, by_stream: Mapping[str, list]
    ) -> UUID | None:
        """Resolve the root from spawn events; missing/cycle returns None."""
        current: UUID = agent_id
        seen: set[UUID] = set()
        while True:
            if current in seen:
                return None
            seen.add(current)
            events = by_stream.get(StreamId("agent", current).key)
            if not events:
                return None
            spawn = events[0]
            if spawn.event_type not in (
                "agent.spawned.v1",
                "agent.spawned.v2",
            ):
                return None
            parent = spawn.payload.get("parent_agent_id")
            if parent is None:
                return current
            try:
                current = UUID(parent)
            except ValueError:
                return None

    def _reconcile_source(
        self, root_agent_id: UUID, high_water: int
    ) -> tuple[dict, str]:
        """Build the exact reconcile source document up to the high-water."""
        all_events = self._read_all_through(high_water)
        terminal_by_child: dict[str, tuple[str, str]] = {}
        terminal_types = (
            "agent.completed.v1",
            "agent.completed.v2",
            "agent.failed.v1",
            "agent.failed.v2",
            "agent.cancelled.v1",
            "agent.cancelled.v2",
        )
        for event in all_events:
            if event.event_type not in terminal_types:
                continue
            child = event.payload.get("agent_id")
            run = event.payload.get("run_id")
            if not isinstance(child, str) or not isinstance(run, str):
                raise AgentError("agent_resource_reconciliation_required")
            state = event.event_type.split(".")[1]
            terminal_by_child.setdefault(child, (state, run))
        budget = self._root_budget(root_agent_id)
        releases: list[dict] = []
        for reservation in budget.active_reservations:
            terminal = terminal_by_child.get(
                str(reservation.child_agent_id)
            )
            if terminal is None:
                continue
            releases.append(
                {
                    "reservation_id": str(reservation.reservation_id),
                    "child_agent_id": str(reservation.child_agent_id),
                    "parent_agent_id": str(reservation.parent_agent_id),
                    "terminal_state": terminal[0],
                    "terminal_run_id": terminal[1],
                }
            )
        releases.sort(key=lambda item: item["reservation_id"])
        source = {
            "schema_version": 1,
            "root_agent_id": str(root_agent_id),
            "source_global_position": high_water,
            "releases": releases,
        }
        digest = sha256_digest(canonical_json(source))
        return source, digest

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

    def _receipt_conflict(self, command_id: UUID, fingerprint: str) -> bool:
        """True when the same command id was committed under a different
        semantic fingerprint (the caller reused an idempotency key)."""

        read_idempotency = getattr(self.event_store, "read_idempotency", None)
        if read_idempotency is None:
            return False
        try:
            read_idempotency(command_id, request_fingerprint=fingerprint)
            return False
        except IdempotencyConflict:
            return True

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

    def _event(
        self,
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
        return _event(
            command_id,
            slot,
            event_type,
            payload,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            turn_id=turn_id,
            run_id=run_id,
        )

    def _depth(self, agent_id: UUID) -> int:
        record = self.graph.load(agent_id)
        if record is not None and record.depth is not None:
            return record.depth
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
        """Compatibility count of active non-root reservations under a root."""

        return self._root_budget(root_agent_id).active_count

    def _validate_spawn_parent(
        self,
        parent: AgentRecord,
        parent_run_id: UUID | None,
        scopes: tuple[str, ...],
    ) -> None:
        if parent.state in (
            AgentState.COMPLETED,
            AgentState.FAILED,
            AgentState.CANCELLED,
        ):
            raise AgentError("parent_agent_not_active")
        if parent.state is AgentState.ORPHANED:
            raise AgentError("parent_agent_orphaned")
        if parent.state is AgentState.RUNNING:
            if parent_run_id is None:
                raise AgentError("parent_run_required")
            if parent_run_id != parent.run_id:
                raise AgentError("parent_run_fenced")
        if not set(scopes).issubset(set(parent.scopes)):
            raise AgentError("child_scope_escalation")

    def _spawned_event(
        self,
        command_id: UUID,
        child_id: UUID,
        *,
        parent: UUID | None,
        root: UUID,
        depth: int,
        task_id: str,
        principal_id: str,
        scopes: tuple[str, ...],
        context_mode: ContextMode,
        turn_id: UUID | None,
        parent_run_id: UUID | None,
        observed_at: datetime,
        capacity_reservation_id: UUID | None,
        budget_reservation_id: UUID | None,
    ) -> NewEvent:
        return self._event(
            command_id,
            "agent-spawned",
            "agent.spawned.v2",
            {
                "agent_id": str(child_id),
                "parent_agent_id": (
                    None if parent is None else str(parent)
                ),
                "root_agent_id": str(root),
                "task_id": task_id,
                "attempt": 1,
                "run_id": None,
                "principal_id": principal_id,
                "scopes": list(scopes),
                "context_mode": context_mode.value,
                "created_at": observed_at.isoformat(),
                "depth": depth,
                "capacity_reservation_id": (
                    None
                    if capacity_reservation_id is None
                    else str(capacity_reservation_id)
                ),
                "budget_reservation_id": (
                    None
                    if budget_reservation_id is None
                    else str(budget_reservation_id)
                ),
            },
            occurred_at=observed_at,
            correlation_id=_correlation(command_id),
            turn_id=turn_id,
            run_id=parent_run_id,
        )

    def _parent_capacity(self, parent_agent_id: UUID) -> ParentCapacity:
        events = self._read_all(capacity_stream(parent_agent_id))
        return rebuild_capacity(parent_agent_id, events)

    def _root_budget(self, root_agent_id: UUID) -> RootAgentBudget:
        events = self._read_all(budget_stream(root_agent_id))
        return rebuild_budget(
            root_agent_id,
            events,
            resolve_parent=self._legacy_parent_for,
        )

    def _legacy_parent_for(self, child_agent_id: UUID) -> UUID | None:
        record = self.graph.load(child_agent_id)
        if record is None:
            return None
        return record.parent_agent_id

    def _current_global_position(self) -> int:
        return self.event_store.current_global_position()

    def _read_all_through(self, through_position: int) -> tuple:
        values = []
        cursor = 0
        while True:
            page = self.event_store.read_all(
                after_position=cursor,
                through_position=through_position,
                limit=500,
            )
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].global_position

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
        with self._spawn_scan_lock:
            cursor = self._spawn_scan_cursor
            while True:
                page = self.event_store.read_all(after_position=cursor, limit=500)
                self._spawn_scan_events.extend(
                    event
                    for event in page
                    if event.event_type in ("agent.spawned.v1", "agent.spawned.v2")
                )
                if page:
                    cursor = page[-1].global_position
                    self._spawn_scan_cursor = cursor
                if len(page) < 500:
                    return list(self._spawn_scan_events)



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
