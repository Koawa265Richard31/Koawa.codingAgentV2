from __future__ import annotations

import json
import subprocess
import sys
import unittest

from koawa_agent_v2.mcp.protocol import (
    INITIALIZE,
    INVALID_REQUEST,
    MCP_PROTOCOL_VERSION,
    TOOLS_CALL,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    McpProtocolError,
    Notification,
    error_payload,
    notification_payload,
    parse_message,
    request_payload,
    response_payload,
)
from koawa_agent_v2.mcp.transport import (
    StdioTransport,
    TransportClosed,
    TransportMalformedFrame,
    TransportTimeout,
)


ECHO_SCRIPT = r"""
import sys


def read_frame():
    header = b""
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        header += line
    length = int(header.split(b":", 1)[1])
    return sys.stdin.buffer.read(length)


def write_frame(body):
    sys.stdout.buffer.write(
        f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    )
    sys.stdout.buffer.flush()


while True:
    body = read_frame()
    if body is None:
        break
    write_frame(body)
"""


class ProtocolTest(unittest.TestCase):
    def test_parse_request_and_roundtrip(self) -> None:
        payload = request_payload(7, INITIALIZE, {"protocolVersion": MCP_PROTOCOL_VERSION})
        message = parse_message(payload)
        self.assertIsInstance(message, JsonRpcRequest)
        self.assertEqual(7, message.id)
        self.assertEqual(INITIALIZE, message.method)
        self.assertEqual(MCP_PROTOCOL_VERSION, message.params["protocolVersion"])
        self.assertEqual(payload, request_payload(7, INITIALIZE, message.params))

    def test_parse_response_and_error(self) -> None:
        message = parse_message(response_payload(3, {"ok": True}))
        self.assertIsInstance(message, JsonRpcResponse)
        self.assertEqual(3, message.id)
        self.assertEqual({"ok": True}, message.result)
        error_message = parse_message(error_payload(3, INVALID_REQUEST, "bad"))
        self.assertIsInstance(error_message, JsonRpcResponse)
        self.assertEqual(INVALID_REQUEST, error_message.error.code)
        self.assertEqual("bad", error_message.error.message)

    def test_parse_notification(self) -> None:
        message = parse_message(notification_payload("notifications/initialized"))
        self.assertIsInstance(message, JsonRpcNotification)
        self.assertIs(Notification, JsonRpcNotification)

    def test_duplicate_key_nan_and_bad_jsonrpc_are_rejected(self) -> None:
        for raw, code in (
            ('{"jsonrpc":"2.0","id":1,"method":"a","id":2}', "duplicate_key"),
            ('{"jsonrpc":"2.0","id":1,"method":"a","params":NaN}', "non_json_number"),
            ('{"jsonrpc":"1.0","id":1,"method":"a"}', "unsupported_jsonrpc_version"),
            ("not-json", "malformed_json"),
            ('{"jsonrpc":"2.0","id":"1","method":"a"}', "invalid_request_id"),
            ('{"jsonrpc":"2.0","id":1}', "invalid_response"),
            (
                '{"jsonrpc":"2.0","id":1,"result":1,"error":{"code":-1,"message":"x"}}',
                "invalid_response",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaises(McpProtocolError) as raised:
                    parse_message(raw)
                self.assertEqual(code, raised.exception.code)

    def test_depth_and_size_limits(self) -> None:
        deep = '{"jsonrpc":"2.0","id":1,"method":"a","params":{"x":' + "[" * 40 + "]" * 40 + "}}"
        with self.assertRaises(McpProtocolError) as raised:
            parse_message(deep)
        self.assertEqual("protocol_size_exceeded", raised.exception.code)
        with self.assertRaises(McpProtocolError):
            parse_message("x" * (1_048_577))

    def test_canonical_json_ordering(self) -> None:
        payload = request_payload(
            1, TOOLS_CALL, {"arguments": {"b": 2, "a": 1}, "name": "echo"}
        )
        document = json.loads(payload)
        self.assertEqual(
            list(document["params"]),
            sorted(document["params"]),
        )


class StdioTransportTest(unittest.TestCase):
    def _transport(self, script: str = ECHO_SCRIPT, **kwargs):
        return StdioTransport(
            [sys.executable, "-c", script],
            env={},
            max_frame_bytes=kwargs.pop("max_frame_bytes", 1_048_576),
            **kwargs,
        )

    def test_roundtrip(self) -> None:
        transport = self._transport()
        transport.open()
        self.addCleanup(transport.close)
        transport.send(request_payload(1, "ping", {"n": 1}))
        message = transport.read(timeout=5)
        self.assertEqual(1, message.id)
        self.assertEqual("ping", message.method)

    def test_timeout_then_eof_then_closed(self) -> None:
        script = "import time; time.sleep(30)"
        transport = self._transport(script)
        transport.open()
        self.addCleanup(transport.close)
        with self.assertRaises(TransportTimeout):
            transport.read(timeout=0.2)
        transport.close()
        with self.assertRaises(TransportClosed):
            transport.read(timeout=0.2)
        with self.assertRaises(TransportClosed):
            transport.send("{}")
        transport.close()

    def test_eof_before_message_raises_closed(self) -> None:
        transport = self._transport("")
        transport.open()
        self.addCleanup(transport.close)
        with self.assertRaises(TransportClosed):
            transport.read(timeout=5)

    def test_malformed_frame(self) -> None:
        script = (
            "import sys; sys.stdout.buffer.write(b'Content-Length: 999999\\r\\n\\r\\n{}'); "
            "sys.stdout.buffer.flush()"
        )
        transport = self._transport(script)
        transport.open()
        self.addCleanup(transport.close)
        with self.assertRaises(TransportMalformedFrame):
            transport.read(timeout=5)

    def test_frame_too_large(self) -> None:
        transport = self._transport(max_frame_bytes=256)
        transport.open()
        self.addCleanup(transport.close)
        transport.send(request_payload(1, "x", {"pad": "a" * 1000}))
        with self.assertRaises(TransportMalformedFrame) as raised:
            transport.read(timeout=5)
        self.assertEqual("frame_too_large", raised.exception.code)

    def test_close_is_idempotent_and_kills_process(self) -> None:
        transport = self._transport()
        transport.open()
        transport.close()
        transport.close()
        self.assertTrue(transport.closed)


if __name__ == "__main__":
    unittest.main()
