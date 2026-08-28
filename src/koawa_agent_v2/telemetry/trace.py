"""D14 event trace with field allowlist and pre-persist redaction."""

from __future__ import annotations
from koawa_agent_v2.telemetry.faults import FaultPoint

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from concurrent.futures import CancelledError as FutureCancelledError
from threading import Lock
from typing import Any, Mapping, Protocol, runtime_checkable
from uuid import UUID, uuid4, uuid5

from ..agents.graph import AgentError
from ..control.event_store import EventMetadata, NewEvent, StreamId, StreamWrite
from ..recovery.redaction import redact_json_value
from .faults import FaultPort, NO_OP_FAULT_PORT


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

MAX_TRACE_FIELD_BYTES = 4_096
MAX_TRACE_EVENT_BYTES = 16_384
DEFAULT_TRACE_CAS_RETRIES = 4


@dataclass(frozen=True, slots=True)
class TraceRecord:
    correlation_id: UUID
    stream: str
    kind: str
    sequence: int
    occurred_at: datetime
    fields: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TraceProbe:
    """Small, already-classified diagnostic DTO accepted by production sinks."""

    correlation_id: UUID
    stream: str
    kind: str
    fields: Mapping[str, Any]


@runtime_checkable
class TraceSink(Protocol):
    """Failure-isolated trace boundary used by production components."""

    def emit(self, probe: TraceProbe) -> None: ...


@dataclass(frozen=True, slots=True)
class TraceDiagnostics:
    dropped_since_start: int
    last_error_code: str | None
    last_failure_at: datetime | None


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
        _validate_trace_size(stream=stream, kind=kind, fields=cleaned)
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


class BestEffortTraceSink:
    """Serialize local emits and never let diagnostic storage break business work.

    ``TraceStore`` deliberately remains strict for management commands and tests.
    Production call sites use this adapter, which records bounded process-local
    diagnostics when persistence or a cross-process CAS race fails.
    """

    def __init__(
        self,
        store: TraceStore,
        *,
        cas_retries: int = DEFAULT_TRACE_CAS_RETRIES,
        fault_port: FaultPort = NO_OP_FAULT_PORT,
    ) -> None:
        if not isinstance(store, TraceStore):
            raise TypeError("store must be TraceStore")
        if not isinstance(cas_retries, int) or isinstance(cas_retries, bool) or cas_retries < 1:
            raise ValueError("cas_retries must be a positive integer")
        self._store = store
        self._cas_retries = cas_retries
        if not callable(getattr(fault_port, "hit", None)):
            raise TypeError("fault_port must implement FaultPort")
        self._fault_port = fault_port
        self._locks_guard = Lock()
        # Count both active emitters and waiters. Remove idle entries explicitly:
        # relying on GC/weak references would retain entries when an exception
        # traceback in a storage adapter still references an old emit frame.
        self._correlation_locks: dict[UUID, tuple[Lock, int]] = {}
        self._diagnostic_lock = Lock()
        self._dropped_since_start = 0
        self._last_error_code: str | None = None
        self._last_failure_at: datetime | None = None

    def emit(self, probe: TraceProbe) -> None:
        if not isinstance(probe, TraceProbe):
            raise TypeError("probe must be TraceProbe")
        with self._correlation_lock(probe.correlation_id) as lock, lock:
            last_error: Exception | None = None
            for _ in range(self._cas_retries):
                try:
                    self._store.append(
                        correlation_id=probe.correlation_id,
                        stream=probe.stream,
                        kind=probe.kind,
                        fields=probe.fields,
                    )
                    return
                except FutureCancelledError:
                    raise
                except Exception as exc:
                    # Only optimistic concurrency is retryable.  Importing here
                    # avoids coupling the DTO protocol to the concrete store.
                    from ..control.event_store import WrongExpectedVersion

                    last_error = exc
                    if isinstance(exc, WrongExpectedVersion):
                        try:
                            self._fault_port.hit(
                                FaultPoint.S5_TRACE_CAS_CONFLICT,
                                {"correlation_id": str(probe.correlation_id)},
                            )
                        except FutureCancelledError:
                            raise
                        except Exception as injected:
                            last_error = injected
                            break
                    else:
                        break
            assert last_error is not None
            try:
                self._fault_port.hit(
                    FaultPoint.S5_TRACE_DROP,
                    {"correlation_id": str(probe.correlation_id)},
                )
            except FutureCancelledError:
                raise
            except Exception as injected:
                last_error = injected
            self._record_drop(_stable_trace_error(last_error))

    def diagnostics(self) -> TraceDiagnostics:
        with self._diagnostic_lock:
            return TraceDiagnostics(
                self._dropped_since_start,
                self._last_error_code,
                self._last_failure_at,
            )

    @contextmanager
    def _correlation_lock(self, correlation_id: UUID):
        with self._locks_guard:
            lock, borrowers = self._correlation_locks.get(correlation_id, (Lock(), 0))
            self._correlation_locks[correlation_id] = (lock, borrowers + 1)
        try:
            yield lock
        finally:
            with self._locks_guard:
                current, borrowers = self._correlation_locks[correlation_id]
                if borrowers == 1:
                    del self._correlation_locks[correlation_id]
                else:
                    self._correlation_locks[correlation_id] = (current, borrowers - 1)

    def _record_drop(self, code: str) -> None:
        with self._diagnostic_lock:
            self._dropped_since_start += 1
            self._last_error_code = code
            self._last_failure_at = datetime.now(timezone.utc)


def _stable_trace_error(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code[:128]
    return "trace_storage_failed"


def _validate_trace_size(*, stream: str, kind: str, fields: Mapping[str, Any]) -> None:
    import json

    for key, value in fields.items():
        encoded = json.dumps(
            {key: value}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8", "strict")
        if len(encoded) > MAX_TRACE_FIELD_BYTES:
            raise AgentError("trace_field_too_large")
    encoded_event = json.dumps(
        {"stream": stream, "kind": kind, "fields": dict(fields)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "strict")
    if len(encoded_event) > MAX_TRACE_EVENT_BYTES:
        raise AgentError("trace_event_too_large")
