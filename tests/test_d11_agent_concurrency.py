"""D11 I2 concurrency tests: takeover races, orphan detection with a live
keeper, late-result fencing. Barriers and manual wait strategies replace any
timing sleeps; the fake clock drives lease arithmetic deterministically.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.agents.graph import AgentError, AgentState, ContextMode
from koawa_agent_v2.agents.messages import MessageKind, MessageStatus
from koawa_agent_v2.agents.scheduler import (
    AgentScheduler,
    ScriptedAgentProvider,
    WaitStrategy,
)
from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.control.sqlite_store import SqliteEventStore


class MutableClock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


class ManualWaitStrategy:
    """Deterministic keeper wait: one heartbeat per release() call."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._releases = 0
        self._stop = False

    def request_stop(self) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify_all()

    def release(self) -> None:
        with self._condition:
            self._releases += 1
            self._condition.notify_all()

    def wait(self, timeout: float) -> bool:
        with self._condition:
            while not self._stop and self._releases == 0:
                self._condition.wait(timeout)
            if self._stop:
                return True
            self._releases -= 1
            return False


class GateProvider:
    """Provider that blocks until released, recording every call."""

    def __init__(self, outcome: str) -> None:
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.calls: list[str] = []
        self.outcome = outcome

    def run(self, task: str, *, tool_allowlist: frozenset[str]) -> str:
        self.calls.append(task)
        self.entered.set()
        self.gate.wait(10)
        return self.outcome


def _orphan_agent(control, agent_id) -> None:
    control.discover_orphans()
    return control.graph.load(agent_id)


