"""D11 durable parent/child Agent graph model (event-sourced).

I3 (section 5.4) adds agent.spawned.v2, agent.child-spawn-authorized.v1,
agent.heartbeat.v2 and terminal v2 events with exact keys; the reducer keeps
v1 events replayable and hardens every transition (started only from CREATED,
takeover only from ORPHANED, heartbeat only from RUNNING with matching
run/attempt, terminal only from RUNNING). Illegal history fails closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from typing import Any, Mapping
from uuid import UUID

from ..control.event_store import StreamId


_STABLE_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
_SCOPE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")


class AgentError(RuntimeError):
    """Stable, content-free D11 control-plane failure."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _STABLE_CODE.fullmatch(code):
            raise ValueError("invalid agent error code")
        self.code = code
        super().__init__(code)


class AgentState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


class ContextMode(StrEnum):
    FRESH = "fresh"
    FORK = "fork"


@dataclass(frozen=True, slots=True)
class AgentRecord:
    agent_id: UUID
    parent_agent_id: UUID | None
    task_id: str
    attempt: int
    run_id: UUID | None
    principal_id: str
    scopes: tuple[str, ...]
    context_mode: ContextMode
    state: AgentState
    version: int
    created_at: datetime
    lease_expires_at: datetime | None = None
    outcome: str | None = None
    reason: str | None = None
    abandoned_run_id: UUID | None = None
    waiting_run_id: UUID | None = None
    blocking_message_ids: tuple[UUID, ...] = ()
    root_agent_id: UUID | None = None
    depth: int | None = None
    capacity_reservation_id: UUID | None = None
    budget_reservation_id: UUID | None = None
    result_ref: str | None = None
    result_digest: str | None = None
    legacy_spawn: bool = False

    def to_document(self) -> dict[str, Any]:
        return {
            "agent_id": str(self.agent_id),
            "parent_agent_id": (
                None if self.parent_agent_id is None else str(self.parent_agent_id)
            ),
            "task_id": self.task_id,
            "attempt": self.attempt,
            "run_id": None if self.run_id is None else str(self.run_id),
            "principal_id": self.principal_id,
            "scopes": list(self.scopes),
            "context_mode": self.context_mode.value,
            "state": self.state.value,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "lease_expires_at": (
                None if self.lease_expires_at is None else self.lease_expires_at.isoformat()
            ),
            "outcome": self.outcome,
            "reason": self.reason,
            "abandoned_run_id": (
                None if self.abandoned_run_id is None else str(self.abandoned_run_id)
            ),
            "waiting_run_id": (
                None if self.waiting_run_id is None else str(self.waiting_run_id)
            ),
            "blocking_message_ids": [
                str(item) for item in self.blocking_message_ids
            ],
            "root_agent_id": (
                None if self.root_agent_id is None else str(self.root_agent_id)
            ),
            "depth": self.depth,
            "capacity_reservation_id": (
                None
                if self.capacity_reservation_id is None
                else str(self.capacity_reservation_id)
            ),
            "budget_reservation_id": (
                None
                if self.budget_reservation_id is None
                else str(self.budget_reservation_id)
            ),
            "result_ref": self.result_ref,
            "result_digest": self.result_digest,
            "legacy_spawn": self.legacy_spawn,
        }


def _require_uuid(payload: Mapping[str, Any], key: str) -> UUID | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentError("corrupt_agent_stream")
    try:
        return UUID(value)
    except ValueError:
        raise AgentError("corrupt_agent_stream") from None


def _require_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AgentError("corrupt_agent_stream")
    return value


def _require_enum(payload: Mapping[str, Any], key: str, enum_type) -> Any:
    value = payload.get(key)
    try:
        return enum_type(value)
    except ValueError:
        raise AgentError("corrupt_agent_stream") from None


