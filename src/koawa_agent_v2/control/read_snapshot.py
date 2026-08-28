"""Bounded, source-byte-preserving SQLite snapshot acquisition.

Only OS read handles touch source media. SQLite sees a private in-memory
image, including the last checksum-valid committed WAL prefix. No raw copy
is written to a file, no source journal recovery/checkpoint is performed.
Format: https://www.sqlite.org/fileformat.html#walformat
Memory adapter: https://www.sqlite.org/c3ref/deserialize.html
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import struct
import time
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
from pathlib import Path


MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_IMAGE_BYTES = 256 * 1024 * 1024
MAX_SHM_BYTES = 4 * 1024 * 1024
MAX_WAL_FRAMES = 262144
CHUNK_BYTES = 1024 * 1024
SUFFIXES = ("", "-wal", "-shm", "-journal")


class ReadSnapshotError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise ReadSnapshotError("database_snapshot_timeout")


def _identity(info):
    if info is None:
        return None
    # Windows path/handle ctime can use different semantics. Compare it only
    # within the same API; these fields must agree between path and handle.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns)


def _stat(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ReadSnapshotError("database_snapshot_unsafe_file")
    return info


def _capture(path, deadline):
    paths = [Path(str(path) + suffix) for suffix in SUFFIXES]
    before = [_stat(item) for item in paths]
    if before[0] is None:
        raise ReadSnapshotError("database_snapshot_changed")
    if sum(item.st_size for item in before if item is not None) > MAX_SOURCE_BYTES:
        raise ReadSnapshotError("database_snapshot_limit")
    if before[2] is not None and before[2].st_size > MAX_SHM_BYTES:
        raise ReadSnapshotError("database_snapshot_limit")
    with ExitStack() as stack:
        handles, opened = [], []
        for candidate, info in zip(paths, before):
            _check_deadline(deadline)
            if info is None:
                handles.append(None)
                opened.append(None)
                continue
            fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            stack.callback(os.close, fd)
            actual = os.fstat(fd)
            if _identity(actual) != _identity(info):
                raise ReadSnapshotError("database_snapshot_changed")
            handles.append(fd)
            opened.append(actual)
        media = []
        for fd, info in zip(handles, before):
            if fd is None:
                media.append(None)
                continue
            data = bytearray()
            while len(data) < info.st_size:
                _check_deadline(deadline)
                chunk = os.read(fd, min(CHUNK_BYTES, info.st_size - len(data)))
                if not chunk:
                    raise ReadSnapshotError("database_snapshot_changed")
                data.extend(chunk)
            media.append(data)
        # A second complete bounded read verifies the same bytes, not merely
        # an unchanged main-file timestamp. SHM presence/content is included.
        for fd, original in zip(handles, media):
            if fd is None:
                continue
            os.lseek(fd, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            remaining = len(original)
            while remaining:
                _check_deadline(deadline)
                chunk = os.read(fd, min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ReadSnapshotError("database_snapshot_changed")
                remaining -= len(chunk)
                digest.update(chunk)
            if digest.digest() != hashlib.sha256(original).digest():
                raise ReadSnapshotError("database_snapshot_changed")
        for candidate, previous, fd, handle_before in zip(paths, before, handles, opened):
            after = _stat(candidate)
            if _identity(after) != _identity(previous):
                raise ReadSnapshotError("database_snapshot_changed")
            if fd is not None:
                handle_after = os.fstat(fd)
                if (_identity(handle_after) != _identity(previous)
                        or after.st_ctime_ns != previous.st_ctime_ns
                        or handle_after.st_ctime_ns != handle_before.st_ctime_ns):
                    raise ReadSnapshotError("database_snapshot_changed")
        return media


def _checksum(data, order, state=(0, 0)):
    a, b = state
    for x, y in struct.iter_unpack(order + "II", data):
        a = (a + x + b) & 0xFFFFFFFF
        b = (b + y + a) & 0xFFFFFFFF
    return a, b


def _materialize(main, wal, journal, deadline):
    if journal and any(journal[:8]):
        raise ReadSnapshotError("database_snapshot_journal_requires_recovery")
    if not main:
        if wal:
            raise ReadSnapshotError("database_snapshot_invalid")
        return main
    if len(main) < 100 or main[:16] != b"SQLite format 3\x00":
        raise ReadSnapshotError("database_snapshot_invalid")
    page_size = int.from_bytes(main[16:18], "big")
    page_size = 65536 if page_size == 1 else page_size
    if (page_size < 512 or page_size > 65536 or page_size & (page_size - 1)
            or len(main) % page_size or main[18] not in (1, 2) or main[19] not in (1, 2)):
        raise ReadSnapshotError("database_snapshot_invalid")
    if main[18:20] != b"\x02\x02" or not wal:
        main[18:20] = b"\x01\x01"
        return main
    if len(wal) < 32:
        raise ReadSnapshotError("database_snapshot_wal_invalid")
    magic, version, wal_page_size, _, salt1, salt2, c1, c2 = struct.unpack_from(">8I", wal)
    if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or wal_page_size != page_size:
        raise ReadSnapshotError("database_snapshot_wal_invalid")
    order = "<" if magic == 0x377F0682 else ">"
    state = _checksum(memoryview(wal)[:24], order)
    if state != (c1, c2):
        raise ReadSnapshotError("database_snapshot_wal_invalid")
    stride = 24 + page_size
    frame_count = (len(wal) - 32) // stride
    if frame_count > MAX_WAL_FRAMES:
        raise ReadSnapshotError("database_snapshot_limit")
    committed_end, final_pages = 32, len(main) // page_size
    stale_tail = False
    for offset in range(32, 32 + frame_count * stride, stride):
        _check_deadline(deadline)
        page, pages, s1, s2, c1, c2 = struct.unpack_from(">6I", wal, offset)
        if (s1, s2) != (salt1, salt2):
            stale_tail = True
            break  # Reused WAL: old-generation frames are not current commits.
        if page == 0 or page * page_size > MAX_IMAGE_BYTES or pages * page_size > MAX_IMAGE_BYTES:
            raise ReadSnapshotError("database_snapshot_limit")
        state = _checksum(memoryview(wal)[offset:offset + 8], order, state)
        state = _checksum(memoryview(wal)[offset + 24:offset + stride], order, state)
        if state != (c1, c2):
            raise ReadSnapshotError("database_snapshot_wal_invalid")
        if pages:
            committed_end, final_pages = offset + stride, pages
    if (len(wal) - 32) % stride and not stale_tail:
        raise ReadSnapshotError("database_snapshot_wal_invalid")
    size = final_pages * page_size
    if size > len(main):
        # Newly allocated pages must actually be present in the commit prefix.
        seen = {struct.unpack_from(">I", wal, off)[0] for off in range(32, committed_end, stride)}
        if any(page not in seen for page in range(len(main) // page_size + 1, final_pages + 1)):
            raise ReadSnapshotError("database_snapshot_wal_invalid")
        main.extend(b"\x00" * (size - len(main)))
    else:
        del main[size:]
    for offset in range(32, committed_end, stride):
        _check_deadline(deadline)
        page = struct.unpack_from(">I", wal, offset)[0]
        if page <= final_pages:
            main[(page - 1) * page_size:page * page_size] = wal[offset + 24:offset + stride]
    if main[:16] != b"SQLite format 3\x00" or main[16:18] != (page_size if page_size < 65536 else 1).to_bytes(2, "big"):
        raise ReadSnapshotError("database_snapshot_wal_invalid")
    main[18:20] = b"\x01\x01"  # Only private image: sqlite3_deserialize cannot open WAL mode.
    return main


@dataclass(frozen=True, slots=True)
class ReadSnapshot:
    image: bytearray = field(repr=False)
    source_digest: str

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        try:
            if not callable(getattr(connection, "deserialize", None)):
                raise ReadSnapshotError("database_snapshot_deserialize_unavailable")
            if self.image:
                connection.deserialize(self.image)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()


def read_snapshot(path: Path, *, timeout_ms: int = 10000) -> ReadSnapshot:
    if type(timeout_ms) is not int or timeout_ms < 1:
        raise ValueError("timeout_ms must be positive int")
    deadline = time.monotonic() + timeout_ms / 1000
    try:
        media = _capture(Path(path), deadline)
        # Preserve the legacy source-digest wire (DB, WAL, SHM concatenation),
        # now computed from exactly the captured bytes used by the reader.
        digest = hashlib.sha256()
        for data in media[:3]:
            if data is not None:
                digest.update(data)
        image = _materialize(media[0], media[1], media[3], deadline)
        return ReadSnapshot(image, digest.hexdigest())
    except ReadSnapshotError:
        raise
    except OSError:
        raise ReadSnapshotError("database_snapshot_unavailable") from None
