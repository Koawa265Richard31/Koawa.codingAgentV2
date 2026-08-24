from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

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
    SpawnSpec,
    StdioTransport,
    SystemProcessSpawner,
    TransportClosed,
    TransportError,
    TransportMalformedFrame,
    TransportOverflow,
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


CANARY_SCRIPT = r"""
import json
import os
import sys

document = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "env",
    "params": {
        "keys": sorted(os.environ),
        "values": {
            name: os.environ.get(name, "")
            for name in (
                "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
                "LANG", "LC_ALL", "PATH",
            )
        },
    },
}
body = json.dumps(document, separators=(",", ":")).encode("utf-8")
sys.stdout.buffer.write(
    b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
)
sys.stdout.buffer.flush()
"""


ENV_REPORT_SCRIPT = r"""
import json
import os
import sys

document = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "env",
    "params": dict(os.environ),
}
body = json.dumps(document, separators=(",", ":")).encode("utf-8")
sys.stdout.buffer.write(
    b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
)
sys.stdout.buffer.flush()
"""


STDERR_SCRIPT = r"""
import sys

sys.stderr.buffer.write(b"x" * 200_000)
sys.stderr.buffer.flush()
body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
sys.stdout.buffer.write(
    b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
)
sys.stdout.buffer.flush()
"""


OVERFLOW_SCRIPT = r"""
import sys

body = b'{"jsonrpc":"2.0","id":1,"method":"m"}'
frame = (
    b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
)
sys.stdout.buffer.write(frame * 1500)
sys.stdout.buffer.flush()
"""


