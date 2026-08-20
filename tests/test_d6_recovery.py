from __future__ import annotations

import sqlite3
import json
import time
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.recovery import (
    AutomaticRecoveryBlocked,
    CheckpointError,
    CheckpointStore,
    DurableExecutionRecorder,
    LeaseConflict,
    LeaseKeeper,
    RecoveryCoordinator,
    RunPhase,
)
from koawa_agent_v2.control.event_store import EventStoreError, NewEvent, EventMetadata, StreamId, StreamPrecondition, StreamWrite, WrongExpectedVersion
from koawa_agent_v2.model.protocol import UserMessage
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.execution.loop import AgentLoop, ToolExecutionResult
from koawa_agent_v2.ledger import LedgerExecutor, READ_ONLY_PROFILE, ToolLedgerStore
from koawa_agent_v2.model.protocol import AssistantMessage, AssistantTextItem, FinishReason, ModelCallRef, ModelTurn, ToolCallEcho, ToolCallItem, ToolDefinition, ToolResultMessage
from koawa_agent_v2.execution.worker import TurnWorker
from tests.test_agent_loop import RecordingToolExecutor, ScriptedClient, _final_script
from datetime import datetime, timezone


class D6RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.db"
        self.store = SqliteEventStore(self.path)
        self.checkpoints = CheckpointStore(self.store)
        self.runtime = ThreadRuntime(self.store)
        thread = self.runtime.create_thread("repo")
        queued = self.runtime.create_turn(thread.thread_id, "fix it", expected_thread_version=thread.version)
        self.running = self.runtime.start_turn(queued.turn_id, queued.version)
        self.recorder = DurableExecutionRecorder(self.store, self.checkpoints, thread_id=thread.thread_id, turn_id=queued.turn_id, run_id=self.running.current_run_id, turn_version=self.running.version, initial_context=(UserMessage("u1", "fix it"),))

    def tearDown(self): self.tmp.cleanup()

    def test_kill_after_event_before_checkpoint_rebuilds_from_truth(self):
        # Simulate a missing projection: typed facts remain sufficient.
        with closing(sqlite3.connect(self.path)) as c: c.execute("DELETE FROM checkpoints"); c.commit()
        item = self.checkpoints.list_recoverable_turns()[0]
        rebuilt = RecoveryCoordinator(self.runtime, self.checkpoints, owner_id="new").reconstruct(item)
        self.assertEqual(rebuilt.context[0]["content"], "fix it")

    def test_start_and_bootstrap_recovery_index_share_one_commit(self):
        candidates = self.checkpoints.list_recoverable_turns()
        self.assertEqual(candidates[0].run_id, self.running.current_run_id)
        with closing(sqlite3.connect(self.path)) as c:
            lease = c.execute("SELECT owner_id,run_id FROM run_leases WHERE turn_id=?", (str(self.running.turn_id),)).fetchone()
        self.assertEqual(lease, ("__bootstrap__", str(self.running.current_run_id)))

    def test_late_d6_install_backfills_an_already_running_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteEventStore(Path(directory) / "late.db")
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(thread.thread_id, "task", expected_thread_version=thread.version)
            running = runtime.start_turn(queued.turn_id, queued.version)
            installed = CheckpointStore(store)
            self.assertEqual(installed.list_recoverable_turns()[0].run_id, running.current_run_id)

    def test_abandon_command_is_idempotent_and_terminal_cleans_projection(self):
        candidate = self.checkpoints.list_recoverable_turns()[0]
        command_id = uuid4()
        first = self.checkpoints.abandon_stale_run(candidate, force=True, command_id=command_id)
        self.assertEqual(self.checkpoints.abandon_stale_run(candidate, force=True, command_id=command_id), first)
        running = self.runtime.start_turn(candidate.turn_id, first)
        self.runtime.complete_turn(running.turn_id, "done", expected_version=running.version, run_id=running.current_run_id)
        self.assertEqual(self.checkpoints.list_recoverable_turns(), ())

    def test_valid_checkpoint_replays_committed_tail(self):
        with closing(sqlite3.connect(self.path)) as c:
            old = c.execute("SELECT execution_version,checkpoint_json FROM checkpoints").fetchone()
        self.recorder.tool_started("c1", "read_file")
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("UPDATE checkpoints SET execution_version=?,checkpoint_json=?", old); c.commit()
        rebuilt = RecoveryCoordinator(self.runtime, self.checkpoints, owner_id="new").reconstruct(self.checkpoints.list_recoverable_turns()[0])
        self.assertEqual(rebuilt.phase, RunPhase.TOOL_IN_PROGRESS)

    def test_background_heartbeat_prevents_false_stale_takeover(self):
        lease = self.checkpoints.acquire_lease(self.running.turn_id, self.running.current_run_id, "live", 1)
        keeper = LeaseKeeper(self.checkpoints, lease, 1); keeper.start()
        try:
            time.sleep(1.4)
            item = self.checkpoints.list_recoverable_turns()[0]
            with self.assertRaises(LeaseConflict): RecoveryCoordinator(self.runtime, self.checkpoints, owner_id="new").claim_stale(item)
            keeper.assert_owned()
        finally:
            keeper.stop()

    def test_truncated_unknown_and_hash_bad_checkpoint_fall_back(self):
        item = self.checkpoints.list_recoverable_turns()[0]
        with closing(sqlite3.connect(self.path)) as c: row = c.execute("SELECT checkpoint_json FROM checkpoints").fetchone()[0]
        document = json.loads(row); document["covered_event_hash"] = "0" * 64
        for bad in ('{', '{"schema_version":99}', json.dumps(document)):
            with closing(sqlite3.connect(self.path)) as c:
                c.execute("UPDATE checkpoints SET checkpoint_json=?", (bad,))
                c.commit()
            rebuilt = RecoveryCoordinator(self.runtime, self.checkpoints, owner_id="new").reconstruct(item)
            self.assertEqual(rebuilt.phase, RunPhase.READY_FOR_MODEL)

    def test_live_lease_rejects_takeover_and_expired_creates_new_run(self):
        lease = self.checkpoints.acquire_lease(self.running.turn_id, self.running.current_run_id, "old", 30)
        item = self.checkpoints.list_recoverable_turns()[0]
        with self.assertRaises(LeaseConflict): RecoveryCoordinator(self.runtime, self.checkpoints, owner_id="new").claim_stale(item)
        claim = RecoveryCoordinator(self.runtime, self.checkpoints, owner_id="new").claim_stale(item, force=True)
        self.assertIsNone(claim.lease)
        self.assertEqual(claim.turn.status, TurnStatus.QUEUED)
        with self.assertRaises(WrongExpectedVersion):
            self.runtime.complete_turn(self.running.turn_id, "late", expected_version=self.running.version, run_id=self.running.current_run_id)
        with self.assertRaises(LeaseConflict): self.checkpoints.heartbeat(lease, 30)

    def test_destroy_runtime_then_discover_and_finalize_without_model_replay(self):
        item = AssistantTextItem(0, "final-item", "durable final")
        turn = ModelTurn(uuid4(), "test", "model", "response", (item,), FinishReason.STOP)
        self.recorder.model_completed(turn, (AssistantMessage("test", turn.model_turn_id, item),), 1, len(item.text), False)
        # New objects emulate a new process. The final model turn must not run twice.
        store = SqliteEventStore(self.path); runtime = ThreadRuntime(store); checkpoints = CheckpointStore(store)
        candidate = RecoveryCoordinator(runtime, checkpoints, owner_id="new").list_recoverable_turns()[0]
        claim = RecoveryCoordinator(runtime, checkpoints, owner_id="new").claim_stale(candidate, force=True)
        client = ScriptedClient()
        worker = TurnWorker(runtime, AgentLoop(client), provider="test", model="model", checkpoint_store=checkpoints)
        result = worker.execute(claim.turn.turn_id, claim.turn.version)
        self.assertEqual(result.turn.status, TurnStatus.COMPLETED)
        self.assertEqual(result.turn.outcome, "durable final")
        self.assertEqual(client.requests, [])
        self.assertEqual(checkpoints.list_recoverable_turns(), ())

    def test_restart_executes_only_remaining_tool_then_continues_model(self):
        first = ToolCallItem(0, "i1", "c1", "read_file", '{"path":"a"}')
        second = ToolCallItem(1, "i2", "c2", "read_file", '{"path":"b"}')
        turn = ModelTurn(uuid4(), "test", "model", "tools", (first, second), FinishReason.TOOL_CALLS)
        echoes = (ToolCallEcho("test", ModelCallRef(turn.model_turn_id, "c1"), first), ToolCallEcho("test", ModelCallRef(turn.model_turn_id, "c2"), second))
        self.recorder.model_completed(turn, echoes, 1, 0, True)
        self.recorder.tool_started("c1", "read_file")
        self.recorder.tool_completed(ToolResultMessage(echoes[0].call_ref, "done-a"), 1)
        store = SqliteEventStore(self.path); runtime = ThreadRuntime(store); checkpoints = CheckpointStore(store)
        candidate = RecoveryCoordinator(runtime, checkpoints, owner_id="new").list_recoverable_turns()[0]
        claim = RecoveryCoordinator(runtime, checkpoints, owner_id="new").claim_stale(candidate, force=True)
        executor = RecordingToolExecutor(ToolExecutionResult("done-b"), definitions=(ToolDefinition("read_file", None, '{"type":"object","properties":{"path":{"type":"string"}}}'),))
        client = ScriptedClient(_final_script("finished", "r2"))
        ledger_executor = LedgerExecutor(
            executor,
            ToolLedgerStore(store),
            {"read_file": READ_ONLY_PROFILE},
        )
        result = TurnWorker(runtime, AgentLoop(client, tool_executor=ledger_executor), provider="test", model="model", checkpoint_store=checkpoints).execute(claim.turn.turn_id, claim.turn.version)
        self.assertEqual([call.call_id for call, _ in executor.calls], ["c2"])
        self.assertEqual(result.turn.outcome, "finished")

    def test_tool_in_progress_blocks_automatic_replay(self):
        self.recorder.tool_started("c1", "apply_patch")
        item = self.checkpoints.list_recoverable_turns()[0]
        self.assertFalse(item.automatic)
        with self.assertRaises(AutomaticRecoveryBlocked): RecoveryCoordinator(self.runtime, self.checkpoints, owner_id="new").claim_stale(item, force=True)

    def test_execution_append_is_fenced_after_turn_changes(self):
        self.runtime.cancel_turn(self.running.turn_id, "stop", expected_version=self.running.version)
        with self.assertRaises(WrongExpectedVersion): self.recorder.tool_started("c1", "read_file")

    def test_stale_checkpoint_cannot_republish_terminal_turn(self):
        checkpoint = self.checkpoints.load(self.running.turn_id)
        self.runtime.cancel_turn(
            self.running.turn_id,
            "stop",
            expected_version=self.running.version,
        )
        with self.assertRaises(CheckpointError):
            self.checkpoints.save(checkpoint)
        self.assertEqual(self.checkpoints.list_recoverable_turns(), ())

    def test_worker_start_seed_and_live_lease_are_one_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteEventStore(Path(directory) / "atomic.db")
            checkpoints = CheckpointStore(store)
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(
                thread.thread_id,
                "atomic task",
                expected_thread_version=thread.version,
            )
            client = ScriptedClient(KeyboardInterrupt())
            worker = TurnWorker(
                runtime,
                AgentLoop(client),
                provider="test",
                model="model",
                checkpoint_store=checkpoints,
                owner_id="atomic-worker",
                lease_seconds=10,
            )
            with self.assertRaises(KeyboardInterrupt):
                worker.execute(queued.turn_id, queued.version)

            running = runtime.get_turn(queued.turn_id)
            turn_started = store.read_stream(StreamId("turn", queued.turn_id))[-1]
            seeded = store.read_stream(StreamId("run-execution", queued.turn_id))[-1]
            self.assertEqual(turn_started.event_type, "turn.started.v1")
            self.assertEqual(seeded.event_type, "run.context-seeded.v1")
            self.assertEqual(turn_started.commit_id, seeded.commit_id)
            checkpoints.get_active_lease(
                running.turn_id,
                running.current_run_id,
                "atomic-worker",
            )
            candidate = checkpoints.list_recoverable_turns()[0]
            rebuilt = RecoveryCoordinator(
                runtime,
                checkpoints,
                owner_id="recovery",
            ).reconstruct(candidate)
            self.assertEqual(rebuilt.context[0]["content"], "atomic task")

    def test_legacy_start_without_seed_recovers_before_first_model_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteEventStore(Path(directory) / "legacy-start.db")
            checkpoints = CheckpointStore(store)
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(
                thread.thread_id,
                "recover original",
                expected_thread_version=thread.version,
            )
            runtime.start_turn(queued.turn_id, queued.version)

            coordinator = RecoveryCoordinator(
                runtime,
                checkpoints,
                owner_id="recovery",
            )
            candidate = coordinator.list_recoverable_turns()[0]
            rebuilt = coordinator.reconstruct(candidate)
            self.assertEqual(rebuilt.execution_version, -1)
            self.assertEqual(rebuilt.context[0]["content"], "recover original")
            claim = coordinator.claim_stale(candidate, force=True)

            client = ScriptedClient(_final_script("done", "legacy-final"))
            result = TurnWorker(
                runtime,
                AgentLoop(client),
                provider="test",
                model="model",
                checkpoint_store=checkpoints,
            ).execute(claim.turn.turn_id, claim.turn.version)
            self.assertEqual(result.turn.status, TurnStatus.COMPLETED)
            self.assertEqual(client.requests[0].input_items[0].content, "recover original")

    def test_claim_can_repeat_after_requeue_before_worker_start(self):
        coordinator = RecoveryCoordinator(
            self.runtime,
            self.checkpoints,
            owner_id="new",
        )
        first = coordinator.claim_stale(
            self.checkpoints.list_recoverable_turns()[0],
            force=True,
        )
        self.assertEqual(first.turn.status, TurnStatus.QUEUED)
        queued_candidate = self.checkpoints.list_recoverable_turns()[0]
        second = coordinator.claim_stale(queued_candidate, force=True)
        self.assertEqual(second.turn.status, TurnStatus.QUEUED)
        self.assertEqual(second.turn.version, first.turn.version)

    def test_resume_input_survives_worker_crash_exactly_once(self):
        waiting = self.runtime.wait_for_input(
            self.running.turn_id,
            "Which branch?",
            expected_version=self.running.version,
            run_id=self.running.current_run_id,
        )
        self.assertEqual(self.checkpoints.list_recoverable_turns(), ())
        queued = self.runtime.request_resume(
            waiting.turn_id,
            waiting.version,
            interrupt_id=waiting.pending_interrupt.interrupt_id,
            response="Use the release branch",
        )

        crashed_client = ScriptedClient(KeyboardInterrupt())
        crashed_worker = TurnWorker(
            self.runtime,
            AgentLoop(crashed_client),
            provider="test",
            model="model",
            checkpoint_store=self.checkpoints,
            owner_id="resume-worker-1",
        )
        with self.assertRaises(KeyboardInterrupt):
            crashed_worker.execute(queued.turn_id, queued.version)
        first_responses = [
            item
            for item in crashed_client.requests[0].input_items
            if isinstance(item, UserMessage) and ":resume:" in item.input_id
        ]
        self.assertEqual([item.content for item in first_responses], ["Use the release branch"])

        candidate = self.checkpoints.list_recoverable_turns()[0]
        claim = RecoveryCoordinator(
            self.runtime,
            self.checkpoints,
            owner_id="recovery",
        ).claim_stale(candidate, force=True)
        resumed_client = ScriptedClient(_final_script("done", "resume-final"))
        result = TurnWorker(
            self.runtime,
            AgentLoop(resumed_client),
            provider="test",
            model="model",
            checkpoint_store=self.checkpoints,
            owner_id="resume-worker-2",
        ).execute(claim.turn.turn_id, claim.turn.version)
        second_responses = [
            item
            for item in resumed_client.requests[0].input_items
            if isinstance(item, UserMessage) and ":resume:" in item.input_id
        ]
        self.assertEqual([item.content for item in second_responses], ["Use the release branch"])
        self.assertEqual(result.turn.status, TurnStatus.COMPLETED)

    def test_denied_approval_is_visible_to_the_model(self):
        waiting = self.runtime.wait_for_approval(
            self.running.turn_id,
            "Apply changes?",
            expected_version=self.running.version,
            run_id=self.running.current_run_id,
        )
        queued = self.runtime.request_resume(
            waiting.turn_id,
            waiting.version,
            interrupt_id=waiting.pending_interrupt.interrupt_id,
            response=False,
        )
        client = ScriptedClient(_final_script("not applied", "denied"))
        TurnWorker(
            self.runtime,
            AgentLoop(client),
            provider="test",
            model="model",
            checkpoint_store=self.checkpoints,
        ).execute(queued.turn_id, queued.version)
        responses = [
            item.content
            for item in client.requests[0].input_items
            if isinstance(item, UserMessage) and ":resume:" in item.input_id
        ]
        self.assertEqual(responses, ["Approval response: denied."])

    def test_persisted_execution_and_checkpoint_redact_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "redaction.db"
            store = SqliteEventStore(database)
            checkpoints = CheckpointStore(store)
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(
                thread.thread_id,
                "redact task",
                expected_thread_version=thread.version,
            )
            running = runtime.start_turn(queued.turn_id, queued.version)
            secret = "sk-test-PLAINTEXT"
            recorder = DurableExecutionRecorder(
                store,
                checkpoints,
                thread_id=thread.thread_id,
                turn_id=running.turn_id,
                run_id=running.current_run_id,
                turn_version=running.version,
                initial_context=(UserMessage("secret-input", f"token={secret}"),),
            )
            assistant = AssistantTextItem(0, "a1", f"Bearer {secret}")
            tool = ToolCallItem(
                1,
                "t1",
                "c1",
                "read_file",
                json.dumps({"api_key": secret, "path": "a.txt"}),
            )
            turn = ModelTurn(
                uuid4(),
                "test",
                "model",
                "redact-response",
                (assistant, tool),
                FinishReason.TOOL_CALLS,
            )
            echo = ToolCallEcho(
                "test",
                ModelCallRef(turn.model_turn_id, "c1"),
                tool,
            )
            recorder.model_completed(
                turn,
                (AssistantMessage("test", turn.model_turn_id, assistant), echo),
                1,
                len(assistant.text),
                True,
            )
            recorder.tool_completed(
                ToolResultMessage(echo.call_ref, f"password:{secret}"),
                1,
            )

            with closing(sqlite3.connect(database)) as connection:
                event_documents = "\n".join(
                    row[0]
                    for row in connection.execute(
                        "SELECT payload_json FROM events WHERE stream_id=? ORDER BY stream_version",
                        (StreamId("run-execution", running.turn_id).key,),
                    )
                )
                checkpoint_document = connection.execute(
                    "SELECT checkpoint_json FROM checkpoints WHERE turn_id=?",
                    (str(running.turn_id),),
                ).fetchone()[0]
            self.assertNotIn(secret, event_documents)
            self.assertNotIn(secret, checkpoint_document)
            self.assertIn("[REDACTED]", event_documents)


if __name__ == "__main__": unittest.main()
