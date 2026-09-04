"""D25 W2: long-lived Docker endpoint for one sandboxed stdio MCP server.

生命周期合同（§5）：endpoint 管理两个外部对象——attach 客户端进程与容器。
关闭/终止成功必须同时证明 attach 句柄已收集且容器已停止并移除；只完成
其中一个不算成功。PID 只是传输句柄（§4.9）；授权身份使用 container id。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import BinaryIO

from ..sandbox.docker_primitives import (
    ContainerSpec,
    _remove_exact,
    _resolve_executable,
    _run_cli,
    create_arguments,
    inspect_and_validate,
    stop_and_remove,
)
from ..sandbox.runtime import SandboxError


class DockerEndpointError(RuntimeError):
    """Stable, content-free container endpoint failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _popen_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class _AttachProcess:
    """Minimal process facade over the ``docker start --attach`` client."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self._popen = popen

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def stdin(self) -> BinaryIO | None:
        return self._popen.stdin

    @property
    def stdout(self) -> BinaryIO | None:
        return self._popen.stdout

    @property
    def stderr(self) -> BinaryIO | None:
        return self._popen.stderr

    def poll(self) -> int | None:
        return self._popen.poll()

    def wait_bounded(self, deadline: float) -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DockerEndpointError("mcp_container_wait_timeout")
        try:
            return self._popen.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise DockerEndpointError("mcp_container_wait_timeout") from None

    def kill(self) -> None:
        try:
            self._popen.kill()
        except (OSError, ValueError):
            pass
        try:
            self._popen.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass

    def close_handles(self) -> None:
        for stream in (
            self._popen.stdin,
            self._popen.stdout,
            self._popen.stderr,
        ):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


class DockerMcpEndpoint:
    """McpProcessEndpoint over one attach client + one container."""

    def __init__(
        self,
        *,
        attach: _AttachProcess,
        container_id: str,
        image_digest: str,
        container_name: str,
        docker: Path,
        adapter: DockerAdapter,
    ) -> None:
        self._attach = attach
        self._container_id = container_id
        self._image_digest = image_digest
        self._container_name = container_name
        self._docker: Path = docker
        self._adapter = adapter
        self._terminated = False
        self._handles_closed = False

    @property
    def pid(self) -> int:
        return self._attach.pid

    @property
    def stdin(self) -> BinaryIO | None:
        return self._attach.stdin

    @property
    def stdout(self) -> BinaryIO | None:
        return self._attach.stdout

    @property
    def stderr(self) -> BinaryIO | None:
        return self._attach.stderr

    @property
    def container_id(self) -> str:
        return self._container_id

    def poll(self) -> int | None:
        return self._attach.poll()

    def wait(self, *, deadline: float) -> int:
        if self._terminated:
            raise DockerEndpointError("mcp_container_already_terminated")
        return self._attach.wait_bounded(deadline)

    def terminate_tree(self, *, deadline: float) -> None:
        """Graceful path: stdin EOF, bounded stop, exact remove, collect."""
        self._terminate(deadline=deadline, kill_attach=False)

    def kill_tree(self, *, deadline: float) -> None:
        self._terminate(deadline=deadline, kill_attach=True)

    def close_handles(self) -> None:
        if not self._handles_closed:
            self._attach.close_handles()
            self._handles_closed = True

    @property
    def external_identity(self) -> dict[str, object]:
        """Digest-only identity; never argv/env/stderr (§4.7)."""
        return {
            "kind": "docker-mcp-container",
            "container_id": self._container_id,
            "image_digest": self._image_digest,
            "container_name": self._container_name,
        }

    # -- internals ---------------------------------------------------------

    def _terminate(self, *, deadline: float, kill_attach: bool) -> None:
        if self._terminated:
            return
        self._terminated = True
        try:
            if kill_attach:
                self._attach.kill()
            else:
                # MCP servers treat stdin EOF as shutdown; bounded wait below
                # turns a hung server into the container-stop path.
                stdin = self._attach.stdin
                if stdin is not None:
                    try:
                        stdin.close()
                    except (OSError, ValueError):
                        pass
                try:
                    self._attach.wait_bounded(deadline)
                except DockerEndpointError:
                    self._attach.kill()
            remaining = deadline - time.monotonic()
            self._adapter.stop_and_remove(
                self._docker,
                self._container_id,
                stop_timeout_seconds=max(
                    1.0, min(10.0, remaining if remaining > 0 else 1.0)
                ),
                cli_timeout_seconds=max(5.0, remaining if remaining > 0 else 5.0),
            )
        except (SandboxError, OSError) as error:
            # The container is a real external object: an uncertain cleanup
            # must surface as a stable UNKNOWN-grade failure, never success.
            self._attach.kill()
            self.close_handles()
            raise DockerEndpointError(
                getattr(error, "code", "mcp_container_cleanup_failed")
            ) from None
        finally:
            if self._attach.poll() is None:
                self._attach.kill()
            self.close_handles()


_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")


class DockerAdapter:
    """Thin trusted wrapper over the Docker CLI (fakes replace this)."""

    def create(self, docker: Path, arguments: tuple[str, ...], *, timeout: float) -> str:
        result = _run_cli(docker, arguments, timeout_seconds=timeout)
        if result.returncode != 0 or result.timed_out:
            raise DockerEndpointError("mcp_container_create_failed")
        container_id = result.stdout.decode("ascii", "replace").strip().lower()
        if not _CONTAINER_ID.fullmatch(container_id):
            raise DockerEndpointError("mcp_container_create_invalid_id")
        return container_id

    def inspect(self, docker: Path, container_id: str, *, timeout: float) -> dict:
        import json as _json

        result = _run_cli(
            docker,
            ("container", "inspect", container_id),
            timeout_seconds=timeout,
        )
        if result.returncode != 0 or result.timed_out:
            if b"No such container" in (result.stderr or b"") or (
                b"No such object" in (result.stderr or b"")
            ):
                # Provably absent: a durable fact (already removed), not an
                # unavailable oracle.  Reconciliation consumes this.
                raise DockerEndpointError("mcp_container_absent")
            raise DockerEndpointError("mcp_container_inspect_unavailable")
        try:
            document = _json.loads(result.stdout.decode("utf-8", "strict"))
        except (ValueError, UnicodeError):
            raise DockerEndpointError("mcp_container_inspect_unavailable") from None
        if not isinstance(document, list) or not document or not isinstance(
            document[0], dict
        ):
            raise DockerEndpointError("mcp_container_inspect_unavailable")
        return document[0]

    def start_attach(
        self, docker: Path, container_id: str
    ) -> "_AttachProcess":
        try:
            popen = subprocess.Popen(
                (
                    str(docker),
                    "container",
                    "start",
                    "--attach",
                    "--interactive",
                    container_id,
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                creationflags=_popen_flags(),
            )
        except (OSError, ValueError):
            raise DockerEndpointError("mcp_container_attach_failed") from None
        return _AttachProcess(popen)

    def list_by_labels(self, docker: Path, labels: tuple[tuple[str, str], ...]) -> list[str]:
        """All containers (running or exited) carrying every given label.

        Used by crash-window reconciliation: a container created right
        before a host crash is usually not running, so ``-a`` is mandatory.
        """
        # Docker abbreviates ``.ID`` to 12 hex characters unless explicitly
        # told otherwise.  Reconciliation identity is container-id based and
        # must remain unambiguous across daemon state, so request the complete
        # 64-hex id before validating it below.
        argv = ["ps", "-a", "--no-trunc", "--format", "{{.ID}}"]
        for name, value in labels:
            argv.extend((f"--filter", f"label={name}={value}"))
        result = _run_cli(docker, tuple(argv), timeout_seconds=20.0)
        if result.returncode != 0 or result.timed_out:
            raise DockerEndpointError("mcp_container_probe_failed")
        ids = [
            line.strip().lower()
            for line in result.stdout.decode("ascii", "replace").splitlines()
            if line.strip()
        ]
        if any(not _CONTAINER_ID.fullmatch(item) for item in ids):
            raise DockerEndpointError("mcp_container_probe_invalid_id")
        return ids

    def stop_and_remove(
        self,
        docker: Path,
        container_id: str,
        *,
        stop_timeout_seconds: float,
        cli_timeout_seconds: float,
    ) -> None:
        stop_and_remove(
            docker,
            container_id,
            stop_timeout_seconds=stop_timeout_seconds,
            cli_timeout_seconds=cli_timeout_seconds,
        )


def launch_container_endpoint(
    spec: ContainerSpec,
    *,
    docker_executable: str,
    process_start_timeout_seconds: float,
    adapter: DockerAdapter | None = None,
) -> DockerMcpEndpoint:
    """Create, verify and attach one sandboxed MCP container.

    Order is intentional: create → inspect exact contract → attach.  Any
    failure before attach performs bounded cleanup and raises; no partially
    verified container is ever handed to the transport layer.
    """
    docker = _resolve_executable(docker_executable)
    adapter = adapter or DockerAdapter()
    timeout = max(10.0, process_start_timeout_seconds)
    container_id = adapter.create(docker, create_arguments(spec), timeout=timeout)
    try:
        document = adapter.inspect(docker, container_id, timeout=timeout)
        inspect_and_validate(document, spec)
        attach = adapter.start_attach(docker, container_id)
    except (SandboxError, DockerEndpointError):
        try:
            adapter.stop_and_remove(
                docker,
                container_id,
                stop_timeout_seconds=5.0,
                cli_timeout_seconds=timeout,
            )
        except (SandboxError, OSError, DockerEndpointError):
            pass  # bounded best-effort; the original failure is re-raised
        raise
    except (OSError, ValueError):
        try:
            adapter.stop_and_remove(
                docker,
                container_id,
                stop_timeout_seconds=5.0,
                cli_timeout_seconds=timeout,
            )
        except (SandboxError, OSError, DockerEndpointError):
            pass
        raise DockerEndpointError("mcp_container_launch_failed") from None
    return DockerMcpEndpoint(
        attach=attach,
        container_id=container_id,
        image_digest=spec.image_id,
        container_name=spec.container_name,
        docker=docker,
        adapter=adapter,
    )
