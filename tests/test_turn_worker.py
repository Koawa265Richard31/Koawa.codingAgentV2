from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import TypeAlias
from uuid import uuid4

from koawa_agent_v2.execution.loop import (
    AgentLoop,
    CancellationToken,
    ToolExecutionContext,
    ToolExecutionResult,
)
from koawa_agent_v2.control.event_store import WrongExpectedVersion
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
    ToolDefinition,
    TurnCompleted,
    TurnStarted,
)
from koawa_agent_v2.control.models import ThreadStatus, TurnStatus
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.worker import ContextUnavailable, TurnWorker


StreamScript: TypeAlias = Callable[[ModelRequest], Iterable[ModelStreamEvent]]


class ScriptedClient:
    """按顺序提供模型流，并记录 Provider 实际被调用了多少次。"""

    def __init__(self, *scripts: StreamScript | BaseException) -> None:
        self._scripts = list(scripts)
        self.requests: list[ModelRequest] = []

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        """执行当前脚本，允许脚本在 Worker 已启动后注入外部竞争。"""
        self.requests.append(request)
        if not self._scripts:
            raise AssertionError("unexpected model request")
        script = self._scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        return script(request)


class RecordingToolExecutor:
    """记录 TurnWorker 内部 Loop 穿过的真实工具执行边界。"""

    def __init__(
        self,
        *results: ToolExecutionResult,
        definitions: Sequence[ToolDefinition] = (),
    ) -> None:
        self._results = list(results)
        self._definitions = tuple(definitions)
        self.calls: list[tuple[ToolCallItem, ToolExecutionContext]] = []

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """返回与该测试执行器绑定的冻结工具定义。"""
        return self._definitions

    def execute(
        self,
        call: ToolCallItem,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """返回预设结果，未预设时提供确定性的默认成功结果。"""
        self.calls.append((call, context))
        if self._results:
            return self._results.pop(0)
        return ToolExecutionResult(f"ok:{call.name}")


def _header(request: ModelRequest, response_id: str, sequence: int) -> StreamHeader:
    """创建与当前 ModelRequest 身份一致的 canonical 事件头。"""
    return StreamHeader(
        request.model_turn_id,
        request.provider,
        response_id,
        sequence,
        sequence,
    )


def _completed_stream(
    request: ModelRequest,
    items: Sequence[AssistantTextItem | ToolCallItem],
    finish_reason: FinishReason,
    response_id: str,
) -> tuple[ModelStreamEvent, ...]:
    """构造 Worker 集成测试需要的最短合法完成流。"""
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
        tuple(items),
        finish_reason,
    )
    events.append(TurnCompleted(_header(request, response_id, sequence), turn))
    return tuple(events)


