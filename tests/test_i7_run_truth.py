from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.event_store import EventMetadata, NewEvent, StreamId, StreamWrite
from koawa_agent_v2.control.models import (
    CompletionEvidenceRef,
    InvalidTransition,
    LegacyRunState,
    RunStatus,
    TurnStatus,
)
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.runtime.truth import RuntimeTruthVerifier


class I7RunTruthTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="koawa-i7-run-")
        self.addCleanup(temporary.cleanup)
        self.store = SqliteEventStore(Path(temporary.name) / "events.sqlite3")
        self.runtime = ThreadRuntime(self.store, actor="i7-test")

    def queued(self):
        thread = self.runtime.create_thread("workspace")
        turn = self.runtime.create_turn(
            thread.thread_id,
            "do the work",
            expected_thread_version=thread.version,
        )
        return thread, turn

    def test_start_creates_explicit_run_in_the_same_commit(self) -> None:
        _, queued = self.queued()
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        run = self.runtime.get_run(running.current_run_id)

        self.assertNotIsInstance(run, LegacyRunState)
        self.assertEqual(RunStatus.RUNNING, run.status)
        self.assertEqual(running.turn_id, run.turn_id)
        self.assertEqual(running.thread_id, run.thread_id)
        self.assertEqual(running.attempt, run.attempt)

        turn_started = self.store.read_stream(StreamId("turn", running.turn_id))[-1]
        run_started = self.store.read_stream(StreamId("run", run.run_id))[-1]
        self.assertEqual(turn_started.commit_id, run_started.commit_id)
        self.assertEqual(2, turn_started.commit_size)

    def test_completion_is_bound_to_exact_evidence_and_rebuilds_truth(self) -> None:
        _, queued = self.queued()
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        evidence = self.runtime.record_completion_evidence(
            running.turn_id, run_id=running.current_run_id, final_text="done"
        )
        forged = CompletionEvidenceRef(
            evidence.stream_id, evidence.stream_version + 1,
            evidence.event_id, evidence.evidence_digest,
        )
        with self.assertRaises(Exception):
            self.runtime.complete_turn(
                running.turn_id, "done", expected_version=running.version,
                run_id=running.current_run_id, evidence_ref=forged,
            )
        self.runtime.complete_turn(
            running.turn_id, "done", expected_version=running.version,
            run_id=running.current_run_id, evidence_ref=evidence,
        )
        truth = RuntimeTruthVerifier(self.runtime, self.store).read(running.turn_id)
        self.assertEqual(RunStatus.COMPLETED, truth.run.status)
        self.assertEqual(evidence, truth.completion_evidence)

    def test_wait_and_pause_close_the_active_run_as_interrupted(self) -> None:
        _, queued = self.queued()
        first = self.runtime.start_turn(queued.turn_id, queued.version)
        waiting = self.runtime.wait_for_input(
            first.turn_id,
            "question",
            expected_version=first.version,
            run_id=first.current_run_id,
        )
        first_run = self.runtime.get_run(first.current_run_id)
        self.assertEqual(RunStatus.INTERRUPTED, first_run.status)
        self.assertEqual(
            self.store.read_stream(StreamId("turn", first.turn_id))[-1].commit_id,
            self.store.read_stream(StreamId("run", first.current_run_id))[-1].commit_id,
        )

        queued_again = self.runtime.request_resume(
            waiting.turn_id,
            waiting.version,
            interrupt_id=waiting.pending_interrupt.interrupt_id,
            response="answer",
        )
        second = self.runtime.start_turn(queued_again.turn_id, queued_again.version)
        paused = self.runtime.pause_turn(
            second.turn_id,
            second.version,
            "pause",
            run_id=second.current_run_id,
        )
        self.assertEqual(TurnStatus.PAUSED, paused.status)
        self.assertEqual(
            RunStatus.INTERRUPTED,
            self.runtime.get_run(second.current_run_id).status,
        )

    def test_stale_requeue_abandons_run_and_terminal_closes_fresh_run(self) -> None:
        thread, queued = self.queued()
        first = self.runtime.start_turn(queued.turn_id, queued.version)
        requeued = self.runtime.requeue_stale_run(
            first.turn_id,
            expected_version=first.version,
            abandoned_run_id=first.current_run_id,
        )
        self.assertEqual(RunStatus.ABANDONED, self.runtime.get_run(first.current_run_id).status)

        second = self.runtime.start_turn(requeued.turn_id, requeued.version)
        completed = self.runtime.complete_turn(
            second.turn_id,
            "done",
            expected_version=second.version,
            run_id=second.current_run_id,
        )
        run = self.runtime.get_run(second.current_run_id)
        detached = self.runtime.get_thread(thread.thread_id)
        self.assertEqual(TurnStatus.COMPLETED, completed.status)
        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertIsNone(detached.active_turn_id)

        terminal_events = [
            event
            for event in self.store.read_all()
            if event.commit_id
            == self.store.read_stream(StreamId("run", run.run_id))[-1].commit_id
        ]
        self.assertEqual(
            {"turn.completed.v1", "thread.turn-detached.v1", "run.completed.v1"},
            {event.event_type for event in terminal_events},
        )
        self.assertTrue(all(event.commit_size == 3 for event in terminal_events))

    def test_queued_cancel_does_not_invent_a_run(self) -> None:
        _, queued = self.queued()
        cancelled = self.runtime.cancel_turn(
            queued.turn_id,
            "cancel before start",
            expected_version=queued.version,
        )
        self.assertEqual(TurnStatus.CANCELLED, cancelled.status)
        run_events = [event for event in self.store.read_all() if event.stream_id.category == "run"]
        self.assertEqual([], run_events)

    def test_operator_timeout_closes_a_running_run(self) -> None:
        _, queued = self.queued()
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        timed_out = self.runtime.timeout_turn(
            running.turn_id,
            "deadline",
            expected_version=running.version,
        )
        self.assertEqual(TurnStatus.TIMED_OUT, timed_out.status)
        self.assertEqual(
            RunStatus.TIMED_OUT,
            self.runtime.get_run(running.current_run_id).status,
        )

    def test_pre_i7_active_run_is_read_only_and_cannot_accept_new_facts(self) -> None:
        thread, queued = self.queued()
        run_id = uuid4()
        command_id = uuid4()
        started = NewEvent(
            uuid4(),
            "turn.started.v1",
            1,
            datetime.now(timezone.utc),
            {"run_id": str(run_id), "attempt": 1},
            EventMetadata(
                command_id,
                command_id,
                thread.thread_id,
                queued.turn_id,
                run_id,
                "legacy",
            ),
        )
        self.store.append_batch(
            (StreamWrite(StreamId("turn", queued.turn_id), queued.version, (started,)),),
            idempotency_key=command_id,
        )

        legacy = self.runtime.get_run(run_id)
        self.assertIsInstance(legacy, LegacyRunState)
        self.assertEqual(RunStatus.RUNNING, legacy.status)
        with self.assertRaisesRegex(
            InvalidTransition, "legacy_active_run_restart_required"
        ):
            self.runtime.wait_for_input(
                queued.turn_id,
                "question",
                expected_version=queued.version + 1,
                run_id=run_id,
            )


if __name__ == "__main__":
    unittest.main()
