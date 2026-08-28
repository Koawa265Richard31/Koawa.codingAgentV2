from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4, uuid5

from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.control.event_store import EventMetadata, NewEvent, StreamId, StreamWrite
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.workspace.artifacts import (
    ArtifactPackageStore, ArtifactV2, TestEvidenceRef, package_from_snapshot,
)
from koawa_agent_v2.workspace.container import InjectedContainerRunner
from koawa_agent_v2.workspace.content import capture_repository, repository_identity
from koawa_agent_v2.workspace.effects import WorkspaceEffectStore
from koawa_agent_v2.workspace.integration import (
    DurableArtifactIntegrator, IntegrationReceiptRef,
)


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _digest_doc(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class I7DurableIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="koawa-i7-integration-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.name", "i7")
        _git(self.repo, "config", "user.email", "i7@example.invalid")
        _git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "value.txt").write_bytes(b"base\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        self.events = SqliteEventStore(self.root / "events.sqlite3")
        self.packages = ArtifactPackageStore(self.root / "state", event_store=self.events)
        self.effects = WorkspaceEffectStore(self.events)
        self.source = self.root / "source"
        _git(self.repo, "worktree", "add", "--detach", str(self.source), self.base)
        (self.source / "value.txt").write_bytes(b"changed\n")
        snapshot = capture_repository(self.source, base_commit=self.base)
        package = package_from_snapshot(
            repository_identity_digest=repository_identity(self.repo),
            base_commit=self.base,
            tracked_binary_patch=snapshot.diff_bytes,
            entries=(),
            prestate_digest=snapshot.prestate.prestate_digest,
            poststate_manifest_digest=snapshot.working_tree_content_digest,
        )
        package_ref = self.packages.put(package)
        evidence_payload = {"result": "passed", "exit_code": 0}
        evidence_command = uuid4()
        evidence_event = NewEvent(
            uuid5(evidence_command, "evidence"), "verification.completed.v1", 1,
            datetime.now(timezone.utc), evidence_payload,
            EventMetadata(evidence_command, evidence_command, actor="test"),
        )
        evidence_stream = StreamId("verification", uuid4())
        self.events.append_batch(
            (StreamWrite(evidence_stream, -1, (evidence_event,)),),
            idempotency_key=evidence_command,
        )
        self.artifact = ArtifactV2.create(
            agent_id=uuid4(), run_id=uuid4(),
            repository_identity_digest=package.repository_identity_digest,
            base_commit=self.base, repo_prestate_digest=snapshot.prestate.prestate_digest,
            package=package_ref, diff_digest=snapshot.diff_digest,
            working_tree_content_digest=snapshot.working_tree_content_digest,
            test_evidence_ref=TestEvidenceRef(
                evidence_stream, 0, evidence_event.event_id, _digest_doc(evidence_payload)
            ),
            sandbox_image_digest="sha256:" + "a" * 64,
            sandbox_profile_digest="b" * 64,
        )
        _git(self.repo, "worktree", "remove", "--force", str(self.source))
        self.integrator = DurableArtifactIntegrator(
            event_store=self.events, package_store=self.packages,
            effect_store=self.effects, repo_root=self.repo,
            integration_root=self.root / "integration",
            runner=InjectedContainerRunner(),
        )

    def test_source_removed_package_integrates_and_receipt_delivers(self) -> None:
        result = self.integrator.integrate(
            [self.artifact], test_argv=[sys.executable, "-c", "raise SystemExit(0)"],
            command_id=uuid4(),
        )
        self.assertEqual("success", result.test_result_kind)
        self.assertFalse(self.source.exists())
        delivery_command = uuid4()
        self.integrator.deliver(result.receipt, command_id=delivery_command)
        # A response-loss retry observes the APPLIED effect and does not apply twice.
        event_count = len(self.events.read_all())
        self.integrator.deliver(result.receipt, command_id=delivery_command)
        self.assertEqual(event_count, len(self.events.read_all()))
        self.assertEqual(b"changed\n", (self.repo / "value.txt").read_bytes())
        event_types = [event.event_type for event in self.events.read_all()]
        self.assertEqual(1, event_types.count("workspace.delivery-lease-acquired.v1"))
        self.assertEqual(1, event_types.count("workspace.delivery-lease-released.v1"))

    def test_known_negative_is_applied_but_not_deliverable(self) -> None:
        result = self.integrator.integrate(
            [self.artifact], test_argv=[sys.executable, "-c", "raise SystemExit(3)"],
            command_id=uuid4(),
        )
        self.assertEqual("known_negative", result.test_result_kind)
        self.assertEqual(3, result.test_exit_code)
        with self.assertRaises(AgentError) as raised:
            self.integrator.deliver(result.receipt, command_id=uuid4())
        self.assertEqual("integration_known_negative_not_deliverable", raised.exception.code)

    def test_forged_receipt_is_rejected(self) -> None:
        result = self.integrator.integrate(
            [self.artifact], test_argv=[sys.executable, "-c", "pass"],
            command_id=uuid4(),
        )
        forged = IntegrationReceiptRef(
            result.receipt.receipt_id, result.receipt.stream_version,
            uuid4(), result.receipt.receipt_digest,
        )
        with self.assertRaises(AgentError) as raised:
            self.integrator.deliver(forged, command_id=uuid4())
        self.assertEqual("integration_receipt_invalid", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
