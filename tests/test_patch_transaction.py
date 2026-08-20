from __future__ import annotations

import hashlib
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from koawa_agent_v2.editing.protocol import PatchError, PatchLimits, parse_patch_document
from koawa_agent_v2.editing.transaction import AtomicPatchWorkspace
from koawa_agent_v2.tools.workspace import WorkspacePathResolver


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _patch(changes: list[dict[str, object]]):
    return parse_patch_document(
        json.dumps(
            {"schema_version": 1, "changes": changes},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _update(path: str, before: bytes, old: str, new: str) -> dict[str, object]:
    return {
        "operation": "update",
        "path": path,
        "base_sha256": _sha(before),
        "hunks": [{"old_start": 1, "old_lines": [old], "new_lines": [new]}],
    }


class PatchTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="koawa-d4-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _workspace(self, *, fault=None):
        resolver = WorkspacePathResolver(self.root, hard_max_read_bytes=4 * 1024 * 1024)
        workspace = AtomicPatchWorkspace(
            self.root,
            resolver,
            limits=PatchLimits(),
            fault_injector=fault,
        )
        return resolver, workspace

    def test_add_update_delete_commit_as_one_planned_transaction(self) -> None:
        old = b"old\nkeep\n"
        gone = b"remove\n"
        (self.root / "old.txt").write_bytes(old)
        (self.root / "gone.txt").write_bytes(gone)
        patch = _patch(
            [
                {
                    "operation": "delete",
                    "path": "gone.txt",
                    "base_sha256": _sha(gone),
                },
                {
                    "operation": "add",
                    "path": "new.txt",
                    "content": "created\n",
                    "newline": "lf",
                    "utf8_bom": False,
                },
                _update("old.txt", old, "old", "updated"),
            ]
        )
        resolver, workspace = self._workspace()
        with resolver:
            result = workspace.apply(patch)

        self.assertEqual(b"updated\nkeep\n", (self.root / "old.txt").read_bytes())
        self.assertEqual(b"created\n", (self.root / "new.txt").read_bytes())
        self.assertFalse((self.root / "gone.txt").exists())
        self.assertEqual(["gone.txt", "new.txt", "old.txt"], [item.path for item in result.files])
        self.assertIn("--- a/old.txt", result.files[-1].diff)
        self.assertEqual([], list(self.root.glob(".koawa-patch-*.tmp")))

    def test_all_preflight_failures_leave_every_file_unchanged(self) -> None:
        original = b"one\ntwo\n"
        (self.root / "a.txt").write_bytes(original)
        cases = (
            (
                [{**_update("a.txt", original, "one", "changed"), "base_sha256": "0" * 64}],
                "stale_patch_base",
            ),
            ([ _update("a.txt", original, "wrong", "changed") ], "patch_context_mismatch"),
            (
                [{"operation": "add", "path": "a.txt", "content": "x", "newline": "lf", "utf8_bom": False}],
                "patch_target_exists",
            ),
            (
                [{"operation": "delete", "path": "missing.txt", "base_sha256": _sha(original)}],
                "patch_target_missing",
            ),
            (
                [{"operation": "add", "path": "../escape.txt", "content": "x", "newline": "lf", "utf8_bom": False}],
                "invalid_workspace_path",
            ),
            (
                [{"operation": "add", "path": ".git/config", "content": "x", "newline": "lf", "utf8_bom": False}],
                "repository_control_path_forbidden",
            ),
        )
        for changes, code in cases:
            resolver, workspace = self._workspace()
            with self.subTest(code=code), resolver, self.assertRaises(PatchError) as raised:
                workspace.apply(_patch(changes))
            self.assertEqual(code, raised.exception.code)
            self.assertEqual(original, (self.root / "a.txt").read_bytes())
            self.assertFalse((self.root.parent / "escape.txt").exists())

    def test_second_stage_failure_has_zero_target_writes_and_no_temp_files(self) -> None:
        a = b"a\n"
        b = b"b\n"
        (self.root / "a.txt").write_bytes(a)
        (self.root / "b.txt").write_bytes(b)

        def fault(point: str, path: str) -> None:
            if point == "after_stage" and path == "b.txt":
                raise OSError("injected")

        resolver, workspace = self._workspace(fault=fault)
        with resolver, self.assertRaises(PatchError) as raised:
            workspace.apply(_patch([_update("a.txt", a, "a", "A"), _update("b.txt", b, "b", "B")]))

        self.assertEqual("workspace_stage_failed", raised.exception.code)
        self.assertEqual(a, (self.root / "a.txt").read_bytes())
        self.assertEqual(b, (self.root / "b.txt").read_bytes())
        self.assertEqual([], list(self.root.glob(".koawa-patch-*.tmp")))

    def test_mid_commit_failure_rolls_back_already_replaced_files(self) -> None:
        a = b"a\n"
        b = b"b\n"
        (self.root / "a.txt").write_bytes(a)
        (self.root / "b.txt").write_bytes(b)

        def fault(point: str, path: str) -> None:
            if point == "after_commit" and path == "a.txt":
                raise OSError("injected")

        resolver, workspace = self._workspace(fault=fault)
        with resolver, self.assertRaises(PatchError) as raised:
            workspace.apply(_patch([_update("a.txt", a, "a", "A"), _update("b.txt", b, "b", "B")]))

        self.assertEqual("workspace_commit_failed", raised.exception.code)
        self.assertEqual(a, (self.root / "a.txt").read_bytes())
        self.assertEqual(b, (self.root / "b.txt").read_bytes())
        self.assertEqual([], list(self.root.glob(".koawa-patch-*.tmp")))

    def test_failure_after_original_move_restores_original_identity_and_bytes(self) -> None:
        original = b"old\n"
        target = self.root / "a.txt"
        target.write_bytes(original)
        before_identity = target.stat().st_ino

        def fault(point: str, path: str) -> None:
            if point == "after_original_moved" and path == "a.txt":
                raise OSError("injected")

        resolver, workspace = self._workspace(fault=fault)
        with resolver, self.assertRaises(PatchError) as raised:
            workspace.apply(_patch([_update("a.txt", original, "old", "new")]))

        self.assertEqual("workspace_commit_failed", raised.exception.code)
        self.assertEqual(original, target.read_bytes())
        self.assertEqual(before_identity, target.stat().st_ino)
        self.assertEqual([], list(self.root.glob(".koawa-patch-*.tmp")))

    def test_concurrent_identity_change_at_commit_gate_is_not_overwritten(self) -> None:
        original = b"same\n"
        target = self.root / "a.txt"
        target.write_bytes(original)

        def fault(point: str, path: str) -> None:
            if point == "before_commit" and path == "a.txt":
                replacement = self.root / "replacement.tmp"
                replacement.write_bytes(original)
                os.replace(replacement, target)

        resolver, workspace = self._workspace(fault=fault)
        with resolver, self.assertRaises(PatchError) as raised:
            workspace.apply(_patch([_update("a.txt", original, "same", "changed")]))

        self.assertEqual("stale_patch_base", raised.exception.code)
        self.assertEqual(original, target.read_bytes())

    def test_rollback_failure_is_explicit_outcome_unknown(self) -> None:
        original = b"a\n"
        target = self.root / "a.txt"
        target.write_bytes(original)

        def fault(point: str, path: str) -> None:
            if point in {"after_commit", "before_rollback"} and path == "a.txt":
                raise OSError("injected")

        resolver, workspace = self._workspace(fault=fault)
        with resolver, self.assertRaises(PatchError) as raised:
            workspace.apply(_patch([_update("a.txt", original, "a", "A")]))

        self.assertEqual("workspace_outcome_unknown", raised.exception.code)

    def test_result_diff_is_deterministically_bounded(self) -> None:
        original = ("x" * 1_000 + "\n").encode()
        (self.root / "a.txt").write_bytes(original)
        limits = PatchLimits(
            max_files=1,
            max_path_chars=64,
            max_result_chars=1_024,
        )
        resolver = WorkspacePathResolver(self.root, hard_max_read_bytes=limits.max_file_bytes)
        workspace = AtomicPatchWorkspace(self.root, resolver, limits=limits)
        with resolver:
            result = workspace.apply(
                _patch([_update("a.txt", original, "x" * 1_000, "y" * 1_000)])
            )
        content = result.to_tool_content(max_chars=1_024)
        document = json.loads(content)
        self.assertLessEqual(len(content), 1_024)
        self.assertTrue(document["diff_truncated"])
        self.assertEqual("a.txt", document["changes"][0]["path"])

    def test_workspace_lock_serializes_same_base_and_only_one_patch_wins(self) -> None:
        original = b"value\n"
        (self.root / "a.txt").write_bytes(original)
        patch_a = _patch([_update("a.txt", original, "value", "A")])
        patch_b = _patch([_update("a.txt", original, "value", "B")])
        barrier = threading.Barrier(3)
        outcomes: list[str] = []
        outcome_lock = threading.Lock()

        def run(patch) -> None:
            resolver, workspace = self._workspace()
            barrier.wait()
            try:
                with resolver:
                    workspace.apply(patch)
                outcome = "success"
            except PatchError as error:
                outcome = error.code
            with outcome_lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=run, args=(patch_a,)),
            threading.Thread(target=run, args=(patch_b,)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(["stale_patch_base", "success"], sorted(outcomes))
        self.assertIn((self.root / "a.txt").read_bytes(), {b"A\n", b"B\n"})


if __name__ == "__main__":
    unittest.main()
