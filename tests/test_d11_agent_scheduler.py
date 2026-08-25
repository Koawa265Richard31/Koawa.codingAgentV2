from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.agents.graph import AgentState, ContextMode
from koawa_agent_v2.agents.messages import MessageKind, MessageStatus
from koawa_agent_v2.agents.scheduler import (
    AgentScheduler,
    ScriptedAgentProvider,
)
from koawa_agent_v2.control.sqlite_store import SqliteEventStore


class MutableClock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


class D11AgentSchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "d11-scheduler.sqlite3"
        self.store = SqliteEventStore(self.database)
        self.clock = MutableClock(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc))
        self.control = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(
                max_depth=3,
                max_total_agents=8,
                max_concurrent_children=4,
            ),
            clock=self.clock,
        )
        self.root = self.control.spawn_agent(
            parent_agent_id=None,
            task_id="root",
            principal_id="root",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )

    def _spawn(self, task: str):
        return self.control.spawn_agent(
            parent_agent_id=self.root.agent_id,
            task_id=task,
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )

    def _scheduler(self, script: dict[str, str]) -> AgentScheduler:
        return AgentScheduler(
            self.control,
            provider=ScriptedAgentProvider(script),
            lease_seconds=30,
        )

    def test_two_parallel_readonly_workers_converge(self) -> None:
        provider = ScriptedAgentProvider(
            {"locate": "tool:read_file", "review": "tool:list_files"}
        )
        scheduler = AgentScheduler(self.control, provider=provider, lease_seconds=30)
        locator = self._spawn("locate")
        reviewer = self._spawn("review")
        self.control.send_message(
            locator.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="locate",
            idempotency_key="locate-1",
        )
        self.control.send_message(
            reviewer.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="review",
            idempotency_key="review-1",
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(
                pool.map(
                    scheduler.run_attempt,
                    (locator.agent_id, reviewer.agent_id),
                )
            )
        self.assertEqual(
            [AgentState.COMPLETED, AgentState.COMPLETED],
            [item.state for item in results],
        )
        self.assertEqual(["list_files", "read_file"], sorted(provider.tools_seen))
        self.assertEqual(
            AgentState.COMPLETED,
            self.control.graph.load(locator.agent_id).state,
        )

    def test_one_failure_and_parent_converges(self) -> None:
        scheduler = self._scheduler({"ok": "tool:search_text", "bad": "raise:worker_failed"})
        good = self._spawn("ok")
        bad = self._spawn("bad")
        self.control.send_message(
            good.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="ok",
            idempotency_key="ok-1",
        )
        self.control.send_message(
            bad.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="bad",
            idempotency_key="bad-1",
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            tuple(pool.map(scheduler.run_attempt, (good.agent_id, bad.agent_id)))
        summary = self.control.wait_agents(self.root.agent_id, timeout_seconds=1)
        states = {item["agent_id"]: item["state"] for item in summary}
        self.assertEqual(AgentState.COMPLETED.value, states[str(good.agent_id)])
        self.assertEqual(AgentState.FAILED.value, states[str(bad.agent_id)])
        self.assertEqual("worker_failed", self.control.graph.load(bad.agent_id).reason)

    def test_cancel_message_cancels_agent(self) -> None:
        scheduler = self._scheduler({"work": "ok"})
        worker = self._spawn("work")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.CANCEL,
            body_ref="cancel",
            idempotency_key="cancel-1",
        )
        result = scheduler.run_attempt(worker.agent_id)
        self.assertEqual(AgentState.CANCELLED, result.state)
        self.assertEqual(
            AgentState.CANCELLED,
            self.control.graph.load(worker.agent_id).state,
        )

    def test_write_tool_is_forbidden(self) -> None:
        scheduler = self._scheduler({"write": "tool:apply_patch"})
        worker = self._spawn("write")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="write",
            idempotency_key="write-1",
        )
        result = scheduler.run_attempt(worker.agent_id)
        self.assertEqual(AgentState.FAILED, result.state)
        self.assertEqual(
            "d11_write_forbidden",
            self.control.graph.load(worker.agent_id).reason,
        )

    def test_restart_recovers_orphan_and_old_result_is_fenced(self) -> None:
        provider = ScriptedAgentProvider({"task": "ok"})
        scheduler = AgentScheduler(self.control, provider=provider, lease_seconds=30)
        worker = self._spawn("task")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="task",
            idempotency_key="task-1",
        )
        first = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=5
        )
        self.clock.value += timedelta(seconds=6)
        self.control.discover_orphans()
        orphan = self.control.graph.load(worker.agent_id)
        self.assertEqual(AgentState.ORPHANED, orphan.state)

        # "Process restart": a fresh control plane over the same SQLite file.
        fresh_store = SqliteEventStore(self.database)
        fresh_control = AgentControlPlane(
            fresh_store,
            limits=AgentBudgetLimits(),
            clock=self.clock,
        )
        fresh_scheduler = AgentScheduler(
            fresh_control,
            provider=ScriptedAgentProvider({"task": "ok"}),
            lease_seconds=30,
        )
        result = fresh_scheduler.run_attempt(worker.agent_id)
        self.assertEqual(AgentState.COMPLETED, result.state)
        final = fresh_control.graph.load(worker.agent_id)
        self.assertEqual(2, final.attempt)
        with self.assertRaises(Exception):
            self.control.terminal(
                worker.agent_id,
                run_id=first.run_id,
                expected_attempt=1,
                state=AgentState.COMPLETED,
                reason=None,
                result_ref="agent-run-result:x",
                result_digest="0" * 64,
                source_message_ids=(),
            )


    # ------------------------------------------------------------------
    # I2 scheduler flow tests (result/ACK recovery, WAITING, cancel)
    # ------------------------------------------------------------------

    def _orphan(self, worker, run_id) -> None:
        self.clock.value += timedelta(seconds=6)
        self.control.discover_orphans()
        orphan = self.control.graph.load(worker.agent_id)
        self.assertEqual(AgentState.ORPHANED, orphan.state)

    def _fresh(self, script=None):
        fresh_store = SqliteEventStore(self.database)
        fresh_control = AgentControlPlane(
            fresh_store,
            limits=AgentBudgetLimits(),
            clock=self.clock,
        )
        return fresh_control, AgentScheduler(
            fresh_control,
            provider=ScriptedAgentProvider(script or {}),
            lease_seconds=30,
        )

    def test_result_recorded_recovery_acks_without_provider_recall(self) -> None:
        """result committed before ACK: restart only ACKs, provider stays 1."""
        scheduler = self._scheduler({"task": "ok"})
        worker = self._spawn("task")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="task",
            idempotency_key="result-recovery-1",
        )
        first = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=5
        )
        delivered = self.control.deliver_message(
            worker.agent_id,
            self.control.mailbox.load(worker.agent_id)[0].message_id,
            run_id=first.run_id,
        )
        self.control.record_message_result(
            worker.agent_id,
            delivered.message_id,
            run_id=first.run_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            outcome="persisted",
        )
        self._orphan(worker, first.run_id)

        fresh_control, fresh_scheduler = self._fresh()
        result = fresh_scheduler.run_attempt(worker.agent_id)
        self.assertEqual(AgentState.COMPLETED, result.state)
        messages = fresh_control.mailbox.load(worker.agent_id)
        self.assertEqual(MessageStatus.ACKED, messages[0].status)
        self.assertEqual("persisted", messages[0].result_summary)
        # provider of the recovered run must never be invoked again
        self.assertEqual([], fresh_scheduler._provider.calls)

    def test_delivered_unacked_goes_waiting_on_recovery(self) -> None:
        """P0-02: killed between deliver and result: never auto-complete."""
        scheduler = self._scheduler({"task": "ok"})
        worker = self._spawn("task")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="task",
            idempotency_key="unacked-1",
        )
        first = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=5
        )
        message = self.control.mailbox.load(worker.agent_id)[0]
        self.control.deliver_message(
            worker.agent_id, message.message_id, run_id=first.run_id
        )
        self._orphan(worker, first.run_id)

        fresh_control, fresh_scheduler = self._fresh()
        result = fresh_scheduler.run_attempt(worker.agent_id)
        self.assertEqual(AgentState.WAITING, result.state)
        agent = fresh_control.graph.load(worker.agent_id)
        self.assertEqual(AgentState.WAITING, agent.state)
        messages = fresh_control.mailbox.load(worker.agent_id)
        self.assertEqual(MessageStatus.UNRESOLVED, messages[0].status)
        self.assertEqual([], fresh_scheduler._provider.calls)

    def test_waiting_requeued_resumes_and_completes_work(self) -> None:
        scheduler = self._scheduler({"task": "ok"})
        worker = self._spawn("task")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="task",
            idempotency_key="resume-1",
        )
        first = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=5
        )
        message = self.control.mailbox.load(worker.agent_id)[0]
        self.control.deliver_message(
            worker.agent_id, message.message_id, run_id=first.run_id
        )
        self._orphan(worker, first.run_id)
        fresh_control, fresh_scheduler = self._fresh({"task": "ok"})
        result = fresh_scheduler.run_attempt(worker.agent_id)
        self.assertEqual(AgentState.WAITING, result.state)
        waiting = fresh_control.graph.load(worker.agent_id)
        mid = waiting.blocking_message_ids[0]
        from koawa_agent_v2.agents.control import Principal
        fresh_control.requeue_message(
            worker.agent_id,
            mid,
            expected_delivery_attempt=1,
            decision_id=uuid4(),
            actor=Principal("worker", ("agents.resolve",)),
            approval_id=None,
            resolution_kind="proven_not_started",
            reason="test",
        )
        result = fresh_scheduler.run_attempt(worker.agent_id)
        self.assertEqual(AgentState.COMPLETED, result.state)
        self.assertEqual(1, len(fresh_scheduler._provider.calls))
        final = fresh_control.graph.load(worker.agent_id)
        self.assertEqual(3, final.attempt)

    def test_waiting_all_cancelled_terminates_cancelled_without_provider(self) -> None:
        scheduler = self._scheduler({"task": "ok"})
        worker = self._spawn("task")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="task",
            idempotency_key="all-cancel-1",
        )
        first = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=5
        )
        message = self.control.mailbox.load(worker.agent_id)[0]
        self.control.deliver_message(
            worker.agent_id, message.message_id, run_id=first.run_id
        )
        self._orphan(worker, first.run_id)
        fresh_control, fresh_scheduler = self._fresh({"task": "ok"})
        result = fresh_scheduler.run_attempt(worker.agent_id)
        self.assertEqual(AgentState.WAITING, result.state)
        waiting = fresh_control.graph.load(worker.agent_id)
        mid = waiting.blocking_message_ids[0]
        from koawa_agent_v2.agents.control import Principal
        fresh_control.cancel_message(
            worker.agent_id,
            mid,
            expected_delivery_attempt=1,
            decision_id=uuid4(),
            actor=Principal("worker", ("agents.resolve",)),
            approval_id=None,
            reason="test",
        )
        result = fresh_scheduler.run_attempt(worker.agent_id)
        self.assertEqual(AgentState.CANCELLED, result.state)
        self.assertEqual([], fresh_scheduler._provider.calls)
        self.assertEqual(
            AgentState.CANCELLED,
            fresh_control.graph.load(worker.agent_id).state,
        )

    def test_cancel_requested_late_result_terminates_cancelled_with_real_digest(self) -> None:
        from koawa_agent_v2.agents.control import Principal
        scheduler = self._scheduler({"task": "ok"})
        worker = self._spawn("task")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="task",
            idempotency_key="cancel-late-1",
        )
        first = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=5
        )
        message = self.control.mailbox.load(worker.agent_id)[0]
        delivered = self.control.deliver_message(
            worker.agent_id, message.message_id, run_id=first.run_id
        )
        self.control.cancel_message(
            worker.agent_id,
            message.message_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            decision_id=uuid4(),
            actor=Principal("worker", ("agents.resolve",)),
            approval_id=None,
            reason="test",
        )
        recorded = self.control.record_message_result(
            worker.agent_id,
            message.message_id,
            run_id=first.run_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            outcome="real-outcome",
        )
        self.assertTrue(recorded.cancel_requested)
        self._orphan(worker, first.run_id)

        fresh_control, fresh_scheduler = self._fresh()
        result = fresh_scheduler.run_attempt(worker.agent_id)
        self.assertEqual(AgentState.CANCELLED, result.state)
        self.assertEqual([], fresh_scheduler._provider.calls)
        messages = fresh_control.mailbox.load(worker.agent_id)
        self.assertEqual(MessageStatus.ACKED, messages[0].status)
        self.assertEqual(recorded.result_digest, messages[0].result_digest)

    def test_two_messages_process_in_sequence_with_real_results(self) -> None:
        scheduler = self._scheduler({"a": "ok", "b": "ok"})
        worker = self._spawn("seq")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="a",
            idempotency_key="seq-1",
        )
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="b",
            idempotency_key="seq-2",
        )
        result = scheduler.run_attempt(worker.agent_id)
        self.assertEqual(AgentState.COMPLETED, result.state)
        self.assertEqual(["a", "b"], scheduler._provider.calls)
        messages = self.control.mailbox.load(worker.agent_id)
        self.assertEqual(
            [MessageStatus.ACKED, MessageStatus.ACKED],
            [item.status for item in messages],
        )
        self.assertEqual(0, messages[0].sequence)
        self.assertEqual(1, messages[1].sequence)
        self.assertEqual(1, self.control.graph.load(worker.agent_id).attempt)
        self.assertFalse(
            str(self.control.graph.load(worker.agent_id).outcome).startswith("completed")
        )


if __name__ == "__main__":
    unittest.main()