def _require_uuid_list(payload: Mapping[str, Any], key: str) -> tuple[UUID, ...]:
    value = payload.get(key)
    if not isinstance(value, (list, tuple)):
        raise AgentError("corrupt_agent_stream")
    result: list[UUID] = []
    for item in value:
        if not isinstance(item, str):
            raise AgentError("corrupt_agent_stream")
        try:
            result.append(UUID(item))
        except ValueError:
            raise AgentError("corrupt_agent_stream") from None
    return tuple(result)


def _exact_keys(payload: Mapping[str, Any], keys: set[str]) -> None:
    """Exact-key validation: unknown or missing fields corrupt the stream."""

    if not isinstance(payload, Mapping):
        raise AgentError("corrupt_agent_stream")
    if set(payload.keys()) != keys:
        raise AgentError("corrupt_agent_stream")


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentError("corrupt_agent_stream")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def rebuild_agent(
    agent_id: UUID,
    events: tuple,
    *,
    initial: AgentRecord | None = None,
) -> AgentRecord | None:
    """Replay one agent stream into its current durable state.

    Supports the I2/I3 wire: spawned.v1/.v2, child-spawn-authorized.v1,
    started, heartbeat.v1/.v2, orphaned, taken-over.v1/.v2, waiting, resumed
    and terminal v1/.v2 events. State machines are enforced during replay:
    started only from CREATED, takeover only from ORPHANED, heartbeat only
    from RUNNING with matching run/attempt, terminal only from RUNNING; any
    illegal history fails closed as corrupt_agent_stream.
    """

    if initial is not None and initial.agent_id != agent_id:
        raise AgentError("corrupt_agent_stream")
    record: AgentRecord | None = initial
    for event in events:
        payload = event.payload
        if event.event_type == "agent.spawned.v1":
            if record is not None or event.stream_version != 0:
                raise AgentError("corrupt_agent_stream")
            parent = _require_uuid(payload, "parent_agent_id")
            record = AgentRecord(
                agent_id=agent_id,
                parent_agent_id=parent,
                task_id=_require_text(payload, "task_id"),
                attempt=int(payload["attempt"]),
                run_id=_require_uuid(payload, "run_id"),
                principal_id=_require_text(payload, "principal_id"),
                scopes=tuple(sorted(payload.get("scopes", ()))),
                context_mode=_require_enum(payload, "context_mode", ContextMode),
                state=AgentState.CREATED,
                version=event.stream_version,
                created_at=_optional_datetime(payload.get("created_at")),
                legacy_spawn=True,
            )
            continue
        if event.event_type == "agent.spawned.v2":
            if record is not None or event.stream_version != 0:
                raise AgentError("corrupt_agent_stream")
            _exact_keys(
                payload,
                {
                    "agent_id",
                    "parent_agent_id",
                    "root_agent_id",
                    "task_id",
                    "attempt",
                    "run_id",
                    "principal_id",
                    "scopes",
                    "context_mode",
                    "created_at",
                    "depth",
                    "capacity_reservation_id",
                    "budget_reservation_id",
                },
            )
            parent = _require_uuid(payload, "parent_agent_id")
            root = _require_uuid(payload, "root_agent_id")
            depth = int(payload["depth"])
            capacity_reservation = _require_uuid(payload, "capacity_reservation_id")
            budget_reservation = _require_uuid(payload, "budget_reservation_id")
            if parent is None:
                if root != agent_id or depth != 0:
                    raise AgentError("corrupt_agent_stream")
                if capacity_reservation is not None or budget_reservation is not None:
                    raise AgentError("corrupt_agent_stream")
            else:
                if depth < 1:
                    raise AgentError("corrupt_agent_stream")
                if capacity_reservation is None or budget_reservation is None:
                    raise AgentError("corrupt_agent_stream")
                if capacity_reservation != budget_reservation:
                    raise AgentError("corrupt_agent_stream")
            record = AgentRecord(
                agent_id=agent_id,
                parent_agent_id=parent,
                task_id=_require_text(payload, "task_id"),
                attempt=int(payload["attempt"]),
                run_id=_require_uuid(payload, "run_id"),
                principal_id=_require_text(payload, "principal_id"),
                scopes=tuple(sorted(payload.get("scopes", ()))),
                context_mode=_require_enum(payload, "context_mode", ContextMode),
                state=AgentState.CREATED,
                version=event.stream_version,
                created_at=_optional_datetime(payload.get("created_at")),
                root_agent_id=root,
                depth=depth,
                capacity_reservation_id=capacity_reservation,
                budget_reservation_id=budget_reservation,
            )
            continue
        if record is None or event.stream_version != record.version + 1:
            raise AgentError("corrupt_agent_stream")
        if (
            event.event_type != "agent.child-spawn-authorized.v1"
            and payload.get("agent_id") != str(agent_id)
        ):
            raise AgentError("corrupt_agent_stream")
        if event.event_type == "agent.started.v1":
            if record.state is not AgentState.CREATED:
                raise AgentError("corrupt_agent_stream")
            run_id = _require_uuid(payload, "run_id")
            record = replace(
                record,
                state=AgentState.RUNNING,
                version=event.stream_version,
                run_id=run_id,
                attempt=int(payload["attempt"]),
                lease_expires_at=_optional_datetime(payload.get("lease_expires_at")),
                abandoned_run_id=None,
            )
        elif event.event_type in ("agent.heartbeat.v1", "agent.heartbeat.v2"):
            if record.state is not AgentState.RUNNING:
                raise AgentError("corrupt_agent_stream")
            if payload.get("run_id") != str(record.run_id):
                raise AgentError("stale_agent_run_fenced")
            if event.event_type == "agent.heartbeat.v2":
                if int(payload["attempt"]) != record.attempt:
                    raise AgentError("corrupt_agent_stream")
            record = replace(
                record,
                version=event.stream_version,
                lease_expires_at=_optional_datetime(payload.get("lease_expires_at")),
            )
        elif event.event_type == "agent.child-spawn-authorized.v1":
            _exact_keys(
                payload,
                {
                    "parent_agent_id",
                    "parent_run_id",
                    "parent_attempt",
                    "child_agent_id",
                    "root_agent_id",
                    "depth",
                    "reservation_id",
                },
            )
            if payload["parent_agent_id"] != str(agent_id):
                raise AgentError("corrupt_agent_stream")
            if record.state in (
                AgentState.COMPLETED,
                AgentState.FAILED,
                AgentState.CANCELLED,
            ):
                raise AgentError("corrupt_agent_stream")
            parent_run_id = _require_uuid(payload, "parent_run_id")
            parent_attempt = int(payload["parent_attempt"])
            if parent_run_id is not None:
                if (
                    record.state is not AgentState.RUNNING
                    or record.run_id != parent_run_id
                    or record.attempt != parent_attempt
                ):
                    raise AgentError("stale_agent_run_fenced")
            declared_depth = int(payload["depth"])
            if record.depth is not None and declared_depth != record.depth + 1:
                raise AgentError("corrupt_agent_stream")
            record = replace(record, version=event.stream_version)
        elif event.event_type == "agent.orphaned.v1":
            if record.state is not AgentState.RUNNING:
                raise AgentError("corrupt_agent_stream")
            record = replace(
                record,
                state=AgentState.ORPHANED,
                version=event.stream_version,
                abandoned_run_id=_require_uuid(payload, "abandoned_run_id"),
                run_id=None,
            )
        elif event.event_type in ("agent.taken-over.v1", "agent.taken-over.v2"):
            if record.state is not AgentState.ORPHANED:
                raise AgentError("invalid_agent_takeover")
            if event.event_type == "agent.taken-over.v2":
                if payload.get("abandoned_run_id") != str(record.abandoned_run_id):
                    raise AgentError("corrupt_agent_stream")
            record = replace(
                record,
                state=AgentState.RUNNING,
                version=event.stream_version,
                attempt=int(payload["attempt"]),
                run_id=_require_uuid(payload, "run_id"),
                lease_expires_at=_optional_datetime(payload.get("lease_expires_at")),
                abandoned_run_id=None,
                waiting_run_id=None,
                blocking_message_ids=(),
            )
        elif event.event_type == "agent.waiting-for-message-resolution.v1":
            if record.state is not AgentState.RUNNING:
                raise AgentError("invalid_agent_waiting")
            run_id = _require_uuid(payload, "run_id")
            if run_id != record.run_id:
                raise AgentError("stale_agent_run_fenced")
            if int(payload["attempt"]) != record.attempt:
                raise AgentError("corrupt_agent_stream")
            blocking = _require_uuid_list(payload, "blocking_message_ids")
            if not blocking:
                raise AgentError("corrupt_agent_stream")
            record = replace(
                record,
                state=AgentState.WAITING,
                version=event.stream_version,
                run_id=None,
                lease_expires_at=None,
                waiting_run_id=run_id,
                blocking_message_ids=blocking,
            )
        elif event.event_type == "agent.resumed.v1":
            if record.state is not AgentState.WAITING:
                raise AgentError("invalid_agent_resume")
            if payload.get("previous_run_id") != str(record.waiting_run_id):
                raise AgentError("stale_agent_run_fenced")
            block_ids = set(record.blocking_message_ids)
            resolved = _require_uuid_list(payload, "resolved_message_ids")
            if set(resolved) != block_ids:
                raise AgentError("corrupt_agent_stream")
            record = replace(
                record,
                state=AgentState.RUNNING,
                version=event.stream_version,
                attempt=int(payload["attempt"]),
                run_id=_require_uuid(payload, "run_id"),
                lease_expires_at=_optional_datetime(payload.get("lease_expires_at")),
                waiting_run_id=None,
                blocking_message_ids=(),
            )
        elif event.event_type == "agent.completed.v1":
            if record.state is not AgentState.RUNNING:
                raise AgentError("corrupt_agent_stream")
            if payload.get("run_id") != str(record.run_id):
                raise AgentError("stale_agent_run_fenced")
            record = replace(
                record,
                state=AgentState.COMPLETED,
                version=event.stream_version,
                outcome=_require_text(payload, "outcome"),
                run_id=None,
                lease_expires_at=None,
            )
        elif event.event_type == "agent.failed.v1":
            if record.state is not AgentState.RUNNING:
                raise AgentError("corrupt_agent_stream")
            if payload.get("run_id") != str(record.run_id):
                raise AgentError("stale_agent_run_fenced")
            record = replace(
                record,
                state=AgentState.FAILED,
                version=event.stream_version,
                reason=_require_text(payload, "reason"),
                run_id=None,
                lease_expires_at=None,
            )
        elif event.event_type == "agent.cancelled.v1":
            if record.state is not AgentState.RUNNING:
                raise AgentError("corrupt_agent_stream")
            if payload.get("run_id") != str(record.run_id):
                raise AgentError("stale_agent_run_fenced")
            record = replace(
                record,
                state=AgentState.CANCELLED,
                version=event.stream_version,
                reason=_require_text(payload, "reason"),
                run_id=None,
                lease_expires_at=None,
            )
        elif event.event_type == "agent.completed.v2":
            if record.state is not AgentState.RUNNING:
                raise AgentError("corrupt_agent_stream")
            if payload.get("run_id") != str(record.run_id):
                raise AgentError("stale_agent_run_fenced")
            _exact_keys(
                payload,
                {
                    "agent_id",
                    "run_id",
                    "attempt",
                    "result_ref",
                    "result_digest",
                    "terminal_at",
                },
            )
            if int(payload["attempt"]) != record.attempt:
                raise AgentError("corrupt_agent_stream")
            record = replace(
                record,
                state=AgentState.COMPLETED,
                version=event.stream_version,
                outcome=None,
                reason=None,
                run_id=None,
                lease_expires_at=None,
                result_ref=_require_text(payload, "result_ref"),
                result_digest=_require_text(payload, "result_digest"),
            )
        elif event.event_type in ("agent.failed.v2", "agent.cancelled.v2"):
            if record.state is not AgentState.RUNNING:
                raise AgentError("corrupt_agent_stream")
            if payload.get("run_id") != str(record.run_id):
                raise AgentError("stale_agent_run_fenced")
            _exact_keys(
                payload,
                {
                    "agent_id",
                    "run_id",
                    "attempt",
                    "result_ref",
                    "result_digest",
                    "terminal_at",
                    "reason",
                },
            )
            if int(payload["attempt"]) != record.attempt:
                raise AgentError("corrupt_agent_stream")
            terminal_state = (
                AgentState.FAILED
                if event.event_type == "agent.failed.v2"
                else AgentState.CANCELLED
            )
            record = replace(
                record,
                state=terminal_state,
                version=event.stream_version,
                reason=_require_text(payload, "reason"),
                run_id=None,
                lease_expires_at=None,
                result_ref=_require_text(payload, "result_ref"),
                result_digest=_require_text(payload, "result_digest"),
            )
        else:
            raise AgentError("unknown_agent_event")
    return record


