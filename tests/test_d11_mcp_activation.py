"""I6 tests: MCP activation, host/sandbox trust, binding identity, lazy plane.

Deterministic only - no sleeps, no network, no docker.  Spawns are counted
through injected spawners/launchers and the activation ledger.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.approval_service import ApprovalService
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import AgentLoop
from koawa_agent_v2.ledger import (
    LedgerExecutor,
    ToolLedgerStore,
    logical_execution_id,
)
from koawa_agent_v2.ledger.protocol import (
    RecoveryMode,
    SideEffectClass,
    ToolRecoveryProfile,
)
from koawa_agent_v2.mcp import McpSession
from koawa_agent_v2.mcp.activation import (
    ActivationService,
    McpActivationError,
    process_start_scope,
    resolve_launch_identity,
    stage_code_artifacts,
)
from koawa_agent_v2.mcp.launcher import (
    HostTrustedLauncher,
    SandboxedLauncher,
)
from koawa_agent_v2.mcp.tool_binding import (
    PhysicalSessionFence,
    bind_catalog,
)
from koawa_agent_v2.runtime.app import AppRuntime
from koawa_agent_v2.runtime.assembly import (
    ActivationPending,
    RuntimeAssemblyError,
    assemble_control_plane,
    assemble_execution_plane,
    assemble_runtime,
    preflight_execution_activation,
)
from koawa_agent_v2.runtime.config import (
    McpExecutionProfile,
    McpResourceLimits,
    McpServerConfig,
    PolicyConfig,
    ProviderConfig,
    RepositoryTrustMode,
    RuntimeConfig,
    RuntimeConfigError,
    SandboxConfig,
    SandboxRunner,
    TestProfileConfig,
    load_runtime_config,
)
from koawa_agent_v2.telemetry.faults import FaultPoint
from koawa_agent_v2.telemetry.trace import TraceStore


def _fixture_command() -> tuple[str, ...]:
    from koawa_agent_v2.mcp import spawn_fixture_command

    return tuple(spawn_fixture_command())


def _base_config(
    repo: Path,
    *,
    mcp_servers: tuple[McpServerConfig, ...] = (),
    policy: PolicyConfig | None = None,
) -> RuntimeConfig:
    return RuntimeConfig(
        repo=repo,
        db=repo.parent / "state.sqlite3",
        provider=ProviderConfig(
            base_url="http://127.0.0.1:1/v1",
            api_key_env="I6_TEST_KEY",
            model="test-model",
        ),
        sandbox=SandboxConfig(
            runner=SandboxRunner.HOST,
            host_trust=RepositoryTrustMode.BUILTIN_FIXTURE,
        ),
        test_profiles=(
            TestProfileConfig(
                "unit",
                (str(Path(sys.executable).resolve()), "-B"),
                timeout_seconds=30,
            ),
        ),
        policy=policy or PolicyConfig(
            principal_scopes=(
                *PolicyConfig().principal_scopes,
                "mcp.host_process.execute",
            ),
        ),
        system_prompt="I6 fixture.",
        mcp_servers=mcp_servers,
    )


def _host_trusted_server(
    *,
    server_id: str = "trusted",
    command: tuple[str, ...] | None = None,
    extra_env: tuple[tuple[str, str], ...] = (),
) -> McpServerConfig:
    return McpServerConfig(
        server_id=server_id,
        command=command or _fixture_command(),
        execution_profile=McpExecutionProfile.HOST_TRUSTED,
        resource_limits=McpResourceLimits(),
        environment=extra_env,
    )


class _FixedClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _git_init(root: Path) -> None:
    import shutil
    import subprocess

    executable = shutil.which("git")
    if executable is None:
        raise unittest.SkipTest("git is not installed")
    subprocess.run(
        (executable, "-C", str(root), "init", "-q"),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        (executable, "-C", str(root), "config", "user.email", "i6@e"),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        (executable, "-C", str(root), "config", "user.name", "I6"),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        (executable, "-C", str(root), "add", "--all"),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        (executable, "-C", str(root), "commit", "-qm", "baseline"),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )



class V3ConfigTest(unittest.TestCase):
    # §8.3: v3 JSON requires explicit profiles; legacy stays compatible.

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "repo").mkdir()
        _git_init(self.root / "repo")

    def _write(self, document: dict) -> Path:
        path = self.root / "config.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def _base(self) -> dict:
        return {
            "config_schema_version": 3,
            "repo": "repo",
            "db": "agent.sqlite3",
            "provider": {
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key_env": "SF_TEST_KEY",
                "model": "m",
            },
            "sandbox": {"runner": "host", "host_trust": "builtin_fixture"},
            "policy": {"patch_decision": "allow"},
            "test_profiles": [
                {"profile_id": "unit", "argv": ["/usr/local/bin/python", "-B"]},
            ],
        }

    def test_v3_mcp_server_requires_execution_profile(self) -> None:
        document = self._base()
        document["mcp_servers"] = [
            {"server_id": "legacy", "command": ["/usr/bin/tool", "--serve"]},
        ]
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(self._write(document))
        self.assertEqual("mcp_profile_migration_required", raised.exception.code)

    def test_v3_sandboxed_requires_image(self) -> None:
        document = self._base()
        document["mcp_servers"] = [
            {
                "server_id": "sand",
                "command": ["/usr/bin/tool"],
                "execution_profile": "sandboxed",
            },
        ]
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(self._write(document))
        self.assertEqual("mcp_sandbox_requires_image", raised.exception.code)

    def test_v3_host_trusted_rejects_image_and_parses_fields(self) -> None:
        document = self._base()
        document["mcp_servers"] = [
            {
                "server_id": "trusted",
                "command": ["/usr/bin/tool", "run"],
                "execution_profile": "host_trusted",
                "image_id": "sha256:aa",
            },
        ]
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(self._write(document))
        self.assertEqual("mcp_host_trusted_no_image", raised.exception.code)
        document["mcp_servers"][0].pop("image_id")
        document["mcp_servers"][0]["resource_limits"] = {"cpus": 0.5, "pids": 4}
        document["mcp_servers"][0]["code_artifacts"] = [
            {"role": "interpreter_script", "argv_index": 1},
        ]
        document["mcp_servers"][0]["read_only_mounts"] = [
            ["/etc/hosts", "/readonly/hosts"],
        ]
        loaded = load_runtime_config(self._write(document))
        server = loaded.mcp_servers[0]
        self.assertIs(McpExecutionProfile.HOST_TRUSTED, server.execution_profile)
        self.assertEqual(0.5, server.resource_limits.cpus)
        self.assertEqual(4, server.resource_limits.pids)
        self.assertEqual(
            ("interpreter_script", 1),
            (server.code_artifacts[0].role, server.code_artifacts[0].argv_index),
        )

    def test_legacy_json_without_profile_loads(self) -> None:
        document = self._base()
        del document["config_schema_version"]
        document["mcp_servers"] = [
            {"server_id": "old", "command": ["/usr/bin/tool"]},
        ]
        loaded = load_runtime_config(self._write(document))
        self.assertIsNone(loaded.mcp_servers[0].execution_profile)

    def test_duplicate_env_and_injection_env_rejected(self) -> None:
        document = self._base()
        document["mcp_servers"] = [
            {
                "server_id": "dup",
                "command": ["/usr/bin/tool"],
                "execution_profile": "host_trusted",
                "environment": [["FOO", "1"], ["foo", "2"]],
                "resource_limits": {"cpus": 1.0},
            },
        ]
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(self._write(document))
        self.assertEqual("duplicate_mcp_environment", raised.exception.code)
        document["mcp_servers"][0]["environment"] = [
            ["NODE_OPTIONS", "--max-old-space"],
        ]
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(self._write(document))
        self.assertEqual("mcp_injection_environment_forbidden", raised.exception.code)


class _CountingSpawner:
    # Records every SpawnSpec; returns a stub OwnedProcess.

    def __init__(self) -> None:
        self.specs: list = []
        self.killed: list[int] = []
        self._pid_counter = 1000

    def spawn(self, spec, *, deadline: float):
        self.specs.append(spec)
        self._pid_counter += 1
        return _StubProcess(self._pid_counter, self.killed)

    @property
    def count(self) -> int:
        return len(self.specs)

    @property
    def last_env(self):
        return self.specs[-1].env if self.specs else None

    @property
    def last_command(self):
        return self.specs[-1].command if self.specs else None


class _RealCountingSpawner:
    def __init__(self) -> None:
        from koawa_agent_v2.mcp.transport import SystemProcessSpawner

        self._delegate = SystemProcessSpawner()
        self.specs: list = []
        self.command_override: tuple[str, ...] | None = None

    def spawn(self, spec, *, deadline: float):
        effective = spec
        if self.command_override is not None:
            effective = type(spec)(self.command_override, spec.env, spec.cwd)
        self.specs.append(effective)
        return self._delegate.spawn(effective, deadline=deadline)

    @property
    def count(self) -> int:
        return len(self.specs)


class _StubProcess:
    def __init__(self, pid: int, killed: list[int]) -> None:
        self._pid = pid
        self._killed = killed
        self._exited = False

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def stdin(self):
        return None

    @property
    def stdout(self):
        return None

    @property
    def stderr(self):
        return None

    def poll(self) -> int | None:
        return 1 if self._exited else None

    def terminate_tree(self, *, deadline: float) -> None:
        self._exited = True

    def kill_tree(self, *, deadline: float) -> None:
        if not self._exited:
            self._killed.append(self._pid)
        self._exited = True

    def wait(self, *, deadline: float) -> int:
        self._exited = True
        return 0

    def close_handles(self) -> None:
        return None


class _PassEnforcer:
    def enforce(self, process, limits):
        return process


class _RefusingEnforcer:
    # Refuses BEFORE any OS create (precheck contract).

    def precheck(self, limits=None) -> None:
        raise McpActivationError("mcp_host_limits_unsupported")

    def enforce(self, process, limits):
        raise McpActivationError("mcp_host_limits_unsupported")


class ActivationServiceTest(unittest.TestCase):
    # §8.5: durable grants, atomic claim, zero-spawn before approval.

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.store = SqliteEventStore(self.base / "activation.sqlite3")
        self.clock = _FixedClock()
        self.service = ActivationService(
            self.store, clock=self.clock, approval_ttl_seconds=300,
        )

    def _identity(self, server: McpServerConfig) -> object:
        return resolve_launch_identity(server, base_dir=self.base)

    def _granted(self, server: McpServerConfig, decision: str = "ask"):
        identity = self._identity(server)
        scope = process_start_scope(server.execution_profile)
        view = self.service.plan_start(
            identity,
            principal_id="root",
            scope=scope,
            decision=decision,
        )
        return identity, view

    def _event_types(self) -> set[str]:
        cursor = 0
        types: set[str] = set()
        while True:
            page = self.store.read_all(after_position=cursor, limit=500)
            types.update(event.event_type for event in page)
            if len(page) < 500:
                return types
            cursor = page[-1].global_position

    def test_host_trusted_requires_identity_bound_durable_approval_before_spawn(self) -> None:
        server = _host_trusted_server()
        identity, view = self._granted(server, decision="ask")
        self.assertEqual(ActivationService.STATUS_REQUESTED, view.status)
        # Only the request event exists before any approval.
        self.assertEqual({"mcp.activation-requested.v1"}, self._event_types())
        pending = self.service.pending_requests()
        self.assertEqual(1, len(pending))
        self.assertEqual(view.request_id, pending[0].request_id)
        granted = self.service.resolve_activation(
            view.request_id, True, expected_version=view.version,
            approver_principal_id="operator",
        )
        self.assertEqual(ActivationService.STATUS_GRANTED, granted.status)
        # Re-entering the same launch reuses the grant.
        again, _again_view = self._granted(server, decision="ask")
        self.assertEqual(identity.config_digest, again.config_digest)
        self.assertEqual(0, len(self.service.pending_requests()))

    def test_denied_mcp_start_has_zero_process_calls(self) -> None:
        server = _host_trusted_server()
        _identity, view = self._granted(server, decision="ask")
        denied = self.service.resolve_activation(
            view.request_id, False, expected_version=view.version,
            approver_principal_id="operator",
        )
        self.assertEqual(ActivationService.STATUS_DENIED, denied.status)
        with self.assertRaises(McpActivationError) as raised:
            self._granted(server, decision="ask")
        self.assertEqual("mcp_process_denied", raised.exception.code)
        self.assertEqual(0, len(self.service.pending_requests()))
        self.assertFalse(any("mcp.process" in kind for kind in self._event_types()))

    def test_delayed_old_decision_cannot_approve_new_request_generation(self) -> None:
        server = _host_trusted_server()
        identity, requested = self._granted(server, decision="ask")
        self.service.resolve_activation(
            requested.request_id, True, expected_version=requested.version,
            approver_principal_id="operator",
        )
        self.clock.advance(301)
        renewed = self.service.plan_start(
            identity, principal_id="root", scope="mcp.host_process.execute",
            decision="ask",
        )
        self.assertEqual(ActivationService.STATUS_REQUESTED, renewed.status)
        with self.assertRaises(McpActivationError) as caught:
            self.service.resolve_activation(
                requested.request_id, True,
                expected_version=requested.version,
                approver_principal_id="operator",
            )
        self.assertEqual("activation_version_conflict", caught.exception.code)
        self.assertEqual(renewed, self.service.get_activation(requested.request_id))

    def test_competing_identical_decision_returns_committed_receipt(self) -> None:
        server = _host_trusted_server()
        _, requested = self._granted(server, decision="ask")
        competing = ActivationService(self.store, clock=self.clock)

        class CompetingGrant:
            fired = False

            def hit(inner, point, _facts):
                if point is FaultPoint.S4_ACTIVATION_BEFORE_GRANT_APPEND and not inner.fired:
                    inner.fired = True
                    competing.resolve_activation(
                        requested.request_id, True,
                        expected_version=requested.version,
                        approver_principal_id="operator",
                    )

        service = ActivationService(
            self.store, clock=self.clock, fault_port=CompetingGrant(),
        )
        granted = service.resolve_activation(
            requested.request_id, True, expected_version=requested.version,
            approver_principal_id="operator",
        )
        self.assertEqual(ActivationService.STATUS_GRANTED, granted.status)
        self.assertEqual(1, sum(
            event.event_type == "mcp.activation-granted.v1"
            for event in self.store.read_all()
        ))

    def test_competing_different_decision_is_stable_version_conflict(self) -> None:
        server = _host_trusted_server()
        _, requested = self._granted(server, decision="ask")
        competing = ActivationService(self.store, clock=self.clock)

        class CompetingDenial:
            fired = False

            def hit(inner, point, _facts):
                if point is FaultPoint.S4_ACTIVATION_BEFORE_GRANT_APPEND and not inner.fired:
                    inner.fired = True
                    competing.resolve_activation(
                        requested.request_id, False,
                        expected_version=requested.version,
                        approver_principal_id="operator",
                    )

        service = ActivationService(
            self.store, clock=self.clock, fault_port=CompetingDenial(),
        )
        with self.assertRaises(McpActivationError) as caught:
            service.resolve_activation(
                requested.request_id, True,
                expected_version=requested.version,
                approver_principal_id="operator",
            )
        self.assertEqual("activation_version_conflict", caught.exception.code)
        self.assertEqual(
            ActivationService.STATUS_DENIED,
            service.get_activation(requested.request_id).status,
        )

    def test_operator_grant_replay_cannot_reinstate_revoked_authorization(self) -> None:
        _, requested = self._granted(_host_trusted_server())
        granted = self.service.resolve_activation(
            requested.request_id, True, expected_version=requested.version,
            approver_principal_id="operator",
        )
        self.service._append_activation(granted.request_id, "mcp.activation-revoked.v1", {
            "request_id": str(granted.request_id), "server_id": granted.server_id,
            "reason": "fixture_revoked",
        }, granted.version)
        before = self.store.read_all()
        with self.assertRaises(McpActivationError) as caught:
            self.service.resolve_activation(
                granted.request_id, True, expected_version=requested.version,
                approver_principal_id="operator",
            )
        self.assertEqual("activation_version_conflict", caught.exception.code)
        self.assertEqual(before, self.store.read_all())
        self.assertFalse(self.service._tickets)

    def test_non_boolean_approval_cannot_write_or_replay_a_grant(self) -> None:
        _, requested = self._granted(_host_trusted_server())
        before = self.store.read_all()
        for decision in (1, 0, "true", None):
            with self.subTest(decision=decision), self.assertRaises(TypeError):
                self.service.resolve_activation(
                    requested.request_id, decision,
                    expected_version=requested.version,
                    approver_principal_id="operator",
                )
            self.assertEqual(before, self.store.read_all())

    def test_replaced_host_trusted_executable_invalidates_approval_before_spawn(self) -> None:
        script = self.base / "mcp-exec.cmd"
        script.write_text("echo first\n", encoding="utf-8")
        server = _host_trusted_server(command=(str(script),))
        identity, view = self._granted(server, decision="ask")
        self.service.resolve_activation(
            view.request_id, True, expected_version=view.version,
            approver_principal_id="operator",
        )
        first_digest = identity.config_digest
        script.write_text("echo replaced-content-now\n", encoding="utf-8")
        drifted, view_after = self._granted(server, decision="ask")
        self.assertNotEqual(first_digest, drifted.config_digest)
        self.assertEqual(ActivationService.STATUS_REQUESTED, view_after.status)
        self.assertFalse(any("mcp.process" in kind for kind in self._event_types()))

    def test_restart_with_mcp_config_or_argv_drift_requires_new_approval(self) -> None:
        server = _host_trusted_server(extra_env=(("TOKEN", "abc"),))
        identity, view = self._granted(server, decision="ask")
        self.service.resolve_activation(
            view.request_id, True, expected_version=view.version,
            approver_principal_id="operator",
        )
        first = identity.config_digest
        drifted = McpServerConfig(
            server_id=server.server_id,
            command=server.command + ("--flag",),
            execution_profile=McpExecutionProfile.HOST_TRUSTED,
            resource_limits=McpResourceLimits(),
            environment=server.environment,
        )
        drifted_identity, view_after = self._granted(drifted, decision="ask")
        self.assertNotEqual(first, drifted_identity.config_digest)
        self.assertEqual(ActivationService.STATUS_REQUESTED, view_after.status)

    def test_allocation_event_wire(self) -> None:
        server = _host_trusted_server()
        _identity, view = self._granted(server, decision="ask")
        granted = self.service.resolve_activation(
            view.request_id, True, expected_version=view.version,
            approver_principal_id="operator",
        )
        intent = self.service.intend(granted)
        ticket = self.service.claim(
            intent, granted, principal_id="root",
        )
        self.service.consume_ticket(ticket)
        self.service.record_started(ticket)
        self.service.record_ready(ticket)
        self.service.record_stopped(ticket)
        types = self._event_types()
        for expected in (
            "mcp.process-intended.v1",
            "mcp.process-claimed.v1",
            "mcp.process-started.v1",
            "mcp.process-ready.v1",
            "mcp.process-stopped.v1",
        ):
            self.assertIn(expected, types)
        # Payloads never expose env values or argv bodies.
        for event in self.store.read_all(after_position=0, limit=5000):
            raw = json.dumps(dict(event.payload), sort_keys=True)
            self.assertNotIn("TOKEN", raw)
            self.assertNotIn(_fixture_command()[-1], raw)

    def test_allocation_intent_response_loss_replays_same_allocation(self) -> None:
        _, requested = self._granted(_host_trusted_server(), decision="ask")
        granted = self.service.resolve_activation(
            requested.request_id, True, expected_version=requested.version,
            approver_principal_id="operator",
        )
        first = self.service.intend(granted)
        second = self.service.intend(granted)
        self.assertEqual(first, second)
        self.assertEqual(1, sum(
            event.event_type == "mcp.process-intended.v1"
            for event in self.store.read_all()
        ))

    def test_claimed_allocation_requires_evidence_bound_reconciliation(self) -> None:
        _, requested = self._granted(_host_trusted_server(), decision="ask")
        granted = self.service.resolve_activation(
            requested.request_id, True, expected_version=requested.version,
            approver_principal_id="operator",
        )
        intent = self.service.intend(granted)
        self.service.claim(intent, granted, principal_id="root")
        claimed = self.service.get_allocation(intent.allocation_id)
        self.assertEqual(ActivationService.ALLOCATION_CLAIMED, claimed.status)
        with self.assertRaises(McpActivationError) as caught:
            self.service.intend(granted)
        self.assertEqual(
            "mcp_allocation_reconciliation_required", caught.exception.code,
        )
        evidence = "e" * 64
        failed = self.service.reconcile_allocation(
            intent.allocation_id, expected_version=claimed.version,
            outcome="failed_before_start", evidence_kind="process_absent",
            evidence_digest=evidence, reconciler_principal_id="operator",
        )
        self.assertEqual(
            ActivationService.ALLOCATION_FAILED_BEFORE_START, failed.status,
        )
        replay = self.service.reconcile_allocation(
            intent.allocation_id, expected_version=claimed.version,
            outcome="failed_before_start", evidence_kind="process_absent",
            evidence_digest=evidence, reconciler_principal_id="operator",
        )
        self.assertEqual(failed, replay)
        next_intent = self.service.intend(granted)
        self.assertEqual(1, next_intent.attempt)
        self.assertNotEqual(intent.allocation_id, next_intent.allocation_id)

    def test_claim_after_revoke_has_zero_writes(self) -> None:
        server = _host_trusted_server()
        _identity, view = self._granted(server, decision="ask")
        granted = self.service.resolve_activation(
            view.request_id, True, expected_version=view.version,
            approver_principal_id="operator",
        )
        intent = self.service.intend(granted)
        # Revoke between intend and claim: the claim precondition must fail
        # and the allocation stream must stay empty (zero writes).
        self.service._append_activation(
            view.request_id,
            "mcp.activation-revoked.v1",
            {
                "request_id": str(view.request_id),
                "server_id": view.server_id,
                "launch_identity_digest": view.launch_identity_digest,
                "execution_profile": view.execution_profile,
                "principal_id": view.principal_id,
                "scope": view.scope,
                "reason": "operator_revoked",
            },
            granted.version,
        )
        with self.assertRaises(McpActivationError) as raised:
            self.service.claim(intent, granted, principal_id="root")
        self.assertEqual("mcp_claim_rejected", raised.exception.code)
        self.assertFalse(any("mcp.process-claimed" in kind for kind in self._event_types()))

    def test_ticket_replay_and_expiry_yield_zero_os_creates(self) -> None:
        server = _host_trusted_server()
        _identity, view = self._granted(server, decision="ask")
        granted = self.service.resolve_activation(
            view.request_id, True, expected_version=view.version,
            approver_principal_id="operator",
        )
        intent = self.service.intend(granted)
        ticket = self.service.claim(intent, granted, principal_id="root")
        self.service.consume_ticket(ticket)
        with self.assertRaises(McpActivationError) as raised:
            self.service.consume_ticket(ticket)
        self.assertEqual("mcp_ticket_replayed_or_unknown", raised.exception.code)
        self.service.record_failed_before_start(
            ticket, reason="fixture_no_external_create",
        )
        ticket_two = self.service.claim(
            self.service.intend(granted), granted, principal_id="root",
        )
        self.clock.advance(301)
        with self.assertRaises(McpActivationError) as raised:
            self.service.consume_ticket(ticket_two)
        self.assertEqual("mcp_ticket_expired", raised.exception.code)


class LauncherTest(unittest.TestCase):
    # §8.6: staged argv, minimal env, limit enforcement or explicit fail.

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.store = SqliteEventStore(self.base / "launcher.sqlite3")
        self.clock = _FixedClock()
        self.service = ActivationService(
            self.store, clock=self.clock, approval_ttl_seconds=300,
        )
        self.staging = self.base / "staging"
        self.spawner = _CountingSpawner()

    def _ticket(self, server: McpServerConfig) -> object:
        plan = stage_code_artifacts(
            server, base_dir=self.base, staging_root=self.staging,
        )
        scope = process_start_scope(server.execution_profile)
        view = self.service.plan_start(
            plan.identity,
            principal_id="root",
            scope=scope,
            decision="ask",
        )
        granted = self.service.resolve_activation(
            view.request_id, True, expected_version=view.version,
            approver_principal_id="operator",
        )
        intent = self.service.intend(granted)
        ticket = self.service.claim(
            intent, granted, principal_id="root",
        )
        return plan, ticket

    def test_host_trusted_launcher_stages_argv_and_minimal_env(self) -> None:
        server = _host_trusted_server(
            extra_env=(("KOAWA_CANARY", "secret-value"),),
        )
        plan, ticket = self._ticket(server)
        launcher = HostTrustedLauncher(
            self.service,
            plan,
            staging_root=self.staging,
            spawner=self.spawner,
            limit_enforcer=_PassEnforcer(),
        )
        endpoint = launcher.launch(ticket)
        self.assertEqual(1, self.spawner.count)
        command = self.spawner.last_command
        # The real argv was rewritten to the staged executable path.
        self.assertTrue(str(self.staging) in command[0])
        env = self.spawner.last_env
        self.assertEqual("secret-value", env["KOAWA_CANARY"])
        self.assertIn("PYTHONHASHSEED", env)
        self.assertIn("PYTHONUTF8", env)
        self.assertIsNotNone(endpoint.pid)

    def test_host_limits_unsupported_rejects_before_spawn(self) -> None:
        server = _host_trusted_server()
        plan, ticket = self._ticket(server)
        launcher = HostTrustedLauncher(
            self.service,
            plan,
            staging_root=self.staging,
            spawner=self.spawner,
            limit_enforcer=_RefusingEnforcer(),
        )
        with self.assertRaises(McpActivationError) as raised:
            launcher.launch(ticket)
        self.assertEqual("mcp_host_limits_unsupported", raised.exception.code)
        self.assertEqual(0, self.spawner.count)
        endpoint_missing = True
        self.assertTrue(endpoint_missing)

    def test_sandboxed_profile_fails_explicitly_without_container(self) -> None:
        server = McpServerConfig(
            server_id="sandboxed",
            command=_fixture_command(),
            execution_profile=McpExecutionProfile.SANDBOXED,
            image_id="sha256:" + "a" * 64,
        )
        plan = stage_code_artifacts(
            server, base_dir=self.base, staging_root=self.staging,
        )
        scope = process_start_scope(server.execution_profile)
        view = self.service.plan_start(
            plan.identity,
            principal_id="root",
            scope=scope,
            decision="allow",
        )
        intent = self.service.intend(view)
        ticket = self.service.claim(
            intent, view, principal_id="root",
        )
        launcher = SandboxedLauncher(self.service, plan)
        with self.assertRaises(McpActivationError) as raised:
            launcher.launch(ticket)
        self.assertEqual("mcp_sandbox_unavailable", raised.exception.code)
        self.assertEqual(0, self.spawner.count)


class IdentityAndSnapshotTest(unittest.TestCase):
    # §8.8: semantic binding stability + per-round catalog snapshot.

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_semantic_binding_stable_across_sessions_fence_differs(self) -> None:
        tools = [
            {
                "name": "echo",
                "description": "d",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        ]
        launch_digest = "a" * 64
        first = bind_catalog(
            "server", 1, tools, launch_identity_digest=launch_digest,
        )
        second = bind_catalog(
            "server", 1, tools, launch_identity_digest=launch_digest,
        )
        sem_first = first.semantic_bindings["server__echo"]
        sem_second = second.semantic_bindings["server__echo"]
        self.assertEqual(
            sem_first.binding_digest, sem_second.binding_digest,
        )
        self.assertEqual(
            sem_first.catalog_epoch_id, sem_second.catalog_epoch_id,
        )
        self.assertEqual(first.catalog_digest, second.catalog_digest)
        # A different launch identity yields a different semantic binding.
        other = bind_catalog(
            "server", 1, tools, launch_identity_digest="b" * 64,
        )
        self.assertNotEqual(
            sem_first.binding_digest,
            other.semantic_bindings["server__echo"].binding_digest,
        )
        fence_a = PhysicalSessionFence(uuid4(), 0, 1)
        fence_b = PhysicalSessionFence(uuid4(), 0, 1)
        self.assertNotEqual(fence_a.session_instance_id, fence_b.session_instance_id)

    def test_loop_snapshot_round_is_frozen_and_used_for_definitions(self) -> None:
        from koawa_agent_v2.runtime.composite_registry import ToolCatalogSnapshot
        from koawa_agent_v2.model.protocol import ToolCallItem, ToolDefinition, ModelRequest, FinishReason, AssistantTextItem, ModelTurn
        from koawa_agent_v2.execution.loop import AgentLoop, AgentLoopLimits
        from koawa_agent_v2.model.stream import StreamLimits
        from uuid import uuid4

        class _SnapshotExecutor:
            definitions_cache = ()

            def __init__(self, snapshot: ToolCatalogSnapshot) -> None:
                self.snapshot = snapshot

            def catalog_snapshot(self):
                return self.snapshot

            def definitions(self):
                return self.snapshot.definitions

            def execute(self, call, *, context):
                return None

        class _CaptureModel:
            def __init__(self) -> None:
                self.requests: list[object] = []

            def stream(self, request):
                self.requests.append(request)
                turn = ModelTurn(
                    request.model_turn_id,
                    request.provider,
                    request.model,
                    "r1",
                    (AssistantTextItem(0, "i", "done"),),
                    FinishReason.STOP,
                )
                yield from _text_stream(request, "done")

        snapshot = ToolCatalogSnapshot(
            catalog_epoch_id=uuid4(),
            definitions=(),
            profiles={},
            resolvers={},
            semantic_bindings={},
            delegates=(),
        )
        model = _CaptureModel()
        loop = AgentLoop(
            model, tool_executor=_SnapshotExecutor(snapshot), limits=AgentLoopLimits(),
        )
        loop.run(
            run_id=uuid4(),
            turn_id=uuid4(),
            turn_version=0,
            input_items=(),
            provider="p",
            model="m",
            max_output_tokens=8,
        )
        self.assertIsNotNone(loop.last_snapshot)
        self.assertIs(loop.last_snapshot, snapshot)
        for request in model.requests:
            self.assertIs(request.tool_definitions, snapshot.definitions)

    def test_ledger_load_for_call_uses_exact_binding_key(self) -> None:
        from uuid import uuid4

        store = SqliteEventStore(self.base / "ledger.sqlite3")
        ledger = ToolLedgerStore(store)
        thread = ThreadRuntime(store, actor="i6").create_thread("t")
        turn = ThreadRuntime(store, actor="i6").create_turn(
            thread.thread_id, "x", expected_thread_version=thread.version,
        )
        running = ThreadRuntime(store, actor="i6").start_turn(
            turn.turn_id, turn.version,
        )
        model_turn_id = uuid4()
        record = ledger.prepare(
            turn_id=running.turn_id,
            turn_version=running.version,
            run_id=running.current_run_id,
            model_turn_id=model_turn_id,
            call_id="call-1",
            tool_name="server__echo",
            arguments_json=json.dumps({"value": "x"}),
            profile=ToolRecoveryProfile(
                SideEffectClass.READ_ONLY, RecoveryMode.RETRY,
            ),
            binding_digest="c" * 64,
        )
        self.assertIsNotNone(ledger.load_for_call(
            running.turn_id, model_turn_id, "call-1", binding_digest="c" * 64,
        ))
        self.assertIsNone(ledger.load_for_call(
            running.turn_id, model_turn_id, "call-1",
        ))


class AppRuntimeActivationFlowTest(unittest.TestCase):
    # §8.5/§8.9: lazy execution plane; host_trusted ASK before any Turn.

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        _git_init(repo)
        self.repo = repo
        self.spawner = _RealCountingSpawner()

    def _launcher_builder(self):
        def builder(activation, server_config, plan):
            if server_config.execution_profile is McpExecutionProfile.SANDBOXED:
                return SandboxedLauncher(activation, plan)
            # This test exercises lazy activation/assembly, while the separate
            # LauncherTest owns staged argv assertions.  Use the repository's
            # real fixture interpreter here so Windows does not try to boot a
            # copied interpreter without its adjacent runtime DLLs.
            self.spawner.command_override = server_config.command
            return HostTrustedLauncher(
                activation,
                plan,
                staging_root=plan.staged_dir,
                spawner=self.spawner,
                limit_enforcer=_PassEnforcer(),
            )

        return builder

    def _server(self) -> McpServerConfig:
        return _host_trusted_server()

    def test_control_commands_do_not_start_execution_plane(self) -> None:
        config = _base_config(self.repo, mcp_servers=(self._server(),))
        app = AppRuntime(config, model_client=_TextModel(), launcher_builder=self._launcher_builder())
        self.assertIsNone(app._execution_plane)
        status = app.status()
        self.assertTrue(status.ok)
        self.assertIsNone(app._execution_plane)
        approvals = app.pending_approvals()
        self.assertTrue(approvals.ok)
        doctor = app.doctor()
        # doctor runs from the control plane; result may be False when the
        # configured provider key env var is absent - it still must not
        # create the execution plane.
        self.assertIsNone(app._execution_plane)
        self.assertEqual(0, self.spawner.count)
        self.assertFalse(app.assembled.store.read_all(after_position=0, limit=5000))
        app.close()

    def test_run_pending_then_approve_then_run_spawns_once(self) -> None:
        config = _base_config(self.repo, mcp_servers=(self._server(),))
        app = AppRuntime(
            config,
            model_client=_TextModel(),
            launcher_builder=self._launcher_builder(),
        )
        outcome = app.run("do it")
        self.assertFalse(outcome.ok)
        self.assertEqual("mcp_process_activation_pending", outcome.code)
        self.assertIn("requests", outcome.payload)
        # Activation is durable, but no Thread/Turn/Run is created for the pending run.
        event_types = {
            event.event_type
            for event in app.assembled.store.read_all(after_position=0, limit=5000)
        }
        self.assertFalse(
            any(kind.startswith(("thread.", "turn.", "run.")) for kind in event_types)
        )
        pending = app.assembled.activation.pending_requests()
        self.assertEqual(1, len(pending))
        approved = app.resolve_approval(
            pending[0].request_id_str, True,
            expected_version=pending[0].version,
        )
        self.assertTrue(approved.ok)
        granted = app.assembled.activation.pending_requests()
        self.assertEqual(0, len(granted))
        # Same semantic command now runs with exactly one spawned server.
        outcome = app.run("do it")
        self.assertNotEqual("mcp_process_activation_pending", outcome.code)
        self.assertIsNotNone(app._execution_plane)
        self.assertEqual(1, self.spawner.count)
        event_types = {
            event.event_type
            for event in app.assembled.store.read_all(after_position=0, limit=5000)
        }
        self.assertIn("mcp.process-started.v1", event_types)
        self.assertIn("mcp.process-ready.v1", event_types)
        app.close()

    def test_replaced_executable_invalidates_approval_before_spawn(self) -> None:
        script = self.root / "mcp-exec.cmd"
        script.write_text("echo first\n", encoding="utf-8")
        server = _host_trusted_server(command=(str(script),))
        config = _base_config(self.repo, mcp_servers=(server,))
        app = AppRuntime(
            config,
            model_client=_TextModel(),
            launcher_builder=self._launcher_builder(),
        )
        self.assertEqual("mcp_process_activation_pending", app.run("x").code)
        pending = app.assembled.activation.pending_requests()
        app.resolve_approval(
            pending[0].request_id_str, approved=True,
            expected_version=pending[0].version,
        )
        script.write_text("echo replaced-content-now\n", encoding="utf-8")
        again = app.run("x")
        self.assertFalse(again.ok)
        self.assertEqual("mcp_process_activation_pending", again.code)
        self.assertEqual(0, self.spawner.count)
        app.close()

    def test_assembly_with_mcp_reaches_assembled_runtime_and_closes(self) -> None:
        # E-stage gap evidence: with a legacy server, assemble_runtime reaches
        # an AssembledRuntime (CompositeToolRegistry now implements assert_complete)
        # and close() fully tears the fixture process down.
        legacy = McpServerConfig(
            server_id="legacy",
            command=_fixture_command(),
        )
        config = _base_config(self.repo, mcp_servers=(legacy,))
        assembled = assemble_runtime(config, model_client=_TextModel())
        self.assertEqual(1, len(assembled.mcp_sessions))
        session = assembled.mcp_sessions[0][1]
        self.assertEqual(McpSession.READY, session.state)
        self.assertIsNotNone(assembled.loop)
        assembled.close()
        assembled.close()
        self.assertIn("closed", (session.state,))


# ---- helpers and imports used by the test classes ----------------------


def _text_stream(request, text: str):
    from koawa_agent_v2.model.protocol import (
        AssistantTextItem,
        FinishReason,
        ItemCompleted,
        ItemStarted,
        ModelTurn,
        OutputKind,
        StreamHeader,
        TurnCompleted,
        TurnStarted,
    )

    def header(sequence: int) -> StreamHeader:
        return StreamHeader(
            request.model_turn_id, request.provider, "r1", sequence, sequence,
        )
    item = AssistantTextItem(0, "final", text)
    turn = ModelTurn(
        request.model_turn_id,
        request.provider,
        request.model,
        "r1",
        (item,),
        FinishReason.STOP,
    )
    return (
        TurnStarted(header(0), request.model),
        ItemStarted(header(1), 0, item.item_id, OutputKind.ASSISTANT_TEXT),
        ItemCompleted(header(2), item),
        TurnCompleted(header(3), turn),
    )


class _TextModel:
    # Deterministic text-only model: one assistant answer per turn.

    def __init__(self) -> None:
        self.rounds = 0

    def stream(self, request):
        self.rounds += 1
        return _text_stream(request, f"answer {self.rounds}")


class _CaptureModel:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def stream(self, request):
        self.requests.append(request)
        return _text_stream(request, "done")


if __name__ == "__main__":
    unittest.main()