class D11AgentConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "d11-concurrency.sqlite3"
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

    def _deliver_two(self, worker):
        first = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=10
        )
        messages = self.control.mailbox.load(worker.agent_id)
        for message in messages:
            self.control.deliver_message(
                worker.agent_id, message.message_id, run_id=first.run_id
            )
        return first

    def test_takeover_marks_unresolved_in_sequence_order_single_commit(self) -> None:
        worker = self._spawn("takeover")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="a",
            idempotency_key="t-1",
        )
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="b",
            idempotency_key="t-2",
        )
        first = self._deliver_two(worker)
        delivered_before = [
            item for item in self.control.mailbox.load(worker.agent_id)
            if item.status is MessageStatus.DELIVERED
        ]
        self.assertEqual(2, len(delivered_before))
        expected_order = [str(item.message_id) for item in delivered_before]
        self.clock.value += timedelta(seconds=11)
        _orphan_agent(self.control, worker.agent_id)
        second = self.control.start_attempt(
            worker.agent_id,
            expected_version=self.control.graph.load(worker.agent_id).version,
            lease_seconds=10,
        )
        self.assertEqual(2, second.attempt)
        events = self.store.read_stream(StreamId("mailbox", worker.agent_id))
        unresolved = [
            event for event in events
            if event.event_type == "message.unresolved.v1"
        ]
        self.assertEqual(2, len(unresolved))
        commit_ids = {event.commit_id for event in unresolved}
        self.assertEqual(1, len(commit_ids))
        payload_ids = [event.payload["message_id"] for event in unresolved]
        self.assertEqual(expected_order, payload_ids)
        messages = self.control.mailbox.load(worker.agent_id)
        self.assertEqual(
            [MessageStatus.UNRESOLVED, MessageStatus.UNRESOLVED],
            [item.status for item in messages],
        )

    def test_terminal_wins_race_and_deliver_appends_zero_events(self) -> None:
        """Barrier race: deliver's stale precondition loses, no events."""
        from koawa_agent_v2.agents.messages import MessageRecord

        worker = self._spawn("race")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="a",
            idempotency_key="race-1",
        )
        running = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=10
        )
        message = self.control.mailbox.load(worker.agent_id)[0]
        self.assertEqual(MessageStatus.QUEUED, message.status)

        at_fault = threading.Event()
        gate = threading.Event()
        deliver_outcome: dict = {}

        def fault(point: str, facts) -> None:
            if point == "d11.deliver.before_append":
                at_fault.set()
                gate.wait(5)

        racing_control = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(),
            clock=self.clock,
            faults=fault,
        )

        def deliverer() -> None:
            try:
                racing_control.deliver_message(
                    worker.agent_id,
                    message.message_id,
                    run_id=running.run_id,
                )
                deliver_outcome["ok"] = True
            except AgentError as error:
                deliver_outcome["code"] = error.code
            except Exception as error:  # pragma: no cover - unexpected
                deliver_outcome["exc"] = type(error).__name__

        thread = threading.Thread(target=deliverer)
        thread.start()
        self.assertTrue(at_fault.wait(10))
        self.control.terminal(
            worker.agent_id,
            run_id=running.run_id,
            state=AgentState.COMPLETED,
            reason="done",
            outcome="done",
        )
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual("stale_agent_run_fenced", deliver_outcome.get("code"))
        # the losing deliver appended zero events: mailbox still has 1 event
        final = self.control.mailbox.load(worker.agent_id)
        self.assertEqual(MessageStatus.QUEUED, final[0].status)
        mailbox_events = self.store.read_stream(StreamId("mailbox", worker.agent_id))
        self.assertEqual(1, len(mailbox_events))
        self.assertEqual(
            AgentState.COMPLETED,
            self.control.graph.load(worker.agent_id).state,
        )

    def test_concurrent_takeover_has_one_winner_and_one_consistent_view(self) -> None:
        worker = self._spawn("race-takeover")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="a",
            idempotency_key="race-takeover-1",
        )
        first = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=10
        )
        self.control.deliver_message(
            worker.agent_id,
            self.control.mailbox.load(worker.agent_id)[0].message_id,
            run_id=first.run_id,
        )
        self.clock.value += timedelta(seconds=11)
        _orphan_agent(self.control, worker.agent_id)
        recorder = {
            "taken_over": [],
            "conflicts": [],
            "errors": [],
        }

        def takeover(index: int) -> None:
            store = SqliteEventStore(self.database)
            control = AgentControlPlane(
                store, limits=AgentBudgetLimits(), clock=self.clock
            )
            record = control.graph.load(worker.agent_id)
            try:
                got = control.start_attempt(
                    worker.agent_id,
                    expected_version=record.version,
                    lease_seconds=10,
                )
                recorder["taken_over"].append((index, got.run_id, got.attempt))
            except AgentError as error:
                if error.code == "agent_takeover_conflict":
                    recorder["conflicts"].append(index)
                else:
                    recorder["errors"].append((index, error.code))

        with ThreadPoolExecutor(max_workers=2) as pool:
            tuple(pool.map(takeover, (0, 1)))
        final = self.control.graph.load(worker.agent_id)
        self.assertEqual(AgentState.RUNNING, final.state)
        self.assertEqual(2, final.attempt)
        self.assertEqual(0, len(recorder["errors"]))
        # idempotent takeover: both callers observe the same run, and exactly
        # one batch was actually committed (loser wrote zero events).
        run_ids = {item[1] for item in recorder["taken_over"]}
        self.assertEqual(1, len(run_ids))
        taken_over = [
            event
            for event in self.store.read_stream(StreamId("agent", worker.agent_id))
            if event.event_type == "agent.taken-over.v2"
        ]
        self.assertEqual(1, len(taken_over))
        unresolved = [
            event for event in self.store.read_stream(StreamId("mailbox", worker.agent_id))
            if event.event_type == "message.unresolved.v1"
        ]
        self.assertEqual(1, len(unresolved))

    def test_slow_live_provider_is_not_orphaned_during_two_heartbeats(self) -> None:
        """P0-04: heartbeats keep a slow provider's run alive; no redelivery."""
        worker = self._spawn("slow")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="slow",
            idempotency_key="slow-1",
        )
        provider = GateProvider("done")
        manual = ManualWaitStrategy()
        scheduler = AgentScheduler(
            self.control,
            provider=provider,
            lease_seconds=30,
            keeper_wait_strategy=manual,
        )
        outcomes = []

        def run() -> None:
            try:
                outcomes.append(scheduler.run_attempt(worker.agent_id))
            except Exception as error:  # pragma: no cover - unexpected
                outcomes.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(provider.entered.wait(10))

        # two keeper heartbeats while the provider is still blocked
        manual.release()
        self.clock.value += timedelta(seconds=10)
        manual.release()
        self.clock.value += timedelta(seconds=20)

        orphans = self.control.discover_orphans()
        self.assertEqual([], orphans)
        self.assertEqual(AgentState.RUNNING, self.control.graph.load(worker.agent_id).state)

        provider.gate.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["slow"], provider.calls)
        result = outcomes[0]
        self.assertEqual(AgentState.COMPLETED, result.state)
        final = self.control.graph.load(worker.agent_id)
        self.assertEqual(AgentState.COMPLETED, final.state)
        messages = self.control.mailbox.load(worker.agent_id)
        self.assertEqual(MessageStatus.ACKED, messages[0].status)

    def test_late_result_from_abandoned_run_is_fenced(self) -> None:
        worker = self._spawn("late")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="late",
            idempotency_key="late-1",
        )
        message = self.control.mailbox.load(worker.agent_id)[0]
        first = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=10
        )
        delivered = self.control.deliver_message(
            worker.agent_id, message.message_id, run_id=first.run_id
        )
        self.clock.value += timedelta(seconds=11)
        _orphan_agent(self.control, worker.agent_id)
        orphan = self.control.graph.load(worker.agent_id)
        second = self.control.start_attempt(
            worker.agent_id, expected_version=orphan.version, lease_seconds=10
        )
        self.assertNotEqual(first.run_id, second.run_id)

        with self.assertRaises(AgentError) as raised:
            self.control.record_message_result(
                worker.agent_id,
                message.message_id,
                run_id=first.run_id,
                expected_delivery_attempt=delivered.delivery_attempt,
                outcome="late",
            )
        self.assertEqual("stale_agent_run_fenced", raised.exception.code)
        with self.assertRaises(AgentError) as raised:
            self.control.ack_message(
                worker.agent_id, message.message_id, run_id=first.run_id
            )
        self.assertEqual("stale_agent_run_fenced", raised.exception.code)
        # takeover already turned the message UNRESOLVED; no result got written
        messages = self.control.mailbox.load(worker.agent_id)
        self.assertEqual(MessageStatus.UNRESOLVED, messages[0].status)
        self.assertIsNone(messages[0].result_digest)

    def test_no_keep_alive_threads_left_after_runs(self) -> None:
        """keeper threads are joined and daemon=False threads are all stopped."""
        worker = self._spawn("threads")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="x",
            idempotency_key="threads-1",
        )
        scheduler = AgentScheduler(
            self.control, provider=ScriptedAgentProvider({"x": "ok"})
        )
        scheduler.run_attempt(worker.agent_id)
        alive = [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("koawa-agent-lease-")
        ]
        self.assertEqual([], alive)


if __name__ == "__main__":
    unittest.main()
