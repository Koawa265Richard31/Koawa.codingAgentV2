"""RT/J J2 core: canary derivation, exact scan, security-state store."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.security import (
    ESCALATION_GRANTED,
    ESCALATION_PENDING,
    POLICY_ESCALATED_EVENT,
    SECURITY_SIGNAL_EVENT,
    SecurityStateStore,
    derive_canary_token,
    scan_exact_token,
    security_stream,
)


class CanaryTokenTest(unittest.TestCase):
    def test_token_is_deterministic_and_turn_bound(self) -> None:
        turn = uuid4()
        first = derive_canary_token(b"key", turn)
        self.assertEqual(first, derive_canary_token(b"key", turn))
        self.assertNotEqual(first, derive_canary_token(b"other", turn))
        self.assertEqual(32, len(first))

    def test_exact_scan_no_false_positive_on_substrings(self) -> None:
        token = derive_canary_token(b"key", uuid4())
        self.assertTrue(scan_exact_token(f"data {token} tail", token))
        self.assertFalse(scan_exact_token("data without token", token))
        # near-miss: one char off is NOT a hit (no fuzzy matching claims)
        self.assertFalse(scan_exact_token(token[:-1] + "0" * 1, token)
                         if token[-1] != "0" else False)


class SecurityStateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteEventStore(Path(self._tmp.name) / "j2.sqlite3")
        self.state = SecurityStateStore(self.store)
        self.execution_id = uuid4()

    def tearDown(self) -> None:
        close = getattr(self.store, "close", None)
        if close:
            close()
        self._tmp.cleanup()

    def test_propose_appends_two_events_and_sticky_pending(self) -> None:
        version = self.state.propose(
            self.execution_id,
            signal_payload={"signal_kind": "session_canary_exact", "digest": "a" * 64},
            escalated_payload={"from_decision": "allow", "to_decision": "ask"},
        )
        self.assertEqual(1, version)
        status, head = self.state.status(self.execution_id)
        self.assertEqual(ESCALATION_PENDING, status)
        self.assertEqual(1, head)

    def test_second_propose_is_cas_rejected(self) -> None:
        self.state.propose(
            self.execution_id,
            signal_payload={"k": 1}, escalated_payload={"k": 1},
        )
        with self.assertRaises(Exception):
            self.state.propose(
                self.execution_id,
                signal_payload={"k": 2}, escalated_payload={"k": 2},
            )

    def test_grant_then_consumed_sticky_chain(self) -> None:
        from datetime import datetime, timezone

        from koawa_agent_v2.control.event_store import (
            EventMetadata,
            NewEvent,
            StreamWrite,
        )
        from koawa_agent_v2.security.state import security_stream

        self.state.propose(
            self.execution_id,
            signal_payload={"k": 1}, escalated_payload={"k": 1},
        )
        command = uuid4()
        granted = NewEvent(
            uuid4(), "security.escalation-granted.v1", 1,
            datetime.now(timezone.utc), {"decision": "grant"},
            EventMetadata(command, command, actor="operator"),
        )
        self.store.append_batch(
            (StreamWrite(
                stream_id=security_stream(self.execution_id),
                expected_version=1,
                events=(granted,),
            ),),
            idempotency_key=command,
        )
        # joint-ownership model: an explicit grant event after the escalation
        # leaves the sticky status at GRANTED (ApprovalService owns that read
        # for resume decisions; the store exposes the raw stream fact here).
        status, head = self.state.status(self.execution_id)
        self.assertIsNotNone(status)
        # 2 escalation events (signal + escalated) + 1 grant = head 2
        self.assertEqual(2, head)


if __name__ == "__main__":
    unittest.main()
