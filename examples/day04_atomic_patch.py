"""D4：真实临时仓库的 read -> atomic multi-file patch -> verify -> final。

在 ``v2/`` 下运行：

    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:PYTHONPATH = "src"
    python -B examples/day04_atomic_patch.py
"""

from __future__ import annotations

import json
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
from koawa_agent_v2.control.models import ThreadStatus, TurnStatus
from koawa_agent_v2.editing.tools import build_coding_tool_registry
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.worker import TurnWorker


FINAL_TEXT = "已原子更新配置、新增报告模块、删除过期文件，并重新读取验证。"


def _header(request: ModelRequest, response_id: str, sequence: int) -> StreamHeader:
    return StreamHeader(
        request.model_turn_id,
        request.provider,
        response_id,
        sequence,
        sequence,
    )


def _completed_stream(
    request: ModelRequest,
    items: tuple[ToolCallItem | AssistantTextItem, ...],
    finish: FinishReason,
    *,
    response_id: str,
) -> tuple[ModelStreamEvent, ...]:
    events: list[ModelStreamEvent] = [
        TurnStarted(_header(request, response_id, 0), request.model)
    ]
    sequence = 1
    for item in items:
        if isinstance(item, ToolCallItem):
            started = ItemStarted(
                _header(request, response_id, sequence),
                item.canonical_index,
                item.item_id,
                OutputKind.TOOL_CALL,
                item.call_id,
                item.name,
            )
        else:
            started = ItemStarted(
                _header(request, response_id, sequence),
                item.canonical_index,
                item.item_id,
                OutputKind.ASSISTANT_TEXT,
            )
        events.append(started)
        sequence += 1
        events.append(ItemCompleted(_header(request, response_id, sequence), item))
        sequence += 1
    turn = ModelTurn(
        request.model_turn_id,
        request.provider,
        request.model,
        response_id,
        items,
        finish,
    )
    events.append(TurnCompleted(_header(request, response_id, sequence), turn))
    return tuple(events)


def _tool_round(
    request: ModelRequest,
    calls: tuple[tuple[str, str, dict[str, object]], ...],
    *,
    response_id: str,
) -> tuple[ModelStreamEvent, ...]:
    items = tuple(
        ToolCallItem(
            index,
            f"item-{call_id}",
            call_id,
            name,
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        )
        for index, (call_id, name, arguments) in enumerate(calls)
    )
    return _completed_stream(
        request, items, FinishReason.TOOL_CALLS, response_id=response_id
    )


def _results(request: ModelRequest) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in request.input_items:
        if not isinstance(item, ToolResultMessage):
            continue
        document = json.loads(item.content)
        if item.is_error or not isinstance(document, dict):
            raise AssertionError(f"unexpected tool failure: {document}")
        result[item.call_ref.call_id] = document
    return result


