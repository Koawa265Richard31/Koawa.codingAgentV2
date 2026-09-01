"""Remaining D11 real-process kill windows with deterministic recovery."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from uuid import UUID, uuid5, NAMESPACE_URL

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane, Principal
from koawa_agent_v2.agents.graph import AgentState, ContextMode
from koawa_agent_v2.agents.messages import MessageKind, MessageStatus
from koawa_agent_v2.agents.scheduler import AgentScheduler, ScriptedAgentProvider
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.telemetry.faults import FAULT_SPECS, NoOpFaultPort
from scripts.stability_benchmark import atomic_json


D11_REMAINING_POINTS = (
    "d11.enqueue.before_append", "d11.deliver.before_append",
    "d11.provider.returned", "d11.result.before_append",
    "d11.ack.before_append", "d11.resume.before_append",
    "d11.cancel.before_append", "d11.cancel.after_commit",
    "d11.resources.baseline.before_append", "d11.resources.baseline.after_commit",
    "d11.spawn.after_read", "d11.spawn.before_append", "d11.spawn.after_commit",
    "d11.heartbeat.before_append", "d11.heartbeat.after_commit",
    "d11.orphan.before_append", "d11.orphan.after_commit",
    "d11.takeover.before_append", "d11.takeover.after_commit",
    "d11.terminal.after_read", "d11.terminal.before_append",
)
START = datetime(2030, 1, 1, tzinfo=timezone.utc)
_SCHEDULER_POINTS = frozenset({
    "d11.provider.returned", "d11.result.before_append",
    "d11.ack.before_append", "d11.terminal.after_read",
    "d11.terminal.before_append",
})


class Clock:
    def __init__(self, value: datetime = START):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class CountingProvider(ScriptedAgentProvider):
    def __init__(self, root: Path, name: str):
        super().__init__({"task": "ok"})
        self.path = root / name
        atomic_json(self.path, {"calls": 0})

    def run(self, task: str, *, tool_allowlist: frozenset[str]) -> str:
        result = super().run(task, tool_allowlist=tool_allowlist)
        atomic_json(self.path, {"calls": len(self.calls)})
        return result


class D11KillPort(NoOpFaultPort):
    def __init__(self, root: Path, point: str, clock: Clock):
        self.root, self.point, self.clock = root, point, clock
        self.armed = False

    def hit(self, point, facts):
        super().hit(point, facts)
        if not self.armed or point != self.point:
            return
        visible = SqliteEventStore(self.root / "runtime.db").read_all()
        atomic_json(self.root / "ready.json", {
            "point": point,
            "point_class": FAULT_SPECS[point].point_class.value,
            "crash_pid": os.getpid(),
            "clock": self.clock.value.isoformat(),
            "visible_count": len(visible),
            "visible_events": [
                [event.stream_id.category, event.stream_version, event.event_type]
                for event in visible
            ],
            "facts": dict(facts),
        })
        Event().wait()


def _control(store, clock: Clock, port=None) -> AgentControlPlane:
    return AgentControlPlane(
        store,
        limits=AgentBudgetLimits(max_depth=3, max_total_agents=8, max_concurrent_children=4),
        clock=clock,
        **({"fault_port": port} if port is not None else {}),
    )


def _seed(control: AgentControlPlane):
    root = control.spawn_agent(
        parent_agent_id=None, task_id="root", principal_id="root", scopes=("read",),
        context_mode=ContextMode.FRESH, semantic_idempotency_key="d11-root",
    )
    worker = control.spawn_agent(
        parent_agent_id=root.agent_id, task_id="task", principal_id="worker", scopes=("read",),
        context_mode=ContextMode.FRESH, semantic_idempotency_key="d11-worker",
    )
    message = control.send_message(
        worker.agent_id, from_agent_id=root.agent_id, kind=MessageKind.TASK,
        body_ref="task", idempotency_key="d11-message",
    )
    return root, worker, message


def _prepare_waiting(control: AgentControlPlane, clock: Clock, worker, message):
    running = control.start_attempt(worker.agent_id, expected_version=worker.version, lease_seconds=3)
    control.deliver_message(worker.agent_id, message.message_id, run_id=running.run_id)
    clock.value += timedelta(seconds=4)
    control.discover_orphans()
    taken = control.start_attempt(
        worker.agent_id, expected_version=control.graph.load(worker.agent_id).version,
        lease_seconds=3,
    )
    waiting = control.enter_waiting_for_resolution(
        worker.agent_id, run_id=taken.run_id, attempt=taken.attempt,
    )
    control.requeue_message(
        worker.agent_id, message.message_id, expected_delivery_attempt=1,
        decision_id=uuid5(NAMESPACE_URL, "d11-resolve"),
        actor=Principal("worker", ("agents.resolve",)), approval_id=None,
        resolution_kind="proven_not_started", reason="fixture",
    )
    return waiting


def crash_d11(root_path: Path, point: str) -> None:
    if point not in D11_REMAINING_POINTS:
        raise ValueError("unsupported D11 point")
    store = SqliteEventStore(root_path / "runtime.db")
    clock = Clock()
    base = _control(store, clock)
    root, worker, message = _seed(base)
    port = D11KillPort(root_path, point, clock)
    target = _control(store, clock, port)
    request = {
        "point": point, "root_id": str(root.agent_id),
        "worker_id": str(worker.agent_id), "message_id": str(message.message_id),
        "decision_id": str(uuid5(NAMESPACE_URL, "d11-cancel")),
    }
    atomic_json(root_path / "request.json", request)

    if point == "d11.enqueue.before_append":
        port.armed = True
        target.send_message(
            worker.agent_id, from_agent_id=root.agent_id, kind=MessageKind.TASK,
            body_ref="second", idempotency_key="d11-target-enqueue",
        )
    elif point == "d11.deliver.before_append":
        running = base.start_attempt(worker.agent_id, expected_version=worker.version, lease_seconds=3)
        request.update({"run_id": str(running.run_id), "attempt": running.attempt})
        atomic_json(root_path / "request.json", request)
        port.armed = True
        target.deliver_message(worker.agent_id, message.message_id, run_id=running.run_id)
    elif point in _SCHEDULER_POINTS:
        atomic_json(root_path / "request.json", request)
        port.armed = True
        AgentScheduler(
            target, provider=CountingProvider(root_path, "provider.json"),
            lease_seconds=30, fault_port=port,
        ).run_attempt(worker.agent_id)
    elif point == "d11.resume.before_append":
        waiting = _prepare_waiting(base, clock, worker, message)
        request["attempt"] = waiting.attempt
        atomic_json(root_path / "request.json", request)
        port.armed = True
        target.start_attempt(worker.agent_id, expected_version=waiting.version, lease_seconds=3)
    elif point.startswith("d11.cancel."):
        atomic_json(root_path / "request.json", request)
        port.armed = True
        target.cancel_message(
            worker.agent_id, message.message_id, expected_delivery_attempt=0,
            decision_id=UUID(request["decision_id"]),
            actor=Principal("worker", ("agents.resolve",)), approval_id=None,
            reason="fixture",
        )
    elif point.startswith("d11.resources.baseline."):
        # The child seed already owns a capacity stream; use a fresh root whose
        # root spawn has no pseudo-capacity projection.
        legacy = base.spawn_agent(
            parent_agent_id=None, task_id="legacy", principal_id="root", scopes=("read",),
            semantic_idempotency_key="d11-legacy-root",
        )
        request["resource_root_id"] = str(legacy.agent_id)
        atomic_json(root_path / "request.json", request)
        port.armed = True
        target.import_capacity_baseline(legacy.agent_id)
    elif point.startswith("d11.spawn."):
        atomic_json(root_path / "request.json", request)
        port.armed = True
        target.spawn_agent(
            parent_agent_id=root.agent_id, task_id="target-child",
            principal_id="worker", scopes=("read",),
            semantic_idempotency_key="d11-target-child",
        )
    elif point.startswith("d11.heartbeat."):
        running = base.start_attempt(worker.agent_id, expected_version=worker.version, lease_seconds=30)
        request.update({"run_id": str(running.run_id), "attempt": running.attempt})
        atomic_json(root_path / "request.json", request)
        port.armed = True
        target.heartbeat(
            worker.agent_id, run_id=running.run_id, attempt=running.attempt,
            lease_seconds=30, beat_number=7,
        )
    elif point.startswith("d11.orphan."):
        running = base.start_attempt(worker.agent_id, expected_version=worker.version, lease_seconds=3)
        request.update({"run_id": str(running.run_id), "attempt": running.attempt})
        clock.value += timedelta(seconds=4)
        atomic_json(root_path / "request.json", request)
        port.armed = True
        target.discover_orphans()
    elif point.startswith("d11.takeover."):
        running = base.start_attempt(worker.agent_id, expected_version=worker.version, lease_seconds=3)
        base.deliver_message(worker.agent_id, message.message_id, run_id=running.run_id)
        clock.value += timedelta(seconds=4)
        base.discover_orphans()
        orphan = base.graph.load(worker.agent_id)
        atomic_json(root_path / "request.json", request)
        port.armed = True
        target.start_attempt(worker.agent_id, expected_version=orphan.version, lease_seconds=3)
    else:
        raise AssertionError("unrouted D11 point")
    if not (root_path / "request.json").exists():
        atomic_json(root_path / "request.json", request)
    raise AssertionError("D11 operation missed kill point")


def recover_d11(root_path: Path) -> None:
    request = json.loads((root_path / "request.json").read_text(encoding="utf-8"))
    marker = json.loads((root_path / "ready.json").read_text(encoding="utf-8"))
    store = SqliteEventStore(root_path / "runtime.db")
    clock = Clock(datetime.fromisoformat(marker["clock"]))
    control = _control(store, clock)
    point = request["point"]
    root_id, worker_id, message_id = map(
        UUID, (request["root_id"], request["worker_id"], request["message_id"]),
    )

    if point == "d11.enqueue.before_append":
        control.send_message(
            worker_id, from_agent_id=root_id, kind=MessageKind.TASK,
            body_ref="second", idempotency_key="d11-target-enqueue",
        )
    elif point == "d11.deliver.before_append":
        control.deliver_message(worker_id, message_id, run_id=UUID(request["run_id"]))
    elif point in _SCHEDULER_POINTS:
        clock.value += timedelta(minutes=5)
        control.discover_orphans()
        AgentScheduler(
            control, provider=CountingProvider(root_path, "recovery-provider.json"),
            lease_seconds=30,
        ).run_attempt(worker_id)
        if json.loads((root_path / "recovery-provider.json").read_text())["calls"] != 0:
            raise AssertionError("D11 recovery replayed provider")
    elif point == "d11.resume.before_append":
        current = control.graph.load(worker_id)
        if current.state is AgentState.WAITING:
            control.start_attempt(worker_id, expected_version=current.version, lease_seconds=3)
    elif point.startswith("d11.cancel."):
        current = control.mailbox.snapshot(worker_id)
        existing = next(item for item in current.messages if item.message_id == message_id)
        if existing.status is not MessageStatus.CANCELLED:
            control.cancel_message(
                worker_id, message_id, expected_delivery_attempt=0,
                decision_id=UUID(request["decision_id"]),
                actor=Principal("worker", ("agents.resolve",)), approval_id=None,
                reason="fixture",
            )
    elif point.startswith("d11.resources.baseline."):
        control.import_capacity_baseline(UUID(request["resource_root_id"]))
    elif point.startswith("d11.spawn."):
        control.spawn_agent(
            parent_agent_id=root_id, task_id="target-child", principal_id="worker",
            scopes=("read",), semantic_idempotency_key="d11-target-child",
        )
    elif point.startswith("d11.heartbeat."):
        control.heartbeat(
            worker_id, run_id=UUID(request["run_id"]), attempt=request["attempt"],
            lease_seconds=30, beat_number=7,
        )
    elif point.startswith("d11.orphan."):
        control.discover_orphans()
    elif point.startswith("d11.takeover."):
        current = control.graph.load(worker_id)
        if current.state is AgentState.ORPHANED:
            control.start_attempt(worker_id, expected_version=current.version, lease_seconds=3)
    else:
        raise AssertionError("unrouted D11 recovery")

    events = store.read_all()
    # Rebuild every affected projection in the fresh interpreter.
    control.graph.load(root_id)
    control.graph.load(worker_id)
    control.mailbox.snapshot(worker_id)
    atomic_json(root_path / "recovered.json", {
        "point": point,
        "events": [
            [event.stream_id.category, event.stream_version, event.event_type]
            for event in events
        ],
        "worker_state": control.graph.load(worker_id).state.value,
        "message_states": [item.status.value for item in control.mailbox.load(worker_id)],
    })
