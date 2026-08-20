"""D11 walkthrough: durable multi-agent control plane with read-only workers.

Run from ``v2/`` with ``PYTHONPATH=src``:

    python -B examples/day11_multi_agent_readonly.py

Everything is SQLite-backed and restarts cleanly; no shared workspace writes,
no network, and only the D11 read-only tool allowlist is permitted.
"""

from __future__ import annotations

import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.agents.graph import AgentState, ContextMode
from koawa_agent_v2.agents.messages import MessageKind
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


def main() -> dict:
    temporary = tempfile.TemporaryDirectory()
    database = Path(temporary.name) / "day11.sqlite3"
    store = SqliteEventStore(database)
    clock = MutableClock(datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc))
    control = AgentControlPlane(
        store,
        limits=AgentBudgetLimits(
            max_depth=3,
            max_total_agents=8,
            max_concurrent_children=4,
        ),
        clock=clock,
    )
    provider = ScriptedAgentProvider(
        {
            "locate": "tool:read_file",
            "review": "tool:list_files",
            "doomed": "raise:worker_failed",
        }
    )
    scheduler = AgentScheduler(control, provider=provider, lease_seconds=30)

    root = control.spawn_agent(
        parent_agent_id=None,
        task_id="root",
        principal_id="root",
        scopes=("read",),
        context_mode=ContextMode.FRESH,
    )
    tasks = {
        "locate": "locate",
        "review": "review",
        "doomed": "doomed",
    }
    children = {}
    for name, task in tasks.items():
        child = control.spawn_agent(
            parent_agent_id=root.agent_id,
            task_id=task,
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        control.send_message(
            child.agent_id,
            from_agent_id=root.agent_id,
            kind=MessageKind.TASK,
            body_ref=task,
            idempotency_key=f"{task}-1",
        )
        children[name] = child

    with ThreadPoolExecutor(max_workers=3) as pool:
        tuple(
            pool.map(
                scheduler.run_attempt,
                (child.agent_id for child in children.values()),
            )
        )
    summary = control.wait_agents(root.agent_id, timeout_seconds=1)
    states = {item["agent_id"]: item["state"] for item in summary}
    assert states[str(children["locate"].agent_id)] == AgentState.COMPLETED.value
    assert states[str(children["review"].agent_id)] == AgentState.COMPLETED.value
    assert states[str(children["doomed"].agent_id)] == AgentState.FAILED.value
    assert sorted(provider.tools_seen) == ["list_files", "read_file"]
    assert control._budget(root.agent_id) == 0

    # Orphan recovery across a "restart" (fresh store over the same file).
    orphan = control.spawn_agent(
        parent_agent_id=root.agent_id,
        task_id="orphan",
        principal_id="worker",
        scopes=("read",),
        context_mode=ContextMode.FRESH,
    )
    control.send_message(
        orphan.agent_id,
        from_agent_id=root.agent_id,
        kind=MessageKind.TASK,
        body_ref="locate",
        idempotency_key="orphan-1",
    )
    first = control.start_attempt(
        orphan.agent_id, expected_version=orphan.version, lease_seconds=5
    )
    clock.value += timedelta(seconds=6)
    control.discover_orphans()
    fresh_control = AgentControlPlane(
        SqliteEventStore(database),
        limits=AgentBudgetLimits(),
        clock=clock,
    )
    fresh_scheduler = AgentScheduler(
        fresh_control,
        provider=ScriptedAgentProvider({"locate": "tool:read_file"}),
        lease_seconds=30,
    )
    result = fresh_scheduler.run_attempt(orphan.agent_id)
    assert result.state is AgentState.COMPLETED
    recovered = fresh_control.graph.load(orphan.agent_id)
    assert recovered.attempt == 2
    assert recovered.run_id != first.run_id

    temporary.cleanup()
    return {
        "storage": {"temporary_sqlite": True, "external_services": []},
        "worker_states": states,
        "tools_seen": provider.tools_seen,
        "budget_after_terminal": 0,
        "orphan_recovery": {
            "attempt": recovered.attempt,
            "state": recovered.state.value,
            "new_run_id": True,
        },
        "all_assertions_passed": True,
    }


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2, sort_keys=True))
    sys.exit(0)
