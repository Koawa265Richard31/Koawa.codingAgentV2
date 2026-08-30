"""I5 database schema manager.

Canonical version is SQLite PRAGMA user_version; a forward-only, fully
transactional migration registry turns an empty file (or a registered earlier
version) into the current schema. Classification happens before directory
creation, journal-mode changes, DDL and ordinary runtime connections: an
existing file is inspected through a read-only snapshot and every rejection
branch leaves the DB/WAL/SHM bytes untouched (no new -wal/-shm files).

Classification outcomes (section 7.2):
- missing path or confirmed-empty SQLite (user_version 0, no non-sqlite_%
  object) -> fresh, bootstrap to latest through the same registry;
- user_version 0 with a user-object set exactly matching a registered legacy
  fingerprint -> database_legacy_export_required, zero writes;
- user_version 0 with any other user objects -> database_schema_unknown;
- 1..current: schema signature and migration ledger must exactly match the
  registered state before any forward migration (current re-verified too);
- > current -> database_schema_too_new;
- signature/checksum mismatch -> database_schema_unknown.

Every migration runs inside one BEGIN IMMEDIATE transaction that re-reads
user_version, executes DDL statement by statement (never a bare executescript
with implicit commits), runs foreign_key_check plus the post-check, inserts
the ledger row and bumps user_version atomically.

Named fault points (section 7.8): s3.migration.after_ddl,
s3.migration.before_user_version, s3.migration.after_user_version_before_commit.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable
from ..telemetry.faults import FaultPoint, emit_fault, require_fault_point
from .read_snapshot import ReadSnapshotError, read_snapshot


DATABASE_LEGACY_EXPORT_REQUIRED = "database_legacy_export_required"
DATABASE_SCHEMA_TOO_NEW = "database_schema_too_new"
DATABASE_SCHEMA_UNKNOWN = "database_schema_unknown"
DATABASE_MIGRATION_FAILED = "database_migration_failed"
DATABASE_CLASSIFICATION_FAILED = "database_classification_failed"


class DatabaseSchemaError(RuntimeError):
    """Content-free schema failure; carries only a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else code)


class DatabaseState(StrEnum):
    FRESH = "fresh"
    CURRENT = "current"
    MIGRATABLE = "migratable"
    LEGACY_EXPORT_REQUIRED = "legacy_export_required"
    TOO_NEW = "too_new"
    UNKNOWN = "unknown"
    FAILED = "failed"


CURRENT_SCHEMA_VERSION = 2


_fault_hooks: dict[str, Callable[[], None]] = {}


def register_fault_hook(point: str, hook: Callable[[], None] | None) -> None:
    """Install/remove one named callable for the deterministic fault points."""
    require_fault_point(point)
    if hook is not None and not callable(hook):
        raise TypeError("fault hook must be callable")
    if hook is None:
        _fault_hooks.pop(point, None)
    else:
        _fault_hooks[point] = hook


def inject_fault(point: str) -> None:
    """Trigger a registered fault once per call; production default is no-op."""
    emit_fault(point, {})
    hook = _fault_hooks.get(point)
    if hook is not None:
        hook()


def _normalize_ddl(value: str) -> str:
    """Deterministic whitespace normalization for checksum/signature use."""
    return " ".join(value.split()).strip()


def _checksum(ddl: str) -> str:
    return hashlib.sha256(_normalize_ddl(ddl).encode("utf-8")).hexdigest()


def _statements(ddl: str) -> tuple[str, ...]:
    return tuple(
        statement.strip()
        for statement in ddl.split(";")
        if statement.strip()
    )


@dataclass(frozen=True, slots=True)
class MigrationStep:
    from_version: int
    to_version: int
    migration_id: str
    checksum: str
    ddl: str
    apply: Callable[[sqlite3.Connection], None]
    postcheck: Callable[[sqlite3.Connection], None]
