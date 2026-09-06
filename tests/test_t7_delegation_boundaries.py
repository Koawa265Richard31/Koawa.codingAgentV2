"""T7 delegation-chain boundary pinning (PSEC/S5, maintainer-approved 2026-09-06).

Updated 2026-09-06 by semantics-C implementation: the T7-b "structural
invisibility" declaration was flipped by shipped code — plain hit() still
scans the own turn token only, while hit_multi() adds deterministically
derived ancestor seed tokens (see tests/test_j2_semantics_c.py for the
config/executor wiring).  These tests pin the CURRENT boundary:

- t7b: own-turn scan ignores the parent seed; the ancestor seed scan hits it.
- t7c: the durable mailbox message schema carries no trust-level field, so a
  parent message containing untrusted content presents no trust signal to the
  child agent (declared, T7-c).

If the boundary moves again, flip these tests together with the
corresponding threat-model T7 row — never silently.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.agents.messages import (
    MessageKind,
    MessageRecord,
    MessageStatus,
)
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.security import SecurityGate, derive_canary_token

KEY = b"t7-boundary-pinning-key"


class T7BCanaryDoesNotPropagateAcrossTurns(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteEventStore(Path(self._tmp.name) / "t7b.sqlite3")
        self.gate = SecurityGate(self.store, KEY)
        self.parent_turn = uuid4()
        self.child_turn = uuid4()
        self.parent_token = derive_canary_token(KEY, self.parent_turn)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_t7b_own_turn_scan_ignores_parent_seed(self) -> None:
        self.assertNotEqual(
            self.parent_token,
            derive_canary_token(KEY, self.child_turn),
        )
        arguments = json.dumps({"payload": f"see {self.parent_token}"})
        self.assertTrue(self.gate.hit(arguments, self.parent_turn))
        # Plain hit() scans the own turn token only: the parent-seeded canary
        # is invisible here.  Ancestor visibility is semantics-C's hit_multi,
        # covered by test_t7b_ancestor_seed_scan_hits_parent_canary below and
        # wired end-to-end in tests/test_j2_semantics_c.py.
        self.assertFalse(self.gate.hit(arguments, self.child_turn))

    def test_t7b_ancestor_seed_scan_hits_parent_canary(self) -> None:
        """Semantics-C (implemented 2026-09-06): the child's scan set includes
        deterministically derived ancestor tokens, so a parent-seeded canary
        that reaches child action arguments now HITS as an ancestor seed."""
        arguments = json.dumps({"payload": f"see {self.parent_token}"})
        self.assertEqual(
            ("ancestor_turn", self.parent_turn),
            self.gate.hit_multi(arguments, self.child_turn, (self.parent_turn,)),
        )
        # Without the ancestor seed list the old boundary still holds.
        self.assertIsNone(self.gate.hit_multi(arguments, self.child_turn, ()))

    def test_child_turn_scan_uses_its_own_exact_token(self) -> None:
        child_token = derive_canary_token(KEY, self.child_turn)
        arguments = json.dumps({"payload": f"see {child_token}"})
        self.assertTrue(self.gate.hit(arguments, self.child_turn))
        self.assertFalse(self.gate.hit(arguments, self.parent_turn))


class T7CMailboxSchemaHasNoTrustMarking(unittest.TestCase):
    def test_message_document_carries_no_trust_field(self) -> None:
        record = MessageRecord(
            agent_id=uuid4(),
            message_id=uuid4(),
            sequence=1,
            from_agent_id=uuid4(),
            kind=MessageKind.TASK,
            body_ref=None,
            idempotency_key="t7c-probe",
            status=MessageStatus.QUEUED,
            version=1,
        )
        document = record.to_document()
        joined_keys = " ".join(document.keys()).lower()
        self.assertNotIn("trust", joined_keys)
        self.assertNotIn("untrusted", joined_keys)
        self.assertNotIn("taint", joined_keys)


if __name__ == "__main__":
    unittest.main()
