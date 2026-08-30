"""S3 production-hook scenarios for the shared OS-kill worker.

Setup is unarmed; markers inspect committed state using fresh connections.
Only the parent kills processes. Recovery never treats a cache as truth.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event
from uuid import UUID

from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.schema import (
    CURRENT_SCHEMA_VERSION, DatabaseState, _expected_ledger,
    _expected_signature, _ledger_rows, classify_database, ensure_schema,
    schema_signature,
)
from koawa_agent_v2.recovery import CheckpointStore, RecoveryCoordinator
from koawa_agent_v2.recovery.context import projection_digest, reduce_execution
from koawa_agent_v2.recovery.store import RecoverableTurn
from koawa_agent_v2.runtime.store_migration import (
    _fresh_partial, _scan_directory_bytes, export_legacy_store,
)
from koawa_agent_v2.telemetry.faults import FAULT_SPECS, NoOpFaultPort, using_fault_port
from scripts.stability_benchmark import atomic_json
from scripts.stability_scenarios import NOW, event_digest, read_events, seed_execution, store_at
from tests.fixtures.legacy_builder import build_legacy_database, build_versioned_v1_database


MIGRATION_POINTS = (
    "s3.migration.after_ddl", "s3.migration.before_user_version",
    "s3.migration.after_user_version_before_commit",
)
EXPORT_POINTS = (
    "s3.export.after_source_scan", "s3.export.mid_destination_import",
    "s3.export.after_verify_before_rename",
)
CHECKPOINT_POINTS = (
    "s3.checkpoint.after_source_read", "s3.checkpoint.before_cache_commit",
    "s3.checkpoint.after_cache_commit", "s3.checkpoint.after_verify_before_tail",
)
S3_POINTS = MIGRATION_POINTS + EXPORT_POINTS + CHECKPOINT_POINTS
CANARY = "sk-legacy-RAW-SECRET-3f9a"


def media_snapshot(path: Path) -> dict:
    return {
        suffix: hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.exists() else None
        for suffix in ("", "-wal", "-shm", ".bak")
        for candidate in (Path(str(path) + suffix),)
    }


def schema_snapshot(path: Path) -> dict:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        signature = schema_signature(connection)
        ledger = _ledger_rows(connection)
        assert signature == _expected_signature(version), "partial_schema"
        assert ledger == _expected_ledger(version), "partial_migration_ledger"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        return {"version": version, "signature": signature, "ledger": ledger,
                "streams": connection.execute("SELECT * FROM streams ORDER BY stream_id").fetchall()}


def _cache_rows(path: Path) -> list:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute(
            "SELECT turn_id, cache_version, execution_version, projection_digest "
            "FROM checkpoint_cache ORDER BY turn_id"
        ).fetchall()


def _publish(store, seed):
    events = read_events(store, StreamId("run-execution", UUID(seed["turn_id"])))
    return CheckpointStore(store).publish_from_source(
        thread_id=UUID(seed["thread_id"]), turn_id=UUID(seed["turn_id"]),
        run_id=UUID(seed["run_id"]), turn_version=seed["turn_version"],
        source_event=events[-1], projection=reduce_execution(events),
    )


def _reconstruct(store, seed):
    item = RecoverableTurn(UUID(seed["turn_id"]), seed["turn_version"], UUID(seed["run_id"]), NOW)
    return RecoveryCoordinator(ThreadRuntime(store), CheckpointStore(store), owner_id="kill-recovery").reconstruct(item)


class S3KillPort(NoOpFaultPort):
    def __init__(self, root: Path, request: dict, *, truth_store=None):
        self.root, self.request, self.truth_store = root, request, truth_store

    def hit(self, point, facts):
        super().hit(point, facts)
        request, root = self.request, self.root
        if point != request["point"]:
            return
        marker = {"point": point, "point_class": FAULT_SPECS[point].point_class.value,
                  "crash_pid": os.getpid()}
        if point in MIGRATION_POINTS:
            snapshot = schema_snapshot(root / "runtime.db")
            assert snapshot["version"] == 1, "uncommitted_migration_visible"
            marker["schema"] = snapshot
        elif point in EXPORT_POINTS:
            assert media_snapshot(root / "legacy.db") == request["source_media"], "source_media_changed"
            assert not (root / "export" / "fresh.db").exists(), "premature_export_publish"
            partial = _fresh_partial(root / "export" / "fresh.db")
            marker["partial_exists"] = partial.exists()
            if point != EXPORT_POINTS[0]:
                events = store_at(partial).read_all()
                assert len(events) == 3, "incomplete_partial_audit"
                assert all(event.stream_id.category == "legacy-import" for event in events)
                marker["partial_event_digest"] = event_digest(store_at(partial))
        else:
            path = root / "runtime.db"
            assert self.truth_store is not None
            assert event_digest(self.truth_store) == request["seed"]["dataset_digest"], "checkpoint_changed_truth"
            rows = _cache_rows(path)
            committed = point in (CHECKPOINT_POINTS[2], CHECKPOINT_POINTS[3])
            assert len(rows) == int(committed), "cache_commit_marker_misplaced"
            if committed:
                assert rows[0][3] == request["seed"]["projection_digest"]
            marker["cache_rows"] = rows
        atomic_json(root / "ready.json", marker)
        Event().wait()


def crash_s3(root: Path, point: str) -> None:
    if point not in S3_POINTS:
        raise ValueError("unsupported S3 scenario")
    request = {"point": point}
    path = root / "runtime.db"
    store = None
    keeper = None
    if point in MIGRATION_POINTS:
        build_versioned_v1_database(path)
        with closing(sqlite3.connect(path)) as connection:
            # Existing data, not just an empty DDL bootstrap.
            connection.execute(
                "INSERT INTO streams VALUES(?,?,?,?)", ("fixture-preserved", "fixture", "preserved", -1)
            )
            connection.commit()
        request["baseline_schema"] = schema_snapshot(path)
        action = lambda: ensure_schema(path)
    elif point in EXPORT_POINTS:
        source = root / "legacy.db"
        if not source.exists():
            build_legacy_database(source, canary=CANARY, include_active_turn=True)
        if not Path(str(source) + "-wal").exists():
            # Standalone worker fallback. The matrix normally supplies one
            # byte-identical, genuinely generated WAL seed to every window.
            keeper = sqlite3.connect(source, isolation_level=None)
            assert keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            keeper.execute("PRAGMA wal_autocheckpoint=0")
            keeper.execute(
                "UPDATE events SET payload_json=? WHERE event_type='turn.completed.v1'",
                (json.dumps({"summary": "wal-export-result " + CANARY}),),
            )
        request["source_media"] = media_snapshot(source)
        (root / "export").mkdir(exist_ok=True)
        action = lambda: export_legacy_store(source, root / "export" / "fresh.db", canaries=[CANARY])
    else:
        seed = seed_execution(path, 31)
        request["seed"] = seed
        if point != CHECKPOINT_POINTS[3]:
            # Explicit cache-loss setup; event log and leases remain unchanged.
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("DELETE FROM checkpoint_cache")
                connection.commit()
        store = store_at(path)
        action = lambda: _reconstruct(store, seed) if point == CHECKPOINT_POINTS[3] else _publish(store, seed)
    atomic_json(root / "request.json", request)
    try:
        with using_fault_port(S3KillPort(root, request, truth_store=store)):
            action()
    finally:
        if keeper is not None:
            keeper.close()
    raise AssertionError("production operation missed S3 kill point")


def recover_s3(root: Path) -> None:
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    marker = json.loads((root / "ready.json").read_text(encoding="utf-8"))
    point = request["point"]
    result = {"point": point, "recovery_pid": os.getpid()}
    if point in MIGRATION_POINTS:
        path = root / "runtime.db"
        before = schema_snapshot(path)
        assert before["version"] == 1, "killed_migration_not_rolled_back"
        assert json.loads(json.dumps(before)) == request["baseline_schema"]
        for _ in range(2):
            assert ensure_schema(path) == CURRENT_SCHEMA_VERSION
        after = schema_snapshot(path)
        assert after["streams"] == before["streams"], "migration_lost_existing_data"
        assert classify_database(path).state is DatabaseState.CURRENT
        result["normalized_state"] = after
    elif point in EXPORT_POINTS:
        source, destination = root / "legacy.db", root / "export" / "fresh.db"
        assert media_snapshot(source) == request["source_media"], "kill_changed_source_media"
        assert not destination.exists(), "kill_published_incomplete_export"
        partial = _fresh_partial(destination)
        if marker["partial_exists"]:
            assert partial.exists(), "OS_kill_did_not_leave_partial"
            assert event_digest(store_at(partial)) == marker["partial_event_digest"]
        report = export_legacy_store(source, destination, canaries=[CANARY])
        assert media_snapshot(source) == request["source_media"], "retry_changed_source_media"
        assert _scan_directory_bytes(destination.parent, [CANARY]) == {}
        assert not partial.exists(), "retry_left_partial_database"
        events = store_at(destination).read_all()
        assert len(events) == 3, "export_duplicate_or_missing_snapshot"
        assert all(event.stream_id.category == "legacy-import" for event in events)
        snapshots = [event.payload for event in events[1:]]
        assert sum(bool(item["requires_manual_restart"]) for item in snapshots) == 1
        assert report["requires_manual_restart"] == 1
        result["normalized_state"] = {"event_digest": event_digest(store_at(destination)),
                                      "source_media": request["source_media"]}
    else:
        path, seed = root / "runtime.db", request["seed"]
        store = store_at(path)
        assert event_digest(store) == seed["dataset_digest"]
        assert json.loads(json.dumps(_cache_rows(path))) == marker["cache_rows"]
        assert projection_digest(_reconstruct(store, seed)) == seed["projection_digest"]
        _publish(store, seed)
        assert not _publish(store, seed).changed, "cache_retry_not_idempotent"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("DELETE FROM checkpoint_cache")
            connection.commit()
        assert projection_digest(_reconstruct(store, seed)) == seed["projection_digest"]
        _publish(store, seed)
        assert projection_digest(_reconstruct(store, seed)) == seed["projection_digest"]
        assert event_digest(store) == seed["dataset_digest"], "cache_rebuild_changed_truth"
        result["normalized_state"] = {"event_digest": event_digest(store),
                                      "projection_digest": seed["projection_digest"]}
    atomic_json(root / "recovered.json", result)