_V1_DDL = """
CREATE TABLE streams (
    stream_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK (current_version >= -1)
);

CREATE TABLE events (
    global_position INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    stream_id TEXT NOT NULL REFERENCES streams(stream_id),
    stream_version INTEGER NOT NULL CHECK (stream_version >= 0),
    commit_id TEXT NOT NULL,
    commit_index INTEGER NOT NULL CHECK (commit_index >= 0),
    commit_size INTEGER NOT NULL CHECK (commit_size > 0),
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE (stream_id, stream_version)
);

CREATE INDEX ix_events_stream
    ON events(stream_id, stream_version);

CREATE TABLE idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE schema_migrations (
    migration_id TEXT PRIMARY KEY,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

_V2_DDL = """
CREATE TABLE checkpoint_cache (
    turn_id TEXT PRIMARY KEY,
    cache_version INTEGER NOT NULL CHECK (cache_version >= 1),
    checkpoint_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    turn_version INTEGER NOT NULL CHECK (turn_version >= 0),
    execution_version INTEGER NOT NULL CHECK (execution_version >= 0),
    reducer_name TEXT NOT NULL,
    reducer_version INTEGER NOT NULL CHECK (reducer_version >= 1),
    source_event_id TEXT NOT NULL,
    source_global_position INTEGER NOT NULL CHECK (source_global_position >= 1),
    projection_digest TEXT NOT NULL,
    checkpoint_json BLOB NOT NULL CHECK (length(checkpoint_json) <= 4194304),
    updated_at TEXT NOT NULL
);

