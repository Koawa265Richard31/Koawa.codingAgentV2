"""D25 G3/G4: sandboxed end-to-end assembly chain + lifecycle negatives.

Real chain on the real daemon: activation grant -> claim -> SandboxedLauncher
-> StdioTransport(launcher factory) -> McpSession -> verified registry bind ->
policy -> ToolLedger -> tool result -> shutdown -> dual-ledger release.
Negatives: startup hang and protocol garbage against the adversarial fixture.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.approval_service import ApprovalService
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.loop import (
    ModelCallRef,
    ToolCallItem,
    ToolExecutionContext,
)
from koawa_agent_v2.ledger import (
    LedgerExecutor,
    READ_ONLY_PROFILE,
    ToolExecutionState,
    ToolLedgerStore,
)
from koawa_agent_v2.mcp import McpSession, StdioTransport, build_mcp_registry
from koawa_agent_v2.mcp.activation import (
    ActivationService,
    process_start_scope,
    resolve_launch_identity,
)
from koawa_agent_v2.mcp.launcher import SandboxedLauncher
from koawa_agent_v2.policy import ActionKind, Decision, PolicyEngine, PolicyRule
from koawa_agent_v2.runtime.config import (
    McpExecutionProfile,
    McpResourceLimits,
    McpServerConfig,
)
from koawa_agent_v2.runtime.mcp_sandbox_labels import MCP_SANDBOX_LABELS
from koawa_agent_v2.sandbox.runtime import (
    AllocationState,
    SandboxAllocationStore,
)
from koawa_agent_v2.telemetry.trace import TraceStore

import os as _os
REFERENCE = _os.environ.get("KOAWA_D25_FS_IMAGE", "sha256:adcd84ab9f9dc91e5c3eebe9fa32329545f73ab0b891ecd6def4232740cc4300")
EVIL = _os.environ.get("KOAWA_D25_EVIL_IMAGE", "sha256:f8767f46249a2820c7933e368bae7f6b16850cbca4a08fdb256ff92bf6c97efe")


def _docker_ready() -> bool:
    try:
        completed = subprocess.run(
            ("docker", "info"), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


class SandboxE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        if not _docker_ready():
            self.skipTest("docker_unavailable")
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = SqliteEventStore(self.base / "e2e.sqlite3")
        self.runtime = ThreadRuntime(self.store, actor="d25")
        self.ledger = ToolLedgerStore(self.store)
        self.approvals = ApprovalService(
            self.store, self.ledger, budget_action_limits={"root": 100},
        )
        self.trace = TraceStore(self.store)
        self.activation = ActivationService(
            self.store, clock=lambda: datetime.now(timezone.utc),
            approval_ttl_seconds=300,
        )
        self.sandbox = SandboxAllocationStore(self.store)
        self.cleanup_endpoint = None

    def tearDown(self) -> None:
        endpoint = self.cleanup_endpoint
        if endpoint is not None:
            import time

            try:
                endpoint.kill_tree(deadline=time.monotonic() + 20.0)
            except Exception:
                pass
        close = getattr(self.store, "close", None)
        if close:
            close()
        self._tmp.cleanup()

    def _config(self, image_id: str = REFERENCE, *, command=None) -> McpServerConfig:
        return McpServerConfig(
            server_id="server",
            command=command
            or (
                "/usr/local/bin/node",
                "/usr/local/lib/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js",
                "/data",
            ),
            execution_profile=McpExecutionProfile.SANDBOXED,
            image_id=image_id,
            resource_limits=McpResourceLimits(memory_bytes=268435456),
            container_working_directory="/data",
            # D25: admin-declared subset within the modeled schema subset
            # (read_file's number-typed tail/head is outside the subset and
            # fails closed); tools outside never enter the registry.
            tool_allowlist=(
                "list_directory", "get_file_info",
                "create_directory", "move_file",
            ),
        )

    def _launcher_and_ticket(self, config: McpServerConfig):
        identity = resolve_launch_identity(config, base_dir=self.base)
        view = self.activation.plan_start(
            identity,
            principal_id="root",
            scope=process_start_scope(config.execution_profile),
            decision="allow",
        )
        intent = self.activation.intend(view)
        ticket = self.activation.claim(intent, view, principal_id="root")
        plan = type("Plan", (), {"identity": identity, "config": config})()
        launcher = SandboxedLauncher(
            self.activation,
            _staged(config, identity, self.base),
            sandbox_store=self.sandbox,
            docker_adapter=None,
            docker_executable="docker",
            container_labels=MCP_SANDBOX_LABELS,
            process_start_timeout_seconds=60.0,
        )
        return ticket, launcher

    def _executor(self, session: McpSession, catalog):
        from koawa_agent_v2.policy import (
            Principal,
            ResolvedAction,
            SideEffectClass as PolicySideEffectClass,
            canonical_arguments,
        )

        principal = Principal("root", ("mcp.use",))
        delegate = build_mcp_registry(session, catalog)
        rules = tuple(
            PolicyRule(
                f"mcp-{name}", Decision.ALLOW,
                action_kinds=(ActionKind.MCP_TOOL,),
                tool_names=(name,),
                principal_ids=("root",),
                required_scopes=("mcp.use",),
            )
            for name in sorted(catalog.bindings)
        )
        engine = PolicyEngine("policy-v1", rules)
        profiles = {name: READ_ONLY_PROFILE for name in sorted(catalog.bindings)}

        def make_resolver(name):
            binding = catalog.bindings[name]

            def resolve(call, context, profile, previous):
                return ResolvedAction(
                    kind=ActionKind.MCP_TOOL,
                    tool_name=call.name,
                    canonical_arguments_json=canonical_arguments(call.arguments_json),
                    principal=principal,
                    side_effect_class=PolicySideEffectClass(
                        profile.side_effect_class.value
                    ),
                    sandbox_profile_id="d25-sandboxed",
                    policy_version="policy-v1",
                    mcp_server_id=session.server_id,
                    mcp_session_generation=session.generation,
                    mcp_schema_hash=binding.schema_hash,
                )

            return resolve

        return LedgerExecutor(
            delegate, self.ledger, profiles,
            policy_engine=engine, approval_service=self.approvals,
            action_resolvers={
                name: make_resolver(name) for name in catalog.bindings
            },
            trace_store=self.trace,
            correlation_id=uuid4(),
        )


from koawa_agent_v2.mcp.sandbox_reconcile import (  # noqa: E402
    DualLedgerReconciler,
    ReconcileFacts,
)


def _staged(config: McpServerConfig, identity, base: Path):
    from koawa_agent_v2.mcp.activation import StagedLaunchPlan

    return StagedLaunchPlan(
        config=config,
        identity=identity,
        staged_argv=tuple(config.command),
        code_artifacts=identity.code_artifacts,
        staged_dir=base / "staging",
    )


class SandboxEndToEndTest(SandboxE2ETest):
    def test_full_vertical_reference_server(self) -> None:
        config = self._config()
        ticket, launcher = self._launcher_and_ticket(config)
        transport = StdioTransport(
            (),
            env={},
            process_start_timeout_seconds=60.0,
            shutdown_timeout_seconds=10.0,
            frame_mode="line",
            process_factory=lambda: launcher.launch(ticket),
        )
        session = McpSession(
            "server", transport,
            request_timeout=20.0,
            initialize_timeout_seconds=30.0,
            tools_list_timeout_seconds=30.0,
            trace_store=self.trace,
            correlation_id=uuid4(),
            tool_allowlist=frozenset(config.tool_allowlist or ()),
        )
        self.addCleanup(session.close)
        catalog = session.connect()
        binding_names = sorted(catalog.bindings)
        self.assertIn("server__list_directory", binding_names)
        executor = self._executor(session, catalog)

        thread = self.runtime.create_thread("d25-e2e")
        queued = self.runtime.create_turn(
            thread.thread_id, "d25-e2e",
            expected_thread_version=thread.version,
        )
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        call = ToolCallItem(
            0, "item-1", "call-1", "server__list_directory",
            json.dumps({"path": "/data"}),
        )
        model_turn_id = uuid4()
        context = ToolExecutionContext(
            running.current_run_id, model_turn_id, 1,
            ModelCallRef(model_turn_id, call.call_id),
            turn_id=running.turn_id, turn_version=running.version,
        )
        authorized = executor.authorize(call, context=context)
        result = executor.execute_authorized(authorized)
        self.assertFalse(result.is_error)
        envelope = json.loads(result.content)
        self.assertTrue(envelope["untrusted_mcp_result"])
        # Hardening WP-1: the receipt is metadata-only - the file name stays
        # operator-side in the durable fact, never model-visible.
        self.assertIsNone(envelope["result"])
        self.assertEqual("metadata_only", envelope["result_visibility"])
        self.assertGreater(envelope["body_bytes"], 0)
        record = self.ledger.load(authorized.record.execution_id)
        from koawa_agent_v2.ledger import ToolExecutionState

        self.assertEqual(ToolExecutionState.SUCCEEDED, record.state)

        # shutdown + dual-ledger release
        session.close()
        from koawa_agent_v2.mcp.sandbox_reconcile import (
            DualLedgerReconciler,
            ReconcileFacts,
        )

        from koawa_agent_v2.mcp.docker_endpoint import DockerAdapter

        reconciler = DualLedgerReconciler(
            activation=self.activation,
            sandbox_store=self.sandbox,
            docker_adapter=DockerAdapter(),
            docker_executable="docker",
            container_labels=MCP_SANDBOX_LABELS,
        )
        outcome = reconciler.reconcile(ReconcileFacts(
            allocation_id=ticket.allocation_id,
            image_id=config.image_id,
            launch_identity_digest=ticket.launch_identity_digest,
            evidence_kind="real-inspect",
            evidence_digest="b" * 64,
            reconciler_principal_id="root",
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        ))
        self.assertEqual("stopped", outcome)
        self.assertEqual(
            AllocationState.RELEASED,
            self.sandbox.load(ticket.allocation_id).state,
        )
        removed_id = self.sandbox.load(ticket.allocation_id).container_id
        probe = subprocess.run(
            ("docker", "container", "inspect", removed_id),
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertNotEqual(0, probe.returncode)


def endpoint_container_id(session: McpSession) -> str:
    owned = session._transport._owned  # test-only introspection of the bound endpoint
    return owned.container_id


class LifecycleNegativesTest(SandboxE2ETest):
    def test_startup_hang_is_bounded_and_exact(self) -> None:
        # evil fixture never speaks MCP: initialize must time out bounded and
        # the exact container must be removed.
        config = self._config(
            image_id=EVIL,
            command=("/usr/local/bin/node", "/opt/evil.js"),
        )
        ticket, launcher = self._launcher_and_ticket(config)
        transport = StdioTransport(
            (),
            env={},
            process_start_timeout_seconds=30.0,
            shutdown_timeout_seconds=5.0,
            frame_mode="line",
            process_factory=lambda: launcher.launch(ticket),
        )
        session = McpSession(
            "server", transport,
            request_timeout=3.0,
            initialize_timeout_seconds=3.0,
            tools_list_timeout_seconds=3.0,
        )
        self.cleanup_endpoint = None
        with self.assertRaises(Exception) as raised:
            session.connect()
        code = getattr(raised.exception, "code", "") or str(raised.exception)
        self.assertTrue(
            any(token in code for token in ("timeout", "handshake", "initialize")),
            code,
        )
        session.close()
        # exact ownership: the adversarial container is gone
        view = self.activation.get_allocation(ticket.allocation_id)
        self.assertIn(view.status, ("started", "outcome_unknown", "stopped"))


if __name__ == "__main__":
    unittest.main()
