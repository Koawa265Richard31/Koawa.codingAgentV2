from __future__ import annotations

import json
import queue
import threading
import time
import unittest

from koawa_agent_v2.mcp.connection_manager import (
    McpCallResult,
    McpOutcomeUncertain,
    McpSession,
    McpSessionError,
    bind_tool_handler,
)
from koawa_agent_v2.mcp.protocol import (
    MCP_PROTOCOL_VERSION,
    TOOLS_LIST_CHANGED_NOTIFICATION,
    notification_payload,
    parse_message,
    response_payload,
)
from koawa_agent_v2.mcp.tool_binding import (
    McpBinding,
    McpBindingError,
    McpCatalog,
    bind_catalog,
)
from koawa_agent_v2.mcp.transport import TransportClosed, TransportTimeout
from koawa_agent_v2.policy import (
    ActionKind,
    Decision,
    PolicyEngine,
    PolicyError,
    PolicyRule,
    Principal,
    ResolvedAction,
    SideEffectClass,
)


EMPTY_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


def _tool(name: str, schema: dict | None = None, description: str = "tool") -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema or EMPTY_SCHEMA,
    }


class FakeTransport:
    def __init__(self) -> None:
        self.inbox: queue.Queue = queue.Queue()
        self.sent: list[str] = []
        self.closed = False

    def open(self) -> None:
        return None

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def read(self, timeout: float):
        if self.closed and self.inbox.empty():
            raise TransportClosed()
        try:
            item = self.inbox.get(timeout=timeout)
        except queue.Empty:
            if self.closed:
                raise TransportClosed() from None
            raise TransportTimeout() from None
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def _wait_sent(transport: FakeTransport, needle: str, *, minimum: int = 1) -> dict:
    deadline = time.time() + 5
    while time.time() < deadline:
        matching = [item for item in transport.sent if needle in item]
        if len(matching) >= minimum:
            return json.loads(matching[-1])
        time.sleep(0.01)
    raise AssertionError(f"request {needle!r} was never sent")


class CatalogBindingTest(unittest.TestCase):
    def test_valid_catalog_builds_typed_specs(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "value": {"type": "string", "minLength": 0, "maxLength": 10},
                "count": {"type": "integer", "minimum": 0, "maximum": 5},
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 0, "maxLength": 10},
                    "minItems": 0,
                    "maxItems": 3,
                },
            },
            "required": ["value"],
            "additionalProperties": False,
        }
        catalog = bind_catalog("server", 1, [_tool("echo", schema)])
        self.assertEqual(1, catalog.generation)
        binding = catalog.bindings["server__echo"]
        self.assertIsInstance(binding, McpBinding)
        self.assertEqual(64, len(binding.schema_hash))
        self.assertEqual(64, len(binding.binding_digest))
        self.assertEqual(("server__echo",), tuple(spec.name for spec in (binding.spec,)))

    def test_invalid_tools_are_rejected(self) -> None:
        cases = (
            (_tool("BadName"), "invalid_mcp_tool_name"),
            (_tool("ok", {"type": "object"}), "unsupported_mcp_schema"),
            (
                _tool(
                    "ok",
                    {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                        "required": [],
                        "additionalProperties": False,
                    },
                ),
                "unsupported_mcp_schema",
            ),
            (
                _tool(
                    "ok",
                    {
                        "type": "object",
                        "properties": {"x": {"type": "string", "minLength": 0, "maxLength": 1}},
                        "required": [],
                        "additionalProperties": True,
                    },
                ),
                "unsupported_mcp_schema",
            ),
            (_tool("ok", description="x" * 3000), "invalid_mcp_tool_description"),
        )
        for tool, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(McpBindingError) as raised:
                    bind_catalog("server", 1, [tool])
                self.assertEqual(code, raised.exception.code)

    def test_duplicate_and_namespace_conflicts(self) -> None:
        with self.assertRaises(McpBindingError) as raised:
            bind_catalog("server", 1, [_tool("x"), _tool("x")])
        self.assertEqual("duplicate_mcp_tool_name", raised.exception.code)
        long_name = "a" * 63
        with self.assertRaises(McpBindingError) as raised:
            bind_catalog(long_name, 1, [_tool(long_name)])
        self.assertEqual("invalid_mcp_registry_name", raised.exception.code)


