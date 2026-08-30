"""Offline legacy-store export to a fresh sanitized store (section 7.7).

An old, unversioned store may hold raw durable text. Production mode refuses
to open it (database_legacy_export_required); this module provides the
offline export-legacy-store path that reduces only permitted terminal
metadata through the current canonical sanitizer into a brand-new event
store, never copies raw events/receipts/checkpoints/leases/trace, and
records legacy-store-imported.v1 / legacy-turn-snapshot-imported.v1 audit
events. The original DB/WAL/SHM/backups are never modified or deleted; they
remain restricted legacy media."""

from __future__ import annotations
from koawa_agent_v2.telemetry.faults import FaultPoint

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from ..control.durable_json import (
    TERMINAL_TEXT_MAX_UTF8_BYTES,
    canonical_json_bytes_v1,
    canonicalize_text,
)
from ..control.event_store import (
    EventMetadata,
    NewEvent,
    StreamId,
    StreamWrite,
)
from ..control.models import TERMINAL_TURN_STATUSES, TurnStatus, rebuild_turn
from ..control.schema import (
    DatabaseState,
    classify_connection,
    ensure_schema,
    inject_fault,
)
from ..control.sqlite_store import SqliteEventStore
from ..control.read_snapshot import ReadSnapshotError, read_snapshot
from ..recovery.redaction import redact_text

LEGACY_EXPORT_DESTINATION_EXISTS = 'legacy_export_destination_exists'
LEGACY_EXPORT_SOURCE_INVALID = 'legacy_export_source_invalid'
LEGACY_EXPORT_VERIFICATION_FAILED = 'legacy_export_verification_failed'
MAX_LEGACY_EVENTS = 100_000
MAX_LEGACY_PAYLOAD_BYTES = 64 * 1024 * 1024


class LegacyExportError(RuntimeError):
    """Content-free export failure carrying a stable code."""

    def __init__(self, code: str, detail: str = '') -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else code)


def _bounded_canonical_text(value: str, max_bytes: int) -> str:
    """Canonical sanitizer for summaries: redact then bound by UTF-8 bytes."""
    redacted = redact_text(value)
    encoded = redacted.encode('utf-8')
    while len(encoded) > max_bytes and redacted:
        redacted = redacted[: max(1, len(redacted) - 1)]
        encoded = redacted.encode('utf-8')
    return canonicalize_text(redacted, max_bytes, name='summary').value


class _RowEvent:
    """Minimal StoredEventLike view over legacy event rows."""

    __slots__ = ('event_type', 'schema_version', 'stream_version', 'occurred_at', 'payload')

    def __init__(self, event_type, schema_version, stream_version, occurred_at, payload) -> None:
        self.event_type = event_type
        self.schema_version = schema_version
        self.stream_version = stream_version
        self.occurred_at = occurred_at
        self.payload = payload


# ---------------------------------------------------------------------------
# source reading (read-only, never writes)
# ---------------------------------------------------------------------------


def _read_legacy_streams(source: Path, busy_timeout_ms: int) -> tuple[dict, str]:
    try:
        snapshot = read_snapshot(source, timeout_ms=busy_timeout_ms)
        with snapshot.connect() as connection:
            if classify_connection(connection).state is not DatabaseState.LEGACY_EXPORT_REQUIRED:
                raise LegacyExportError(LEGACY_EXPORT_SOURCE_INVALID)
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise LegacyExportError(LEGACY_EXPORT_SOURCE_INVALID)
            return _legacy_rows(connection), snapshot.source_digest
    except (ReadSnapshotError, sqlite3.Error, ValueError):
        raise LegacyExportError(LEGACY_EXPORT_SOURCE_INVALID) from None


def _legacy_rows(connection) -> dict:
    try:
        rows = connection.execute(
            'SELECT s.stream_id, s.category, s.aggregate_id, e.stream_version, '
            'e.event_type, e.schema_version, e.occurred_at, e.payload_json '
            'FROM streams s JOIN events e ON e.stream_id = s.stream_id '
            "WHERE s.category IN ('thread','turn') ORDER BY s.stream_id, e.stream_version",
        )
        streams = {}
        event_count = 0
        payload_bytes = 0
        for row in rows:
            event_count += 1
            raw_payload = row['payload_json']
            if not isinstance(raw_payload, str):
                raise LegacyExportError(LEGACY_EXPORT_SOURCE_INVALID)
            payload_bytes += len(raw_payload.encode('utf-8', 'strict'))
            if event_count > MAX_LEGACY_EVENTS or payload_bytes > MAX_LEGACY_PAYLOAD_BYTES:
                raise LegacyExportError(LEGACY_EXPORT_SOURCE_INVALID)
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LegacyExportError(LEGACY_EXPORT_SOURCE_INVALID) from exc
            occurred_at = datetime.fromisoformat(row['occurred_at'])
            event = _RowEvent(
                row['event_type'],
                int(row['schema_version']),
                int(row['stream_version']),
                occurred_at,
                payload,
            )
            streams.setdefault(str(row['stream_id']), []).append(event)
        return {key: tuple(value) for key, value in streams.items()}
    except LegacyExportError:
        raise
    except (sqlite3.Error, ValueError) as exc:
        raise LegacyExportError(LEGACY_EXPORT_SOURCE_INVALID) from exc


