"""D19 session memory enhancement: compaction v2 (files), recall, journal."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

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
    ToolCallItem,
    TurnCompleted,
    TurnStarted,
)
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
    SessionHistoryLimits,
    SessionJournal,
    SessionMemory,
    SessionTurn,
)
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.workspace.content import repository_identity
from koawa_agent_v2.workspace.effects import WorkspaceEffectStore


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
    _git(root, "config", "user.email", "d19@example.invalid")
    _git(root, "config", "user.name", "D19 Fixture")
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


class _TopicalModel:
    """Round-based texts so recall can rank by topic."""

    def __init__(self) -> None:
        self.round = 0
        self.texts = ("fixed the addition bug in calc", "cleaned up the docs")

    def stream(self, request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        self.round += 1
        text = self.texts[min(self.round - 1, len(self.texts) - 1)]
        return _stream(
            request,
            AssistantTextItem(0, f"chat-text:{self.round}", text),
            FinishReason.STOP,
            f"r{self.round}",
        )


class _GitDiffModel:
    """Round 1 calls git_diff (repo already dirty), round 2 answers."""

    def __init__(self) -> None:
        self.round = 0

    def stream(self, request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        self.round += 1
        if self.round == 1:
            return _stream(
                request,
                _call("git_diff", {}, "call-diff"),
                FinishReason.TOOL_CALLS,
                "g1",
            )
        return _stream(
            request,
            AssistantTextItem(0, "chat-text:g", "diff captured"),
            FinishReason.STOP,
            "g2",
        )


def _runtime_config(repo: Path) -> RuntimeConfig:
    return RuntimeConfig(
        repo=repo,
        db=repo.parent / "agent.sqlite3",
        provider=ProviderConfig(
            base_url="http://127.0.0.1:1/v1",
            api_key_env="D19_TEST_KEY",
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
        system_prompt="You are KoawaAgent V2 in a session.",
    )


class SessionMemoryEnhancementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="koawa-d19-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)

    def test_compaction_projection_includes_changed_files(self) -> None:
        history = SessionHistory(
            provider="siliconflow",
            limits=SessionHistoryLimits(max_turns=1, compact_min_turns=2),
        )
        for index in range(4):
            history.append(
                SessionTurn(
                    user_input=f"request {index}",
                    final_text=f"answer {index}",
                    turn_id=uuid4(),
                    status="completed",
                    changed_files=("calc.py", "tests/test_calc.py")
                    if index == 1
                    else ("calc.py",),
                )
            )
        items = history.context_items()
        block = items[0].content
        self.assertIn("files=calc.py", block)
        self.assertIn("files=calc.py,tests/test_calc.py", block)

    def test_recall_ranks_topic_matches(self) -> None:
        app = AppRuntime(_runtime_config(self.repo), model_client=_TopicalModel())
        history = SessionHistory(provider="test")
        first = app.chat("fix the bug", history=history)
        thread_id = UUID(first.payload["thread_id"])
        history.append(
            SessionTurn(
                user_input="fix the bug",
                final_text=first.payload["final_text"],
                turn_id=UUID(first.payload["turn_id"]),
                status=first.payload["status"],
            )
        )
        second = app.chat("clean docs", thread_id=thread_id, history=history)
        history.append(
            SessionTurn(
                user_input="clean docs",
                final_text=second.payload["final_text"],
                turn_id=UUID(second.payload["turn_id"]),
                status=second.payload["status"],
            )
        )
        memory = SessionMemory(app.assembled.store, app.assembled.runtime)
        hits = memory.recall(thread_id, "addition")
        self.assertEqual(1, len(hits))
        self.assertEqual("fix the bug", hits[0].user_input)
        self.assertIn("addition", hits[0].final_text or "")
        self.assertEqual((), memory.recall(thread_id, "nonexistent-topic"))

    def test_journal_writes_deterministic_markdown(self) -> None:
        turns = (
            SessionTurn(
                user_input="fix the bug",
                final_text="done",
                turn_id=uuid4(),
                status="completed",
                changed_files=("calc.py",),
            ),
            SessionTurn(
                user_input="clean docs",
                final_text=None,
                turn_id=uuid4(),
                status="failed",
                error="d2:model_client_failed",
            ),
        )
        event_store = SqliteEventStore(self.root / "journal.sqlite3")
        run_id = uuid4()
        semantic_id = uuid4()
        journal = SessionJournal(WorkspaceEffectStore(event_store))
        target = journal.write(
            self.repo,
            turns,
            run_id=run_id,
            semantic_command_id=semantic_id,
            repository_identity_digest=repository_identity(self.repo),
        )
        content = target.read_text(encoding="utf-8")
        self.assertEqual("SESSION.md", target.name)
        self.assertIn("- turns: 2", content)
        self.assertIn("**user:** fix the bug", content)
        self.assertIn("**agent:** done", content)
        self.assertIn("**files:** calc.py", content)
        self.assertIn("**status:** failed", content)
        self.assertIn("**error:** d2:model_client_failed", content)
        # A response-loss retry reuses the same effect and exact bytes.
        self.assertEqual(
            target,
            journal.write(
                self.repo,
                turns,
                run_id=run_id,
                semantic_command_id=semantic_id,
                repository_identity_digest=repository_identity(self.repo),
            ),
        )
        events = event_store.read_all()
        self.assertIn("workspace.effect-intended.v2", {e.event_type for e in events})
        self.assertIn("workspace.effect-applied.v2", {e.event_type for e in events})

    def test_cli_extracts_changed_files_from_git_diff(self) -> None:
        import contextlib
        import io

        from koawa_agent_v2.runtime.cli import _store_position, _turn_changed_files

        app = AppRuntime(_runtime_config(self.repo), model_client=_GitDiffModel())
        # dirty the file AFTER assembly: the baseline is captured at assembly,
        # so the change becomes an agent change visible to git_diff.
        (self.repo / "calc.py").write_text(
            "def add(a, b):\n    return a - b  # dirty\n", encoding="utf-8"
        )
        before = _store_position(app)
        outcome = app.chat(
            "show diff",
            history=SessionHistory(provider="test"),
        )
        self.assertTrue(outcome.ok, outcome.payload)
        files = _turn_changed_files(app, before)
        self.assertIn("calc.py", files)


if __name__ == "__main__":
    unittest.main()
