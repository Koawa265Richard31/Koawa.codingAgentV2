"""D3：离线跑通真实临时仓库的 search_text -> read_file -> final。

运行方式（在 v2/ 下）：

    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:PYTHONPATH = "src"
    python -B examples/day03_repository_read_loop.py

脚本模型不会假装知道文件内容：第二轮先解析 Registry 返回的真实搜索结果，
再用命中的路径请求 read_file；第三轮必须在真实读取结果里看到目标正文，
才会给出 final。最后重新创建 Runtime，从 SQLite 重放并验证 Turn 已完成、
Thread 已解除 active Turn 绑定。
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
from koawa_agent_v2.tools.repository import build_repository_tool_registry
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.worker import TurnWorker


SEARCH_QUERY = "CHECKPOINT_PAYLOAD"
EXPECTED_PATH = "src/checkpoint.py"
FINAL_TEXT = "已定位并读取 src/checkpoint.py：它把 checkpoint payload 编码为 UTF-8。"


def _header(request: ModelRequest, response_id: str, sequence: int) -> StreamHeader:
    """为一条离线 response 构造连续、身份稳定的 canonical header。"""
    return StreamHeader(
        model_turn_id=request.model_turn_id,
        provider=request.provider,
        provider_response_id=response_id,
        sequence=sequence,
        provider_sequence=sequence,
    )


def _completed_stream(
    request: ModelRequest,
    item: AssistantTextItem | ToolCallItem,
    finish_reason: FinishReason,
    *,
    response_id: str,
) -> tuple[ModelStreamEvent, ...]:
    """产生一条完整 typed stream；D3 关注 completed ToolCall 之后的真实分发。"""
    if isinstance(item, ToolCallItem):
        started = ItemStarted(
            _header(request, response_id, 1),
            item.canonical_index,
            item.item_id,
            OutputKind.TOOL_CALL,
            item.call_id,
            item.name,
        )
    else:
        started = ItemStarted(
            _header(request, response_id, 1),
            item.canonical_index,
            item.item_id,
            OutputKind.ASSISTANT_TEXT,
        )
    turn = ModelTurn(
        model_turn_id=request.model_turn_id,
        provider=request.provider,
        model=request.model,
        provider_response_id=response_id,
        output_items=(item,),
        finish_reason=finish_reason,
    )
    return (
        TurnStarted(_header(request, response_id, 0), request.model),
        started,
        ItemCompleted(_header(request, response_id, 2), item),
        TurnCompleted(_header(request, response_id, 3), turn),
    )


def _tool_round(
    request: ModelRequest,
    *,
    response_id: str,
    item_id: str,
    call_id: str,
    name: str,
    arguments: dict[str, object],
) -> tuple[ModelStreamEvent, ...]:
    """把脚本模型的一次决定编码为完成态 ToolCallItem。"""
    item = ToolCallItem(
        canonical_index=0,
        item_id=item_id,
        call_id=call_id,
        name=name,
        arguments_json=json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    return _completed_stream(
        request,
        item,
        FinishReason.TOOL_CALLS,
        response_id=response_id,
    )


def _results(request: ModelRequest) -> tuple[ToolResultMessage, ...]:
    """只提取 AgentLoop 已按 call identity 回填的真实工具结果。"""
    return tuple(
        item for item in request.input_items if isinstance(item, ToolResultMessage)
    )


def _document(result: ToolResultMessage) -> dict[str, object]:
    """示例也 fail closed：工具错误或非 object JSON 都不能驱动下一次调用。"""
    if result.is_error:
        raise AssertionError(f"unexpected tool error for {result.call_ref.call_id}")
    document = json.loads(result.content)
    if not isinstance(document, dict) or document.get("ok") is not True:
        raise AssertionError("repository tool result must be a successful JSON object")
    return document


class ScriptedRepositoryModel:
    """三轮离线模型：搜索、检查结果后读取、检查正文后回答。"""

    def __init__(self) -> None:
        self.request_count = 0
        self.search_match_path: str | None = None
        self.read_sha256: str | None = None

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        self.request_count += 1
        if self.request_count == 1:
            return self._search(request)
        if self.request_count == 2:
            return self._read_real_match(request)
        if self.request_count == 3:
            return self._answer_from_real_content(request)
        raise AssertionError("the D3 demo expects exactly three model rounds")

    def _search(self, request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        """第一轮只能请求搜索；此时模型还不知道目标文件路径。"""
        visible_tools = {definition.name for definition in request.tool_definitions}
        if visible_tools != {"list_files", "read_file", "search_text"}:
            raise AssertionError("model must receive the sealed repository tool catalog")
        return _tool_round(
            request,
            response_id="d3-response-search",
            item_id="d3-item-search",
            call_id="d3-call-search",
            name="search_text",
            arguments={
                "query": SEARCH_QUERY,
                "path": ".",
                "max_depth": 4,
                "max_files": 32,
                "max_matches": 8,
                "case_sensitive": True,
                "include": ["*.py"],
                "exclude": [],
            },
        )

    def _read_real_match(
        self,
        request: ModelRequest,
    ) -> tuple[ModelStreamEvent, ...]:
        """第二轮从实际 search_text JSON 取路径，绝不硬编码 read 参数。"""
        results = _results(request)
        if len(results) != 1 or results[0].call_ref.call_id != "d3-call-search":
            raise AssertionError("second round must contain the search result")
        search = _document(results[0])
        matches = search.get("matches")
        if not isinstance(matches, list) or not matches:
            raise AssertionError("search_text must return at least one real match")
        first = matches[0]
        if not isinstance(first, dict) or not isinstance(first.get("path"), str):
            raise AssertionError("search match must contain a validated relative path")
        match_path = first["path"]
        if match_path != EXPECTED_PATH or first.get("line") != 4:
            raise AssertionError("search result does not match the temporary repository")
        self.search_match_path = match_path
        return _tool_round(
            request,
            response_id="d3-response-read",
            item_id="d3-item-read",
            call_id="d3-call-read",
            name="read_file",
            arguments={"path": match_path, "start_line": 1, "max_lines": 20},
        )

    def _answer_from_real_content(
        self,
        request: ModelRequest,
    ) -> tuple[ModelStreamEvent, ...]:
        """第三轮只有检查 read_file 正文和 digest 后才允许产生 final。"""
        results = _results(request)
        if [item.call_ref.call_id for item in results] != [
            "d3-call-search",
            "d3-call-read",
        ]:
            raise AssertionError("third round must preserve both ordered tool results")
        read = _document(results[-1])
        content = read.get("content")
        sha256 = read.get("sha256")
        if (
            read.get("path") != self.search_match_path
            or not isinstance(content, str)
            or 'CHECKPOINT_PAYLOAD = "utf-8"' not in content
            or not isinstance(sha256, str)
            or len(sha256) != 64
        ):
            raise AssertionError("read_file did not return the expected real file evidence")
        self.read_sha256 = sha256
        item = AssistantTextItem(0, "d3-item-final", FINAL_TEXT)
        return _completed_stream(
            request,
            item,
            FinishReason.STOP,
            response_id="d3-response-final",
        )


def _write_fixture(repository: Path) -> None:
    """建立真实磁盘仓库；Agent 工具随后只读，不把内存字符串当文件结果。"""
    (repository / ".git").mkdir(parents=True)
    (repository / ".git" / "HEAD").write_text(
        "ref: refs/heads/main\n",
        encoding="utf-8",
    )
    (repository / "src").mkdir()
    (repository / "README.md").write_text(
        "# D3 fixture\n\nThe repository tools must inspect real files.\n",
        encoding="utf-8",
    )
    (repository / "src" / "checkpoint.py").write_text(
        "from __future__ import annotations\n"
        "\n"
        "\n"
        'CHECKPOINT_PAYLOAD = "utf-8"\n'
        "\n"
        "\n"
        "def encode_checkpoint(value: str) -> bytes:\n"
        "    return value.encode(CHECKPOINT_PAYLOAD)\n",
        encoding="utf-8",
    )


def _print_event(event: ModelStreamEvent) -> None:
    """观察 canonical 事件，不输出未验证的原始 Provider 数据。"""
    print(f"  event[{event.header.sequence}] {type(event).__name__}")


def main() -> None:
    """运行 D1 + D2 + D3 闭环，并用全新 Runtime 重放最终状态。"""
    with TemporaryDirectory(prefix="koawa-d3-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        repository = temporary_root / "sample-repository"
        repository.mkdir()
        _write_fixture(repository)

        database_path = temporary_root / "events.sqlite3"
        runtime = ThreadRuntime(SqliteEventStore(database_path), actor="day03-demo")
        thread = runtime.create_thread(str(repository))
        queued = runtime.create_turn(
            thread.thread_id,
            "定位 checkpoint payload 的编码方式，并给出有文件证据的答案",
            expected_thread_version=thread.version,
        )

        client = ScriptedRepositoryModel()
        with build_repository_tool_registry(repository) as registry:
            worker = TurnWorker(
                runtime,
                AgentLoop(client, tool_executor=registry),
                provider="scripted",
                model="koawa-d3-offline",
                instructions=(
                    InstructionMessage(
                        InstructionRole.DEVELOPER,
                        "必须先搜索真实仓库，再读取命中文件，最后基于工具证据回答。",
                    ),
                ),
            )
            print("D3 repository read loop:")
            result = worker.execute(
                queued.turn_id,
                queued.version,
                event_sink=_print_event,
            )

        # 不复用旧 Store/Runtime 对象；终态必须能从 SQLite 事件重新投影。
        restarted = ThreadRuntime(
            SqliteEventStore(database_path),
            actor="day03-demo-restart",
        )
        replayed_turn = restarted.get_turn(queued.turn_id)
        replayed_thread = restarted.get_thread(thread.thread_id)

        assert result.loop_result is not None
        assert result.loop_result.model_rounds == 3
        assert result.loop_result.tool_calls == 2
        assert client.request_count == 3
        assert client.search_match_path == EXPECTED_PATH
        assert client.read_sha256 is not None
        assert replayed_turn.status is TurnStatus.COMPLETED
        assert replayed_turn.outcome == FINAL_TEXT
        assert replayed_thread.status is ThreadStatus.OPEN
        assert replayed_thread.active_turn_id is None

        print("\nD1/D2/D3 result:")
        print(f"  model_rounds = {result.loop_result.model_rounds}")
        print(f"  tool_calls   = {result.loop_result.tool_calls}")
        print(f"  matched_path = {client.search_match_path}")
        print(f"  file_sha256  = {client.read_sha256}")
        print(f"  turn_status  = {replayed_turn.status.value}")
        print(f"  detached     = {replayed_thread.active_turn_id is None}")
        print(f"  final        = {replayed_turn.outcome}")


if __name__ == "__main__":
    main()
