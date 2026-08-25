"""D11 durable mailbox: per-agent sequence, dedupe, delivery state.

I2 (section 4) extends the mailbox state machine with RESULT_RECORDED and
UNRESOLVED, adds delivery/result accounting fields, and freezes the I2
result encoding contract (limits, canonicalization, digest, ref). Every
mailbox write uses the current mailbox stream head as its expected
version (P0-01 oracle); MessageRecord.version is only a last-event-version
for compatibility and never a stream expected version.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .graph import AgentError
from ..control.event_store import StreamId


_RESULT_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}")

MESSAGE_RESULT_MAX_INPUT_UTF8_BYTES = 1_048_576
MESSAGE_RESULT_SUMMARY_MAX_UTF8_BYTES = 4_096
MESSAGE_RESULT_REF_MAX_ASCII_CHARS = 49
MESSAGE_ERROR_CODE_MAX_ASCII_CHARS = 128
TRUNCATION_MARKER = "\n[truncated]"


class MessageKind(StrEnum):
    TASK = "task"
    FOLLOWUP = "followup"
    RESULT = "result"
    ARTIFACT = "artifact"
    CANCEL = "cancel"


class MessageStatus(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    RESULT_RECORDED = "result_recorded"
    ACKED = "acked"
    UNRESOLVED = "unresolved"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MessageRecord:
    """Projection of one message inside a mailbox stream.

    The version field is the stream version of the last event that changed
    this message; it is kept for compatibility and is never used as a mailbox
    stream expected version (delivery/result/ack writes use snapshot head).
    """

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
    delivery_attempt: int = 0
    delivered_agent_attempt: int | None = None
    delivery_lease_expires_at: datetime | None = None
    result_ref: str | None = None
    result_digest: str | None = None
    result_summary: str | None = None
    result_is_error: bool = False
    result_error_code: str | None = None
    unresolved_reason: str | None = None
    cancel_requested: bool = False
    legacy_delivery: bool = False

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
            "delivery_attempt": self.delivery_attempt,
            "delivered_agent_attempt": self.delivered_agent_attempt,
            "delivery_lease_expires_at": (
                None
                if self.delivery_lease_expires_at is None
                else self.delivery_lease_expires_at.isoformat()
            ),
            "result_ref": self.result_ref,
            "result_digest": self.result_digest,
            "result_summary": self.result_summary,
            "result_is_error": self.result_is_error,
            "result_error_code": self.result_error_code,
            "unresolved_reason": self.unresolved_reason,
            "cancel_requested": self.cancel_requested,
            "legacy_delivery": self.legacy_delivery,
        }


def rebuild_mailbox(agent_id: UUID, events: tuple) -> list[MessageRecord]:
    """Replay one mailbox stream into ordered message records.

    Supports the I2 wire family (delivered.v2/acked.v2/cancelled.v2 and
    result-recorded/unresolved/requeued/cancel-requested.v1) together with
    legacy v1 events read from pre-I2 databases.
    """

    messages: dict[UUID, MessageRecord] = {}
    sequence_seen = -1
    for event in events:
        payload = event.payload
        if payload.get("agent_id") != str(agent_id):
            raise AgentError("corrupt_mailbox_stream")
        if event.event_type == "message.enqueued.v1":
            message_id = _uuid(payload, "message_id", required=True)
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
        message_id = _uuid(payload, "message_id", required=True)
        message = messages.get(message_id)
        if message is None:
            raise AgentError("corrupt_mailbox_stream")
        event_type = event.event_type
        if event_type == "message.delivered.v2":
            if message.status is not MessageStatus.QUEUED:
                raise AgentError("corrupt_mailbox_stream")
            message = replace(
                message,
                status=MessageStatus.DELIVERED,
                version=event.stream_version,
                delivered_run_id=_uuid(payload, "run_id", required=True),
                delivery_attempt=int(payload["delivery_attempt"]),
                delivered_agent_attempt=int(payload["agent_attempt"]),
                delivery_lease_expires_at=_optional_datetime(
                    payload.get("lease_expires_at")
                ),
                legacy_delivery=False,
            )
        elif event_type == "message.delivered.v1":
            if message.status is not MessageStatus.QUEUED:
                raise AgentError("corrupt_mailbox_stream")
            message = replace(
                message,
                status=MessageStatus.DELIVERED,
                version=event.stream_version,
                delivered_run_id=_uuid(payload, "run_id"),
                delivery_attempt=1,
                delivered_agent_attempt=None,
                delivery_lease_expires_at=None,
                legacy_delivery=True,
            )
        elif event_type == "message.result-recorded.v1":
            if message.status is not MessageStatus.DELIVERED:
                raise AgentError("corrupt_mailbox_stream")
            delivery_attempt = int(payload["delivery_attempt"])
            if delivery_attempt != message.delivery_attempt:
                raise AgentError("corrupt_mailbox_stream")
            message = replace(
                message,
                status=MessageStatus.RESULT_RECORDED,
                version=event.stream_version,
                result_ref=_text(payload, "result_ref"),
                result_digest=_text(payload, "result_digest"),
                result_summary=_text_allow_empty(payload, "result_summary"),
                result_is_error=bool(payload["is_error"]),
                result_error_code=payload.get("error_code"),
            )
        elif event_type == "message.acked.v2":
            if message.status is not MessageStatus.RESULT_RECORDED:
                raise AgentError("corrupt_mailbox_stream")
            delivery_attempt = int(payload["delivery_attempt"])
            if delivery_attempt != message.delivery_attempt:
                raise AgentError("corrupt_mailbox_stream")
            message = replace(
                message,
                status=MessageStatus.ACKED,
                version=event.stream_version,
                result_ref=_text(payload, "result_ref"),
                result_digest=_text(payload, "result_digest"),
            )
        elif event_type == "message.acked.v1":
            if message.status is not MessageStatus.DELIVERED:
                raise AgentError("corrupt_mailbox_stream")
            message = replace(
                message,
                status=MessageStatus.ACKED,
                version=event.stream_version,
                result_ref=None,
                result_digest=None,
            )
        elif event_type == "message.unresolved.v1":
            if message.status is not MessageStatus.DELIVERED:
                raise AgentError("corrupt_mailbox_stream")
            delivery_attempt = int(payload["delivery_attempt"])
            if delivery_attempt != message.delivery_attempt:
                raise AgentError("corrupt_mailbox_stream")
            message = replace(
                message,
                status=MessageStatus.UNRESOLVED,
                version=event.stream_version,
                unresolved_reason=_text(payload, "reason"),
            )
        elif event_type == "message.requeued.v1":
            if message.status is not MessageStatus.UNRESOLVED:
                raise AgentError("corrupt_mailbox_stream")
            message = replace(
                message,
                status=MessageStatus.QUEUED,
                version=event.stream_version,
                unresolved_reason=None,
                cancel_requested=False,
            )
        elif event_type == "message.cancel-requested.v1":
            if message.status not in (
                MessageStatus.DELIVERED,
                MessageStatus.RESULT_RECORDED,
            ):
                raise AgentError("corrupt_mailbox_stream")
            message = replace(
                message,
                version=event.stream_version,
                cancel_requested=True,
            )
        elif event_type == "message.cancelled.v2":
            previous_status = payload.get("previous_status")
            if previous_status == "queued":
                if message.status is not MessageStatus.QUEUED:
                    raise AgentError("corrupt_mailbox_stream")
                message = replace(
                    message,
                    status=MessageStatus.CANCELLED,
                    version=event.stream_version,
                    delivery_attempt=0,
                )
            elif previous_status == "unresolved":
                if message.status is not MessageStatus.UNRESOLVED:
                    raise AgentError("corrupt_mailbox_stream")
                message = replace(
                    message,
                    status=MessageStatus.CANCELLED,
                    version=event.stream_version,
                    delivery_attempt=int(payload["delivery_attempt"]),
                )
            else:
                raise AgentError("corrupt_mailbox_stream")
        elif event_type == "message.cancelled.v1":
            if message.status is not MessageStatus.QUEUED:
                raise AgentError("corrupt_mailbox_stream")
            message = replace(
                message,
                status=MessageStatus.CANCELLED,
                version=event.stream_version,
                delivery_attempt=0,
            )
        else:
            raise AgentError("unknown_mailbox_event")
        messages[message_id] = message
    return [
        messages[key]
        for key in sorted(messages, key=lambda item: messages[item].sequence)
    ]


def _uuid(
    payload: Mapping[str, Any], key: str, *, required: bool = False
) -> UUID | None:
    value = payload.get(key)
    if value is None:
        if required:
            raise AgentError("corrupt_mailbox_stream")
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


def _text_allow_empty(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise AgentError("corrupt_mailbox_stream")
    return value


def _enum(payload: Mapping[str, Any], key: str, enum_type) -> Any:
    value = payload.get(key)
    try:
        return enum_type(value)
    except ValueError:
        raise AgentError("corrupt_mailbox_stream") from None


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentError("corrupt_mailbox_stream")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AgentError("corrupt_mailbox_stream")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class MailboxSnapshot:
    """One consistent read of a mailbox stream with its real stream head.

    The head version is the only valid expected version for mailbox writes;
    per-message version values cannot be used as stream CAS tokens.
    """

    agent_id: UUID
    stream_version: int
    messages: tuple[MessageRecord, ...]

    def unfinished(self) -> tuple[MessageRecord, ...]:
        return tuple(
            message
            for message in self.messages
            if message.status
            in (
                MessageStatus.QUEUED,
                MessageStatus.DELIVERED,
                MessageStatus.UNRESOLVED,
                MessageStatus.RESULT_RECORDED,
            )
        )


class AgentMailbox:
    def __init__(self, event_store) -> None:
        if not hasattr(event_store, "read_stream"):
            raise TypeError("event_store must implement read_stream")
        self._event_store = event_store

    def snapshot(self, agent_id: UUID) -> MailboxSnapshot:
        if not isinstance(agent_id, UUID):
            raise TypeError("agent_id must be UUID")
        events = self._read_all(StreamId("mailbox", agent_id))
        messages = () if not events else tuple(rebuild_mailbox(agent_id, events))
        head = -1 if not events else events[-1].stream_version
        return MailboxSnapshot(agent_id, head, messages)

    def load(self, agent_id: UUID) -> list[MessageRecord]:
        return list(self.snapshot(agent_id).messages)

    def queued(self, agent_id: UUID) -> list[MessageRecord]:
        return [
            message
            for message in self.snapshot(agent_id).messages
            if message.status is MessageStatus.QUEUED
        ]

    def result_recorded(self, agent_id: UUID) -> list[MessageRecord]:
        return [
            message
            for message in self.snapshot(agent_id).messages
            if message.status is MessageStatus.RESULT_RECORDED
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


# ---------------------------------------------------------------------------
# I2 frozen result encoding (section 4.4).
# ---------------------------------------------------------------------------


def canonicalize_result(
    outcome: str | None,
    error_code: str | None,
) -> tuple[str, bool, str | None]:
    """Return (canonical_outcome, is_error, error_code).

    Rejects surrogate/NUL text, normalizes line endings, applies Unicode NFC
    and enforces the 1 MiB input bound (message_result_too_large).
    """

    if error_code is None:
        is_error = False
    else:
        if not isinstance(error_code, str) or not error_code:
            raise AgentError("message_result_invalid_text")
        if (
            len(error_code.encode("ascii", errors="ignore"))
            > MESSAGE_ERROR_CODE_MAX_ASCII_CHARS
        ):
            raise AgentError("message_result_invalid_text")
        if not _RESULT_ERROR_CODE.fullmatch(error_code):
            raise AgentError("message_result_invalid_text")
        is_error = True
    if outcome is None:
        canonical_outcome = ""
    else:
        if not isinstance(outcome, str):
            raise AgentError("message_result_invalid_text")
        for character in outcome:
            code_point = ord(character)
            if 0xD800 <= code_point <= 0xDFFF or character == "\x00":
                raise AgentError("message_result_invalid_text")
        normalized = outcome.replace("\r\n", "\n").replace("\r", "\n")
        canonical_outcome = unicodedata.normalize("NFC", normalized)
        if (
            len(canonical_outcome.encode("utf-8"))
            > MESSAGE_RESULT_MAX_INPUT_UTF8_BYTES
        ):
            raise AgentError("message_result_too_large")
    return canonical_outcome, is_error, error_code


def result_ref_for(
    agent_id: UUID, message_id: UUID, delivery_attempt: int,
) -> str:
    result_id = uuid5(
        NAMESPACE_URL,
        "koawa-v2:agent-result:"
        + str(agent_id)
        + ":"
        + str(message_id)
        + ":"
        + str(delivery_attempt),
    )
    return "agent-result:" + str(result_id)


def result_digest_for(
    agent_id: UUID,
    message_id: UUID,
    delivery_attempt: int,
    canonical_outcome: str,
    is_error: bool,
    error_code: str | None,
) -> str:
    document = {
        "error_code": error_code,
        "is_error": is_error,
        "outcome": canonical_outcome,
    }
    return hashlib.sha256(
        _canonical_json(document).encode("utf-8")
    ).hexdigest()


def summarize_outcome(canonical_outcome: str) -> str:
    """Truncate to a bounded, code-point-safe summary (marker included)."""

    utf8 = canonical_outcome.encode("utf-8")
    if len(utf8) <= MESSAGE_RESULT_SUMMARY_MAX_UTF8_BYTES:
        return canonical_outcome
    marker_utf8 = TRUNCATION_MARKER.encode("utf-8")
    budget = MESSAGE_RESULT_SUMMARY_MAX_UTF8_BYTES - len(marker_utf8)
    prefix = utf8[:budget].decode("utf-8", errors="ignore")
    return prefix + TRUNCATION_MARKER


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = [
    "AgentMailbox",
    "MailboxSnapshot",
    "MessageKind",
    "MessageRecord",
    "MessageStatus",
    "MESSAGE_RESULT_MAX_INPUT_UTF8_BYTES",
    "MESSAGE_RESULT_SUMMARY_MAX_UTF8_BYTES",
    "TRUNCATION_MARKER",
    "canonicalize_result",
    "rebuild_mailbox",
    "result_digest_for",
    "result_ref_for",
    "summarize_outcome",
]
