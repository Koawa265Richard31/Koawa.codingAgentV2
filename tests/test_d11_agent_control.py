from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from koawa_agent_v2.agents.control import (
    AgentBudgetLimits,
    AgentControlPlane,
    Principal,
)
from koawa_agent_v2.agents.graph import AgentError, AgentState, ContextMode
from koawa_agent_v2.agents.messages import (
    MESSAGE_RESULT_MAX_INPUT_UTF8_BYTES,
    MESSAGE_RESULT_SUMMARY_MAX_UTF8_BYTES,
    TRUNCATION_MARKER,
    MessageKind,
    MessageStatus,
    canonicalize_result,
    summarize_outcome,
)
from koawa_agent_v2.control.event_store import (
    EventMetadata,
    NewEvent,
    StreamId,
    StreamWrite,
)
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
        recorded = self.control.record_message_result(
            child.agent_id,
            first.message_id,
            run_id=running.run_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            outcome="ok",
        )
        self.assertEqual(MessageStatus.RESULT_RECORDED, recorded.status)
        self.assertEqual(delivered.delivery_attempt, recorded.delivery_attempt)
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


    # ------------------------------------------------------------------
    # I2 mailbox-head CAS / result / resolution contract tests
    # ------------------------------------------------------------------

    def _operator(self) -> Principal:
        return Principal("child-principal", ("agents.resolve",))

    def _legacy_message(self, child, run_id, *, message_id=None):
        """Append message.enqueued.v1 + message.delivered.v1 (old wire)."""
        message_id = message_id or uuid4()
        occurred = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        command = uuid4()
        enqueued = NewEvent(
            uuid5(command, "event:enqueue"),
            "message.enqueued.v1",
            1,
            occurred,
            {
                "agent_id": str(child.agent_id),
                "message_id": str(message_id),
                "from_agent_id": None,
                "sequence": 0,
                "kind": MessageKind.TASK.value,
                "body_ref": "legacy-task",
                "idempotency_key": "legacy-1",
                "status": "queued",
            },
            EventMetadata(command, uuid5(command, "correlation"), actor="test"),
        )
        delivered = NewEvent(
            uuid5(command, "event:delivered"),
            "message.delivered.v1",
            1,
            occurred,
            {
                "agent_id": str(child.agent_id),
                "message_id": str(message_id),
                "run_id": str(run_id),
            },
            EventMetadata(command, uuid5(command, "correlation"), actor="test"),
        )
        self.store.append_batch(
            (
                StreamWrite(
                    StreamId("mailbox", child.agent_id),
                    -1,
                    (enqueued, delivered),
                ),
            ),
            idempotency_key=command,
        )
        return self.control.mailbox.load(child.agent_id)[0]

    def test_deliver_uses_mailbox_head_version(self) -> None:
        """P0-01: delivering an older message uses the current mailbox head."""
        child = self._spawn(task="head")
        m1 = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="m1",
            idempotency_key="head-1",
        )
        self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="m2",
            idempotency_key="head-2",
        )
        self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="m3",
            idempotency_key="head-3",
        )
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=30
        )
        delivered = self.control.deliver_message(
            child.agent_id, m1.message_id, run_id=running.run_id
        )
        self.assertEqual(MessageStatus.DELIVERED, delivered.status)
        self.assertEqual(1, delivered.delivery_attempt)
        remaining = self.control.mailbox.load(child.agent_id)
        self.assertEqual(
            [MessageStatus.DELIVERED, MessageStatus.QUEUED, MessageStatus.QUEUED],
            [item.status for item in remaining],
        )

    def test_redelivery_increments_attempt_and_preserves_sequence(self) -> None:
        """requeue keeps the sequence; the next delivery increments attempt."""
        child = self._spawn(task="redeliver")
        message = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="redeliver",
            idempotency_key="redeliver-1",
        )
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=30
        )
        first = self.control.deliver_message(
            child.agent_id, message.message_id, run_id=running.run_id
        )
        self.assertEqual(1, first.delivery_attempt)
        unresolved = self.control.mark_message_unresolved(
            child.agent_id,
            message.message_id,
            abandoned_run_id=running.run_id,
            takeover_run_id=running.run_id,
            expected_delivery_attempt=1,
            reason="test_unresolved",
        )
        self.assertEqual(MessageStatus.UNRESOLVED, unresolved.status)
        requeued = self.control.requeue_message(
            child.agent_id,
            message.message_id,
            expected_delivery_attempt=1,
            decision_id=uuid4(),
            actor=self._operator(),
            approval_id=None,
            resolution_kind="proven_not_started",
            reason="test_requeue",
        )
        self.assertEqual(MessageStatus.QUEUED, requeued.status)
        self.assertEqual(0, requeued.sequence)
        self.assertEqual(1, requeued.delivery_attempt)
        second = self.control.deliver_message(
            child.agent_id, message.message_id, run_id=running.run_id
        )
        self.assertEqual(2, second.delivery_attempt)
        self.assertEqual(0, second.sequence)

    def test_cancelled_message_cannot_be_redelivered(self) -> None:
        child = self._spawn(task="cancel-redeliver")
        message = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="x",
            idempotency_key="cancel-redeliver-1",
        )
        cancelled = self.control.cancel_message(
            child.agent_id,
            message.message_id,
            expected_delivery_attempt=0,
            decision_id=uuid4(),
            actor=self._operator(),
            approval_id=None,
            reason="test_cancel",
        )
        self.assertEqual(MessageStatus.CANCELLED, cancelled.status)
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=30
        )
        with self.assertRaises(AgentError) as raised:
            self.control.deliver_message(
                child.agent_id,
                message.message_id,
                run_id=running.run_id,
            )
        self.assertEqual("message_transition_invalid", raised.exception.code)

    def test_stale_message_delivery_attempt_is_rejected(self) -> None:
        child = self._spawn(task="stale")
        message = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="stale",
            idempotency_key="stale-1",
        )
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=30
        )
        self.control.deliver_message(
            child.agent_id, message.message_id, run_id=running.run_id
        )
        with self.assertRaises(AgentError) as raised:
            self.control.record_message_result(
                child.agent_id,
                message.message_id,
                run_id=running.run_id,
                expected_delivery_attempt=7,
                outcome="late",
            )
        self.assertEqual("stale_message_delivery_attempt", raised.exception.code)

    def test_result_encoding_boundaries(self) -> None:
        exact = "a" * MESSAGE_RESULT_MAX_INPUT_UTF8_BYTES
        canonical, is_error, code = canonicalize_result(exact, None)
        self.assertFalse(is_error)
        self.assertEqual(canonical, exact)
        with self.assertRaises(AgentError) as raised:
            canonicalize_result(exact + "b", None)
        self.assertEqual("message_result_too_large", raised.exception.code)

        long_outcome = "x" * (MESSAGE_RESULT_SUMMARY_MAX_UTF8_BYTES + 200)
        summary = summarize_outcome(long_outcome)
        self.assertLessEqual(
            len(summary.encode("utf-8")), MESSAGE_RESULT_SUMMARY_MAX_UTF8_BYTES
        )
        self.assertTrue(summary.endswith(TRUNCATION_MARKER))

        smashed = "é" * 3000
        smashed_summary = summarize_outcome(smashed)
        smashed_summary.encode("utf-8").decode("utf-8")
        self.assertTrue(smashed_summary.endswith(TRUNCATION_MARKER))

        with self.assertRaises(AgentError) as raised:
            canonicalize_result("ok", "Bad Code")
        self.assertEqual("message_result_invalid_text", raised.exception.code)

    def test_result_identity_conflict(self) -> None:
        child = self._spawn(task="identity")
        message = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="identity",
            idempotency_key="identity-1",
        )
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=30
        )
        delivered = self.control.deliver_message(
            child.agent_id, message.message_id, run_id=running.run_id
        )
        recorded = self.control.record_message_result(
            child.agent_id,
            message.message_id,
            run_id=running.run_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            outcome="first",
        )
        self.assertEqual(MessageStatus.RESULT_RECORDED, recorded.status)
        with self.assertRaises(AgentError) as raised:
            self.control.record_message_result(
                child.agent_id,
                message.message_id,
                run_id=running.run_id,
                expected_delivery_attempt=delivered.delivery_attempt,
                outcome="second",
            )
        self.assertEqual("message_result_identity_conflict", raised.exception.code)

    def test_requeue_requires_matching_operator_authority(self) -> None:
        child = self._spawn(task="auth")
        message = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="auth",
            idempotency_key="auth-1",
        )
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=30
        )
        self.control.deliver_message(
            child.agent_id, message.message_id, run_id=running.run_id
        )
        self.control.mark_message_unresolved(
            child.agent_id,
            message.message_id,
            abandoned_run_id=running.run_id,
            takeover_run_id=running.run_id,
            expected_delivery_attempt=1,
            reason="test",
        )
        with self.assertRaises(AgentError) as raised:
            self.control.requeue_message(
                child.agent_id,
                message.message_id,
                expected_delivery_attempt=1,
                decision_id=uuid4(),
                actor=Principal("intruder", ("agents.resolve",)),
                approval_id=None,
                resolution_kind="proven_not_started",
                reason="test",
            )
        self.assertEqual("message_requeue_not_authorized", raised.exception.code)
        with self.assertRaises(AgentError) as raised:
            self.control.cancel_message(
                child.agent_id,
                message.message_id,
                expected_delivery_attempt=1,
                decision_id=uuid4(),
                actor=Principal("child-principal", ("read",)),
                approval_id=None,
                reason="test",
            )
        self.assertEqual("message_resolution_not_authorized", raised.exception.code)

    def test_legacy_delivered_requires_resolution(self) -> None:
        child = self._spawn(task="legacy")
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=10
        )
        legacy = self._legacy_message(child, running.run_id)
        self.assertEqual(MessageStatus.DELIVERED, legacy.status)
        self.assertTrue(legacy.legacy_delivery)
        with self.assertRaises(AgentError) as raised:
            self.control.ack_message(
                child.agent_id, legacy.message_id, run_id=running.run_id
            )
        self.assertEqual("legacy_delivery_requires_resolution", raised.exception.code)
        with self.assertRaises(AgentError) as raised:
            self.control.record_message_result(
                child.agent_id,
                legacy.message_id,
                run_id=running.run_id,
                expected_delivery_attempt=1,
                outcome="late",
            )
        self.assertEqual("legacy_delivery_requires_resolution", raised.exception.code)

        self.clock.value += timedelta(seconds=11)
        self.control.discover_orphans()
        orphan = self.control.graph.load(child.agent_id)
        self.assertEqual(AgentState.ORPHANED, orphan.state)
        second = self.control.start_attempt(
            child.agent_id, expected_version=orphan.version, lease_seconds=10
        )
        self.assertEqual(2, second.attempt)
        post_takeover = self.control.mailbox.load(child.agent_id)
        self.assertEqual(MessageStatus.UNRESOLVED, post_takeover[0].status)

    def test_takeover_writes_agent_and_unresolved_in_one_commit(self) -> None:
        child = self._spawn(task="atomic")
        m1 = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="m1",
            idempotency_key="atomic-1",
        )
        m2 = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="m2",
            idempotency_key="atomic-2",
        )
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=10
        )
        self.control.deliver_message(
            child.agent_id, m1.message_id, run_id=running.run_id
        )
        self.control.deliver_message(
            child.agent_id, m2.message_id, run_id=running.run_id
        )
        self.clock.value += timedelta(seconds=11)
        self.control.discover_orphans()
        orphan = self.control.graph.load(child.agent_id)
        second = self.control.start_attempt(
            child.agent_id, expected_version=orphan.version, lease_seconds=10
        )
        self.assertEqual(2, second.attempt)
        mailbox_events = self.store.read_stream(
            StreamId("mailbox", child.agent_id)
        )
        unresolved_events = [
            event for event in mailbox_events
            if event.event_type == "message.unresolved.v1"
        ]
        taken_over_events = [
            event for event in self.store.read_stream(
                StreamId("agent", child.agent_id)
            )
            if event.event_type == "agent.taken-over.v2"
        ]
        self.assertEqual(2, len(unresolved_events))
        self.assertEqual(1, len(taken_over_events))
        commits = {event.commit_id for event in unresolved_events}
        commits.add(taken_over_events[0].commit_id)
        self.assertEqual(1, len(commits))

    def test_waiting_resume_flow_and_resolution_mixed(self) -> None:
        child = self._spawn(task="waiting")
        m1 = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="m1",
            idempotency_key="waiting-1",
        )
        m2 = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="m2",
            idempotency_key="waiting-2",
        )
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=10
        )
        self.control.deliver_message(
            child.agent_id, m1.message_id, run_id=running.run_id
        )
        self.control.deliver_message(
            child.agent_id, m2.message_id, run_id=running.run_id
        )
        self.clock.value += timedelta(seconds=11)
        self.control.discover_orphans()
        orphan = self.control.graph.load(child.agent_id)
        second = self.control.start_attempt(
            child.agent_id, expected_version=orphan.version, lease_seconds=10
        )
        waiting = self.control.enter_waiting_for_resolution(
            child.agent_id, run_id=second.run_id, attempt=second.attempt
        )
        self.assertEqual(AgentState.WAITING, waiting.state)
        self.assertEqual(
            {m1.message_id, m2.message_id},
            set(waiting.blocking_message_ids),
        )

        self.assertEqual(
            {m1.message_id, m2.message_id},
            set(waiting.blocking_message_ids),
        )

    def test_waiting_mixed_resolution_is_refused(self) -> None:
        child = self._spawn(task="waiting-mixed")
        m1 = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="m1",
            idempotency_key="waiting-mixed-1",
        )
        m2 = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="m2",
            idempotency_key="waiting-mixed-2",
        )
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=10
        )
        self.control.deliver_message(
            child.agent_id, m1.message_id, run_id=running.run_id
        )
        self.control.deliver_message(
            child.agent_id, m2.message_id, run_id=running.run_id
        )
        self.clock.value += timedelta(seconds=11)
        self.control.discover_orphans()
        orphan = self.control.graph.load(child.agent_id)
        second = self.control.start_attempt(
            child.agent_id, expected_version=orphan.version, lease_seconds=10
        )
        waiting = self.control.enter_waiting_for_resolution(
            child.agent_id, run_id=second.run_id, attempt=second.attempt
        )
        self.control.requeue_message(
            child.agent_id,
            m1.message_id,
            expected_delivery_attempt=1,
            decision_id=uuid4(),
            actor=self._operator(),
            approval_id=None,
            resolution_kind="proven_not_started",
            reason="test",
        )
        self.control.cancel_message(
            child.agent_id,
            m2.message_id,
            expected_delivery_attempt=1,
            decision_id=uuid4(),
            actor=self._operator(),
            approval_id=None,
            reason="test",
        )
        with self.assertRaises(AgentError) as raised:
            self.control.start_attempt(
                child.agent_id,
                expected_version=waiting.version,
                lease_seconds=10,
            )
        self.assertEqual("message_resolution_mixed", raised.exception.code)

    def test_waiting_all_requeued_resumes_with_fresh_run(self) -> None:
        child = self._spawn(task="waiting-resume")
        m1 = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="m1",
            idempotency_key="waiting-resume-1",
        )
        m2 = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="m2",
            idempotency_key="waiting-resume-2",
        )
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=10
        )
        self.control.deliver_message(
            child.agent_id, m1.message_id, run_id=running.run_id
        )
        self.control.deliver_message(
            child.agent_id, m2.message_id, run_id=running.run_id
        )
        self.clock.value += timedelta(seconds=11)
        self.control.discover_orphans()
        orphan = self.control.graph.load(child.agent_id)
        second = self.control.start_attempt(
            child.agent_id, expected_version=orphan.version, lease_seconds=10
        )
        waiting = self.control.enter_waiting_for_resolution(
            child.agent_id, run_id=second.run_id, attempt=second.attempt
        )
        self.assertEqual(AgentState.WAITING, waiting.state)
        self.control.requeue_message(
            child.agent_id,
            m1.message_id,
            expected_delivery_attempt=1,
            decision_id=uuid4(),
            actor=self._operator(),
            approval_id=None,
            resolution_kind="proven_not_started",
            reason="test",
        )
        self.control.requeue_message(
            child.agent_id,
            m2.message_id,
            expected_delivery_attempt=1,
            decision_id=uuid4(),
            actor=self._operator(),
            approval_id=None,
            resolution_kind="proven_not_started",
            reason="test",
        )
        current = self.control.graph.load(child.agent_id)
        resumed = self.control.start_attempt(
            child.agent_id,
            expected_version=current.version,
            lease_seconds=10,
        )
        self.assertEqual(AgentState.RUNNING, resumed.state)
        self.assertEqual(3, resumed.attempt)
        self.assertNotEqual(second.run_id, resumed.run_id)

    def test_cancel_request_and_late_result_preserve_real_digest(self) -> None:
        child = self._spawn(task="cancel-request")
        message = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="work",
            idempotency_key="cancel-request-1",
        )
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=30
        )
        delivered = self.control.deliver_message(
            child.agent_id, message.message_id, run_id=running.run_id
        )
        requested = self.control.cancel_message(
            child.agent_id,
            message.message_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            decision_id=uuid4(),
            actor=self._operator(),
            approval_id=None,
            reason="test",
        )
        self.assertEqual(MessageStatus.DELIVERED, requested.status)
        self.assertTrue(requested.cancel_requested)
        recorded = self.control.record_message_result(
            child.agent_id,
            message.message_id,
            run_id=running.run_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            outcome="real-provider-fact",
        )
        self.assertEqual(MessageStatus.RESULT_RECORDED, recorded.status)
        self.assertTrue(recorded.cancel_requested)
        self.assertIsNotNone(recorded.result_digest)
        acked = self.control.ack_message(
            child.agent_id, message.message_id, run_id=running.run_id
        )
        self.assertEqual(MessageStatus.ACKED, acked.status)
        self.assertTrue(acked.cancel_requested)
        self.assertEqual(recorded.result_ref, acked.result_ref)
        self.assertEqual(recorded.result_digest, acked.result_digest)

    def test_ack_retry_returns_same_receipt_without_extra_events(self) -> None:
        child = self._spawn(task="ack-retry")
        message = self.control.send_message(
            child.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="m",
            idempotency_key="ack-retry-1",
        )
        running = self.control.start_attempt(
            child.agent_id, expected_version=child.version, lease_seconds=30
        )
        delivered = self.control.deliver_message(
            child.agent_id, message.message_id, run_id=running.run_id
        )
        self.control.record_message_result(
            child.agent_id,
            message.message_id,
            run_id=running.run_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            outcome="ok",
        )
        first_ack = self.control.ack_message(
            child.agent_id, message.message_id, run_id=running.run_id
        )
        before = self.control.mailbox.snapshot(child.agent_id).stream_version
        second_ack = self.control.ack_message(
            child.agent_id, message.message_id, run_id=running.run_id
        )
        after = self.control.mailbox.snapshot(child.agent_id).stream_version
        self.assertEqual(first_ack.message_id, second_ack.message_id)
        self.assertEqual(MessageStatus.ACKED, second_ack.status)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

