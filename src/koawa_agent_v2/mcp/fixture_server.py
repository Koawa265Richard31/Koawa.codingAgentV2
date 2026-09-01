"""Real local MCP stdio fixture server with env-driven fault injection.

Run as ``python -m koawa_agent_v2.mcp.fixture_server``.  The server speaks the
same Content-Length framed JSON-RPC 2.0 as the MCP stdio transport so tests
exercise a real subprocess boundary instead of a mock client.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any


DEFAULT_PROTOCOL_VERSION = "2025-06-18"

DEFAULT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "Echo one bounded string back.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "minLength": 0,
                    "maxLength": 1000,
                }
            },
            "required": ["value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fail",
        "description": "Return a typed error result.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "isError": True,
        "result": {"content": [{"type": "text", "text": "boom"}]},
    },
    {
        "name": "slow",
        "description": "Return after a configurable delay.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "result": {"content": [{"type": "text", "text": "slow-ok"}]},
    },
]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return []
    return [
        item.strip()
        for item in raw.split(",")
        if item.strip()
    ]


_PROBE_TOOLS: dict[str, dict[str, Any]] = {
    "probe_env_names": {
        "name": "probe_env_names",
        "description": "Return the sorted visible environment variable names.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    "probe_host_path": {
        "name": "probe_host_path",
        "description": "Read one host path and return its sha256 (canary).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "probe_network": {
        "name": "probe_network",
        "description": "Attempt one HTTP GET against the given URL (canary).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2048,
                }
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}


def _load_tools() -> list[dict[str, Any]]:
    raw = os.environ.get("KOAWA_MCP_FIXTURE_TOOLS_JSON")
    if not raw:
        return DEFAULT_TOOLS
    try:
        tools = json.loads(raw)
    except json.JSONDecodeError:
        return DEFAULT_TOOLS
    if not isinstance(tools, list):
        return DEFAULT_TOOLS
    visible = [item for item in tools if isinstance(item, dict)]
    for name in _env_list("KOAWA_MCP_FIXTURE_EXTRA_TOOLS"):
        probe = _PROBE_TOOLS.get(name)
        if probe is not None:
            visible.append(probe)
    return visible


def _parse_message(raw: str) -> dict[str, Any] | None:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        document = json.loads(raw, object_pairs_hook=pairs)
    except (json.JSONDecodeError, ValueError, UnicodeError):
        return None
    return document if isinstance(document, dict) else None


class _FrameReader:
    def read(self) -> bytes | None:
        stream = sys.stdin.buffer
        header = bytearray()
        while True:
            line = stream.readline()
            if not line:
                return None
            if line == b"\r\n":
                break
            header.extend(line)
            if len(header) > 8_192:
                return None
        text = header.decode("ascii", "ignore").strip()
        if not text.startswith("Content-Length:"):
            return None
        value = text[len("Content-Length:"):].strip()
        if not value.isdigit():
            return None
        length = int(value)
        if length <= 0 or length > 4_194_304:
            return None
        body = stream.read(length)
        if len(body) != length:
            return None
        return body


def _write_frame(payload: bytes, lock: threading.Lock) -> None:
    header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
    with lock:
        sys.stdout.buffer.write(header)
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()


def _canonical(document: Any) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _append_call_marker(path: str, document: dict[str, Any]) -> None:
    """Persist fixture-only evidence that a tool body was entered.

    The marker is deliberately opt-in through the fixture environment.  It is
    used by the I8 real-process kill matrix to distinguish a request that was
    merely sent from one whose external body actually ran.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (_canonical(document) + "\n").encode("utf-8", "strict")
    with target.open("ab", buffering=0) as handle:
        handle.write(payload)
        os.fsync(handle.fileno())


