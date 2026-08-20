"""D5：失败测试 -> 精确 Patch -> 测试通过 -> Git 证据 -> Finalizer。

在 ``v2/`` 下运行：

    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:PYTHONPATH = "src"
    python -B examples/day05_coding_vertical_slice.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryDirectory

from koawa_agent_v2.execution.loop import AgentLoop
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    InstructionMessage,
    InstructionRole,
    ItemCompleted,
    ItemStarted,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    OutputKind,
    StreamHeader,
    ToolCallItem,
    ToolResultMessage,
    TurnCompleted,
    TurnStarted,
)
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.verification.runner import (
    CommandProfile,
    RepositoryTrust,
)
from koawa_agent_v2.execution.worker import TurnWorker
from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry


FINAL_TEXT = "add 已修复；固定测试配置通过，Git status/diff 与最终报告均已核验。"


def _header(request: ModelRequest, response_id: str, sequence: int) -> StreamHeader:
    return StreamHeader(
        request.model_turn_id,
        request.provider,
        response_id,
        sequence,
        sequence,
    )


def _events(
    request: ModelRequest,
    item: ToolCallItem | AssistantTextItem,
    finish: FinishReason,
    response_id: str,
) -> tuple[ModelStreamEvent, ...]:
    kind = OutputKind.TOOL_CALL if isinstance(item, ToolCallItem) else OutputKind.ASSISTANT_TEXT
    if isinstance(item, ToolCallItem):
        started = ItemStarted(
            _header(request, response_id, 1),
            item.canonical_index,
            item.item_id,
            kind,
            item.call_id,
            item.name,
        )
    else:
        started = ItemStarted(
            _header(request, response_id, 1),
            item.canonical_index,
            item.item_id,
            kind,
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


def _result(request: ModelRequest, call_id: str) -> dict[str, object]:
    result = next(
        item
        for item in request.input_items
        if isinstance(item, ToolResultMessage) and item.call_ref.call_id == call_id
    )
    document = json.loads(result.content)
    if result.is_error:
        raise AssertionError(document)
    return document


class ScriptedCodingModel:
    """八轮离线模型，演示 D5 的真实执行顺序，不伪造测试或 Git 结果。"""

    def __init__(self) -> None:
        self.round = 0
        self.report: dict[str, object] | None = None

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        self.round += 1
        if self.round == 1:
            return _events(
                request,
                _call("run_test_profile", {"profile_id": "unit"}, "test-before"),
                FinishReason.TOOL_CALLS,
                "d5-test-before",
            )
        if self.round == 2:
            failed = _result(request, "test-before")
            assert failed["outcome"] == "failed"
            return _events(
                request,
                _call(
                    "read_file",
                    {"path": "app.py", "start_line": 1, "max_lines": 30},
                    "read",
                ),
                FinishReason.TOOL_CALLS,
                "d5-read",
            )
        if self.round == 3:
            read = _result(request, "read")
            patch_json = json.dumps(
                {
                    "schema_version": 1,
                    "changes": [
                        {
                            "operation": "update",
                            "path": "app.py",
                            "base_sha256": read["sha256"],
                            "hunks": [
                                {
                                    "old_start": 2,
                                    "old_lines": ["    return left - right"],
                                    "new_lines": ["    return left + right  # fixed"],
                                }
                            ],
                        }
                    ],
                },
                separators=(",", ":"),
            )
            return _events(
                request,
                _call("apply_patch", {"patch_json": patch_json}, "patch"),
                FinishReason.TOOL_CALLS,
                "d5-patch",
            )
        if self.round == 4:
            assert _result(request, "patch")["changed_files"] == 1
            return _events(
                request,
                _call("run_test_profile", {"profile_id": "unit"}, "test-after"),
                FinishReason.TOOL_CALLS,
                "d5-test-after",
            )
        if self.round == 5:
            assert _result(request, "test-after")["outcome"] == "passed"
            return _events(
                request,
                _call("git_status", {}, "status"),
                FinishReason.TOOL_CALLS,
                "d5-status",
            )
        if self.round == 6:
            assert _result(request, "status")["agent_changed_paths"] == ["app.py"]
            return _events(
                request,
                _call("git_diff", {}, "diff"),
                FinishReason.TOOL_CALLS,
                "d5-diff",
            )
        if self.round == 7:
            assert "left + right" in str(_result(request, "diff")["diff"])
            return _events(
                request,
                _call("finalize_task", {}, "finalize"),
                FinishReason.TOOL_CALLS,
                "d5-finalize",
            )
        if self.round == 8:
            self.report = _result(request, "finalize")
            return _events(
                request,
                AssistantTextItem(0, "d5-final", FINAL_TEXT),
                FinishReason.STOP,
                "d5-final",
            )
        raise AssertionError("unexpected model round")


def _git(root: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git_not_available")
    subprocess.run(
        (executable, "-C", str(root), *arguments),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def _fixture(repository: Path) -> None:
    (repository / "tests").mkdir()
    (repository / "app.py").write_text(
        "def add(left, right):\n    return left - right\n", encoding="utf-8"
    )
    (repository / "tests" / "test_app.py").write_text(
        "import unittest\nfrom app import add\n\n"
        "class AppTest(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(5, add(2, 3))\n",
        encoding="utf-8",
    )
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "day05@example.invalid")
    _git(repository, "config", "user.name", "Day05 Fixture")
    _git(repository, "config", "core.fsmonitor", "false")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "config", "core.filemode", "false")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-qm", "failing fixture")


def main() -> None:
    with TemporaryDirectory(prefix="koawa-d5-") as temporary:
        root = Path(temporary)
        repository = root / "repository"
        repository.mkdir()
        _fixture(repository)
        database = root / "events.sqlite3"

        runtime = ThreadRuntime(SqliteEventStore(database), actor="day05-demo")
        thread = runtime.create_thread(str(repository))
        turn = runtime.create_turn(
            thread.thread_id,
            "修复 add，运行可信测试，并用 Git 证据完成验收。",
            expected_thread_version=thread.version,
        )
        profile = CommandProfile(
            "unit",
            (
                str(Path(sys.executable).resolve()),
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ),
        )
        model = ScriptedCodingModel()
        with build_verified_coding_tool_registry(
            repository,
            command_profiles=(profile,),
            repository_trust=RepositoryTrust.BUILTIN_FIXTURE,
        ) as registry:
            loop = AgentLoop(
                model,
                tool_executor=registry,
                completion_gate=registry,
            )
            worker = TurnWorker(
                runtime,
                loop,
                provider="scripted",
                model="koawa-d5-offline",
                instructions=(
                    InstructionMessage(
                        InstructionRole.DEVELOPER,
                        "只运行固定 profile；测试通过、status、diff、finalize 缺一不可。",
                    ),
                ),
            )
            result = worker.execute(turn.turn_id, turn.version)

        replayed = ThreadRuntime(
            SqliteEventStore(database), actor="day05-demo-restart"
        ).get_turn(turn.turn_id)
        assert result.loop_result is not None
        assert replayed.status is TurnStatus.COMPLETED
        assert replayed.outcome == FINAL_TEXT
        assert model.report is not None

        print("D5 coding vertical slice:")
        print(f"  model_rounds = {result.loop_result.model_rounds}")
        print(f"  tool_calls   = {result.loop_result.tool_calls}")
        print("  test_path    = failed -> patch -> passed")
        print(f"  changed      = {model.report['agent_changed_paths']}")
        print(f"  diff_sha256  = {model.report['diff_sha256']}")
        print(f"  turn_status  = {replayed.status.value}")
        print(f"  final        = {replayed.outcome}")


if __name__ == "__main__":
    main()
