from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.approval_service import (
    ApprovalService,
    ApprovalStatus,
    ApprovalWaiting,
)
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.loop import (
    ToolExecutionContext,
    ToolExecutionResult,
)
from koawa_agent_v2.ledger import (
    LedgerExecutor,
    MANUAL_WRITE_PROFILE,
    READ_ONLY_PROFILE,
    ToolExecutionState,
    ToolLedgerError,
    ToolLedgerStore,
    ToolOutcomeBlocked,
    ToolRecoveryProfile,
    logical_execution_id,
)
from koawa_agent_v2.mcp import (
    McpSession,
    McpSessionError,
    StdioTransport,
    build_mcp_registry,
    spawn_fixture_command,
)
import threading
import time

from koawa_agent_v2.mcp.connection_manager import TOOLS_LIST_CHANGED_NOTIFICATION
from koawa_agent_v2.mcp.protocol import (
    JsonRpcNotification,
    JsonRpcResponse,
    MCP_PROTOCOL_VERSION,
)
from koawa_agent_v2.mcp.transport import TransportClosed, TransportTimeout
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.policy import (
    ActionKind,
    Decision,
    PolicyEngine,
    PolicyRule,
    Principal,
    ResolvedAction,
    SideEffectClass as PolicySideEffectClass,
    canonical_arguments,
)
from koawa_agent_v2.telemetry.trace import TraceStore
from koawa_agent_v2.tools.errors import ToolRegistryError


REPO_ROOT = Path(__file__).resolve().parents[1]


