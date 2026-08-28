from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.models import (
    InvalidTransition,
    RunStatus,
    TurnStatus,
)
from koawa_agent_v2.control.run_effects import RunEffectIndex
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.runtime.truth import RuntimeTruthVerifier
from koawa_agent_v2.workspace.effects import (
    WorkspaceEffectKind,
    WorkspaceEffectResultKind,
    WorkspaceEffectStore,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


class I7RunEffectIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.events = SqliteEventStore(Path(temporary.name) / "run-effects.sqlite3")
        self.runtime = ThreadRuntime(self.events)
        self.effects = WorkspaceEffectStore(self.events)
        thread = self.runtime.create_thread("D:/repo")
        self.thread_id = thread.thread_id
        turn = self.runtime.create_turn(
            thread.thread_id,
            "change the repository",
            expected_thread_version=thread.version,
        )
        self.turn = self.runtime.start_turn(turn.turn_id, expected_version=turn.version)
        assert self.turn.current_run_id is not None
        self.run_id = self.turn.current_run_id

    def intend(self):
        return self.effects.intend(
            semantic_command_id=uuid4(),
            kind=WorkspaceEffectKind.ARTIFACT_APPLY,
            repository_identity_digest=SHA_A,
            agent_id=None,
            run_id=self.run_id,
            resource_ref="integration/result",
            base_digest=SHA_B,
            input_digest=SHA_C,
            precondition_digest=SHA_D,
            expected_postcondition_digest=SHA_E,
        )

    def terminal_effect(self):
        intended = self.intend().record
        claimed = self.effects.claim(
            intended.effect_id,
            expected_version=intended.version,
            owner_id="worker",
        ).record
        return self.effects.record_applied(
            claimed.effect_id,
            expected_version=claimed.version,
            claim_epoch=claimed.claim_epoch,
            claim_token=claimed.claim_token,
            result_kind=WorkspaceEffectResultKind.SUCCESS,
            result_code="applied",
            exit_code=0,
            postcondition_digest=SHA_E,
            evidence_digest=SHA_A,
        ).record

    def test_intent_and_run_link_are_one_atomic_commit(self) -> None:
        written = self.intend().record
        effect_event = self.events.read_stream(written_stream(written.effect_id))[0]
        index_event = self.events.read_stream(RunEffectIndex.stream(self.run_id))[0]
        self.assertEqual(effect_event.commit_id, index_event.commit_id)
        self.assertEqual(
            {0, 1}, {effect_event.commit_index, index_event.commit_index}
        )
        self.assertEqual((2, 2), (effect_event.commit_size, index_event.commit_size))

    def test_open_effect_atomically_pauses_turn_and_closes_run_unknown(self) -> None:
        self.intend()
        paused = self.runtime.complete_turn(
            self.turn.turn_id,
            "must not be accepted",
            expected_version=self.turn.version,
            run_id=self.run_id,
        )
        self.assertEqual(TurnStatus.PAUSED, paused.status)
        self.assertEqual(RunStatus.OUTCOME_UNKNOWN, self.runtime.get_run(self.run_id).status)
        self.assertEqual(
            paused.turn_id,
            self.runtime.get_thread(self.thread_id).active_turn_id,
        )
        truth = RuntimeTruthVerifier(self.runtime, self.events).read(paused.turn_id)
        self.assertTrue(truth.workspace_uncertain)
        self.assertEqual("runtime_outcome_unknown", truth.outcome_code)
        with self.assertRaises(InvalidTransition):
            self.runtime.request_resume(paused.turn_id, paused.version)
        with self.assertRaises(InvalidTransition):
            self.runtime.resolve_runtime_outcome(
                paused.turn_id,
                expected_version=paused.version,
                run_id=self.run_id,
                evidence_kind="effect_reconciliation",
                evidence_digest=SHA_A,
                reconciler="recovery",
            )

    def test_typed_resolution_requires_terminal_effect_and_starts_fresh_run(self) -> None:
        intended = self.intend().record
        paused = self.runtime.complete_turn(
            self.turn.turn_id,
            "blocked",
            expected_version=self.turn.version,
            run_id=self.run_id,
        )
        claimed = self.effects.claim(
            intended.effect_id,
            expected_version=intended.version,
            owner_id="reconciler",
        ).record
        self.effects.record_applied(
            claimed.effect_id,
            expected_version=claimed.version,
            claim_epoch=claimed.claim_epoch,
            claim_token=claimed.claim_token,
            result_kind=WorkspaceEffectResultKind.SUCCESS,
            result_code="reconciled",
            exit_code=0,
            postcondition_digest=SHA_E,
            evidence_digest=SHA_A,
        )
        queued = self.runtime.resolve_runtime_outcome(
            paused.turn_id,
            expected_version=paused.version,
            run_id=self.run_id,
            evidence_kind="effect_reconciliation",
            evidence_digest=SHA_A,
            reconciler="recovery",
        )
        self.assertEqual(TurnStatus.QUEUED, queued.status)
        self.assertIsNone(queued.current_run_id)
        fresh = self.runtime.start_turn(queued.turn_id, expected_version=queued.version)
        self.assertNotEqual(self.run_id, fresh.current_run_id)

    def test_terminal_effect_allows_evidence_bound_completion(self) -> None:
        self.terminal_effect()
        evidence = self.runtime.record_completion_evidence(
            self.turn.turn_id,
            run_id=self.run_id,
            final_text="done",
        )
        completed = self.runtime.complete_turn(
            self.turn.turn_id,
            "done",
            expected_version=self.turn.version,
            run_id=self.run_id,
            evidence_ref=evidence,
        )
        self.assertEqual(TurnStatus.COMPLETED, completed.status)
        truth = RuntimeTruthVerifier(self.runtime, self.events).read(completed.turn_id)
        self.assertFalse(truth.workspace_uncertain)
        self.assertEqual("turn_completed", truth.outcome_code)


def written_stream(effect_id):
    from koawa_agent_v2.control.event_store import StreamId

    return StreamId("workspace-effect", effect_id)


if __name__ == "__main__":
    unittest.main()