class AgentGraph:
    """Read-only graph facade over per-agent event streams."""

    def __init__(self, event_store) -> None:
        if not hasattr(event_store, "read_stream"):
            raise TypeError("event_store must implement read_stream")
        self._event_store = event_store
        # The log remains authoritative.  This cache only stores a projection
        # plus its exact stream version; every load asks the store for the tail
        # after that version, so writes from other processes remain visible.
        self._cache: dict[UUID, AgentRecord] = {}
        self._cache_lock = RLock()
        self._spawn_cursor = 0
        self._children_by_parent: dict[UUID, list[UUID]] = {}

    def load(self, agent_id: UUID) -> AgentRecord | None:
        if not isinstance(agent_id, UUID):
            raise TypeError("agent_id must be UUID")
        with self._cache_lock:
            cached = self._cache.get(agent_id)
            events = self._read_all(
                StreamId("agent", agent_id),
                after_version=-1 if cached is None else cached.version,
            )
            if not events:
                return cached
            rebuilt = rebuild_agent(agent_id, events, initial=cached)
            if rebuilt is not None:
                self._cache[agent_id] = rebuilt
            return rebuilt

    def children(self, parent_agent_id: UUID) -> list[AgentRecord]:
        """Return children whose spawn event names this parent."""
        if not isinstance(parent_agent_id, UUID):
            raise TypeError("parent_agent_id must be UUID")
        with self._cache_lock:
            cursor = self._spawn_cursor
            while True:
                page = self._event_store.read_all(after_position=cursor, limit=500)
                for event in page:
                    if event.event_type not in ("agent.spawned.v1", "agent.spawned.v2"):
                        continue
                    raw_parent = event.payload.get("parent_agent_id")
                    raw_agent = event.payload.get("agent_id")
                    if not isinstance(raw_parent, str) or not isinstance(raw_agent, str):
                        continue
                    try:
                        parent_id, agent_id = UUID(raw_parent), UUID(raw_agent)
                    except ValueError:
                        continue
                    children = self._children_by_parent.setdefault(parent_id, [])
                    if agent_id not in children:
                        children.append(agent_id)
                if page:
                    cursor = page[-1].global_position
                    self._spawn_cursor = cursor
                if len(page) < 500:
                    break
            child_ids = tuple(self._children_by_parent.get(parent_agent_id, ()))
        results = []
        for child_id in child_ids:
            record = self.load(child_id)
            if record is not None:
                results.append(record)
        return sorted(results, key=lambda item: str(item.agent_id))

    def has_cycle(self, parent_agent_id: UUID, child_agent_id: UUID) -> bool:
        """True if child is an ancestor of parent (spawn would create a cycle)."""

        current: UUID | None = parent_agent_id
        while current is not None:
            if current == child_agent_id:
                return True
            record = self.load(current)
            current = record.parent_agent_id if record is not None else None
        return False

    def _read_all(self, stream: StreamId, *, after_version: int = -1) -> tuple:
        values = []
        cursor = after_version
        while True:
            page = self._event_store.read_stream(
                stream, after_version=cursor, limit=500
            )
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version
