"""D14 event trace with field allowlist and pre-persist redaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4, uuid5

from ..agents.graph import AgentError
from ..control.event_store import EventMetadata, NewEvent, StreamId, StreamWrite
from ..recovery.redaction import redact_json_value


ALLOWED_STREAMS = frozenset(
    {
        "thread",
        "turn",
        "run",
        "model",
        "tool",
        "ledger",
        "mcp",
        "sandbox",
        "subagent",
    }
)
ALLOWED_FIELDS = frozenset(
    {
        "kind",
        "sequence",
        "duration_ms",
        "usage_tokens",
        "result_code",
        "failure_class",
        "tool_name",
        "server_id",
        "image_digest",
        "attempt",
        "state",
    }
)


@dataclass(frozen=True, slots=True)
class TraceRecord:
    correlation_id: UUID
    stream: str
    kind: str
    sequence: int
    occurred_at: datetime
    fields: Mapping[str, Any]


class TraceStore:
    """Persist bounded trace facts; raw bodies/credentials never enter."""

    def __init__(self, event_store) -> None:
        self.event_store = event_store

    def append(
        self,
        *,
        correlation_id: UUID,
        stream: str,
        kind: str,
        fields: Mapping[str, Any],
    ) -> TraceRecord:
        if stream not in ALLOWED_STREAMS:
            raise AgentError("trace_stream_not_allowed")
        cleaned = {
            str(key): redact_json_value(value)
            for key, value in fields.items()
            if str(key) in ALLOWED_FIELDS
        }
        events = self._read_all(StreamId("trace", correlation_id))
        sequence = len(events)
        command_id = uuid4()
        event = NewEvent(
            uuid5(command_id, f"event:trace-{sequence}"),
            f"trace.{stream}.v1",
            1,
            datetime.now(timezone.utc),
            {
                "correlation_id": str(correlation_id),
                "stream": stream,
                "kind": kind,
                "sequence": sequence,
                "fields": cleaned,
            },
            EventMetadata(command_id, correlation_id, actor="trace"),
        )
        self.event_store.append_batch(
            (
                StreamWrite(
                    StreamId("trace", correlation_id),
                    -1 if not events else events[-1].stream_version,
                    (event,),
                ),
            ),
            idempotency_key=command_id,
        )
        return TraceRecord(
            correlation_id,
            stream,
            kind,
            sequence,
            event.occurred_at,
            cleaned,
        )

    def read(self, correlation_id: UUID) -> list[TraceRecord]:
        events = self._read_all(StreamId("trace", correlation_id))
        records = []
        for event in events:
            payload = event.payload
            records.append(
                TraceRecord(
                    correlation_id,
                    payload["stream"],
                    payload["kind"],
                    int(payload["sequence"]),
                    event.occurred_at,
                    payload.get("fields", {}),
                )
            )
        return records

    def _read_all(self, stream: StreamId) -> tuple:
        values = []
        cursor = -1
        while True:
            page = self.event_store.read_stream(
                stream, after_version=cursor, limit=500
            )
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version
