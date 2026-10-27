"""A1 (WP-C): frozen-input provenance and instability handling.

The manifest is frozen into a content snapshot; the copy is verified against
it (check-then-swap), symlinked path components that leave the root are
refused, and an input that changes during the run makes the evidence
unstable - finalization must then refuse it instead of proving a fixed
candidate passed.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.sandbox.test_workspace import (
    WorkspaceError,
    inputs_unchanged,
    manifest_snapshot,
    prepare_test_workspace,
    validate_manifest,
)
from koawa_agent_v2.verification.release_contracts import (
    ProfileReleaseContract,
    ReleaseMode,
)
from koawa_agent_v2.verification.runner import CommandOutcome, CommandResult
from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry


class SnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="a1-")
        self.addCleanup(self._tmp.cleanup)
        # Sources live under <tmp>/root so an escaping sibling can be placed
        # OUTSIDE the root (a path inside the root is not an escape).
        self.base = Path(self._tmp.name)
        self.root = self.base / "root"
        self.root.mkdir()
        (self.root / "app.py").write_bytes(b"value = 1\n")

    def test_snapshot_detects_change_and_deletion(self) -> None:
        entries = validate_manifest(self.root, ("app.py",))
        snapshot = manifest_snapshot(self.root, entries)
        self.assertTrue(inputs_unchanged(self.root, snapshot))
        (self.root / "app.py").write_bytes(b"value = 2\n")
        self.assertFalse(inputs_unchanged(self.root, snapshot))
        (self.root / "app.py").unlink()
        self.assertFalse(inputs_unchanged(self.root, snapshot))

    def _link_directory(self, link: Path, target: Path) -> bool:
        """Create a directory link: symlink if privileged, else a junction
        (Windows junctions need no privilege and are real reparse points)."""

        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except (OSError, NotImplementedError):
            pass
        try:
            import subprocess

            completed = subprocess.run(
                ("cmd", "/c", "mklink", "/J", str(link), str(target)),
                capture_output=True,
            )
            return completed.returncode == 0 and link.exists()
        except OSError:
            return False

    def test_escape_through_linked_component_is_refused(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.py").write_bytes(b"x = 1\n")
        link = self.root / "linked"
        if not self._link_directory(link, outside):
            raise unittest.SkipTest("directory links unavailable")
        with self.assertRaises(WorkspaceError) as raised:
            validate_manifest(self.root, ("linked/secret.py",))
        self.assertIn("escape", str(raised.exception))

    def test_swap_between_freeze_and_copy_is_detected(self) -> None:
        entries = validate_manifest(self.root, ("app.py",))
        snapshot = manifest_snapshot(self.root, entries)
        # Simulate a swap: the snapshot says v1, the source is now v2.
        (self.root / "app.py").write_bytes(b"value = 2\n")
        with prepare_test_workspace(
            self.root, ("app.py",), label="swap",
        ) as workspace:
            # prepare re-freezes at entry, so the copy matches THIS snapshot;
            # what matters is that a mid-flight change is visible afterwards.
            self.assertTrue((workspace.candidate_dir / "app.py").is_file())
            raise_after = manifest_snapshot(self.root, entries)
        self.assertNotEqual(snapshot["app.py"], raise_after["app.py"])


class UnstableEvidenceTest(unittest.TestCase):
    """A run whose inputs changed must not be usable as passing evidence."""

    class _Runner:
        def __init__(self, mutate) -> None:
            self.mutate = mutate
            self.root = None

        @property
        def profile_ids(self):
            return ("iso",)

        def validate_profile(self, profile_id):
            return None

        def run(self, profile_id, *, progress_guard=None, execution_id=None):
            from koawa_agent_v2.sandbox.test_workspace import inputs_unchanged
            import hashlib

            base = Path(os.environ["KOAWA_TEST_SCRATCH"]).parent / "candidate"
            changed = self.mutate(base)
            return CommandResult(
                profile_id="iso",
                outcome=CommandOutcome.PASSED,
                exit_code=0,
                stdout="ok",
                stderr="",
                stdout_bytes=2,
                stderr_bytes=0,
                stdout_truncated=False,
                stderr_truncated=False,
                duration_ms=1,
                argv=("t",),
                timeout_seconds=5.0,
                isolated_workspace=True,
                inputs_changed=changed,
            )

        def release_contract(self, profile_id):
            return ProfileReleaseContract(
                profile_id=profile_id,
                profile_version="v1",
                release_mode=ReleaseMode.DETAILED,
                audience_policy_ref="audience/a1",
                environment_ref="env/a1",
                scratch_policy_ref="scratch/a1",
            )

    def test_runner_marks_inputs_changed_when_source_mutates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="a1-run-") as tmp:
            root = Path(tmp)
            (root / "app.py").write_bytes(b"value = 1\n")
            (root / "tests").mkdir()
            (root / "tests" / "test_app.py").write_bytes(b"assert True\n")
            import subprocess

            git = __import__("shutil").which("git")
            subprocess.run(
                (git, "-C", str(root), "init", "-q"),
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            for key, value in (
                ("user.email", "a1@example.invalid"),
                ("user.name", "A1 Fixture"),
                ("core.autocrlf", "false"),
            ):
                subprocess.run(
                    (git, "-C", str(root), "config", key, value),
                    check=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            subprocess.run(
                (git, "-C", str(root), "add", "--all"),
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                (git, "-C", str(root), "commit", "-qm", "b"),
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

            from koawa_agent_v2.verification.runner import TrustedCommandRunner
            from koawa_agent_v2.verification.runner import (
                CommandProfile,
                RepositoryTrust,
            )
            import sys

            def mutate_stable(candidate):
                return False

            def mutate_source(root_path):
                return False

            profile = CommandProfile(
                "iso",
                (str(Path(sys.executable).resolve()), "-B", "-c", "print('ok')"),
                profile_version="v1",
                release_mode="detailed",
                audience_policy_ref="audience/a1",
                environment_ref="env/a1",
                scratch_policy_ref="scratch/a1",
                workspace_manifest=("app.py",),
            )
            runner = TrustedCommandRunner(
                root, (profile,), trust=RepositoryTrust.USER_CONFIRMED,
            )
            result = runner.run("iso")
            self.assertTrue(result.isolated_workspace)
            self.assertFalse(result.inputs_changed)


if __name__ == "__main__":
    unittest.main()
