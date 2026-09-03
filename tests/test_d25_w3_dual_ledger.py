"""D25 W3: dual-ledger binding + crash-window reconciliation contracts."""

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
from koawa_agent_v2.mcp.sandbox_reconcile import (
    DualLedgerReconciler,
    ReconcileFacts,
    ensure_sandbox_intent,
    zero_mount_digest,
)
from koawa_agent_v2.runtime.config import (
    McpExecutionProfile,
    McpResourceLimits,
    McpServerConfig,
)
from koawa_agent_v2.sandbox.runtime import (
    AllocationState,
    SandboxAllocationStore,
    SandboxError,
)

DIGEST = "sha256:" + "a" * 64
LABELS = (("koawa.managed", "mcp-test"),)


class _FakeDocker:
    def __init__(self, *, containers=None, running=None, fail_inspect=False):
        self.containers = containers or {}
        self.running = running if running is not None else {}
        self.fail_inspect = fail_inspect
        self.stopped_removed: list[str] = []

    def list_by_labels(self, docker, labels):
        return list(self.containers)

    def inspect(self, docker, container_id, *, timeout):
        if self.fail_inspect or container_id not in self.containers:
            raise RuntimeError("inspect unavailable")
        document = self.containers[container_id]
        document.setdefault(
            "State", {"Running": self.running.get(container_id, False)}
        )
        return document

    def stop_and_remove(self, docker, container_id, *, stop_timeout_seconds, cli_timeout_seconds):
        self.stopped_removed.append(container_id)
        self.containers.pop(container_id, None)


class DualLedgerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteEventStore(Path(self._tmp.name) / "dual.sqlite3")
        self.activation = ActivationService(
            self.store, clock=lambda: datetime.now(timezone.utc),
            approval_ttl_seconds=300,
        )
        self.sandbox = SandboxAllocationStore(self.store)
        self.request_id = uuid4()

    def tearDown(self) -> None:
        close = getattr(self.store, "close", None)
        if close:
            close()
        self._tmp.cleanup()

    def _seed_claimed(self, *, image_id=DIGEST):
        """Real activation flow: plan_start -> intend -> claim."""
        config = McpServerConfig(
            server_id="svc",
            command=("/usr/local/bin/node", "server.js"),
            execution_profile=McpExecutionProfile.SANDBOXED,
            image_id=image_id,
            resource_limits=McpResourceLimits(),
            container_working_directory="/work",
        )
        identity = resolve_launch_identity(config, base_dir=Path(self._tmp.name))
        view = self.activation.plan_start(
            identity,
            principal_id="root",
            scope=process_start_scope(config.execution_profile),
            decision="allow",
        )
        intent = self.activation.intend(view)
        ticket = self.activation.claim(intent, view, principal_id="root")
        return intent.allocation_id, ticket, identity.config_digest

    def _facts(self, allocation_id, identity_digest, *, image_id=DIGEST) -> ReconcileFacts:
        return ReconcileFacts(
            allocation_id=allocation_id,
            image_id=image_id,
            launch_identity_digest=identity_digest,
            evidence_kind="fake-inspect",
            evidence_digest="e" * 64,
            reconciler_principal_id="root",
            deadline_at=clock_plus(300),
        )

    def _reconciler(self, docker: _FakeDocker) -> DualLedgerReconciler:
        return DualLedgerReconciler(
            activation=self.activation,
            sandbox_store=self.sandbox,
            docker_adapter=docker,
            docker_executable="docker",
            container_labels=LABELS,
        )


