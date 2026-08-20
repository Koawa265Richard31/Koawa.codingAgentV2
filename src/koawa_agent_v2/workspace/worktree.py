"""D12 host-coordinated Git worktree creation and diff capture."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID

from ..agents.graph import AgentError
from .store import AgentWorkspaceStore


class WorktreeManager:
    """Create per-agent worktrees and collect diffs; Git runs on the host."""

    def __init__(
        self,
        store: AgentWorkspaceStore,
        *,
        repo_root: Path,
        git_binary: str = "git",
    ) -> None:
        self.store = store
        self.repo_root = Path(repo_root).resolve()
        self.git_binary = git_binary

    def user_worktree_dirty(self) -> bool:
        result = self._git("status", "--porcelain")
        return bool(result.strip())

    def create(
        self,
        agent_id: UUID,
        *,
        run_id: UUID,
        base_commit: str,
        branch: str,
        write_agent: bool,
    ) -> Path:
        worktree_path = self.store.managed_root / str(agent_id)
        if write_agent and self.user_worktree_dirty():
            raise AgentError("dirty_user_worktree_requires_snapshot")
        self._git("worktree", "add", "--detach", str(worktree_path), base_commit)
        return self.store.allocate(
            agent_id,
            run_id=run_id,
            worktree_path=worktree_path,
            base_commit=base_commit,
            branch=branch,
        ).worktree_path

    def diff(self, agent_id: UUID, *, base_commit: str) -> str:
        record = self.store.load(agent_id)
        if record is None:
            raise AgentError("workspace_missing")
        worktree = Path(record.worktree_path)
        return self._git(
            "-C",
            str(worktree),
            "diff",
            "--binary",
            base_commit,
        )

    def _git(self, *arguments: str) -> str:
        command = [self.git_binary, *arguments]
        try:
            result = subprocess.run(
                command,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise AgentError("git_worktree_failed") from None
        if result.returncode != 0:
            raise AgentError("git_worktree_failed")
        return result.stdout
