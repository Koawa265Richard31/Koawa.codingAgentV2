"""D11 durable parent/child Agent graph model (event-sourced)."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
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

    @property
    def depth(self) -> int:
        return 0 if self.parent_agent_id is None else 1

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
        }


def _require_uuid(payload: Mapping[str, Any], key: str) -> UUID:
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


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentError("corrupt_agent_stream")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def rebuild_agent(agent_id: UUID, events: tuple) -> AgentRecord | None:
    """Replay one agent stream into its current durable state."""

    record: AgentRecord | None = None
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
            )
            continue
        if record is None or event.stream_version != record.version + 1:
            raise AgentError("corrupt_agent_stream")
        if payload.get("agent_id") != str(agent_id):
            raise AgentError("corrupt_agent_stream")
        if event.event_type == "agent.started.v1":
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
        elif event.event_type == "agent.heartbeat.v1":
            if payload.get("run_id") != str(record.run_id):
                raise AgentError("stale_agent_run_fenced")
            record = replace(
                record,
                version=event.stream_version,
                lease_expires_at=_optional_datetime(payload.get("lease_expires_at")),
            )
        elif event.event_type == "agent.orphaned.v1":
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
        else:
            raise AgentError("unknown_agent_event")
    return record


class AgentGraph:
    """Read-only graph facade over per-agent event streams."""

    def __init__(self, event_store) -> None:
        if not hasattr(event_store, "read_stream"):
            raise TypeError("event_store must implement read_stream")
        self._event_store = event_store

    def load(self, agent_id: UUID) -> AgentRecord | None:
        if not isinstance(agent_id, UUID):
            raise TypeError("agent_id must be UUID")
        events = self._read_all(StreamId("agent", agent_id))
        return None if not events else rebuild_agent(agent_id, events)

    def children(self, parent_agent_id: UUID) -> list[AgentRecord]:
        """Return children whose spawn event names this parent."""

        # The graph is append-only; a child registry is not maintained, so a
        # linear scan over the agent category is bounded by D11's small scale.
        results: list[AgentRecord] = []
        cursor = 0
        while True:
            page = self._event_store.read_all(after_position=cursor, limit=500)
            for event in page:
                if event.event_type == "agent.spawned.v1":
                    payload = event.payload
                    raw_parent = payload.get("parent_agent_id")
                    if raw_parent != str(parent_agent_id):
                        continue
                    raw_agent = payload.get("agent_id")
                    if not isinstance(raw_agent, str):
                        continue
                    try:
                        record = self.load(UUID(raw_agent))
                    except ValueError:
                        continue
                    if record is not None:
                        results.append(record)
            if len(page) < 500:
                break
            cursor = page[-1].global_position
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

    def _read_all(self, stream: StreamId) -> tuple:
        values = []
        cursor = -1
        while True:
            page = self._event_store.read_stream(
                stream, after_version=cursor, limit=500
            )
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version
