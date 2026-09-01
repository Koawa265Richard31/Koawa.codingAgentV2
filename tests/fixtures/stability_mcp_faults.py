"""Real stdio MCP kill windows and fresh-session recovery evidence."""
from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

from koawa_agent_v2.mcp import McpSession, StdioTransport, spawn_fixture_command
from koawa_agent_v2.telemetry.faults import FAULT_SPECS, NoOpFaultPort
from scripts.stability_benchmark import atomic_json


MCP_POINTS = (
    "s4.mcp.initialize.after_send_before_result",
    "s4.mcp.initialize.after_result_before_commit",
    "s4.mcp.list.after_page_before_cursor",
    "s4.mcp.list.after_catalog_commit",
    "s4.mcp.call.after_send_before_result",
    "s4.mcp.call.after_result_before_ledger",
    "s4.mcp.refresh.after_list_before_publish",
)
_CALL_POINTS = frozenset(MCP_POINTS[4:6])
_REPO = Path(__file__).resolve().parents[2]


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_fixture_process(pid: int) -> None:
    if not process_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _call_records(root: Path) -> list[dict]:
    path = root / "mcp-calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _transport(root: Path) -> StdioTransport:
    return StdioTransport(
        spawn_fixture_command(),
        env={
            "PYTHONPATH": str(_REPO / "src"),
            "KOAWA_MCP_FIXTURE_CALL_MARKER": str(root / "mcp-calls.jsonl"),
        },
        cwd=str(_REPO),
    )


class McpKillPort(NoOpFaultPort):
    def __init__(self, root: Path, point: str):
        self.root = root
        self.point = point
        self.armed = False
        self.transport: StdioTransport | None = None
        self.session: McpSession | None = None

    def hit(self, point, facts):
        super().hit(point, facts)
        if not self.armed or point != self.point:
            return
        assert self.transport is not None and self.session is not None
        if point in _CALL_POINTS:
            deadline = time.monotonic() + 10
            while not _call_records(self.root):
                if time.monotonic() >= deadline:
                    raise AssertionError("mcp external call marker timeout")
                time.sleep(.01)
        catalog = self.session.catalog
        atomic_json(self.root / "ready.json", {
            "point": point,
            "point_class": FAULT_SPECS[point].point_class.value,
            "crash_pid": os.getpid(),
            "child_pid": self.transport.pid,
            "session_state": self.session.state,
            "generation": self.session.generation,
            "catalog_digest": None if catalog is None else catalog.catalog_digest,
            "call_count": len(_call_records(self.root)),
        })
        Event().wait()


def crash_mcp(root: Path, point: str) -> None:
    if point not in MCP_POINTS:
        raise ValueError("unsupported MCP fault point")
    atomic_json(root / "request.json", {"point": point})
    transport = _transport(root)
    port = McpKillPort(root, point)
    session = McpSession("server", transport, fault_port=port)
    port.transport = transport
    port.session = session
    port.armed = point in MCP_POINTS[:4]
    catalog = session.connect()
    if point in _CALL_POINTS:
        port.armed = True
        session.call(catalog.bindings["server__echo"], '{"value":"once"}')
    elif point == MCP_POINTS[6]:
        port.armed = True
        session.refresh()
    raise AssertionError("MCP operation missed kill point")


def recover_mcp(root: Path) -> None:
    marker = json.loads((root / "ready.json").read_text(encoding="utf-8"))
    child_pid = marker["child_pid"]
    deadline = time.monotonic() + 10
    while process_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(.02)
    if process_alive(child_pid):
        raise AssertionError("killed MCP owner left its stdio child alive")

    transport = _transport(root)
    session = McpSession("server", transport)
    catalog = session.connect()
    try:
        records = _call_records(root)
        expected_calls = 1 if marker["point"] in _CALL_POINTS else 0
        if len(records) != expected_calls:
            raise AssertionError("recovery replayed or lost an MCP external call")
        if marker["catalog_digest"] is not None:
            if marker["catalog_digest"] != catalog.catalog_digest:
                raise AssertionError("fresh MCP session rebuilt a different catalog")
    finally:
        session.close()
    atomic_json(root / "recovered.json", {
        "point": marker["point"],
        "old_child_alive": process_alive(child_pid),
        "fresh_generation": catalog.generation,
        "catalog_digest": catalog.catalog_digest,
        "tools": sorted(catalog.bindings),
        "calls": records,
    })
