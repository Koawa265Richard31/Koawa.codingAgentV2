"""D23 §12.1 offline scripted golden: 100 model rounds, >=3 in-run
compactions, restart byte-equivalence, no dangling calls.

The golden drives AgentLoop with a durable recorder + a compacting loop
memory config, verifies that compaction actually fires multiple times, that
the final projection reproduces identically after a fresh reduce (restart),
and that no pending calls survive.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import AgentLoop, AgentLoopLimits
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ModelCallRef,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    ToolCallEcho,
    ToolCallItem,
    ToolDefinition,
    ToolResultMessage,
    TurnCompleted,
    UserMessage,
)
from koawa_agent_v2.recovery.context import reduce_execution
from koawa_agent_v2.recovery.execution import DurableExecutionRecorder
from koawa_agent_v2.recovery.store import CheckpointStore
from koawa_agent_v2.runtime.memory import MemoryConfig
from tests.test_agent_loop import (
    READ_FILE,
    RecordingToolExecutor,
    ScriptedClient,
    _final_script,
    _tool_script,
)


READ_FILE = ToolDefinition(
    "read_file", "读取文件",
    '{"type":"object","properties":{"path":{"type":"string"}}}',
)


class GoldenRecorderSink:
    """Wraps a real DurableExecutionRecorder as the loop compaction sink."""

    def __init__(self, recorder: DurableExecutionRecorder):
        self._recorder = recorder
        self.epochs = 0

    @property
    def _source_versions(self):
        return self._recorder._source_versions

    @property
    def tool_count(self):
        return self._recorder.tool_count

    @property
    def pending_calls(self):
        return self._recorder.pending_calls

    def source_versions_for(self, first: int, last: int):
        """Forward to the recorder's version-contiguous mapping."""
        return self._recorder.source_versions_for(first, last)

    def synced_context(self):
        """The recorder's authoritative context as ModelContextItems."""
        return self._recorder.synced_context()

    def compact(self, **kwargs):
        self.epochs += 1
        from koawa_agent_v2.recovery.execution import context_document
        replacement = kwargs["replacement"]
        if not isinstance(replacement, dict):
            replacement = context_document(replacement)
        digest = kwargs.get("source_event_ids_digest") or None
        return self._recorder.compact(
            epoch=kwargs["epoch"],
            source_first_version=kwargs["source_first_version"],
            source_last_version=kwargs["source_last_version"],
            source_event_ids_digest=digest,
            replacement=replacement,
            resulting_context_digest=kwargs.get("resulting_context_digest") or "",
            target_chars=kwargs["target_chars"],
        )


def _tool_stream(request: ModelRequest, call_id: str) -> tuple[ModelStreamEvent, ...]:
    item = ToolCallItem(0, f"i{call_id}", call_id, "read_file", '{"path":"a.py"}')
    return (
        ModelStreamEvent("test", "model", request.model_turn_id, None,
                         TurnCompleted(request.model_turn_id)),
        ModelStreamEvent("test", "model", request.model_turn_id, None,
                         item),
    )


def _final_stream(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
    item = AssistantTextItem(0, f"i{uuid4()}", "golden done")
    return (
        ModelStreamEvent("test", "model", request.model_turn_id, None,
                         TurnCompleted(request.model_turn_id)),
        ModelStreamEvent("test", "model", request.model_turn_id, None,
                         item),
    )


class GoldenCompactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.db"
        self.store = SqliteEventStore(self.path)
        self.runtime = ThreadRuntime(self.store)
        self.thread = self.runtime.create_thread("repo")
        queued = self.runtime.create_turn(
            self.thread.thread_id, "golden task",
            expected_thread_version=self.thread.version,
        )
        self.running = self.runtime.start_turn(queued.turn_id, queued.version)
        self.recorder = DurableExecutionRecorder(
            self.store, CheckpointStore(self.store),
            thread_id=self.thread.thread_id,
            turn_id=self.running.turn_id,
            run_id=self.running.current_run_id,
            turn_version=self.running.version,
            initial_context=(UserMessage("u1", "golden task"),),
            provider="test", model="model", max_output_tokens=4096,
        )
        self.sink = GoldenRecorderSink(self.recorder)

    def tearDown(self):
        self.tmp.cleanup()

    def test_100_rounds_compact_at_least_three_times_and_restart_parity(self):
        memory = MemoryConfig.from_mapping({
            "request_context_soft_chars": 4000,
            # hard raised for D13-D23-001: richer replacement blocks carry bounded
            # result-fact lines, so 100 rounds legitimately hold more than before.
            "request_context_hard_chars": 20000,
            "request_context_reserve_chars": 800,
            "compaction_target_chars": 2000,
            "conclusion_max_chars": 1000,
            "compaction_summary_max_chars": 1000,
            "in_run_keep_groups": 2,
            "max_compaction_epochs_per_run": 64,
        })
        executor = RecordingToolExecutor(
            *([ToolExecutionResultOK()] * 100),
            definitions=(READ_FILE,),
        )
        scripts = []
        for index in range(100):
            scripts.append(
                _tool_script((("c%d" % index, "read_file", '{"path":"a.py"}'),),
                             "r%d" % index)
            )
        scripts.append(_final_script("golden done", "rfinal"))
        client = ScriptedClient(*scripts)
        loop = AgentLoop(
            client,
            tool_executor=executor,
            memory=memory,
            compaction_sink=self.sink,
            limits=AgentLoopLimits(max_model_rounds=150, max_tool_calls=200),
        )
        result = loop.run(
            run_id=uuid4(),
            input_items=(UserMessage("u1", "golden task"),),
            provider="test",
            model="model",
            durable_sink=self.recorder,
        )
        self.assertEqual(result.final_text, "golden done")
        self.assertGreaterEqual(self.sink.epochs, 3)

        # Restart parity: a fresh reducer over the same facts reproduces the
        # loop's final context byte-for-byte (modulo replacement markers).
        events = self.store.read_stream(
            StreamId("run-execution", self.running.turn_id), limit=2000
        )
        projection = reduce_execution(events)
        final_context = [item for item in result.context]
        # The recorder's own context is authoritative for what was sent.
        recorder_docs = [dict(item) for item in self.recorder.context]
        reduced_docs = [dict(item) for item in projection.context]
        self.assertEqual(len(recorder_docs), len(reduced_docs))
        for expected, actual in zip(recorder_docs, reduced_docs):
            self.assertEqual(expected, actual)
        # No pending calls survive.
        self.assertEqual(projection.pending_tool_calls, ())
        self.assertEqual(tuple(self.recorder.pending_calls), ())


class ToolExecutionResultOK:
    def __init__(self):
        from koawa_agent_v2.execution.loop import ToolExecutionResult
        self._value = ToolExecutionResult('{"ok": true, "lines": []}')

    def __call__(self, call, context=None):
        return self._value


if __name__ == "__main__":
    unittest.main()