def _turn_summary(turn) -> dict:
    summary = {'turn_id': str(turn.turn_id), 'status': turn.status.value}
    if turn.status in TERMINAL_TURN_STATUSES:
        summary['terminal'] = True
        if turn.outcome is not None:
            summary['outcome'] = _bounded_canonical_text(
                turn.outcome, TERMINAL_TEXT_MAX_UTF8_BYTES
            )
        if turn.error is not None:
            summary['error'] = _bounded_canonical_text(
                turn.error, TERMINAL_TEXT_MAX_UTF8_BYTES
            )
        summary['created_at'] = turn.created_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        summary['updated_at'] = turn.updated_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        summary['requires_manual_restart'] = False
    else:
        # Active/nonterminal executions are never resumed with original
        # context; they are marked for a manual restart decision.
        summary['terminal'] = False
        summary['requires_manual_restart'] = True
    return summary


def _scan_directory_bytes(directory: Path, canaries: Sequence[str]) -> dict:
    hits = {}
    needle_set = {item.encode('utf-8', 'ignore') for item in canaries if item}
    if not needle_set:
        return hits
    for path in sorted(directory.rglob('*')):
        if not path.is_file():
            continue
        data = path.read_bytes()
        for needle in needle_set:
            if needle and needle in data:
                hits[path.name] = hits.get(path.name, 0) + 1
    return hits


def _scan_destination_files(partial: Path, canaries: Sequence[str]) -> dict:
    """Scan only the fresh destination DB and its own WAL/SHM/temp files.

    The source directory must never be scanned: the legacy medium legitimately
    still holds the raw canaries.
    """
    candidates = [partial]
    for sidecar in _sidecars(partial):
        if sidecar.exists():
            candidates.append(sidecar)
    hits = {}
    for path in candidates:
        if not path.exists():
            continue
        data = path.read_bytes()
        for item in canaries:
            if not item:
                continue
            needle = item.encode('utf-8', 'ignore')
            if needle and needle in data:
                hits[path.name] = hits.get(path.name, 0) + 1
    return hits


# ---------------------------------------------------------------------------
# export core
# ---------------------------------------------------------------------------


def _fresh_partial(destination: Path) -> Path:
    return destination.parent / ('.partial-' + str(uuid5(NAMESPACE_URL, destination.name)) + '.db')


def export_legacy_store(
    source: str | Path,
    destination: str | Path,
    *,
    busy_timeout_ms: int = 10_000,
    canaries: Sequence[str] = (),
) -> dict:
    """Export one legacy store to a fresh sanitized destination.

    The final destination never exists until every verification passes: the
    fresh DB is built at a .partial path in the same directory, the audit
    stream is appended through the public EventStore API, integrity/replay
    and canary scans succeed, and only then os.replace() publishes it.  Any
    failure removes the partial and leaves the destination absent.
    """
    if busy_timeout_ms < 1:
        raise ValueError('busy_timeout_ms must be positive')
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if source_path == destination_path:
        raise LegacyExportError(LEGACY_EXPORT_SOURCE_INVALID)
    if destination_path.exists():
        raise LegacyExportError(LEGACY_EXPORT_DESTINATION_EXISTS)
    streams, source_digest = _read_legacy_streams(source_path, busy_timeout_ms)
    inject_fault(FaultPoint.S3_EXPORT_AFTER_SOURCE_SCAN)
    partial = _fresh_partial(destination_path)
    try:
        ensure_schema(partial, busy_timeout_ms=busy_timeout_ms)
        store = SqliteEventStore(partial, busy_timeout_ms=busy_timeout_ms)
        terminal_summaries: list[dict] = []
        manual_restart: list[str] = []
        for stream_id in sorted(streams):
            events = streams[stream_id]
            if not events or events[0].event_type != 'turn.created.v1':
                continue
            try:
                turn = rebuild_turn(UUID(events[0].payload['turn_id']), events)
            except Exception as exc:
                raise LegacyExportError(LEGACY_EXPORT_SOURCE_INVALID) from exc
            summary = _turn_summary(turn)
            terminal_summaries.append(summary)
            if summary['requires_manual_restart']:
                manual_restart.append(str(turn.turn_id))
        _write_legacy_import_audit(
            store,
            source_digest=source_digest,
            terminal_summaries=terminal_summaries,
            manual_restart=manual_restart,
        )
        inject_fault(FaultPoint.S3_EXPORT_MID_DESTINATION_IMPORT)
        _verify_destination(store, source_digest, terminal_summaries)
        if canaries:
            hits = _scan_destination_files(partial, canaries)
            if hits:
                raise LegacyExportError(LEGACY_EXPORT_VERIFICATION_FAILED)
        inject_fault(FaultPoint.S3_EXPORT_AFTER_VERIFY_BEFORE_RENAME)
        _close_wal(store, partial)
        os.replace(partial, destination_path)
        for sidecar in _sidecars(partial):
            if sidecar.exists():
                os.replace(sidecar, destination_path.parent / (destination_path.name + sidecar.suffix))
    except BaseException:
        _remove_partial(partial)
        raise
    return {
        'source_digest': source_digest,
        'destination_name': destination_path.name,
        'terminal_turns': len(terminal_summaries),
        'requires_manual_restart': len(manual_restart),
    }


