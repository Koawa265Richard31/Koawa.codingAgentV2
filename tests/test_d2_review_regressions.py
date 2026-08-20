from __future__ import annotations

import traceback
import unittest
from collections.abc import Callable, Iterable
from uuid import uuid4

from koawa_agent_v2.execution.loop import AgentLoop, AgentLoopError
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelProtocolError,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    ModelUsage,
    OutputKind,
    StreamHeader,
    ToolCallItem,
    ToolDefinition,
    TurnCompleted,
    TurnStarted,
    UserMessage,
)
from koawa_agent_v2.model.stream import assemble_model_stream


SECRET = "Authorization: Bearer sk-live-DO-NOT-LOG"


def _header(request: ModelRequest, sequence: int) -> StreamHeader:
    return StreamHeader(
        request.model_turn_id,
        request.provider,
        "response-review-regression",
        sequence,
        sequence,
    )


def _completed_stream(
    request: ModelRequest,
    item: AssistantTextItem | ToolCallItem,
    finish_reason: FinishReason,
) -> tuple[ModelStreamEvent, ...]:
    started = ItemStarted(
        _header(request, 1),
        item.canonical_index,
        item.item_id,
        item.kind,
        item.call_id if isinstance(item, ToolCallItem) else None,
        item.name if isinstance(item, ToolCallItem) else None,
    )
    turn = ModelTurn(
        request.model_turn_id,
        request.provider,
        request.model,
        "response-review-regression",
        (item,),
        finish_reason,
    )
    return (
        TurnStarted(_header(request, 0), request.model),
        started,
        ItemCompleted(_header(request, 2), item),
        TurnCompleted(_header(request, 3), turn),
    )


class ScriptedClient:
    def __init__(self, script: Callable[[ModelRequest], Iterable[ModelStreamEvent]]) -> None:
        self._script = script

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        return self._script(request)


class SecretExplodingExecutor:
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (ToolDefinition("read_file", "read", "{}"),)

    def execute(self, call, *, context):
        raise RuntimeError(SECRET)


class D2ReviewRegressionTest(unittest.TestCase):
    def _run_loop(self, loop: AgentLoop, **kwargs) -> AgentLoopError:
        with self.assertRaises(AgentLoopError) as raised:
            loop.run(
                run_id=uuid4(),
                input_items=(UserMessage("input-1", "do the task"),),
                provider="test-provider",
                model="test-model",
                **kwargs,
            )
        return raised.exception

    def assert_secret_not_in_traceback(self, error: AgentLoopError) -> None:
        rendered = "".join(traceback.format_exception(error))
        self.assertNotIn(SECRET, rendered)
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)

    def test_untrusted_boundary_exceptions_are_not_chained_into_tracebacks(self) -> None:
        """Provider、observer、tool 的原始异常正文都不能穿过安全边界。"""
        def provider_explodes(_request: ModelRequest):
            raise RuntimeError(SECRET)

        provider_error = self._run_loop(AgentLoop(ScriptedClient(provider_explodes)))
        self.assertEqual("model_client_failed", provider_error.code)
        self.assert_secret_not_in_traceback(provider_error)

        def final_script(request: ModelRequest):
            return _completed_stream(
                request,
                AssistantTextItem(0, "final-item", "done"),
                FinishReason.STOP,
            )

        def sink_explodes(_event: ModelStreamEvent) -> None:
            raise RuntimeError(SECRET)

        sink_error = self._run_loop(
            AgentLoop(ScriptedClient(final_script)),
            event_sink=sink_explodes,
        )
        self.assertEqual("model_event_sink_failed", sink_error.code)
        self.assert_secret_not_in_traceback(sink_error)

        def tool_script(request: ModelRequest):
            return _completed_stream(
                request,
                ToolCallItem(0, "tool-item", "call-1", "read_file", "{}"),
                FinishReason.TOOL_CALLS,
            )

        tool_error = self._run_loop(
            AgentLoop(
                ScriptedClient(tool_script),
                tool_executor=SecretExplodingExecutor(),
            ),
        )
        self.assertEqual("tool_executor_failed", tool_error.code)
        self.assert_secret_not_in_traceback(tool_error)

    def test_terminal_usage_without_usage_event_is_rejected(self) -> None:
        """UsageReported 缺失时，terminal 不能凭空引入另一份 usage 事实。"""
        model_turn_id = uuid4()
        started_header = StreamHeader(
            model_turn_id,
            "test-provider",
            "response-usage",
            0,
        )
        terminal_header = StreamHeader(
            model_turn_id,
            "test-provider",
            "response-usage",
            1,
        )
        turn = ModelTurn(
            model_turn_id,
            "test-provider",
            "test-model",
            "response-usage",
            (),
            FinishReason.STOP,
            ModelUsage(1, 2, 3),
        )

        with self.assertRaises(ModelProtocolError) as raised:
            assemble_model_stream(
                (TurnStarted(started_header, "test-model"), TurnCompleted(terminal_header, turn))
            )

        self.assertEqual("completed_turn_usage_mismatch", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