CREATE TABLE run_leases (
    turn_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    version INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE recoverable_turns (
    turn_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    turn_version INTEGER NOT NULL,
    lease_expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _noop_postcheck(connection: sqlite3.Connection) -> None:
    """Default post-check: fail closed on any foreign-key violation."""
    violating = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violating:
        raise DatabaseSchemaError(
            DATABASE_MIGRATION_FAILED, "foreign_key_check reported violations"
        )


def _make_step(
    from_version: int,
    to_version: int,
    migration_id: str,
    ddl: str,
) -> MigrationStep:
    def apply(connection: sqlite3.Connection) -> None:
        for statement in _statements(ddl):
            connection.execute(statement)

    return MigrationStep(
        from_version=from_version,
        to_version=to_version,
        migration_id=migration_id,
        checksum=_checksum(ddl),
        ddl=ddl,
        apply=apply,
        postcheck=_noop_postcheck,
    )


REGISTERED_STEPS: tuple[MigrationStep, ...] = (
    _make_step(0, 1, "0001_event_store_v1", _V1_DDL),
    _make_step(1, 2, "0002_recovery_projection_v2", _V2_DDL),
)

_STEP_BY_VERSION = {step.from_version: step for step in REGISTERED_STEPS}


def _db_now(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()
    return str(row[0])

# ---------------------------------------------------------------------------
# schema signature
# ---------------------------------------------------------------------------


def _normalize_sql(value: str | None) -> str:
    return _normalize_ddl(value or "")


def schema_signature(connection) -> str:
    """Canonical user-object signature for classification and verification.

    Tables contribute columns (name/type/not-null/pk) plus normalized DDL;
    indexes/triggers/views contribute their normalized DDL.  sqlite_%
    bookkeeping objects (sqlite_sequence included) are excluded.
    """
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    parts: list[str] = []
    for object_type, name, _tbl_name, sql in rows:
        signature = f"{object_type}:{name}"
        if object_type == "table":
            columns = connection.execute(f'PRAGMA table_info("{name}")').fetchall()
            column_signature = ";".join(
                f"{col[1]}={col[2]}.{col[3]}.{col[5]}".upper()
                for col in columns
            )
            parts.append(f"{signature}[{column_signature}]ddl={_normalize_sql(sql)}")
        else:
            parts.append(f"{signature}ddl={_normalize_sql(sql)}")
    return "\n".join(parts)


def _ledger_rows(connection) -> tuple[tuple[int, int, str, str], ...]:
    present = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if present is None:
        return ()
    rows = connection.execute(
        "SELECT from_version, to_version, migration_id, checksum "
        "FROM schema_migrations ORDER BY to_version"
    ).fetchall()
    return tuple(
        (int(row[0]), int(row[1]), str(row[2]), str(row[3]))
        for row in rows
    )


def _expected_signature(version: int) -> str:
    """Build the registered schema DDL in memory and fingerprint it."""
    connection = sqlite3.connect(":memory:")
    try:
        for step in REGISTERED_STEPS:
            if step.to_version > version:
                break
            for statement in _statements(step.ddl):
                connection.execute(statement)
        return schema_signature(connection)
    finally:
        connection.close()


def _expected_ledger(version: int) -> tuple[tuple[int, int, str, str], ...]:
    expected: list[tuple[int, int, str, str]] = []
    for step in REGISTERED_STEPS:
        if step.to_version > version:
            break
        expected.append(
            (step.from_version, step.to_version, step.migration_id, step.checksum),
        )
    return tuple(expected)

# ---------------------------------------------------------------------------
# legacy fingerprint
# ---------------------------------------------------------------------------

# A database produced by the pre-I5 baseline (unversioned, user_version=0)
# carries one of the user-object fingerprints below: the plain event store,
# and the event store plus the D6 CheckpointStore tables.  The set of
# tables/columns/indexes must match exactly; anything else is UNKNOWN.
# Both are derived from the baseline DDL (v1 DDL without the migration
# ledger table, plus the historical D6 projection tables).

_LEGACY_D6_DDL = """
CREATE TABLE checkpoints(
  turn_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, execution_version INTEGER NOT NULL,
  checkpoint_json TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE run_leases(
  turn_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, owner_id TEXT NOT NULL,
  generation INTEGER NOT NULL, version INTEGER NOT NULL,
  expires_at TEXT NOT NULL);

CREATE TABLE recoverable_turns(
  turn_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, run_id TEXT NOT NULL,
  turn_version INTEGER NOT NULL, phase TEXT NOT NULL, automatic INTEGER NOT NULL,
  updated_at TEXT NOT NULL);

CREATE TABLE recovery_commands(
  command_id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
  result_version INTEGER NOT NULL, created_at TEXT NOT NULL);
"""


def _legacy_signature(include_d6: bool) -> str:
    """Build the historical unversioned baseline schema and fingerprint it."""
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _statements(_V1_DDL):
            if "schema_migrations" in statement:
                continue
            connection.execute(statement)
        if include_d6:
            for statement in _statements(_LEGACY_D6_DDL):
                connection.execute(statement)
        return schema_signature(connection)
    finally:
        connection.close()


LEGACY_V0_FINGERPRINTS: frozenset[str] = frozenset(
    {_legacy_signature(False), _legacy_signature(True)}
)

# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Classification:
    state: DatabaseState
    user_version: int


def _read_only_connection(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    """Lock-coordinated read-only connection (SQLite handles WAL read marks).

    Unlike the byte-preserving raw snapshot, this lets SQLite arbitrate its own
    locks, so classification can read committed state while another connection
    holds an open write transaction (fault-worker IN_TRANSACTION contract).
    """
    connection = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        timeout=busy_timeout_ms / 1000,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    return connection


def classify_database(
    database_path: str | Path,
    *,
    busy_timeout_ms: int = 10_000,
) -> Classification:
    """Classify an existing database file with a read-only snapshot.

    Every rejection branch is byte-preserving: the DB/WAL/SHM digests captured
    before classification must equal the digests after classification, and no
    -wal/-shm file may be created.

    The raw snapshot path reads media through OS handles; when a live writer
    holds Windows mandatory byte locks on the WAL index (-shm), raw reads are
    denied. In that case classification falls back to a lock-coordinated
    read-only SQLite connection, which still sees only committed state and
    never creates sidecars. Other failures stay fail-closed.
    """
    if busy_timeout_ms < 1:
        raise ValueError("busy_timeout_ms must be positive")
    path = Path(database_path)
    if not path.exists():
        return Classification(DatabaseState.FRESH, 0)
    if not path.is_file():
        raise DatabaseSchemaError(DATABASE_CLASSIFICATION_FAILED, "path is not a file")
    try:
        snapshot = read_snapshot(path, timeout_ms=busy_timeout_ms)
        with snapshot.connect() as connection:
            return classify_connection(connection)
    except ReadSnapshotError as error:
        if error.code != "database_snapshot_unavailable":
            raise DatabaseSchemaError(DATABASE_CLASSIFICATION_FAILED) from None
        try:
            connection = _read_only_connection(path, busy_timeout_ms)
        except sqlite3.Error:
            raise DatabaseSchemaError(DATABASE_CLASSIFICATION_FAILED) from None
        try:
            return classify_connection(connection)
        except sqlite3.DatabaseError:
            raise DatabaseSchemaError(DATABASE_CLASSIFICATION_FAILED) from None
        finally:
            connection.close()
    except sqlite3.Error:
        raise DatabaseSchemaError(DATABASE_CLASSIFICATION_FAILED) from None


def classify_connection(connection: sqlite3.Connection) -> Classification:
    """Classify the caller's already captured snapshot, without reopening media."""
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    signature = schema_signature(connection)
    ledger = _ledger_rows(connection)

    if user_version == 0 and not signature:
        return Classification(DatabaseState.FRESH, 0)
    if user_version == 0 and signature in LEGACY_V0_FINGERPRINTS:
        return Classification(DatabaseState.LEGACY_EXPORT_REQUIRED, 0)
    if user_version == 0:
        return Classification(DatabaseState.UNKNOWN, 0)
    if user_version > CURRENT_SCHEMA_VERSION:
        return Classification(DatabaseState.TOO_NEW, user_version)
    if (
        signature == _expected_signature(user_version)
        and ledger == _expected_ledger(user_version)
    ):
        state = (
            DatabaseState.CURRENT
            if user_version == CURRENT_SCHEMA_VERSION
            else DatabaseState.MIGRATABLE
        )
        return Classification(state, user_version)
    return Classification(DatabaseState.UNKNOWN, user_version)

# ---------------------------------------------------------------------------
# migration execution
# ---------------------------------------------------------------------------

_schema_lock = threading.Lock()


def _apply_step(connection, step: MigrationStep) -> None:
    """Apply one migration step inside the caller's BEGIN IMMEDIATE."""
    step.apply(connection)
    inject_fault(FaultPoint.S3_MIGRATION_AFTER_DDL)
    step.postcheck(connection)
    connection.execute(
        "INSERT INTO schema_migrations "
        "(migration_id, from_version, to_version, checksum, applied_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            step.migration_id,
            step.from_version,
            step.to_version,
            step.checksum,
            _db_now(connection),
        ),
    )
    inject_fault(FaultPoint.S3_MIGRATION_BEFORE_USER_VERSION)
    connection.execute(f"PRAGMA user_version = {step.to_version}")
    inject_fault(FaultPoint.S3_MIGRATION_AFTER_USER_VERSION_BEFORE_COMMIT)


def _migrate_connection(connection, start_version: int) -> int:
    """Run registered steps from start_version inside one BEGIN IMMEDIATE."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("PRAGMA user_version").fetchone()
        current = int(row[0])
        if current == CURRENT_SCHEMA_VERSION:
            connection.commit()
            return current
        if current > CURRENT_SCHEMA_VERSION:
            raise DatabaseSchemaError(
                DATABASE_SCHEMA_TOO_NEW, f"user_version {current}"
            )
        while current < CURRENT_SCHEMA_VERSION:
            step = _STEP_BY_VERSION.get(current)
            if step is None:
                raise DatabaseSchemaError(
                    DATABASE_SCHEMA_UNKNOWN, f"no migration step from {current}"
                )
            _apply_step(connection, step)
            current = step.to_version
        connection.commit()
        return current
    except DatabaseSchemaError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise DatabaseSchemaError(DATABASE_MIGRATION_FAILED, str(exc)) from exc
    except Exception:
        # Injected faults and programming errors must never leave a half-open
        # migration transaction behind.
        connection.rollback()
        raise


def ensure_schema(
    database_path: str | Path,
    *,
    busy_timeout_ms: int = 10_000,
) -> int:
    """Classify then (if needed) migrate the database to the current version.

    Fresh files are bootstrapped through the *same* registry as real
    migrations; rejection branches leave the file bytes untouched.
    """
    path = Path(database_path)
    with _schema_lock:
        classification = classify_database(path, busy_timeout_ms=busy_timeout_ms)
        if classification.state is DatabaseState.FRESH:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(path),
                timeout=busy_timeout_ms / 1000,
                isolation_level=None,
            )
            try:
                connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
                return _migrate_connection(connection, 0)
            finally:
                connection.close()
        if classification.state is DatabaseState.CURRENT:
            return classification.user_version
        if classification.state is DatabaseState.MIGRATABLE:
            connection = sqlite3.connect(
                str(path),
                timeout=busy_timeout_ms / 1000,
                isolation_level=None,
            )
            try:
                connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
                return _migrate_connection(connection, classification.user_version)
            finally:
                connection.close()
        if classification.state is DatabaseState.LEGACY_EXPORT_REQUIRED:
            raise DatabaseSchemaError(DATABASE_LEGACY_EXPORT_REQUIRED)
        if classification.state is DatabaseState.TOO_NEW:
            raise DatabaseSchemaError(
                DATABASE_SCHEMA_TOO_NEW,
                f"user_version {classification.user_version}",
            )
        if classification.state is DatabaseState.UNKNOWN:
            raise DatabaseSchemaError(DATABASE_SCHEMA_UNKNOWN)
        raise DatabaseSchemaError(DATABASE_CLASSIFICATION_FAILED)
