"""I8 real Git add/remove windows, including physical and durable markers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from threading import Event
from uuid import UUID

from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.control.durable_json import canonical_json_bytes_v1
from koawa_agent_v2.control.run_effects import effect_identity_digest
from koawa_agent_v2.telemetry.faults import FAULT_SPECS, NoOpFaultPort
from koawa_agent_v2.workspace.effects import WorkspaceEffectStore, WorkspaceEffectKind, WorkspaceEffectState, workspace_effect_id
from koawa_agent_v2.workspace.store import AgentWorkspaceStore
from koawa_agent_v2.workspace.worktree import WorktreeManager
from scripts.stability_benchmark import atomic_json
from scripts.stability_scenarios import event_digest, identity, store_at


WORKTREE_POINTS = (
    "s5.workspace.add.after_intent_commit", "s5.workspace.add.after_claim_commit",
    "s5.workspace.add.after_effect_before_ack", "s5.workspace.remove.after_intent_commit",
    "s5.workspace.remove.after_claim_commit", "s5.workspace.remove.after_effect_before_ack",
)
DELTAS = dict(zip(WORKTREE_POINTS, (2, 3, 3, 2, 3, 4)))


def manager_at(root, port=None):
    store = store_at(root / "runtime.db")
    inventory = AgentWorkspaceStore(store, managed_root=root / "managed")
    effects = WorkspaceEffectStore(store, **({"fault_port": port} if port else {}))
    return WorktreeManager(inventory, repo_root=root / "repo", effect_store=effects)


def setup_repository(root):
    repo = root / "repo"
    repo.mkdir()
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git_executable_unavailable")
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull}
    for args in (("init", "-b", "main"), ("config", "user.name", "fixture"),
                 ("config", "user.email", "fixture@example.invalid"), ("config", "core.autocrlf", "false")):
        subprocess.run([git, *args], cwd=repo, env=env, capture_output=True, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    for args in (("add", "a.txt"), ("-c", "core.hooksPath=", "commit", "-m", "fixture")):
        subprocess.run([git, *args], cwd=repo, env=env, capture_output=True, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class WorktreeKillPort(NoOpFaultPort):
    def __init__(self, root, request):
        self.root, self.request = root, request

    def hit(self, point, facts):
        super().hit(point, facts)
        if point != self.request["point"]:
            return
        manager = manager_at(self.root)
        record = manager.effects.load(UUID(self.request["effect_id"]))
        assert str(record.effect_id) == facts["effect_id"]
        expected_version = 0 if point in (WORKTREE_POINTS[0], WORKTREE_POINTS[3]) else 1
        assert record.version == expected_version
        target = manager._safe_target(record.resource_ref)
        exists = point in (WORKTREE_POINTS[2], WORKTREE_POINTS[3], WORKTREE_POINTS[4])
        assert target.exists() == exists
        assert manager._registered(target) == exists
        if exists:
            manager._verify_active(target, self.request["base_commit"])
            assert (target / "a.txt").read_text(encoding="utf-8") == "alpha\n"
        store = manager.store.event_store
        assert len(store.read_all()) == self.request["baseline_count"] + DELTAS[point]
        atomic_json(self.root / "ready.json", {
            "point": point, "point_class": FAULT_SPECS[point].point_class.value,
            "crash_pid": os.getpid(), "effect_version": record.version,
            "exists": exists, "event_digest": event_digest(store),
        })
        Event().wait()


def crash_worktree(root, point):
    if point not in WORKTREE_POINTS:
        raise ValueError("unsupported worktree scenario")
    setup_repository(root)
    manager = manager_at(root)
    base = manager._git("rev-parse", "HEAD").decode().strip()
    agent, run = identity("worktree-agent"), identity("worktree-run")
    add_command, remove_command = identity("worktree-add"), identity("worktree-remove")
    remove = point in WORKTREE_POINTS[3:]
    if remove:
        manager.create(agent, run_id=run, base_commit=base, write_agent=True, semantic_command_id=add_command)
    command = remove_command if remove else add_command
    kind = WorkspaceEffectKind.WORKTREE_REMOVE if remove else WorkspaceEffectKind.WORKTREE_ADD
    request = {"point": point, "base_commit": base, "agent_id": str(agent), "run_id": str(run),
               "command_id": str(command), "effect_id": str(workspace_effect_id(kind, command)),
               "baseline_count": len(manager.store.event_store.read_all())}
    atomic_json(root / "request.json", request)
    manager = manager_at(root, WorktreeKillPort(root, request))
    if remove:
        manager.reap(agent, run_id=run, semantic_command_id=command)
    else:
        manager.create(agent, run_id=run, base_commit=base, write_agent=True, semantic_command_id=command)
    raise AssertionError("worktree production operation missed kill point")


def normalized_events(manager):
    events = manager.store.event_store.read_all()
    intents = {str(event.stream_id.aggregate_id): dict(event.payload) for event in events
               if event.event_type == "workspace.effect-intended.v2"}
    output = []
    for event in events:
        payload = json.loads(canonical_json_bytes_v1(event.payload))
        for key in tuple(payload):
            if key.endswith("_at"):
                # Validate timestamps before normalizing the permitted clock field.
                from datetime import datetime
                datetime.fromisoformat(payload[key].replace("Z", "+00:00"))
                payload[key] = "<timestamp>"
        if "repository_identity_digest" in payload:
            assert payload["repository_identity_digest"] == manager.repository_identity_digest
            payload["repository_identity_digest"] = "<repository-path-identity>"
        if event.event_type == "run.effect-linked.v1":
            intent = {key: value for key, value in intents[payload["stream_id"]].items() if not key.endswith("_at")}
            assert payload["identity_digest"] == effect_identity_digest(intent)
            intent["repository_identity_digest"] = "<repository-path-identity>"
            payload["identity_digest"] = effect_identity_digest(intent)
        output.append({"stream": event.stream_id.key, "version": event.stream_version,
                       "type": event.event_type, "payload": payload,
                       "commit_index": event.commit_index, "commit_size": event.commit_size})
    return output


def recover_worktree(root):
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    marker = json.loads((root / "ready.json").read_text(encoding="utf-8"))
    manager = manager_at(root)
    store = manager.store.event_store
    assert event_digest(store) == marker["event_digest"]
    effect_id = UUID(request["effect_id"])
    agent, run, command = UUID(request["agent_id"]), UUID(request["run_id"]), UUID(request["command_id"])
    point = request["point"]
    record = manager.effects.load(effect_id)
    if record.state is WorkspaceEffectState.INTENDED:
        if point in WORKTREE_POINTS[3:]:
            manager.reap(agent, run_id=run, semantic_command_id=command)
        else:
            manager.create(agent, run_id=run, base_commit=request["base_commit"], write_agent=True,
                           semantic_command_id=command)
    else:
        record = manager.reconcile(effect_id, expected_version=record.version, base_commit=request["base_commit"])
    record = manager.effects.load(effect_id)
    expected_state = ("failed_before_effect" if point == WORKTREE_POINTS[1] else
                      "outcome_unknown" if point == WORKTREE_POINTS[4] else "applied")
    assert record.state.value == expected_state
    before_retry = event_digest(store)
    for _ in range(2):
        manager.reconcile(effect_id, expected_version=record.version, base_commit=request["base_commit"])
    assert event_digest(store) == before_retry, "reconciliation_duplicated_facts"
    try:
        manager.reconcile(effect_id, expected_version=-1, base_commit=request["base_commit"])
    except AgentError as error:
        assert error.code == "workspace_effect_version_conflict"
    else:
        raise AssertionError("stale_reconciler_was_not_fenced")
    inventory = manager.store.load(agent, run_id=run)
    target = manager._safe_target(record.resource_ref)
    expected_exists = point in (WORKTREE_POINTS[0], WORKTREE_POINTS[2], WORKTREE_POINTS[4])
    assert target.exists() == expected_exists and manager._registered(target) == expected_exists
    if inventory is not None:
        assert inventory.state == ("unknown" if expected_state == "outcome_unknown" else
                                   "active" if expected_exists else "reaped")
    else:
        assert expected_state == "failed_before_effect"
    assert manager._git("status", "--porcelain") == b""
    assert (root / "repo" / "a.txt").read_text(encoding="utf-8") == "alpha\n"
    atomic_json(root / "recovered.json", {
        "point": point, "recovery_pid": os.getpid(), "effect_state": expected_state,
        "inventory_state": inventory.state if inventory else None, "exists": expected_exists,
        "normalized_events": normalized_events(manager),
    })
