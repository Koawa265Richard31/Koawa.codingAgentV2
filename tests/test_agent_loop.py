from __future__ import annotations

import unittest
from collections.abc import Callable, Iterable, Sequence
from typing import TypeAlias
from uuid import UUID, uuid4

from koawa_agent_v2.execution.loop import (
    AgentLoop,
    AgentLoopCancelled,
    AgentLoopError,
    AgentLoopLimits,
    CancellationToken,
    ToolExecutionContext,
    ToolExecutionResult,
)
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelProtocolError,
    ModelRequest,
    ModelStreamEvent,
    ModelStreamFailure,
    ModelTurn,
    OutputKind,
    StreamFailed,
    StreamFailureKind,
    StreamHeader,
    ToolCallEcho,
    ToolCallItem,
    ToolDefinition,
    ToolResultMessage,
    TurnCompleted,
    TurnStarted,
    UserMessage,
)


StreamScript: TypeAlias = Callable[[ModelRequest], Iterable[ModelStreamEvent]]


class ScriptedClient:
    """按测试脚本依次返回 canonical stream，并记录每轮完整请求。"""

    def __init__(self, *scripts: StreamScript | BaseException) -> None:
        self._scripts = list(scripts)
        self.requests: list[ModelRequest] = []

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        """消费一段脚本；异常脚本用于模拟 Provider 在调用入口失败。"""
        self.requests.append(request)
        if not self._scripts:
            raise AssertionError("unexpected model request")
        script = self._scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        return script(request)

    @property
    def remaining_scripts(self) -> int:
        """返回尚未被模型轮次消费的脚本数量。"""
        return len(self._scripts)


ToolOutcome: TypeAlias = (
    ToolExecutionResult
    | BaseException
    | Callable[[ToolCallItem, ToolExecutionContext], ToolExecutionResult]
)


class RecordingToolExecutor:
    """记录实际工具副作用边界，并按顺序返回结果或抛出异常。"""

    def __init__(
        self,
        *outcomes: ToolOutcome,
        definitions: Sequence[ToolDefinition] = (),
    ) -> None:
        self._outcomes = list(outcomes)
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
        """先记录调用身份，再执行当前测试安排的结果。"""
        self.calls.append((call, context))
        outcome: ToolOutcome
        if self._outcomes:
            outcome = self._outcomes.pop(0)
        else:
            outcome = ToolExecutionResult(f"ok:{call.name}")
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(call, context)
        return outcome


def _header(
    request: ModelRequest,
    response_id: str,
    sequence: int,
) -> StreamHeader:
    """为某轮模型请求创建身份一致、顺序连续的事件头。"""
    return StreamHeader(
        model_turn_id=request.model_turn_id,
        provider=request.provider,
        provider_response_id=response_id,
        sequence=sequence,
        provider_sequence=sequence,
    )


def _completed_stream(
    request: ModelRequest,
    output_items: Sequence[AssistantTextItem | ToolCallItem],
    finish_reason: FinishReason,
    *,
    response_id: str,
) -> tuple[ModelStreamEvent, ...]:
    """把完成态输出包装成最短但完整合法的 typed stream。"""
    events: list[ModelStreamEvent] = [
        TurnStarted(_header(request, response_id, 0), request.model)
    ]
    sequence = 1
    for item in output_items:
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
        events.append(
            ItemCompleted(_header(request, response_id, sequence), item)
        )
        sequence += 1

    turn = ModelTurn(
        model_turn_id=request.model_turn_id,
        provider=request.provider,
        model=request.model,
        provider_response_id=response_id,
        output_items=tuple(output_items),
        finish_reason=finish_reason,
    )
    events.append(
        TurnCompleted(_header(request, response_id, sequence), turn)
    )
    return tuple(events)


