"""Recovery projection port and protocol-only CheckpointStore.

The recovery package holds no sqlite3 import and no private-table SQL: the
checkpoint cache, recoverable/lease projections and the stale-run requeue are
maintained by typed events and the control-layer projection adapter
(RecoveryProjectionPort).  The port forbids bare lease mutators; recovery
coordination only issues typed ThreadRuntime commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol, Sequence
from uuid import UUID

from ..control.event_store import StoredEvent, StreamId
from .context import (
    ExecutionProjection,
    projection_digest,
    projection_document,
    reduce_execution,
)
from .protocol import (
    REDUCER_NAME,
    REDUCER_VERSION,
    Checkpoint,
    CheckpointError,
    stored_event_hash_v2,
)


class LeaseConflict(RuntimeError):
    """Recovery fencing failure: lease missing, expired or ownership lost."""


@dataclass(frozen=True, slots=True)
class RunLease:
    turn_id: UUID
    run_id: UUID
    owner_id: str
    generation: int
    version: int
    expires_at: str


@dataclass(frozen=True, slots=True)
class CheckpointCacheRecord:
    turn_id: UUID
    cache_version: int
    checkpoint_id: UUID
    run_id: UUID
    turn_version: int
    execution_version: int
    reducer_name: str
    reducer_version: int
    source_event_id: UUID
    source_global_position: int
    projection_digest: str
    checkpoint_json: bytes
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RecoverableTurn:
    turn_id: UUID
    turn_version: int
    run_id: UUID
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class CacheReceipt:
    turn_id: UUID
    cache_version: int
    checkpoint_id: UUID
    changed: bool


class RecoveryEventSource(Protocol):
    """The minimal read surface CheckpointStore needs from an event store."""

    def read_stream(
        self,
        stream_id: StreamId,
        *,
        after_version: int = -1,
        limit: int = 500,
    ) -> tuple[StoredEvent, ...]: ...


class RecoveryProjectionPort(Protocol):
    """Control-layer projection adapter; mutators are forbidden on the port.

    The port exposes only the cache plus recoverable/lease reads; lease
    mutations travel exclusively as typed Turn events through ThreadRuntime.
    """

    def database_time(self) -> datetime: ...

    def publish_checkpoint_cache(
        self,
        record: CheckpointCacheRecord,
        *,
        expected_cache_version: int,
    ) -> CacheReceipt: ...

    def load_checkpoint_cache(self, turn_id: UUID) -> CheckpointCacheRecord | None: ...

    def list_recoverable(
        self,
        *,
        expired_before: datetime,
        after_turn_id: UUID | None,
        limit: int,
    ) -> tuple[RecoverableTurn, ...]: ...

    def get_active_lease(
        self,
        turn_id: UUID,
        run_id: UUID,
        owner_id: str,
    ) -> RunLease | None: ...



# ---------------------------------------------------------------------------
# CheckpointStore: verified checkpoint cache + recovery reads
# ---------------------------------------------------------------------------


class CheckpointStore:
    """Protocol-only recovery facade.

    The store reads events through a RecoveryEventSource and projects through
    RecoveryProjectionPort; it never opens a connection, never constructs SQL
    and never mutates leases directly.  Publishing a checkpoint replays the
    covered segment with the canonical reducer and rejects any projection that
    does not match the reducer output.
    """

    def __init__(
        self,
        event_store,
        projections=None,
    ) -> None:
        if event_store is None or not callable(getattr(event_store, "read_stream", None)):
            raise TypeError("event_store must implement read_stream")
        if projections is None:
            projections = event_store
        if not callable(getattr(projections, "publish_checkpoint_cache", None)):
            raise TypeError("projections must implement RecoveryProjectionPort")
        self.event_store = event_store
        self._projections = projections

    def database_time(self) -> datetime:
        return self._projections.database_time()

    def publish_from_source(
        self,
        *,
        thread_id: UUID,
        turn_id: UUID,
        run_id: UUID,
        turn_version: int,
        source_event: StoredEvent,
        projection: ExecutionProjection,
    ) -> CacheReceipt:
        events = self._read_covered(source_event)
        reduced = reduce_execution(events)
        if (
            projection_document(reduced) != projection_document(projection)
            or projection_digest(reduced) != projection_digest(projection)
        ):
            raise CheckpointError("checkpoint_projection_mismatch")
        document = projection_document(reduced)
        digest = projection_digest(reduced)
        checkpoint = Checkpoint.build(
            source_category="run-execution",
            source_aggregate_id=turn_id,
            covered_stream_version=source_event.stream_version,
            covered_event_id=source_event.event_id,
            covered_global_position=source_event.global_position,
            covered_commit_id=source_event.commit_id,
            covered_event_hash=stored_event_hash_v2(source_event),
            thread_id=thread_id,
            turn_id=turn_id,
            run_id=run_id,
            turn_stream_version=turn_version,
            projection=document,
            projection_digest=digest,
            created_at=source_event.recorded_at,
        )
        record = CheckpointCacheRecord(
            turn_id=turn_id,
            cache_version=source_event.stream_version + 1,
            checkpoint_id=checkpoint.checkpoint_id,
            run_id=run_id,
            turn_version=turn_version,
            execution_version=source_event.stream_version,
            reducer_name=REDUCER_NAME,
            reducer_version=REDUCER_VERSION,
            source_event_id=source_event.event_id,
            source_global_position=source_event.global_position,
            projection_digest=digest,
            checkpoint_json=checkpoint.wire_bytes(),
            updated_at=source_event.recorded_at,
        )
        return self._projections.publish_checkpoint_cache(
            record,
            expected_cache_version=record.cache_version,
        )

    def load(self, turn_id: UUID) -> Checkpoint | None:
        record = self._projections.load_checkpoint_cache(turn_id)
        if record is None:
            return None
        try:
            checkpoint = Checkpoint.parse(record.checkpoint_json)
        except CheckpointError:
            return None
        if (
            checkpoint.checkpoint_id != record.checkpoint_id
            or checkpoint.run_id != record.run_id
            or checkpoint.turn_id != turn_id
            or checkpoint.turn_stream_version != record.turn_version
        ):
            return None
        return checkpoint

    def list_recoverable(
        self,
        *,
        expired_before: datetime | None = None,
        after_turn_id: UUID | None = None,
        limit: int = 1_000,
    ) -> tuple[RecoverableTurn, ...]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("limit must be between 1 and 10000")
        return self._projections.list_recoverable(
            expired_before=(
                self._projections.database_time()
                if expired_before is None
                else expired_before
            ),
            after_turn_id=after_turn_id,
            limit=limit,
        )

    def list_recoverable_turns(self) -> tuple[RecoverableTurn, ...]:
        """Compatibility alias for pre-I5 callers."""
        return self.list_recoverable()

    def get_active_lease(
        self,
        turn_id: UUID,
        run_id: UUID,
        owner_id: str,
    ) -> RunLease:
        lease = self._projections.get_active_lease(turn_id, run_id, owner_id)
        if lease is None:
            raise LeaseConflict("atomic run lease is missing")
        now = self._projections.database_time()
        if now > _parse_lease_time(lease.expires_at):
            raise LeaseConflict("atomic run lease has expired")
        return lease

    def _read_covered(self, source_event: StoredEvent) -> tuple[StoredEvent, ...]:
        stream = source_event.stream_id
        values: list[StoredEvent] = []
        cursor = -1
        while True:
            page = self.event_store.read_stream(stream, after_version=cursor, limit=500)
            values.extend(page)
            if not page or page[-1].stream_version >= source_event.stream_version:
                if not page or page[-1].stream_version != source_event.stream_version:
                    raise CheckpointError("checkpoint_source_event_missing")
                break
            cursor = page[-1].stream_version
        return tuple(values)


def _parse_lease_time(value: str) -> datetime:
    from datetime import timezone

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# LeaseKeeper: renews an exact Turn run lease through typed heartbeats
# ---------------------------------------------------------------------------


class LeaseKeeper:
    """Renews one exact run lease while a Worker may block.

    The heartbeater is a callable that appends the typed
    turn.recovery-lease-heartbeated.v1 event (via ThreadRuntime) and returns
    the renewed expiry; failures are recorded and surfaced by assert_owned so
    the next external side effect is fenced.
    """

    def __init__(
        self,
        heartbeater: Callable[[], datetime],
        *,
        ttl_seconds: int,
    ) -> None:
        if not callable(heartbeater):
            raise TypeError("heartbeater must be callable")
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self._heartbeater = heartbeater
        self._ttl = ttl_seconds
        self._stop = False
        self._failure: BaseException | None = None
        from threading import Event, Lock, Thread

        self._stop_event = Event()
        self._lock = Lock()
        self._thread = Thread(
            target=self._run, name="lease-keeper", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        interval = max(0.2, self._ttl / 3)
        while not self._stop_event.wait(interval):
            try:
                self._heartbeater()
            except BaseException as exc:
                with self._lock:
                    self._failure = exc
                return

    def assert_owned(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise LeaseConflict("lease heartbeat failed") from failure

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self._ttl / 2))