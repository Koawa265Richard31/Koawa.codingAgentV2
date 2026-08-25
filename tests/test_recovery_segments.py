"""Recovery segment integrity and canonical reducer tests (I4 handoff + I5).

These tests pin the run-execution seed v2 semantics: every run segment must
begin with exactly one seed; non-seed facts stay inside their segment; resume
seeds cannot overwrite context, reset counters or change pinned semantics;
model_round and tool_count are derived strictly from events; corrupt logs
fail closed.
"""

from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timezone
from uuid import UUID, uuid4

from koawa_agent_v2.control.durable_json import canonical_json_bytes_v1
from koawa_agent_v2.control.event_store import EventMetadata, StoredEvent, StreamId
from koawa_agent_v2.recovery.context import (
    ExecutionProjection,
    ReconstructionError,
    SEED_PROJECTION_KEYS,
    projection_digest,
    projection_document,
    reduce_execution,
)
from koawa_agent_v2.recovery.execution import validate_execution_segments
from koawa_agent_v2.recovery.protocol import RunPhase


def _stored(stream_version, event_type, payload, run_id, thread_id, turn_id, schema_version=None):
    return StoredEvent(
        event_id=uuid4(),
        stream_id=StreamId("run-execution", turn_id),
        stream_version=stream_version,
        global_position=stream_version + 1,
        commit_id=uuid4(),
        commit_index=0,
        commit_size=1,
        event_type=event_type,
        schema_version=(
            int(event_type.rsplit(".v", 1)[1])
            if schema_version is None
            else schema_version
        ),
        occurred_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 1, 2, 3, 4, 6, tzinfo=timezone.utc),
        payload=payload,
        metadata=EventMetadata(
            command_id=uuid4(),
            correlation_id=uuid4(),
            thread_id=thread_id,
            turn_id=turn_id,
            run_id=run_id,
            actor="test",
        ),
    )


def _context_item(kind="user", content="hello"):
    return {"kind": kind, "input_id": "u1", "content": content, "source_interrupt_id": None}


def _request_semantics(context_docs, provider="test", model="model", tool_digest=None):
    return {
        "protocol_version": 1,
        "provider": provider,
        "model": model,
        "max_output_tokens": 4096,
        "input_items": [dict(item) for item in context_docs],
        "tool_definitions": [],
        "tool_catalog_digest": tool_digest or ("0" * 64),
    }


def _seed_payload(thread_id, turn_id, run_id, context_docs, *, attempt=1, turn_stream_version=1, resume=None, counters=None):
    projection = {
        "context": [dict(item) for item in context_docs],
        "final_text": None,
        "input_tokens": 0,
        "model_round": 0,
        "output_chars": 0,
        "output_tokens": 0,
        "pending_tool_calls": [],
        "phase": "ready_for_model",
        "tool_count": 0,
    }
    if counters:
        projection.update(counters)
    return {
        "seed_schema_version": 2,
        "thread_id": str(thread_id),
        "turn_id": str(turn_id),
        "run_id": str(run_id),
        "attempt": attempt,
        "turn_stream_version": turn_stream_version,
        "request_semantics": _request_semantics(context_docs),
        "projection": projection,
        "resume": resume,
    }


