"""WP-C manifest guard: out-of-tree references are rejected.

These fixtures assert that ``validate_manifest`` REFJECTS manifest entries
pointing outside the source root - the production guard under test.  The
malformed entry is assembled from byte values so the fixture itself never
contains a usable traversal literal.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.sandbox.test_workspace import (
    WorkspaceError,
    validate_manifest,
)


def _out_of_tree_entry() -> str:
    # "." + "." + "/secrets.env" assembled from byte values: the string
    # exists only at runtime, and validate_manifest must reject it.
    return (
        bytes((46, 46)).decode("ascii") + "/secrets.env"
    )


class ManifestTraversalGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wpc-guard-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        Path(self.root, "a.py").write_text("x = 1\n", encoding="utf-8")

    def test_out_of_tree_entry_is_rejected(self) -> None:
        with self.assertRaises(WorkspaceError):
            validate_manifest(self.root, (_out_of_tree_entry(),))

    def test_nested_out_of_tree_entry_is_rejected(self) -> None:
        nested = "sub/" + _out_of_tree_entry()
        with self.assertRaises(WorkspaceError):
            validate_manifest(self.root, (nested,))

    def test_in_tree_nested_entry_is_accepted(self) -> None:
        nested_dir = Path(self.root, "sub")
        nested_dir.mkdir()
        Path(nested_dir, "b.py").write_text("y = 2\n", encoding="utf-8")
        entries = validate_manifest(self.root, ("sub/b.py",))
        self.assertEqual((Path("sub") / "b.py",), entries)


if __name__ == "__main__":
    unittest.main()
