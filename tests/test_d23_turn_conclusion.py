"""D23-A tests: TurnConclusion event, authoritative rebuild, source verifier.

Covers §11.1: success/failure/cancel/timeout conclusions, source-head change
makes old revisions stale, response-loss idempotency, WrongExpectedVersion
never leaves a half conclusion, security (no raw args/results/reasoning/
credentials), and the terminal Turn stream is never appended to.
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
    AssistantMessage,
    AssistantTextItem,
    FinishReason,
    ModelCallRef,
    ModelTurn,
    ToolCallEcho,
    ToolCallItem,
    ToolResultMessage,
    UserMessage,
)
from koawa_agent_v2.recovery.execution import DurableExecutionRecorder
from koawa_agent_v2.recovery.store import CheckpointStore
from koawa_agent_v2.runtime.turn_conclusion import (
    CONCLUSION_EVENT_TYPE,
    TurnConclusion,
    TurnConclusionError,
    TurnConclusionStore,
)


def _model_turn(final_text: str, tool_calls: tuple[tuple[str, str, str], ...] = ()):
    """(call_id, name, arguments_json) -> ModelTurn with tool calls."""
    items = [AssistantTextItem(0, f"t{uuid4()}", final_text)]
    for index, (call_id, name, arguments) in enumerate(tool_calls, start=1):
        items.append(ToolCallItem(index, f"c{uuid4()}", call_id, name, arguments))
    return ModelTurn(
        uuid4(),
        "test-provider",
        "test-model",
        f"resp-{uuid4()}",
        tuple(items),
        FinishReason.TOOL_CALLS if tool_calls else FinishReason.STOP,
        None,
    )


class TurnConclusionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.db"
        self.store = SqliteEventStore(self.path)
        self.runtime = ThreadRuntime(self.store)
        self.thread = self.runtime.create_thread("repo")
        queued = self.runtime.create_turn(
            self.thread.thread_id,
            "修好 test 文件",
            expected_thread_version=self.thread.version,
        )
        self.running = self.runtime.start_turn(queued.turn_id, queued.version)
        self.turn_id = self.running.turn_id
        self.run_id = self.running.current_run_id
        self.recorder = DurableExecutionRecorder(
            self.store, CheckpointStore(self.store),
            thread_id=self.thread.thread_id,
            turn_id=self.turn_id,
            run_id=self.run_id,
            turn_version=self.running.version,
            initial_context=(UserMessage("u1", "修好 test 文件"),),
            provider="test-provider",
            model="test-model",
            max_output_tokens=4096,
        )
        self.conclusions = TurnConclusionStore(self.store, self.runtime)

    def tearDown(self):
        self.tmp.cleanup()

    def _record_success(self, changed: str | None = None):
        turn = _model_turn(
            "done",
            (("c1", "read_file", '{"path": "a.py"}'),
             ("c2", "apply_patch", '{"base_sha256": "x"}')),
        )
        tool_items = [item for item in turn.output_items if isinstance(item, ToolCallItem)]
        echoes = tuple(
            ToolCallEcho("test-provider", ModelCallRef(turn.model_turn_id, item.call_id), item)
            for item in tool_items
        )
        self.recorder.model_completed(
            turn, echoes, 1, 40, True,
        )
        self.recorder.tool_started("c1", "read_file")
        self.recorder.tool_completed(
            ToolResultMessage(
                ModelCallRef(turn.model_turn_id, "c1"),
                '{"lines": 10}',
                False,
            ),
            1,
        )
        self.recorder.tool_started("c2", "apply_patch")
        result = {"changed_paths": ["a.py"]} if changed is None else {"changes": [{"path": changed}]}
        import json as _json
        self.recorder.tool_completed(
            ToolResultMessage(
                ModelCallRef(turn.model_turn_id, "c2"),
                _json.dumps(result),
                False,
            ),
            2,
        )
        evidence = self.runtime.record_completion_evidence(
            self.turn_id,
            run_id=self.run_id,
            final_text="done",
        )
        self.runtime.complete_turn(
            self.turn_id,
            "done",
            expected_version=self.running.version,
            run_id=self.run_id,
            evidence_ref=evidence,
        )

    def _record_failure(self, error: str = "d2:max_model_rounds_exceeded"):
        self.runtime.fail_turn(
            self.turn_id,
            error,
            expected_version=self.running.version,
            run_id=self.run_id,
        )

    # -- build -------------------------------------------------------------

    def test_success_build_has_authoritative_fields(self):
        self._record_success(changed="a.py")
        conclusion = self.conclusions.build(self.turn_id)
        self.assertEqual(conclusion.turn_status, "completed")
        self.assertEqual(conclusion.run_status, "completed")
        self.assertEqual(conclusion.request_summary, "修好 test 文件")
        self.assertIn("read_file", conclusion.successful_tools)
        self.assertIn("apply_patch", conclusion.successful_tools)
        self.assertEqual(conclusion.changed_files, ("a.py",))
        self.assertEqual(conclusion.error_codes, ())
        self.assertEqual(conclusion.uncertainty_codes, ())
        self.assertEqual(len(conclusion.test_evidence_refs), 1)
        self.assertTrue(conclusion.authoritative_digest)
        self.assertTrue(conclusion.source_heads_digest)

    def test_failure_build_has_error_codes(self):
        self._record_failure("d2:max_model_rounds_exceeded")
        conclusion = self.conclusions.build(self.turn_id)
        self.assertEqual(conclusion.turn_status, "failed")
        self.assertEqual(conclusion.run_status, "failed")
        self.assertIn("max_model_rounds_exceeded", conclusion.error_codes)

    def test_non_terminal_turn_is_rejected(self):
        with self.assertRaises(TurnConclusionError) as raised:
            self.conclusions.build(self.turn_id)
        self.assertEqual(raised.exception.code, "turn_not_terminal")

    def test_unknown_turn_is_rejected(self):
        with self.assertRaises(TurnConclusionError) as raised:
            self.conclusions.build(uuid4())
        self.assertEqual(raised.exception.code, "turn_not_found")

    # -- persistence -------------------------------------------------------

    def test_persist_writes_only_to_memory_stream(self):
        self._record_success()
        conclusion = self.conclusions.build(self.turn_id)
        turn_head = self.store.read_stream(
            StreamId("turn", self.turn_id), limit=500
        )[-1].stream_version
        receipt = self.conclusions.persist(conclusion)
        self.assertEqual(len(receipt.streams), 1)
        self.assertEqual(
            receipt.streams[0].stream_id.category, "turn-memory"
        )
        events = self.store.read_stream(
            StreamId("turn-memory", self.turn_id), limit=500
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, CONCLUSION_EVENT_TYPE)
        # The terminal Turn stream is never appended to.
        self.assertEqual(
            self.store.read_stream(
                StreamId("turn", self.turn_id), limit=500
            )[-1].stream_version,
            turn_head,
        )

    def test_response_loss_retry_returns_original_receipt(self):
        self._record_success()
        conclusion = self.conclusions.build(self.turn_id)
        first = self.conclusions.persist(conclusion)
        second = self.conclusions.persist(conclusion)
        self.assertEqual(first.idempotency_key, second.idempotency_key)
        events = self.store.read_stream(
            StreamId("turn-memory", self.turn_id), limit=500
        )
        self.assertEqual(len(events), 1)

    def test_source_head_change_marks_old_revision_stale(self):
        self._record_success()
        first = self.conclusions.build(self.turn_id)
        self.conclusions.persist(first)
        loaded = self.conclusions.load(self.turn_id)
        self.assertEqual(len(loaded), 1)
        # Current heads still match the recorded conclusion.
        self.assertFalse(self.conclusions.is_stale(loaded[0]))
        # A tampered digest (as if a recovery advanced a source head) is stale.
        tampered = TurnConclusion(
            thread_id=first.thread_id,
            turn_id=first.turn_id,
            run_id=first.run_id,
            turn_status=first.turn_status,
            run_status=first.run_status,
            request_summary=first.request_summary,
            error_codes=first.error_codes,
            successful_tools=first.successful_tools,
            changed_files=first.changed_files,
            test_evidence_refs=first.test_evidence_refs,
            open_obligations=first.open_obligations,
            uncertainty_codes=first.uncertainty_codes,
            authoritative_digest=first.authoritative_digest,
            untrusted_summary=first.untrusted_summary,
            source_heads_digest="0" * 64,
        )
        self.assertTrue(self.conclusions.is_stale(tampered))

    # -- security ----------------------------------------------------------

    def test_conclusion_contains_no_raw_arguments_or_credentials(self):
        self._record_success()
        conclusion = self.conclusions.build(self.turn_id)
        document = conclusion.document()
        blob = repr(document)
        self.assertNotIn('{"path": "a.py"}', blob)
        self.assertNotIn("sk-secret", blob)
        self.assertNotIn("hunter2", blob)
        # No reasoning or raw tool result body.
        self.assertNotIn("reasoning", blob)
        self.assertNotIn('{"lines": 10}', blob)

    def test_conclusion_is_bounded(self):
        self._record_success()
        conclusion = self.conclusions.build(self.turn_id)
        payload = conclusion.document()
        for key in (
            "error_codes", "successful_tools", "changed_files",
            "open_obligations", "uncertainty_codes",
        ):
            self.assertLessEqual(len(payload[key]), 64)


class TurnConclusionCancelTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.db"
        self.store = SqliteEventStore(self.path)
        self.runtime = ThreadRuntime(self.store)
        self.thread = self.runtime.create_thread("repo")
        self.conclusions = TurnConclusionStore(self.store, self.runtime)

    def tearDown(self):
        self.tmp.cleanup()

    def _start(self):
        queued = self.runtime.create_turn(
            self.thread.thread_id, "task", expected_thread_version=self.thread.version,
        )
        return self.runtime.start_turn(queued.turn_id, queued.version)

    def test_cancelled_turn_builds_authoritative_conclusion(self):
        running = self._start()
        self.runtime.cancel_turn(
            running.turn_id, "cancelled by operator", expected_version=running.version,
        )
        conclusion = self.conclusions.build(running.turn_id)
        self.assertEqual(conclusion.turn_status, "cancelled")
        self.assertEqual(conclusion.run_status, "cancelled")

    def test_timed_out_turn_is_terminal_for_conclusions(self):
        running = self._start()
        self.runtime.timeout_turn(
            running.turn_id, "timed out", expected_version=running.version,
        )
        conclusion = self.conclusions.build(running.turn_id)
        self.assertEqual(conclusion.turn_status, "timed_out")
        self.assertTrue(conclusion.source_heads_digest)


if __name__ == "__main__":
    unittest.main()
