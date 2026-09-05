"""D25 G2: the three mandatory crash windows on real Docker (doc §W5 matrix 6).

Windows exercised end-to-end against the daemon, not fakes:
  - create-before-bind  (intent committed, container created, host crashed)
  - start-before-event  (container running, MCP started event never written)
  - stop-before-event   (container already exited, MCP stopped event missing)
"""

from __future__ import annotations

import json
import subprocess
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
from koawa_agent_v2.mcp.sandbox_reconcile import (
    DualLedgerReconciler,
    ReconcileFacts,
    ensure_sandbox_intent,
)
from koawa_agent_v2.runtime.config import (
    McpExecutionProfile,
    McpResourceLimits,
    McpServerConfig,
)
from koawa_agent_v2.sandbox.docker_primitives import (
    ContainerSpec,
    create_arguments,
)
from koawa_agent_v2.sandbox.runtime import (
    AllocationState,
    SandboxAllocationStore,
)

EVIL = __import__("os").environ.get(
    "KOAWA_D25_EVIL_IMAGE",
    "sha256:f8767f46249a2820c7933e368bae7f6b16850cbca4a08fdb256ff92bf6c97efe",
)
LABELS = (("koawa.managed", "d25-g2"),)


def _docker_ready() -> bool:
    try:
        completed = subprocess.run(
            ("docker", "info"), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


class RealCrashWindowsTest(unittest.TestCase):
    def setUp(self) -> None:
        if not _docker_ready():
            # Keep an unavailable daemon explicit in test output: this is an
            # environment block, never a passing/unknown crash-window result.
            self.skipTest("ENV_BLOCKED: docker_unavailable")
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = SqliteEventStore(self.base / "g2.sqlite3")
        self.activation = ActivationService(
            self.store, clock=lambda: datetime.now(timezone.utc),
            approval_ttl_seconds=300,
        )
        self.sandbox = SandboxAllocationStore(self.store)
        self.docker = DockerAdapter()
        self.docker_exe = Path("docker")

    def tearDown(self) -> None:
        close = getattr(self.store, "close", None)
        if close:
            close()
        self._tmp.cleanup()

    def _seed_claimed(self):
        config = McpServerConfig(
            server_id="svc",
            command=("/usr/local/bin/node", "/opt/evil.js"),
            execution_profile=McpExecutionProfile.SANDBOXED,
            image_id=EVIL,
            resource_limits=McpResourceLimits(memory_bytes=134217728, pids=32),
            container_working_directory="/",
        )
        identity = resolve_launch_identity(config, base_dir=self.base)
        view = self.activation.plan_start(
            identity,
            principal_id="root",
            scope=process_start_scope(config.execution_profile),
            decision="allow",
        )
        intent = self.activation.intend(view)
        ticket = self.activation.claim(intent, view, principal_id="root")
        ensure_sandbox_intent(
            self.sandbox, ticket, image_id=EVIL,
            launch_identity_digest=ticket.launch_identity_digest,
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        )
        spec = ContainerSpec(
            image_id=EVIL,
            argv=tuple(config.command),
            container_working_directory="/",
            environment=(),
            cpus=limits_cpu(),
            memory_bytes=134217728,
            pids_limit=32,
            tmpfs_bytes=16777216,
            container_name=f"koawa-d25-g2-{ticket.allocation_id}",
            labels=LABELS
            + (
                ("koawa.mcp.allocation", str(ticket.allocation_id)),
                ("koawa.mcp.owner", str(ticket.request_id)),
            ),
        )
        facts = ReconcileFacts(
            allocation_id=ticket.allocation_id,
            image_id=EVIL,
            launch_identity_digest=ticket.launch_identity_digest,
            evidence_kind="real-inspect",
            evidence_digest="a" * 64,
            reconciler_principal_id="root",
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        )
        reconciler = DualLedgerReconciler(
            activation=self.activation,
            sandbox_store=self.sandbox,
            docker_adapter=self.docker,
            docker_executable="docker",
            container_labels=LABELS,
        )
        return ticket, spec, facts, reconciler

    def _reconciler(self) -> DualLedgerReconciler:
        return DualLedgerReconciler(
            activation=self.activation,
            sandbox_store=self.sandbox,
            docker_adapter=self.docker,
            docker_executable="docker",
            container_labels=LABELS,
        )

    def test_create_before_bind_on_real_daemon(self) -> None:
        ticket, spec, facts, _reconciler = self._seed_claimed()
        # crash right after create: the container exists with full labels but
        # no bind/start event was ever written.
        container_id = self.docker.create(
            self.docker_exe, create_arguments(spec), timeout=30.0
        )
        try:
            outcome = self._reconciler().reconcile(facts)
            self.assertEqual("failed_before_start", outcome)
            self.assertEqual(
                AllocationState.RELEASED,
                self.sandbox.load(ticket.allocation_id).state,
            )
            probe = subprocess.run(
                ("docker", "container", "inspect", container_id),
                capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertNotEqual(0, probe.returncode)
        finally:
            subprocess.run(("docker", "rm", "-f", container_id),
                           capture_output=True, timeout=15)

    def test_start_before_event_on_real_daemon(self) -> None:
        ticket, spec, facts, _reconciler = self._seed_claimed()
        container_id = self.docker.create(
            self.docker_exe, create_arguments(spec), timeout=30.0
        )
        started = subprocess.run(("docker", "start", container_id),
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(0, started.returncode)
        try:
            outcome = self._reconciler().reconcile(facts)
            # claimed (never started on our side): the container provably ran,
            # but tool effects are ledger-bound; recovery grades UNKNOWN.
            self.assertEqual("outcome_unknown", outcome)
            self.assertEqual(
                AllocationState.RELEASED,
                self.sandbox.load(ticket.allocation_id).state,
            )
        finally:
            subprocess.run(("docker", "rm", "-f", container_id),
                           capture_output=True, timeout=15)

    def test_stop_before_event_backfills_stopped(self) -> None:
        ticket, spec, facts, _reconciler = self._seed_claimed()
        self.activation.record_started(ticket)
        container_id = self.docker.create(
            self.docker_exe, create_arguments(spec), timeout=30.0
        )
        subprocess.run(("docker", "start", container_id),
                       capture_output=True, text=True, timeout=30)
        # stop happens on the daemon before our stopped event exists
        subprocess.run(("docker", "stop", "-t", "2", container_id),
                       capture_output=True, text=True, timeout=30)
        try:
            outcome = self._reconciler().reconcile(facts)
            self.assertEqual("stopped", outcome)
            view = self.activation.get_allocation(ticket.allocation_id)
            self.assertEqual("stopped", view.status)
            self.assertEqual(
                AllocationState.RELEASED,
                self.sandbox.load(ticket.allocation_id).state,
            )
        finally:
            subprocess.run(("docker", "rm", "-f", container_id),
                           capture_output=True, timeout=15)


def limits_cpu() -> float:
    return 1.0


if __name__ == "__main__":
    unittest.main()
