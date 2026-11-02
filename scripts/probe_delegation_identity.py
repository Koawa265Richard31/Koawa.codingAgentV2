"""Reproduce F1/F2 from docs/open-active/agent-team-security-baseline.md.

Question: can a caller mint an agent that carries another principal's name?

Part A  - delegation: a non-root parent spawns a child named "root".
Part A2 - control: the same parent cannot widen its own scope.
Part B  - policy: Principal.narrow() accepts any caller-chosen name.
Part B2 - the forged name matches a rule scoped to principal_ids=("root",).

Run:  PYTHONPATH=src python scripts/probe_delegation_identity.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.getcwd(), "src"))

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.agents.graph import ContextMode
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.policy import Decision, Principal, PolicyRule


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


def main() -> int:
    work = tempfile.mkdtemp(prefix="koawa-probe-delegation-")
    store = SqliteEventStore(Path(work, "probe.sqlite3"))
    control = AgentControlPlane(
        store,
        limits=AgentBudgetLimits(
            max_depth=3, max_total_agents=8, max_concurrent_children=4
        ),
        clock=_Clock(),
    )

    worker = control.spawn_agent(
        parent_agent_id=None,
        task_id="worker-root",
        principal_id="worker",
        scopes=("workspace.read", "workspace.write"),
        context_mode=ContextMode.FRESH,
    )
    print("parent principal:", worker.principal_id, "scopes:", worker.scopes)

    try:
        forged = control.spawn_agent(
            parent_agent_id=worker.agent_id,
            task_id="claim-root",
            principal_id="root",
            scopes=("workspace.write",),
            context_mode=ContextMode.FRESH,
        )
    except Exception as error:  # noqa: BLE001 - the probe reports the code
        print("PART A: refused ->", getattr(error, "code", type(error).__name__))
    else:
        print("PART A: ACCEPTED -> child principal:", forged.principal_id)

    try:
        control.spawn_agent(
            parent_agent_id=worker.agent_id,
            task_id="widen",
            principal_id="worker",
            scopes=("workspace.read", "workspace.write", "sandbox.test"),
            context_mode=ContextMode.FRESH,
        )
    except Exception as error:  # noqa: BLE001
        print("PART A2: scope widening refused ->", getattr(error, "code", "?"))
    else:
        print("PART A2: ACCEPTED (scope widening NOT prevented)")

    parent = Principal("worker", ("workspace.read", "workspace.write"))
    narrowed = parent.narrow("root", ["workspace.write"])
    print(
        "PART B: narrowed principal_id=",
        narrowed.principal_id,
        "parent=",
        narrowed.parent_principal_id,
        "scopes=",
        narrowed.scopes,
    )
    rule = PolicyRule(
        "builtin-patch-allow-or-ask",
        Decision.ALLOW,
        tool_names=("apply_patch",),
        principal_ids=("root",),
        required_scopes=("workspace.write",),
    )
    print(
        "PART B2: rule principal_ids=",
        rule.principal_ids,
        "| name matches forged principal:",
        narrowed.principal_id in rule.principal_ids,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
