"""Legacy-store export oracle tests (I5/P0-10).

Production mode refuses old databases; export-to-fresh reduces only terminal
metadata through the canonical sanitizer with a legacy-store-imported.v1
source digest, never copies raw events, marks active runs as
requires_manual_restart, and leaves the original DB/WAL/SHM/backups intact.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.control.schema import DatabaseSchemaError
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.runtime.store_migration import (
    LEGACY_EXPORT_DESTINATION_EXISTS,
    LEGACY_EXPORT_SOURCE_INVALID,
    LEGACY_EXPORT_VERIFICATION_FAILED,
    LegacyExportError,
    _scan_directory_bytes,
    export_legacy_store,
)
from tests.fixtures.legacy_builder import build_legacy_database
from tests.fixtures.stability_s3_faults import media_snapshot

CANARY = "sk-legacy-RAW-SECRET-3f9a"


class LegacyExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _legacy(self, **kwargs):
        return self.directory / "legacy.db"

    def test_production_mode_refuses_legacy_store_with_zero_writes(self):
        database = self.directory / "legacy.db"
        build_legacy_database(database, canary=CANARY)
        before = database.read_bytes()
        with self.assertRaises(DatabaseSchemaError) as raised:
            SqliteEventStore(database)
        self.assertEqual(raised.exception.code, "database_legacy_export_required")
        self.assertEqual(database.read_bytes(), before)
        self.assertFalse(Path(str(database) + "-wal").exists())
        self.assertFalse(Path(str(database) + "-shm").exists())

    def test_offline_export_creates_sanitized_fresh_store(self):
        source = self.directory / "legacy.db"
        build_legacy_database(
            source,
            canary=CANARY,
            terminal_summary="finished with " + CANARY,
        )
        destination = self.directory / "fresh.db"
        report = export_legacy_store(
            source, destination, canaries=[CANARY],
        )
        self.assertTrue(destination.exists())
        self.assertEqual(report["terminal_turns"], 1)
        self.assertEqual(report["requires_manual_restart"], 0)
        store = SqliteEventStore(destination)
        page = store.read_all(limit=100)
        types = [event.event_type for event in page]
        self.assertEqual(types[0], "legacy-store-imported.v1")
        self.assertEqual(types[1:], ["legacy-turn-snapshot-imported.v1"])
        imported = page[0].payload
        self.assertEqual(imported["source_digest"], report["source_digest"])
        # no raw events were copied: only the audit stream exists
        streams = {
            event.stream_id.category
            for event in page
        }
        self.assertEqual(streams, {"legacy-import"})
        snapshot = page[1].payload
        self.assertEqual(snapshot["terminal"], True)
        self.assertNotIn(CANARY, str(snapshot))
        self.assertTrue(snapshot["outcome"].startswith("finished with"))
        self.assertIn("REDACTED", snapshot["outcome"])

    def test_committed_wal_is_read_without_changing_source_db_wal_or_shm(self):
        import json

        source = self.directory / "legacy-wal.db"
        build_legacy_database(source, canary=CANARY)
        keeper = sqlite3.connect(source, isolation_level=None)
        try:
            self.assertEqual("wal", keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0])
            keeper.execute("PRAGMA wal_autocheckpoint=0")
            keeper.execute(
                "UPDATE events SET payload_json=? WHERE event_type=?",
                (json.dumps({"summary": "wal-only-result " + CANARY}), "turn.completed.v1"),
            )
            before = media_snapshot(source)
            self.assertIsNotNone(before["-wal"])
            self.assertIsNotNone(before["-shm"])
            destination = self.directory / "fresh-wal.db"
            export_legacy_store(source, destination, canaries=[CANARY])
            self.assertEqual(before, media_snapshot(source))
            events = SqliteEventStore(destination).read_all()
            self.assertTrue(events[1].payload["outcome"].startswith("wal-only-result"))
            self.assertNotIn(CANARY, events[1].payload["outcome"])
        finally:
            keeper.close()

    def test_active_run_becomes_requires_manual_restart(self):
        source = self.directory / "legacy.db"
        build_legacy_database(
            source,
            canary=CANARY,
            include_active_turn=True,
        )
        destination = self.directory / "fresh.db"
        report = export_legacy_store(source, destination, canaries=[CANARY])
        self.assertEqual(report["requires_manual_restart"], 1)
        store = SqliteEventStore(destination)
        page = store.read_all(limit=100)
        active = [event for event in page if event.event_type == "legacy-turn-snapshot-imported.v1" and not event.payload["terminal"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].payload["requires_manual_restart"], True)
        # No resumable ordinary Turn exists in the fresh store.
        turn_streams = [event for event in page if event.stream_id.category == "turn"]
        self.assertEqual(turn_streams, [])

    def test_canary_scan_covers_db_wal_shm_temp_and_backup(self):
        source = self.directory / "legacy.db"
        build_legacy_database(
            source,
            canary=CANARY,
            terminal_summary="result token=" + CANARY,
        )
        # a legacy WAL/backup carrying the raw canary stays behind
        (self.directory / "legacy.db-wal").write_text("wal " + CANARY, encoding="utf-8")
        (self.directory / "legacy.db-shm").write_text("shm " + CANARY, encoding="utf-8")
        (self.directory / "legacy.db.bak").write_text("backup " + CANARY, encoding="utf-8")
        destination_dir = self.directory / "fresh"
        destination = destination_dir / "fresh.db"
        report = export_legacy_store(
            source, destination, canaries=[CANARY],
        )
        # destination directory: DB/WAL/SHM/temp must not contain the raw value
        self.assertEqual(_scan_directory_bytes(destination_dir, [CANARY]), {})
        # the legacy media still holds the canary (restricted, not deleted)
        hits = _scan_directory_bytes(self.directory, [CANARY])
        self.assertGreaterEqual(len(hits), 3)
        self.assertTrue((self.directory / "legacy.db-wal").exists())
        self.assertTrue((self.directory / "legacy.db-shm").exists())
        self.assertTrue((self.directory / "legacy.db.bak").exists())
        self.assertTrue((self.directory / "legacy.db").exists())

    def test_export_destination_exists_fails_without_touching_source(self):
        source = self.directory / "legacy.db"
        build_legacy_database(source, canary=CANARY)
        destination = self.directory / "exists.db"
        destination.write_text("occupied", encoding="utf-8")
        before = source.read_bytes()
        with self.assertRaises(LegacyExportError) as raised:
            export_legacy_store(source, destination)
        self.assertEqual(raised.exception.code, LEGACY_EXPORT_DESTINATION_EXISTS)
        self.assertEqual(source.read_bytes(), before)

    def test_export_invalid_source_fails(self):
        source = self.directory / "current.db"
        SqliteEventStore(source)  # a current v2 store is not a legacy store
        destination = self.directory / "fresh.db"
        with self.assertRaises(LegacyExportError) as raised:
            export_legacy_store(source, destination)
        self.assertEqual(raised.exception.code, LEGACY_EXPORT_SOURCE_INVALID)
        self.assertFalse(destination.exists())

    def test_mid_export_failure_leaves_no_final_destination(self):
        from koawa_agent_v2.control.schema import register_fault_hook

        source = self.directory / "legacy.db"
        build_legacy_database(source, canary=CANARY)
        destination = self.directory / "fresh.db"
        for point in ("s3.export.mid_destination_import", "s3.export.after_verify_before_rename"):
            with self.subTest(point=point):
                destination = self.directory / ("fresh-" + point.split(".")[-1] + ".db")

                def boom():
                    raise RuntimeError("injected export failure")

                register_fault_hook(point, boom)
                try:
                    with self.assertRaises(RuntimeError):
                        export_legacy_store(source, destination, canaries=[CANARY])
                finally:
                    register_fault_hook(point, None)
                self.assertFalse(destination.exists())
                self.assertTrue(source.exists())
                leftover = [path for path in self.directory.glob(".partial-*")]
                self.assertEqual(leftover, [])

