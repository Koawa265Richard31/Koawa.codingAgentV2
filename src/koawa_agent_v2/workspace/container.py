"""D12 container boundary: injectable runner (real Docker is env-gated)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence
from uuid import uuid4

from ..agents.graph import AgentError


@dataclass(frozen=True, slots=True)
class ContainerResult:
    exit_code: int | None
    stdout: str
    stderr: str
    image_digest: str


class ContainerRunner(Protocol):
    def run(
        self,
        worktree: Path,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
    ) -> ContainerResult: ...


class InjectedContainerRunner:
    """Host-bootstrap runner for deterministic tests (D8 real runner later)."""

    image_digest = "injected-test-image"

    def run(
        self,
        worktree: Path,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
    ) -> ContainerResult:
        try:
            result = subprocess.run(
                list(argv),
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise AgentError("container_run_failed") from None
        return ContainerResult(
            result.returncode,
            result.stdout,
            result.stderr,
            self.image_digest,
        )


class DockerContainerRunner:
    """Real D8 Docker runner bound to one immutable image."""

    def __init__(
        self,
        event_store,
        *,
        image_id: str,
        docker_executable: str = "docker",
        limits=None,
    ) -> None:
        self._event_store = event_store
        self._image_id = image_id
        self._docker_executable = docker_executable
        self._limits = limits

    def run(
        self,
        worktree: Path,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
    ) -> ContainerResult:
        from ..sandbox.protocol import SandboxCommandProfile
        from ..sandbox.runtime import DockerCommandRunner
        from ..verification.runner import CommandRunnerError

        profile = SandboxCommandProfile(
            "d12-worktree-run",
            tuple(argv),
            working_directory="/workspace",
            timeout_seconds=timeout,
        )
        try:
            runner = DockerCommandRunner(
                worktree,
                [profile],
                self._event_store,
                self._image_id,
                docker_executable=self._docker_executable,
                limits=self._limits,
            )
        except CommandRunnerError as error:
            code = getattr(error, "code", None)
            if code in (
                "docker_daemon_unavailable",
                "docker_executable_unavailable",
                "sandbox_image_unavailable",
            ):
                raise AgentError("docker_unavailable") from None
            raise AgentError("container_run_failed") from None
        result = runner.run_profile_object(profile, execution_id=uuid4())
        return ContainerResult(
            result.exit_code,
            result.stdout,
            result.stderr,
            self._image_id,
        )
