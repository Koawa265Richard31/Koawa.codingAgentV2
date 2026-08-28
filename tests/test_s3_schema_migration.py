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
from unittest.mock import patch

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
from koawa_agent_v2.control.event_store import EventStoreError
from tests.fixtures.legacy_builder import (
    build_legacy_database,
    build_versioned_v1_database,
)

ROOT = Path(__file__).resolve().parents[1]


class WalInitializationTest(unittest.TestCase):
    def _exercise(self, directory, *, failures, code=sqlite3.SQLITE_BUSY, clock=None):
        database = Path(directory) / "wal-init.db"
        ensure_schema(database)
        target = SqliteEventStore.__new__(SqliteEventStore)
        target._database_path = str(database)
        target._busy_timeout_ms = 1000
        original_connect = sqlite3.connect
        calls, connections = [], []

        class Connection:
            def __init__(self, raw):
                self.raw = raw
                self.closed = False

            def execute(self, sql):
                calls.append(sql)
                if sql == "PRAGMA journal_mode = WAL" and len(connections) <= failures:
                    error = sqlite3.OperationalError("injected_journal_transition_error")
                    error.sqlite_errorcode = code
                    raise error
                return self.raw.execute(sql)

            def rollback(self):
                self.raw.rollback()

            def close(self):
                self.closed = True
                self.raw.close()

        def connect(*args, **kwargs):
            self.assertTrue(all(item.closed for item in connections), "retry retained old read locks")
            connection = Connection(original_connect(*args, **kwargs))
            connections.append(connection)
            return connection

        with patch("koawa_agent_v2.control.sqlite_store.sqlite3.connect", side_effect=connect):
            with patch("koawa_agent_v2.control.sqlite_store.time.monotonic", **(
                {"side_effect": clock} if clock else {"return_value": 0}
            )):
                try:
                    target._initialize()
                    outcome = None
                except EventStoreError as error:
                    outcome = error
        self.assertTrue(all(item.closed for item in connections))
        return database, calls, len(connections), outcome

    def test_busy_retry_releases_connection_and_waits_on_fresh_writer_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            database, calls, attempts, error = self._exercise(directory, failures=1)
            self.assertIsNone(error)
            self.assertEqual(2, attempts)
            self.assertEqual(1, calls.count("BEGIN IMMEDIATE"))
            with sqlite3.connect(database) as connection:
                self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            connection.close()

    def test_non_busy_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            _, calls, attempts, error = self._exercise(directory, failures=9, code=sqlite3.SQLITE_IOERR)
            self.assertIsInstance(error, EventStoreError)
            self.assertEqual(1, attempts)
            self.assertNotIn("BEGIN IMMEDIATE", calls)

    def test_persistent_busy_has_fixed_attempt_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, attempts, error = self._exercise(directory, failures=9)
            self.assertIsInstance(error, EventStoreError)
            self.assertEqual(4, attempts)

    def test_busy_deadline_is_shared_across_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, attempts, error = self._exercise(directory, failures=9, clock=[0, .01, 2])
            self.assertIsInstance(error, EventStoreError)
            self.assertEqual(1, attempts)


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
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for _ in range(2)
        ]
        try:
            results = [(process, *process.communicate(timeout=60)) for process in processes]
            for process, stdout, stderr in results:
                self.assertEqual(process.returncode, 0, stderr + stdout)
                self.assertIn("2", stdout)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)
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