def _final_script(text: str, response_id: str) -> StreamScript:
    """返回一个可按请求身份生成 final 回合的脚本。"""

    def script(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        item = AssistantTextItem(0, f"item-{response_id}", text)
        return _completed_stream(request, (item,), FinishReason.STOP, response_id)

    return script


def _tool_script(response_id: str) -> StreamScript:
    """返回一个 read_file 工具回合，用于贯穿 Worker 与 Loop。"""

    def script(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        call = ToolCallItem(
            0,
            f"item-{response_id}",
            "call-read",
            "read_file",
            '{"path":"README.md"}',
        )
        return _completed_stream(
            request,
            (call,),
            FinishReason.TOOL_CALLS,
            response_id,
        )

    return script


READ_FILE = ToolDefinition(
    "read_file",
    "读取文件",
    '{"type":"object","properties":{"path":{"type":"string"}}}',
)


class TurnWorkerTest(unittest.TestCase):
    """用真实 SQLite EventStore 验证 D2 Loop 与 D1 生命周期的接缝。"""

    def setUp(self) -> None:
        """为每个用例建立独立文件数据库，避免共享事件历史。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database_path = Path(temporary.name) / "turn-worker.sqlite3"
        self.store = SqliteEventStore(database_path)
        self.runtime = ThreadRuntime(self.store, actor="turn-worker-test")

    def create_turn(self):
        """创建一个仍占用 Thread 的标准 QUEUED Turn。"""
        thread = self.runtime.create_thread("D:/work/repository")
        turn = self.runtime.create_turn(
            thread.thread_id,
            "修复仓库中的失败测试",
            expected_thread_version=thread.version,
        )
        return thread, turn

    def worker(
        self,
        client: ScriptedClient,
        *,
        executor: RecordingToolExecutor | None = None,
    ) -> TurnWorker:
        """组装使用真实 Runtime、脚本模型和可选工具执行器的 Worker。"""
        loop = AgentLoop(client, tool_executor=executor)
        return TurnWorker(
            self.runtime,
            loop,
            provider="test-provider",
            model="test-model",
        )

    def assert_thread_detached(self, thread_id) -> None:
        """断言终态提交与 Thread detach 已在同一事务中可见。"""
        thread = self.runtime.get_thread(thread_id)
        self.assertEqual(ThreadStatus.OPEN, thread.status)
        self.assertIsNone(thread.active_turn_id)

    def test_success_completes_d1_turn_and_detaches_thread(self) -> None:
        """Loop 经工具回合获得 final 后，Worker 提交 COMPLETED 并释放 Thread。"""
        thread, turn = self.create_turn()
        client = ScriptedClient(
            _tool_script("response-tool"),
            _final_script("修复完成，测试通过", "response-final"),
        )
        executor = RecordingToolExecutor(
            ToolExecutionResult("README content"),
            definitions=(READ_FILE,),
        )
        worker = self.worker(client, executor=executor)

        result = worker.execute(turn.turn_id, turn.version)

        self.assertEqual(TurnStatus.COMPLETED, result.turn.status)
        self.assertEqual("修复完成，测试通过", result.turn.outcome)
        self.assertIsNotNone(result.loop_result)
        self.assertEqual(2, result.loop_result.model_rounds)
        self.assertEqual(1, len(executor.calls))
        self.assert_thread_detached(thread.thread_id)

    def test_loop_failure_fails_d1_turn_and_detaches_thread(self) -> None:
        """空模型流由 Worker 转为稳定 FAILED outcome，并原子释放 Thread。"""
        thread, turn = self.create_turn()
        client = ScriptedClient(lambda _request: ())
        worker = self.worker(client)

        result = worker.execute(turn.turn_id, turn.version)

        self.assertEqual(TurnStatus.FAILED, result.turn.status)
        self.assertEqual("d2:empty_model_stream", result.turn.error)
        self.assertIsNone(result.loop_result)
        self.assertEqual(result.turn, self.runtime.get_turn(turn.turn_id))
        self.assert_thread_detached(thread.thread_id)

    def test_stale_expected_version_never_calls_provider(self) -> None:
        """调用者版本过期时必须在启动 Run 前失败，Provider 调用次数保持为零。"""
        thread, turn = self.create_turn()
        client = ScriptedClient(_final_script("不应调用", "response-unused"))
        worker = self.worker(client)

        with self.assertRaises(WrongExpectedVersion):
            worker.execute(turn.turn_id, turn.version + 1)

        self.assertEqual([], client.requests)
        unchanged = self.runtime.get_turn(turn.turn_id)
        self.assertEqual(TurnStatus.QUEUED, unchanged.status)
        self.assertEqual(turn.version, unchanged.version)
        self.assertEqual(turn.turn_id, self.runtime.get_thread(thread.thread_id).active_turn_id)

    def test_external_cancel_wins_over_old_worker_completion(self) -> None:
        """模型返回 final 前外部取消时，旧 Worker 的 stale completion 不能覆盖终态。"""
        thread, turn = self.create_turn()

        def cancel_then_finish(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
            running = self.runtime.get_turn(turn.turn_id)
            self.assertEqual(TurnStatus.RUNNING, running.status)
            self.runtime.cancel_turn(
                turn.turn_id,
                "用户在模型执行期间取消",
                expected_version=running.version,
            )
            item = AssistantTextItem(0, "item-late-final", "迟到的成功结果")
            return _completed_stream(
                request,
                (item,),
                FinishReason.STOP,
                "response-late-final",
            )

        client = ScriptedClient(cancel_then_finish)
        worker = self.worker(client)

        with self.assertRaises(WrongExpectedVersion):
            worker.execute(turn.turn_id, turn.version)

        cancelled = self.runtime.get_turn(turn.turn_id)
        self.assertEqual(TurnStatus.CANCELLED, cancelled.status)
        self.assertEqual("用户在模型执行期间取消", cancelled.error)
        self.assertIsNone(cancelled.outcome)
        self.assertEqual(1, len(client.requests))
        self.assert_thread_detached(thread.thread_id)

    def test_resumed_turn_without_explicit_context_is_rejected_before_provider(self) -> None:
        """未配置 D6 store 时必须拒绝续跑，不能只拿原始用户输入重新执行。"""
        thread, turn = self.create_turn()
        running = self.runtime.start_turn(turn.turn_id, turn.version)
        interrupt_id = uuid4()
        waiting = self.runtime.wait_for_input(
            turn.turn_id,
            "应该使用哪个数据库？",
            expected_version=running.version,
            run_id=running.current_run_id,
            interrupt_id=interrupt_id,
        )
        queued = self.runtime.request_resume(
            turn.turn_id,
            waiting.version,
            interrupt_id=interrupt_id,
            response="PostgreSQL",
        )
        client = ScriptedClient(_final_script("不应调用", "response-unused"))
        worker = self.worker(client)

        with self.assertRaises(ContextUnavailable) as raised:
            worker.execute(queued.turn_id, queued.version)

        self.assertEqual("durable_context_unavailable", raised.exception.code)
        self.assertEqual([], client.requests)
        unchanged = self.runtime.get_turn(turn.turn_id)
        self.assertEqual(TurnStatus.QUEUED, unchanged.status)
        self.assertEqual(queued.version, unchanged.version)
        self.assertEqual(turn.turn_id, self.runtime.get_thread(thread.thread_id).active_turn_id)

    def test_pre_cancelled_worker_cancels_queued_turn_without_provider_call(self) -> None:
        """Worker 启动前已收到取消时，直接提交 D1 CANCELLED 并释放 Thread。"""
        thread, turn = self.create_turn()
        token = CancellationToken()
        token.cancel()
        client = ScriptedClient(_final_script("不应调用", "response-unused"))
        worker = self.worker(client)

        result = worker.execute(
            turn.turn_id,
            turn.version,
            cancellation=token,
        )

        self.assertEqual(TurnStatus.CANCELLED, result.turn.status)
        self.assertEqual("d2:agent_loop_cancelled", result.turn.error)
        self.assertIsNone(result.loop_result)
        self.assertEqual([], client.requests)
        self.assert_thread_detached(thread.thread_id)

    def test_cancellation_during_stream_cancels_running_turn(self) -> None:
        """模型流中协作取消由 Worker 转为 D1 CANCELLED，半截输出不会完成 Turn。"""
        thread, turn = self.create_turn()
        token = CancellationToken()

        def cancel_mid_stream(request: ModelRequest) -> Iterable[ModelStreamEvent]:
            response_id = "response-cancel-mid-stream"
            yield TurnStarted(_header(request, response_id, 0), request.model)
            token.cancel()
            yield ItemStarted(
                _header(request, response_id, 1),
                0,
                "partial-item",
                OutputKind.ASSISTANT_TEXT,
            )

        client = ScriptedClient(cancel_mid_stream)
        worker = self.worker(client)

        result = worker.execute(
            turn.turn_id,
            turn.version,
            cancellation=token,
        )

        self.assertEqual(TurnStatus.CANCELLED, result.turn.status)
        self.assertEqual("d2:agent_loop_cancelled", result.turn.error)
        self.assertIsNone(result.loop_result)
        self.assertEqual(1, len(client.requests))
        self.assert_thread_detached(thread.thread_id)


if __name__ == "__main__":
    unittest.main()
