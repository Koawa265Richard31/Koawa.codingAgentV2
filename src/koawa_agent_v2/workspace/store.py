"""I7 projection-only inventory for deterministic detached worktrees."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from ..agents.graph import AgentError
from ..control.event_store import EventMetadata, NewEvent, StreamId, StreamWrite


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    agent_id: UUID
    run_id: UUID
    worktree_path: str
    base_commit: str
    branch: str | None
    state: str
    version: int
    allocation_id: UUID
    resource_nonce: UUID
    effect_id: UUID | None = None
    legacy_unverified: bool = False


class AgentWorkspaceStore:
    """Typed inventory projection; physical Git/filesystem work belongs to manager."""

    def __init__(self, event_store, *, managed_root: Path) -> None:
        self.event_store = event_store
        self.managed_root = Path(managed_root).resolve()

    def allocate(
        self, agent_id: UUID, *, run_id: UUID, worktree_path: Path,
        base_commit: str, branch: str | None = None,
        allocation_id: UUID | None = None, resource_nonce: UUID | None = None,
        effect_id: UUID | None = None, command_id: UUID | None = None,
    ) -> WorkspaceRecord:
        resolved = Path(worktree_path).resolve()
        if not self._inside_managed(resolved):
            raise AgentError("workspace_outside_managed_root")
        _commit(base_commit)
        allocation = allocation_id or uuid5(
            NAMESPACE_URL, f"koawa-v2:workspace-allocation:{agent_id}:{run_id}"
        )
        nonce = resource_nonce or uuid5(allocation, "resource")
        command = command_id or uuid5(allocation, "inventory-active")
        stream = _inventory_stream(agent_id)
        events = self._read_all(stream)
        expected = events[-1].stream_version if events else -1
        event = NewEvent(
            uuid5(command, "event:workspace-active"), "workspace.inventory-active.v2", 2,
            datetime.now(timezone.utc),
            {"allocation_id": str(allocation), "agent_id": str(agent_id),
             "run_id": str(run_id), "resource_nonce": str(nonce),
             "resource_ref": resolved.relative_to(self.managed_root).as_posix(),
             "base_commit": base_commit,
             "effect_id": str(effect_id) if effect_id else None, "state": "active"},
            EventMetadata(command, command, run_id=run_id, actor="workspace-store"),
        )
        self.event_store.append_batch(
            (StreamWrite(stream, expected, (event,)),), idempotency_key=command,
            request_fingerprint=f"workspace-active:{allocation}",
        )
        record = self.load(agent_id, run_id=run_id)
        if record is None:
            raise AgentError("workspace_allocated_missing")
        return record

    def mark_reap_pending(self, agent_id: UUID, *, run_id: UUID, effect_id: UUID, command_id: UUID) -> WorkspaceRecord:
        return self._transition(agent_id, run_id, "reap_pending", effect_id, command_id)

    def mark_reaped(self, agent_id: UUID, *, run_id: UUID, effect_id: UUID, command_id: UUID) -> WorkspaceRecord:
        return self._transition(agent_id, run_id, "reaped", effect_id, command_id)

    def mark_unknown(self, agent_id: UUID, *, run_id: UUID, effect_id: UUID, command_id: UUID) -> WorkspaceRecord:
        return self._transition(agent_id, run_id, "unknown", effect_id, command_id)

    def reap(self, agent_id: UUID, *, run_id: UUID, reason: str) -> WorkspaceRecord:
        """Legacy projection transition; no physical deletion occurs here."""
        current = self.load(agent_id, run_id=run_id)
        if current is None:
            raise AgentError("stale_workspace_fenced")
        effect = current.effect_id or uuid5(current.allocation_id, "legacy-reap-effect")
        return self.mark_reaped(
            agent_id, run_id=run_id, effect_id=effect,
            command_id=uuid5(current.allocation_id, f"legacy-reap:{reason}"),
        )

    def load(self, agent_id: UUID, *, run_id: UUID | None = None) -> WorkspaceRecord | None:
        records = self.list(agent_id)
        if run_id is not None:
            records = tuple(item for item in records if item.run_id == run_id)
        return records[-1] if records else None

    def list(self, agent_id: UUID) -> tuple[WorkspaceRecord, ...]:
        states: dict[UUID, WorkspaceRecord] = {}
        for event in self._read_all(_inventory_stream(agent_id)):
            payload = event.payload
            if event.event_type == "workspace.allocated.v1":
                allocation = uuid5(NAMESPACE_URL, f"koawa-v2:legacy-workspace:{agent_id}:{payload['run_id']}")
                states[allocation] = WorkspaceRecord(
                    agent_id, UUID(payload["run_id"]), payload["worktree_path"],
                    payload["base_commit"], payload.get("branch"), payload.get("state", "created"),
                    event.stream_version, allocation, uuid5(allocation, "resource"),
                    legacy_unverified=True,
                )
            elif event.event_type == "workspace.inventory-active.v2":
                allocation = UUID(payload["allocation_id"])
                states[allocation] = WorkspaceRecord(
                    agent_id, UUID(payload["run_id"]),
                    str(self.managed_root / Path(payload["resource_ref"])),
                    payload["base_commit"], None, "active", event.stream_version,
                    allocation, UUID(payload["resource_nonce"]),
                    UUID(payload["effect_id"]) if payload.get("effect_id") else None,
                )
            elif event.event_type == "workspace.inventory-state.v2":
                allocation = UUID(payload["allocation_id"])
                current = states.get(allocation)
                if current is None:
                    raise AgentError("workspace_projection_corrupt")
                states[allocation] = WorkspaceRecord(
                    current.agent_id, current.run_id, current.worktree_path, current.base_commit,
                    current.branch, payload["state"], event.stream_version, current.allocation_id,
                    current.resource_nonce, UUID(payload["effect_id"]), current.legacy_unverified,
                )
        return tuple(states.values())

    def _transition(self, agent_id: UUID, run_id: UUID, state: str, effect_id: UUID, command_id: UUID) -> WorkspaceRecord:
        current = self.load(agent_id, run_id=run_id)
        if current is None:
            raise AgentError("stale_workspace_fenced")
        stream = _inventory_stream(agent_id)
        head = self._read_all(stream)[-1].stream_version
        event = NewEvent(
            uuid5(command_id, f"event:workspace-{state}"), "workspace.inventory-state.v2", 2,
            datetime.now(timezone.utc),
            {"allocation_id": str(current.allocation_id), "run_id": str(run_id),
             "effect_id": str(effect_id), "state": state},
            EventMetadata(command_id, command_id, run_id=run_id, actor="workspace-store"),
        )
        self.event_store.append_batch(
            (StreamWrite(stream, head, (event,)),), idempotency_key=command_id,
            request_fingerprint=f"workspace-state:{current.allocation_id}:{state}:{effect_id}",
        )
        updated = self.load(agent_id, run_id=run_id)
        if updated is None:
            raise AgentError("workspace_projection_corrupt")
        return updated

    def _inside_managed(self, path: Path) -> bool:
        try:
            path.relative_to(self.managed_root)
        except ValueError:
            return False
        return path != self.managed_root

    def _read_all(self, stream: StreamId) -> tuple:
        values = []
        cursor = -1
        while True:
            page = self.event_store.read_stream(stream, after_version=cursor, limit=500)
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version


def _inventory_stream(agent_id: UUID) -> StreamId:
    return StreamId("workspace", agent_id)


def _commit(value: str) -> None:
    if not isinstance(value, str) or not 7 <= len(value) <= 64 or any(c not in "0123456789abcdef" for c in value):
        raise AgentError("invalid_workspace_identity")


__all__ = ["AgentWorkspaceStore", "WorkspaceRecord"]
