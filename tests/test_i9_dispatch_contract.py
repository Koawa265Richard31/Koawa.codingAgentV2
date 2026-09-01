"""I9 §11.5: one assembled dispatch contract for every entry kind.

These tests deliberately exercise the production ``LedgerExecutor`` wiring
instead of proving the individual modules in isolation.  A denied model call
must stop before the typed handler/transport, close the prepared ledger record
as a durable failure (never success), and leave only bounded/redacted facts.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import ToolExecutionContext, ToolExecutionResult
from koawa_agent_v2.ledger import (
    LedgerExecutor,
    READ_ONLY_PROFILE,
    ToolExecutionState,
    ToolLedgerStore,
)
from koawa_agent_v2.mcp import McpSession, StdioTransport, build_mcp_registry, spawn_fixture_command
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.policy import (
    ActionKind,
    Decision,
    PolicyEngine,
    PolicyRule,
    Principal,
    ResolvedAction,
    SideEffectClass,
    canonical_arguments,
)
from koawa_agent_v2.approval_service import ApprovalService
from koawa_agent_v2.tools.registry import ToolRegistry
from koawa_agent_v2.tools.schema import ToolSpec
from koawa_agent_v2.tools.errors import ToolRegistryError


CANARY = "password=i9-dispatch-credential-canary"


@dataclass(frozen=True, slots=True)
class ProbeArguments:
    payload: str


PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "payload": {"type": "string", "minLength": 1, "maxLength": 512},
    },
    "required": ["payload"],
    "additionalProperties": False,
}


PROBE_SPEC = ToolSpec(
    "dispatch_probe",
    "I9 dispatch contract probe",
    ProbeArguments,
    PROBE_SCHEMA,
)


class CountingHandler:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, arguments, *, context: ToolExecutionContext):
        del arguments, context
        self.calls += 1
        return ToolExecutionResult("should-not-run")


class CountingTransport:
    """Observe sends while delegating to the real stdio transport unchanged."""

    def __init__(self, transport) -> None:
        self._transport = transport
        self.sent = 0

    def open(self):
        return self._transport.open()

    def send(self, payload):
        self.sent += 1
        return self._transport.send(payload)

    def read(self, timeout):
        return self._transport.read(timeout)

    def close(self):
        return self._transport.close()


class SubagentHandler:
    """Real D11 control-plane entry; it must never be reached on DENY."""

    def __init__(self, control: AgentControlPlane, parent_id) -> None:
        self.control = control
        self.parent_id = parent_id
        self.calls = 0

    def __call__(self, arguments: ProbeArguments, *, context):
        self.calls += 1
        self.control.spawn_agent(
            parent_agent_id=self.parent_id,
            task_id=arguments.payload,
            principal_id="child",
            scopes=("agents.resolve",),
        )
        return ToolExecutionResult("should-not-run")


class I9DispatchContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="koawa-i9-dispatch-")
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.store = SqliteEventStore(base / "state.sqlite3")
        self.runtime = ThreadRuntime(self.store, actor="i9-dispatch")
        self.ledger = ToolLedgerStore(self.store)
        self.approvals = ApprovalService(
            self.store, self.ledger, budget_action_limits={"root": 20}
        )

    def _context(self, label: str, call: ToolCallItem):
        thread = self.runtime.create_thread("i9-" + label)
        queued = self.runtime.create_turn(
            thread.thread_id, label, expected_thread_version=thread.version
        )
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        return ToolExecutionContext(
            running.current_run_id,
            uuid4(),
            1,
            ModelCallRef(uuid4(), call.call_id),
            turn_id=running.turn_id,
            turn_version=running.version,
        )

    def _resolver(
        self,
        kind: ActionKind,
        tool_name: str,
        principal: Principal,
        *,
        mcp_server_id: str | None = None,
        mcp_session_generation: int | None = None,
        mcp_schema_hash: str | None = None,
    ):
        def resolve(call, _context, profile, _previous):
            return ResolvedAction(
                kind=kind,
                tool_name=tool_name,
                canonical_arguments_json=canonical_arguments(call.arguments_json),
                principal=principal,
                side_effect_class=SideEffectClass(profile.side_effect_class.value),
                sandbox_profile_id="i9-deny-sandbox",
                policy_version="policy-v1",
                mcp_server_id=mcp_server_id,
                mcp_session_generation=mcp_session_generation,
                mcp_schema_hash=mcp_schema_hash,
            )

        return resolve

    def _deny_executor(
        self,
        delegate,
        tool_name: str,
        *,
        kind: ActionKind = ActionKind.BUILTIN_TOOL,
        principal: Principal | None = None,
        mcp_server_id: str | None = None,
        mcp_session_generation: int | None = None,
        mcp_schema_hash: str | None = None,
    ) -> LedgerExecutor:
        principal = principal or Principal("root", ())
        engine = PolicyEngine(
            "policy-v1",
            (
                PolicyRule(
                    "requires-capability",
                    Decision.ALLOW,
                    action_kinds=(kind,),
                    tool_names=(tool_name,),
                    principal_ids=("root",),
                    required_scopes=(
                        "mcp.use" if kind is ActionKind.MCP_TOOL else "dispatch.use",
                    ),
                ),
            ),
        )
        return LedgerExecutor(
            delegate,
            self.ledger,
            {tool_name: READ_ONLY_PROFILE},
            policy_engine=engine,
            approval_service=self.approvals,
            action_resolvers={
                tool_name: self._resolver(
                    kind,
                    tool_name,
                    principal,
                    mcp_server_id=mcp_server_id,
                    mcp_session_generation=mcp_session_generation,
                    mcp_schema_hash=mcp_schema_hash,
                ),
            },
        )

    def _assert_denied_before_effect(
        self,
        executor: LedgerExecutor,
        delegate,
        call: ToolCallItem,
        context: ToolExecutionContext,
    ) -> None:
        # The old direct registry/adapter route is the injected bypass attempt.
        with self.assertRaises(ToolRegistryError) as raised:
            delegate.execute(call, context=context)
        self.assertEqual("policy_authorization_required", raised.exception.code)

        ticket = executor.authorize(call, context=context)
        result = executor.execute_authorized(ticket)
        self.assertTrue(result.is_error)
        self.assertEqual(
            {"code": "denied_by_default", "error": "policy_denied"},
            json.loads(result.content),
        )
        self.assertIsNotNone(ticket.record)
        record = self.ledger.load(ticket.record.execution_id)
        self.assertIsNotNone(record)
        self.assertEqual(ToolExecutionState.FAILED, record.state)
        self.assertNotEqual(ToolExecutionState.SUCCEEDED, record.state)
        self.assertEqual(
            ("tool.execution-prepared.v1", "tool.execution-failed.v1"),
            tuple(
                event.event_type
                for event in self.store.read_stream(
                    StreamId("tool-execution", record.execution_id)
                )
            ),
        )
        self.assertNotIn(
            "tool.execution-succeeded.v1",
            {
                event.event_type
                for event in self.store.read_all(after_position=0, limit=500)
            },
        )
        # The durable deny audit is stable and contains neither arguments nor
        # credential values (the ledger stores only their digest/size).
        self.assertEqual(result.content, record.result.content)
        all_events = self.store.read_all(after_position=0, limit=500)
        self.assertNotIn(
            CANARY,
            json.dumps(
                [event.payload for event in all_events],
                ensure_ascii=False,
                sort_keys=True,
                default=lambda value: dict(value),
            ),
        )

    def test_builtin_schema_registry_policy_ledger_sandbox_contract(self) -> None:
        registry = ToolRegistry()
        handler = CountingHandler()
        registry.register(PROBE_SPEC, handler)
        executor = self._deny_executor(registry, "dispatch_probe")
        model_turn_id = uuid4()
        call = ToolCallItem(
            0,
            "builtin-item",
            "builtin-call",
            "dispatch_probe",
            json.dumps({"payload": CANARY}),
        )
        context = self._context("builtin", call)
        # Ensure the ModelCallRef is the exact call identity expected by D7.
        context = ToolExecutionContext(
            context.run_id,
            model_turn_id,
            context.turn_version,
            ModelCallRef(model_turn_id, call.call_id),
            turn_id=context.turn_id,
            turn_version=context.turn_version,
        )
        self._assert_denied_before_effect(executor, registry, call, context)
        self.assertEqual(0, handler.calls)

    def test_mcp_schema_registry_policy_ledger_transport_contract(self) -> None:
        transport = CountingTransport(
            StdioTransport(
                spawn_fixture_command(),
                env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
                cwd=str(Path(__file__).resolve().parents[1]),
            )
        )
        session = McpSession(
            "i9server",
            transport,
            request_timeout=2.0,
        )
        self.addCleanup(session.close)
        catalog = session.connect()
        handshake_sends = transport.sent
        adapter = build_mcp_registry(session, catalog)
        tool_name = "i9server__echo"
        principal = Principal("root", ())
        executor = LedgerExecutor(
            adapter,
            self.ledger,
            {name: READ_ONLY_PROFILE for name in catalog.bindings},
            policy_engine=PolicyEngine(
                "policy-v1",
                (
                    PolicyRule(
                        "requires-mcp-capability",
                        Decision.ALLOW,
                        action_kinds=(ActionKind.MCP_TOOL,),
                        tool_names=(tool_name,),
                        principal_ids=("root",),
                        required_scopes=("mcp.use",),
                    ),
                ),
            ),
            approval_service=self.approvals,
            action_resolvers={
                name: self._resolver(
                    ActionKind.MCP_TOOL,
                    name,
                    principal,
                    mcp_server_id="i9server",
                    mcp_session_generation=catalog.generation,
                    mcp_schema_hash=binding.schema_hash,
                )
                for name, binding in catalog.bindings.items()
            },
        )
        model_turn_id = uuid4()
        call = ToolCallItem(
            0,
            "mcp-item",
            "mcp-call",
            tool_name,
            json.dumps({"value": CANARY}),
        )
        context = self._context("mcp", call)
        context = ToolExecutionContext(
            context.run_id,
            model_turn_id,
            context.turn_version,
            ModelCallRef(model_turn_id, call.call_id),
            turn_id=context.turn_id,
            turn_version=context.turn_version,
        )
        self._assert_denied_before_effect(executor, adapter, call, context)
        self.assertEqual(handshake_sends, transport.sent)

    def test_subagent_registry_policy_ledger_transport_contract(self) -> None:
        control = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(max_depth=2, max_total_agents=4),
        )
        root = control.spawn_agent(
            parent_agent_id=None,
            task_id="root",
            principal_id="root",
            scopes=(),
        )
        handler = SubagentHandler(control, root.agent_id)
        registry = ToolRegistry()
        subagent_spec = ToolSpec(
            "dispatch_subagent",
            "I9 D11 subagent dispatch probe",
            ProbeArguments,
            PROBE_SCHEMA,
        )
        registry.register(subagent_spec, handler)
        executor = self._deny_executor(registry, "dispatch_subagent")
        model_turn_id = uuid4()
        call = ToolCallItem(
            0,
            "subagent-item",
            "subagent-call",
            "dispatch_subagent",
            json.dumps({"payload": CANARY}),
        )
        context = self._context("subagent", call)
        context = ToolExecutionContext(
            context.run_id,
            model_turn_id,
            context.turn_version,
            ModelCallRef(model_turn_id, call.call_id),
            turn_id=context.turn_id,
            turn_version=context.turn_version,
        )
        self._assert_denied_before_effect(executor, registry, call, context)
        self.assertEqual(0, handler.calls)
        # The real D11 control plane has no child/resource event: the injected
        # subagent path was stopped at Policy, before spawn/transport.
        self.assertEqual([], control.graph.children(root.agent_id))
        spawned = [
            event
            for event in self.store.read_all(after_position=0, limit=500)
            if event.event_type == "agent.spawned.v2"
        ]
        self.assertEqual(1, len(spawned))
        self.assertEqual(str(root.agent_id), spawned[0].payload["agent_id"])


if __name__ == "__main__":
    unittest.main()
