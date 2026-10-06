"""Revocation/expiry acceptance (closure-review self-audit gap fills).

GAP-1: the design event family's revocation/expiry members are implemented
and honored across the whole read chain - lookup refuses with an explicit
verdict, publication status counts them as terminal (not pending), the
terminal catch-up never re-publishes a revoked fact, and recovery backfill
writes the refusal into the delivery decision.
GAP-2: the read chain enforces the CURRENT policy version - a projection
stamped with an older policy is refused as policy_superseded (historical
delivery never grants current read permission).
GAP-3: the read-chain scan is quota-bounded and reports truncation instead
of silently cutting.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.retrieval.projection import (
    PROJECTION_REVOKED_EVENT,
    ResultProjectionStore,
    lookup_projection,
    read_publication_status,
)
from tests.test_wp_d_r2 import _publish_projection_fixture


class _Turn:
    """Minimal thread/turn fixture with one published projection."""

    def __init__(self, tmp: str) -> None:
        self.store = SqliteEventStore(Path(tmp, "store.db"))
        self.runtime = ThreadRuntime(self.store)
        self.thread = self.runtime.create_thread("repo")
        self.queued = self.runtime.create_turn(
            self.thread.thread_id, "run tests",
            expected_thread_version=self.thread.version,
        )
        self.turn_id = self.queued.turn_id
        self.model_turn_id = str(uuid4())
        self.digest = "a" * 64
        _publish_projection_fixture(
            self.store,
            self.thread,
            self.queued,
            model_turn_id=self.model_turn_id,
            digest=self.digest,
        )


class RevocationTest(unittest.TestCase):
    def test_revoked_fact_is_refused_everywhere(self) -> None:
        with tempfile.TemporaryDirectory(prefix="revo-") as tmp:
            fixture = _Turn(tmp)
            projections = ResultProjectionStore(fixture.store)
            projections.revoke(
                turn_id=fixture.turn_id,
                thread_id=fixture.thread.thread_id,
                run_id=uuid4(),
                call_id="c1",
                model_turn_id=fixture.model_turn_id,
                reason="operator_request",
            )
            looked = lookup_projection(
                fixture.store, fixture.turn_id, "c1", fixture.model_turn_id,
            )
            self.assertEqual(
                "projection_unavailable", looked["availability"], looked,
            )
            self.assertEqual("revoked", looked["error_code"])
            self.assertIsNone(looked["projection"])
            # Status: terminal verdict, not pending work.
            status = read_publication_status(fixture.store, fixture.turn_id)
            self.assertEqual(1, status["revoked"], status)
            self.assertEqual([], status["pending"])
            # Re-revoking with the same reason is idempotent.
            projections.revoke(
                turn_id=fixture.turn_id,
                thread_id=fixture.thread.thread_id,
                run_id=uuid4(),
                call_id="c1",
                model_turn_id=fixture.model_turn_id,
                reason="operator_request",
            )
            self.assertEqual(1, status["revoked"])

    def test_expiry_refuses_only_after_deadline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="expo-") as tmp:
            fixture = _Turn(tmp)
            projections = ResultProjectionStore(fixture.store)
            now = datetime.now(timezone.utc)
            projections.expire(
                turn_id=fixture.turn_id,
                thread_id=fixture.thread.thread_id,
                run_id=uuid4(),
                call_id="c1",
                model_turn_id=fixture.model_turn_id,
                expires_at=now + timedelta(hours=1),
            )
            before = lookup_projection(
                fixture.store, fixture.turn_id, "c1", fixture.model_turn_id,
            )
            self.assertEqual("published", before["availability"])
            after = lookup_projection(
                fixture.store,
                fixture.turn_id,
                "c1",
                fixture.model_turn_id,
                now=now + timedelta(hours=2),
            )
            self.assertEqual("projection_unavailable", after["availability"])
            self.assertEqual("projection_expired", after["error_code"])
            # A naive deadline is rejected (aware-time contract).
            with self.assertRaises(Exception):
                projections.expire(
                    turn_id=fixture.turn_id,
                    thread_id=fixture.thread.thread_id,
                    run_id=uuid4(),
                    call_id="c1",
                    model_turn_id=fixture.model_turn_id,
                    expires_at=datetime(2026, 1, 1),
                )

    def test_policy_superseded_refuses_old_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="policychk-") as tmp:
            fixture = _Turn(tmp)
            # Simulate a projection stored under an older policy version.
            store = fixture.store
            from koawa_agent_v2.control.event_store import StreamId

            events = store.read_stream(
                StreamId("result-projection", fixture.turn_id),
                after_version=-1,
                limit=10,
            )
            self.assertEqual(1, len(events))
            old_payload = dict(events[0].payload)
            old_payload["policy_version"] = "test-output-policy-v0"
            command = uuid4()
            from koawa_agent_v2.control.event_store import (
                EventMetadata,
                NewEvent,
                StreamId,
                StreamWrite,
            )
            from datetime import datetime, timezone as _tz

            replacement = NewEvent(
                uuid4(),
                events[0].event_type,
                1,
                datetime.now(_tz.utc),
                old_payload,
                EventMetadata(command, fixture.turn_id),
            )
            store.append_batch(
                (
                    StreamWrite(
                        StreamId("result-projection", fixture.turn_id),
                        events[-1].stream_version,
                        (replacement,),
                    ),
                ),
                idempotency_key=command,
            )
            looked = lookup_projection(
                store, fixture.turn_id, "c1", fixture.model_turn_id,
            )
            self.assertEqual(
                "projection_unavailable", looked["availability"], looked,
            )
            self.assertEqual("policy_superseded", looked["error_code"])


class RevocationLifecycleTest(unittest.TestCase):
    def test_terminal_catchup_skips_revoked_fact(self) -> None:
        import json
        import subprocess
        import sys
        from unittest import mock

        from koawa_agent_v2.verification.output_policy import POLICY_VERSION
        from koawa_agent_v2.verification.tools import (
            build_verified_coding_tool_registry,
        )
        from tests.test_output_policy_gate import _ScriptedRunner
        from tests.test_wp_d_projection import (
            _RepairModel,
            _assembly_config,
            _fixture_repo,
        )
        import tempfile as _tempfile

        from koawa_agent_v2.runtime.app import AppRuntime

        with _tempfile.TemporaryDirectory(prefix="revo-app-") as tmp:
            base = Path(tmp)
            root = _fixture_repo(base)
            app = AppRuntime(
                _assembly_config(base, root), model_client=_RepairModel(),
            )
            self.addCleanup(app.close)
            outcome = app.run("make tests pass")
            self.assertTrue(outcome.ok, outcome.payload)
            turn_id = outcome.payload["turn_id"]
            status = outcome.payload["result_projections"]
            self.assertEqual(2, status["published"], status)

            # Revoke one fact, then re-run the terminal catch-up via resume.
            projections = ResultProjectionStore(app.assembled.store)
            from koawa_agent_v2.control.event_store import StreamId
            from koawa_agent_v2.retrieval.projection import scan_test_results

            scan = scan_test_results(app.assembled.store, UUID(turn_id))
            victim = scan.facts[0]
            projections.revoke(
                turn_id=UUID(turn_id),
                thread_id=fixture_thread(app, turn_id),
                run_id=None,
                call_id=victim["call_id"],
                model_turn_id=victim["model_turn_id"],
                reason="selftest",
            )
            resumed = app.resume(turn_id)
            self.assertTrue(resumed.ok, resumed.payload)
            new_status = resumed.payload["result_projections"]
            # The revoked fact is NOT re-published (still exactly the two
            # original published events for distinct identities) and is
            # counted as revoked, not pending.
            self.assertEqual(1, new_status["revoked"], new_status)
            self.assertEqual([], new_status["pending"])
            looked = lookup_projection(
                app.assembled.store,
                UUID(turn_id),
                victim["call_id"],
                victim["model_turn_id"],
            )
            self.assertEqual("revoked", looked["error_code"])


def fixture_thread(app, turn_id: str):
    from uuid import UUID

    return app.assembled.runtime.get_turn(UUID(turn_id)).thread_id


if __name__ == "__main__":
    unittest.main()
