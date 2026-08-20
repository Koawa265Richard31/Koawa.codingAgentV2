from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.agents.graph import AgentError, AgentState, ContextMode
from koawa_agent_v2.agents.messages import MessageKind, MessageStatus
from koawa_agent_v2.control.sqlite_store import SqliteEventStore


class MutableClock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


class D11AgentControlTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "d11.sqlite3"
        self.store = SqliteEventStore(self.database)
        self.clock = MutableClock(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc))
        self.control = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(
                max_depth=3,
                max_total_agents=4,
                max_concurrent_children=2,
            ),
            clock=self.clock,
        )
        self.root = self.control.spawn_agent(
            parent_agent_id=None,
            task_id="root-task",
            principal_id="root",
            scopes=("read", "write"),
            context_mode=ContextMode.FRESH,
        )

    def _spawn(self, parent=None, task="child"):
        return self.control.spawn_agent(
            parent_agent_id=parent or self.root.agent_id,
            task_id=task,
            principal_id="child-principal",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )

    def test_spawn_creates_durable_graph_and_budget(self) -> None:
        child = self._spawn(task="locate")
        self.assertEqual(AgentState.CREATED, child.state)
        self.assertEqual(self.root.agent_id, child.parent_agent_id)
        reloaded = AgentControlPlane(
            SqliteEventStore(self.database),
            limits=AgentBudgetLimits(),
        )
        self.assertEqual(
            child.agent_id,
            reloaded.graph.load(child.agent_id).agent_id,
        )
        self.assertEqual(1, self.control._budget(self.root.agent_id))

    def test_depth_concurrency_and_total_limits(self) -> None:
        child_a = self._spawn(task="a")
        child_b = self._spawn(task="b")
        with self.assertRaises(AgentError) as raised:
            self._spawn(task="c")
        self.assertEqual("agent_concurrency_exceeded", raised.exception.code)

        grandchild = self.control.spawn_agent(
            parent_agent_id=child_a.agent_id,
            task_id="gc",
            principal_id="child-principal",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        self.assertEqual(AgentState.CREATED, grandchild.state)
        with self.assertRaises(AgentError) as raised:
            self.control.spawn_agent(
                parent_agent_id=grandchild.agent_id,
                task_id="ggc",
                principal_id="child-principal",
                scopes=("read",),
                context_mode=ContextMode.FRESH,
            )
        self.assertEqual("agent_depth_exceeded", raised.exception.code)

        child_a_running = self.control.start_attempt(
            child_a.agent_id, expected_version=child_a.version
        )
        self.control.terminal(
            child_a.agent_id,
            run_id=child_a_running.run_id,
            state=AgentState.COMPLETED,
            reason="done",
        )
        # Terminal with a wrong run_id is fenced and must not release budget.
        with self.assertRaises(AgentError):
            self.control.terminal(
                child_a.agent_id,
                run_id=uuid4(),
                state=AgentState.COMPLETED,
                reason="late",
            )
        self.assertEqual(2, self.control._budget(self.root.agent_id))

    def test_cycle_spawn_is_rejected(self) -> None:
        child = self._spawn(task="cycle")
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version
        )
        self.assertTrue(
            self.control.graph.has_cycle(child.agent_id, self.root.agent_id)
        )
        self.assertFalse(
            self.control.graph.has_cycle(self.root.agent_id, child.agent_id)
        )
        self.assertIsNotNone(running.run_id)

    def test_message_dedupe_and_terminal_target(self) -> None:
        child = self._spawn(task="msg")
        first = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="do-x",
            idempotency_key="key-1",
        )
        duplicate = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="do-x",
            idempotency_key="key-1",
        )
        self.assertEqual(first.message_id, duplicate.message_id)
        self.assertEqual(1, len(self.control.mailbox.load(child.agent_id)))
        self.assertEqual(MessageStatus.QUEUED, first.status)

        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version
        )
        delivered = self.control.deliver_message(
            child.agent_id, first.message_id, run_id=running.run_id
        )
        self.assertEqual(MessageStatus.DELIVERED, delivered.status)
        acked = self.control.ack_message(
            child.agent_id, first.message_id, run_id=running.run_id
        )
        self.assertEqual(MessageStatus.ACKED, acked.status)

        done = self.control.terminal(
            child.agent_id,
            run_id=running.run_id,
            state=AgentState.COMPLETED,
            reason="done",
            outcome="ok",
        )
        self.assertEqual(AgentState.COMPLETED, done.state)
        with self.assertRaises(AgentError) as raised:
            self.control.send_message(
                child.agent_id,
                from_agent_id=self.root.agent_id,
                kind=MessageKind.FOLLOWUP,
                body_ref="late",
                idempotency_key="key-2",
            )
        self.assertEqual("message_target_terminal", raised.exception.code)

    def test_stale_run_is_fenced_and_orphan_takeover(self) -> None:
        child = self._spawn(task="orphan")
        first_run = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=10
        )
        self.clock.value += timedelta(seconds=11)
        orphans = self.control.discover_orphans()
        self.assertEqual(1, len(orphans))
        self.assertEqual(AgentState.ORPHANED, orphans[0].state)

        with self.assertRaises(AgentError) as raised:
            self.control.terminal(
                child.agent_id,
                run_id=first_run.run_id,
                state=AgentState.COMPLETED,
                reason="late",
            )
        self.assertEqual("stale_agent_run_fenced", raised.exception.code)

        orphan = self.control.graph.load(child.agent_id)
        second_run = self.control.start_attempt(
            child.agent_id,
            expected_version=orphan.version,
            lease_seconds=10,
        )
        self.assertNotEqual(first_run.run_id, second_run.run_id)
        self.assertEqual(2, second_run.attempt)
        done = self.control.terminal(
            child.agent_id,
            run_id=second_run.run_id,
            state=AgentState.COMPLETED,
            reason="done",
            outcome="recovered",
        )
        self.assertEqual(AgentState.COMPLETED, done.state)

    def test_interrupt_enqueues_cancel(self) -> None:
        child = self._spawn(task="cancel")
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version
        )
        message = self.control.interrupt_agent(
            child.agent_id, run_id=running.run_id
        )
        self.assertEqual(MessageKind.CANCEL, message.kind)
        self.assertEqual(MessageStatus.QUEUED, message.status)

    def test_list_and_wait_agents(self) -> None:
        self._spawn(task="w1")
        self._spawn(task="w2")
        listed = self.control.list_agents(self.root.agent_id)
        self.assertEqual(2, len(listed))
        waiting = self.control.wait_agents(self.root.agent_id, timeout_seconds=0.1)
        self.assertEqual(2, len(waiting))


if __name__ == "__main__":
    unittest.main()