def _pid_alive(pid: int) -> bool:
    """Probe whether a pid exists (cross-platform, stdlib only)."""

    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _AckLossSpawner:
    """Spawn whose ACK is returned only after process_start deadline elapses.

    The OS child really exists (simulating an owned tree whose ACK was lost);
    the transport must force-collect the tree and fail closed with
    mcp_process_start_timeout, reporting only after wait confirms the tree is
    gone.
    """

    def __init__(self, pid_file: Path) -> None:
        self._pid_file = pid_file

    def spawn(self, spec: SpawnSpec, *, deadline: float):
        time.sleep(1.0)  # far past the 0.2s deadline used by the test
        spawner = SystemProcessSpawner()
        owned = spawner.spawn(spec, deadline=float("inf"))  # bypass pre-check
        self._pid_file.write_text(str(owned.pid), encoding="ascii")
        return owned


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
            env=kwargs.pop("env", {}),
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

    def test_inbound_frame_too_large(self) -> None:
        # I1: send() now rejects outbound bodies above max_frame_bytes before
        # writing, so the read-side size enforcement is exercised inbound: a
        # child that declares a frame larger than the cap.
        script = (
            "import sys; "
            "sys.stdout.buffer.write(b'Content-Length: 999999\\r\\n\\r\\n{}'); "
            "sys.stdout.buffer.flush(); "
            "import time; time.sleep(0.2)"
        )
        transport = self._transport(script, max_frame_bytes=256)
        transport.open()
        self.addCleanup(transport.close)
        with self.assertRaises(TransportMalformedFrame) as raised:
            transport.read(timeout=5)
        self.assertEqual("frame_too_large", raised.exception.code)

    def test_close_is_idempotent_and_kills_process(self) -> None:
        transport = self._transport()
        transport.open()
        transport.close()
        transport.close()
        self.assertTrue(transport.closed)

    def test_close_before_open_and_after_failed_open_and_twice(self) -> None:
        transport = self._transport("")
        self.assertFalse(transport.closed)
        transport.close()
        transport.close()
        self.assertTrue(transport.closed)
        with self.assertRaises(TransportClosed):
            transport.open()

        missing = str(Path(tempfile.gettempdir()) / f"no-such-mcp-{uuid4().hex}")
        failed = StdioTransport([missing], env={})
        with self.assertRaises(TransportError) as raised:
            failed.open()
        self.assertEqual("mcp_process_start_failed", raised.exception.code)
        self.assertTrue(failed.closed)
        failed.close()
        failed.close()
        with self.assertRaises(TransportError) as raised:
            failed.open()
        self.assertEqual("transport_state_invalid", raised.exception.code)

    def test_send_to_closed_transport_raises_closed(self) -> None:
        transport = self._transport("")
        transport.close()
        with self.assertRaises(TransportClosed):
            transport.send("{}")

    def test_send_validates_payload_before_writing(self) -> None:
        transport = self._transport(max_frame_bytes=256)
        transport.open()
        self.addCleanup(transport.close)
        with self.assertRaises(TransportError) as raised:
            transport.send("x" * 1000)
        self.assertEqual("transport_payload_invalid", raised.exception.code)
        self.assertEqual("not_sent", transport.send_state)
        with self.assertRaises(TransportError) as raised:
            transport.send('{"bad": "\ud800"}')
        self.assertEqual("transport_payload_invalid", raised.exception.code)
        transport.send("{}")
        self.assertEqual("sent", transport.send_state)

    def test_secret_and_injection_env_are_rejected(self) -> None:
        cases = (
            ({"FOO_API_KEY": "x"}, "secret_variable_forbidden"),
            ({"GITHUB_TOKEN": "x"}, "secret_variable_forbidden"),
            ({"SERVICE_AUTHORIZATION": "x"}, "secret_variable_forbidden"),
            ({"AWS_SECRET_ACCESS_KEY": "x"}, "secret_variable_forbidden"),
        )
        if os.name != "nt":
            # NOTE: subprocess_env rejects injection names on POSIX; on Windows
            # the builder compares casefolded keys against an uppercase set so
            # the injection check is not reachable there (pre-existing infra
            # gap, out of I1-C scope; secret-shaped names reject everywhere).
            cases += (
                ({"LD_PRELOAD": "/tmp/x.so"}, "injection_variable_forbidden"),
                ({"BASH_ENV": "/tmp/x"}, "injection_variable_forbidden"),
            )
        for env_map, code in cases:
            with self.subTest(env=tuple(env_map), code=code):
                transport = StdioTransport([sys.executable, "-c", ""], env=env_map)
                with self.assertRaises(TransportError) as raised:
                    transport.open()
                self.assertEqual(code, raised.exception.code)
                self.assertTrue(transport.closed)
                transport.close()
                transport.close()

    def test_explicit_env_is_passed_through_default_allowlist(self) -> None:
        transport = self._transport(
            ENV_REPORT_SCRIPT, env={"FOO_UNUSED": "bar", "CUSTOM_ONLY": "baz"}
        )
        transport.open()
        self.addCleanup(transport.close)
        message = transport.read(timeout=5)
        self.assertEqual("env", message.method)
        self.assertEqual("bar", message.params["FOO_UNUSED"])
        self.assertEqual("baz", message.params["CUSTOM_ONLY"])

    def test_parent_canary_env_absent_from_child(self) -> None:
        canary = f"KOAWA_CANARY_{uuid4().hex}"
        provider = f"KOAWA_PROVIDER_KEY_{uuid4().hex}"
        os.environ[canary] = "canary-value"
        os.environ[provider] = "must-not-leak"
        self.addCleanup(os.environ.pop, canary, None)
        self.addCleanup(os.environ.pop, provider, None)

        transport = self._transport(CANARY_SCRIPT)
        transport.open()
        self.addCleanup(transport.close)
        message = transport.read(timeout=5)
        self.assertEqual("env", message.method)
        keys = message.params["keys"]
        self.assertNotIn(canary, keys)
        self.assertNotIn(provider, keys)
        self.assertNotIn("PATH", keys)
        values = message.params["values"]
        if os.name == "nt":
            # Windows os.environ normalizes keys to uppercase.
            upper_keys = {key.upper() for key in keys}
            self.assertIn("SYSTEMROOT", upper_keys)
            self.assertIn("COMSPEC", upper_keys)
            root = values["SystemRoot"]
            self.assertTrue(root, "SystemRoot must be set")
            self.assertTrue(
                Path(root).is_dir(), f"SystemRoot is not a directory: {root!r}"
            )
            comspec = values["COMSPEC"]
            self.assertTrue(comspec, "COMSPEC must be set")
            self.assertEqual(Path(root) / "System32" / "cmd.exe", Path(comspec))
            self.assertTrue(Path(values["TEMP"]).is_absolute())
            self.assertTrue(Path(values["TMP"]).is_absolute())
        else:
            self.assertIn("LANG", keys)
            self.assertIn("LC_ALL", keys)
            self.assertEqual("C", values["LANG"])
            self.assertEqual("C", values["LC_ALL"])
        # Sanity: the parent process still holds the canaries.
        self.assertEqual("canary-value", os.environ.get(canary))

    def test_large_stderr_drained_and_truncated(self) -> None:
        transport = self._transport(STDERR_SCRIPT, max_stderr_bytes=4096)
        transport.open()
        self.addCleanup(transport.close)
        message = transport.read(timeout=10)
        self.assertEqual("ping", message.method)
        transport.close()
        self.assertTrue(transport.stderr_truncated)
        self.assertGreaterEqual(transport.stderr_bytes, 200_000)

    def test_process_start_deadline_fails_closed_and_kills_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "ack-late-pid.txt"
            transport = StdioTransport(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                env={},
                process_start_timeout_seconds=0.2,
                shutdown_timeout_seconds=2.0,
                process_spawner=_AckLossSpawner(pid_file),
            )
            with self.assertRaises(TransportError) as raised:
                transport.open()
            self.assertEqual("mcp_process_start_timeout", raised.exception.code)
            self.assertTrue(transport.closed)
            self.assertEqual("failed", transport.state)
            pid = int(pid_file.read_text(encoding="ascii"))
            deadline = time.monotonic() + 5.0
            while _pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(
                _pid_alive(pid), f"spawned process {pid} survived the teardown"
            )

    def test_inbound_queue_overflow_fails_closed(self) -> None:
        transport = self._transport(OVERFLOW_SCRIPT, max_inbound_messages=16)
        transport.open()
        self.addCleanup(transport.close)
        deadline = time.monotonic() + 10.0
        while True:
            if time.monotonic() >= deadline:
                self.fail("inbound queue overflow was never observed")
            try:
                transport.read(timeout=0.5)
            except TransportOverflow as error:
                self.assertEqual("inbound_queue_overflow", error.code)
                break
            except TransportClosed:
                self.fail("transport closed before overflow was observed")
        self.assertTrue(transport.closed)


if __name__ == "__main__":
    unittest.main()