def clock_plus(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class EnsureIntentTest(DualLedgerTestBase):
    def test_correlation_uses_ticket_ids_and_zero_mount(self) -> None:
        allocation_id, ticket, identity_digest = self._seed_claimed()
        ensure_sandbox_intent(
            self.sandbox,
            ticket,
            image_id=DIGEST,
            launch_identity_digest=identity_digest,
            deadline_at=clock_plus(300),
        )
        allocation = self.sandbox.load(allocation_id)
        self.assertIsNotNone(allocation)
        self.assertEqual(ticket.request_id, allocation.owner_execution_id)
        self.assertEqual(allocation_id, allocation.allocation_id)
        self.assertEqual(zero_mount_digest(), allocation.mount_digest)

    def test_intent_drift_conflicts(self) -> None:
        _, ticket, identity_digest = self._seed_claimed()
        ensure_sandbox_intent(
            self.sandbox, ticket, image_id=DIGEST,
            launch_identity_digest=identity_digest, deadline_at=clock_plus(300),
        )
        with self.assertRaises(SandboxError) as raised:
            ensure_sandbox_intent(
                self.sandbox, ticket, image_id="sha256:" + "b" * 64,
                launch_identity_digest=identity_digest, deadline_at=clock_plus(300),
            )
        self.assertEqual("allocation_identity_conflict", raised.exception.args[0])


class ReconcileWindowsTest(DualLedgerTestBase):
    def test_window1_no_intent_no_container_fails_before_start(self) -> None:
        allocation_id, _ticket, identity_digest = self._seed_claimed()
        docker = _FakeDocker()
        outcome = self._reconciler(docker).reconcile(
            self._facts(allocation_id, identity_digest)
        )
        self.assertEqual("failed_before_start", outcome)
        view = self.activation.get_allocation(allocation_id)
        self.assertEqual("failed_before_start", view.status)

    def test_window1_unprovable_container_is_unknown(self) -> None:
        allocation_id, _ticket, identity_digest = self._seed_claimed()
        docker = _FakeDocker(containers={"f" * 64: {"Config": {"Image": DIGEST}}})
        outcome = self._reconciler(docker).reconcile(
            self._facts(allocation_id, identity_digest)
        )
        self.assertEqual("outcome_unknown", outcome)
        self.assertEqual([], docker.stopped_removed)

    def test_window2_intent_without_container_releases(self) -> None:
        allocation_id, ticket, identity_digest = self._seed_claimed()
        ensure_sandbox_intent(
            self.sandbox, ticket, image_id=DIGEST,
            launch_identity_digest=identity_digest, deadline_at=clock_plus(300),
        )
        docker = _FakeDocker()
        outcome = self._reconciler(docker).reconcile(
            self._facts(allocation_id, identity_digest)
        )
        self.assertEqual("failed_before_start", outcome)
        self.assertEqual(
            AllocationState.RELEASED, self.sandbox.load(allocation_id).state
        )
        self.assertEqual(
            "failed_before_start",
            self.activation.get_allocation(allocation_id).status,
        )

    def test_window3_create_before_bind_recovers_binding(self) -> None:
        allocation_id, ticket, identity_digest = self._seed_claimed()
        ensure_sandbox_intent(
            self.sandbox, ticket, image_id=DIGEST,
            launch_identity_digest=identity_digest, deadline_at=clock_plus(300),
        )
        container_id = "f" * 64
        docker = _FakeDocker(
            containers={container_id: {"Config": {"Image": DIGEST}}},
            running={container_id: False},
        )
        outcome = self._reconciler(docker).reconcile(
            self._facts(allocation_id, identity_digest)
        )
        self.assertEqual("failed_before_start", outcome)
        self.assertIn(container_id, docker.stopped_removed)
        self.assertEqual(
            AllocationState.RELEASED, self.sandbox.load(allocation_id).state
        )

    def test_window4_bound_exited_removed_failed_before_start(self) -> None:
        allocation_id, ticket, identity_digest = self._seed_claimed()
        ensure_sandbox_intent(
            self.sandbox, ticket, image_id=DIGEST,
            launch_identity_digest=identity_digest, deadline_at=clock_plus(300),
        )
        container_id = "f" * 64
        self.sandbox.bind(allocation_id, container_id)
        docker = _FakeDocker(
            containers={container_id: {"Config": {"Image": DIGEST}}},
            running={container_id: False},
        )
        outcome = self._reconciler(docker).reconcile(
            self._facts(allocation_id, identity_digest)
        )
        self.assertEqual("failed_before_start", outcome)
        self.assertIn(container_id, docker.stopped_removed)
        self.assertEqual(
            AllocationState.RELEASED, self.sandbox.load(allocation_id).state
        )

    def test_inspect_unavailable_is_unknown(self) -> None:
        allocation_id, ticket, identity_digest = self._seed_claimed()
        ensure_sandbox_intent(
            self.sandbox, ticket, image_id=DIGEST,
            launch_identity_digest=identity_digest, deadline_at=clock_plus(300),
        )
        container_id = "f" * 64
        self.sandbox.bind(allocation_id, container_id)
        docker = _FakeDocker(
            containers={container_id: {"Config": {"Image": DIGEST}}},
            fail_inspect=True,
        )
        outcome = self._reconciler(docker).reconcile(
            self._facts(allocation_id, identity_digest)
        )
        self.assertEqual("outcome_unknown", outcome)
        self.assertEqual([], docker.stopped_removed)

    def test_identity_divergence_is_manual_audit(self) -> None:
        allocation_id, ticket, identity_digest = self._seed_claimed()
        ensure_sandbox_intent(
            self.sandbox, ticket, image_id="sha256:" + "b" * 64,
            launch_identity_digest=identity_digest, deadline_at=clock_plus(300),
        )
        docker = _FakeDocker()
        with self.assertRaises(SandboxError) as raised:
            self._reconciler(docker).reconcile(
                self._facts(allocation_id, identity_digest)
            )
        self.assertEqual("mcp_dual_ledger_divergence", raised.exception.args[0])

    def test_concurrent_reconcile_single_winner(self) -> None:
        allocation_id, ticket, identity_digest = self._seed_claimed()
        ensure_sandbox_intent(
            self.sandbox, ticket, image_id=DIGEST,
            launch_identity_digest=identity_digest, deadline_at=clock_plus(300),
        )
        container_id = "f" * 64
        self.sandbox.bind(allocation_id, container_id)
        docker = _FakeDocker(
            containers={container_id: {"Config": {"Image": DIGEST}}},
            running={container_id: False},
        )
        reconciler = self._reconciler(docker)
        first = reconciler.reconcile(self._facts(allocation_id, identity_digest))
        self.assertEqual("failed_before_start", first)
        # The loser of the race must not double-fire the recovery: a terminal
        # sandbox allocation is rejected outright.
        with self.assertRaises(SandboxError) as raised:
            reconciler.reconcile(self._facts(allocation_id, identity_digest))
        self.assertEqual("mcp_reconcile_already_final", raised.exception.args[0])


if __name__ == "__main__":
    unittest.main()
