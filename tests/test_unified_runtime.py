from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from koawa_agent_v2.mcp import McpSession, StdioTransport, spawn_fixture_command
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    OutputKind,
    StreamHeader,
    TurnCompleted,
    TurnStarted,
)
from koawa_agent_v2.runtime import assembly as assembly_module
from koawa_agent_v2.runtime.app import AppRuntime
from koawa_agent_v2.runtime.assembly import RuntimeAssemblyError, assemble_runtime
from koawa_agent_v2.runtime.config import (
    McpServerConfig,
    PolicyConfig,
    ProviderConfig,
    RepositoryTrustMode,
    RuntimeConfig,
    SandboxConfig,
    SandboxRunner,
    TestProfileConfig,
)
from koawa_agent_v2.runtime.unified import UnifiedAgentRuntime


def _git(cwd: Path, *args: str) -> None:
    executable = shutil.which("git")
    if executable is None:
        raise unittest.SkipTest("git is not installed")
    subprocess.run(
        [executable, "-C", str(cwd), *args],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "i1@example.invalid")
    _git(root, "config", "user.name", "I1 Fixture")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "fixture baseline")


def _runtime_config(
    repo: Path, *, mcp_servers: tuple[McpServerConfig, ...] = ()
) -> RuntimeConfig:
    return RuntimeConfig(
        repo=repo,
        db=repo.parent / "agent.sqlite3",
        provider=ProviderConfig(
            base_url="http://127.0.0.1:1/v1",
            api_key_env="I1_TEST_KEY",
            model="test-model",
        ),
        sandbox=SandboxConfig(
            runner=SandboxRunner.HOST,
            host_trust=RepositoryTrustMode.BUILTIN_FIXTURE,
        ),
        test_profiles=(
            TestProfileConfig(
                "unit",
                (str(Path(sys.executable).resolve()), "-B", "-m", "unittest"),
                timeout_seconds=30,
            ),
        ),
        policy=PolicyConfig(),
        system_prompt="I1 ownership-chain fixture.",
        mcp_servers=mcp_servers,
    )


def _connect_session(server_config: McpServerConfig):
    """Connect one real fixture session through the possibly patched transport.

    Uses assembly_module.StdioTransport so the test can observe created
    transports and their close() order.
    """
    transport = assembly_module.StdioTransport(
        server_config.command,
        env=dict(server_config.environment),
        cwd=str(server_config.cwd) if server_config.cwd is not None else None,
    )
    session = McpSession(
        server_config.server_id,
        transport,
        request_timeout=server_config.request_timeout_seconds,
    )
    session.connect()
    return session


def _header(request: ModelRequest, response_id: str, sequence: int) -> StreamHeader:
    return StreamHeader(
        request.model_turn_id,
        request.provider,
        response_id,
        sequence,
        sequence,
    )


def _text_stream(request: ModelRequest, text: str) -> tuple[ModelStreamEvent, ...]:
    item = AssistantTextItem(0, "i1-final", text)
    turn = ModelTurn(
        request.model_turn_id,
        request.provider,
        request.model,
        "r1",
        (item,),
        FinishReason.STOP,
    )
    return (
        TurnStarted(_header(request, "r1", 0), request.model),
        ItemStarted(_header(request, "r1", 1), 0, item.item_id, OutputKind.ASSISTANT_TEXT),
        ItemCompleted(_header(request, "r1", 2), item),
        TurnCompleted(_header(request, "r1", 3), turn),
    )


