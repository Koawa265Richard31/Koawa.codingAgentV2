"""D12 durable per-agent workspace inventory with a safe reaper."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from ..agents.graph import AgentError
from ..control.event_store import (
    EventMetadata,
    NewEvent,
    StreamId,
    StreamWrite,
)


_SHA = re.compile(r"[0-9a-f]{7,64}")
_BRANCH = re.compile(r"[a-z][a-z0-9_./:-]{0,127}")


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    agent_id: UUID
    run_id: UUID | None
    worktree_path: str
    base_commit: str
    branch: str
    state: str
    version: int


class AgentWorkspaceStore:
    """Inventory for per-agent Git worktrees; deletion is never broad."""

    def __init__(self, event_store, *, managed_root: Path) -> None:
        self.event_store = event_store
        self.managed_root = Path(managed_root).resolve()

    def allocate(
        self,
        agent_id: UUID,
        *,
        run_id: UUID,
        worktree_path: Path,
        base_commit: str,
        branch: str,
    ) -> WorkspaceRecord:
        resolved = Path(worktree_path).resolve()
        if not self._inside_managed(resolved):
            raise AgentError("workspace_outside_managed_root")
        if not _SHA.fullmatch(base_commit) or not _BRANCH.fullmatch(branch):
            raise AgentError("invalid_workspace_identity")
        command_id = uuid4()
        event = NewEvent(
            uuid5(command_id, "event:workspace"),
            "workspace.allocated.v1",
            1,
            datetime.now(timezone.utc),
            {
                "agent_id": str(agent_id),
                "run_id": str(run_id),
                "worktree_path": str(resolved),
                "base_commit": base_commit,
                "branch": branch,
                "state": "created",
            },
            EventMetadata(command_id, command_id, actor="workspace-store"),
        )
        self.event_store.append_batch(
            (StreamWrite(StreamId("workspace", agent_id), -1, (event,)),),
            idempotency_key=command_id,
        )
        record = self.load(agent_id)
        if record is None:
            raise AgentError("workspace_allocated_missing")
        return record

    def load(self, agent_id: UUID) -> WorkspaceRecord | None:
        events = self._read_all(StreamId("workspace", agent_id))
        if not events:
            return None
        allocated = next(
            (
                event
                for event in events
                if event.event_type == "workspace.allocated.v1"
            ),
            None,
        )
        if allocated is None:
            return None
        latest = events[-1]
        payload = latest.payload
        return WorkspaceRecord(
            agent_id=agent_id,
            run_id=UUID(allocated.payload["run_id"]),
            worktree_path=allocated.payload["worktree_path"],
            base_commit=allocated.payload["base_commit"],
            branch=allocated.payload["branch"],
            state=payload["state"],
            version=latest.stream_version,
        )

    def reap(self, agent_id: UUID, *, run_id: UUID, reason: str) -> WorkspaceRecord:
        current = self.load(agent_id)
        if current is None or current.run_id != run_id:
            raise AgentError("stale_workspace_fenced")
        target = Path(current.worktree_path)
        if not self._inside_managed(target):
            raise AgentError("workspace_outside_managed_root")
        command_id = uuid4()
        event = NewEvent(
            uuid5(command_id, "event:workspace-reap"),
            "workspace.reaped.v1",
            1,
            datetime.now(timezone.utc),
            {
                "agent_id": str(agent_id),
                "run_id": str(run_id),
                "worktree_path": current.worktree_path,
                "reason": reason,
                "state": "reaped",
            },
            EventMetadata(command_id, command_id, actor="workspace-store"),
        )
        self.event_store.append_batch(
            (StreamWrite(StreamId("workspace", agent_id), current.version, (event,)),),
            idempotency_key=command_id,
        )
        # Safe cleanup: only the exact resolved managed worktree is removed.
        if target.exists():
            for child in sorted(target.iterdir(), reverse=True):
                if child.is_dir():
                    _remove_tree(child)
                else:
                    child.unlink()
            target.rmdir()
        return self.load(agent_id)

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
            page = self.event_store.read_stream(
                stream, after_version=cursor, limit=500
            )
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version


def _remove_tree(path: Path) -> None:
    for child in sorted(path.iterdir(), reverse=True):
        if child.is_dir():
            _remove_tree(child)
        else:
            child.unlink()
    path.rmdir()
