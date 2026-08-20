from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from koawa_agent_v2.execution.loop import AgentLoop
from koawa_agent_v2.control.event_store import WrongExpectedVersion
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelRequest,
    ModelTurn,
    OutputKind,
    StreamHeader,
    TurnCompleted,
    TurnStarted,
)
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.worker import TurnWorker


class BarrierRuntime(ThreadRuntime):
    def __init__(self, store, *, actor: str, barrier: threading.Barrier) -> None:
        super().__init__(store, actor=actor)
        self._barrier = barrier

    def start_turn(self, *args, **kwargs):
        self._barrier.wait(timeout=5)
        return super().start_turn(*args, **kwargs)


class ThreadSafeFinalClient:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def stream(self, request: ModelRequest):
        with self._lock:
            self.calls += 1
        response_id = f"response-{request.model_turn_id}"
        item = AssistantTextItem(0, "final-item", "done")

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
            (item,),
            FinishReason.STOP,
        )
        return (
            TurnStarted(header(0), request.model),
            ItemStarted(header(1), 0, item.item_id, OutputKind.ASSISTANT_TEXT),
            ItemCompleted(header(2), item),
            TurnCompleted(header(3), turn),
        )


class D2DispatchConcurrencyTest(unittest.TestCase):
    def test_two_physical_dispatches_cannot_share_one_start_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            barrier = threading.Barrier(2)
            runtime = BarrierRuntime(
                SqliteEventStore(Path(directory) / "dispatch.sqlite3"),
                actor="dispatch-test",
                barrier=barrier,
            )
            thread = runtime.create_thread("D:/work/repository")
            queued = runtime.create_turn(
                thread.thread_id,
                "finish once",
                expected_thread_version=thread.version,
            )
            client = ThreadSafeFinalClient()
            worker = TurnWorker(
                runtime,
                AgentLoop(client),
                provider="test-provider",
                model="test-model",
            )

            def dispatch():
                try:
                    return worker.execute(queued.turn_id, queued.version)
                except Exception as exc:
                    return exc

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = tuple(pool.map(lambda _: dispatch(), range(2)))

            successes = [result for result in results if not isinstance(result, Exception)]
            conflicts = [result for result in results if isinstance(result, WrongExpectedVersion)]
            self.assertEqual(1, len(successes))
            self.assertEqual(1, len(conflicts))
            self.assertEqual(1, client.calls)
            self.assertEqual(TurnStatus.COMPLETED, runtime.get_turn(queued.turn_id).status)


if __name__ == "__main__":
    unittest.main()
