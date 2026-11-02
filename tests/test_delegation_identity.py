"""Issue #15 F1 regression: a child may not adopt an ancestor's identity.

Measured before the fix: a ``worker`` parent could spawn a child with
``principal_id="root"``, and policy rules scoped to ``("root",)`` matched it
by name.  Scopes were narrowed correctly; the NAME was the hole.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.agents.graph import ContextMode
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.policy import PolicyError, Principal


def _clock() -> datetime:
    return datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class DelegationIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SqliteEventStore(
            Path(temporary.name, "delegation.sqlite3")
        )
        self.control = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(
                max_depth=3, max_total_agents=8, max_concurrent_children=4
            ),
            clock=_clock,
        )
        self.root = self.control.spawn_agent(
            parent_agent_id=None,
            task_id="root",
            principal_id="root",
            scopes=("workspace.read", "workspace.write"),
            context_mode=ContextMode.FRESH,
        )
        self.worker = self.control.spawn_agent(
            parent_agent_id=self.root.agent_id,
            task_id="worker",
            principal_id="worker",
            scopes=("workspace.read", "workspace.write"),
            context_mode=ContextMode.FRESH,
        )

    def _spawn(self, principal_id: str, *, scopes=("workspace.read",)):
        return self.control.spawn_agent(
            parent_agent_id=self.worker.agent_id,
            task_id="child-" + principal_id,
            principal_id=principal_id,
            scopes=scopes,
            context_mode=ContextMode.FRESH,
        )

    def test_child_cannot_claim_ancestor_principal(self) -> None:
        with self.assertRaises(Exception) as raised:
            self._spawn("root")
        self.assertEqual(
            "child_principal_impersonation", getattr(raised.exception, "code", None)
        )

    def test_child_may_inherit_parent_principal(self) -> None:
        child = self._spawn("worker")
        self.assertEqual("worker", child.principal_id)

    def test_child_may_use_a_new_principal_name(self) -> None:
        child = self._spawn("analyst")
        self.assertEqual("analyst", child.principal_id)

    def test_scope_escalation_is_still_refused(self) -> None:
        with self.assertRaises(Exception) as raised:
            self._spawn(
                "analyst", scopes=("workspace.read", "workspace.write", "sandbox.test")
            )
        self.assertEqual(
            "child_scope_escalation", getattr(raised.exception, "code", None)
        )


class PrincipalNarrowTest(unittest.TestCase):
    def test_cannot_adopt_the_delegating_principal(self) -> None:
        principal = Principal(
            "worker", ("workspace.read",), parent_principal_id="root",
        )
        with self.assertRaises(PolicyError) as raised:
            principal.narrow("root", ["workspace.read"])
        self.assertEqual(
            "child_principal_impersonation", raised.exception.code
        )

    def test_cannot_adopt_own_name(self) -> None:
        principal = Principal("worker", ("workspace.read",))
        with self.assertRaises(PolicyError) as raised:
            principal.narrow("worker", ["workspace.read"])
        self.assertEqual(
            "child_principal_impersonation", raised.exception.code
        )

    def test_new_name_narrows_scope(self) -> None:
        principal = Principal("worker", ("workspace.read", "workspace.write"))
        child = principal.narrow("analyst", ["workspace.write", "sandbox.test"])
        self.assertEqual("analyst", child.principal_id)
        self.assertEqual(("workspace.write",), child.scopes)


if __name__ == "__main__":
    unittest.main()
