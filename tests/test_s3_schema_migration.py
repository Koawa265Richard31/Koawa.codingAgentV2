"""DB schema manager tests (I5/S6-A): fresh bootstrap, forward migration,
interruption rollback, unknown high versions and legacy classification.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.control.schema import (
    CURRENT_SCHEMA_VERSION,
    DATABASE_LEGACY_EXPORT_REQUIRED,
    DATABASE_SCHEMA_TOO_NEW,
    DATABASE_SCHEMA_UNKNOWN,
    DatabaseState,
    DatabaseSchemaError,
    _expected_ledger,
    _expected_signature,
    classify_database,
    ensure_schema,
    register_fault_hook,
    schema_signature,
)
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from tests.fixtures.legacy_builder import (
    build_legacy_database,
    build_versioned_v1_database,
)

ROOT = Path(__file__).resolve().parents[1]


class SchemaMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_empty_path_bootstraps_latest_through_registry(self):
        database = self.directory / "fresh.db"
        version = ensure_schema(database)
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        connection = sqlite3.connect(database)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
                )
            }
            self.assertEqual(
                tables,
                {"streams", "events", "idempotency_keys", "schema_migrations",
                 "checkpoint_cache", "run_leases", "recoverable_turns"},
            )
        finally:
            connection.close()
        self.assertEqual(classify_database(database).state, DatabaseState.CURRENT)

    def test_registered_v1_migrates_in_place_and_preserves_data(self):
        database = self.directory / "v1.db"
        build_versioned_v1_database(database)
        connection = sqlite3.connect(database)
        connection.execute("INSERT INTO streams(stream_id,category,aggregate_id,current_version) VALUES(?,?,?,?)", ("turn-u1", "turn", "u1", 0))
        connection.commit()
        connection.close()
        self.assertEqual(classify_database(database).state, DatabaseState.MIGRATABLE)
        self.assertEqual(ensure_schema(database), CURRENT_SCHEMA_VERSION)
        connection = sqlite3.connect(database)
        try:
            self.assertEqual(connection.execute("SELECT stream_id FROM streams").fetchone()[0], "turn-u1")
        finally:
            connection.close()
        self.assertEqual(classify_database(database).state, DatabaseState.CURRENT)

    def test_fresh_v2_and_migrated_v2_signatures_and_ledgers_are_equal(self):
        fresh = self.directory / "fresh.db"
        ensure_schema(fresh)
        migrated = self.directory / "migrated.db"
        build_versioned_v1_database(migrated)
        ensure_schema(migrated)
        fresh_connection = sqlite3.connect(fresh)
        migrated_connection = sqlite3.connect(migrated)
        try:
            self.assertEqual(schema_signature(fresh_connection), schema_signature(migrated_connection))
            self.assertEqual(_expected_signature(CURRENT_SCHEMA_VERSION), schema_signature(fresh_connection))
        finally:
            fresh_connection.close()
            migrated_connection.close()

    def test_repeated_open_is_idempotent(self):
        database = self.directory / "repeat.db"
        for _ in range(3):
            SqliteEventStore(database)
        connection = sqlite3.connect(database)
        try:
            rows = connection.execute("SELECT migration_id FROM schema_migrations ORDER BY to_version").fetchall()
            self.assertEqual(rows, [("0001_event_store_v1",), ("0002_recovery_projection_v2",)])
        finally:
            connection.close()

    def test_interrupted_migration_rolls_back_each_fault(self):
        points = (
            "s3.migration.after_ddl",
            "s3.migration.before_user_version",
            "s3.migration.after_user_version_before_commit",
        )
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "v1.db"
                build_versioned_v1_database(database)

                def boom():
                    raise RuntimeError("injected migration failure")

                register_fault_hook(point, boom)
                try:
                    with self.assertRaises(RuntimeError):
                        ensure_schema(database)
                finally:
                    register_fault_hook(point, None)
                connection = sqlite3.connect(database)
                try:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)
                finally:
                    connection.close()
                self.assertEqual(ensure_schema(database), CURRENT_SCHEMA_VERSION)

    def test_unknown_higher_version_fails_closed_with_zero_writes(self):
        database = self.directory / "toonew.db"
        connection = sqlite3.connect(database, isolation_level=None)
        connection.execute("PRAGMA user_version = 99")
        connection.execute("CREATE TABLE stranger(value TEXT)")
        connection.commit()
        connection.close()
        before = _digest(database)
        with self.assertRaises(DatabaseSchemaError) as raised:
            SqliteEventStore(database)
        self.assertEqual(raised.exception.code, DATABASE_SCHEMA_TOO_NEW)
        self.assertEqual(before, _digest(database))
        self.assertFalse(Path(str(database) + "-wal").exists())
        self.assertFalse(Path(str(database) + "-shm").exists())

    def test_unknown_v0_shape_fails_closed_with_zero_writes(self):
        database = self.directory / "unknown.db"
        connection = sqlite3.connect(database, isolation_level=None)
        connection.execute("CREATE TABLE weird_table(value TEXT)")
        connection.commit()
        connection.close()
        before = _digest(database)
        with self.assertRaises(DatabaseSchemaError) as raised:
            SqliteEventStore(database)
        self.assertEqual(raised.exception.code, DATABASE_SCHEMA_UNKNOWN)
        self.assertEqual(before, _digest(database))

    def test_legacy_populated_db_is_classified_export_only_zero_writes(self):
        database = self.directory / "legacy.db"
        build_legacy_database(database)
        before = _digest(database)
        with self.assertRaises(DatabaseSchemaError) as raised:
            SqliteEventStore(database)
        self.assertEqual(raised.exception.code, DATABASE_LEGACY_EXPORT_REQUIRED)
        self.assertEqual(classify_database(database).state, DatabaseState.LEGACY_EXPORT_REQUIRED)
        self.assertEqual(before, _digest(database))
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ["legacy.db"])

    def test_partial_legacy_shape_is_unknown(self):
        database = self.directory / "half.db"
        build_legacy_database(database, with_d6_tables=False)
        connection = sqlite3.connect(database, isolation_level=None)
        connection.execute("DROP TABLE idempotency_keys")
        connection.commit()
        connection.close()
        self.assertEqual(classify_database(database).state, DatabaseState.UNKNOWN)

    def test_non_sqlite_file_is_classification_failed(self):
        database = self.directory / "text.db"
        database.write_text("this is not a sqlite database", encoding="utf-8")
        with self.assertRaises(DatabaseSchemaError) as raised:
            classify_database(database)
        self.assertEqual(raised.exception.code, "database_classification_failed")

    def test_two_process_migration_only_one_applies(self):
        database = self.directory / "shared.db"
        build_versioned_v1_database(database)
        body = (
            "import sys\nsys.path.insert(0, " + repr(str(ROOT / "src")) + ")\n"
            "from koawa_agent_v2.control.sqlite_store import SqliteEventStore\n"
            "SqliteEventStore(" + repr(str(database)) + ")\n"
            "from koawa_agent_v2.control.schema import ensure_schema\n"
            "print(ensure_schema(" + repr(str(database)) + "))\n",
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-B", "-c", "".join(body)],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stderr + stdout)
            self.assertIn("2", stdout)
        connection = sqlite3.connect(database)
        try:
            rows = connection.execute("SELECT migration_id FROM schema_migrations ORDER BY to_version").fetchall()
            self.assertEqual(rows, [("0001_event_store_v1",), ("0002_recovery_projection_v2",)])
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        finally:
            connection.close()


def _digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
