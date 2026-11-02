"""Issue #18 F2: root authority is issued by the session, not by a name.

With an owner configured, a root agent can only be created for an existing
session created by that owner, and only under the issued root principal.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.agents.graph import ContextMode
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore


def _clock() -> datetime:
    return datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class SessionRootIssuanceTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SqliteEventStore(Path(temporary.name, "session.sqlite3"))
        self.control = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(
                max_depth=3, max_total_agents=8, max_concurrent_children=4
            ),
            clock=_clock,
            owner_id="local-user",
            root_principal_id="root",
        )
        self.runtime = ThreadRuntime(
            self.store, actor="local-user", owner_id="local-user"
        )

    def _thread(self, owner: str = "local-user"):
        other = ThreadRuntime(self.store, actor=owner, owner_id=owner)
        return other.create_thread("repo")

    def _spawn_root(self, *, thread_id=None, principal_id="root"):
        return self.control.spawn_agent(
            parent_agent_id=None,
            task_id="root",
            principal_id=principal_id,
            scopes=("read",),
            context_mode=ContextMode.FRESH,
            thread_id=thread_id,
        )

    def test_root_spawn_requires_a_session(self) -> None:
        with self.assertRaises(Exception) as raised:
            self._spawn_root()
        self.assertEqual(
            "root_spawn_requires_session", getattr(raised.exception, "code", None)
        )

    def test_root_is_issued_from_an_existing_session(self) -> None:
        thread = self._thread()
        root = self._spawn_root(thread_id=thread.thread_id)
        self.assertEqual("root", root.principal_id)

    def test_unknown_session_is_refused(self) -> None:
        from uuid import uuid4

        with self.assertRaises(Exception) as raised:
            self._spawn_root(thread_id=uuid4())
        self.assertEqual(
            "session_root_unissued", getattr(raised.exception, "code", None)
        )

    def test_session_owned_by_someone_else_is_refused(self) -> None:
        foreign = self._thread(owner="someone-else")
        with self.assertRaises(Exception) as raised:
            self._spawn_root(thread_id=foreign.thread_id)
        self.assertEqual(
            "session_owner_mismatch", getattr(raised.exception, "code", None)
        )

    def test_other_principal_names_are_not_issued(self) -> None:
        thread = self._thread()
        with self.assertRaises(Exception) as raised:
            self._spawn_root(thread_id=thread.thread_id, principal_id="admin")
        self.assertEqual(
            "root_principal_not_issued", getattr(raised.exception, "code", None)
        )

    def test_session_record_carries_the_owner(self) -> None:
        thread = self._thread()
        self.assertEqual("local-user", thread.owner_id)


class LegacyDelegationTest(unittest.TestCase):
    """Without a configured owner the embedded/test path stays available."""

    def test_no_owner_means_no_session_required(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SqliteEventStore(Path(temporary.name, "legacy.sqlite3"))
        control = AgentControlPlane(store, clock=_clock)
        root = control.spawn_agent(
            parent_agent_id=None,
            task_id="root",
            principal_id="root",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        self.assertEqual("root", root.principal_id)


if __name__ == "__main__":
    unittest.main()
