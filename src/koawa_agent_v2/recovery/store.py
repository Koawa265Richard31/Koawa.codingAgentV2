"""SQLite checkpoint, lease, and recoverable-turn projections for D6."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from uuid import uuid4, uuid5, NAMESPACE_URL
from threading import Event, Lock, Thread

from ..control.event_store import StreamId
from ..control.sqlite_store import SqliteEventStore
from .protocol import Checkpoint, CheckpointError, RunPhase, event_hash


class LeaseConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RunLease:
    turn_id: UUID
    run_id: UUID
    owner_id: str
    generation: int
    version: int
    expires_at: str


@dataclass(frozen=True, slots=True)
class RecoverableTurn:
    thread_id: UUID
    turn_id: UUID
    run_id: UUID
    turn_version: int
    phase: RunPhase
    automatic: bool


class CheckpointStore:
    def __init__(self, event_store: SqliteEventStore) -> None:
        if not isinstance(event_store, SqliteEventStore):
            raise TypeError("D6 requires SqliteEventStore")
        self.event_store = event_store
        self.path = Path(event_store.database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS checkpoints(
              turn_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, execution_version INTEGER NOT NULL,
              checkpoint_json TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS run_leases(
              turn_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, owner_id TEXT NOT NULL,
              generation INTEGER NOT NULL, version INTEGER NOT NULL,
              expires_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recoverable_turns(
              turn_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, run_id TEXT NOT NULL,
              turn_version INTEGER NOT NULL, phase TEXT NOT NULL, automatic INTEGER NOT NULL,
              updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recovery_commands(
              command_id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
              result_version INTEGER NOT NULL, created_at TEXT NOT NULL);
            """)
            # Installing D6 after a Turn was already started still discovers it.
            rows = c.execute("""
                SELECT s.aggregate_id,e.stream_version,e.payload_json,e.metadata_json
                FROM streams s JOIN events e ON e.stream_id=s.stream_id
                  AND e.stream_version=s.current_version
                WHERE s.category='turn' AND e.event_type='turn.started.v1'
            """).fetchall()
            db_now = c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
            for row in rows:
                payload, metadata = json.loads(row["payload_json"]), json.loads(row["metadata_json"])
                c.execute("INSERT OR IGNORE INTO run_leases VALUES(?,?,'__bootstrap__',0,0,?)", (row["aggregate_id"], payload["run_id"], db_now))
                c.execute("INSERT OR IGNORE INTO recoverable_turns VALUES(?,?,?,?, 'ready_for_model',1,?)", (row["aggregate_id"], metadata["thread_id"], payload["run_id"], row["stream_version"], db_now))

    def save(self, checkpoint: Checkpoint) -> None:
        # Validate the exact covered event before publishing the projection.
        events = self.event_store.read_stream(StreamId("run-execution", checkpoint.turn_id), after_version=checkpoint.execution_version - 1, limit=1)
        if len(events) != 1:
            raise CheckpointError("checkpoint covered event is missing")
        event = events[0]
        actual = event_hash(event.event_type, dict(event.payload), event.stream_version, event.commit_id)
        if event.global_position != checkpoint.covered_global_position or event.commit_id != checkpoint.covered_commit_id or actual != checkpoint.covered_event_hash:
            raise CheckpointError("checkpoint coverage mismatch")
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as c:
            c.execute("BEGIN IMMEDIATE")
            turn_stream = StreamId("turn", checkpoint.turn_id).key
            active = c.execute(
                "SELECT e.stream_version,e.event_type,e.payload_json "
                "FROM streams s JOIN events e ON e.stream_id=s.stream_id "
                "AND e.stream_version=s.current_version WHERE s.stream_id=?",
                (turn_stream,),
            ).fetchone()
            if (
                active is None
                or int(active["stream_version"]) != checkpoint.turn_version
                or active["event_type"] != "turn.started.v1"
                or json.loads(active["payload_json"]).get("run_id")
                != str(checkpoint.run_id)
            ):
                c.rollback()
                raise CheckpointError("checkpoint turn/run fence is no longer active")
            c.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET run_id=excluded.run_id, execution_version=excluded.execution_version, checkpoint_json=excluded.checkpoint_json, updated_at=excluded.updated_at WHERE excluded.execution_version >= checkpoints.execution_version", (str(checkpoint.turn_id), str(checkpoint.run_id), checkpoint.execution_version, json.dumps(checkpoint.document(), sort_keys=True, separators=(",", ":"), ensure_ascii=False), now))
            c.execute("INSERT INTO recoverable_turns VALUES(?,?,?,?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET run_id=excluded.run_id, turn_version=excluded.turn_version, phase=excluded.phase, automatic=excluded.automatic, updated_at=excluded.updated_at", (str(checkpoint.turn_id), str(checkpoint.thread_id), str(checkpoint.run_id), checkpoint.turn_version, checkpoint.phase.value, int(checkpoint.phase is not RunPhase.BLOCKED_UNCERTAIN_SIDE_EFFECT), now))
            c.commit()

    def load(self, turn_id: UUID) -> Checkpoint | None:
        with closing(self._connect()) as c:
            row = c.execute("SELECT checkpoint_json FROM checkpoints WHERE turn_id=?", (str(turn_id),)).fetchone()
        return None if row is None else Checkpoint.parse(row[0])

    def list_recoverable_turns(self) -> tuple[RecoverableTurn, ...]:
        with closing(self._connect()) as c:
            rows = c.execute("SELECT * FROM recoverable_turns ORDER BY updated_at, turn_id").fetchall()
        return tuple(RecoverableTurn(UUID(r["thread_id"]), UUID(r["turn_id"]), UUID(r["run_id"]), r["turn_version"], RunPhase(r["phase"]), bool(r["automatic"])) for r in rows)

    def get_active_lease(
        self,
        turn_id: UUID,
        run_id: UUID,
        owner_id: str,
    ) -> RunLease:
        """Read the exact lease created in the atomic durable-start commit."""

        with closing(self._connect()) as c:
            row = c.execute(
                "SELECT * FROM run_leases WHERE turn_id=? AND run_id=? AND owner_id=? "
                "AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (str(turn_id), str(run_id), owner_id),
            ).fetchone()
        if row is None:
            raise LeaseConflict("atomic run lease is missing or expired")
        return RunLease(
            turn_id,
            run_id,
            owner_id,
            int(row["generation"]),
            int(row["version"]),
            str(row["expires_at"]),
        )

    def acquire_lease(self, turn_id: UUID, run_id: UUID, owner_id: str, ttl_seconds: int, *, thread_id: UUID | None = None, turn_version: int | None = None) -> RunLease:
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds < 1 or not owner_id.strip():
            raise ValueError("invalid lease request")
        modifier = f"+{ttl_seconds} seconds"
        with closing(self._connect()) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM run_leases WHERE turn_id=?", (str(turn_id),)).fetchone()
            if row is not None and row["expires_at"] > c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]:
                raise LeaseConflict("run lease is still active")
            generation = 1 if row is None else int(row["generation"]) + 1
            expires = c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now',?)", (modifier,)).fetchone()[0]
            c.execute("INSERT INTO run_leases VALUES(?,?,?,?,0,?) ON CONFLICT(turn_id) DO UPDATE SET run_id=excluded.run_id, owner_id=excluded.owner_id, generation=excluded.generation, version=0, expires_at=excluded.expires_at", (str(turn_id), str(run_id), owner_id, generation, expires))
            if thread_id is not None and turn_version is not None:
                now = c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
                c.execute("INSERT INTO recoverable_turns VALUES(?,?,?,?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET thread_id=excluded.thread_id,run_id=excluded.run_id,turn_version=excluded.turn_version,phase=excluded.phase,automatic=1,updated_at=excluded.updated_at", (str(turn_id), str(thread_id), str(run_id), turn_version, RunPhase.READY_FOR_MODEL.value, 1, now))
            c.commit()
        return RunLease(turn_id, run_id, owner_id, generation, 0, expires)

    def heartbeat(self, lease: RunLease, ttl_seconds: int) -> RunLease:
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds < 1:
            raise ValueError("ttl_seconds must be a positive integer")
        with closing(self._connect()) as c:
            c.execute("BEGIN IMMEDIATE")
            expires = c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now',?)", (f"+{ttl_seconds} seconds",)).fetchone()[0]
            cur = c.execute("UPDATE run_leases SET version=version+1, expires_at=? WHERE turn_id=? AND run_id=? AND owner_id=? AND generation=? AND version=? AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')", (expires, str(lease.turn_id), str(lease.run_id), lease.owner_id, lease.generation, lease.version))
            if cur.rowcount != 1:
                raise LeaseConflict("lease ownership was lost")
            c.commit()
        return RunLease(lease.turn_id, lease.run_id, lease.owner_id, lease.generation, lease.version + 1, expires)

    def remove_recoverable(self, turn_id: UUID) -> None:
        with closing(self._connect()) as c:
            c.execute("DELETE FROM recoverable_turns WHERE turn_id=?", (str(turn_id),))

    def finish_run(self, turn_id: UUID, run_id: UUID) -> None:
        with closing(self._connect()) as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM run_leases WHERE turn_id=? AND run_id=?", (str(turn_id), str(run_id)))
            c.execute("DELETE FROM recoverable_turns WHERE turn_id=? AND run_id=?", (str(turn_id), str(run_id)))
            c.commit()

    def abandon_stale_run(self, item: RecoverableTurn, *, force: bool = False, command_id: UUID | None = None) -> int:
        """Atomically expire the lease, fence old run, append requeue, update index."""
        stream_key = StreamId("turn", item.turn_id).key
        resolved_command = command_id or uuid5(NAMESPACE_URL, f"koawa-d6:abandon:{item.turn_id}:{item.run_id}:{item.turn_version}:{force}")
        request_json = json.dumps({"turn_id": str(item.turn_id), "run_id": str(item.run_id), "turn_version": item.turn_version, "force": force}, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as c:
            c.execute("BEGIN IMMEDIATE")
            prior = c.execute("SELECT request_json,result_version FROM recovery_commands WHERE command_id=?", (str(resolved_command),)).fetchone()
            if prior is not None:
                if prior["request_json"] != request_json: raise LeaseConflict("recovery command id was reused")
                c.commit(); return int(prior["result_version"])
            lease = c.execute("SELECT * FROM run_leases WHERE turn_id=?", (str(item.turn_id),)).fetchone()
            if not force and lease is not None and lease["expires_at"] > c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]:
                raise LeaseConflict("run lease has not expired")
            head = c.execute("SELECT current_version FROM streams WHERE stream_id=?", (stream_key,)).fetchone()
            actual = -1 if head is None else int(head[0])
            latest = c.execute("SELECT event_type,payload_json FROM events WHERE stream_id=? AND stream_version=?", (stream_key, actual)).fetchone()
            if actual != item.turn_version or latest is None or latest["event_type"] != "turn.started.v1" or json.loads(latest["payload_json"]).get("run_id") != str(item.run_id):
                raise LeaseConflict("turn/run fence no longer matches")
            now = c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
            event_id = uuid5(resolved_command, "turn.stale-run-requeued")
            payload = json.dumps({"abandoned_run_id": str(item.run_id), "reason": "lease_expired"}, sort_keys=True, separators=(",", ":"))
            metadata = json.dumps({"command_id": str(resolved_command), "correlation_id": str(resolved_command), "causation_id": None, "thread_id": str(item.thread_id), "turn_id": str(item.turn_id), "run_id": str(item.run_id), "actor": "recovery"}, sort_keys=True, separators=(",", ":"))
            c.execute("INSERT INTO events(event_id,stream_id,stream_version,commit_id,commit_index,commit_size,event_type,schema_version,occurred_at,recorded_at,payload_json,metadata_json) VALUES(?,?,?,?,0,1,'turn.stale-run-requeued.v1',1,?,?,?,?)", (str(event_id), stream_key, actual + 1, str(resolved_command), now, now, payload, metadata))
            c.execute("UPDATE streams SET current_version=? WHERE stream_id=? AND current_version=?", (actual + 1, stream_key, actual))
            c.execute("DELETE FROM run_leases WHERE turn_id=?", (str(item.turn_id),))
            c.execute("UPDATE recoverable_turns SET turn_version=?, updated_at=? WHERE turn_id=?", (actual + 1, now, str(item.turn_id)))
            c.execute("INSERT INTO recovery_commands VALUES(?,?,?,?)", (str(resolved_command), request_json, actual + 1, now))
            c.commit()
            return actual + 1


class LeaseKeeper:
    """Renews one exact owner/generation lease while a Worker may block."""
    def __init__(self, store: CheckpointStore, lease: RunLease, ttl_seconds: int) -> None:
        self._store, self._lease, self._ttl = store, lease, ttl_seconds
        self._stop = Event(); self._lock = Lock(); self._failure: BaseException | None = None
        self._thread = Thread(target=self._run, name=f"lease-{lease.turn_id}", daemon=True)

    def start(self) -> None: self._thread.start()

    def _run(self) -> None:
        interval = max(0.2, self._ttl / 3)
        while not self._stop.wait(interval):
            try:
                renewed = self._store.heartbeat(self._lease, self._ttl)
                with self._lock: self._lease = renewed
            except BaseException as exc:
                with self._lock: self._failure = exc
                return

    def assert_owned(self) -> None:
        with self._lock: failure = self._failure
        if failure is not None: raise LeaseConflict("lease heartbeat failed") from failure

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive(): self._thread.join(timeout=max(1.0, self._ttl / 2))
