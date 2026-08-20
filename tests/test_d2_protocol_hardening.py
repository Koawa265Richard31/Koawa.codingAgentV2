from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.execution.loop import AgentLoop, AgentLoopError, ToolExecutionResult
from koawa_agent_v2.model.protocol import (
    AssistantMessage,
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelCallRef,
    ModelProtocolError,
    ModelRequest,
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
from koawa_agent_v2.model.stream import assemble_model_stream
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.model.openai_client import _request_body
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.worker import TurnWorker


def _header(
    request: ModelRequest,
    sequence: int,
    *,
    provider: str | None = None,
    response_id: str = "response-hardening",
) -> StreamHeader:
    return StreamHeader(
        request.model_turn_id,
        provider or request.provider,
        response_id,
        sequence,
        sequence,
    )


class CrossProviderToolClient:
    def stream(self, request: ModelRequest):
        provider = "untrusted-provider"
        call = ToolCallItem(0, "item-1", "call-1", "read_file", "{}")
        turn = ModelTurn(
            request.model_turn_id,
            provider,
            request.model,
            "response-hardening",
            (call,),
            FinishReason.TOOL_CALLS,
        )
        return (
            TurnStarted(_header(request, 0, provider=provider), request.model),
            ItemStarted(
                _header(request, 1, provider=provider),
                0,
                call.item_id,
                OutputKind.TOOL_CALL,
                call.call_id,
                call.name,
            ),
            ItemCompleted(_header(request, 2, provider=provider), call),
            TurnCompleted(_header(request, 3, provider=provider), turn),
        )


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (ToolDefinition("read_file", "read", "{}"),)

    def execute(self, call, *, context) -> ToolExecutionResult:
        self.calls += 1
        return ToolExecutionResult("unexpected")


class CancelledStreamClient:
    def stream(self, request: ModelRequest):
        return (
            TurnStarted(_header(request, 0), request.model),
            StreamFailed(
                _header(request, 1),
                StreamFailureKind.CANCELLED,
                "provider_cancelled",
                False,
            ),
        )


class D2ProtocolHardeningTest(unittest.TestCase):
    def test_cross_provider_turn_is_rejected_before_tool_execution(self) -> None:
        executor = RecordingExecutor()
        loop = AgentLoop(CrossProviderToolClient(), tool_executor=executor)

        with self.assertRaises(AgentLoopError) as raised:
            loop.run(
                run_id=uuid4(),
                input_items=(UserMessage("input-1", "read"),),
                provider="approved-provider",
                model="test-model",
            )

        self.assertEqual("model_provider_identity_mismatch", raised.exception.code)
        self.assertEqual(0, executor.calls)

    def test_stream_failure_preserves_kind_and_provider_cancel_maps_to_cancelled(self) -> None:
        model_turn_id = uuid4()
        start = StreamHeader(model_turn_id, "provider", "response", 0)
        failed = StreamHeader(model_turn_id, "provider", "response", 1)
        with self.assertRaises(ModelStreamFailure) as raised:
            assemble_model_stream(
                (
                    TurnStarted(start, "model"),
                    StreamFailed(
                        failed,
                        StreamFailureKind.CANCELLED,
                        "provider_cancelled",
                    ),
                )
            )
        self.assertIs(StreamFailureKind.CANCELLED, raised.exception.kind)

        with tempfile.TemporaryDirectory() as directory:
            runtime = ThreadRuntime(
                SqliteEventStore(Path(directory) / "cancelled-stream.sqlite3"),
                actor="cancelled-stream-test",
            )
            thread = runtime.create_thread("D:/work/repository")
            queued = runtime.create_turn(
                thread.thread_id,
                "cancel at provider",
                expected_thread_version=thread.version,
            )
            worker = TurnWorker(
                runtime,
                AgentLoop(CancelledStreamClient()),
                provider="test-provider",
                model="test-model",
            )

            result = worker.execute(queued.turn_id, queued.version)

            self.assertEqual(TurnStatus.CANCELLED, result.turn.status)
            self.assertEqual("d2:provider_cancelled", result.turn.error)

    def test_chat_projection_preserves_error_flag_and_mixed_output_order(self) -> None:
        model_turn_id = uuid4()
        call = ToolCallItem(0, "call-item", "call-1", "read_file", "{}")
        text = AssistantTextItem(1, "text-item", "I will inspect it.")
        call_ref = ModelCallRef(model_turn_id, call.call_id)
        request = ModelRequest(
            uuid4(),
            "openai_compatible",
            "gpt-test",
            (
                UserMessage("input-1", "read"),
                ToolCallEcho("openai_compatible", call_ref, call),
                AssistantMessage("openai_compatible", model_turn_id, text),
                ToolResultMessage(call_ref, "false", is_error=True),
            ),
        )

        wire = json.loads(_request_body(request).decode("utf-8"))

        self.assertEqual("I will inspect it.", wire["messages"][1]["content"])
        self.assertEqual("call-1", wire["messages"][1]["tool_calls"][0]["id"])
        envelope = json.loads(wire["messages"][2]["content"])
        self.assertEqual({"is_error": True, "content": "false"}, envelope)


if __name__ == "__main__":
    unittest.main()
