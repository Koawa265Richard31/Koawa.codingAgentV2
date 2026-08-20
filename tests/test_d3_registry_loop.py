from __future__ import annotations

import json
import unittest
from collections.abc import Callable, Iterable, Sequence
from dataclasses import FrozenInstanceError, dataclass
from typing import TypeAlias
from uuid import uuid4

from koawa_agent_v2.execution.loop import (
    AgentLoop,
    ToolExecutionContext,
    ToolExecutionResult,
)
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelCallRef,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    OutputKind,
    StreamHeader,
    ToolCallItem,
    ToolResultMessage,
    TurnCompleted,
    TurnStarted,
    UserMessage,
)
from koawa_agent_v2.tools.errors import ToolConfigurationError
from koawa_agent_v2.tools.registry import ToolRegistry
from koawa_agent_v2.tools.schema import ToolSpec


StreamScript: TypeAlias = Callable[[ModelRequest], Iterable[ModelStreamEvent]]


@dataclass(frozen=True, slots=True)
class EchoArguments:
    text: str


def _schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    }


def _spec(name: str = "echo") -> ToolSpec[EchoArguments]:
    return ToolSpec(name, "回显一段文本", EchoArguments, _schema())


class ScriptedClient:
    """依次运行模型脚本，并保留每轮实际收到的 Registry definitions。"""

    def __init__(self, *scripts: StreamScript) -> None:
        self._scripts = list(scripts)
        self.requests: list[ModelRequest] = []

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        self.requests.append(request)
        if not self._scripts:
            raise AssertionError("unexpected model request")
        return self._scripts.pop(0)(request)


class RecordingHandler:
    """记录 Registry 解码后的 typed arguments 与原执行身份。"""

    def __init__(self) -> None:
        self.calls: list[tuple[EchoArguments, ToolExecutionContext]] = []

    def __call__(
        self,
        arguments: EchoArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        self.calls.append((arguments, context))
        return ToolExecutionResult(f"echo:{arguments.text}")


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
    items: Sequence[AssistantTextItem | ToolCallItem],
    finish_reason: FinishReason,
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
        tuple(items),
        finish_reason,
    )
    events.append(TurnCompleted(_header(request, response_id, sequence), turn))
    return tuple(events)