def _final_script(text: str, response_id: str) -> StreamScript:
    """创建只返回一个最终 assistant 文本的模型脚本。"""

    def script(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        item = AssistantTextItem(0, f"item-{response_id}", text)
        return _completed_stream(
            request,
            (item,),
            FinishReason.STOP,
            response_id=response_id,
        )

    return script


def _tool_script(
    calls: Sequence[tuple[str, str, str]],
    response_id: str,
) -> StreamScript:
    """创建包含一个或多个完整工具调用的模型脚本。"""

    def script(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        items = tuple(
            ToolCallItem(index, f"item-{call_id}", call_id, name, arguments)
            for index, (call_id, name, arguments) in enumerate(calls)
        )
        return _completed_stream(
            request,
            items,
            FinishReason.TOOL_CALLS,
            response_id=response_id,
        )

    return script


READ_FILE = ToolDefinition(
    "read_file",
    "读取文件",
    '{"type":"object","properties":{"path":{"type":"string"}}}',
)
SEARCH = ToolDefinition(
    "search",
    "搜索文本",
    '{"type":"object","properties":{"query":{"type":"string"}}}',
)


class AgentLoopTest(unittest.TestCase):
    """验证完整模型回合、工具批次、有界终止和取消的 Loop 语义。"""

    def test_tool_result_rejects_text_that_cannot_be_encoded_as_utf8(self) -> None:
        with self.assertRaisesRegex(ValueError, "tool result content is invalid"):
            ToolExecutionResult("invalid-\ud800-result")

    def run_loop(
        self,
        client: ScriptedClient,
        executor: RecordingToolExecutor | None = None,
        *,
        limits: AgentLoopLimits | None = None,
        cancellation: CancellationToken | None = None,
    ):
        """用固定身份和初始用户消息运行一个测试 Loop。"""
        loop = AgentLoop(client, tool_executor=executor, limits=limits)
        return loop.run(
            run_id=uuid4(),
            input_items=(UserMessage("input-1", "修复失败测试"),),
            provider="test-provider",
            model="test-model",
            cancellation=cancellation,
        )

    def test_two_tool_calls_feed_results_to_next_round_then_finish(self) -> None:
        """同一回合的两个调用都执行，结果按调用顺序进入下一轮后才能 final。"""
        client = ScriptedClient(
            _tool_script(
                (
                    ("call-read", "read_file", '{"path":"README.md"}'),
                    ("call-search", "search", '{"query":"TODO"}'),
                ),
                "response-tools",
            ),
            _final_script("修改完成，测试通过", "response-final"),
        )
        executor = RecordingToolExecutor(
            ToolExecutionResult("README content"),
            ToolExecutionResult("TODO not found"),
            definitions=(READ_FILE, SEARCH),
        )

        result = self.run_loop(client, executor)

        self.assertEqual("修改完成，测试通过", result.final_text)
        self.assertEqual(2, result.model_rounds)
        self.assertEqual(2, result.tool_calls)
        self.assertEqual(["read_file", "search"], [call.name for call, _ in executor.calls])
        self.assertEqual(2, len(client.requests))
        second_context = client.requests[1].input_items
        self.assertEqual(
            ["call-read", "call-search"],
            [item.call_ref.call_id for item in second_context if isinstance(item, ToolCallEcho)],
        )
        self.assertEqual(
            ["README content", "TODO not found"],
            [item.content for item in second_context if isinstance(item, ToolResultMessage)],
        )
        self.assertEqual(0, client.remaining_scripts)

    def test_completed_tool_item_is_not_executed_before_stream_terminal_validation(self) -> None:
        """即使 ToolCall 已 done，尾部 typed failure 仍必须让工具执行次数保持为零。"""

        def failed_tail(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
            call = ToolCallItem(0, "item-call", "call-read", "read_file", '{}')
            response_id = "response-failed-tail"
            return (
                TurnStarted(_header(request, response_id, 0), request.model),
                ItemStarted(
                    _header(request, response_id, 1),
                    0,
                    call.item_id,
                    OutputKind.TOOL_CALL,
                    call.call_id,
                    call.name,
                ),
                ItemCompleted(_header(request, response_id, 2), call),
                StreamFailed(
                    _header(request, response_id, 3),
                    StreamFailureKind.STREAM_INTERRUPTED,
                    "provider_stream_interrupted",
                    retryable=True,
                ),
            )

        client = ScriptedClient(failed_tail)
        executor = RecordingToolExecutor(definitions=(READ_FILE,))

        with self.assertRaises(ModelStreamFailure) as raised:
            self.run_loop(client, executor)

        self.assertEqual("provider_stream_interrupted", raised.exception.code)
        self.assertEqual([], executor.calls)

    def test_unknown_tool_blocks_the_entire_batch_before_first_execution(self) -> None:
        """后置未知工具必须全量拦截，不能先执行排在前面的合法副作用。"""
        client = ScriptedClient(
            _tool_script(
                (
                    ("call-known", "read_file", '{}'),
                    ("call-unknown", "delete_everything", '{}'),
                ),
                "response-unknown-tool",
            )
        )
        executor = RecordingToolExecutor(definitions=(READ_FILE,))

        with self.assertRaises(AgentLoopError) as raised:
            self.run_loop(client, executor)

        self.assertEqual("unknown_tool_requested", raised.exception.code)
        self.assertEqual([], executor.calls)

    def test_business_tool_error_is_visible_to_the_next_model_round(self) -> None:
        """普通工具失败是模型可见结果，不应被误判为 Loop 基础设施失败。"""
        client = ScriptedClient(
            _tool_script(
                (("call-read", "read_file", '{"path":"missing.txt"}'),),
                "response-tool-error",
            ),
            _final_script("文件不存在，已说明原因", "response-after-error"),
        )
        executor = RecordingToolExecutor(
            ToolExecutionResult("file_not_found", is_error=True),
            definitions=(READ_FILE,),
        )

        result = self.run_loop(client, executor)

        self.assertEqual("文件不存在，已说明原因", result.final_text)
        [tool_result] = [
            item
            for item in client.requests[1].input_items
            if isinstance(item, ToolResultMessage)
        ]
        self.assertEqual("file_not_found", tool_result.content)
        self.assertIs(True, tool_result.is_error)
        self.assertEqual(1, result.tool_calls)

    def test_executor_exception_becomes_a_stable_loop_failure(self) -> None:
        """Executor 基础设施异常不能泄漏原文，只能跨边界传播稳定分类。"""
        client = ScriptedClient(
            _tool_script(
                (("call-read", "read_file", '{}'),),
                "response-executor-failure",
            )
        )
        executor = RecordingToolExecutor(
            RuntimeError("secret backend detail"),
            definitions=(READ_FILE,),
        )

        with self.assertRaises(AgentLoopError) as raised:
            self.run_loop(client, executor)

        self.assertEqual("tool_executor_failed", raised.exception.code)
        self.assertEqual(1, len(executor.calls))
        self.assertNotIn("secret backend detail", str(raised.exception))

    def test_last_allowed_round_may_return_final(self) -> None:
        """最后一轮仍可返回 final，轮数上限不是提前一轮终止。"""
        client = ScriptedClient(
            _tool_script(
                (("call-read", "read_file", '{}'),),
                "response-round-one",
            ),
            _final_script("恰好在最后一轮完成", "response-round-two"),
        )
        executor = RecordingToolExecutor(
            ToolExecutionResult("content"),
            definitions=(READ_FILE,),
        )

        result = self.run_loop(
            client,
            executor,
            limits=AgentLoopLimits(max_model_rounds=2),
        )

        self.assertEqual("恰好在最后一轮完成", result.final_text)
        self.assertEqual(2, result.model_rounds)
        self.assertEqual(1, len(executor.calls))

    def test_tool_call_on_last_allowed_round_is_not_executed(self) -> None:
        """无限请求工具时，最后一轮因没有反馈预算而不得再产生工具副作用。"""
        client = ScriptedClient(
            _tool_script(
                (("call-one", "read_file", '{}'),),
                "response-round-one",
            ),
            _tool_script(
                (("call-two", "read_file", '{}'),),
                "response-round-two",
            ),
        )
        executor = RecordingToolExecutor(
            ToolExecutionResult("first"),
            ToolExecutionResult("must-not-run"),
            definitions=(READ_FILE,),
        )

        with self.assertRaises(AgentLoopError) as raised:
            self.run_loop(
                client,
                executor,
                limits=AgentLoopLimits(max_model_rounds=2),
            )

        self.assertEqual("max_model_rounds_exceeded", raised.exception.code)
        self.assertEqual(["call-one"], [call.call_id for call, _ in executor.calls])

    def test_cancel_before_model_call_makes_no_provider_or_tool_call(self) -> None:
        """预先取消必须在第一轮 Provider 调用前生效。"""
        token = CancellationToken()
        token.cancel()
        client = ScriptedClient(_final_script("不应返回", "response-unused"))
        executor = RecordingToolExecutor(definitions=(READ_FILE,))

        with self.assertRaises(AgentLoopCancelled):
            self.run_loop(
                client,
                executor,
                cancellation=token,
            )

        self.assertEqual([], client.requests)
        self.assertEqual([], executor.calls)

    def test_cancel_during_model_stream_discards_partial_output(self) -> None:
        """流式处理中取消时，半截文本不能成为成功结果或触发工具。"""
        token = CancellationToken()

        def cancel_mid_stream(request: ModelRequest) -> Iterable[ModelStreamEvent]:
            response_id = "response-cancelled"
            yield TurnStarted(_header(request, response_id, 0), request.model)
            token.cancel()
            yield ItemStarted(
                _header(request, response_id, 1),
                0,
                "partial-item",
                OutputKind.ASSISTANT_TEXT,
            )

        client = ScriptedClient(cancel_mid_stream)
        executor = RecordingToolExecutor(definitions=(READ_FILE,))

        with self.assertRaises(AgentLoopCancelled):
            self.run_loop(
                client,
                executor,
                cancellation=token,
            )

        self.assertEqual(1, len(client.requests))
        self.assertEqual([], executor.calls)

    def test_provider_exception_and_empty_stream_fail_closed(self) -> None:
        """Provider 抛异常和空 EOF 都必须类型化失败，不能伪造空 final。"""
        provider = ScriptedClient(RuntimeError("authorization header leaked"))
        with self.assertRaises(AgentLoopError) as provider_failure:
            self.run_loop(provider)
        self.assertEqual("model_client_failed", provider_failure.exception.code)
        self.assertNotIn("authorization header leaked", str(provider_failure.exception))

        empty = ScriptedClient(lambda _request: ())
        with self.assertRaises(ModelProtocolError) as empty_failure:
            self.run_loop(empty)
        self.assertEqual("empty_model_stream", empty_failure.exception.code)


if __name__ == "__main__":
    unittest.main()
