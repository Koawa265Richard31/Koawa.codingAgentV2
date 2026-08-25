"""D16 interactive session: bounded history projection, compaction, chat CLI layer."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.control.event_store import WrongExpectedVersion
from koawa_agent_v2.model.protocol import (
    AssistantMessage,
    AssistantTextItem,
    FinishReason,
    UserMessage,
    ItemCompleted,
    ItemStarted,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    OutputKind,
    StreamHeader,
    ToolCallItem,
    TurnCompleted,
    TurnStarted,
)
from koawa_agent_v2.policy import Decision
from koawa_agent_v2.runtime.app import AppRuntime
from koawa_agent_v2.runtime.config import (
    PolicyConfig,
    ProviderConfig,
    RepositoryTrustMode,
    RuntimeConfig,
    SandboxConfig,
    SandboxRunner,
    TestProfileConfig,
)
from koawa_agent_v2.runtime.session import (
    SessionHistory,
    SessionHistoryError,
    SessionHistoryLimits,
    SessionTurn,
)


def _git(root: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    if executable is None:
        raise unittest.SkipTest("git is not installed")
    subprocess.run(
        (executable, "-C", str(root), *arguments),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "d16@example.invalid")
    _git(root, "config", "user.name", "D16 Fixture")
    _git(root, "config", "core.fsmonitor", "false")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "core.filemode", "false")
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "fixture baseline")


def _header(request: ModelRequest, response_id: str, sequence: int) -> StreamHeader:
    return StreamHeader(
        request.model_turn_id,
        request.provider,
        response_id,
        sequence,
        sequence,
    )


def _stream(
    request: ModelRequest,
    item: ToolCallItem | AssistantTextItem,
    finish: FinishReason,
    response_id: str,
) -> tuple[ModelStreamEvent, ...]:
    kind = (
        OutputKind.TOOL_CALL
        if isinstance(item, ToolCallItem)
        else OutputKind.ASSISTANT_TEXT
    )
    started = (
        ItemStarted(
            _header(request, response_id, 1),
            0,
            item.item_id,
            kind,
            item.call_id,
            item.name,
        )
        if isinstance(item, ToolCallItem)
        else ItemStarted(_header(request, response_id, 1), 0, item.item_id, kind)
    )
    turn = ModelTurn(
        request.model_turn_id,
        request.provider,
        request.model,
        response_id,
        (item,),
        finish,
    )
    return (
        TurnStarted(_header(request, response_id, 0), request.model),
        started,
        ItemCompleted(_header(request, response_id, 2), item),
        TurnCompleted(_header(request, response_id, 3), turn),
    )


def _call(name: str, arguments: dict[str, object], call_id: str) -> ToolCallItem:
    return ToolCallItem(
        0,
        f"item-{call_id}",
        call_id,
        name,
        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    )


class _ChatModel:
    """Two-round scripted model: first answer, then answer and record history."""

    def __init__(self) -> None:
        self.round = 0
        self.requests: list[ModelRequest] = []
        self.saw_history = False

    def stream(self, request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        self.requests.append(request)
        self.round += 1
        if self.round == 1:
            return _stream(
                request,
                AssistantTextItem(0, "chat-text:1", "first answer"),
                FinishReason.STOP,
                "r1",
            )
        self.saw_history = any(
            isinstance(item, AssistantMessage) and item.item.text == "first answer"
            for item in request.input_items
        )
        return _stream(
            request,
            AssistantTextItem(0, "chat-text:2", "second answer"),
            FinishReason.STOP,
            "r2",
        )


class _TraceModel:
    """Round 1: one successful read_file tool call; round 2: text answer."""

    def __init__(self) -> None:
        self.round = 0

    def stream(self, request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        self.round += 1
        if self.round == 1:
            return _stream(
                request,
                _call(
                    "read_file",
                    {"path": "calc.py", "start_line": 1, "max_lines": 5},
                    "call-trace",
                ),
                FinishReason.TOOL_CALLS,
                "t1",
            )
        return _stream(
            request,
            AssistantTextItem(0, "chat-text:t", "read it"),
            FinishReason.STOP,
            "t2",
        )


class _ApprovalModel:
    """Tool call first (triggers durable approval), then a final text answer."""

    def __init__(self) -> None:
        self.round = 0

    def stream(self, request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        self.round += 1
        if self.round == 1:
            return _stream(
                request,
                _call("apply_patch", {"patch_json": "{}"}, "patch"),
                FinishReason.TOOL_CALLS,
                "a1",
            )
        return _stream(
            request,
            AssistantTextItem(0, "chat-text:a", "approved and done"),
            FinishReason.STOP,
            "a2",
        )


def _runtime_config(
    repo: Path,
    *,
    patch_decision: Decision = Decision.ALLOW,
) -> RuntimeConfig:
    return RuntimeConfig(
        repo=repo,
        db=repo.parent / "agent.sqlite3",
        provider=ProviderConfig(
            base_url="http://127.0.0.1:1/v1",
            api_key_env="D16_TEST_KEY",
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
        policy=PolicyConfig(patch_decision=patch_decision),
        system_prompt="You are KoawaAgent V2 in an interactive session.",
    )


class SessionHistoryTest(unittest.TestCase):
    def test_whitelist_projection_only_user_and_final_text(self) -> None:
        history = SessionHistory(provider="siliconflow")
        history.append(SessionTurn(user_input="read the repo", final_text="done reading"))
        history.append(
            SessionTurn(
                user_input="change add()",
                final_text=None,
                turn_id=uuid4(),
                status="failed",
                error="d2:model_client_failed",
            )
        )
        items = history.context_items()
        self.assertEqual(3, len(items))
        self.assertIsInstance(items[0], UserMessage)
        self.assertEqual("read the repo", items[0].content)
        self.assertIsInstance(items[1], AssistantMessage)
        self.assertEqual("done reading", items[1].item.text)
        # interrupted turn leaves no assistant echo, only its user request
        self.assertIsInstance(items[2], UserMessage)
        self.assertEqual("change add()", items[2].content)

    def test_truncation_drops_oldest_turns_and_chars(self) -> None:
        history = SessionHistory(
            provider="siliconflow",
            limits=SessionHistoryLimits(max_turns=2, max_chars=1_000_000),
        )
        for index in range(4):
            history.append(
                SessionTurn(
                    user_input=f"turn {index}",
                    final_text=f"answer {index}",
                )
            )
        items = history.context_items()
        self.assertEqual(4, len(items))  # last two turns x 2 items
        self.assertEqual("turn 2", items[0].content)
        self.assertEqual("turn 3", items[2].content)

        tiny = SessionHistory(
            provider="siliconflow",
            limits=SessionHistoryLimits(max_turns=100, max_chars=20),
        )
        for index in range(3):
            tiny.append(
                SessionTurn(
                    user_input=f"turn {index}",
                    final_text="x" * 50,
                )
            )
        # 20-char budget cannot fit any pair; keep newest that fits (none) -> 0
        self.assertEqual((), tiny.context_items())

    def test_compaction_keeps_authoritative_and_marker(self) -> None:
        summarized: list[str] = []
        history = SessionHistory(
            provider="siliconflow",
            limits=SessionHistoryLimits(max_turns=1, compact_min_turns=2),
            summarize=lambda text: summarized.append(text) or "summary of the past",
        )
        for index in range(4):
            history.append(
                SessionTurn(
                    user_input=f"request {index}",
                    final_text=f"answer {index}",
                    turn_id=uuid4(),
                    status="completed",
                )
            )
        items = history.context_items()
        # compaction block + the single retained turn (user + assistant)
        self.assertEqual(3, len(items))
        block = items[0]
        self.assertIsInstance(block, UserMessage)
        self.assertIn("# compacted conversation history (authoritative)", block.content)
        self.assertIn("[untrusted-session-summary]", block.content)
        self.assertIn("status=completed", block.content)
        self.assertEqual(1, len(summarized))

    def test_compaction_failure_falls_back_to_authoritative(self) -> None:
        def broken(_: str) -> str:
            raise RuntimeError("summarizer down")

        history = SessionHistory(
            provider="siliconflow",
            limits=SessionHistoryLimits(max_turns=1, compact_min_turns=2),
            summarize=broken,
        )
        for index in range(4):
            history.append(
                SessionTurn(
                    user_input=f"request {index}",
                    final_text=f"answer {index}",
                    turn_id=uuid4(),
                    status="completed",
                )
            )
        items = history.context_items()
        self.assertIn("# compacted conversation history (authoritative)", items[0].content)
        self.assertNotIn("[untrusted-session-summary]", items[0].content)

    def test_from_thread_unknown_thread_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-d16-") as temporary:
            store_path = Path(temporary) / "agent.sqlite3"
            from koawa_agent_v2.control.sqlite_store import SqliteEventStore
            from koawa_agent_v2.control.runtime import ThreadRuntime

            store = SqliteEventStore(store_path)
            runtime = ThreadRuntime(store, actor="d16-test")
            with self.assertRaises(SessionHistoryError) as raised:
                SessionHistory.from_thread(
                    store,
                    runtime,
                    uuid4(),
                    provider="siliconflow",
                )
            self.assertEqual("thread_not_found", raised.exception.code)


class InteractiveSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="koawa-d16-chat-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)

    def test_chat_second_turn_sees_first_turn(self) -> None:
        model = _ChatModel()
        app = AppRuntime(_runtime_config(self.repo), model_client=model)
        history = SessionHistory(provider=app.config.provider.provider)
        first = app.chat("hello", history=history)
        self.assertTrue(first.ok, first.payload)
        self.assertEqual("first answer", first.payload["final_text"])
        thread_id = UUID(first.payload["thread_id"])
        turn_id = UUID(first.payload["turn_id"])
        history.append(
            SessionTurn(
                user_input="hello",
                final_text="first answer",
                turn_id=turn_id,
                status=first.payload["status"],
            )
        )
        second = app.chat("what did I say?", thread_id=thread_id, history=history)
        self.assertTrue(second.ok, second.payload)
        self.assertEqual("second answer", second.payload["final_text"])
        self.assertTrue(model.saw_history)

    def test_chat_invalid_thread_id_fails_cleanly(self) -> None:
        app = AppRuntime(_runtime_config(self.repo), model_client=_ChatModel())
        outcome = app.chat("hello", thread_id="not-a-uuid")
        self.assertFalse(outcome.ok)
        self.assertEqual("invalid_thread_id", outcome.code)

    def test_chat_event_sink_sees_tool_calls(self) -> None:
        """事件通道把模型的工具调用实时暴露给 CLI（轨迹显示的数据源）。"""
        model = _TraceModel()
        app = AppRuntime(_runtime_config(self.repo), model_client=model)
        seen: list[str] = []

        def sink(event: object) -> None:
            if (
                isinstance(event, ItemCompleted)
                and getattr(event.item, "kind", None) is OutputKind.TOOL_CALL
            ):
                seen.append(event.item.name)

        outcome = app.chat(
            "read calc.py",
            history=SessionHistory(provider="test"),
            event_sink=sink,
        )
        self.assertTrue(outcome.ok, outcome.payload)
        self.assertEqual(["read_file"], seen)

    def test_cli_tool_trace_helper_prints_ok_lines(self) -> None:
        """CLI 工具轨迹：成功的工具执行打印 ✓ 行。"""
        import contextlib
        import io

        from koawa_agent_v2.runtime.cli import _print_tool_trace, _store_position

        model = _TraceModel()
        app = AppRuntime(_runtime_config(self.repo), model_client=model)
        before = _store_position(app)
        calls: dict[str, str] = {}

        def sink(event: object) -> None:
            if (
                isinstance(event, ItemCompleted)
                and getattr(event.item, "kind", None) is OutputKind.TOOL_CALL
            ):
                calls[event.item.call_id] = event.item.name

        app.chat(
            "read calc.py",
            history=SessionHistory(provider="test"),
            event_sink=sink,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _print_tool_trace(app, before, calls)
        self.assertIn("✓ read_file", buffer.getvalue())

    def test_approval_ask_then_approve_resumes_to_completed(self) -> None:
        model = _ApprovalModel()
        app = AppRuntime(
            _runtime_config(self.repo, patch_decision=Decision.ASK),
            model_client=model,
        )
        outcome = app.chat("please change the code", history=SessionHistory(provider="test"))
        self.assertEqual("waiting_for_approval", outcome.payload.get("status"))
        pending = app.pending_approvals().payload["pending_approvals"]
        self.assertTrue(pending)
        resolved = app.resolve_approval(pending[0]["request_id"], True)
        self.assertTrue(resolved.ok, resolved.payload)
        self.assertEqual("completed", resolved.payload["resume"]["status"])

    def test_stale_thread_version_is_rejected(self) -> None:
        app = AppRuntime(_runtime_config(self.repo), model_client=_ChatModel())
        first = app.chat("hello")
        thread_id = UUID(first.payload["thread_id"])
        stale = app.assembled.runtime.get_thread(thread_id).version
        second = app.chat("again", thread_id=thread_id)
        self.assertTrue(second.ok)
        with self.assertRaises(WrongExpectedVersion):
            app.assembled.runtime.create_turn(
                thread_id,
                "stale write",
                expected_thread_version=stale,
            )

    def test_in_process_history_matches_restart_from_thread(self) -> None:
        """Same-process second-turn history equals a restart from_thread (I4)."""
        from koawa_agent_v2.runtime.session import SessionTurn

        canary = "sk-abc1234567890xyz"
        model = _ChatModel()
        app = AppRuntime(_runtime_config(self.repo), model_client=model)
        history = SessionHistory(provider=app.config.provider.provider)
        first = app.chat("please handle " + canary, history=history)
        self.assertTrue(first.ok, first.payload)
        thread_id = UUID(first.payload["thread_id"])
        turn_id = UUID(first.payload["turn_id"])
        history.append(
            SessionTurn(
                user_input="please handle " + canary,
                final_text=first.payload["final_text"],
                turn_id=turn_id,
                status=first.payload["status"],
            )
        )
        # The in-process history already holds the canonical (redacted) input;
        # a restarted app rebuilding the same thread must be byte-identical.
        restarted_app = AppRuntime(_runtime_config(self.repo), model_client=_ChatModel())
        from_thread = SessionHistory.from_thread(
            restarted_app.assembled.store,
            restarted_app.assembled.runtime,
            thread_id,
            provider=restarted_app.config.provider.provider,
        )
        def projected(items: tuple) -> tuple:
            # model_turn_id is a fresh uuid for every in-memory projection, so
            # the durable identity compare is the content projection only.
            return tuple(
                (type(item).__name__, getattr(item, "content", None))
                for item in items
            )

        self.assertEqual(
            len(history.context_items()),
            len(from_thread.context_items()),
        )
        self.assertEqual(
            projected(history.context_items()),
            projected(from_thread.context_items()),
        )
        # The raw canary must never appear in the rebuilt context items.
        for item in from_thread.context_items():
            if isinstance(item, UserMessage):
                self.assertNotIn(canary, item.content)

    def test_chat_persists_canonical_user_input_across_restart(self) -> None:
        """A credential-shaped chat message is canonical before first model call."""
        canary = "sk-abc1234567890xyz"
        model = _ChatModel()
        app = AppRuntime(_runtime_config(self.repo), model_client=model)
        outcome = app.chat("please fix " + canary)
        self.assertTrue(outcome.ok, outcome.payload)
        turn_id = UUID(outcome.payload["turn_id"])
        state = app.assembled.runtime.get_turn(turn_id)
        self.assertNotIn(canary, state.user_input)
        self.assertIn("[REDACTED]", state.user_input)
        # The scripted model saw the canonical text as its first user message.
        request = model.requests[0]
        content = request.input_items[-1].content
        self.assertNotIn(canary, content)
        self.assertIn("[REDACTED]", content)

    def test_repo_override_applies_before_assembly(self) -> None:
        other = self.root / "other-repo"
        other.mkdir()
        _init_repo(other)
        config_path = self.root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "repo": str(self.repo),
                    "db": str(self.root / "agent.sqlite3"),
                    "provider": {
                        "base_url": "https://api.siliconflow.cn/v1",
                        "api_key_env": "SF_CodingAgentTestKey",
                        "model": "Qwen/Qwen3.5-35B-A3B",
                    },
                    "sandbox": {"runner": "host", "host_trust": "builtin_fixture"},
                    "test_profiles": [
                        {
                            "profile_id": "unit",
                            "argv": [str(Path(sys.executable).resolve()), "-B"],
                        }
                    ],
                    "policy": {"patch_decision": "allow"},
                }
            ),
            encoding="utf-8",
        )
        app = AppRuntime.from_config_file(
            config_path,
            repo_override=str(other),
            model_client=_ChatModel(),
        )
        self.assertEqual(other.resolve(), app.config.repo)


if __name__ == "__main__":
    unittest.main()
