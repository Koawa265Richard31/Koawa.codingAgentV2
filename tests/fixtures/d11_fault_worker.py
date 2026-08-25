"""Child process used by D11 I2 kill-window tests.

The parent OS-kills this process once the named fault point has durably
written its marker; a fresh process then recovers from the same database.
All datetimes come from an injected fake clock so lease expiry is
deterministic; markers carry only ids/versions/attempts, never task text.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from uuid import uuid4

from koawa_agent_v2.agents.control import (
    AgentBudgetLimits,
    AgentControlPlane,
    Principal,
)
from koawa_agent_v2.agents.graph import AgentState, ContextMode
from koawa_agent_v2.agents.messages import MessageKind
from koawa_agent_v2.agents.scheduler import AgentScheduler, ScriptedAgentProvider
from koawa_agent_v2.control.sqlite_store import SqliteEventStore


DATABASE = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
CALLS = Path(sys.argv[3])
POINT = sys.argv[4]


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class RecordingProvider(ScriptedAgentProvider):
    def __init__(self, script: dict[str, str], calls_path: Path) -> None:
        super().__init__(script)
        calls_path.write_text(
            json.dumps({"provider_calls": 0}), encoding="utf-8"
        )
        self._calls_path = calls_path

    def run(self, task: str, *, tool_allowlist: frozenset[str]) -> str:
        self.calls.append(task)
        self._calls_path.write_text(
            json.dumps({"provider_calls": len(self.calls)}), encoding="utf-8"
        )
        return super().run(task, tool_allowlist=tool_allowlist)


def _block() -> None:
    Event().wait()


def main() -> None:
    store = SqliteEventStore(DATABASE)
    clock = Clock()

    def fault(point: str, facts) -> None:
        if point == POINT:
            MARKER.write_text(
                json.dumps(
                    {
                        "point": POINT,
                        "agent_id": str(facts.get("agent_id", "")),
                        "attempt": int(facts.get("attempt", 0)),
                        "message_ids": [
                            str(item) for item in facts.get("message_ids", [])
                        ],
                        "clock": clock.value.isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            _block()

    control = AgentControlPlane(
        store,
        limits=AgentBudgetLimits(
            max_depth=3, max_total_agents=8, max_concurrent_children=4
        ),
        clock=clock,
        faults=fault,
    )
    provider = RecordingProvider({"task": "ok"}, CALLS)
    root = control.spawn_agent(
        parent_agent_id=None,
        task_id="root",
        principal_id="root",
        scopes=("read",),
        context_mode=ContextMode.FRESH,
    )
    worker = control.spawn_agent(
        parent_agent_id=root.agent_id,
        task_id="task",
        principal_id="worker",
        scopes=("read",),
        context_mode=ContextMode.FRESH,
    )
    message = control.send_message(
        worker.agent_id,
        from_agent_id=root.agent_id,
        kind=MessageKind.TASK,
        body_ref="task",
        idempotency_key="kill-1",
    )
    scheduler = AgentScheduler(
        control, provider=provider, lease_seconds=3, faults=fault
    )

    if POINT == "d11.deliver.after_commit":
        running = control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=3
        )
        control.deliver_message(
            worker.agent_id, message.message_id, run_id=running.run_id
        )
        _block()
    elif POINT in (
        "d11.enqueue.after_commit",
        "d11.provider.entered",
        "d11.result.after_commit",
        "d11.ack.after_commit",
        "d11.terminal.after_commit",
    ):
        try:
            scheduler.run_attempt(worker.agent_id)
        except Exception:
            pass
        _block()
    elif POINT in ("d11.unresolved.after_commit", "d11.waiting.after_commit"):
        running = control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=3
        )
        control.deliver_message(
            worker.agent_id, message.message_id, run_id=running.run_id
        )
        clock.value += timedelta(seconds=4)
        control.discover_orphans()
        try:
            scheduler.run_attempt(worker.agent_id)
        except Exception:
            pass
        _block()
    elif POINT == "d11.resume.after_commit":
        running = control.start_attempt(
            worker.agent_id, expected_version=worker.version, lease_seconds=3
        )
        control.deliver_message(
            worker.agent_id, message.message_id, run_id=running.run_id
        )
        clock.value += timedelta(seconds=4)
        control.discover_orphans()
        result = scheduler.run_attempt(worker.agent_id)
        if result.state is AgentState.WAITING:
            waiting = control.graph.load(worker.agent_id)
            control.requeue_message(
                worker.agent_id,
                waiting.blocking_message_ids[0],
                expected_delivery_attempt=1,
                decision_id=uuid4(),
                actor=Principal("worker", ("agents.resolve",)),
                approval_id=None,
                resolution_kind="proven_not_started",
                reason="test",
            )
            try:
                scheduler.run_attempt(worker.agent_id)
            except Exception:
                pass
        _block()


if __name__ == "__main__":
    main()
