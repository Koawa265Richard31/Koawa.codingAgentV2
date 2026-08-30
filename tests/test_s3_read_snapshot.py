from __future__ import annotations

import hashlib
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from koawa_agent_v2.control.read_snapshot import (
    MAX_SOURCE_BYTES, ReadSnapshotError, _checksum, read_snapshot,
)


def media(path):
    return {
        suffix: candidate.read_bytes() if candidate.exists() else None
        for suffix in ("", "-wal", "-shm", "-journal")
        for candidate in (Path(str(path) + suffix),)
    }


class ReadSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "source.db"

    def database(self):
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("CREATE TABLE values_v1(value TEXT NOT NULL)")
        connection.execute("INSERT INTO values_v1 VALUES('base')")
        return connection

    def test_wal_snapshot_reads_last_commit_and_preserves_every_source_byte(self):
        keeper = self.database()
        try:
            self.assertEqual("wal", keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0])
            keeper.execute("PRAGMA wal_autocheckpoint=0")
            keeper.execute("UPDATE values_v1 SET value='committed'")
            before = media(self.path)
            snapshot = read_snapshot(self.path)
            self.assertEqual(before, media(self.path))
            digest = hashlib.sha256()
            for suffix in ("", "-wal", "-shm"):
                if before[suffix] is not None:
                    digest.update(before[suffix])
            self.assertEqual(digest.hexdigest(), snapshot.source_digest)
            with snapshot.connect() as connection:
                self.assertEqual("committed", connection.execute("SELECT value FROM values_v1").fetchone()[0])
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("UPDATE values_v1 SET value='changed'")
            self.assertEqual(before, media(self.path))
        finally:
            keeper.close()

    def test_valid_but_uncommitted_wal_tail_is_not_visible(self):
        keeper = self.database()
        keeper.execute("PRAGMA journal_mode=WAL")
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("UPDATE values_v1 SET value='committed'")
        keeper.execute("UPDATE values_v1 SET value='uncommitted'")
        original = media(self.path)
        keeper.close()

        # Turn the last transaction's commit frame into a checksum-valid
        # uncommitted tail, modelling a writer lost before its commit marker.
        wal = bytearray(original["-wal"])
        magic = int.from_bytes(wal[:4], "big")
        order = "<" if magic == 0x377F0682 else ">"
        page_size = int.from_bytes(wal[8:12], "big")
        stride = 24 + page_size
        offsets = list(range(32, len(wal), stride))
        state = _checksum(memoryview(wal)[:24], order)
        for offset in offsets[:-1]:
            state = _checksum(memoryview(wal)[offset:offset + 8], order, state)
            state = _checksum(memoryview(wal)[offset + 24:offset + stride], order, state)
        last = offsets[-1]
        wal[last + 4:last + 8] = b"\x00" * 4
        state = _checksum(memoryview(wal)[last:last + 8], order, state)
        state = _checksum(memoryview(wal)[last + 24:last + stride], order, state)
        struct.pack_into(">II", wal, last + 16, *state)
        for suffix, value in original.items():
            if value is not None:
                Path(str(self.path) + suffix).write_bytes(wal if suffix == "-wal" else value)

        snapshot = read_snapshot(self.path)
        with snapshot.connect() as connection:
            self.assertEqual("committed", connection.execute("SELECT value FROM values_v1").fetchone()[0])

    def test_truncated_or_corrupt_wal_fails_without_source_writes(self):
        keeper = self.database()
        keeper.execute("PRAGMA journal_mode=WAL")
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("UPDATE values_v1 SET value='committed'")
        original = media(self.path)
        keeper.close()
        # Keeper close may checkpoint/delete sidecars, so persist an exact
        # corruption fixture after capturing a live WAL.
        for label, mutate in (
            ("truncated", lambda value: value[:-1]),
            ("checksum", lambda value: value[:63] + bytes([value[63] ^ 1]) + value[64:]),
        ):
            with self.subTest(label=label):
                for suffix, value in original.items():
                    candidate = Path(str(self.path) + suffix)
                    if value is None:
                        if candidate.exists():
                            candidate.unlink()
                    else:
                        candidate.write_bytes(mutate(value) if suffix == "-wal" else value)
                before = media(self.path)
                with self.assertRaises(ReadSnapshotError) as caught:
                    read_snapshot(self.path, timeout_ms=200)
                self.assertEqual("database_snapshot_wal_invalid", caught.exception.code)
                self.assertEqual(before, media(self.path))

    def test_source_limit_is_checked_before_reading_sparse_body(self):
        with open(self.path, "wb") as handle:
            handle.truncate(MAX_SOURCE_BYTES + 1)
        with self.assertRaises(ReadSnapshotError) as caught:
            read_snapshot(self.path)
        self.assertEqual("database_snapshot_limit", caught.exception.code)

    def test_malformed_database_header_and_page_size_fail_closed(self):
        for label, contents in (
            ("header", b"not sqlite" + b"\x00" * 4096),
            (
                "page-size",
                b"SQLite format 3\x00" + b"\x00\x03" + b"\x01\x01"
                + b"\x00" * (4096 - 20),
            ),
        ):
            with self.subTest(label=label):
                self.path.write_bytes(contents)
                before = media(self.path)
                with self.assertRaises(ReadSnapshotError) as caught:
                    read_snapshot(self.path, timeout_ms=200)
                self.assertEqual("database_snapshot_invalid", caught.exception.code)
                self.assertEqual(before, media(self.path))

    def test_retry_exhaustion_returns_stable_failure(self):
        from koawa_agent_v2.control import read_snapshot as module

        with patch.object(
            module, "_capture",
            side_effect=ReadSnapshotError("database_snapshot_changed"),
        ) as capture:
            with self.assertRaises(ReadSnapshotError) as caught:
                read_snapshot(self.path, timeout_ms=1000)
        self.assertEqual("database_snapshot_changed", caught.exception.code)
        self.assertEqual(4, capture.call_count)

    def test_retry_discards_failed_capture_and_uses_new_complete_media(self):
        connection = self.database()
        connection.close()
        from koawa_agent_v2.control import read_snapshot as module
        original = module._capture
        calls = []

        def transient(path, deadline):
            calls.append(path)
            if len(calls) == 1:
                raise ReadSnapshotError("database_snapshot_changed")
            return original(path, deadline)

        with patch.object(module, "_capture", side_effect=transient):
            snapshot = read_snapshot(self.path)
        self.assertEqual(2, len(calls))
        with snapshot.connect() as reopened:
            self.assertEqual("base", reopened.execute("SELECT value FROM values_v1").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
