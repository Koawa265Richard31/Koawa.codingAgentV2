from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import (
    AgentLoop,
    ToolExecutionContext,
    ToolExecutionResult,
)
from koawa_agent_v2.ledger import LedgerExecutor, READ_ONLY_PROFILE, ToolLedgerStore
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ModelCallRef,
    ToolCallItem,
    ToolDefinition,
)
from koawa_agent_v2.runtime.app import _trace_diagnostics
from koawa_agent_v2.runtime.cli import _completed_stream
from koawa_agent_v2.telemetry.trace import (
    BestEffortTraceSink,
    TraceProbe,
    TraceStore,
)


class _FailingEventStore:
    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure or RuntimeError("database unavailable")

    def read_stream(self, stream, *, after_version=-1, limit=500):
        return ()

    def append_batch(self, writes, *, idempotency_key):
        raise self.failure


class _FinalProvider:
    def stream(self, request):
        item = AssistantTextItem(0, "item-final", "done")
        yield from _completed_stream(
            request,
            (item,),
            FinishReason.STOP,
            "response-final",
        )


_PROBE = ToolDefinition(
    "probe",
    "I7 trace isolation probe",
    '{"type":"object","properties":{}}',
)


class _ToolDelegate:
    def definitions(self):
        return (_PROBE,)

    def execute(self, call, *, context):
        return ToolExecutionResult("committed-result")


class I7TraceIsolationTest(unittest.TestCase):
    def _failing_sink(self, failure: BaseException | None = None):
        return BestEffortTraceSink(
            TraceStore(_FailingEventStore(failure)),
            cas_retries=2,
        )

    def test_model_result_survives_trace_storage_failure(self) -> None:
        sink = self._failing_sink()
        loop = AgentLoop(
            _FinalProvider(),
            trace_sink=sink,
            correlation_id=uuid4(),
        )

        result = loop.run(
            run_id=uuid4(),
            input_items=(),
            provider="fixture",
            model="fixture",
        )

        self.assertEqual("done", result.final_text)
        self.assertEqual(1, sink.diagnostics().dropped_since_start)

    def test_ledger_claim_and_result_survive_trace_storage_failure(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SqliteEventStore(Path(temporary.name) / "runtime.sqlite3")
        runtime = ThreadRuntime(store, actor="i7-trace-test")
        thread = runtime.create_thread("trace-isolation")
        queued = runtime.create_turn(
            thread.thread_id,
            "trace isolation",
            expected_thread_version=thread.version,
        )
        running = runtime.start_turn(queued.turn_id, queued.version)
        model_turn_id = uuid4()
        call = ToolCallItem(0, "item-call", "call-1", "probe", "{}")
        context = ToolExecutionContext(
            running.current_run_id,
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, "call-1"),
            turn_id=running.turn_id,
            turn_version=running.version,
        )
        sink = self._failing_sink()
        ledger = ToolLedgerStore(store)
        executor = LedgerExecutor(
            _ToolDelegate(),
            ledger,
            {"probe": READ_ONLY_PROFILE},
            trace_sink=sink,
            correlation_id=uuid4(),
        )

        result = executor.execute(call, context=context)

        self.assertEqual("committed-result", result.content)
        record = ledger.load_for_call(running.turn_id, model_turn_id, "call-1")
        self.assertEqual("succeeded", record.state.value)
        # prepared, claimed and committed result probes each failed independently.
        self.assertEqual(3, sink.diagnostics().dropped_since_start)

    def test_process_control_signals_are_never_swallowed(self) -> None:
        probe = TraceProbe(uuid4(), "model", "round", {"kind": "round"})
        for signal in (KeyboardInterrupt(), SystemExit(9), GeneratorExit()):
            with self.subTest(signal=type(signal).__name__):
                sink = self._failing_sink(signal)
                with self.assertRaises(type(signal)):
                    sink.emit(probe)
                self.assertEqual(0, sink.diagnostics().dropped_since_start)

    def test_diagnostics_are_process_local_and_stable(self) -> None:
        sink = self._failing_sink()
        sink.emit(TraceProbe(uuid4(), "tool", "result", {"result_code": "ok"}))

        document = _trace_diagnostics(sink)

        self.assertEqual("process_local", document["scope"])
        self.assertEqual(1, document["dropped_since_start"])
        self.assertEqual("trace_storage_failed", document["last_error_code"])
        self.assertIsInstance(document["last_failure_at"], str)

    def test_idle_correlation_locks_are_not_retained(self) -> None:
        sink = self._failing_sink()
        for _ in range(1000):
            sink.emit(TraceProbe(uuid4(), "tool", "result", {"result_code": "ok"}))
        self.assertEqual(1000, sink.diagnostics().dropped_since_start)
        self.assertEqual(0, len(sink._correlation_locks))

    def test_concurrent_callers_share_lock_until_last_reference_is_released(self) -> None:
        sink = self._failing_sink()
        correlation = uuid4()
        barrier = Barrier(8)

        def acquire():
            with sink._correlation_lock(correlation) as lock:
                barrier.wait(timeout=10)
                with lock:
                    identity = id(lock)
                barrier.wait(timeout=10)
                return identity

        with ThreadPoolExecutor(max_workers=8) as executor:
            identities = list(executor.map(lambda _: acquire(), range(8)))
        self.assertEqual(1, len(set(identities)))
        self.assertEqual(0, len(sink._correlation_locks))

    def test_fault_callback_cancellation_is_never_swallowed(self) -> None:
        from koawa_agent_v2.control.event_store import StreamId, WrongExpectedVersion

        class CancelPort:
            def hit(self, point, facts):
                raise CancelledError()

        probe = TraceProbe(uuid4(), "tool", "result", {})
        for failure in (RuntimeError("unavailable"), WrongExpectedVersion(StreamId("trace", uuid4()), -1, 0)):
            with self.subTest(failure=type(failure).__name__):
                sink = BestEffortTraceSink(TraceStore(_FailingEventStore(failure)), fault_port=CancelPort())
                with self.assertRaises(CancelledError):
                    sink.emit(probe)
                self.assertEqual(0, sink.diagnostics().dropped_since_start)
                self.assertEqual(0, len(sink._correlation_locks))


if __name__ == "__main__":
    unittest.main()
