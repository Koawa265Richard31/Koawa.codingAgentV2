"""D11 durable mailbox: per-agent sequence, dedupe, delivery state."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping
from uuid import UUID

from .graph import AgentError
from ..control.event_store import StreamId


_STABLE_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")


class MessageKind(StrEnum):
    TASK = "task"
    FOLLOWUP = "followup"
    RESULT = "result"
    ARTIFACT = "artifact"
    CANCEL = "cancel"


class MessageStatus(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    ACKED = "acked"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MessageRecord:
    agent_id: UUID
    message_id: UUID
    sequence: int
    from_agent_id: UUID | None
    kind: MessageKind
    body_ref: str | None
    idempotency_key: str
    status: MessageStatus
    version: int
    delivered_run_id: UUID | None = None

    def to_document(self) -> dict[str, Any]:
        return {
            "agent_id": str(self.agent_id),
            "message_id": str(self.message_id),
            "sequence": self.sequence,
            "from_agent_id": (
                None if self.from_agent_id is None else str(self.from_agent_id)
            ),
            "kind": self.kind.value,
            "body_ref": self.body_ref,
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
            "version": self.version,
            "delivered_run_id": (
                None if self.delivered_run_id is None else str(self.delivered_run_id)
            ),
        }


def rebuild_mailbox(agent_id: UUID, events: tuple) -> list[MessageRecord]:
    """Replay one mailbox stream into ordered message records."""

    messages: dict[UUID, MessageRecord] = {}
    sequence_seen = -1
    for event in events:
        payload = event.payload
        if payload.get("agent_id") != str(agent_id):
            raise AgentError("corrupt_mailbox_stream")
        if event.event_type == "message.enqueued.v1":
            message_id = _uuid(payload, "message_id")
            sequence = int(payload["sequence"])
            if sequence != sequence_seen + 1:
                raise AgentError("corrupt_mailbox_stream")
            sequence_seen = sequence
            messages[message_id] = MessageRecord(
                agent_id=agent_id,
                message_id=message_id,
                sequence=sequence,
                from_agent_id=_uuid(payload, "from_agent_id"),
                kind=_enum(payload, "kind", MessageKind),
                body_ref=_text(payload, "body_ref"),
                idempotency_key=_text(payload, "idempotency_key"),
                status=MessageStatus.QUEUED,
                version=event.stream_version,
            )
            continue
        message_id = _uuid(payload, "message_id")
        message = messages.get(message_id)
        if message is None:
            raise AgentError("corrupt_mailbox_stream")
        run_id = _uuid(payload, "run_id")
        if event.event_type == "message.delivered.v1":
            if message.status is not MessageStatus.QUEUED:
                raise AgentError("corrupt_mailbox_stream")
            message = replace(
                message,
                status=MessageStatus.DELIVERED,
                version=event.stream_version,
                delivered_run_id=run_id,
            )
        elif event.event_type == "message.acked.v1":
            if message.status is not MessageStatus.DELIVERED:
                raise AgentError("corrupt_mailbox_stream")
            message = replace(
                message,
                status=MessageStatus.ACKED,
                version=event.stream_version,
            )
        elif event.event_type == "message.cancelled.v1":
            if message.status is not MessageStatus.QUEUED:
                raise AgentError("corrupt_mailbox_stream")
            message = replace(
                message,
                status=MessageStatus.CANCELLED,
                version=event.stream_version,
            )
        else:
            raise AgentError("unknown_mailbox_event")
        messages[message_id] = message
    return [messages[key] for key in sorted(messages, key=lambda item: messages[item].sequence)]


def _uuid(payload: Mapping[str, Any], key: str) -> UUID | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentError("corrupt_mailbox_stream")
    try:
        return UUID(value)
    except ValueError:
        raise AgentError("corrupt_mailbox_stream") from None


def _text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise AgentError("corrupt_mailbox_stream")
    return value


def _enum(payload: Mapping[str, Any], key: str, enum_type) -> Any:
    value = payload.get(key)
    try:
        return enum_type(value)
    except ValueError:
        raise AgentError("corrupt_mailbox_stream") from None


class AgentMailbox:
    def __init__(self, event_store) -> None:
        if not hasattr(event_store, "read_stream"):
            raise TypeError("event_store must implement read_stream")
        self._event_store = event_store

    def load(self, agent_id: UUID) -> list[MessageRecord]:
        if not isinstance(agent_id, UUID):
            raise TypeError("agent_id must be UUID")
        events = self._read_all(StreamId("mailbox", agent_id))
        return [] if not events else rebuild_mailbox(agent_id, events)

    def queued(self, agent_id: UUID) -> list[MessageRecord]:
        return [
            message
            for message in self.load(agent_id)
            if message.status is MessageStatus.QUEUED
        ]

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
