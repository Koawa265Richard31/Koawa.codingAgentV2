"""D25 G3/G4 production-equivalent protocol and assembly evidence.

G3 uses real local subprocesses for protocol/lifecycle fault injection.  The
process boundary is real, but these tests do not claim Docker isolation.  G4
uses the real runtime assembly, launcher, StdioTransport, McpSession, verified
registry, policy and ledger; only the Docker adapter is a deterministic local
equivalent so the suite remains runnable when Docker is unavailable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.mcp.connection_manager import McpSession, McpSessionError
from koawa_agent_v2.mcp.docker_endpoint import DockerAdapter, DockerEndpointError
from koawa_agent_v2.mcp.launcher import SandboxedLauncher
from koawa_agent_v2.mcp.protocol import MCP_PROTOCOL_VERSION
from koawa_agent_v2.mcp.transport import (
    SpawnSpec,
    StdioTransport,
    SystemProcessSpawner,
    TransportMalformedFrame,
)
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.runtime.assembly import (
    assemble_control_plane,
    assemble_execution_plane,
    preflight_execution_activation,
)
from koawa_agent_v2.runtime.config import (
    McpExecutionProfile,
    McpResourceLimits,
    McpServerConfig,
    PolicyConfig,
    ProviderConfig,
    RepositoryTrustMode,
    RuntimeConfig,
    SandboxConfig,
    SandboxRunner,
    TestProfileConfig,
)
from koawa_agent_v2.policy import Decision
from koawa_agent_v2.sandbox.docker_primitives import ContainerSpec
from koawa_agent_v2.sandbox.runtime import SandboxAllocationStore


REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "src" / "koawa_agent_v2" / "mcp" / "fixture_server.py"
DIGEST = "sha256:" + "a" * 64


def _fixture_command() -> tuple[str, ...]:
    from koawa_agent_v2.mcp import spawn_fixture_command

    return tuple(spawn_fixture_command())


class G3ProtocolLifecycleTest(unittest.TestCase):
    """Faults not covered by the D10 unit matrix at a real process boundary."""

    def _transport(self, *, extra_env: dict[str, str] | None = None, shutdown=5.0):
        env = {} if extra_env is None else dict(extra_env)
        return StdioTransport(
            _fixture_command(),
            env=env,
            cwd=str(REPO),
            process_start_timeout_seconds=5.0,
            shutdown_timeout_seconds=shutdown,
        )

    def test_invalid_json_body_from_real_process_is_observable(self) -> None:
        script = (
            "import sys; body=b'not-json'; "
            "sys.stdout.buffer.write(b'Content-Length: 8\\r\\n\\r\\n'+body); "
            "sys.stdout.buffer.flush()"
        )
        transport = StdioTransport(
            (sys.executable, "-c", script), env={},
            process_start_timeout_seconds=2.0,
        )
        transport.open()
        self.addCleanup(transport.close)
        with self.assertRaises(TransportMalformedFrame) as raised:
            transport.read(timeout=3.0)
        self.assertEqual("malformed_json", raised.exception.code)

    def test_duplicate_response_is_counted_after_first_response_completes(self) -> None:
        # A real child sends the same response twice.  The first response must
        # complete the call; the duplicate has no authority to complete a new
        # request and is retained only as bounded diagnostic state.
        script = r'''
import json, sys
def read():
    header=b""
    while True:
        line=sys.stdin.buffer.readline()
        if not line: return None
        if line == b"\r\n": break
        header += line
    return json.loads(sys.stdin.buffer.read(int(header.split(b":",1)[1])))
def send(value):
    body=json.dumps(value,separators=(",",":"),sort_keys=True).encode()
    sys.stdout.buffer.write(b"Content-Length: "+str(len(body)).encode()+b"\r\n\r\n"+body)
    sys.stdout.buffer.flush()
while True:
    request=read()
    if request is None: break
    method=request.get("method")
    ident=request.get("id")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":ident,"result":{"protocolVersion":"2025-06-18"}})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":ident,"result":{"tools":[{"name":"echo","description":"d","inputSchema":{"type":"object","properties":{"value":{"type":"string","minLength":0,"maxLength":20}},"required":["value"],"additionalProperties":False}}]}})
    elif method == "tools/call":
        value={"jsonrpc":"2.0","id":ident,"result":{"content":[{"type":"text","text":"ok"}]}}
        send(value)
        send(value)
'''
        transport = StdioTransport(
            (sys.executable, "-c", script), env={},
            process_start_timeout_seconds=2.0,
        )
        session = McpSession("duplicate", transport, request_timeout=2.0)
        self.addCleanup(session.close)
        catalog = session.connect()
        result = session.call(catalog.bindings["duplicate__echo"], '{"value":"x"}')
        self.assertEqual("ok", result.content)
        deadline = time.monotonic() + 2.0
        while session.unknown_response_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(session.unknown_response_count, 1)

    def test_real_start_hang_fails_and_reaps_transport(self) -> None:
        transport = self._transport(
            extra_env={"KOAWA_MCP_FIXTURE_INIT_DELAY_MS": "1500"},
            shutdown=1.0,
        )
        session = McpSession(
            "startup_hang", transport,
            initialize_timeout_seconds=0.2,
            tools_list_timeout_seconds=1.0,
            tool_call_timeout_seconds=1.0,
        )
        with self.assertRaises(McpSessionError) as raised:
            session.connect()
        self.assertEqual("mcp_request_timeout", raised.exception.code)
        self.assertEqual(McpSession.FAILED, session.state)
        report = transport.close_report()
        self.assertIsNotNone(report)
        self.assertFalse(report.uncertain)
        self.assertTrue(report.process_exited)

    def test_real_close_hang_is_bounded_and_reported_unknown(self) -> None:
        transport = self._transport(
            extra_env={"KOAWA_MCP_FIXTURE_SHUTDOWN_HANG_MS": "1500"},
            shutdown=0.2,
        )
        session = McpSession("shutdown_hang", transport, request_timeout=2.0)
        session.connect()
        owned = transport._owned  # retain an exact process handle for cleanup
        try:
            session.close()
            self.assertEqual(McpSession.CLOSED, session.state)
            report = transport.close_report()
            self.assertIsNotNone(report)
            # The short shared deadline intentionally cannot prove this
            # cooperative shutdown; UNKNOWN is the only honest result.
            self.assertFalse(report.process_exited)
            self.assertTrue(report.uncertain)
        finally:
            if owned is not None and owned.poll() is None:
                owned.kill_tree(deadline=time.monotonic() + 5.0)
            if owned is not None:
                owned.close_handles()

    def test_close_cancels_inflight_call_at_real_process_boundary(self) -> None:
        transport = self._transport(
            extra_env={"KOAWA_MCP_FIXTURE_CALL_DELAY_MS": "2000"},
            shutdown=1.0,
        )
        session = McpSession("cancel_call", transport, request_timeout=5.0)
        self.addCleanup(session.close)
        catalog = session.connect()
        binding = catalog.bindings["cancel_call__slow"]
        result: list[object] = []

        def invoke() -> None:
            try:
                result.append(session.call(binding, "{}", timeout=5.0))
            except Exception as error:  # expected close fence
                result.append(error)

        thread = threading.Thread(target=invoke)
        thread.start()
        time.sleep(0.15)
        session.close()
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(result))
        self.assertIsInstance(result[0], McpSessionError)
        self.assertEqual("mcp_session_closed", result[0].code)


class _LocalAttach:
    """Attach-process facade used by the production Docker endpoint."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self._popen = popen

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def stdin(self):
        return self._popen.stdin

    @property
    def stdout(self):
        return self._popen.stdout

    @property
    def stderr(self):
        return self._popen.stderr

    def poll(self):
        return self._popen.poll()

    def wait_bounded(self, deadline: float) -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DockerEndpointError("mcp_container_wait_timeout")
        try:
            return self._popen.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise DockerEndpointError("mcp_container_wait_timeout") from None

    def kill(self) -> None:
        try:
            self._popen.kill()
            self._popen.wait(timeout=2.0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass

    def close_handles(self) -> None:
        for stream in (self._popen.stdin, self._popen.stdout, self._popen.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


class _LocalDockerEquivalent(DockerAdapter):
    """Real subprocess behind the endpoint, deterministic Docker inspect facts."""

    def __init__(self) -> None:
        self.container_id = "d" * 64
        self.spec = None
        self.created: list[tuple[str, ...]] = []
        self.attach: _LocalAttach | None = None
        self.removed = False

    def create(self, docker, arguments, *, timeout):
        del docker, timeout
        self.created.append(arguments)
        return self.container_id

    def inspect(self, docker, container_id, *, timeout):
        del docker, container_id, timeout
        labels: dict[str, str] = {}
        args = self.created[-1]
        for index, value in enumerate(args):
            if value == "--label":
                name, label_value = args[index + 1].split("=", 1)
                labels[name] = label_value
        spec = self.spec
        limits = spec.resource_limits
        return {
            "Id": self.container_id,
            "Config": {
                "Image": spec.image_id,
                "Tty": False,
                "OpenStdin": True,
                "Entrypoint": [spec.command[0]],
                "Cmd": list(spec.command[1:]),
                "WorkingDir": spec.container_working_directory,
                "User": "65532:65532",
                "Labels": labels,
            },
            "HostConfig": {
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Memory": limits.memory_bytes,
                "MemorySwap": limits.memory_bytes,
                "NanoCpus": int(limits.cpus * 1_000_000_000),
                "PidsLimit": limits.pids,
                "Mounts": [],
            },
        }

    def start_attach(self, docker, container_id):
        del docker, container_id
        env = {"LANG": "C", "LC_ALL": "C"}
        if os.name == "nt":
            env["SystemRoot"] = os.environ["SystemRoot"]
            env["COMSPEC"] = os.environ.get("COMSPEC", str(Path(env["SystemRoot"]) / "System32" / "cmd.exe"))
        popen = subprocess.Popen(
            (sys.executable, str(FIXTURE)),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.attach = _LocalAttach(popen)
        return self.attach

    def stop_and_remove(self, docker, container_id, *, stop_timeout_seconds, cli_timeout_seconds):
        del docker, container_id, stop_timeout_seconds, cli_timeout_seconds
        self.removed = True
        if self.attach is not None and self.attach.poll() is None:
            self.attach.kill()


class _NoopModel:
    """Only used to satisfy assembly's model-client port; G4 calls the ledger directly."""

    def stream(self, request):
        del request
        raise AssertionError("G4 must exercise the assembled executor directly")


def _git_init(path: Path) -> None:
    subprocess.run(("git", "-C", str(path), "init", "-q"), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(path), "config", "user.email", "g4@example.invalid"), check=True)
    subprocess.run(("git", "-C", str(path), "config", "user.name", "G4"), check=True)
    subprocess.run(("git", "-C", str(path), "add", "--all"), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(path), "commit", "-qm", "fixture"), check=True, capture_output=True)


class G4AssemblyLifecycleTest(unittest.TestCase):
    """The complete activated sandboxed MCP chain through the real assembly."""

    def test_assembly_transport_registry_policy_ledger_and_release(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-d25-g4-") as temporary:
            base = Path(temporary)
            repo = base / "repo"
            repo.mkdir()
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            _git_init(repo)
            server = McpServerConfig(
                server_id="assembly",
                command=("/usr/local/bin/node", "server.js"),
                execution_profile=McpExecutionProfile.SANDBOXED,
                image_id=DIGEST,
                resource_limits=McpResourceLimits(),
                container_working_directory="/work",
                decision=Decision.ALLOW,
            )
            config = RuntimeConfig(
                repo=repo,
                db=base / "state.sqlite3",
                provider=ProviderConfig(
                    base_url="http://127.0.0.1:1/v1",
                    api_key_env="G4_UNUSED_KEY",
                    model="test-model",
                ),
                sandbox=SandboxConfig(
                    runner=SandboxRunner.HOST,
                    host_trust=RepositoryTrustMode.BUILTIN_FIXTURE,
                ),
                test_profiles=(
                    TestProfileConfig("unit", (str(Path(sys.executable).resolve()), "-B")),
                ),
                policy=PolicyConfig(),
                system_prompt="G4 assembly fixture.",
                mcp_servers=(server,),
            )
            control = assemble_control_plane(config, config_base_dir=repo)
            self.addCleanup(control.close)
            plan = preflight_execution_activation(control, command_context="g4")
            adapter = _LocalDockerEquivalent()

            def launcher_builder(activation, server_config, launch_plan):
                adapter.spec = server_config
                return SandboxedLauncher(
                    activation,
                    launch_plan,
                    sandbox_store=SandboxAllocationStore(control.store),
                    docker_adapter=adapter,
                    docker_executable=str(Path(sys.executable).resolve()),
                    container_labels=(("koawa.managed", "mcp-sandbox"),),
                )

            assembled = assemble_execution_plane(
                control,
                plan,
                model_client=_NoopModel(),
                launcher_builder=launcher_builder,
            )
            self.addCleanup(assembled.close)
            self.assertEqual(1, len(assembled.mcp_sessions))
            session = assembled.mcp_sessions[0][1]
            self.assertEqual(McpSession.READY, session.state)
            self.assertIn("assembly__echo", {item.name for item in assembled.executor.definitions()})

            thread = assembled.runtime.create_thread("g4")
            turn = assembled.runtime.create_turn(
                thread.thread_id, "invoke", expected_thread_version=thread.version,
            )
            running = assembled.runtime.start_turn(turn.turn_id, turn.version)
            model_turn_id = uuid4()
            call = ToolCallItem(
                0, "item-echo", "call-echo", "assembly__echo", '{"value":"g4-ok"}',
            )
            context = ToolExecutionContext(
                running.current_run_id,
                model_turn_id,
                1,
                ModelCallRef(model_turn_id, call.call_id),
                turn_id=running.turn_id,
                turn_version=running.version,
            )
            result = assembled.executor.execute(call, context=context)
            self.assertFalse(result.is_error)
            self.assertIn('"untrusted_mcp_result":true', result.content)
            # Hardening WP-1: metadata-only receipt - the body ("g4-ok")
            # stays in the durable fact, never model-visible.
            self.assertNotIn("g4-ok", result.content)
            self.assertIn('"result_visibility":"metadata_only"', result.content)

            allocation = next(
                event.payload.get("allocation_id")
                for event in assembled.store.read_all(after_position=0, limit=500)
                if event.event_type == "mcp.process-intended.v1"
            )
            from uuid import UUID

            allocation_id = UUID(allocation)
            assembled.close()
            self.assertEqual(McpSession.CLOSED, session.state)
            self.assertTrue(adapter.removed)
            sandbox = SandboxAllocationStore(assembled.store).load(allocation_id)
            self.assertIsNotNone(sandbox)
            self.assertEqual("released", sandbox.state.value)
            mcp_view = assembled.store.read_stream(StreamId("mcp-allocation", allocation_id))
            self.assertTrue(any(event.event_type == "mcp.process-stopped.v1" for event in mcp_view))
            ledger_events = [
                event.event_type for event in assembled.store.read_all(after_position=0, limit=500)
            ]
            self.assertIn("tool.execution-succeeded.v1", ledger_events)


if __name__ == "__main__":
    unittest.main()