def _tool_script(arguments_json: str, response_id: str) -> StreamScript:
    def script(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        call = ToolCallItem(
            0,
            f"item-{response_id}",
            f"call-{response_id}",
            "echo",
            arguments_json,
        )
        return _completed_stream(
            request,
            (call,),
            FinishReason.TOOL_CALLS,
            response_id=response_id,
        )

    return script


def _final_script(text: str, response_id: str) -> StreamScript:
    def script(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        item = AssistantTextItem(0, f"item-{response_id}", text)
        return _completed_stream(
            request,
            (item,),
            FinishReason.STOP,
            response_id=response_id,
        )

    return script


class D3RegistryLoopTest(unittest.TestCase):
    def run_loop(self, client: ScriptedClient, registry: ToolRegistry):
        return AgentLoop(client, tool_executor=registry).run(
            run_id=uuid4(),
            input_items=(UserMessage("input-1", "调用 echo"),),
            provider="test-provider",
            model="test-model",
        )

    def test_registry_decodes_frozen_arguments_and_preserves_execution_context(self) -> None:
        """合法调用经同源 definition/schema/decoder 后把结果送入下一轮。"""
        handler = RecordingHandler()
        registry = ToolRegistry()
        spec = _spec()
        registry.register(spec, handler)

        def final_after_result(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
            results = [
                item
                for item in request.input_items
                if isinstance(item, ToolResultMessage)
            ]
            self.assertEqual(1, len(results))
            self.assertEqual("echo:hello", results[0].content)
            self.assertIs(False, results[0].is_error)
            return _final_script("done", "response-final")(request)

        client = ScriptedClient(
            _tool_script('{"text":"hello"}', "response-tool"),
            final_after_result,
        )
        run_id = uuid4()
        result = AgentLoop(client, tool_executor=registry).run(
            run_id=run_id,
            input_items=(UserMessage("input-1", "调用 echo"),),
            provider="test-provider",
            model="test-model",
        )

        self.assertEqual("done", result.final_text)
        self.assertEqual(2, result.model_rounds)
        self.assertEqual(1, result.tool_calls)
        self.assertEqual(1, len(handler.calls))
        arguments, context = handler.calls[0]
        self.assertIsInstance(arguments, EchoArguments)
        self.assertEqual(EchoArguments("hello"), arguments)
        with self.assertRaises(FrozenInstanceError):
            arguments.text = "changed"  # type: ignore[misc]
        self.assertEqual(run_id, context.run_id)
        self.assertEqual(client.requests[0].model_turn_id, context.model_turn_id)
        self.assertEqual(1, context.model_round)
        self.assertEqual(
            ModelCallRef(client.requests[0].model_turn_id, "call-response-tool"),
            context.call_ref,
        )
        self.assertEqual(
            (spec.definition(),),
            client.requests[0].tool_definitions,
        )
        self.assertEqual(
            client.requests[0].tool_definitions,
            client.requests[1].tool_definitions,
        )

    def test_invalid_arguments_are_model_visible_without_calling_handler(self) -> None:
        """缺失、额外和错类型参数均稳定失败，且模型仍可进入下一轮。"""
        cases = (
            ("missing", "{}", "missing_required", "text"),
            (
                "additional",
                '{"text":"safe","unexpected":"do-not-echo"}',
                "additional_property",
                "unexpected",
            ),
            ("wrong-type", '{"text":7}', "wrong_type", "text"),
        )
        for name, arguments_json, reason, field in cases:
            with self.subTest(name=name):
                handler = RecordingHandler()
                registry = ToolRegistry()
                registry.register(_spec(), handler)

                def final_after_error(
                    request: ModelRequest,
                    *,
                    expected_reason: str = reason,
                    expected_field: str = field,
                ) -> tuple[ModelStreamEvent, ...]:
                    results = [
                        item
                        for item in request.input_items
                        if isinstance(item, ToolResultMessage)
                    ]
                    self.assertEqual(1, len(results))
                    self.assertIs(True, results[0].is_error)
                    self.assertEqual(
                        {
                            "error": {
                                "code": "invalid_tool_arguments",
                                "field": expected_field,
                                "reason": expected_reason,
                            }
                        },
                        json.loads(results[0].content),
                    )
                    self.assertNotIn("do-not-echo", results[0].content)
                    return _final_script("handled", f"response-final-{name}")(
                        request
                    )

                client = ScriptedClient(
                    _tool_script(arguments_json, f"response-invalid-{name}"),
                    final_after_error,
                )

                result = self.run_loop(client, registry)

                self.assertEqual("handled", result.final_text)
                self.assertEqual(2, result.model_rounds)
                self.assertEqual(1, result.tool_calls)
                self.assertEqual([], handler.calls)
                self.assertEqual(2, len(client.requests))

    def test_definitions_and_dispatch_are_one_sealed_source(self) -> None:
        """AgentLoop 取快照即 seal，后续注册不能改变模型视图或分发表。"""
        registry = ToolRegistry()
        handler = RecordingHandler()
        registry.register(_spec("zeta"), handler)
        registry.register(_spec("alpha"), handler)
        client = ScriptedClient(_final_script("done", "response-final-only"))
        loop = AgentLoop(client, tool_executor=registry)
        definitions_before = registry.definitions()

        with self.assertRaises(ToolConfigurationError) as raised:
            registry.register(_spec("late_tool"), handler)

        result = loop.run(
            run_id=uuid4(),
            input_items=(UserMessage("input-1", "直接回答"),),
            provider="test-provider",
            model="test-model",
        )

        self.assertEqual("tool_registry_sealed", raised.exception.code)
        self.assertTrue(registry.sealed)
        self.assertIs(definitions_before, registry.definitions())
        self.assertEqual(["alpha", "zeta"], [item.name for item in definitions_before])
        self.assertEqual(definitions_before, client.requests[0].tool_definitions)
        self.assertEqual("done", result.final_text)
        self.assertEqual([], handler.calls)


if __name__ == "__main__":
    unittest.main()
