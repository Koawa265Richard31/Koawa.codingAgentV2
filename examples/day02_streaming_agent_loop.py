"""D2：离线演示 typed model stream、Agent Loop 与 D1 durable lifecycle。

运行方式（在 v2/ 下）：

    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:PYTHONPATH = "src"
    python -B examples/day02_streaming_agent_loop.py

这个示例不访问网络，也不是 D3 的 Tool Registry。脚本模型先请求读取一份
内存中的说明文件，Loop 执行一次工具，再把 ToolResult 放入第二轮模型上下文；
最后 TurnWorker 用真实 SQLite Event Store 提交 COMPLETED，并释放 Thread。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryDirectory

from koawa_agent_v2.execution.loop import (
    AgentLoop,
    ToolExecutionContext,
    ToolExecutionResult,
)
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    ContentDelta,
    ContentKind,
    FinishReason,
    InstructionMessage,
    InstructionRole,
    ItemCompleted,
    ItemStarted,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    ModelUsage,
    OutputKind,
    StreamHeader,
    ToolArgumentsDelta,
    ToolCallItem,
    ToolDefinition,
    ToolResultMessage,
    TurnCompleted,
    TurnStarted,
    UsageReported,
)
from koawa_agent_v2.control.models import ThreadStatus, TurnStatus
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.worker import TurnWorker


READ_NOTE = ToolDefinition(
    name="read_note",
    description="读取工作区中的一份文本说明",
    input_schema_json=(
        '{"type":"object","properties":{"path":{"type":"string"}},'
        '"required":["path"],"additionalProperties":false}'
    ),
)


def _header(request: ModelRequest, response_id: str, sequence: int) -> StreamHeader:
    """为当前 response 构造连续且身份稳定的 canonical header。"""
    return StreamHeader(
        model_turn_id=request.model_turn_id,
        provider=request.provider,
        provider_response_id=response_id,
        sequence=sequence,
        provider_sequence=sequence,
    )


def _tool_round(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
    """第一轮：用多个 delta 流式产生一个完整 read_note 调用。"""
    response_id = "offline-response-tool"
    item = ToolCallItem(
        canonical_index=0,
        item_id="offline-tool-item",
        call_id="offline-call-read-note",
        name="read_note",
        arguments_json='{"path":"README.md"}',
    )
    usage = ModelUsage(input_tokens=20, output_tokens=8, total_tokens=28)
    turn = ModelTurn(
        model_turn_id=request.model_turn_id,
        provider=request.provider,
        model=request.model,
        provider_response_id=response_id,
        output_items=(item,),
        finish_reason=FinishReason.TOOL_CALLS,
        usage=usage,
    )
    return (
        TurnStarted(_header(request, response_id, 0), request.model),
        ItemStarted(
            _header(request, response_id, 1),
            item.canonical_index,
            item.item_id,
            OutputKind.TOOL_CALL,
            item.call_id,
            item.name,
        ),
        ToolArgumentsDelta(
            _header(request, response_id, 2),
            item.canonical_index,
            item.item_id,
            item.call_id,
            '{"path":"',
        ),
        ToolArgumentsDelta(
            _header(request, response_id, 3),
            item.canonical_index,
            item.item_id,
            item.call_id,
            'README.md"}',
        ),
        ItemCompleted(_header(request, response_id, 4), item),
        UsageReported(_header(request, response_id, 5), usage),
        TurnCompleted(_header(request, response_id, 6), turn),
    )


def _final_round(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
    """第二轮：确认工具结果已进入上下文，然后流式给出最终答案。"""
    tool_results = [
        item for item in request.input_items if isinstance(item, ToolResultMessage)
    ]
    if len(tool_results) != 1 or tool_results[0].is_error:
        raise AssertionError("second model round must contain one successful tool result")

    response_id = "offline-response-final"
    text = "已读取 README.md；D2 的模型流、工具回合和持久化 Turn 已形成闭环。"
    item = AssistantTextItem(
        canonical_index=0,
        item_id="offline-final-item",
        text=text,
    )
    usage = ModelUsage(input_tokens=36, output_tokens=18, total_tokens=54)
    turn = ModelTurn(
        model_turn_id=request.model_turn_id,
        provider=request.provider,
        model=request.model,
        provider_response_id=response_id,
        output_items=(item,),
        finish_reason=FinishReason.STOP,
        usage=usage,
    )
    split_at = text.index("；") + 1
    return (
        TurnStarted(_header(request, response_id, 0), request.model),
        ItemStarted(
            _header(request, response_id, 1),
            item.canonical_index,
            item.item_id,
            OutputKind.ASSISTANT_TEXT,
        ),
        ContentDelta(
            _header(request, response_id, 2),
            item.canonical_index,
            item.item_id,
            ContentKind.ASSISTANT_TEXT,
            text[:split_at],
        ),
        ContentDelta(
            _header(request, response_id, 3),
            item.canonical_index,
            item.item_id,
            ContentKind.ASSISTANT_TEXT,
            text[split_at:],
        ),
        ItemCompleted(_header(request, response_id, 4), item),
        UsageReported(_header(request, response_id, 5), usage),
        TurnCompleted(_header(request, response_id, 6), turn),
    )


class ScriptedModelClient:
    """最小离线 ModelClient：两次请求分别返回工具回合和最终回合。"""

    def __init__(self) -> None:
        self.request_count = 0

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        self.request_count += 1
        if self.request_count == 1:
            return _tool_round(request)
        if self.request_count == 2:
            return _final_round(request)
        raise AssertionError("the demo expects exactly two model rounds")


class ReadNoteExecutor:
    """D3 前的演示执行器；只实现一个确定性的内存只读工具。"""

    def __init__(self) -> None:
        self.call_count = 0

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """返回模型可见的冻结工具定义。"""
        return (READ_NOTE,)

    def execute(
        self,
        call: ToolCallItem,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del context  # 演示工具无副作用；真实执行器会用该身份做审计和 D7 ledger。
        self.call_count += 1
        if call.name != "read_note":
            return ToolExecutionResult("unsupported tool", is_error=True)
        if call.arguments.get("path") != "README.md":
            return ToolExecutionResult("file not found", is_error=True)
        return ToolExecutionResult(
            "README.md: KoawaAgent V2 is a durable coding-agent runtime."
        )


def _print_event(event: ModelStreamEvent) -> None:
    """事件观察器只打印类型与序号，不泄漏正文或半截工具参数。"""
    print(f"  event[{event.header.sequence}] {type(event).__name__}")


def main() -> None:
    """运行完整 D1 + D2 成功路径，并从新 Runtime 重读最终持久状态。"""
    with TemporaryDirectory(prefix="koawa-d2-") as temporary_directory:
        database_path = Path(temporary_directory) / "events.sqlite3"
        runtime = ThreadRuntime(
            SqliteEventStore(database_path),
            actor="day02-demo",
        )
        thread = runtime.create_thread(str(Path.cwd()))
        queued = runtime.create_turn(
            thread.thread_id,
            "读取项目说明，并告诉我 D2 是否形成闭环",
            expected_thread_version=thread.version,
        )

        client = ScriptedModelClient()
        executor = ReadNoteExecutor()
        worker = TurnWorker(
            runtime,
            AgentLoop(client, tool_executor=executor),
            provider="scripted",
            model="koawa-d2-offline",
            instructions=(
                InstructionMessage(
                    InstructionRole.DEVELOPER,
                    "需要仓库信息时先调用只读工具，再基于结果回答。",
                ),
            ),
        )

        print("D2 typed stream:")
        result = worker.execute(
            queued.turn_id,
            queued.version,
            event_sink=_print_event,
        )

        # 新建 Runtime/Store 实例，证明终态来自 SQLite 重放，而非旧对象内存。
        restarted_runtime = ThreadRuntime(
            SqliteEventStore(database_path),
            actor="day02-demo-restart",
        )
        persisted_turn = restarted_runtime.get_turn(queued.turn_id)
        persisted_thread = restarted_runtime.get_thread(thread.thread_id)

        assert result.loop_result is not None
        assert persisted_turn.status is TurnStatus.COMPLETED
        assert persisted_thread.status is ThreadStatus.OPEN
        assert persisted_thread.active_turn_id is None
        assert client.request_count == 2
        assert executor.call_count == 1

        print("\nD1/D2 result:")
        print(f"  model_rounds = {result.loop_result.model_rounds}")
        print(f"  tool_calls   = {result.loop_result.tool_calls}")
        print(f"  turn_status  = {persisted_turn.status.value}")
        print(f"  detached     = {persisted_thread.active_turn_id is None}")
        print(f"  final        = {persisted_turn.outcome}")


if __name__ == "__main__":
    main()