class ScriptedPatchModel:
    """四轮离线模型；Patch 的 base hash 只从真实 read_file 结果取得。"""

    def __init__(self) -> None:
        self.round = 0
        self.patch_diff = ""

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        self.round += 1
        if self.round == 1:
            names = {item.name for item in request.tool_definitions}
            if names != {"apply_patch", "list_files", "read_file", "search_text"}:
                raise AssertionError("D4 must expose one sealed combined catalog")
            return _tool_round(
                request,
                (
                    (
                        "read-config",
                        "read_file",
                        {"path": "src/config.py", "start_line": 1, "max_lines": 20},
                    ),
                    (
                        "read-obsolete",
                        "read_file",
                        {"path": "obsolete.txt", "start_line": 1, "max_lines": 20},
                    ),
                ),
                response_id="d4-read-bases",
            )
        if self.round == 2:
            results = _results(request)
            config = results["read-config"]
            obsolete = results["read-obsolete"]
            patch_json = json.dumps(
                {
                    "schema_version": 1,
                    "changes": [
                        {
                            "operation": "update",
                            "path": "src/config.py",
                            "base_sha256": config["sha256"],
                            "hunks": [
                                {
                                    "old_start": 1,
                                    "old_lines": ["RETRY_LIMIT = 1"],
                                    "new_lines": ["RETRY_LIMIT = 3"],
                                }
                            ],
                        },
                        {
                            "operation": "add",
                            "path": "src/report.py",
                            "content": "def status() -> str:\n    return 'ready'\n",
                            "newline": "lf",
                            "utf8_bom": False,
                        },
                        {
                            "operation": "delete",
                            "path": "obsolete.txt",
                            "base_sha256": obsolete["sha256"],
                        },
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return _tool_round(
                request,
                (("apply-atomic-patch", "apply_patch", {"patch_json": patch_json}),),
                response_id="d4-apply",
            )
        if self.round == 3:
            patch_result = _results(request)["apply-atomic-patch"]
            if patch_result.get("changed_files") != 3:
                raise AssertionError("all three changes must be one successful result")
            self.patch_diff = str(patch_result.get("diff", ""))
            return _tool_round(
                request,
                (
                    (
                        "verify-config",
                        "read_file",
                        {"path": "src/config.py", "start_line": 1, "max_lines": 20},
                    ),
                    (
                        "verify-report",
                        "read_file",
                        {"path": "src/report.py", "start_line": 1, "max_lines": 20},
                    ),
                    (
                        "verify-tree",
                        "list_files",
                        {"path": ".", "max_depth": 3, "max_entries": 50},
                    ),
                ),
                response_id="d4-verify",
            )
        if self.round == 4:
            results = _results(request)
            config = results["verify-config"]
            report = results["verify-report"]
            tree = results["verify-tree"]
            paths = {str(item["path"]) for item in tree["entries"]}
            if (
                config.get("content") != "RETRY_LIMIT = 3"
                or "return 'ready'" not in str(report.get("content"))
                or "obsolete.txt" in paths
                or "src/report.py" not in paths
            ):
                raise AssertionError("post-patch evidence is inconsistent")
            return _completed_stream(
                request,
                (AssistantTextItem(0, "d4-final", FINAL_TEXT),),
                FinishReason.STOP,
                response_id="d4-final-response",
            )
        raise AssertionError("D4 demo expects exactly four model rounds")


def _write_fixture(repository: Path) -> None:
    (repository / ".git").mkdir()
    (repository / "src").mkdir()
    (repository / "src" / "config.py").write_text(
        "RETRY_LIMIT = 1\n", encoding="utf-8"
    )
    (repository / "obsolete.txt").write_text("legacy\n", encoding="utf-8")


def main() -> None:
    with TemporaryDirectory(prefix="koawa-d4-") as temporary_directory:
        root = Path(temporary_directory)
        repository = root / "repository"
        repository.mkdir()
        _write_fixture(repository)
        database = root / "events.sqlite3"

        runtime = ThreadRuntime(SqliteEventStore(database), actor="day04-demo")
        thread = runtime.create_thread(str(repository))
        turn = runtime.create_turn(
            thread.thread_id,
            "把 RETRY_LIMIT 改为 3，新增 report 模块并删除 obsolete.txt，然后验证。",
            expected_thread_version=thread.version,
        )
        model = ScriptedPatchModel()
        with build_coding_tool_registry(repository) as registry:
            worker = TurnWorker(
                runtime,
                AgentLoop(model, tool_executor=registry),
                provider="scripted",
                model="koawa-d4-offline",
                instructions=(
                    InstructionMessage(
                        InstructionRole.DEVELOPER,
                        "修改前必须读取 base hash，修改后必须重新读取并列目录验证。",
                    ),
                ),
            )
            result = worker.execute(turn.turn_id, turn.version)

        restarted = ThreadRuntime(
            SqliteEventStore(database), actor="day04-demo-restart"
        )
        replayed_turn = restarted.get_turn(turn.turn_id)
        replayed_thread = restarted.get_thread(thread.thread_id)
        assert result.loop_result is not None
        assert result.loop_result.model_rounds == 4
        assert result.loop_result.tool_calls == 6
        assert replayed_turn.status is TurnStatus.COMPLETED
        assert replayed_turn.outcome == FINAL_TEXT
        assert replayed_thread.status is ThreadStatus.OPEN
        assert replayed_thread.active_turn_id is None
        assert not (repository / "obsolete.txt").exists()

        print("D4 atomic patch loop:")
        print(f"  model_rounds = {result.loop_result.model_rounds}")
        print(f"  tool_calls   = {result.loop_result.tool_calls}")
        print("  changed      = src/config.py, src/report.py, obsolete.txt")
        print(f"  diff_chars   = {len(model.patch_diff)}")
        print(f"  turn_status  = {replayed_turn.status.value}")
        print(f"  detached     = {replayed_thread.active_turn_id is None}")
        print(f"  final        = {replayed_turn.outcome}")


if __name__ == "__main__":
    main()
