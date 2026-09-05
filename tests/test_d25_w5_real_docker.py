"""D25 W5: real-Docker lifecycle against the pinned filesystem MCP server.

Runs the real @modelcontextprotocol/server-filesystem (version pinned in the
image) inside the sandboxed container contract: network=none, zero mounts,
read-only rootfs, non-root, bounded resources.  Skips when Docker is
unavailable; the Windows Docker lane must not skip (doc §8.2).
"""

from __future__ import annotations

import json
import queue
import threading
import unittest
from pathlib import Path

from koawa_agent_v2.mcp.docker_endpoint import launch_container_endpoint
from koawa_agent_v2.sandbox.docker_primitives import ContainerSpec

IMAGE_ID = __import__("os").environ.get(
    "KOAWA_D25_FS_IMAGE",
    "sha256:adcd84ab9f9dc91e5c3eebe9fa32329545f73ab0b891ecd6def4232740cc4300",
)
NODE = "/usr/local/bin/node"
DIST = "/usr/local/lib/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js"
PROVENANCE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "d25_mcp_server" / "provenance.json"


def _docker_ready() -> bool:
    import subprocess

    try:
        completed = subprocess.run(
            ("docker", "info"), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _readline(stream, timeout: float) -> str:
    box: queue.Queue = queue.Queue()

    def reader():
        try:
            box.put(stream.readline())
        except Exception as error:  # pragma: no cover - pipe death
            box.put(error)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        item = box.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError("mcp_response_timeout") from None
    if isinstance(item, Exception):
        raise AssertionError(f"mcp_stream_broken: {item}")
    return item.decode("utf-8", "replace").strip()


class RealFilesystemServerTest(unittest.TestCase):
    def setUp(self) -> None:
        if not _docker_ready():
            self.skipTest("docker_unavailable")
        spec = ContainerSpec(
            image_id=IMAGE_ID,
            argv=(NODE, DIST, "/data"),
            container_working_directory="/data",
            environment=(),
            cpus=1.0,
            memory_bytes=256 * 1024 * 1024,
            pids_limit=128,
            tmpfs_bytes=32 * 1024 * 1024,
            container_name="koawa-d25-it",
            labels=(("koawa.managed", "mcp-test"),),
        )
        self.endpoint = launch_container_endpoint(
            spec,
            docker_executable="docker",
            process_start_timeout_seconds=60.0,
        )

    def tearDown(self) -> None:
        import time

        try:
            self.endpoint.kill_tree(deadline=time.monotonic() + 20.0)
        except Exception:
            pass

    def _rpc(self, payload: dict, timeout: float = 20.0) -> dict:
        assert self.endpoint.stdin is not None
        assert self.endpoint.stdout is not None
        self.endpoint.stdin.write(
            (json.dumps(payload) + "\n").encode("utf-8")
        )
        self.endpoint.stdin.flush()
        line = _readline(self.endpoint.stdout, timeout)
        return json.loads(line)

    def test_initialize_tools_list_call_and_shutdown(self) -> None:
        import time

        initialized = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "koawa-d25", "version": "0"},
                },
            }
        )
        self.assertEqual("2.0", initialized.get("jsonrpc"))
        server_info = initialized.get("result", {}).get("serverInfo", {})
        self.assertTrue(server_info.get("name"))
        # notification: initialized (no response expected)
        assert self.endpoint.stdin is not None
        self.endpoint.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode()
        )
        self.endpoint.stdin.flush()
        listing = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )
        tools = listing.get("result", {}).get("tools", [])
        names = {tool.get("name") for tool in tools}
        self.assertIn("list_directory", names)
        self.assertIn("read_file", names)
        called = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "list_directory",
                    "arguments": {"path": "/data"},
                },
            }
        )
        text = json.dumps(called.get("result", {}))
        self.assertIn("sample.txt", text)
        # isolation negative: a Windows host decoy path is not readable
        decoy = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "read_file",
                    "arguments": {"path": "C:/Windows/win.ini"},
                },
            }
        )
        self.assertTrue(decoy.get("result", {}).get("isError"))
        # graceful shutdown: stdin EOF then bounded stop+remove
        deadline = time.monotonic() + 30.0
        self.endpoint.terminate_tree(deadline=deadline)
        import subprocess

        probe = subprocess.run(
            ("docker", "container", "inspect", self.endpoint.container_id),
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertNotEqual(0, probe.returncode)

    def test_provenance_pinned(self) -> None:
        document = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        self.assertEqual("2026.8.31", document["npm_version"])
        self.assertEqual(
            "7f88dfab06d4521a4bf937dccbaeb79d46a3e717", document["tarball_shasum"]
        )
        self.assertEqual(IMAGE_ID, document["image_id"])


if __name__ == "__main__":
    unittest.main()
