"""D11 I2 concurrency tests: takeover races, orphan detection with a live
keeper, late-result fencing. Barriers and manual wait strategies replace any
timing sleeps; the fake clock drives lease arithmetic deterministically.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from koawa_agent_v2.agents.control import (
    AgentBudgetLimits,
    AgentControlPlane,
    Principal,
    terminal_result_identity,
)
from koawa_agent_v2.agents.graph import AgentError, AgentState, ContextMode
from koawa_agent_v2.agents.messages import MessageKind, MessageStatus
from koawa_agent_v2.agents.scheduler import (
    AgentScheduler,
    ScriptedAgentProvider,
    WaitStrategy,
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
        from koawa_agent_v2.agents.control import Principal
        from koawa_agent_v2.agents.control import terminal_result_identity

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
        # The only terminal legal while the message is undelivered is the
        # cancelled-before-dispatch path: cancel first, then CANCELLED empty.
        self.control.cancel_message(
            worker.agent_id,
            message.message_id,
            expected_delivery_attempt=0,
            decision_id=uuid4(),
            actor=Principal("worker", ("agents.resolve",)),
            approval_id=None,
            reason="race_cancel",
        )
        current = self.control.graph.load(worker.agent_id)
        ref, digest = terminal_result_identity(
            worker.agent_id,
            running.run_id,
            AgentState.CANCELLED,
            "cancelled",
            self.control.mailbox.load(worker.agent_id),
        )
        self.control.terminal(
            worker.agent_id,
            run_id=running.run_id,
            expected_attempt=current.attempt,
            state=AgentState.CANCELLED,
            reason="cancelled",
            result_ref=ref,
            result_digest=digest,
            source_message_ids=(),
        )
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual("stale_agent_run_fenced", deliver_outcome.get("code"))
        # the losing deliver appended zero events: mailbox still has 2 events
        final = self.control.mailbox.load(worker.agent_id)
        self.assertEqual(MessageStatus.CANCELLED, final[0].status)
        mailbox_events = self.store.read_stream(StreamId("mailbox", worker.agent_id))
        self.assertEqual(2, len(mailbox_events))
        self.assertEqual(
            AgentState.CANCELLED,
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
        # wait for both heartbeats to be durably committed (observable fact,
        # never a timing guess) before judging orphan-ness
        deadline = time.monotonic() + 10
        while (
            len(
                [
                    event
                    for event in self.store.read_stream(
                        StreamId("agent", worker.agent_id)
                    )
                    if event.event_type == "agent.heartbeat.v2"
                ]
            )
            < 2
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

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


    # ------------------------------------------------------------------
    # I3 resource/spawn/terminal atomicity (section 5.8)
    # ------------------------------------------------------------------

    def _complete_worker(self, worker) -> None:
        """Deliver + result + ACK + atomic terminal for one quiet worker."""
        message = self.control.mailbox.load(worker.agent_id)[0]
        running = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=10
        )
        delivered = self.control.deliver_message(
            worker.agent_id, message.message_id, run_id=running.run_id
        )
        self.control.record_message_result(
            worker.agent_id,
            message.message_id,
            run_id=running.run_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            outcome="ok",
        )
        self.control.ack_message(
            worker.agent_id, message.message_id, run_id=running.run_id
        )
        current = self.control.graph.load(worker.agent_id)
        ref, digest = terminal_result_identity(
            worker.agent_id,
            running.run_id,
            AgentState.COMPLETED,
            None,
            self.control.mailbox.load(worker.agent_id),
        )
        self.control.terminal(
            worker.agent_id,
            run_id=running.run_id,
            expected_attempt=current.attempt,
            state=AgentState.COMPLETED,
            reason=None,
            result_ref=ref,
            result_digest=digest,
            source_message_ids=(message.message_id,),
        )

    def _legacy_spawn_events(
        self,
        agent_id,
        *,
        parent,
        task="legacy",
        started_run=None,
        command=None,
    ) -> tuple[NewEvent, ...]:
        command = command or uuid4()
        occurred = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        spawned = NewEvent(
            uuid5(command, "event:spawned"),
            "agent.spawned.v1",
            1,
            occurred,
            {
                "agent_id": str(agent_id),
                "parent_agent_id": None if parent is None else str(parent),
                "task_id": task,
                "attempt": 1,
                "run_id": None,
                "principal_id": "worker",
                "scopes": ["read"],
                "context_mode": "fresh",
                "created_at": occurred.isoformat(),
            },
            EventMetadata(command, uuid5(command, "correlation"), actor="test"),
        )
        events = [spawned]
        if started_run is not None:
            started = NewEvent(
                uuid5(command, "event:started"),
                "agent.started.v1",
                1,
                occurred,
                {
                    "agent_id": str(agent_id),
                    "run_id": str(started_run),
                    "attempt": 1,
                    "lease_expires_at": (
                        occurred + timedelta(seconds=30)
                    ).isoformat(),
                },
                EventMetadata(command, uuid5(command, "correlation"), actor="test"),
            )
            events.append(started)
        return tuple(events)

    def _append_legacy_agent(self, agent_id, *, parent, started_run=None) -> None:
        command = uuid4()
        self.store.append_batch(
            (
                StreamWrite(
                    StreamId("agent", agent_id),
                    -1,
                    self._legacy_spawn_events(
                        agent_id,
                        parent=parent,
                        started_run=started_run,
                        command=command,
                    ),
                ),
            ),
            idempotency_key=command,
        )

    def test_concurrent_spawns_contend_for_last_parent_slot_one_child(self) -> None:
        """P0-03: two spawns pass the parent slot check, one commits."""
        for name in ("a", "b", "c"):
            self.control.spawn_agent(
                parent_agent_id=self.root.agent_id,
                task_id=name,
                principal_id="worker",
                scopes=("read",),
                context_mode=ContextMode.FRESH,
                semantic_idempotency_key="slot-" + name,
            )
        entered = threading.Barrier(2)
        release = threading.Event()
        outcomes: dict = {"wins": [], "errors": []}

        def fault(point: str, facts) -> None:
            if point == "d11.spawn.before_append":
                entered.wait(timeout=10)
                release.wait(10)

        racing = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(
                max_depth=3, max_total_agents=8, max_concurrent_children=4
            ),
            clock=self.clock,
            faults=fault,
        )

        def racer(name: str) -> None:
            try:
                racing.spawn_agent(
                    parent_agent_id=self.root.agent_id,
                    task_id="race-" + name,
                    principal_id="worker",
                    scopes=("read",),
                    context_mode=ContextMode.FRESH,
                    semantic_idempotency_key="slot-race-" + name,
                )
                outcomes["wins"].append(name)
            except AgentError as error:
                outcomes["errors"].append((name, error.code))

        with ThreadPoolExecutor(max_workers=2) as pool:
            tuple(pool.map(racer, ("x", "y")))
        self.assertEqual(1, len(outcomes["wins"]))
        self.assertEqual(1, len(outcomes["errors"]))
        self.assertEqual(
            "agent_concurrency_exceeded", outcomes["errors"][0][1]
        )
        # exactly one new child committed: capacity head is exact at 4
        capacity = self.control._parent_capacity(self.root.agent_id)
        self.assertEqual(4, capacity.active_count)
        self.assertEqual(4, len(self.control.graph.children(self.root.agent_id)))
        self.assertEqual(
            4,
            len(
                [
                    event
                    for event in self.store.read_stream(
                        StreamId("agent-capacity", self.root.agent_id)
                    )
                ]
            ),
        )

    def test_two_parents_contend_for_last_root_slot_one_child(self) -> None:
        """Two different parents race for the last root total slot."""
        racing = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(
                max_depth=3, max_total_agents=3, max_concurrent_children=4
            ),
            clock=self.clock,
        )
        p1 = racing.spawn_agent(
            parent_agent_id=self.root.agent_id,
            task_id="p1",
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        p2 = racing.spawn_agent(
            parent_agent_id=self.root.agent_id,
            task_id="p2",
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        entered = threading.Barrier(2)
        release = threading.Event()
        outcomes: dict = {"wins": [], "errors": []}

        def fault(point: str, facts) -> None:
            if point == "d11.spawn.before_append":
                entered.wait(timeout=10)
                release.wait(10)

        racing2 = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(
                max_depth=3, max_total_agents=3, max_concurrent_children=4
            ),
            clock=self.clock,
            faults=fault,
        )

        def racer(parent_id, name: str) -> None:
            try:
                racing2.spawn_agent(
                    parent_agent_id=parent_id,
                    task_id="child-" + name,
                    principal_id="worker",
                    scopes=("read",),
                    context_mode=ContextMode.FRESH,
                )
                outcomes["wins"].append(name)
            except AgentError as error:
                outcomes["errors"].append((name, error.code))

        with ThreadPoolExecutor(max_workers=2) as pool:
            tuple(
                pool.map(
                    lambda item: racer(item[0], item[1]),
                    ((p1.agent_id, "x"), (p2.agent_id, "y")),
                )
            )
        self.assertEqual(1, len(outcomes["wins"]))
        self.assertEqual(1, len(outcomes["errors"]))
        self.assertEqual("agent_total_exceeded", outcomes["errors"][0][1])
        budget = self.control._root_budget(self.root.agent_id)
        self.assertEqual(3, budget.active_count)

    def test_four_stream_spawn_batch_shared_commit(self) -> None:
        child = self._spawn("shared-commit")
        events = self.store.read_all()
        four = [
            event
            for event in events
            if event.commit_size == 4
            and event.event_type
            in (
                "agent.child-spawn-authorized.v1",
                "agent.capacity-reserved.v1",
                "budget.reserved.v2",
                "agent.spawned.v2",
            )
        ]
        self.assertEqual(4, len(four))
        self.assertEqual(1, len({event.commit_id for event in four}))
        self.assertEqual({4}, {event.commit_size for event in four})

    def test_root_spawn_wire_and_root_terminal_own_capacity_precondition(self) -> None:
        root = self.root
        root_events = self.store.read_stream(StreamId("agent", root.agent_id))
        spawned = [
            event
            for event in root_events
            if event.event_type == "agent.spawned.v2"
        ]
        self.assertEqual(1, len(spawned))
        payload = spawned[0].payload
        self.assertIsNone(payload["parent_agent_id"])
        self.assertEqual(str(root.agent_id), payload["root_agent_id"])
        self.assertEqual(0, payload["depth"])
        self.assertIsNone(payload["capacity_reservation_id"])
        self.assertIsNone(payload["budget_reservation_id"])
        # no pseudo capacity/budget stream is created for a root
        self.assertEqual(
            (),
            self.store.read_stream(StreamId("agent-capacity", root.agent_id)),
        )
        running = self.control.start_attempt(
            root.agent_id, expected_version=root.version, lease_seconds=10
        )
        ref, digest = terminal_result_identity(
            root.agent_id,
            running.run_id,
            AgentState.CANCELLED,
            "cancelled",
            (),
        )
        self.control.terminal(
            root.agent_id,
            run_id=running.run_id,
            expected_attempt=running.attempt,
            state=AgentState.CANCELLED,
            reason="cancelled",
            result_ref=ref,
            result_digest=digest,
            source_message_ids=(),
        )
        root_stream = self.store.read_stream(StreamId("agent", root.agent_id))
        terminal_events = [
            event
            for event in root_stream
            if event.event_type == "agent.cancelled.v2"
        ]
        self.assertEqual(1, len(terminal_events))
        # the root still has no capacity stream: the -1 precondition held
        self.assertEqual(
            (),
            self.store.read_stream(StreamId("agent-capacity", root.agent_id)),
        )

    def test_legacy_baseline_fixed_high_water_and_single_commit(self) -> None:
        store = SqliteEventStore(self.database)
        control = AgentControlPlane(
            store, limits=AgentBudgetLimits(), clock=self.clock
        )
        root = uuid4()
        c1 = uuid4()
        c2 = uuid4()
        self._append_legacy_agent(root, parent=None)
        self._append_legacy_agent(c1, parent=root)
        self._append_legacy_agent(c2, parent=root, started_run=uuid4())
        boundary = store.current_global_position()
        control.import_capacity_baseline(root)
        baseline_events = store.read_stream(StreamId("agent-capacity", root))
        self.assertEqual(1, len(baseline_events))
        baseline = baseline_events[0]
        self.assertEqual(
            "agent.capacity-baseline-imported.v1", baseline.event_type
        )
        self.assertEqual(boundary, baseline.payload["source_global_position"])
        reservations = sorted(
            baseline.payload["reservations"],
            key=lambda item: item["child_agent_id"],
        )
        self.assertEqual(
            sorted([str(c1), str(c2)]),
            [item["child_agent_id"] for item in reservations],
        )
        for item in reservations:
            self.assertEqual(item["reservation_id"], item["child_agent_id"])
        # a post-boundary legacy child never enters the committed digest
        c3 = uuid4()
        self._append_legacy_agent(c3, parent=root)
        control.import_capacity_baseline(root)
        baseline_events = store.read_stream(StreamId("agent-capacity", root))
        self.assertEqual(1, len(baseline_events))
        self.assertEqual(
            sorted([str(c1), str(c2)]),
            sorted(
                item["child_agent_id"]
                for item in baseline_events[0].payload["reservations"]
            ),
        )
        # repeated/concurrent initialization still writes exactly one baseline
        def import_again() -> None:
            AgentControlPlane(store, clock=self.clock).import_capacity_baseline(root)

        with ThreadPoolExecutor(max_workers=2) as pool:
            tuple(pool.map(lambda _: import_again(), (0, 1)))
        self.assertEqual(
            1, len(store.read_stream(StreamId("agent-capacity", root)))
        )
        # and the next real spawn succeeds on top of the baseline
        child = control.spawn_agent(
            parent_agent_id=root,
            task_id="new",
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
            semantic_idempotency_key="post-baseline-1",
        )
        self.assertEqual(AgentState.CREATED, child.state)
        capacity_types = [
            event.event_type
            for event in store.read_stream(StreamId("agent-capacity", root))
        ]
        self.assertEqual(
            ["agent.capacity-baseline-imported.v1", "agent.capacity-reserved.v1"],
            capacity_types,
        )

    def test_reconcile_empty_zero_events_and_typed_release_idempotent(self) -> None:
        receipt = self.control.reconcile_legacy_resources(self.root.agent_id)
        self.assertFalse(receipt.changed)
        self.assertEqual((), receipt.released_reservation_ids)
        self.assertEqual(0, self.control._budget(self.root.agent_id))
        budget_events = self.store.read_stream(
            StreamId("agent-budget", self.root.agent_id)
        )
        self.assertEqual((), budget_events)
        self.assertEqual(
            receipt,
            self.control.reconcile_legacy_resources(self.root.agent_id),
        )

    def test_reconcile_releases_only_provable_terminal_reservations(self) -> None:
        store = SqliteEventStore(self.database)
        control = AgentControlPlane(
            store, limits=AgentBudgetLimits(), clock=self.clock
        )
        root = uuid4()
        done_child = uuid4()
        live_child = uuid4()
        done_run = uuid4()
        self._append_legacy_agent(root, parent=None)
        self._append_legacy_agent(done_child, parent=root, started_run=done_run)
        self._append_legacy_agent(live_child, parent=root)
        command = uuid4()
        occurred = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)

        def _legacy_budget(child_id) -> NewEvent:
            return NewEvent(
                uuid5(command, "event:budget:" + str(child_id)),
                "budget.reserved.v1",
                1,
                occurred,
                {
                    "root_agent_id": str(root),
                    "child_agent_id": str(child_id),
                    "total_agents": 1,
                },
                EventMetadata(command, uuid5(command, "correlation"), actor="test"),
            )

        store.append_batch(
            (
                StreamWrite(
                    StreamId("agent-budget", root),
                    -1,
                    (_legacy_budget(done_child), _legacy_budget(live_child)),
                ),
            ),
            idempotency_key=command,
        )
        # only done_child has a provable terminal event
        terminal_command = uuid4()
        terminal_event = NewEvent(
            uuid5(terminal_command, "event:completed"),
            "agent.completed.v1",
            1,
            occurred,
            {
                "agent_id": str(done_child),
                "run_id": str(done_run),
                "outcome": "legacy-done",
            },
            EventMetadata(
                terminal_command,
                uuid5(terminal_command, "correlation"),
                actor="test",
            ),
        )
        done_latest = self.control.graph.load(done_child)
        store.append_batch(
            (
                StreamWrite(
                    StreamId("agent", done_child),
                    done_latest.version,
                    (terminal_event,),
                ),
            ),
            idempotency_key=terminal_command,
        )
        receipt = control.reconcile_legacy_resources(root)
        self.assertTrue(receipt.changed)
        self.assertEqual((done_child,), receipt.released_reservation_ids)
        budget = control._root_budget(root)
        self.assertEqual(1, budget.active_count)
        self.assertEqual(
            live_child, budget.active_reservations[0].child_agent_id
        )
        events = store.read_stream(StreamId("agent-budget", root))
        reconciled = [
            event
            for event in events
            if event.event_type == "budget.legacy-reconciled.v1"
        ]
        self.assertEqual(1, len(reconciled))
        self.assertEqual(
            str(done_child),
            reconciled[0].payload["releases"][0]["reservation_id"],
        )
        # second pass: nothing left to release, zero new writes
        second = control.reconcile_legacy_resources(root)
        self.assertFalse(second.changed)
        self.assertEqual(
            1,
            len(
                [
                    event
                    for event in store.read_stream(StreamId("agent-budget", root))
                    if event.event_type == "budget.legacy-reconciled.v1"
                ]
            ),
        )


    def test_parent_terminal_vs_spawn_two_commit_orders(self) -> None:
        # order 1: parent terminal commits first -> the spawn re-reads a
        # terminal parent and is refused (parent_agent_not_active).
        p = self._spawn("parent-terminal-first")
        p_run = self.control.start_attempt(
            p.agent_id, expected_version=p.version, lease_seconds=10
        )
        spawn_at_fault = threading.Event()
        spawn_gate = threading.Event()
        spawn_outcomes: list = []
        terminal_outcomes: list = []

        def spawn_holder() -> None:
            holder = AgentControlPlane(
                self.store,
                limits=AgentBudgetLimits(),
                clock=self.clock,
                faults=lambda point, facts: (
                    (spawn_at_fault.set(), spawn_gate.wait(10))
                    if point == "d11.spawn.before_append"
                    else None
                ),
            )
            try:
                holder.spawn_agent(
                    parent_agent_id=p.agent_id,
                    task_id="late",
                    principal_id="worker",
                    scopes=("read",),
                    context_mode=ContextMode.FRESH,
                    parent_run_id=p_run.run_id,
                )
                spawn_outcomes.append("ok")
            except AgentError as error:
                spawn_outcomes.append(error.code)

        st = threading.Thread(target=spawn_holder)
        st.start()
        self.assertTrue(spawn_at_fault.wait(10))
        try:
            ref, digest = terminal_result_identity(
                p.agent_id,
                p_run.run_id,
                AgentState.CANCELLED,
                "cancelled",
                (),
            )
            self.control.terminal(
                p.agent_id,
                run_id=p_run.run_id,
                expected_attempt=p_run.attempt,
                state=AgentState.CANCELLED,
                reason="cancelled",
                result_ref=ref,
                result_digest=digest,
                source_message_ids=(),
            )
            terminal_outcomes.append("ok")
        except AgentError as error:
            terminal_outcomes.append(error.code)
        spawn_gate.set()
        st.join(timeout=10)
        self.assertFalse(st.is_alive())
        self.assertEqual(["ok"], terminal_outcomes)
        self.assertEqual(["parent_agent_not_active"], spawn_outcomes)

        # order 2: spawn commits first -> terminal WEVs and re-reads active
        p2 = self._spawn("spawn-before-terminal")
        p2_run = self.control.start_attempt(
            p2.agent_id, expected_version=p2.version, lease_seconds=10
        )
        terminal_at_fault = threading.Event()
        terminal_gate = threading.Event()
        terminal2_outcomes: list = []

        def terminal2_holder() -> None:
            holder = AgentControlPlane(
                self.store,
                limits=AgentBudgetLimits(),
                clock=self.clock,
                faults=lambda point, facts: (
                    (terminal_at_fault.set(), terminal_gate.wait(10))
                    if point == "d11.terminal.before_append"
                    else None
                ),
            )
            ref2, digest2 = terminal_result_identity(
                p2.agent_id,
                p2_run.run_id,
                AgentState.CANCELLED,
                "cancelled",
                (),
            )
            try:
                holder.terminal(
                    p2.agent_id,
                    run_id=p2_run.run_id,
                    expected_attempt=p2_run.attempt,
                    state=AgentState.CANCELLED,
                    reason="cancelled",
                    result_ref=ref2,
                    result_digest=digest2,
                    source_message_ids=(),
                )
                terminal2_outcomes.append("ok")
            except AgentError as error:
                terminal2_outcomes.append(error.code)

        t2 = threading.Thread(target=terminal2_holder)
        t2.start()
        self.assertTrue(terminal_at_fault.wait(10))
        self.control.spawn_agent(
            parent_agent_id=p2.agent_id,
            task_id="c2",
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
            parent_run_id=p2_run.run_id,
        )
        terminal_gate.set()
        t2.join(timeout=10)
        self.assertFalse(t2.is_alive())
        self.assertEqual(["agent_children_active"], terminal2_outcomes)

    def test_terminal_response_loss_no_duplicate_release(self) -> None:
        worker = self._spawn("resp-loss")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="x",
            idempotency_key="resp-loss-1",
        )
        self._complete_worker(worker)
        release_events = [
            event
            for event in self.store.read_all()
            if event.event_type
            in ("agent.capacity-released.v1", "budget.released.v2")
        ]
        self.assertEqual(2, len(release_events))
        result_enqueues = [
            event
            for event in self.store.read_all()
            if event.event_type == "message.enqueued.v1"
            and event.payload.get("kind") == "result"
        ]
        self.assertEqual(1, len(result_enqueues))
        # response loss: the same terminal command retries only via receipt
        before = len(self.store.read_all())
        worker_latest = self.control.graph.load(worker.agent_id)
        self.assertEqual(AgentState.COMPLETED, worker_latest.state)
        self.assertEqual(before, len(self.store.read_all()))

    def test_terminal_aggregate_rejects_missing_extra_forged_and_parent_wire(self) -> None:
        worker = self._spawn("aggregate")
        message = self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="x",
            idempotency_key="aggregate-1",
        )
        running = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=10
        )
        delivered = self.control.deliver_message(
            worker.agent_id, message.message_id, run_id=running.run_id
        )
        self.control.record_message_result(
            worker.agent_id,
            message.message_id,
            run_id=running.run_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            outcome="aggregated",
        )
        self.control.ack_message(
            worker.agent_id, message.message_id, run_id=running.run_id
        )
        current = self.control.graph.load(worker.agent_id)
        ref, digest = terminal_result_identity(
            worker.agent_id,
            running.run_id,
            AgentState.COMPLETED,
            None,
            self.control.mailbox.load(worker.agent_id),
        )
        terminal_args = {
            "agent_id": worker.agent_id,
            "run_id": running.run_id,
            "expected_attempt": current.attempt,
            "state": AgentState.COMPLETED,
            "reason": None,
        }
        # missing the only ACKed result
        with self.assertRaises(AgentError) as raised:
            self.control.terminal(
                **terminal_args,
                result_ref=ref,
                result_digest=digest,
                source_message_ids=(),
            )
        self.assertEqual("agent_result_identity_conflict", raised.exception.code)
        # an extra/foreign message id
        with self.assertRaises(AgentError) as raised:
            self.control.terminal(
                **terminal_args,
                result_ref=ref,
                result_digest=digest,
                source_message_ids=(uuid4(),),
            )
        self.assertEqual("agent_result_identity_conflict", raised.exception.code)
        # forged ref
        with self.assertRaises(AgentError) as raised:
            self.control.terminal(
                **terminal_args,
                result_ref="agent-run-result:forged",
                result_digest=digest,
                source_message_ids=(message.message_id,),
            )
        self.assertEqual("agent_result_identity_conflict", raised.exception.code)
        # forged digest
        with self.assertRaises(AgentError) as raised:
            self.control.terminal(
                **terminal_args,
                result_ref=ref,
                result_digest="1" * 64,
                source_message_ids=(message.message_id,),
            )
        self.assertEqual("agent_result_identity_conflict", raised.exception.code)
        # valid settlement commits; parent RESULT wire is exact and body-less
        self.control.terminal(
            **terminal_args,
            result_ref=ref,
            result_digest=digest,
            source_message_ids=(message.message_id,),
        )
        parent_messages = self.control.mailbox.load(self.root.agent_id)
        self.assertEqual(1, len(parent_messages))
        result_message = parent_messages[0]
        self.assertEqual(MessageKind.RESULT, result_message.kind)
        self.assertEqual(worker.agent_id, result_message.from_agent_id)
        self.assertEqual(0, result_message.sequence)
        self.assertEqual(ref, result_message.body_ref)
        self.assertEqual(
            "child-result:" + str(worker.agent_id) + ":" + str(running.run_id),
            result_message.idempotency_key,
        )
        self.assertIsNone(result_message.result_summary)
        self.assertEqual(MessageStatus.QUEUED, result_message.status)
        self.assertEqual(
            0,
            self.control._parent_capacity(self.root.agent_id).active_count,
        )

    def test_parent_terminal_branch_skips_enqueue_releases_same_commit(self) -> None:
        parent = self._spawn("legacy-parent-window")
        parent_run = self.control.start_attempt(
            parent.agent_id, expected_version=parent.version, lease_seconds=10
        )
        worker = self.control.spawn_agent(
            parent_agent_id=parent.agent_id,
            task_id="child-of-parent",
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
            parent_run_id=parent_run.run_id,
        )
        message = self.control.send_message(
            worker.agent_id,
            from_agent_id=parent.agent_id,
            kind=MessageKind.TASK,
            body_ref="x",
            idempotency_key="parent-window-1",
        )
        running = self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=10
        )
        delivered = self.control.deliver_message(
            worker.agent_id, message.message_id, run_id=running.run_id
        )
        self.control.record_message_result(
            worker.agent_id,
            message.message_id,
            run_id=running.run_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            outcome="ok",
        )
        self.control.ack_message(
            worker.agent_id, message.message_id, run_id=running.run_id
        )
        current = self.control.graph.load(worker.agent_id)
        ref, digest = terminal_result_identity(
            worker.agent_id,
            running.run_id,
            AgentState.COMPLETED,
            None,
            self.control.mailbox.load(worker.agent_id),
        )
        release = threading.Event()
        at_fault = threading.Event()

        def holder_fault(point: str, facts) -> None:
            if point == "d11.terminal.before_append":
                at_fault.set()
                release.wait(10)

        holder = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(),
            clock=self.clock,
            faults=holder_fault,
        )
        outcomes: list = []

        def terminal_worker() -> None:
            try:
                holder.terminal(
                    worker.agent_id,
                    run_id=running.run_id,
                    expected_attempt=current.attempt,
                    state=AgentState.COMPLETED,
                    reason=None,
                    result_ref=ref,
                    result_digest=digest,
                    source_message_ids=(message.message_id,),
                )
                outcomes.append("ok")
            except AgentError as error:
                outcomes.append(error.code)

        thread = threading.Thread(target=terminal_worker)
        thread.start()
        self.assertTrue(at_fault.wait(10))
        # legacy/repair window: the parent's terminal is written directly,
        # exactly like old code that never checked active children.
        terminal_command = uuid4()
        occurred = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        parent_terminal = NewEvent(
            uuid5(terminal_command, "event:completed"),
            "agent.completed.v1",
            1,
            occurred,
            {
                "agent_id": str(parent.agent_id),
                "run_id": str(parent_run.run_id),
                "outcome": "legacy-repair",
            },
            EventMetadata(
                terminal_command,
                uuid5(terminal_command, "correlation"),
                actor="test",
            ),
        )
        parent_latest = self.control.graph.load(parent.agent_id)
        self.store.append_batch(
            (
                StreamWrite(
                    StreamId("agent", parent.agent_id),
                    parent_latest.version,
                    (parent_terminal,),
                ),
            ),
            idempotency_key=terminal_command,
        )
        release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["ok"], outcomes)
        self.assertEqual(
            AgentState.COMPLETED,
            self.control.graph.load(worker.agent_id).state,
        )
        # the parent-active branch fell back to the terminal-parent branch:
        # no enqueue on the parent mailbox, yet both releases are in the same
        # commit as the child terminal event.
        parent_mailbox = self.store.read_stream(
            StreamId("mailbox", parent.agent_id)
        )
        self.assertEqual((), parent_mailbox)
        all_events = self.store.read_all()
        terminal_events = [
            event
            for event in all_events
            if event.event_type == "agent.completed.v2"
        ]
        releases = [
            event
            for event in all_events
            if event.event_type
            in ("agent.capacity-released.v1", "budget.released.v2")
        ]
        self.assertEqual(1, len(terminal_events))
        self.assertEqual(2, len(releases))
        commit_ids = {event.commit_id for event in terminal_events + releases}
        self.assertEqual(1, len(commit_ids))
        self.assertEqual(
            0, self.control._parent_capacity(parent.agent_id).active_count
        )

    def test_double_orphan_race_only_one_orphan_event(self) -> None:
        worker = self._spawn("double-orphan")
        self.control.send_message(
            worker.agent_id,
            from_agent_id=self.root.agent_id,
            kind=MessageKind.TASK,
            body_ref="x",
            idempotency_key="double-orphan-1",
        )
        self.control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=10
        )
        self.clock.value += timedelta(seconds=11)

        def discover() -> None:
            AgentControlPlane(
                self.store, limits=AgentBudgetLimits(), clock=self.clock
            ).discover_orphans()

        with ThreadPoolExecutor(max_workers=2) as pool:
            tuple(pool.map(lambda _: discover(), (0, 1)))
        orphan_events = [
            event
            for event in self.store.read_stream(StreamId("agent", worker.agent_id))
            if event.event_type == "agent.orphaned.v1"
        ]
        self.assertEqual(1, len(orphan_events))
        self.assertEqual(
            AgentState.ORPHANED,
            self.control.graph.load(worker.agent_id).state,
        )

    def test_100_rounds_budget_never_negative_over_or_double_terminal(self) -> None:
        for index in range(100):
            worker = self.control.spawn_agent(
                parent_agent_id=self.root.agent_id,
                task_id="round-" + str(index),
                principal_id="worker",
                scopes=("read",),
                context_mode=ContextMode.FRESH,
                semantic_idempotency_key="round-" + str(index),
            )
            self.control.send_message(
                worker.agent_id,
                from_agent_id=self.root.agent_id,
                kind=MessageKind.TASK,
                body_ref="round-" + str(index),
                idempotency_key="round-msg-" + str(index),
            )
            self._complete_worker(worker)
            budget = self.control._root_budget(self.root.agent_id)
            self.assertGreaterEqual(budget.active_count, 0)
            self.assertLessEqual(
                budget.active_count, self.control.limits.max_total_agents
            )
            agent = self.control.graph.load(worker.agent_id)
            self.assertEqual(AgentState.COMPLETED, agent.state)
            terminals = [
                event
                for event in self.store.read_stream(
                    StreamId("agent", worker.agent_id)
                )
                if event.event_type.startswith("agent.completed.v2")
            ]
            self.assertEqual(1, len(terminals))
        self.assertEqual(
            0, self.control._root_budget(self.root.agent_id).active_count
        )
        self.assertEqual(
            0, self.control._parent_capacity(self.root.agent_id).active_count
        )



if __name__ == "__main__":
    unittest.main()