def main() -> int:
    protocol_version = os.environ.get(
        "KOAWA_MCP_FIXTURE_PROTOCOL_VERSION", DEFAULT_PROTOCOL_VERSION
    )
    tools = _load_tools()
    page_size = _env_int("KOAWA_MCP_FIXTURE_PAGE_SIZE", 2)
    call_delay_ms = _env_int("KOAWA_MCP_FIXTURE_CALL_DELAY_MS", 0)
    init_delay_ms = _env_int("KOAWA_MCP_FIXTURE_INIT_DELAY_MS", 0)
    list_delay_ms = _env_int("KOAWA_MCP_FIXTURE_LIST_DELAY_MS", 0)
    shutdown_hang_ms = _env_int("KOAWA_MCP_FIXTURE_SHUTDOWN_HANG_MS", 0)
    list_changed_after = _env_int("KOAWA_MCP_FIXTURE_LIST_CHANGED_AFTER_CALLS", 0)
    unknown_id = _env_bool("KOAWA_MCP_FIXTURE_UNKNOWN_ID_RESPONSE")
    throw_on_tool = os.environ.get("KOAWA_MCP_FIXTURE_THROW_ON_TOOL")
    malformed = os.environ.get("KOAWA_MCP_FIXTURE_MALFORMED_FIRST_FRAME", "")
    huge_frame = _env_int("KOAWA_MCP_FIXTURE_HUGE_FRAME_BYTES", 0)
    stderr_bytes = _env_int("KOAWA_MCP_FIXTURE_STDERR_BYTES", 0)
    call_marker = os.environ.get("KOAWA_MCP_FIXTURE_CALL_MARKER")
    tools_by_name = {str(tool.get("name", "")): tool for tool in tools}

    if malformed == "garbage":
        sys.stdout.buffer.write(b"garbage-header\r\n\r\n")
        sys.stdout.buffer.flush()
    elif malformed == "bad-length":
        sys.stdout.buffer.write(b"Content-Length: 999999\r\n\r\n{}")
        sys.stdout.buffer.flush()
    if stderr_bytes > 0:
        sys.stderr.buffer.write(b"x" * stderr_bytes)
        sys.stderr.buffer.flush()

    initialized = False
    call_count = 0
    state_lock = threading.Lock()
    write_lock = threading.Lock()
    frame_reader = _FrameReader()

    def respond(request_id: int, result: Any) -> None:
        payload = _canonical(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        )
        if huge_frame > 0:
            payload = payload + " " * max(huge_frame - len(payload.encode("utf-8")), 0)
        _write_frame(payload.encode("utf-8"), write_lock)

    def respond_error(request_id: int, code: int, message: str) -> None:
        payload = _canonical(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )
        _write_frame(payload.encode("utf-8"), write_lock)

    def handle_request(request: dict[str, Any]) -> None:
        nonlocal initialized, call_count
        request_id = request.get("id")
        if not isinstance(request_id, int):
            return
        method = request.get("method")
        if method == "initialize":
            if init_delay_ms > 0:
                import time as _time

                _time.sleep(init_delay_ms / 1000.0)
            result = {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "koawa-fixture", "version": "1.0.0"},
            }
            respond(request_id, result)
            return
        if method == "notifications/initialized":
            initialized = True
            return
        if method == "tools/list":
            if list_delay_ms > 0:
                import time as _time

                _time.sleep(list_delay_ms / 1000.0)
            params = request.get("params") or {}
            cursor = params.get("cursor")
            start = 0
            if isinstance(cursor, str) and cursor.isdigit():
                start = int(cursor)
            page = tools[start : start + page_size]
            result: dict[str, Any] = {"tools": page}
            if start + page_size < len(tools):
                result["nextCursor"] = str(start + page_size)
            respond(request_id, result)
            return
        if method == "tools/call":
            if not initialized:
                respond_error(request_id, -32600, "not initialized")
                return
            params = request.get("params") or {}
            tool_name = params.get("name")
            tool = tools_by_name.get(tool_name)
            if unknown_id:
                respond_error(999999, -32602, "wrong id")
                return
            if throw_on_tool is not None and tool_name == throw_on_tool:
                respond_error(request_id, -32602, "invalid params")
                return
            if tool is None:
                respond_error(request_id, -32602, "unknown tool")
                return
            with state_lock:
                call_count += 1
                ordinal = call_count
            if call_marker:
                _append_call_marker(call_marker, {
                    "ordinal": ordinal,
                    "pid": os.getpid(),
                    "tool_name": tool_name,
                })
            if tool_name == "slow" and call_delay_ms > 0:
                import time

                time.sleep(call_delay_ms / 1000.0)
            arguments = params.get("arguments") or {}
            if tool_name == "probe_env_names":
                result = {
                    "content": [
                        {"type": "text", "text": _canonical({"names": sorted(os.environ)})}
                    ]
                }
            elif tool_name == "probe_host_path":
                target = arguments.get("path", "")
                import hashlib as _hashlib

                try:
                    with open(target, "rb") as handle:
                        digest = _hashlib.sha256(handle.read()).hexdigest()
                    result = {"content": [{"type": "text", "text": digest}]}
                except OSError:
                    result = {"content": [{"type": "text", "text": "unreadable"}]}
                    result["isError"] = True
            elif tool_name == "probe_network":
                import urllib.request as _request

                url = arguments.get("url", "")
                try:
                    with _request.urlopen(url, timeout=2) as response:
                        code = response.getcode() or 0
                    result = {"content": [{"type": "text", "text": f"http:{code}"}]}
                except Exception:
                    result = {"content": [{"type": "text", "text": "unreachable"}]}
                    result["isError"] = True
            elif tool_name == "echo":
                value = arguments.get("value", "")
                result = {
                    "content": [{"type": "text", "text": _canonical({"echo": value})}]
                }
            else:
                result = dict(tool.get("result") or {})
                if tool.get("isError"):
                    result["isError"] = True
            respond(request_id, result)
            with state_lock:
                if list_changed_after > 0 and call_count == list_changed_after:
                    notification = _canonical(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/tools/list_changed",
                        }
                    )
                    _write_frame(notification.encode("utf-8"), write_lock)
            return
        respond_error(request_id, -32601, "method not found")

    while True:
        frame = frame_reader.read()
        if frame is None:
            if shutdown_hang_ms > 0:
                import time as _time

                _time.sleep(shutdown_hang_ms / 1000.0)
            return 0
        try:
            raw = frame.decode("utf-8", "strict")
        except UnicodeError:
            continue
        message = _parse_message(raw)
        if message is None:
            continue
        if "id" not in message:
            if message.get("method") == "notifications/initialized":
                initialized = True
            continue
        threading.Thread(
            target=handle_request,
            args=(message,),
            daemon=True,
        ).start()


if __name__ == "__main__":
    raise SystemExit(main())
