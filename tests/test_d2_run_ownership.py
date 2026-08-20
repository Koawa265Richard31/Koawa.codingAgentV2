from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.execution.loop import AgentLoop, ToolExecutionResult
from koawa_agent_v2.control.event_store import WrongExpectedVersion
from koawa_agent_v2.model.protocol import (
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelRequest,
    ModelTurn,
    OutputKind,
    StreamHeader,
    ToolCallItem,
    ToolDefinition,
    TurnCompleted,
    TurnStarted,
)
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.worker import TurnWorker


class RecordingExecutor:
    def __init__(self) -> None:
        self.call_count = 0

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (ToolDefinition("read_file", "read", "{}"),)

    def execute(self, call, *, context) -> ToolExecutionResult:
        self.call_count += 1
        return ToolExecutionResult("should not run")


class CancelThenRequestToolClient:
    """模拟 Provider 流期间外部命令已经撤销当前 Run。"""

    def __init__(self, runtime: ThreadRuntime, turn_id) -> None:
        self._runtime = runtime
        self._turn_id = turn_id

    def stream(self, request: ModelRequest):
        running = self._runtime.get_turn(self._turn_id)
        self._runtime.cancel_turn(
            self._turn_id,
            "external cancellation won",
            expected_version=running.version,
        )
        response_id = "response-after-cancel"
        call = ToolCallItem(0, "item-after-cancel", "call-1", "read_file", "{}")

        def header(sequence: int) -> StreamHeader:
            return StreamHeader(
                request.model_turn_id,
                request.provider,
                response_id,
                sequence,
                sequence,
            )

        turn = ModelTurn(
            request.model_turn_id,
            request.provider,
            request.model,
            response_id,
            (call,),
            FinishReason.TOOL_CALLS,
        )
        return (
            TurnStarted(header(0), request.model),
            ItemStarted(
                header(1),
                call.canonical_index,
                call.item_id,
                OutputKind.TOOL_CALL,
                call.call_id,
                call.name,
            ),
            ItemCompleted(header(2), call),
            TurnCompleted(header(3), turn),
        )


class D2RunOwnershipTest(unittest.TestCase):
    def test_external_cancel_is_rechecked_before_tool_side_effect(self) -> None:
        """旧 Worker 在 Provider 返回后必须先复核 D1 ownership，再执行工具。"""
        with tempfile.TemporaryDirectory() as directory:
            runtime = ThreadRuntime(
                SqliteEventStore(Path(directory) / "ownership.sqlite3"),
                actor="ownership-test",
            )
            thread = runtime.create_thread("D:/work/repository")
            queued = runtime.create_turn(
                thread.thread_id,
                "read a file",
                expected_thread_version=thread.version,
            )
            executor = RecordingExecutor()
            worker = TurnWorker(
                runtime,
                AgentLoop(
                    CancelThenRequestToolClient(runtime, queued.turn_id),
                    tool_executor=executor,
                ),
                provider="test-provider",
                model="test-model",
            )

            with self.assertRaises(WrongExpectedVersion):
                worker.execute(queued.turn_id, queued.version)

            self.assertEqual(0, executor.call_count)
            cancelled = runtime.get_turn(queued.turn_id)
            self.assertEqual(TurnStatus.CANCELLED, cancelled.status)
            self.assertEqual("external cancellation won", cancelled.error)


if __name__ == "__main__":
    unittest.main()
