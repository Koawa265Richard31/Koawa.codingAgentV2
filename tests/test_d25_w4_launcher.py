"""D25 W4: sandboxed launcher integration chain over a fake Docker adapter."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.mcp.activation import (
    ActivationService,
    process_start_scope,
    resolve_launch_identity,
)
from koawa_agent_v2.mcp.docker_endpoint import DockerAdapter
from koawa_agent_v2.mcp.launcher import SandboxedLauncher
from koawa_agent_v2.runtime.config import (
    McpExecutionProfile,
    McpResourceLimits,
    McpServerConfig,
)
from koawa_agent_v2.sandbox.runtime import (
    AllocationState,
    SandboxAllocationStore,
)

DIGEST = "sha256:" + "a" * 64
LABELS = (("koawa.managed", "mcp-sandbox"),)


class _FakeAttach:
    def __init__(self):
        self.pid = 777
        self.poll_value = None
        self.killed = False
        self.handles_closed = False

    @property
    def stdin(self):
        return None

    @property
    def stdout(self):
        return None

    @property
    def stderr(self):
        return None

    def poll(self):
        return self.poll_value

    def wait_bounded(self, deadline):
        self.poll_value = 0
        return 0

    def kill(self):
        self.killed = True
        self.poll_value = -9

    def close_handles(self):
        self.handles_closed = True


class _RecordingDocker(DockerAdapter):
    def __init__(self):
        self.containers: dict[str, dict] = {}
        self.created_argv: list[tuple[str, ...]] = []

    def create(self, docker, arguments, *, timeout):
        self.created_argv.append(arguments)
        container_id = "e" * 64
        self.containers[container_id] = True
        return container_id

    def inspect(self, docker, container_id, *, timeout):
        return {
            "Id": container_id,
            "Config": {
                "Image": DIGEST,
                "Tty": False,
                "OpenStdin": True,
                "Entrypoint": ["/usr/local/bin/node"],
                "Cmd": ["server.js"],
                "WorkingDir": "/work",
                "User": "65532:65532",
                "Labels": {
                    "koawa.managed": "mcp-sandbox",
                    "koawa.mcp.allocation": str(self._allocation_id),
                    "koawa.mcp.owner": str(self._request_id),
                },
            },
            "HostConfig": {
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Memory": 268435456,
                "MemorySwap": 268435456,
                "NanoCpus": 1_000_000_000,
                "PidsLimit": 256,
                "Mounts": [],
            },
            "State": {"Running": True},
        }

    def start_attach(self, docker, container_id):
        return _FakeAttach()

    def stop_and_remove(self, docker, container_id, *, stop_timeout_seconds, cli_timeout_seconds):
        self.containers.pop(container_id, None)


def clock_plus(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class LauncherChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = SqliteEventStore(self.base / "w4.sqlite3")
        self.activation = ActivationService(
            self.store, clock=lambda: datetime.now(timezone.utc),
            approval_ttl_seconds=300,
        )
        self.sandbox = SandboxAllocationStore(self.store)
        self.docker = _RecordingDocker()

    def tearDown(self) -> None:
        close = getattr(self.store, "close", None)
        if close:
            close()
        self._tmp.cleanup()

    def _config(self) -> McpServerConfig:
        return McpServerConfig(
            server_id="svc",
            command=("/usr/local/bin/node", "server.js"),
            environment=(("MCP_MODE", "stdio"),),
            execution_profile=McpExecutionProfile.SANDBOXED,
            image_id=DIGEST,
            resource_limits=McpResourceLimits(memory_bytes=268435456),
            container_working_directory="/work",
        )

    def _ticket_for(self, config: McpServerConfig):
        identity = resolve_launch_identity(config, base_dir=self.base)
        view = self.activation.plan_start(
            identity,
            principal_id="root",
            scope=process_start_scope(config.execution_profile),
            decision="allow",
        )
        intent = self.activation.intend(view)
        return self.activation.claim(intent, view, principal_id="root"), identity

    def test_full_chain_grant_claim_create_bind_start(self) -> None:
        config = self._config()
        ticket, _identity = self._ticket_for(config)
        self.docker._allocation_id = ticket.allocation_id
        self.docker._request_id = ticket.request_id
        launcher = SandboxedLauncher(
            self.activation,
            resolve_launch_identity(config, base_dir=self.base) and
            _plan_of(config, self.base),
            sandbox_store=self.sandbox,
            docker_adapter=self.docker,
            container_labels=LABELS,
        )
        endpoint = launcher.launch(ticket)
        self.assertEqual("e" * 64, endpoint.container_id)
        # dual-ledger facts: sandbox BOUND/STARTED, mcp started
        allocation = self.sandbox.load(ticket.allocation_id)
        self.assertIn(
            allocation.state,
            (AllocationState.STARTED, AllocationState.BOUND),
        )
        self.assertEqual("started", self.activation.get_allocation(ticket.allocation_id).status)
        # shutdown terminates the container through the adapter
        import time

        endpoint.terminate_tree(deadline=time.monotonic() + 10.0)
        self.assertEqual({}, self.docker.containers)
        # ticket replay is refused (one-time)
        from koawa_agent_v2.mcp.activation import McpActivationError

        with self.assertRaises(McpActivationError):
            launcher.launch(ticket)

    def test_identity_drift_refused_before_any_create(self) -> None:
        config = self._config()
        ticket, _identity = self._ticket_for(config)
        from koawa_agent_v2.mcp.activation import (
            McpActivationError,
            resolve_launch_identity as _r,
        )

        plan = _plan_of(self._config(), self.base)
        plan_identity_digest = plan.identity.config_digest
        object.__setattr__(ticket, "launch_identity_digest", "f" * 64)
        launcher = SandboxedLauncher(
            self.activation,
            plan,
            sandbox_store=self.sandbox,
            docker_adapter=self.docker,
        )
        with self.assertRaises(McpActivationError) as raised:
            launcher.launch(ticket)
        self.assertEqual("mcp_launch_identity_mismatch", raised.exception.args[0])
        self.assertEqual([], self.docker.created_argv)
        self.assertIsNone(self.sandbox.load(ticket.allocation_id))
        del plan_identity_digest

    def test_inspect_tamper_records_failed_before_start(self) -> None:
        config = self._config()
        ticket, _identity = self._ticket_for(config)
        self.docker._allocation_id = ticket.allocation_id
        self.docker._request_id = ticket.request_id

        def tampered_inspect(docker, container_id, *, timeout):
            document = _RecordingDocker.inspect(
                self.docker, docker, container_id, timeout=timeout
            )
            document["HostConfig"]["NetworkMode"] = "bridge"
            return document

        self.docker.inspect = tampered_inspect
        launcher = SandboxedLauncher(
            self.activation,
            _plan_of(config, self.base),
            sandbox_store=self.sandbox,
            docker_adapter=self.docker,
        )
        from koawa_agent_v2.mcp.activation import McpActivationError
        from koawa_agent_v2.sandbox.runtime import SandboxError

        with self.assertRaises((McpActivationError, SandboxError)) as raised:
            launcher.launch(ticket)
        self.assertEqual(
            "mcp_container_contract_mismatch",
            getattr(raised.exception, "code", getattr(raised.exception, "args")[0]),
        )
        view = self.activation.get_allocation(ticket.allocation_id)
        self.assertEqual("failed_before_start", view.status)
        self.assertEqual({}, self.docker.containers)


def _plan_of(config: McpServerConfig, base: Path):
    from koawa_agent_v2.mcp.activation import stage_code_artifacts

    return stage_code_artifacts(
        config, base_dir=base, staging_root=base / "staging",
    )


if __name__ == "__main__":
    unittest.main()
