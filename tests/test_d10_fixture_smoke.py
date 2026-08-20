from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.mcp.protocol import (
    INITIALIZE,
    INITIALIZED_NOTIFICATION,
    MCP_PROTOCOL_VERSION,
    TOOLS_CALL,
    TOOLS_LIST,
    notification_payload,
    parse_message,
    request_payload,
)
from koawa_agent_v2.mcp.transport import (
    StdioTransport,
    TransportMalformedFrame,
)


def _fixture_command():
    return [sys.executable, "-m", "koawa_agent_v2.mcp.fixture_server"]


def _base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "KOAWA_MCP_FIXTURE_PAGE_SIZE": "2",
    }
    if extra:
        env.update(extra)
    return env


class FixtureSmokeTest(unittest.TestCase):
    def _open(self, extra: dict[str, str] | None = None) -> StdioTransport:
        transport = StdioTransport(
            _fixture_command(),
            env=_base_env(extra),
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        transport.open()
        self.addCleanup(transport.close)
        return transport

    def _initialize(self, transport: StdioTransport) -> dict:
        transport.send(
            request_payload(
                1,
                INITIALIZE,
                {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}},
            )
        )
        response = transport.read(timeout=5)
        self.assertEqual(1, response.id)
        transport.send(notification_payload(INITIALIZED_NOTIFICATION))
        return response.result

    def test_initialize_and_tools_list_pagination(self) -> None:
        tools_json = [
            {"name": "t1", "description": "one", "inputSchema": {
                "type": "object", "properties": {}, "required": [],
                "additionalProperties": False}},
            {"name": "t2", "description": "two", "inputSchema": {
                "type": "object", "properties": {}, "required": [],
                "additionalProperties": False}},
            {"name": "t3", "description": "three", "inputSchema": {
                "type": "object", "properties": {}, "required": [],
                "additionalProperties": False}},
            {"name": "t4", "description": "four", "inputSchema": {
                "type": "object", "properties": {}, "required": [],
                "additionalProperties": False}},
        ]
        transport = self._open({"KOAWA_MCP_FIXTURE_TOOLS_JSON": json_dumps(tools_json)})
        result = self._initialize(transport)
        self.assertEqual(MCP_PROTOCOL_VERSION, result["protocolVersion"])
        self.assertEqual("koawa-fixture", result["serverInfo"]["name"])

        transport.send(request_payload(2, TOOLS_LIST, {}))
        first = transport.read(timeout=5)
        self.assertEqual(2, first.id)
        self.assertEqual(["t1", "t2"], [t["name"] for t in first.result["tools"]])
        self.assertIn("nextCursor", first.result)

        transport.send(
            request_payload(3, TOOLS_LIST, {"cursor": first.result["nextCursor"]})
        )
        second = transport.read(timeout=5)
        self.assertEqual(3, second.id)
        self.assertEqual(["t3", "t4"], [t["name"] for t in second.result["tools"]])
        self.assertNotIn("nextCursor", second.result)

    def test_echo_call_and_concurrent_ids(self) -> None:
        transport = self._open()
        self._initialize(transport)
        for request_id, value in ((10, "alpha"), (11, "beta")):
            transport.send(
                request_payload(
                    request_id,
                    TOOLS_CALL,
                    {"name": "echo", "arguments": {"value": value}},
                )
            )
        seen: dict[int, str] = {}
        for _ in range(2):
            response = transport.read(timeout=5)
            seen[response.id] = response.result["content"][0]["text"]
        self.assertEqual(set(seen), {10, 11})
        self.assertIn("alpha", seen[10])
        self.assertIn("beta", seen[11])

    def test_throw_on_tool_returns_error(self) -> None:
        transport = self._open({"KOAWA_MCP_FIXTURE_THROW_ON_TOOL": "fail"})
        self._initialize(transport)
        transport.send(
            request_payload(20, TOOLS_CALL, {"name": "fail", "arguments": {}})
        )
        response = transport.read(timeout=5)
        self.assertEqual(20, response.id)
        self.assertIsNotNone(response.error)
        self.assertEqual(-32602, response.error.code)

    def test_version_override(self) -> None:
        transport = self._open({"KOAWA_MCP_FIXTURE_PROTOCOL_VERSION": "2025-03-26"})
        result = self._initialize(transport)
        self.assertEqual("2025-03-26", result["protocolVersion"])

    def test_malformed_first_frame_is_observable(self) -> None:
        transport = self._open({"KOAWA_MCP_FIXTURE_MALFORMED_FIRST_FRAME": "garbage"})
        with self.assertRaises(TransportMalformedFrame):
            transport.read(timeout=5)


def json_dumps(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    unittest.main()
