"""Audit F6 regression: a failed rollback must not delete the surviving
backup of the original content.

Commit moves a.txt to a backup temp file; if restoring that backup also
fails, the outcome is unknown and the backup is the only copy of the
original.  The cleanup stage used to unlink it unconditionally, turning an
"unknown" into irreversible data loss.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.editing.protocol import PatchError
from koawa_agent_v2.editing.transaction import AtomicPatchWorkspace
from koawa_agent_v2.tools.repository import WorkspacePathResolver
from tests.test_patch_transaction import _patch, _sha, _update


class RollbackKeepsBackupTest(unittest.TestCase):
    def test_failed_restore_preserves_backup_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-f6-") as directory:
            root = Path(directory)
            target = root / "a.txt"
            original = b"a\n"
            target.write_bytes(original)
            resolver = WorkspacePathResolver(root)
            workspace = AtomicPatchWorkspace(
                root,
                resolver,
                fault_injector=lambda point, path: (
                    (_ for _ in ()).throw(OSError("injected"))
                    if point in {"after_original_moved", "before_rollback"}
                    and path == "a.txt"
                    else None
                ),
            )
            with resolver:
                with self.assertRaises(PatchError) as raised:
                    workspace.apply(_patch([_update("a.txt", original, "a", "A")]))
            self.assertEqual("workspace_outcome_unknown", raised.exception.code)
            backups = list(root.glob(".koawa-patch-backup-*.tmp"))
            self.assertEqual(1, len(backups))
            self.assertEqual(original, backups[0].read_bytes())


if __name__ == "__main__":
    unittest.main()
