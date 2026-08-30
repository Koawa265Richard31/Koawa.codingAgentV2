"""Real artifact apply/retest/deliver kill windows and fresh-process recovery."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from uuid import NAMESPACE_URL, UUID, uuid5

from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.control.event_store import EventMetadata, NewEvent, StreamId, StreamWrite
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.telemetry.faults import FAULT_SPECS, NoOpFaultPort
from koawa_agent_v2.workspace.artifacts import (
    ArtifactPackageRef, ArtifactPackageStore, ArtifactV2, TestEvidenceRef,
    package_from_snapshot,
)
from koawa_agent_v2.workspace.container import ContainerResult
from koawa_agent_v2.workspace.content import capture_repository, repository_identity
from koawa_agent_v2.workspace.effects import WorkspaceEffectState, WorkspaceEffectStore
from koawa_agent_v2.workspace.integration import DurableArtifactIntegrator, IntegrationReceiptRef
from scripts.stability_benchmark import atomic_json


ARTIFACT_POINTS = tuple(
    f"s5.workspace.{kind}.{phase}"
    for kind in ("apply", "retest", "deliver")
    for phase in ("after_intent_commit", "after_claim_commit", "after_effect_before_ack")
)
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, "koawa-artifact-kill-v1:" + name)


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True,
        timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


class MarkerRunner:
    image_digest = "sha256:" + "c" * 64

    def __init__(self, root: Path):
        self.path = root / "runner-effects.json"

    def run(self, _worktree, _argv, *, timeout=60.0):
        count = 0
        if self.path.exists():
            count = json.loads(self.path.read_text(encoding="utf-8"))["count"]
        atomic_json(self.path, {"count": count + 1})
        return ContainerResult(0, "", "", self.image_digest)


def _artifact_document(artifact: ArtifactV2) -> dict:
    reference = artifact.test_evidence_ref
    return {
        "artifact_id": str(artifact.artifact_id), "agent_id": str(artifact.agent_id),
        "run_id": str(artifact.run_id),
        "repository_identity_digest": artifact.repository_identity_digest,
        "base_commit": artifact.base_commit,
        "repo_prestate_digest": artifact.repo_prestate_digest,
        "package": {
            "package_ref": artifact.package.package_ref,
            "package_digest": artifact.package.package_digest,
            "package_json_bytes": artifact.package.package_json_bytes,
        },
        "diff_digest": artifact.diff_digest,
        "working_tree_content_digest": artifact.working_tree_content_digest,
        "evidence": {
            "category": reference.stream_id.category,
            "aggregate_id": str(reference.stream_id.aggregate_id),
            "stream_version": reference.stream_version,
            "event_id": str(reference.event_id),
            "evidence_digest": reference.evidence_digest,
        },
        "sandbox_image_digest": artifact.sandbox_image_digest,
        "sandbox_profile_digest": artifact.sandbox_profile_digest,
        "created_at": artifact.created_at.isoformat(),
    }


def _artifact_from(document: dict) -> ArtifactV2:
    package = document["package"]
    evidence = document["evidence"]
    return ArtifactV2(
        UUID(document["artifact_id"]), UUID(document["agent_id"]),
        UUID(document["run_id"]), document["repository_identity_digest"],
        document["base_commit"], document["repo_prestate_digest"],
        ArtifactPackageRef(
            package["package_ref"], package["package_digest"],
            package["package_json_bytes"],
        ),
        document["diff_digest"], document["working_tree_content_digest"],
        TestEvidenceRef(
            StreamId(evidence["category"], UUID(evidence["aggregate_id"])),
            evidence["stream_version"], UUID(evidence["event_id"]),
            evidence["evidence_digest"],
        ),
        document["sandbox_image_digest"], document["sandbox_profile_digest"],
        datetime.fromisoformat(document["created_at"]),
    )


def _seed(root: Path) -> tuple[ArtifactV2, SqliteEventStore, ArtifactPackageStore]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "artifact-kill")
    _git(repo, "config", "user.email", "artifact-kill@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "value.txt").write_bytes(b"base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    store = SqliteEventStore(root / "events.sqlite3")
    packages = ArtifactPackageStore(root / "state", event_store=store)
    source = root / "source"
    _git(repo, "worktree", "add", "--detach", str(source), base)
    (source / "value.txt").write_bytes(b"changed\n")
    snapshot = capture_repository(source, base_commit=base)
    package = package_from_snapshot(
        repository_identity_digest=repository_identity(repo), base_commit=base,
        tracked_binary_patch=snapshot.diff_bytes, entries=(),
        prestate_digest=snapshot.prestate.prestate_digest,
        poststate_manifest_digest=snapshot.working_tree_content_digest,
    )
    package_ref = packages.put(package)
    payload = {"result": "passed", "exit_code": 0}
    command = _id("evidence-command")
    event = NewEvent(
        _id("evidence-event"), "verification.completed.v1", 1, NOW, payload,
        EventMetadata(command, command, actor="artifact-kill"),
    )
    stream = StreamId("verification", _id("evidence-stream"))
    store.append_batch((StreamWrite(stream, -1, (event,)),), idempotency_key=command)
    artifact = ArtifactV2.create(
        agent_id=_id("agent"), run_id=_id("run"),
        repository_identity_digest=package.repository_identity_digest,
        base_commit=base, repo_prestate_digest=snapshot.prestate.prestate_digest,
        package=package_ref, diff_digest=snapshot.diff_digest,
        working_tree_content_digest=snapshot.working_tree_content_digest,
        test_evidence_ref=TestEvidenceRef(stream, 0, event.event_id, _digest(payload)),
        sandbox_image_digest="sha256:" + "a" * 64,
        sandbox_profile_digest="b" * 64, created_at=NOW,
    )
    _git(repo, "worktree", "remove", "--force", str(source))
    return artifact, store, packages


def _integrator(root: Path, artifact: ArtifactV2, store, packages, *, port=None):
    return DurableArtifactIntegrator(
        event_store=store, package_store=packages,
        effect_store=WorkspaceEffectStore(store, fault_port=port or NoOpFaultPort()),
        repo_root=root / "repo", integration_root=root / "integration",
        runner=MarkerRunner(root), owner_id="artifact-kill",
    )


class ArtifactKillPort(NoOpFaultPort):
    def __init__(self, root: Path, request: dict):
        self.root, self.request = root, request

    def hit(self, point, facts):
        super().hit(point, facts)
        if point != self.request["point"]:
            return
        effect = WorkspaceEffectStore(
            SqliteEventStore(self.root / "events.sqlite3"),
        ).load(UUID(facts["effect_id"]))
        phase = point.rsplit(".", 1)[-1]
        expected = (
            WorkspaceEffectState.INTENDED if phase == "after_intent_commit"
            else WorkspaceEffectState.CLAIMED
        )
        assert effect.state is expected
        kind = point.split(".")[2]
        external = False
        if phase == "after_effect_before_ack":
            if kind == "apply":
                external = (self.root / "integration" / "value.txt").read_bytes() == b"changed\n"
            elif kind == "retest":
                external = json.loads((self.root / "runner-effects.json").read_text())["count"] >= 1
            else:
                external = (self.root / "repo" / "value.txt").read_bytes() == b"changed\n"
            assert external
        atomic_json(self.root / "ready.json", {
            "point": point, "point_class": FAULT_SPECS[point].point_class.value,
            "crash_pid": os.getpid(), "effect_id": str(effect.effect_id),
            "effect_version": effect.version, "effect_state": effect.state.value,
            "external_marker": external,
        })
        Event().wait()


def crash_artifact(root: Path, point: str) -> None:
    if point not in ARTIFACT_POINTS:
        raise ValueError("unsupported artifact point")
    artifact, store, packages = _seed(root)
    command = _id("operation:" + point)
    request = {
        "point": point, "command_id": str(command),
        "artifact": _artifact_document(artifact),
    }
    atomic_json(root / "request.json", request)
    if ".deliver." in point:
        receipt = _integrator(root, artifact, store, packages).integrate(
            [artifact], test_argv=[sys.executable, "-c", "raise SystemExit(0)"],
            command_id=_id("integration:" + point),
        ).receipt
        request["receipt"] = {
            "receipt_id": str(receipt.receipt_id),
            "stream_version": receipt.stream_version,
            "event_id": str(receipt.event_id),
            "receipt_digest": receipt.receipt_digest,
        }
        atomic_json(root / "request.json", request)
        _integrator(
            root, artifact, store, packages, port=ArtifactKillPort(root, request),
        ).deliver(receipt, command_id=command)
    else:
        _integrator(
            root, artifact, store, packages, port=ArtifactKillPort(root, request),
        ).integrate(
            [artifact], test_argv=[sys.executable, "-c", "raise SystemExit(0)"],
            command_id=command,
        )
    raise AssertionError("artifact operation missed kill point")


def recover_artifact(root: Path) -> None:
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    marker = json.loads((root / "ready.json").read_text(encoding="utf-8"))
    artifact = _artifact_from(request["artifact"])
    store = SqliteEventStore(root / "events.sqlite3")
    packages = ArtifactPackageStore(root / "state", event_store=store)
    integrator = _integrator(root, artifact, store, packages)
    point = request["point"]
    phase = point.rsplit(".", 1)[-1]
    command = UUID(request["command_id"])
    unknown = phase != "after_intent_commit"
    try:
        if ".deliver." in point:
            value = request["receipt"]
            receipt = IntegrationReceiptRef(
                UUID(value["receipt_id"]), value["stream_version"],
                UUID(value["event_id"]), value["receipt_digest"],
            )
            integrator.deliver(receipt, command_id=command)
        else:
            integrator.integrate(
                [artifact], test_argv=[sys.executable, "-c", "raise SystemExit(0)"],
                command_id=command,
            )
    except AgentError as error:
        if not unknown or error.code != "workspace_outcome_unknown":
            raise
    else:
        if unknown:
            raise AssertionError("uncertain external effect was silently replayed")
    effect = WorkspaceEffectStore(store).load(UUID(marker["effect_id"]))
    assert effect.state is (
        WorkspaceEffectState.OUTCOME_UNKNOWN if unknown
        else WorkspaceEffectState.APPLIED
    )
    if ".deliver." in point:
        lease_events = [
            event for event in store.read_all()
            if event.stream_id.category == "workspace-delivery-lease"
        ]
        assert lease_events[-1].event_type == "workspace.delivery-lease-released.v1"
    runner_count = 0
    if (root / "runner-effects.json").exists():
        runner_count = json.loads((root / "runner-effects.json").read_text())["count"]
    normalized = [
        [event.stream_id.category, event.stream_version, event.event_type]
        for event in store.read_all()
    ]
    atomic_json(root / "recovered.json", {
        "point": point, "recovery_pid": os.getpid(),
        "state": effect.state.value, "runner_count": runner_count,
        "repo_changed": (root / "repo" / "value.txt").read_bytes() == b"changed\n",
        "normalized_events": normalized,
    })
