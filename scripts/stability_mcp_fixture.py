"""Bounded MCP load fixture: hold calls until an explicit release notification."""
from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from koawa_agent_v2.mcp.fixture_server import _FrameReader, _canonical, _write_frame
from koawa_agent_v2.mcp.protocol import MCP_PROTOCOL_VERSION


def serve(pending: int, notifications: int) -> None:
    reader, lock = _FrameReader(), threading.Lock()
    calls = []
    initialized = False
    released = False

    def send(document):
        _write_frame(_canonical(document).encode("utf-8"), lock)

    def response(request, result):
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})

    while (frame := reader.read()) is not None:
        request = json.loads(frame)
        method = request["method"]
        if method == "initialize":
            response(request, {"protocolVersion": MCP_PROTOCOL_VERSION,
                               "capabilities": {"tools": {"listChanged": True}},
                               "serverInfo": {"name": "stability-fixture", "version": "1"}})
        elif method == "notifications/initialized":
            initialized = True
        elif method == "tools/list":
            response(request, {"tools": [{"name": "hold", "description": "bounded load probe",
                "inputSchema": {"type": "object", "properties": {
                    "index": {"type": "integer", "minimum": 0, "maximum": 100}},
                    "required": ["index"], "additionalProperties": False}}]})
        elif method == "tools/call":
            if not initialized or released or len(calls) >= pending:
                raise RuntimeError("fixture_unexpected_call")
            calls.append(request)
        elif method == "benchmark/release":
            if len(calls) != pending or released:
                raise RuntimeError("fixture_release_before_pending_barrier")
            released = True
            for _ in range(notifications):
                send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            # Deliberately out of order; request-id routing must remain exact.
            for call in reversed(calls):
                response(call, {"content": [{"type": "text", "text": _canonical({
                    "index": call["params"]["arguments"]["index"],
                    "received_calls": len(calls), "notifications": notifications})}]})
        else:
            raise RuntimeError("fixture_unexpected_method")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pending", type=int, required=True, choices=range(1, 101))
    parser.add_argument("--notifications", type=int, required=True, choices=range(1, 65537))
    args = parser.parse_args()
    serve(args.pending, args.notifications)
