"""D25 real-Docker lane: create / inspect / stdio / cancel / reap.

Runs the pinned third-party filesystem server inside the sandboxed container
contract and emits a JSON report (doc §8.3 fields).  Same script for the
Windows Docker Desktop lane and the Linux/WSL lane.

    PYTHONPATH=src python -B scripts/d25_lane.py --report d25-lane.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from koawa_agent_v2.mcp.docker_endpoint import launch_container_endpoint  # noqa: E402
from koawa_agent_v2.sandbox.docker_primitives import ContainerSpec  # noqa: E402

IMAGE_ID = "sha256:adcd84ab9f9dc91e5c3eebe9fa32329545f73ab0b891ecd6def4232740cc4300"
NODE = "/usr/local/bin/node"
DIST = "/usr/local/lib/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js"
PROVENANCE = REPO / "tests" / "fixtures" / "d25_mcp_server" / "provenance.json"


def _docker_context() -> str | None:
    try:
        completed = subprocess.run(
            ("docker", "context", "show"),
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _docker_client_version() -> str | None:
    try:
        completed = subprocess.run(
            ("docker", "version", "--format", "{{.Client.Version}}"),
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None


def _docker_server_version() -> str | None:
    try:
        completed = subprocess.run(
            ("docker", "version", "--format", "{{.Server.Version}}"),
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None


def _docker_server_name() -> str | None:
    try:
        completed = subprocess.run(
            ("docker", "info", "--format", "{{.Name}}"),
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None


def _lane_name(explicit: str | None) -> str:
    if explicit:
        return explicit
    if platform.system() == "Windows":
        return "windows-docker"
    release = platform.release().lower()
    if "microsoft" in release or "wsl_interop" in os.environ:
        return "linux-wsl"
    return "linux"


def _git_identity() -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=REPO,
            capture_output=True, text=True, timeout=15, check=False,
        )
        dirty = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=no"), cwd=REPO,
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    commit_id = commit.stdout.strip() or None
    return commit_id, dirty.returncode == 0 and bool(dirty.stdout.strip())


def _runtime_matches_lane(lane: str) -> bool:
    """Prevent an evidence report from being mislabeled by a CLI flag."""
    if lane == "windows-docker":
        return platform.system() == "Windows"
    if platform.system() != "Linux":
        return False
    if lane == "linux-wsl":
        release = platform.release().lower()
        return "microsoft" in release or "wsl_interop" in os.environ
    return True


def _readline(stream, timeout: float) -> str:
    box: queue.Queue = queue.Queue()

    def reader():
        try:
            box.put(stream.readline())
        except Exception as error:
            box.put(error)

    threading.Thread(target=reader, daemon=True).start()
    item = box.get(timeout=timeout)
    if isinstance(item, Exception):
        raise AssertionError(f"stdio_broken: {item}")
    line = item.decode("utf-8", "replace").strip()
    if not line:
        raise AssertionError("stdio_empty_frame")
    return line


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--lane", choices=("windows-docker", "linux-wsl", "linux"),
        help="Evidence lane label; auto-detected when omitted.",
    )
    parser.add_argument(
        "--docker-context",
        help="Docker context to use (also exported as DOCKER_CONTEXT for this run).",
    )
    arguments = parser.parse_args()

    if arguments.docker_context:
        os.environ["DOCKER_CONTEXT"] = arguments.docker_context

    steps: list[dict] = []
    ok = True
    lane = _lane_name(arguments.lane)
    started_at = datetime.now(timezone.utc).isoformat()
    commit_id, dirty = _git_identity()

    def step(name: str, passed: bool, detail: dict | None = None) -> None:
        nonlocal ok
        ok = ok and passed
        steps.append({"step": name, "ok": passed, "detail": detail or {}})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}", flush=True)

    started = time.monotonic()
    step("lane_runtime", _runtime_matches_lane(lane), {"lane": lane})
    if PROVENANCE.is_file():
        provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        step("provenance_pinned", provenance.get("image_id") == IMAGE_ID,
             {"image_id": IMAGE_ID, "npm_version": provenance.get("npm_version")})
    else:
        step("provenance_pinned", False, {"reason": "provenance_missing"})

    spec = ContainerSpec(
        image_id=IMAGE_ID,
        argv=(NODE, DIST, "/data"),
        container_working_directory="/data",
        environment=(),
        cpus=1.0,
        memory_bytes=256 * 1024 * 1024,
        pids_limit=128,
        tmpfs_bytes=32 * 1024 * 1024,
        container_name="koawa-d25-lane",
        labels=(("koawa.managed", "d25-lane"),),
    )
    try:
        endpoint = launch_container_endpoint(
            spec, docker_executable="docker",
            process_start_timeout_seconds=arguments.timeout,
        )
    except Exception as error:
        step("create_inspect_attach", False,
             {"code": getattr(error, "code", str(error)[:120])})
        report = {
            "schema_version": 1, "slice": "D25", "lane": lane,
            "platform": platform.platform(), "python": platform.python_version(),
            "docker_context": _docker_context(),
            "docker_client": _docker_client_version(),
            "docker_server": _docker_server_version(),
            "docker_server_name": _docker_server_name(),
            "commit": commit_id, "dirty": dirty,
            "started_at": started_at,
            "duration_s": round(time.monotonic() - started, 3),
            "ok": False, "steps": steps,
            "passed": sum(item["ok"] for item in steps),
            "failed": sum(not item["ok"] for item in steps),
            "skipped": 0,
        }
        arguments.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 1
    step("create_inspect_attach", True, {"container_id": endpoint.container_id[:12] + "…"})

    def rpc(payload: dict, request_id: int) -> dict:
        assert endpoint.stdin is not None and endpoint.stdout is not None
        body = dict(payload, jsonrpc="2.0", id=request_id)
        endpoint.stdin.write((json.dumps(body) + "\n").encode("utf-8"))
        endpoint.stdin.flush()
        return json.loads(_readline(endpoint.stdout, arguments.timeout))

    try:
        handshake = rpc({"method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "koawa-d25-lane", "version": "0"},
        }}, 1)
        step("stdio_initialize", handshake.get("result", {}).get("serverInfo", {}).get("name") is not None)
        assert endpoint.stdin is not None
        endpoint.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
        endpoint.stdin.flush()
        listing = rpc({"method": "tools/list", "params": {}}, 2)
        names = {tool.get("name") for tool in listing.get("result", {}).get("tools", [])}
        step("stdio_tools_list", {"list_directory", "read_file"} <= names, {"tool_count": len(names)})
        called = rpc({"method": "tools/call", "params": {
            "name": "list_directory", "arguments": {"path": "/data"},
        }}, 3)
        step("stdio_tools_call", "sample.txt" in json.dumps(called.get("result", {})))
    except Exception as error:
        step("stdio", False, {"error": str(error)[:120]})

    # Dispatch a second valid call and deliberately do not consume its
    # response.  Closing/terminating the endpoint immediately after the write
    # exercises cancellation at the real stdio process boundary, rather than
    # recording a synthetic "cancel" success.
    cancel_dispatched = False
    try:
        assert endpoint.stdin is not None
        endpoint.stdin.write((json.dumps({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "/data/sample.txt"}},
        }) + "\n").encode("utf-8"))
        endpoint.stdin.flush()
        cancel_dispatched = True
    except (OSError, ValueError):
        pass
    step("cancel_terminate", cancel_dispatched,
         {"mode": "terminate_tree_with_inflight_call"})
    try:
        endpoint.terminate_tree(deadline=time.monotonic() + 30.0)
        step("terminate_bounded", True)
    except Exception as error:
        step("terminate_bounded", False, {"error": str(error)[:120]})

    probe = subprocess.run(
        ("docker", "container", "inspect", endpoint.container_id),
        capture_output=True, text=True, timeout=15, check=False,
    )
    step("reap_exact", probe.returncode != 0)
    close = getattr(endpoint, "close_handles", None)
    if close:
        close()

    report = {
        "schema_version": 1,
        "slice": "D25",
        "lane": lane,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "docker_context": _docker_context(),
        "docker_client": _docker_client_version(),
        "docker_server": _docker_server_version(),
        "docker_server_name": _docker_server_name(),
        "image_id": IMAGE_ID,
        "commit": commit_id,
        "dirty": dirty,
        "started_at": started_at,
        "duration_s": round(time.monotonic() - started, 3),
        "ok": ok,
        "steps": steps,
        "passed": sum(item["ok"] for item in steps),
        "failed": sum(not item["ok"] for item in steps),
        "skipped": 0,
    }
    arguments.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": ok, "report": str(arguments.report)}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
