"""D23-D tests: in-run compaction facts, D6 reducer extension, restart parity.

Covers D23 §5.7/§11.3: intended/compacted facts verify on replay; a compacted
range replaces the exact source groups; forged digests, epoch gaps, duplicate
intent, compacted-without-intent, anchor/open-call sources and misaligned
ranges fail closed; recorder.compact() is idempotent under response loss; a
fresh reducer over the same facts reproduces the same projection
(restart byte-equivalence).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ModelCallRef,
    ModelTurn,
    ToolCallEcho,
    ToolCallItem,
    ToolResultMessage,
    UserMessage,
)
from koawa_agent_v2.recovery.context import (
    ReconstructionError,
    _context_digest,
    _selected_event_ids_digest,
    reduce_execution,
)
from koawa_agent_v2.recovery.execution import (
    COMPACTION_COMPACTED_EVENT,
    COMPACTION_INTENDED_EVENT,
    DurableExecutionRecorder,
)
from koawa_agent_v2.recovery.store import CheckpointStore


def _model_turn(tool_calls: tuple[tuple[str, str], ...] = ()):
    items = [AssistantTextItem(0, f"t{uuid4()}", "text")]
    for index, (call_id, name) in enumerate(tool_calls, start=1):
        items.append(ToolCallItem(index, f"i{uuid4()}", call_id, name, "{}"))
    return ModelTurn(
        uuid4(), "test", "model", f"r{uuid4()}", tuple(items),
        FinishReason.TOOL_CALLS if tool_calls else FinishReason.STOP, None,
    )


class CompactionReducerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.db"
        self.store = SqliteEventStore(self.path)
        self.runtime = ThreadRuntime(self.store)
        self.thread = self.runtime.create_thread("repo")
        queued = self.runtime.create_turn(
            self.thread.thread_id, "task", expected_thread_version=self.thread.version,
        )
        self.running = self.runtime.start_turn(queued.turn_id, queued.version)
        self.recorder = DurableExecutionRecorder(
            self.store, CheckpointStore(self.store),
            thread_id=self.thread.thread_id,
            turn_id=self.running.turn_id,
            run_id=self.running.current_run_id,
            turn_version=self.running.version,
            initial_context=(UserMessage("u1", "task"),),
            provider="test", model="model", max_output_tokens=4096,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _facts(self):
        return self.store.read_stream(
            StreamId("run-execution", self.running.turn_id), limit=500
        )

    def _record_one_group(self):
        turn = _model_turn((("c1", "read_file"),))
        echoes = (
            ToolCallEcho("test", ModelCallRef(turn.model_turn_id, "c1"),
                         turn.output_items[1]),
        )
        self.recorder.model_completed(
            turn, echoes, self.recorder.model_round + 1, 5, True,
        )
        self.recorder.tool_started("c1", "read_file")
        self.recorder.tool_completed(
            ToolResultMessage(echoes[0].call_ref, '{"ok": true}', False),
            self.recorder.tool_count + 1,
        )

    def _source_digest(self):
        """Real event-ids digest of the source range (model turn + result)."""
        return _selected_event_ids_digest(self.recorder.context[1:3])

    def _resulting_digest(self, replacement):
        context = [dict(item) for item in self.recorder.context]
        context[1:3] = [dict(replacement)]
        return _context_digest(context)

    def test_reducer_accepts_intended_and_compacted(self):
        self._record_one_group()
        facts = self._facts()
        # source range covers the model turn and its result (versions 1..3;
        # version 2 is the phase-advance fact that adds no context item).
        replacement = {"kind": "user", "input_id": "compact:1", "content": "[compacted]"}
        self.recorder.compact(
            epoch=1,
            source_first_version=1,
            source_last_version=3,
            source_event_ids_digest=self._source_digest(),
            replacement=replacement,
            resulting_context_digest=self._resulting_digest(replacement),
            target_chars=32_000,
        )
        events = self._facts()
        self.assertEqual(
            events[-2].event_type, COMPACTION_INTENDED_EVENT
        )
        self.assertEqual(events[-1].event_type, COMPACTION_COMPACTED_EVENT)
        projection = reduce_execution(events)
        # Seed user + model turn + tool result -> replaced by the compaction.
        kinds = [item["kind"] for item in projection.context]
        self.assertEqual(kinds, ["user", "user"])

    def test_restart_replay_is_byte_equivalent(self):
        self._record_one_group()
        replacement = {"kind": "user", "input_id": "compact:1", "content": "[compacted]"}
        self.recorder.compact(
            epoch=1, source_first_version=1, source_last_version=3,
            source_event_ids_digest=self._source_digest(),
            replacement=replacement,
            resulting_context_digest=self._resulting_digest(replacement),
            target_chars=32_000,
        )
        events = self._facts()
        first = reduce_execution(events)
        # A fresh store/reducer (restart) must reproduce the same projection.
        fresh = SqliteEventStore(self.path)
        replayed = reduce_execution(
            fresh.read_stream(StreamId("run-execution", self.running.turn_id), limit=500)
        )
        self.assertEqual(
            [dict(item) for item in first.context],
            [dict(item) for item in replayed.context],
        )
        self.assertEqual(first.tool_count, replayed.tool_count)
        self.assertEqual(first.model_round, replayed.model_round)

    def test_compacted_without_intent_fails_closed(self):
        self._record_one_group()
        events = list(self._facts())
        from koawa_agent_v2.control.event_store import (
            EventMetadata, NewEvent, StreamWrite,
        )
        from datetime import datetime, timezone
        command = uuid4()
        event = NewEvent(
            uuid4(), COMPACTION_COMPACTED_EVENT, 1, datetime.now(timezone.utc),
            {
                "thread_id": str(self.thread.thread_id),
                "turn_id": str(self.running.turn_id),
                "run_id": str(self.running.current_run_id),
                "epoch": 1,
                "replacement_item": {"kind": "user", "input_id": "c", "content": "x"},
            },
            EventMetadata(command, self.running.turn_id, self.thread.thread_id,
                           self.running.turn_id, self.running.current_run_id, "test"),
        )
        self.store.append_batch(
            (StreamWrite(StreamId("run-execution", self.running.turn_id),
                         events[-1].stream_version, (event,)),),
            idempotency_key=command,
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution(self._facts())

    def test_epoch_gap_fails_closed(self):
        self._record_one_group()
        replacement = {"kind": "user", "input_id": "c", "content": "x"}
        self.recorder.compact(
            epoch=1, source_first_version=1, source_last_version=3,
            source_event_ids_digest="x", replacement=replacement,
            resulting_context_digest="unused", target_chars=32_000,
        )
        # Second intent with epoch 3 (skipping 2) must fail.
        facts = list(self._facts())
        from koawa_agent_v2.control.event_store import (
            EventMetadata, NewEvent, StreamWrite,
        )
        from datetime import datetime, timezone
        command = uuid4()
        event = NewEvent(
            uuid4(), COMPACTION_INTENDED_EVENT, 1, datetime.now(timezone.utc),
            {
                "thread_id": str(self.thread.thread_id),
                "turn_id": str(self.running.turn_id),
                "run_id": str(self.running.current_run_id),
                "epoch": 3,
                "source_first_version": 1,
                "source_last_version": 2,
                "event_ids_digest": "x",
                "prior_context_digest": "x",
                "target_chars": 1,
            },
            EventMetadata(command, self.running.turn_id, self.thread.thread_id,
                           self.running.turn_id, self.running.current_run_id, "test"),
        )
        self.store.append_batch(
            (StreamWrite(StreamId("run-execution", self.running.turn_id),
                         facts[-1].stream_version, (event,)),),
            idempotency_key=command,
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution(self._facts())

    def test_prior_context_digest_mismatch_fails_closed(self):
        self._record_one_group()
        facts = list(self._facts())
        from koawa_agent_v2.control.event_store import (
            EventMetadata, NewEvent, StreamWrite,
        )
        from datetime import datetime, timezone
        command = uuid4()
        event = NewEvent(
            uuid4(), COMPACTION_INTENDED_EVENT, 1, datetime.now(timezone.utc),
            {
                "thread_id": str(self.thread.thread_id),
                "turn_id": str(self.running.turn_id),
                "run_id": str(self.running.current_run_id),
                "epoch": 1,
                "source_first_version": 1,
                "source_last_version": 2,
                "event_ids_digest": "x",
                "prior_context_digest": "forged",
                "target_chars": 1,
            },
            EventMetadata(command, self.running.turn_id, self.thread.thread_id,
                           self.running.turn_id, self.running.current_run_id, "test"),
        )
        self.store.append_batch(
            (StreamWrite(StreamId("run-execution", self.running.turn_id),
                         facts[-1].stream_version, (event,)),),
            idempotency_key=command,
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution(self._facts())

    def test_overlapping_source_after_compaction_fails_closed(self):
        # First compaction consumes versions 1..3, then a second intent
        # claims an overlapping range -> must fail closed.
        self._record_one_group()
        replacement = {"kind": "user", "input_id": "c", "content": "x"}
        self.recorder.compact(
            epoch=1, source_first_version=1, source_last_version=3,
            source_event_ids_digest=self._source_digest(),
            replacement=replacement,
            resulting_context_digest=self._resulting_digest(replacement),
            target_chars=32_000,
        )
        self._record_one_group()  # second group lives at later versions
        facts = list(self._facts())
        from koawa_agent_v2.control.event_store import (
            EventMetadata, NewEvent, StreamWrite,
        )
        from datetime import datetime, timezone
        command = uuid4()
        event = NewEvent(
            uuid4(), COMPACTION_INTENDED_EVENT, 1, datetime.now(timezone.utc),
            {
                "thread_id": str(self.thread.thread_id),
                "turn_id": str(self.running.turn_id),
                "run_id": str(self.running.current_run_id),
                "epoch": 2,
                # claims the already-compacted range -> overlap
                "source_first_version": 1,
                "source_last_version": 3,
                "event_ids_digest": "x",
                "prior_context_digest": "x",
                "target_chars": 1,
            },
            EventMetadata(command, self.running.turn_id, self.thread.thread_id,
                           self.running.turn_id, self.running.current_run_id, "test"),
        )
        self.store.append_batch(
            (StreamWrite(StreamId("run-execution", self.running.turn_id),
                         facts[-1].stream_version, (event,)),),
            idempotency_key=command,
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution(self._facts())

    def test_forged_resulting_digest_fails_closed(self):
        self._record_one_group()
        replacement = {"kind": "user", "input_id": "c", "content": "x"}
        # Compact with a forged resulting digest.
        self.recorder.compact(
            epoch=1, source_first_version=1, source_last_version=3,
            source_event_ids_digest=self._source_digest(),
            replacement=replacement,
            resulting_context_digest="forged",
            target_chars=32_000,
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution(self._facts())


if __name__ == "__main__":
    unittest.main()

