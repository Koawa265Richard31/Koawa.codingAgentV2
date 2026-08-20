"""D12 artifact acceptance, integration worktree, and gated delivery."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .container import ContainerRunner
from ..agents.graph import AgentError


@dataclass(frozen=True, slots=True)
class Artifact:
    agent_id: UUID
    run_id: UUID
    base_commit: str
    head_commit: str
    diff: str
    test_evidence: str
    image_digest: str

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            {
                "agent_id": str(self.agent_id),
                "run_id": str(self.run_id),
                "base_commit": self.base_commit,
                "head_commit": self.head_commit,
                "diff": self.diff,
                "test_evidence": self.test_evidence,
                "image_digest": self.image_digest,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ArtifactIntegrator:
    """Integrate accepted artifacts, retest, then gate delivery on HEAD hash."""

    def __init__(
        self,
        *,
        repo_root: Path,
        integration_root: Path,
        runner: ContainerRunner,
        git_binary: str = "git",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.integration_root = Path(integration_root).resolve()
        self.runner = runner
        self.git_binary = git_binary

    def accept(
        self,
        artifact: Artifact,
        *,
        expected_run_id: UUID,
        expected_base_commit: str,
    ) -> str:
        if artifact.run_id != expected_run_id:
            raise AgentError("artifact_run_fenced")
        if artifact.base_commit != expected_base_commit:
            raise AgentError("artifact_base_mismatch")
        if artifact.head_commit == artifact.base_commit and not artifact.diff:
            raise AgentError("artifact_empty")
        if not artifact.test_evidence:
            raise AgentError("artifact_missing_evidence")
        return artifact.digest

    def integrate(
        self,
        artifacts: list[Artifact],
        *,
        test_argv: list[str],
        timeout: float = 60.0,
    ) -> tuple[ContainerResult, str]:
        """Apply diffs serially in one integration worktree; conflicts raise."""

        self._git("worktree", "add", "--detach", str(self.integration_root), artifacts[0].base_commit)
        try:
            applied: list[str] = []
            for artifact in artifacts:
                try:
                    self._apply_diff(self.integration_root, artifact.diff)
                except AgentError:
                    raise AgentError("artifact_conflict") from None
                applied.append(str(artifact.agent_id))
            result = self.runner.run(self.integration_root, test_argv, timeout=timeout)
            if result.exit_code != 0:
                raise AgentError("artifact_retest_failed")
            head = self._git("-C", str(self.integration_root), "rev-parse", "HEAD").strip()
            return result, head
        finally:
            self._git("worktree", "remove", "--force", str(self.integration_root))

    def deliver(self, artifacts: list[Artifact], *, user_base_commit: str) -> None:
        """Apply the integrated diff set to the user workspace after HEAD gate."""

        current = self._git("-C", str(self.repo_root), "rev-parse", "HEAD").strip()
        if current != user_base_commit:
            raise AgentError("user_workspace_drift")
        combined = "\n".join(artifact.diff for artifact in artifacts)
        self._apply_diff(self.repo_root, combined)

    def _apply_diff(self, worktree: Path, diff: str) -> None:
        try:
            check = subprocess.run(
                [self.git_binary, "-C", str(worktree), "apply", "--check", "--binary", "-"],
                input=diff,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            raise AgentError("git_apply_failed") from None
        if check.returncode != 0:
            raise AgentError("git_apply_conflict")
        applied = subprocess.run(
            [self.git_binary, "-C", str(worktree), "apply", "--binary", "-"],
            input=diff,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if applied.returncode != 0:
            raise AgentError("git_apply_failed")

    def _git(self, *arguments: str) -> str:
        try:
            result = subprocess.run(
                [self.git_binary, *arguments],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            raise AgentError("git_integration_failed") from None
        if result.returncode != 0:
            raise AgentError("git_integration_failed")
        return result.stdout