class D10McpIntegrationTest(unittest.TestCase):
    """Real MCP fixture through Registry -> Policy -> Ledger -> transport."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "d10.sqlite3"
        self.store = SqliteEventStore(self.database)
        self.runtime = ThreadRuntime(self.store, actor="d10")
        self.ledger = ToolLedgerStore(self.store)
        self.approvals = ApprovalService(
            self.store,
            self.ledger,
            budget_action_limits={"root": 100},
        )
        self.trace = TraceStore(self.store)
        self.correlation_id = uuid4()
        self.principal = Principal("root", ("mcp.use",))

    def open_session(
        self,
        extra_env: dict[str, str] | None = None,
        *,
        request_timeout: float = 1.0,
    ) -> McpSession:
        env = {"PYTHONPATH": str(REPO_ROOT / "src")}
        if extra_env:
            env.update(extra_env)
        transport = StdioTransport(
            spawn_fixture_command(),
            env=env,
            cwd=str(REPO_ROOT),
        )
        session = McpSession(
            "server",
            transport,
            request_timeout=request_timeout,
            trace_store=self.trace,
            correlation_id=self.correlation_id,
        )
        self.addCleanup(session.close)
        return session

    def start_turn(self, label: str):
        thread = self.runtime.create_thread(f"repo-{label}")
        queued = self.runtime.create_turn(
            thread.thread_id,
            label,
            expected_thread_version=thread.version,
        )
        return self.runtime.start_turn(queued.turn_id, queued.version)

    def direct_call(
        self,
        running,
        label: str,
        tool_name: str,
        arguments: str = '{"value":"hi"}',
    ):
        model_turn_id = uuid4()
        call = ToolCallItem(
            0,
            f"item-{label}",
            f"call-{label}",
            tool_name,
            arguments,
        )
        context = ToolExecutionContext(
            running.current_run_id,
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, call.call_id),
            turn_id=running.turn_id,
            turn_version=running.version,
        )
        return call, context

    def execution_id_for(self, catalog, running, context, call):
        binding = catalog.bindings[call.name]
        return logical_execution_id(
            running.turn_id,
            context.model_turn_id,
            call.call_id,
            binding_digest=binding.binding_digest,
        )

    def resolver(self, session: McpSession, catalog, name: str):
        binding = catalog.bindings[name]

        def resolve(call, context, profile, previous):
            return ResolvedAction(
                kind=ActionKind.MCP_TOOL,
                tool_name=call.name,
                canonical_arguments_json=canonical_arguments(call.arguments_json),
                principal=self.principal,
                side_effect_class=PolicySideEffectClass(
                    profile.side_effect_class.value
                ),
                sandbox_profile_id="d10-mcp",
                policy_version="policy-v1",
                mcp_server_id=session.server_id,
                mcp_session_generation=session.generation,
                mcp_schema_hash=binding.schema_hash,
            )

        return resolve

    def build_executor(
        self,
        session: McpSession,
        catalog,
        decision: Decision,
        *,
        profiles: dict[str, ToolRecoveryProfile] | None = None,
        delegate=None,
    ) -> LedgerExecutor:
        delegate = delegate or build_mcp_registry(session, catalog)
        default_profiles = {
            name: READ_ONLY_PROFILE for name in sorted(catalog.bindings)
        }
        rules = tuple(
            PolicyRule(
                f"mcp-{name}",
                decision,
                action_kinds=(ActionKind.MCP_TOOL,),
                tool_names=(name,),
                principal_ids=("root",),
                required_scopes=("mcp.use",),
            )
            for name in sorted(catalog.bindings)
        )
        engine = PolicyEngine("policy-v1", rules)
        return LedgerExecutor(
            delegate,
            self.ledger,
            profiles or default_profiles,
            policy_engine=engine,
            approval_service=self.approvals,
            action_resolvers={
                name: self.resolver(session, catalog, name)
                for name in catalog.bindings
            },
            trace_store=self.trace,
            correlation_id=self.correlation_id,
        )

    def test_allow_mcp_call_roundtrip_and_ledger_binding(self) -> None:
        session = self.open_session()
        catalog = session.connect()
        binding = catalog.bindings["server__echo"]
        executor = self.build_executor(session, catalog, Decision.ALLOW)
        running = self.start_turn("mcp-allow")
        call, context = self.direct_call(running, "echo", "server__echo")

        ticket = executor.authorize(call, context=context)
        result = executor.execute_authorized(ticket)

        self.assertFalse(result.is_error)
        envelope = json.loads(result.content)
        self.assertTrue(envelope["untrusted_mcp_result"])
        self.assertEqual("server", envelope["server_id"])
        self.assertIn("hi", envelope["result"])
        record = self.ledger.load(ticket.record.execution_id)
        self.assertEqual(ToolExecutionState.SUCCEEDED, record.state)
        self.assertEqual(binding.binding_digest, record.binding_digest)

    def test_ask_grant_resume_executes_once_through_fixture(self) -> None:
        session = self.open_session()
        catalog = session.connect()
        executor = self.build_executor(session, catalog, Decision.ASK)
        running = self.start_turn("mcp-ask")
        call, context = self.direct_call(running, "ask", "server__echo")

        with self.assertRaises(ApprovalWaiting):
            executor.authorize(call, context=context)
        record = self.ledger.load(
            self.execution_id_for(catalog, running, context, call)
        )
        self.assertEqual(ToolExecutionState.PREPARED, record.state)
        pending = self.approvals.load(record.execution_id)
        self.assertEqual(ApprovalStatus.PENDING, pending.status)
        waiting_turn = self.runtime.get_turn(running.turn_id)

        granted = self.approvals.resolve(
            pending,
            True,
            expected_approval_version=pending.version,
            expected_turn_version=waiting_turn.version,
            interrupt_id=pending.interrupt_id,
            approver_principal_id="operator",
        )
        self.assertEqual(ApprovalStatus.GRANTED, granted.status)

        queued_turn = self.runtime.get_turn(running.turn_id)
        resumed = self.runtime.start_turn(queued_turn.turn_id, queued_turn.version)
        resumed_context = ToolExecutionContext(
            resumed.current_run_id,
            context.model_turn_id,
            1,
            ModelCallRef(context.model_turn_id, call.call_id),
            turn_id=resumed.turn_id,
            turn_version=resumed.version,
        )
        ticket = executor.authorize(call, context=resumed_context)
        result = executor.execute_authorized(ticket)
        self.assertFalse(result.is_error)
        final = self.ledger.load(record.execution_id)
        self.assertEqual(ToolExecutionState.SUCCEEDED, final.state)
        consumed = self.approvals.load(record.execution_id)
        self.assertEqual(ApprovalStatus.CONSUMED, consumed.status)

    def test_refresh_creates_new_binding_and_new_approval(self) -> None:
        session = self.open_session()
        first_catalog = session.connect()
        first_executor = self.build_executor(
            session, first_catalog, Decision.ASK
        )
        running = self.start_turn("mcp-refresh")
        call, context = self.direct_call(running, "refresh", "server__echo")

        with self.assertRaises(ApprovalWaiting):
            first_executor.authorize(call, context=context)
        first_record = self.ledger.load(
            self.execution_id_for(first_catalog, running, context, call)
        )
        first_pending = self.approvals.load(first_record.execution_id)
        waiting_turn = self.runtime.get_turn(running.turn_id)
        self.approvals.resolve(
            first_pending,
            True,
            expected_approval_version=first_pending.version,
            expected_turn_version=waiting_turn.version,
            interrupt_id=first_pending.interrupt_id,
            approver_principal_id="operator",
        )

        second_catalog = session.refresh()
        self.assertEqual(2, second_catalog.generation)
        self.assertNotEqual(
            first_catalog.bindings["server__echo"].binding_digest,
            second_catalog.bindings["server__echo"].binding_digest,
        )
        second_executor = self.build_executor(
            session, second_catalog, Decision.ASK
        )
        queued_turn = self.runtime.get_turn(running.turn_id)
        resumed = self.runtime.start_turn(queued_turn.turn_id, queued_turn.version)
        resumed_context = ToolExecutionContext(
            resumed.current_run_id,
            context.model_turn_id,
            1,
            ModelCallRef(context.model_turn_id, call.call_id),
            turn_id=resumed.turn_id,
            turn_version=resumed.version,
        )
        with self.assertRaises(ApprovalWaiting):
            second_executor.authorize(call, context=resumed_context)
        second_record = self.ledger.load(
            self.execution_id_for(second_catalog, resumed, resumed_context, call)
        )
        self.assertNotEqual(
            first_record.execution_id,
            second_record.execution_id,
        )
        second_pending = self.approvals.load(second_record.execution_id)
        self.assertEqual(ApprovalStatus.PENDING, second_pending.status)
        self.assertNotEqual(
            first_pending.request_id,
            second_pending.request_id,
        )

    def test_timeout_marks_outcome_unknown(self) -> None:
        session = self.open_session(
            {"KOAWA_MCP_FIXTURE_CALL_DELAY_MS": "2000"},
            request_timeout=0.5,
        )
        catalog = session.connect()
        executor = self.build_executor(
            session,
            catalog,
            Decision.ALLOW,
            profiles={
                "server__slow": MANUAL_WRITE_PROFILE,
                "server__echo": READ_ONLY_PROFILE,
                "server__fail": READ_ONLY_PROFILE,
            },
        )
        running = self.start_turn("mcp-timeout")
        call, context = self.direct_call(running, "slow", "server__slow", "{}")
        ticket = executor.authorize(call, context=context)

        with self.assertRaises(ToolOutcomeBlocked) as raised:
            executor.execute_authorized(ticket)
        self.assertEqual("tool_outcome_unknown", raised.exception.code)
        record = self.ledger.load(ticket.record.execution_id)
        self.assertEqual(ToolExecutionState.OUTCOME_UNKNOWN, record.state)

    def test_unknown_response_id_becomes_uncertain_outcome(self) -> None:
        session = self.open_session(
            {"KOAWA_MCP_FIXTURE_UNKNOWN_ID_RESPONSE": "1"},
            request_timeout=0.5,
        )
        catalog = session.connect()
        executor = self.build_executor(
            session,
            catalog,
            Decision.ALLOW,
            profiles={
                "server__echo": MANUAL_WRITE_PROFILE,
                "server__fail": READ_ONLY_PROFILE,
                "server__slow": READ_ONLY_PROFILE,
            },
        )
        running = self.start_turn("mcp-unknown-id")
        call, context = self.direct_call(running, "unknown", "server__echo")
        ticket = executor.authorize(call, context=context)

        with self.assertRaises(ToolOutcomeBlocked) as raised:
            executor.execute_authorized(ticket)
        self.assertEqual("tool_outcome_unknown", raised.exception.code)
        self.assertGreaterEqual(session.unknown_response_count, 1)
        record = self.ledger.load(ticket.record.execution_id)
        self.assertEqual(ToolExecutionState.OUTCOME_UNKNOWN, record.state)

    def test_trace_wired_into_tool_ledger_and_mcp(self) -> None:
        session = self.open_session()
        catalog = session.connect()
        executor = self.build_executor(session, catalog, Decision.ALLOW)
        running = self.start_turn("trace")
        call, context = self.direct_call(running, "trace", "server__echo")
        ticket = executor.authorize(call, context=context)
        executor.execute_authorized(ticket)
        records = self.trace.read(self.correlation_id)
        streams = {record.stream for record in records}
        self.assertIn("ledger", streams)
        self.assertIn("tool", streams)
        self.assertIn("mcp", streams)

    def test_mcp_handler_has_no_direct_bypass(self) -> None:
        session = self.open_session()
        catalog = session.connect()
        delegate = build_mcp_registry(session, catalog)
        executor = self.build_executor(
            session, catalog, Decision.ALLOW, delegate=delegate
        )
        running = self.start_turn("mcp-bypass")
        call, context = self.direct_call(running, "bypass", "server__echo")

        with self.assertRaises(ToolRegistryError) as raised:
            delegate.execute(call, context=context)
        self.assertEqual("policy_authorization_required", raised.exception.code)
        result = executor.execute(
            call,
            context=context,
        )
        self.assertFalse(result.is_error)


class SessionDeadlineLimitsTest(unittest.TestCase):
    """I1 Stage D: session-level staged deadlines and bounded limits (impl doc 3.6)."""

    class _ScriptedTransport:
        """Serves scripted responses gated on observed request sends (the
        notification loop consumes asynchronously, so a response must never
        be popped before its request was actually sent). Notifications are
        server-initiated and pop freely."""

        def __init__(self) -> None:
            self.sent: list[str] = []
            self.reads: list[object] = []
            self.closed = False
            self.sent_event = threading.Event()
            self._request_count = 0

        def open(self) -> None:
            if self.closed:
                raise TransportClosed()

        def send(self, payload: str) -> None:
            self.sent.append(payload)
            self.sent_event.set()

        def read(self, timeout: float):
            if not self.reads:
                raise TransportTimeout()
            item = self.reads[0]
            if hasattr(item, "method"):
                self.reads.pop(0)
                return item
            # JsonRpcResponse: wait until the matching request actually left.
            deadline = time.monotonic() + timeout
            while True:
                requests = self._sent_requests()
                if len(requests) > self._request_count:
                    self._request_count += 1
                    self.reads.pop(0)
                    return item
                if time.monotonic() >= deadline:
                    raise TransportTimeout()
                self.sent_event.wait(0.02)

        def _sent_requests(self) -> list[int]:
            ids: list[int] = []
            for payload in self.sent:
                try:
                    parsed = json.loads(payload)
                except Exception:
                    continue
                if isinstance(parsed, dict) and isinstance(parsed.get("id"), int):
                    ids.append(parsed["id"])
            return ids

        def close(self) -> None:
            self.closed = True

    @staticmethod
    def init_response():
        return JsonRpcResponse("2.0", 1, {"protocolVersion": MCP_PROTOCOL_VERSION})

    @staticmethod
    def list_page(request_id: int, next_cursor: str | None):
        result: dict[str, object] = {
            "tools": [
                {"name": "echo", "description": "d",
                 "inputSchema": {"type": "object",
                                 "properties": {"value": {"type": "string", "description": "v",
                                                          "minLength": 1, "maxLength": 10}},
                                 "required": ["value"], "additionalProperties": False}},
            ],
        }
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return JsonRpcResponse("2.0", request_id, result)

    def test_pending_limit_sends_zero_bytes(self) -> None:
        """Fill one pending slot; the second request must fail before sending."""
        transport = self._ScriptedTransport()
        session = McpSession("srv", transport,
                             tool_call_timeout_seconds=0.5,
                             max_pending_requests=1)
        result: list[str] = []

        def first() -> None:
            try:
                session._request("ping", {})
            except McpSessionError as exc:
                result.append(exc.code)

        thread = threading.Thread(target=first, daemon=True)
        thread.start()
        self.assertTrue(transport.sent_event.wait(1.0), "first request must be sent")
        with self.assertRaises(McpSessionError) as raised:
            session._request("ping-two", {})
        self.assertEqual("mcp_pending_limit_exceeded", raised.exception.code)
        self.assertEqual(1, len(transport.sent))
        thread.join(timeout=2.0)
        self.assertEqual(["mcp_request_timeout"], result)

    def test_list_cursor_repeated_fails_closed(self) -> None:
        transport = self._ScriptedTransport()
        transport.reads = [self.init_response(),
                           self.list_page(2, "c1"),
                           self.list_page(3, "c1")]
        session = McpSession("srv", transport, tools_list_timeout_seconds=5.0)
        with self.assertRaises(McpSessionError) as raised:
            session.connect()
        self.assertEqual("mcp_cursor_repeated", raised.exception.code)

    def test_list_pages_exceeded_fails_closed(self) -> None:
        transport = self._ScriptedTransport()
        transport.reads = [self.init_response()] + [
            self.list_page(index + 1, f"c{index}") for index in range(1, 5)
        ]
        session = McpSession("srv", transport, tools_list_timeout_seconds=5.0,
                             max_list_pages=3)
        with self.assertRaises(McpSessionError) as raised:
            session.connect()
        self.assertEqual("mcp_list_pages_exceeded", raised.exception.code)

    def test_cursor_too_large_fails_closed(self) -> None:
        transport = self._ScriptedTransport()
        transport.reads = [self.init_response(),
                           self.list_page(2, "12345")]
        session = McpSession("srv", transport, tools_list_timeout_seconds=5.0,
                             max_cursor_bytes=4)
        with self.assertRaises(McpSessionError) as raised:
            session.connect()
        self.assertEqual("mcp_cursor_too_large", raised.exception.code)

    def test_initialize_deadline_is_independent_of_tool_call(self) -> None:
        """A short initialize deadline fails fast although the tool-call
        deadline is generous: startup never inherits the call deadline."""
        transport = self._ScriptedTransport()
        session = McpSession("srv", transport,
                             initialize_timeout_seconds=0.1,
                             tool_call_timeout_seconds=10.0)
        with self.assertRaises(McpSessionError) as raised:
            session.connect()
        self.assertEqual("mcp_request_timeout", raised.exception.code)
        self.assertTrue(transport.closed, "failed session must tear down transport")

    def test_notification_storm_is_throttled(self) -> None:
        """After max_notifications_per_window the loop drops further notices;
        pending_refresh stays visible without unbounded worker spawning."""
        transport = self._ScriptedTransport()
        transport.reads = [self.init_response(),
                           self.list_page(2, None),
                           *([JsonRpcNotification("2.0", TOOLS_LIST_CHANGED_NOTIFICATION)] * 20)]
        session = McpSession("srv", transport, tools_list_timeout_seconds=0.1,
                             max_notifications_per_window=5, auto_refresh=True)
        session.connect()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and session._notify_window_count < 6:
            time.sleep(0.01)
        self.assertGreaterEqual(session._notify_window_count, 6)
        self.assertTrue(session.pending_refresh)
        session.close()
        self.assertEqual("closed", session.state)

    def test_refresh_worker_runs_and_releases_single_flight_lock(self) -> None:
        """An in-flight worker absorbs concurrent attempts; the worker must
        run to completion and release the single-flight lock."""
        transport = self._ScriptedTransport()
        session = McpSession("srv", transport, tools_list_timeout_seconds=0.1)
        self.assertTrue(session._refresh_worker_busy.acquire(blocking=False))
        session._spawn_refresh_worker()
        self.assertFalse(session._refresh_worker_busy.acquire(blocking=False),
                         "busy lock must absorb concurrent spawn")
        session._refresh_worker_busy.release()
        session._spawn_refresh_worker()
        acquired = session._refresh_worker_busy.acquire(blocking=True, timeout=2.0)
        self.assertTrue(acquired, "worker must finish and release the lock")
        session._refresh_worker_busy.release()
        session.close()

if __name__ == "__main__":
    unittest.main()
