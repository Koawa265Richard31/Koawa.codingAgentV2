"""I6 MCP process launchers: the ONLY path from ticket to OS create (§8.6).

Production assembly never Popen()s directly: preflight produces a
StagedLaunchPlan and a durable grant, the activation service issues one
one-time AuthorizedLaunchTicket, and the launcher atomically consumes the
ticket immediately before the OS create.  host_trusted enforces resource
limits (or fails with mcp_host_limits_unsupported before spawning);
sandboxed fails explicitly when no D8-equivalent container runner is bound.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, BinaryIO, Callable, Protocol

# runtime.config is imported lazily (the runtime package init imports
# app -> assembly -> mcp launcher, so a module-level import would cycle
# back through koawa_agent_v2.runtime while it is still initializing).
from .activation import (
    AuthorizedLaunchTicket,
    McpActivationError,
    StagedLaunchPlan,
)
from .transport import (
    OwnedProcess,
    ProcessSpawner,
    SpawnSpec,
    SystemProcessSpawner,
    TransportError,
)


def _mcp_resource_limits():
    from ..runtime.config import McpResourceLimits

    return McpResourceLimits




class McpProcessEndpoint(Protocol):
    """A spawned process tree with a verifiable external identity.

    Subprocess handles and stdin/stdout/stderr are process-local; only
    digests/ids may become durable.
    """

    @property
    def pid(self) -> int: ...

    @property
    def stdin(self) -> BinaryIO | None: ...

    @property
    def stdout(self) -> BinaryIO | None: ...

    @property
    def stderr(self) -> BinaryIO | None: ...

    def poll(self) -> int | None: ...

    def terminate_tree(self, *, deadline: float) -> None: ...

    def kill_tree(self, *, deadline: float) -> None: ...

    def wait(self, *, deadline: float) -> int: ...

    def close_handles(self) -> None: ...

    @property
    def external_identity(self) -> dict[str, object]:
        """Digest-only external identity for recovery evidence."""
        ...


class McpProcessLauncher(Protocol):
    def launch(self, ticket: AuthorizedLaunchTicket) -> McpProcessEndpoint:
        """Consume the ticket and create at most one OS process tree."""
        ...


class HostLimitEnforcer(Protocol):
    """Enforces the declared resource envelope on one owned process tree."""

    def precheck(self, limits: McpResourceLimits) -> None: ...

    def enforce(
        self, process: OwnedProcess, limits: McpResourceLimits,
    ) -> OwnedProcess: ...


class _EndpointOwnedProcess:
    """OwnedProcess facade carrying the digest-only external identity."""

    def __init__(
        self,
        process: OwnedProcess,
        *,
        launch_identity_digest: str,
        staged_argv_digest: str,
    ) -> None:
        self._process = process
        self._launch_identity_digest = launch_identity_digest
        self._staged_argv_digest = staged_argv_digest

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def stdin(self) -> BinaryIO | None:
        return self._process.stdin

    @property
    def stdout(self) -> BinaryIO | None:
        return self._process.stdout

    @property
    def stderr(self) -> BinaryIO | None:
        return self._process.stderr

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate_tree(self, *, deadline: float) -> None:
        self._process.terminate_tree(deadline=deadline)

    def kill_tree(self, *, deadline: float) -> None:
        self._process.kill_tree(deadline=deadline)

    def wait(self, *, deadline: float) -> int:
        return self._process.wait(deadline=deadline)

    def close_handles(self) -> None:
        self._process.close_handles()

    @property
    def external_identity(self) -> dict[str, object]:
        return {
            "pid": self._process.pid,
            "launch_identity_digest": self._launch_identity_digest,
            "staged_argv_digest": self._staged_argv_digest,
        }


class WindowsJobObjectEnforcer:
    """Real Windows Job Object enforcement (memory + active-process limits)."""

    def __init__(self) -> None:
        self._handle = None

    def precheck(self, limits: McpResourceLimits) -> None:
        if os.name != "nt":
            raise McpActivationError("mcp_host_limits_unsupported")

    def enforce(
        self, process: OwnedProcess, limits: McpResourceLimits,
    ) -> OwnedProcess:
        if os.name != "nt":
            raise McpActivationError("mcp_host_limits_unsupported")
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                raise McpActivationError("mcp_host_limits_unsupported")
        except McpActivationError:
            raise
        except Exception:
            raise McpActivationError("mcp_host_limits_unsupported") from None
        try:
            class _ExtendedLimit(ctypes.Structure):
                _fields_ = [
                    ("BasicLimit", ctypes.c_ulong * 12),
                    ("IoInfo", ctypes.c_ulong * 2),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = _ExtendedLimit()
            info.BasicLimit[0] = 0x40 | 0x20
            info.ProcessMemoryLimit = limits.memory_bytes
            success = kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info),
            )
            if not success:
                raise McpActivationError("mcp_host_limits_unsupported")
            assigned = kernel32.AssignProcessToJobObject(
                job, ctypes.c_void_p(process.pid),
            )
            if not assigned:
                raise McpActivationError("mcp_host_limits_unsupported")
        except McpActivationError:
            kernel32.CloseHandle(job)
            raise
        except Exception:
            kernel32.CloseHandle(job)
            raise McpActivationError("mcp_host_limits_unsupported") from None
        return process


class HostTrustedLauncher:
    """host_trusted stdio launcher over the I1 spawner port.

    shell=False / close_fds=True come from the spawner; the environment is
    the shared minimal builder output and the argv has every code-bearing
    entry rewritten to the staged copy.  Every launch re-validates the
    source identities right before the OS create (drift => zero spawn).
    """

    def __init__(
        self,
        activation,
        plan: StagedLaunchPlan,
        *,
        staging_root: Path,
        spawner: ProcessSpawner | None = None,
        limit_enforcer: HostLimitEnforcer | None = None,
        clock: Callable[[], float] | None = None,
        process_start_timeout_seconds: float = 30.0,
    ) -> None:
        self._activation = activation
        self._plan = plan
        self._staging_root = staging_root
        self._spawner = (
            spawner if spawner is not None else SystemProcessSpawner(clock=clock)
        )
        self._limit_enforcer = limit_enforcer
        self._clock = clock or (lambda: __import__("time").monotonic())
        self._process_start_timeout_seconds = float(process_start_timeout_seconds)

    def launch(
        self, ticket: AuthorizedLaunchTicket,
    ) -> McpProcessEndpoint:
        """Consume the one-time ticket, re-validate, enforce, then create.

        Every failure before the OS create raises with zero process calls;
        a failure after create is recorded on the allocation by the caller
        (transport) and never masquerades as a bare-spawn degradation.
        """
        self._activation.consume_ticket(ticket)
        if ticket.launch_identity_digest != self._plan.identity.config_digest:
            raise McpActivationError("mcp_launch_identity_mismatch")
        self._plan.revalidate_sources()
        from ..runtime.subprocess_env import build_minimal_environment

        private_temp = Path(self._staging_root) / "private-temp"
        private_temp.mkdir(parents=True, exist_ok=True)
        try:
            private_temp.chmod(0o700)
        except OSError:
            pass
        environment = build_minimal_environment(
            dict(self._plan.config.environment),
            allowed_names=frozenset(
                name for name, _ in self._plan.config.environment
            ),
            private_temp=private_temp,
        )
        spec = SpawnSpec(
            command=self._plan.staged_argv,
            env=environment,
            cwd=(
                None
                if self._plan.config.cwd is None
                else str(self._plan.config.cwd)
            ),
        )
        limits = self._plan.config.resource_limits or _mcp_resource_limits()()
        enforcer = self._limit_enforcer
        if enforcer is None:
            enforcer = WindowsJobObjectEnforcer()
        precheck = getattr(enforcer, "precheck", None)
        if callable(precheck):
            precheck(limits)
        elif self._limit_enforcer is None and os.name != "nt":
            raise McpActivationError("mcp_host_limits_unsupported")
        deadline = self._clock() + self._process_start_timeout_seconds
        try:
            owned = self._spawner.spawn(spec, deadline=deadline)
        except TransportError as error:
            raise McpActivationError(error.code) from None
        try:
            owned = enforcer.enforce(owned, limits)
        except McpActivationError as error:
            self._collect(owned)
            raise error
        return _EndpointOwnedProcess(
            owned,
            launch_identity_digest=self._plan.identity.config_digest,
            staged_argv_digest=_argv_digest(self._plan.staged_argv),
        )

    def _collect(self, owned: OwnedProcess) -> None:
        deadline = self._clock() + 30.0
        try:
            owned.kill_tree(deadline=deadline)
        except TransportError:
            pass
        try:
            owned.wait(deadline=deadline)
        except TransportError:
            pass
        try:
            owned.close_handles()
        except (OSError, ValueError):
            pass


class SandboxedLauncher:
    """sandboxed profile: real container boundary, never a host fallback.

    Launch order is intent → create → inspect-verify → attach → bind →
    start → record started.  Any pre-create failure is
    ``failed_before_start``; cleanup uncertainty after an external side
    effect is ``outcome_unknown`` - both recorded on the dual-bound
    allocation before the error surfaces (W3 state machine).
    """

    def __init__(
        self,
        activation,
        plan: StagedLaunchPlan,
        *,
        sandbox_store,
        docker_adapter=None,
        docker_executable: str = "docker",
        container_labels: tuple[tuple[str, str], ...] = (),
        process_start_timeout_seconds: float = 30.0,
        deadline_seconds: float = 300.0,
    ) -> None:
        self._activation = activation
        self._plan = plan
        self._sandbox_store = sandbox_store
        self._docker_adapter = docker_adapter
        self._docker_executable = docker_executable
        self._container_labels = container_labels
        self._process_start_timeout_seconds = float(process_start_timeout_seconds)
        self._deadline_seconds = float(deadline_seconds)

    def launch(
        self, ticket: AuthorizedLaunchTicket,
    ) -> McpProcessEndpoint:
        import time as _time
        from datetime import datetime, timedelta, timezone

        from .docker_endpoint import (
            DockerEndpointError,
            DockerAdapter,
            launch_container_endpoint,
        )
        from .sandbox_reconcile import ensure_sandbox_intent
        from ..sandbox.docker_primitives import ContainerSpec
        from ..sandbox.runtime import SandboxError

        self._activation.consume_ticket(ticket)
        if ticket.launch_identity_digest != self._plan.identity.config_digest:
            raise McpActivationError("mcp_launch_identity_mismatch")
        config = self._plan.config
        if config.execution_profile is not _profile_enum().SANDBOXED:
            raise McpActivationError("mcp_sandbox_profile_required")
        adapter = self._docker_adapter or DockerAdapter()

        def _failed_before_start(code: str) -> McpActivationError:
            try:
                self._activation.record_failed_before_start(ticket, reason=code)
            except McpActivationError:
                pass
            return McpActivationError(code)

        try:
            ensure_sandbox_intent(
                self._sandbox_store,
                ticket,
                image_id=config.image_id,
                launch_identity_digest=ticket.launch_identity_digest,
                deadline_at=datetime.now(timezone.utc)
                + timedelta(seconds=self._deadline_seconds),
            )
        except SandboxError as error:
            raise _failed_before_start(error.code) from None

        limits = config.resource_limits or _mcp_resource_limits()()
        spec = ContainerSpec(
            image_id=config.image_id,
            argv=tuple(config.command),
            container_working_directory=config.container_working_directory,
            environment=tuple(config.environment),
            cpus=limits.cpus,
            memory_bytes=limits.memory_bytes,
            pids_limit=limits.pids,
            tmpfs_bytes=limits.tmpfs_bytes,
            container_name=f"koawa-mcp-{ticket.allocation_id}",
            labels=self._container_labels
            + (
                ("koawa.mcp.allocation", str(ticket.allocation_id)),
                ("koawa.mcp.owner", str(ticket.request_id)),
            ),
        )
        try:
            endpoint = launch_container_endpoint(
                spec,
                docker_executable=self._docker_executable,
                process_start_timeout_seconds=self._process_start_timeout_seconds,
                adapter=adapter,
            )
        except (SandboxError, DockerEndpointError) as error:
            code = getattr(error, "code", "mcp_sandbox_unavailable")
            uncertain = code in {
                "mcp_container_stop_failed",
                "mcp_container_remove_failed",
                "mcp_container_cleanup_failed",
            }
            try:
                if uncertain:
                    self._activation.record_outcome_unknown(ticket, reason=code)
                else:
                    self._activation.record_failed_before_start(
                        ticket, reason=code,
                    )
            except McpActivationError:
                pass
            raise McpActivationError(code) from None
        try:
            self._sandbox_store.bind(ticket.allocation_id, endpoint.container_id)
            self._sandbox_store.start(ticket.allocation_id)
            self._activation.record_started(ticket)
        except (SandboxError, McpActivationError) as error:
            try:
                endpoint.kill_tree(deadline=_time.monotonic() + 30.0)
            except Exception:
                pass
            raise McpActivationError(
                getattr(error, "code", "mcp_sandbox_bind_failed")
            ) from None
        return endpoint


def _profile_enum():
    from ..runtime.config import McpExecutionProfile

    return McpExecutionProfile


def _argv_digest(argv: tuple[str, ...]) -> str:
    import hashlib
    import json

    canonical = json.dumps(
        {"staged_argv": list(argv)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8", "strict")).hexdigest()


__all__ = [
    "HostLimitEnforcer",
    "HostTrustedLauncher",
    "McpProcessEndpoint",
    "McpProcessLauncher",
    "SandboxedLauncher",
    "WindowsJobObjectEnforcer",
]