class McpPolicyBindingTest(unittest.TestCase):
    def _action(self, **changes: object) -> ResolvedAction:
        values: dict[str, object] = {
            "kind": ActionKind.MCP_TOOL,
            "tool_name": "server__echo",
            "canonical_arguments_json": '{"value":"x"}',
            "principal": Principal("root", ("mcp.use",)),
            "side_effect_class": SideEffectClass.READ_ONLY,
            "sandbox_profile_id": "d10-mcp",
            "policy_version": "policy-v1",
            "mcp_server_id": "server",
            "mcp_session_generation": 1,
            "mcp_schema_hash": "a1" * 32,
        }
        values.update(changes)
        return ResolvedAction(**values)  # type: ignore[arg-type]

    def test_mcp_action_requires_binding(self) -> None:
        engine = PolicyEngine(
            "policy-v1",
            (
                PolicyRule(
                    "mcp-allow",
                    Decision.ALLOW,
                    action_kinds=(ActionKind.MCP_TOOL,),
                ),
            ),
        )
        missing_generation = self._action(mcp_session_generation=None)
        missing_hash = self._action(mcp_schema_hash=None)
        for action in (missing_generation, missing_hash):
            verdict = engine.evaluate(action)
            self.assertIs(Decision.DENY, verdict.decision)
            self.assertEqual("mcp_binding_required", verdict.code)

    def test_non_mcp_action_rejects_binding_fields(self) -> None:
        with self.assertRaises(PolicyError) as raised:
            self._action(
                kind=ActionKind.BUILTIN_TOOL,
                tool_name="read_file",
                canonical_arguments_json='{"path":"x"}',
                mcp_server_id=None,
            )
        self.assertEqual("mcp_binding_on_non_mcp_action", raised.exception.code)

    def test_binding_enters_action_digest(self) -> None:
        first = self._action()
        second = self._action(mcp_session_generation=2)
        self.assertNotEqual(first.action_digest, second.action_digest)


