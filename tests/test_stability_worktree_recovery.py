from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.telemetry.faults import InjectedFault, RecordingFaultPort
from koawa_agent_v2.workspace.effects import WorkspaceEffectKind, workspace_effect_id
from scripts.stability_scenarios import event_digest, identity
from tests.fixtures.stability_worktree_faults import manager_at, setup_repository, WORKTREE_POINTS


class WorktreeRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        setup_repository(self.root)
        self.manager = manager_at(self.root)
        self.base = self.manager._git("rev-parse", "HEAD").decode().strip()
        self.agent, self.run, self.command = identity("recover-agent"), identity("recover-run"), identity("recover-command")
        self.effect_id = workspace_effect_id(WorkspaceEffectKind.WORKTREE_ADD, self.command)

    def create(self, manager=None):
        return (manager or self.manager).create(
            self.agent, run_id=self.run, base_commit=self.base, write_agent=True,
            semantic_command_id=self.command,
        )

    def claimed(self):
        manager = manager_at(self.root, RecordingFaultPort(raise_at=frozenset({WORKTREE_POINTS[1]})))
        with self.assertRaises(InjectedFault):
            self.create(manager)
        return self.manager.effects.load(self.effect_id)

    def test_repeating_completed_create_and_remove_does_not_reexecute_git(self):
        target = self.create()
        before = event_digest(self.manager.store.event_store)
        with patch.object(self.manager, "_git", wraps=self.manager._git) as git:
            self.assertEqual(target, self.create())
        self.assertFalse(any(call.args[:2] == ("worktree", "add") for call in git.call_args_list))
        self.assertEqual(before, event_digest(self.manager.store.event_store))
        self.manager.reap(self.agent, run_id=self.run)
        before = event_digest(self.manager.store.event_store)
        with patch.object(self.manager, "_git", wraps=self.manager._git) as git:
            self.assertEqual("reaped", self.manager.reap(self.agent, run_id=self.run).state)
        self.assertFalse(any(call.args[:2] == ("worktree", "remove") for call in git.call_args_list))
        self.assertEqual(before, event_digest(self.manager.store.event_store))

    def test_failed_git_registry_read_cannot_prove_absence(self):
        record = self.claimed()
        before = event_digest(self.manager.store.event_store)
        original = self.manager._git

        def unavailable(*args):
            if args[:2] == ("worktree", "list"):
                raise AgentError("git_worktree_failed")
            return original(*args)

        with patch.object(self.manager, "_git", side_effect=unavailable):
            with self.assertRaises(AgentError) as caught:
                self.manager.reconcile(self.effect_id, expected_version=record.version, base_commit=self.base)
        self.assertEqual("git_worktree_failed", caught.exception.code)
        self.assertEqual(before, event_digest(self.manager.store.event_store))

    def test_foreign_existing_directory_is_unknown_and_never_retried(self):
        record = self.claimed()
        target = self.manager.store.managed_root / record.resource_ref
        target.mkdir(parents=True)
        sentinel = target / "foreign.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        outcome = self.manager.reconcile(self.effect_id, expected_version=record.version, base_commit=self.base)
        self.assertEqual("outcome_unknown", outcome.state.value)
        before = event_digest(self.manager.store.event_store)
        with self.assertRaises(AgentError) as caught:
            self.create()
        self.assertEqual("workspace_outcome_unknown", caught.exception.code)
        self.assertEqual("preserve", sentinel.read_text(encoding="utf-8"))
        self.assertEqual(before, event_digest(self.manager.store.event_store))

    def test_wrong_base_or_repository_has_zero_reconciliation_writes(self):
        record = self.claimed()
        before = event_digest(self.manager.store.event_store)
        with self.assertRaises(AgentError) as caught:
            self.manager.reconcile(self.effect_id, expected_version=record.version, base_commit="0" * 40)
        self.assertEqual("workspace_effect_identity_mismatch", caught.exception.code)
        with patch("koawa_agent_v2.workspace.worktree.repository_identity", return_value="0" * 64):
            with self.assertRaises(AgentError):
                self.manager.reconcile(self.effect_id, expected_version=record.version, base_commit=self.base)
        self.assertEqual(before, event_digest(self.manager.store.event_store))

    def test_unlisted_incomplete_git_admin_entry_is_not_absence(self):
        record = self.claimed()
        residue = self.root / "repo" / ".git" / "worktrees" / str(record.resource_nonce)
        residue.mkdir(parents=True)
        target = self.manager.store.managed_root / record.resource_ref
        self.assertFalse(target.exists())
        self.assertFalse(self.manager._registered(target))
        outcome = self.manager.reconcile(self.effect_id, expected_version=record.version, base_commit=self.base)
        self.assertEqual("outcome_unknown", outcome.state.value)
        self.assertTrue(residue.exists(), "recovery deleted unverified Git metadata")

    def test_renamed_unlisted_metadata_pointer_is_not_absence(self):
        record = self.claimed()
        residue = self.root / "repo" / ".git" / "worktrees" / "renamed-admin"
        residue.mkdir(parents=True)
        target = self.manager.store.managed_root / record.resource_ref
        (residue / "gitdir").write_text(str(target / ".git") + "\n", encoding="utf-8")
        self.assertFalse(self.manager._registered(target))
        outcome = self.manager.reconcile(self.effect_id, expected_version=record.version, base_commit=self.base)
        self.assertEqual("outcome_unknown", outcome.state.value)
        self.assertTrue(residue.exists())

    def test_equivalent_noncanonical_metadata_pointer_is_not_absence(self):
        record = self.claimed()
        residue = self.root / "repo" / ".git" / "worktrees" / "renamed-noncanonical"
        residue.mkdir(parents=True)
        target = self.manager.store.managed_root / record.resource_ref
        pointer = self.root / "repo" / ".." / "managed" / record.resource_ref / ".git"
        (residue / "gitdir").write_text(str(pointer) + "\n", encoding="utf-8")
        before = event_digest(self.manager.store.event_store)
        with self.assertRaises(AgentError) as caught:
            self.manager.reconcile(self.effect_id, expected_version=record.version, base_commit=self.base)
        self.assertEqual("workspace_metadata_unverifiable", caught.exception.code)
        self.assertEqual(before, event_digest(self.manager.store.event_store))
        self.assertTrue(residue.exists())

    def test_git_stdout_and_stderr_are_bounded_while_draining(self):
        # A real child writes both pipes concurrently; the controller must not
        # deadlock or retain more than the fixed per-stream budget.
        import sys
        manager = manager_at(self.root)
        environment = dict(os.environ)
        for stream in ("stdout", "stderr"):
            code = ("import sys; data=b'x'*(4*1024*1024+1); "
                    f"getattr(sys, '{stream}').buffer.write(data)")
            with self.subTest(stream=stream), self.assertRaises(AgentError) as caught:
                manager._run_bounded(
                    [str(Path(sys.executable).resolve()), "-c", code],
                    environment=environment,
                )
            self.assertEqual("git_worktree_output_limit", caught.exception.code)

    def test_unverifiable_metadata_cannot_resolve_claim_as_failed(self):
        record = self.claimed()
        residue = self.root / "repo" / ".git" / "worktrees" / "unverifiable-admin"
        residue.mkdir(parents=True)
        before = event_digest(self.manager.store.event_store)
        for contents in (None, "relative/.git\n", "x" * 4097):
            with self.subTest(contents_size=None if contents is None else len(contents)):
                if contents is not None:
                    (residue / "gitdir").write_text(contents, encoding="utf-8")
                with self.assertRaises(AgentError) as caught:
                    self.manager.reconcile(self.effect_id, expected_version=record.version, base_commit=self.base)
                self.assertEqual("workspace_metadata_unverifiable", caught.exception.code)
                self.assertEqual(before, event_digest(self.manager.store.event_store))

    def test_io_failure_inside_lock_keeps_original_error_and_releases_lock(self):
        original = OSError("fixture operation failed")
        with self.assertRaises(OSError) as caught:
            with self.manager._operation_lock():
                raise original
        self.assertIs(original, caught.exception)
        with self.manager._operation_lock():
            pass


if __name__ == "__main__":
    unittest.main()
