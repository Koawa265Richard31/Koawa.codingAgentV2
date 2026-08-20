from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.tools.workspace import (
    WorkspaceEntryKind,
    WorkspacePathError,
    WorkspacePathResolver,
)


class WorkspacePathResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repository"
        self.root.mkdir()
        (self.root / "nested").mkdir()
        (self.root / "nested" / "hello.txt").write_bytes(b"hello\n")
        self.resolver = WorkspacePathResolver(
            self.root,
            hard_max_read_bytes=1024,
            hard_max_directory_scan_entries=100,
        )

    def tearDown(self) -> None:
        self.resolver.close()
        self.temporary.cleanup()

    def assert_path_error(self, code: str, callback) -> WorkspacePathError:
        with self.assertRaises(WorkspacePathError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))
        return caught.exception

    def test_read_bytes_is_bounded_and_returns_only_relative_identity(self) -> None:
        result = self.resolver.read_bytes("nested\\hello.txt", max_bytes=16)

        self.assertEqual("nested/hello.txt", result.path)
        self.assertEqual(b"hello\n", result.data)
        self.assertEqual(6, result.byte_length)
        self.assertEqual(64, len(result.sha256))
        self.assertNotIn(str(self.root), repr(result))

    def test_lexical_gate_rejects_posix_and_windows_escape_forms(self) -> None:
        attacks = (
            "../outside.txt",
            "nested/../../outside.txt",
            "..\\outside.txt",
            "/etc/passwd",
            "\\Windows\\win.ini",
            "C:\\Windows\\win.ini",
            "C:Windows\\win.ini",
            "\\\\server\\share\\file.txt",
            "\\\\?\\C:\\Windows\\win.ini",
            "\\\\.\\PhysicalDrive0",
            "nested/hello.txt:secret",
            "nested/bad. ",
            "nested/bad.",
            "nested/bad ",
            "NUL",
            "con.txt",
            "COM1.log",
            "COM¹.log",
            "LPT².txt",
            "nested/\x00bad",
            "nested/\udcff",
        )
        for attack in attacks:
            with self.subTest(attack=repr(attack)):
                self.assert_path_error(
                    "invalid_workspace_path",
                    lambda attack=attack: self.resolver.read_bytes(
                        attack, max_bytes=16
                    ),
                )

    def test_missing_type_and_size_fail_with_stable_codes(self) -> None:
        self.assert_path_error(
            "workspace_path_not_found",
            lambda: self.resolver.read_bytes("missing.txt", max_bytes=16),
        )
        self.assert_path_error(
            "workspace_not_regular_file",
            lambda: self.resolver.read_bytes("nested", max_bytes=16),
        )
        (self.root / "large.txt").write_bytes(b"12345")
        self.assert_path_error(
            "workspace_file_too_large",
            lambda: self.resolver.read_bytes("large.txt", max_bytes=4),
        )

    def test_limits_reject_bool_zero_and_values_over_hard_ceiling(self) -> None:
        for invalid in (True, 0, -1, 1025):
            with self.subTest(invalid=invalid):
                self.assert_path_error(
                    "invalid_workspace_limit",
                    lambda invalid=invalid: self.resolver.read_bytes(
                        "nested/hello.txt", max_bytes=invalid
                    ),
                )
        self.assert_path_error(
            "invalid_workspace_limit",
            lambda: self.resolver.list_directory(
                ".", max_entries=3, max_scan_entries=2
            ),
        )
        with self.assertRaises(WorkspacePathError) as read_ceiling:
            WorkspacePathResolver(
                self.root,
                hard_max_read_bytes=64 * 1024 * 1024 + 1,
            )
        self.assertEqual("invalid_workspace_limit", read_ceiling.exception.code)
        with self.assertRaises(WorkspacePathError) as scan_ceiling:
            WorkspacePathResolver(
                self.root,
                hard_max_directory_scan_entries=1_000_001,
            )
        self.assertEqual("invalid_workspace_limit", scan_ceiling.exception.code)

    def test_directory_listing_is_sorted_truncated_and_forward_slashed(self) -> None:
        for name in ("zeta.txt", "Alpha.txt", "middle.txt"):
            (self.root / name).write_text(name, encoding="utf-8")

        listing = self.resolver.list_directory(
            ".", max_entries=3, max_scan_entries=10
        )

        self.assertEqual(".", listing.path)
        self.assertEqual(4, listing.total_entries)
        self.assertTrue(listing.truncated)
        self.assertEqual(
            ["Alpha.txt", "middle.txt", "nested"],
            [entry.path for entry in listing.entries],
        )
        self.assertEqual(
            [WorkspaceEntryKind.FILE, WorkspaceEntryKind.FILE, WorkspaceEntryKind.DIRECTORY],
            [entry.kind for entry in listing.entries],
        )
        self.assertIsNone(listing.entries[-1].size)

    def test_directory_scan_limit_fails_without_random_partial_result(self) -> None:
        for index in range(3):
            (self.root / f"item-{index}.txt").write_text("x", encoding="utf-8")
        self.assert_path_error(
            "workspace_directory_scan_limit_exceeded",
            lambda: self.resolver.list_directory(
                ".", max_entries=2, max_scan_entries=2
            ),
        )

    def test_listing_a_file_is_rejected(self) -> None:
        self.assert_path_error(
            "workspace_not_directory",
            lambda: self.resolver.list_directory(
                "nested/hello.txt", max_entries=2, max_scan_entries=2
            ),
        )

    def test_progress_guard_is_observed_and_its_exception_is_not_sanitized(self) -> None:
        class StopNow(Exception):
            pass

        def stop() -> None:
            raise StopNow()

        with self.assertRaises(StopNow):
            self.resolver.read_bytes(
                "nested/hello.txt", max_bytes=16, progress_guard=stop
            )

    def test_closed_resolver_fails_stably(self) -> None:
        self.resolver.close()
        self.assert_path_error(
            "workspace_resolver_closed",
            lambda: self.resolver.read_bytes("nested/hello.txt", max_bytes=16),
        )

    def test_external_file_symlink_and_directory_symlink_are_rejected(self) -> None:
        outside_file = self.base / "outside.txt"
        outside_file.write_text("secret", encoding="utf-8")
        outside_dir = self.base / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")
        file_link = self.root / "file-link"
        directory_link = self.root / "directory-link"
        try:
            os.symlink(outside_file, file_link)
            os.symlink(outside_dir, directory_link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")

        self.assert_path_error(
            "workspace_path_link_forbidden",
            lambda: self.resolver.read_bytes("file-link", max_bytes=32),
        )
        self.assert_path_error(
            "workspace_path_link_forbidden",
            lambda: self.resolver.read_bytes(
                "directory-link/secret.txt", max_bytes=32
            ),
        )
        self.assert_path_error(
            "workspace_path_link_forbidden",
            lambda: self.resolver.list_directory(
                ".", max_entries=10, max_scan_entries=10
            ),
        )

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows attack surface")
    def test_external_junction_is_rejected(self) -> None:
        outside = self.base / "junction-target"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        junction = self.root / "junction"
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest("junction creation unavailable")

        self.assertTrue(os.path.isjunction(junction))
        self.assert_path_error(
            "workspace_path_link_forbidden",
            lambda: self.resolver.read_bytes("junction/secret.txt", max_bytes=32),
        )

    @unittest.skipUnless(os.name == "nt", "junction root contract is Windows-specific")
    def test_trusted_root_junction_is_canonicalized_and_bound(self) -> None:
        target = self.base / "trusted-target"
        target.mkdir()
        (target / "visible.txt").write_text("visible", encoding="utf-8")
        alias = self.base / "trusted-root-alias"
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest("junction creation unavailable")

        with WorkspacePathResolver(alias) as resolver:
            result = resolver.read_bytes("visible.txt", max_bytes=32)
        self.assertEqual(b"visible", result.data)
        self.assertEqual("visible.txt", result.path)

    @unittest.skipIf(os.name == "nt", "FIFO is POSIX-only")
    def test_fifo_is_rejected_without_blocking(self) -> None:
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))
        self.assert_path_error(
            "workspace_not_regular_file",
            lambda: self.resolver.read_bytes("pipe", max_bytes=16),
        )

    @unittest.skipIf(os.name == "nt", "surrogateescape filenames are POSIX-only")
    def test_invalid_utf8_directory_entry_cannot_enter_a_listing(self) -> None:
        raw_path = os.fsencode(self.root) + b"/bad-\xff"
        descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(descriptor)
        self.assert_path_error(
            "invalid_workspace_path",
            lambda: self.resolver.list_directory(
                ".", max_entries=10, max_scan_entries=10
            ),
        )


if __name__ == "__main__":
    unittest.main()