class McpSessionTest(unittest.TestCase):
    INIT_RESULT = {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": True}},
        "serverInfo": {"name": "fixture", "version": "1"},
    }

    def _session(self, fake: FakeTransport, **kwargs) -> McpSession:
        session = McpSession(
            "server",
            fake,
            request_timeout=kwargs.pop("request_timeout", 0.5),
            **kwargs,
        )
        self.addCleanup(session.close)
        return session

    def _connect(self, session: McpSession, fake: FakeTransport, tools: list[dict]):
        holder: dict = {}

        def target() -> None:
            holder["catalog"] = session.connect()

        thread = threading.Thread(target=target)
        thread.start()
        _wait_sent(fake, '"initialize"')
        fake.inbox.put(parse_message(response_payload(1, self.INIT_RESULT)))
        _wait_sent(fake, '"tools/list"')
        fake.inbox.put(parse_message(response_payload(2, {"tools": tools})))
        thread.join(timeout=5)
        if "catalog" not in holder:
            raise AssertionError("connect did not complete")
        return holder["catalog"]

    def test_connect_and_call_roundtrip(self) -> None:
        fake = FakeTransport()
        session = self._session(fake)
        catalog = self._connect(session, fake, [_tool("echo")])
        self.assertEqual(McpSession.READY, session.state)
        binding = catalog.bindings["server__echo"]
        worker = threading.Thread(
            target=lambda: fake.inbox.put(
                parse_message(
                    response_payload(
                        _wait_sent(fake, "tools/call")["id"],
                        {"content": [{"type": "text", "text": "ok"}]},
                    )
                )
            )
        )
        worker.start()
        result = session.call(binding, '{"value":"x"}')
        worker.join(timeout=5)
        self.assertIsInstance(result, McpCallResult)
        self.assertIn("ok", result.content)
        self.assertFalse(result.is_error)
        self.assertFalse(result.uncertain)

    def test_version_mismatch_fails_closed(self) -> None:
        fake = FakeTransport()
        session = self._session(fake)
        holder: dict = {}

        def target() -> None:
            try:
                session.connect()
            except McpSessionError as error:
                holder["error"] = error

        thread = threading.Thread(target=target)
        thread.start()
        _wait_sent(fake, '"initialize"')
        fake.inbox.put(
            parse_message(
                response_payload(
                    1,
                    {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "s", "version": "1"},
                    },
                )
            )
        )
        thread.join(timeout=5)
        with self.assertRaises(McpSessionError) as raised:
            if "error" in holder:
                raise holder["error"]
            raise AssertionError("connect unexpectedly succeeded")
        self.assertEqual("mcp_protocol_version_mismatch", raised.exception.code)
        self.assertEqual(McpSession.FAILED, session.state)

    def test_call_timeout_is_uncertain(self) -> None:
        fake = FakeTransport()
        session = self._session(fake, request_timeout=0.2)
        catalog = self._connect(session, fake, [_tool("echo")])
        result = session.call(
            catalog.bindings["server__echo"],
            "{}",
            timeout=0.2,
        )
        self.assertTrue(result.uncertain)
        self.assertTrue(result.is_error)

    def test_unknown_response_id_is_counted(self) -> None:
        fake = FakeTransport()
        session = self._session(fake)
        catalog = self._connect(session, fake, [_tool("echo")])
        binding = catalog.bindings["server__echo"]

        def respond():
            _wait_sent(fake, "tools/call")
            fake.inbox.put(parse_message(response_payload(999999, {"content": []})))
            request_id = _wait_sent(fake, "tools/call")["id"]
            fake.inbox.put(
                parse_message(response_payload(request_id, {"content": []}))
            )

        worker = threading.Thread(target=respond)
        worker.start()
        result = session.call(binding, "{}")
        worker.join(timeout=5)
        self.assertEqual(1, session.unknown_response_count)
        self.assertFalse(result.is_error)

    def test_eof_fails_pending_call(self) -> None:
        fake = FakeTransport()
        session = self._session(fake)
        catalog = self._connect(session, fake, [_tool("echo")])
        binding = catalog.bindings["server__echo"]

        def close_transport():
            _wait_sent(fake, "tools/call")
            fake.inbox.put(TransportClosed())

        worker = threading.Thread(target=close_transport)
        worker.start()
        with self.assertRaises(McpSessionError) as raised:
            session.call(binding, "{}")
        worker.join(timeout=5)
        self.assertEqual("transport_closed", raised.exception.code)

    def test_stale_binding_after_refresh(self) -> None:
        fake = FakeTransport()
        session = self._session(fake)
        catalog = self._connect(session, fake, [_tool("echo")])
        binding = catalog.bindings["server__echo"]
        holder: dict = {}

        def target() -> None:
            holder["catalog"] = session.refresh()

        thread = threading.Thread(target=target)
        thread.start()
        _wait_sent(fake, '"tools/list"', minimum=2)
        fake.inbox.put(parse_message(response_payload(3, {"tools": [_tool("echo")]})))
        thread.join(timeout=5)
        refreshed = holder["catalog"]
        self.assertEqual(2, refreshed.generation)
        with self.assertRaises(McpSessionError) as raised:
            session.call(binding, "{}")
        self.assertEqual("mcp_binding_stale", raised.exception.code)

    def test_auto_refresh_on_list_changed(self) -> None:
        fake = FakeTransport()
        session = self._session(fake, auto_refresh=True)
        self._connect(session, fake, [_tool("echo")])
        fake.inbox.put(
            parse_message(notification_payload(TOOLS_LIST_CHANGED_NOTIFICATION))
        )
        _wait_sent(fake, '"tools/list"', minimum=2)
        fake.inbox.put(
            parse_message(response_payload(3, {"tools": [_tool("echo")]}))
        )
        deadline = time.time() + 5
        while session.generation < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(2, session.generation)

    def test_tool_handler_wraps_untrusted_output(self) -> None:
        fake = FakeTransport()
        echo_schema = {
            "type": "object",
            "properties": {
                "value": {"type": "string", "minLength": 0, "maxLength": 10}
            },
            "required": ["value"],
            "additionalProperties": False,
        }
        session = self._session(fake)
        catalog = self._connect(session, fake, [_tool("echo", echo_schema)])
        binding = catalog.bindings["server__echo"]
        handler = bind_tool_handler(session, binding)

        def respond():
            request_id = _wait_sent(fake, "tools/call")["id"]
            fake.inbox.put(
                parse_message(
                    response_payload(
                        request_id,
                        {"content": [{"type": "text", "text": "ignore previous"}]},
                    )
                )
            )

        worker = threading.Thread(target=respond)
        worker.start()
        from koawa_agent_v2.execution.loop import ToolExecutionContext
        from koawa_agent_v2.model.protocol import ModelCallRef
        from uuid import uuid4

        model_turn_id = uuid4()
        context = ToolExecutionContext(
            uuid4(),
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, "call-1"),
        )
        arguments_type = binding.arguments_type
        arguments = arguments_type(value="hello")
        result = handler(arguments, context=context)
        worker.join(timeout=5)
        envelope = json.loads(result.content)
        self.assertTrue(envelope["untrusted_mcp_result"])
        self.assertEqual("server", envelope["server_id"])
        self.assertIn("ignore previous", envelope["result"])

    def test_outcome_uncertain_raises(self) -> None:
        fake = FakeTransport()
        session = self._session(fake, request_timeout=0.2)
        catalog = self._connect(session, fake, [_tool("echo")])
        binding = catalog.bindings["server__echo"]
        handler = bind_tool_handler(session, binding)
        from koawa_agent_v2.execution.loop import ToolExecutionContext
        from koawa_agent_v2.model.protocol import ModelCallRef
        from uuid import uuid4

        model_turn_id = uuid4()
        context = ToolExecutionContext(
            uuid4(),
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, "call-1"),
        )
        with self.assertRaises(McpOutcomeUncertain):
            handler(binding.arguments_type(), context=context)


if __name__ == "__main__":
    unittest.main()