def _write_legacy_import_audit(
    store: SqliteEventStore,
    *,
    source_digest: str,
    terminal_summaries: Sequence[Mapping[str, Any]],
    manual_restart: Sequence[str],
) -> None:
    """Write the independent legacy-import audit stream via typed events."""
    audit_id = uuid5(NAMESPACE_URL, 'legacy-import:' + source_digest)
    stream = StreamId('legacy-import', audit_id)
    command_id = uuid5(NAMESPACE_URL, 'legacy-store-import:' + source_digest)
    now = datetime.now(timezone.utc)
    imported = NewEvent(
        uuid5(command_id, 'event:legacy-store-imported'),
        'legacy-store-imported.v1',
        1,
        now,
        {
            'source_digest': source_digest,
            'terminal_turns': len(terminal_summaries),
            'requires_manual_restart': list(manual_restart),
        },
        EventMetadata(command_id, command_id, actor='legacy-export'),
    )
    snapshot_events = [imported]
    for index, summary in enumerate(terminal_summaries):
        snapshot_events.append(
            NewEvent(
                uuid5(command_id, 'event:snapshot-' + str(index)),
                'legacy-turn-snapshot-imported.v1',
                1,
                now,
                dict(summary),
                EventMetadata(command_id, command_id, actor='legacy-export'),
            ),
        )
    store.append_batch(
        (StreamWrite(stream, -1, tuple(snapshot_events)),),
        idempotency_key=command_id,
        # A killed export may already have committed this sanitized audit to
        # the private partial DB. Wall-clock event time is not command identity:
        # a fresh interpreter must replay the receipt for the same source and
        # canonical summaries, rather than conflict on datetime.now().
        request_fingerprint=hashlib.sha256(canonical_json_bytes_v1({
            'source_digest': source_digest,
            'terminal_summaries': list(terminal_summaries),
            'manual_restart': list(manual_restart),
        })).hexdigest(),
    )


def _verify_destination(
    store: SqliteEventStore,
    source_digest: str,
    terminal_summaries: Sequence[Mapping[str, Any]],
) -> None:
    """Integrity/foreign-key/replay verification before the rename."""
    connection = sqlite3.connect(str(store.database_path), isolation_level=None)
    try:
        row = connection.execute('PRAGMA integrity_check').fetchone()
        if row is None or row[0] != 'ok':
            raise LegacyExportError(LEGACY_EXPORT_VERIFICATION_FAILED)
        violating = connection.execute('PRAGMA foreign_key_check').fetchall()
        if violating:
            raise LegacyExportError(LEGACY_EXPORT_VERIFICATION_FAILED)
    finally:
        connection.close()
    # Replay: the audit stream must be readable through the public API and
    # carry the exact source digest and snapshot count.
    audit_id = uuid5(NAMESPACE_URL, 'legacy-import:' + source_digest)
    stream = StreamId('legacy-import', audit_id)
    page = store.read_stream(stream, limit=1_000)
    if not page or page[0].event_type != 'legacy-store-imported.v1':
        raise LegacyExportError(LEGACY_EXPORT_VERIFICATION_FAILED)
    payload = page[0].payload
    if payload.get('source_digest') != source_digest:
        raise LegacyExportError(LEGACY_EXPORT_VERIFICATION_FAILED)
    snapshots = [dict(event.payload) for event in page[1:]
                 if event.event_type == 'legacy-turn-snapshot-imported.v1']
    if (
        len(page) != len(terminal_summaries) + 1
        or snapshots != [dict(summary) for summary in terminal_summaries]
        or payload.get('terminal_turns') != len(terminal_summaries)
        or list(payload.get('requires_manual_restart', ())) != [
            summary['turn_id'] for summary in terminal_summaries if summary['requires_manual_restart']
        ]
    ):
        raise LegacyExportError(LEGACY_EXPORT_VERIFICATION_FAILED)


def _sidecars(path: Path):
    return (
        Path(str(path) + '-wal'),
        Path(str(path) + '-shm'),
    )


def _close_wal(store: SqliteEventStore, path: Path) -> None:
    """Checkpoint the WAL so the final file is self-contained."""
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:
        connection.close()


def _remove_partial(path: Path) -> None:
    for candidate in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
        if candidate.exists():
            try:
                candidate.unlink()
            except OSError:
                pass