class _TextModel:
    """Deterministic text-only model: one assistant answer per turn."""

    def __init__(self) -> None:
        self.rounds = 0

    def stream(self, request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        self.rounds += 1
        return _text_stream(request, f"answer {self.rounds}")


class UnifiedRuntimeTest(unittest.TestCase):
    def test_unified_flow_wires_context_subagent_compaction_trace(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo = root / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        (repo / "src.py").write_text(
            "def locate():\n    return 'target'\n" * 5, encoding="utf-8"
        )
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "base")

        runtime = UnifiedAgentRuntime(db=root / "u.sqlite3", repo=repo)
        result = runtime.execute("locate")

        self.assertEqual("completed", result.turn_status)
        self.assertIn("src.py", result.context_items)
        self.assertEqual(("completed",), result.child_states)
        self.assertIn("[untrusted-model-summary]", result.compacted)
        self.assertIn("user_goal=locate", result.compacted)
        self.assertIn("subagent", result.trace_streams)
        self.assertIn("model", result.trace_streams)


class AssemblyOwnershipTest(unittest.TestCase):
    """I1 (doc §3.6/§3.7): idempotent close and reverse-order teardown."""

    def _git_repo(self) -> Path:
        temporary = tempfile.TemporaryDirectory(prefix="koawa-i1-")
        self.addCleanup(temporary.cleanup)
        repo = Path(temporary.name) / "repo"
        repo.mkdir()
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        _init_repo(repo)
        return repo

    def _patch_transport(self):
        """Record StdioTransport instances and their close() order."""
        recorded: list[StdioTransport] = []
        close_order: list[StdioTransport] = []
        original = assembly_module.StdioTransport

        class _RecordingTransport(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                recorded.append(self)

            def close(self):
                close_order.append(self)
                super().close()

        assembly_module.StdioTransport = _RecordingTransport
        self.addCleanup(setattr, assembly_module, "StdioTransport", original)
        return recorded, close_order

    def test_assembly_failure_closes_every_started_session(self) -> None:
        repo = self._git_repo()
        good = McpServerConfig(
            server_id="good",
            command=tuple(spawn_fixture_command()),
            cwd=Path(repo).parent,
        )
        bad = McpServerConfig(
            server_id="bad",
            command=(str(Path(repo).parent / "no-such-mcp-server.exe"),),
        )
        config = _runtime_config(repo, mcp_servers=(good, bad))

        recorded, close_order = self._patch_transport()
        with self.assertRaises(RuntimeAssemblyError) as raised:
            assemble_runtime(config, model_client=_TextModel())
        self.assertEqual("runtime_assembly_failed", raised.exception.code)
        # Two transports were created; only the first server connected, so the
        # mid-assembly failure must close exactly that one.  The second
        # transport never opened a process: under the I1 transport contract
        # (impl doc 3.5) failed-open is terminal (FAILED -> closed is True), so
        # both transports are observed as closed but close() was called only
        # for the connected one.
        self.assertEqual(2, len(recorded))
        self.assertEqual([recorded[0]], close_order)
        self.assertTrue(recorded[0].closed)
        self.assertTrue(recorded[1].closed)

    def test_assembled_runtime_close_is_reverse_ordered_and_idempotent(self) -> None:
        # Full assemble_runtime() with configured MCP servers cannot currently
        # reach AgentLoop (composite_registry.CompositeToolRegistry is not a
        # CompletionGate; I6 replaces the owner).  So attach two real connected
        # fixture sessions to a no-MCP assembled runtime via dataclasses.replace
        # and exercise AssembledRuntime.close()/__enter__/__exit__ directly.
        repo = self._git_repo()
        config = _runtime_config(repo)
        base = assemble_runtime(config, model_client=_TextModel())
        base_dir = Path(repo).parent
        first = McpServerConfig(
            server_id="first",
            command=tuple(spawn_fixture_command()),
            cwd=base_dir,
        )
        second = McpServerConfig(
            server_id="second",
            command=tuple(spawn_fixture_command()),
            cwd=base_dir,
        )

        recorded, close_order = self._patch_transport()
        session_first = _connect_session(first)
        session_second = _connect_session(second)
        self.assertEqual(2, len(recorded))

        assembled = replace(
            base,
            mcp_sessions=(
                (first, session_first, session_first.catalog),
                (second, session_second, session_second.catalog),
            ),
        )
        self.assertEqual("first", assembled.mcp_sessions[0][1].server_id)
        self.assertEqual("second", assembled.mcp_sessions[1][1].server_id)
        self.assertFalse(recorded[0].closed)
        self.assertFalse(recorded[1].closed)

        with assembled:
            pass
        # __exit__ closed both sessions in reverse assembly order.
        self.assertEqual([recorded[1], recorded[0]], close_order)
        self.assertTrue(recorded[0].closed)
        self.assertTrue(recorded[1].closed)

        # Idempotent: a second close() is a no-op.
        assembled.close()
        self.assertEqual([recorded[1], recorded[0]], close_order)
        base.close()

    def test_app_runtime_close_is_idempotent_and_supports_context_manager(self) -> None:
        repo = self._git_repo()
        config = _runtime_config(repo)

        app = AppRuntime(config, model_client=_TextModel())
        app.close()
        app.close()  # second close must be a no-op
        app.assembled.close()  # assembled-level idempotency too

        calls: list[int] = []
        original_close = AppRuntime.close

        def spied_close(self):
            calls.append(1)
            return original_close(self)

        AppRuntime.close = spied_close
        self.addCleanup(setattr, AppRuntime, "close", original_close)

        with AppRuntime(config, model_client=_TextModel()) as managed:
            self.assertIsInstance(managed, AppRuntime)
        self.assertEqual(1, len(calls))  # __exit__ closed exactly once
        managed.close()  # close after __exit__ is still safe

    def test_app_runtime_close_after_conversational_turn(self) -> None:
        repo = self._git_repo()
        config = _runtime_config(repo)
        app = AppRuntime(config, model_client=_TextModel())
        outcome = app.chat("hello")
        self.assertTrue(outcome.ok)
        app.close()
        app.close()  # no-op after a worked lifecycle


if __name__ == "__main__":
    unittest.main()