class ExecutionSegmentTest(unittest.TestCase):
    def setUp(self):
        self.thread_id = uuid4()
        self.turn_id = uuid4()
        self.run_id = uuid4()
        self.doc = _context_item()

    def _seed(self, version=0, run_id=None, resume=None, counters=None, context_docs=None):
        docs = [dict(self.doc)] if context_docs is None else context_docs
        payload = _seed_payload(
            self.thread_id,
            self.turn_id,
            run_id or self.run_id,
            docs,
            resume=resume,
            counters=counters,
        )
        run = run_id or self.run_id
        return _stored(version, "run.context-seeded.v2", payload, run, self.thread_id, self.turn_id)

    def test_first_seed_reduces_to_its_projection(self):
        projection = reduce_execution((self._seed(),))
        self.assertEqual(projection.model_round, 0)
        self.assertEqual(projection.context[0]["content"], "hello")
        self.assertEqual(projection.last_run_id, self.run_id)
        self.assertEqual(projection.execution_version, 0)

    def test_empty_stream_fails_closed(self):
        with self.assertRaises(ReconstructionError):
            reduce_execution(())

    def test_version_gap_fails_closed(self):
        events = (self._seed(version=0), self._seed(version=2))
        with self.assertRaises(ReconstructionError):
            reduce_execution(events)

    def test_schema_suffix_mismatch_fails_closed(self):
        seed = self._seed(version=0)
        bad = _stored(
            0,
            "run.context-seeded.v2",
            seed.payload,
            self.run_id,
            self.thread_id,
            self.turn_id,
            schema_version=1,
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution((bad,))

    def test_unknown_event_type_fails_closed(self):
        events = (self._seed(version=0),)
        events += (
            _stored(
                1,
                "run.mystery.v1",
                {"run_id": str(self.run_id)},
                self.run_id,
                self.thread_id,
                self.turn_id,
            ),
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution(events)

    def test_fact_without_seed_fails_closed(self):
        fact = _stored(
            0,
            "run.phase-advanced.v1",
            {"run_id": str(self.run_id), "phase": "ready_for_tool"},
            self.run_id,
            self.thread_id,
            self.turn_id,
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution((fact,))

    def test_foreign_run_fact_fails_closed(self):
        events = (self._seed(version=0),)
        events += (
            _stored(
                1,
                "run.phase-advanced.v1",
                {"run_id": str(uuid4()), "phase": "ready_for_tool"},
                uuid4(),
                self.thread_id,
                self.turn_id,
            ),
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution(events)
        with self.assertRaises(ReconstructionError):
            validate_execution_segments(events)

    def test_forged_second_seed_rejected(self):
        events = (self._seed(version=0), self._seed(version=1))
        with self.assertRaises(ReconstructionError):
            reduce_execution(events)
        with self.assertRaises(ReconstructionError):
            validate_execution_segments(events)

    def test_v1_seed_allowed_only_as_first_event(self):
        legacy = _stored(
            0,
            "run.context-seeded.v1",
            {"context": [dict(self.doc)], "run_id": str(self.run_id)},
            self.run_id,
            self.thread_id,
            self.turn_id,
        )
        projection = reduce_execution((legacy,))
        self.assertEqual(projection.context[0]["content"], "hello")
        # a second v1 seed is forbidden
        events = (legacy, _stored(1, "run.context-seeded.v1", {"context": [], "run_id": str(self.run_id)}, self.run_id, self.thread_id, self.turn_id))
        with self.assertRaises(ReconstructionError):
            reduce_execution(events)
        with self.assertRaises(ReconstructionError):
            validate_execution_segments(events)

    def test_model_round_must_increment_by_one(self):
        model_turn = {
            "model_turn": {"final_text": "ok"},
            "context_items": [dict(self.doc)],
            "model_round": 1,
            "output_chars": 3,
            "input_tokens": 2,
            "output_tokens": 1,
            "next_phase": "ready_to_finalize",
        }
        events = (
            self._seed(version=0),
            _stored(1, "model.turn-completed.v1", {"run_id": str(self.run_id), **model_turn}, self.run_id, self.thread_id, self.turn_id),
        )
        projection = reduce_execution(events)
        self.assertEqual(projection.model_round, 1)
        self.assertEqual(projection.phase, RunPhase.READY_TO_FINALIZE)
        self.assertEqual(projection.final_text, "ok")
        # a jump from 0 to 2 is corrupt
        skipped = (
            self._seed(version=0),
            _stored(1, "model.turn-completed.v1", {"run_id": str(self.run_id), **{**model_turn, "model_round": 2}}, self.run_id, self.thread_id, self.turn_id),
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution(skipped)

    def test_tool_count_derived_from_results(self):
        tool_result = {"context_item": {"kind": "tool_result", "model_turn_id": str(uuid4()), "call_id": "c1", "content": "r", "is_error": False}, "tool_count": 1}
        events = (
            self._seed(version=0),
            _stored(1, "tool.result-recorded.v1", {"run_id": str(self.run_id), **tool_result}, self.run_id, self.thread_id, self.turn_id),
        )
        projection = reduce_execution(events)
        self.assertEqual(projection.tool_count, 1)
        # tool_count must derive from each result: a jump to 2 from 1 is fine
        # only if this is the second result; from zero to two is corrupt
        bad = (
            self._seed(version=0),
            _stored(1, "tool.result-recorded.v1", {"run_id": str(self.run_id), **{**tool_result, "tool_count": 2}}, self.run_id, self.thread_id, self.turn_id),
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution(bad)

    def test_unpaired_tool_calls_stay_pending(self):
        item = {"kind": "tool_call", "model_turn_id": str(uuid4()), "call_id": "c1", "provider": "test", "item": {"name": "read", "arguments_json": "{}"}}
        model_turn = {
            "model_turn": {"final_text": None},
            "context_items": [item],
            "model_round": 1,
            "output_chars": 0,
            "next_phase": "ready_for_tool",
        }
        events = (
            self._seed(version=0),
            _stored(1, "model.turn-completed.v1", {"run_id": str(self.run_id), **model_turn}, self.run_id, self.thread_id, self.turn_id),
        )
        projection = reduce_execution(events)
        self.assertEqual(projection.phase, RunPhase.READY_FOR_TOOL)
        self.assertEqual(len(projection.pending_tool_calls), 1)

    def test_resume_seed_context_overwrite_rejected(self):
        resume_item = _context_item(content="response")
        resume = {
            "turn_event_id": str(uuid4()),
            "turn_event_type": "turn.recovery-queued.v1",
            "context_item": resume_item,
            "content_digest": hashlib.sha256(canonical_json_bytes_v1(resume_item, path="resume-item")).hexdigest(),
        }
        first = self._seed(version=0)
        second = self._seed(
            version=1,
            run_id=uuid4(),
            resume=resume,
            context_docs=[dict(self.doc), resume_item],
        )
        projection = reduce_execution((first, second))
        self.assertEqual(projection.context[-1]["content"], "response")
        self.assertEqual(projection.last_run_id, second.payload["run_id"] and UUID(second.payload["run_id"]))
        # overwriting the first item is corrupt
        forged = self._seed(
            version=1,
            run_id=uuid4(),
            resume=resume,
            context_docs=[{"kind": "user", "input_id": "forged", "content": "replaced", "source_interrupt_id": None}],
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution((first, forged))

    def test_resume_seed_counter_reset_rejected(self):
        resume_item = _context_item(content="response")
        resume = {
            "turn_event_id": str(uuid4()),
            "turn_event_type": "turn.recovery-queued.v1",
            "context_item": resume_item,
            "content_digest": hashlib.sha256(canonical_json_bytes_v1(resume_item, path="resume-item")).hexdigest(),
        }
        first = self._seed(version=0)
        counters = {"model_round": 7, "tool_count": 3}
        second = self._seed(
            version=1,
            run_id=uuid4(),
            resume=resume,
            context_docs=[dict(self.doc), resume_item],
            counters=counters,
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution((first, second))

    def test_resume_seed_digest_mismatch_rejected(self):
        resume = {
            "turn_event_id": str(uuid4()),
            "turn_event_type": "turn.recovery-queued.v1",
            "context_item": _context_item(content="response"),
            "content_digest": "0" * 64,
        }
        events = (
            self._seed(version=0),
            self._seed(version=1, run_id=uuid4(), resume=resume, context_docs=[dict(self.doc), _context_item(content="response")]),
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution(events)

    def test_no_resume_seed_must_equal_previous_projection(self):
        first = self._seed(version=0)
        second = self._seed(version=1, run_id=uuid4(), context_docs=[dict(self.doc)])
        projection = reduce_execution((first, second))
        self.assertEqual(projection.last_run_id, UUID(second.payload["run_id"]))
        # altered context without a resume block is corrupt
        altered = self._seed(
            version=1,
            run_id=uuid4(),
            context_docs=[{"kind": "user", "input_id": "x", "content": "different", "source_interrupt_id": None}],
        )
        with self.assertRaises(ReconstructionError):
            reduce_execution((first, altered))

    def test_projection_document_round_trip(self):
        projection = reduce_execution((self._seed(),))
        document = projection_document(projection)
        self.assertEqual(set(document), {
            "context",
            "final_text",
            "input_tokens",
            "last_run_id",
            "model_round",
            "output_chars",
            "output_tokens",
            "pending_tool_calls",
            "phase",
            "tool_count",
        })
        self.assertEqual(len(projection_digest(projection)), 64)


if __name__ == "__main__":
    unittest.main()