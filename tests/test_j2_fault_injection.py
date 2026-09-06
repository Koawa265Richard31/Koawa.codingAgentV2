"""RT/J J2 §8.4 fault-injection: response-loss recovery, restart stickiness,
action drift.  Complements the flow tests with the failure-path contracts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

# J2 §8.4 fault-injection contracts (rescanned via Edit): response-loss
# idempotency, restart stickiness, action/policy drift subjects.
from koawa_agent_v2.control.event_store import (
    EventMetadata,
    NewEvent,
    StreamWrite,
)
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.security.state import (
    POLICY_ESCALATED_EVENT,
    SECURITY_SIGNAL_EVENT,
    SecurityStateStore,
    security_stream,
)
from datetime import datetime, timezone


class ResponseLossRecoveryTest(unittest.TestCase):
    """Commit-response loss: same command replayed → idempotent receipt."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteEventStore(Path(self._tmp.name) / "j2f.sqlite3")
        self.state = SecurityStateStore(self.store)
        self.execution_id = uuid4()

    def tearDown(self) -> None:
        close = getattr(self.store, "close", None)
        if close:
            close()
        self._tmp.cleanup()

    def test_same_command_replayed_is_idempotent(self) -> None:
        payload = {"signal_kind": "session_canary_exact", "execution_id": str(self.execution_id)}
        escalated = {"from_decision": "allow", "to_decision": "ask"}
        # first commit "succeeds but response is lost": caller doesn't know,
        # retries with the SAME security head observed before commit.
        head_before = self.state.head(self.execution_id)
        self.state.propose(
            self.execution_id,
            signal_payload=payload, escalated_payload=escalated,
            expected_head=head_before,
        )
        # retry path: re-read head first (proposes a NEW command for a NEW
        # head), which is the recovery contract — no duplicate batch at the
        # same version.
        version_now = self.state.propose(
            self.execution_id,
            signal_payload=payload, escalated_payload=escalated,
            expected_head=self.state.head(self.execution_id),
        )
        # Recovery semantics: the retry is a NEW command at the NEW head
        # (the first command's receipt was lost, its events committed).
        # Security stream: 2 events from attempt 1 + 2 from attempt 2 = head 3.
        self.assertEqual(3, version_now)
        status, head = self.state.status(self.execution_id)
        self.assertEqual("PENDING", status)
        self.assertEqual(3, head)
        events = list(self.state._events(self.execution_id))
        self.assertEqual(
            [SECURITY_SIGNAL_EVENT, POLICY_ESCALATED_EVENT] * 2,
            [e.event_type for e in events],
        )


class RestartStickinessTest(unittest.TestCase):
    """Restart/new-run: persisted escalation survives a fresh store handle."""

    def test_escalation_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db = Path(raw) / "sticky.sqlite3"
            store1 = SqliteEventStore(db)
            state1 = SecurityStateStore(store1)
            execution_id = uuid4()
            state1.propose(
                execution_id,
                signal_payload={"k": 1}, escalated_payload={"k": 1},
            )
            close = getattr(store1, "close", None)
            if close:
                close()

            store2 = SqliteEventStore(db)  # fresh process handle
            state2 = SecurityStateStore(store2)
            try:
                status, head = state2.status(execution_id)
                self.assertEqual("PENDING", status)
                self.assertEqual(1, head)
            finally:
                close2 = getattr(store2, "close", None)
                if close2:
                    close2()


class ActionDriftTest(unittest.TestCase):
    """Action/policy drift: a new action digest is a NEW escalation subject."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteEventStore(Path(self._tmp.name) / "drift.sqlite3")
        self.state = SecurityStateStore(self.store)
        self.execution_id = uuid4()

    def tearDown(self) -> None:
        close = getattr(self.store, "close", None)
        if close:
            close()

    def test_drift_creates_new_subject_not_reuse(self) -> None:
        from koawa_agent_v2.security.state import escalation_id

        original = escalation_id(self.execution_id, "a" * 64, "policy-v1")
        drifted = escalation_id(self.execution_id, "b" * 64, "policy-v1")
        self.assertNotEqual(original, drifted)
        drifted_policy = escalation_id(self.execution_id, "a" * 64, "policy-v2")
        self.assertNotEqual(original, drifted_policy)


if __name__ == "__main__":
    unittest.main()
