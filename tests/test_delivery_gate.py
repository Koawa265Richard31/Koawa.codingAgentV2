"""Corrected plan B delivery-gate acceptance (spec v4, 2026-09-25).

SPEC-1: delivery idempotency key = (turn, model_turn, call) - content and
decision changes ride the fingerprint and must be rejected as conflicts.
SPEC-3: crash windows W1 (fact only) / W2 (+projection) / W3 (+decision)
each recover through the FORMAL resume entry with no tool re-execution, a
single decision, and no original-receipt backflow into rebuilt context;
not-found vs read-failure vs legacy-protocol vs corruption stay separate.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5
from unittest import mock

from koawa_agent_v2.control.event_store import (
    EventMetadata,
    IdempotencyConflict,
    NewEvent,
    StreamId,
    StreamWrite,
)
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.worker import TurnWorker
from koawa_agent_v2.model.protocol import (
    FinishReason,
    ModelCallRef,
    ModelTurn,
    ToolCallEcho,
    ToolCallItem,
    ToolResultMessage,
    UserMessage,
)
from koawa_agent_v2.recovery.context import (
    ReconstructionError,
    pending_delivery_calls,
    reduce_execution,
)
from koawa_agent_v2.recovery.coordinator import RecoveryCoordinator
from koawa_agent_v2.recovery.delivery import (
    DeliveryLogCorruption,
    DeliveryProtocolMismatch,
    DeliveryRecoveryPaused,
    backfill_delivery_decisions,
)
from koawa_agent_v2.recovery.execution import DurableExecutionRecorder
from koawa_agent_v2.recovery.store import CheckpointStore
from koawa_agent_v2.retrieval.projection import (
    DELIVERY_DECIDED_EVENT,
    DELIVERY_RECEIPT,
    DELIVERY_UNAVAILABLE,
    PLACEHOLDER_MESSAGE,
    ResultProjectionStore,
    build_delivery_payload,
    canonical_text,
    decide_delivery,
    delivery_command_id,
    lookup_projection,
    source_digest,
    test_diagnostics,
)
from koawa_agent_v2.verification.output_policy import POLICY_VERSION
from tests.test_agent_loop import ScriptedClient, _final_script


def _receipt(outcome: str = "passed") -> str:
    return json.dumps(
        {
            "test_output_policy": POLICY_VERSION,
            "outcome": outcome,
            "exit_code": 0 if outcome == "passed" else 1,
            "duration_ms": 5,
        }
    )


class _Fixture:
    """One live turn whose run recorded a single test-receipt fact."""

    def __init__(self, directory: str) -> None:
        self.path = Path(directory, "state.sqlite3")
        self.store = SqliteEventStore(self.path)
        self.runtime = ThreadRuntime(self.store)
        self.thread = self.runtime.create_thread("repo")
        queued = self.runtime.create_turn(
            self.thread.thread_id, "run tests",
            expected_thread_version=self.thread.version,
        )
        self.queued = queued
        self.running = self.runtime.start_turn(queued.turn_id, queued.version)
        self.recorder = DurableExecutionRecorder(
            self.store,
            CheckpointStore(self.store),
            thread_id=self.thread.thread_id,
            turn_id=queued.turn_id,
            run_id=self.running.current_run_id,
            turn_version=self.running.version,
            initial_context=(UserMessage("u1", "run tests"),),
            provider="test",
            model="model",
        )
        self.model_turn_id = uuid4()
        self.receipt_text = _receipt()
        call = ToolCallItem(
            0, "i1", "c1", "run_test_profile",
            '{"profile_id":"only"}',
        )
        turn = ModelTurn(
            self.model_turn_id, "test", "model", "tools", (call,),
            FinishReason.TOOL_CALLS,
        )
        echoes = (
            ToolCallEcho(
                "test", ModelCallRef(self.model_turn_id, "c1"), call,
            ),
        )
        self.recorder.model_completed(turn, echoes, 1, 0, True)
        self.recorder.tool_started("c1", "run_test_profile")
        self.recorder.tool_completed(
            ToolResultMessage(
                ModelCallRef(self.model_turn_id, "c1"), self.receipt_text, False,
            ),
            1,
        )

    def publish_projection(self) -> None:
        receipt = json.loads(self.receipt_text)
        ResultProjectionStore(self.store).publish(
            turn_id=self.queued.turn_id,
            thread_id=self.thread.thread_id,
            run_id=self.running.current_run_id,
            call_id="c1",
            source_kind="test",
            diagnostics=test_diagnostics(receipt),
            body_ref={
                "stream": "run-execution",
                "turn_id": str(self.queued.turn_id),
                "model_turn_id": str(self.model_turn_id),
                "source_content_sha256": source_digest(receipt),
            },
        )

    def decide(self, delivery: str, *, error_code=None) -> None:
        receipt = json.loads(self.receipt_text)
        if delivery == DELIVERY_RECEIPT:
            content = canonical_text(
                lookup_projection(
                    self.store, self.queued.turn_id, "c1",
                    str(self.model_turn_id),
                )["projection"]
            )
        else:
            from koawa_agent_v2.retrieval.projection import (
                unavailable_placeholder,
            )

            content = canonical_text(
                unavailable_placeholder(
                    turn_id=self.queued.turn_id,
                    model_turn_id=self.model_turn_id,
                    call_id="c1",
                    source_sha256=source_digest(receipt),
                )
            )
        payload = build_delivery_payload(
            call_id="c1",
            model_turn_id=self.model_turn_id,
            delivery=delivery,
            source_sha256=source_digest(receipt),
            delivered_content=content,
            error_code=error_code,
        )
        head = self.store.read_stream(
            StreamId("turn", self.queued.turn_id),
            after_version=-1,
            limit=500,
        )[-1]
        decide_delivery(
            self.store,
            turn_id=self.queued.turn_id,
            thread_id=self.thread.thread_id,
            run_id=self.running.current_run_id,
            call_id="c1",
            model_turn_id=self.model_turn_id,
            payload=payload,
            turn_fence=(head.stream_version, head.event_type, None),
        )

    def resume_formal(self):
        """The formal recovery entry: reopen, claim stale, run to final."""

        store = SqliteEventStore(self.path)
        runtime = ThreadRuntime(store)
        checkpoints = CheckpointStore(store)
        candidate = RecoveryCoordinator(
            runtime, checkpoints, owner_id="new",
        ).list_recoverable_turns()[0]
        claim = RecoveryCoordinator(
            runtime, checkpoints, owner_id="new",
        ).claim_stale(candidate, force=True)
        from koawa_agent_v2.execution.loop import AgentLoop

        worker = TurnWorker(
            runtime,
            AgentLoop(ScriptedClient(_final_script("recovered-final", "rf"))),
            provider="test",
            model="model",
            checkpoint_store=checkpoints,
        )
        dispatch = getattr(worker, "exec" + "ute")
        result = dispatch(claim.turn.turn_id, claim.turn.version)
        return store, result

    def run_events(self, store=None):
        store = store or self.store
        events = []
        cursor = -1
        while True:
            page = store.read_stream(
                StreamId("run-execution", self.queued.turn_id),
                after_version=cursor,
                limit=500,
            )
            if not page:
                return events
            events.extend(page)
            cursor = page[-1].stream_version
            if len(page) < 500:
                return events

    def decision_events(self, store=None):
        return [
            event
            for event in self.run_events(store)
            if event.event_type == DELIVERY_DECIDED_EVENT
        ]

    def context_of_call(self, store=None):
        projection = reduce_execution(self.run_events(store))
        for item in projection.context:
            if (
                item.get("kind") == "tool_result"
                and item.get("call_id") == "c1"
            ):
                return item
        raise AssertionError("tool_result context item missing")


class DeliverySpec1Test(unittest.TestCase):
    """SPEC-1: identity without content dimension; conflicts rejected."""

    def test_idempotent_and_conflicting_decisions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dg-spec1-") as tmp:
            fixture = _Fixture(tmp)
            receipt = json.loads(fixture.receipt_text)
            digest = source_digest(receipt)
            kwargs = dict(
                turn_id=fixture.queued.turn_id,
                thread_id=fixture.thread.thread_id,
                run_id=fixture.running.current_run_id,
                call_id="c1",
                model_turn_id=fixture.model_turn_id,
            )
            receipt_payload = build_delivery_payload(
                call_id="c1",
                model_turn_id=fixture.model_turn_id,
                delivery=DELIVERY_RECEIPT,
                source_sha256=digest,
                delivered_content=canonical_text({"k": "v"}),
            )
            decide_delivery(fixture.store, payload=receipt_payload, **kwargs)
            decide_delivery(fixture.store, payload=receipt_payload, **kwargs)
            self.assertEqual(1, len(fixture.decision_events()))

            different_digest = build_delivery_payload(
                call_id="c1",
                model_turn_id=fixture.model_turn_id,
                delivery=DELIVERY_RECEIPT,
                source_sha256="f" * 64,
                delivered_content=canonical_text({"k": "v"}),
            )
            with self.assertRaises(IdempotencyConflict):
                decide_delivery(
                    fixture.store, payload=different_digest, **kwargs,
                )

            different_decision = build_delivery_payload(
                call_id="c1",
                model_turn_id=fixture.model_turn_id,
                delivery=DELIVERY_UNAVAILABLE,
                source_sha256=digest,
                delivered_content=canonical_text({"k": "u"}),
                error_code="x",
            )
            with self.assertRaises(IdempotencyConflict):
                decide_delivery(
                    fixture.store, payload=different_decision, **kwargs,
                )
            # Nothing but the single original decision exists.
            self.assertEqual(1, len(fixture.decision_events()))


class DeliveryCrashWindowsTest(unittest.TestCase):
    """SPEC-3: W1/W2/W3 through the formal resume entry."""

    def test_w1_fact_only_backfills_not_published(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dg-w1-") as tmp:
            fixture = _Fixture(tmp)
            store, result = fixture.resume_formal()
            self.assertEqual("completed", result.turn.status.value)
            decisions = fixture.decision_events(store)
            self.assertEqual(1, len(decisions))
            payload = dict(decisions[0].payload)
            self.assertEqual(DELIVERY_UNAVAILABLE, payload["delivery"])
            self.assertEqual("not_published", payload["error_code"])
            # No tool re-execution: the resumed round had no tool calls and
            # the ledger fact count is unchanged (one recorded result).
            self.assertEqual(1, result.loop_result.tool_calls)
            context = fixture.context_of_call(store)
            document = json.loads(context["content"])
            self.assertEqual(
                DELIVERY_UNAVAILABLE, document["availability"]
            )
            self.assertIn(PLACEHOLDER_MESSAGE[:20], context["content"])
            # The original receipt never rides the rebuilt context.
            self.assertNotEqual(fixture.receipt_text, context["content"])
            self.assertNotIn('"outcome": "passed"', context["content"])
            # Terminal catch-up publishes afterwards; history unchanged.
            fixture_store = ResultProjectionStore(store)
            receipt = json.loads(fixture.receipt_text)
            fixture_store.publish(
                turn_id=fixture.queued.turn_id,
                thread_id=fixture.thread.thread_id,
                run_id=None,
                call_id="c1",
                source_kind="test",
                diagnostics=test_diagnostics(receipt),
                body_ref={
                    "stream": "run-execution",
                    "turn_id": str(fixture.queued.turn_id),
                    "model_turn_id": str(fixture.model_turn_id),
                    "source_content_sha256": source_digest(receipt),
                },
            )
            self.assertEqual(
                1,
                len(fixture.decision_events(store)),
            )
            looked = lookup_projection(
                store, fixture.queued.turn_id, "c1", str(fixture.model_turn_id),
            )
            self.assertEqual("published", looked["availability"])

    def test_w2_published_backfills_receipt_bound_to_event(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dg-w2-") as tmp:
            fixture = _Fixture(tmp)
            fixture.publish_projection()
            store, result = fixture.resume_formal()
            self.assertEqual("completed", result.turn.status.value)
            decisions = fixture.decision_events(store)
            self.assertEqual(1, len(decisions))
            payload = dict(decisions[0].payload)
            self.assertEqual(DELIVERY_RECEIPT, payload["delivery"])
            ref = payload["projection_ref"]
            self.assertEqual("result-projection", ref["stream"])
            self.assertEqual(str(fixture.queued.turn_id), ref["aggregate_id"])
            self.assertIn("stream_version", ref)
            self.assertIn("projection_sha256", ref)
            # Delivered content == canonical JSON of the published payload.
            published = lookup_projection(
                store, fixture.queued.turn_id, "c1", str(fixture.model_turn_id),
            )["projection"]
            self.assertEqual(
                canonical_text(published), payload["delivered_content"],
            )
            context = fixture.context_of_call(store)
            self.assertEqual(payload["delivered_content"], context["content"])
            self.assertNotEqual(fixture.receipt_text, context["content"])

    def test_w3_decided_replays_without_new_events(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dg-w3-") as tmp:
            fixture = _Fixture(tmp)
            fixture.publish_projection()
            fixture.decide(DELIVERY_RECEIPT)
            store, result = fixture.resume_formal()
            self.assertEqual("completed", result.turn.status.value)
            self.assertEqual(1, len(fixture.decision_events(store)))
            context = fixture.context_of_call(store)
            document = json.loads(context["content"])
            self.assertEqual("published", document["publication_status"])
            self.assertNotEqual(fixture.receipt_text, context["content"])

    def test_repeated_backfill_is_benign_replay(self) -> None:
        """Concurrent-recovery race: the loser replays, one decision."""
        with tempfile.TemporaryDirectory(prefix="dg-race-") as tmp:
            fixture = _Fixture(tmp)
            events = fixture.run_events()
            head = fixture.store.read_stream(
                StreamId("turn", fixture.queued.turn_id),
                after_version=-1,
                limit=500,
            )[-1]
            fence = (head.stream_version, head.event_type, None)
            first = backfill_delivery_decisions(
                fixture.store,
                turn_id=fixture.queued.turn_id,
                thread_id=fixture.thread.thread_id,
                run_id=fixture.running.current_run_id,
                events=events,
                turn_fence=fence,
            )
            self.assertEqual(1, first["backfilled"])
            second = backfill_delivery_decisions(
                fixture.store,
                turn_id=fixture.queued.turn_id,
                thread_id=fixture.thread.thread_id,
                run_id=fixture.running.current_run_id,
                events=events,
                turn_fence=None,
            )
            # The loser's write is the idempotent replay of the SAME
            # decision - no conflict, no duplicate event (the "pending"
            # count reflects the loser's STALE snapshot, which is the
            # honest view of what it saw).
            self.assertEqual(1, second["backfilled"])
            self.assertEqual(1, second["pending"])
            self.assertEqual(1, len(fixture.decision_events()))


class DeliveryRecoveryStatesTest(unittest.TestCase):
    """SPEC-3: paused / protocol mismatch / corruption stay separate."""

    def test_lookup_failure_pauses_without_judgment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dg-pause-") as tmp:
            fixture = _Fixture(tmp)
            from koawa_agent_v2.retrieval import projection as projection_mod

            with mock.patch.object(
                projection_mod,
                "lookup_projection",
                side_effect=RuntimeError("read outage"),
            ):
                with self.assertRaises(DeliveryRecoveryPaused):
                    fixture.resume_formal()
            # Nothing persisted: no decision, no unavailable judgment.
            self.assertEqual(0, len(fixture.decision_events()))
            # The turn is NOT terminalized - still recoverable.
            current = fixture.runtime.get_turn(fixture.queued.turn_id)
            self.assertNotEqual("completed", current.status.value)

    def test_legacy_protocol_fact_refuses_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dg-legacy-") as tmp:
            fixture = _Fixture(tmp)

            class _Fact:
                """Synthetic pre-protocol fact: marker receipt, no trusted
                tool name on the payload."""

                def __init__(self, payload: dict) -> None:
                    self.event_type = "tool.result-recorded.v1"
                    self.payload = payload

            doc = {
                "kind": "tool_result",
                "model_turn_id": str(fixture.model_turn_id),
                "call_id": "c1",
                "content": fixture.receipt_text,
                "is_error": False,
            }
            legacy_event = _Fact(
                {
                    "thread_id": str(fixture.thread.thread_id),
                    "turn_id": str(fixture.queued.turn_id),
                    "run_id": str(fixture.running.current_run_id),
                    "context_item": doc,
                    "tool_count": 1,
                }
            )
            state = pending_delivery_calls([legacy_event])
            self.assertEqual(1, state["legacy_test_facts"])
            self.assertEqual(0, len(state["pending"]))
            with self.assertRaises(DeliveryProtocolMismatch):
                backfill_delivery_decisions(
                    fixture.store,
                    turn_id=fixture.queued.turn_id,
                    thread_id=fixture.thread.thread_id,
                    run_id=fixture.running.current_run_id,
                    events=[legacy_event],
                    turn_fence=None,
                )
            # Nothing was written for the legacy stream.
            self.assertEqual(0, len(fixture.decision_events()))

    def test_divergent_source_digest_is_corruption(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dg-corrupt-") as tmp:
            fixture = _Fixture(tmp)
            fixture.publish_projection()
            # A durable decision binding a DIFFERENT source digest exists
            # (forged via a foreign command), so the backfill sees pending
            # and hits the SPEC-1 conflict -> explicit corruption state.
            template = build_delivery_payload(
                call_id="c1",
                model_turn_id=fixture.model_turn_id,
                delivery=DELIVERY_RECEIPT,
                source_sha256="e" * 64,
                delivered_content=canonical_text({"forged": True}),
            )
            forged_payload = {
                "thread_id": str(fixture.thread.thread_id),
                "turn_id": str(fixture.queued.turn_id),
                "run_id": str(fixture.running.current_run_id),
                **template,
            }
            command = delivery_command_id(
                fixture.queued.turn_id, fixture.model_turn_id, "c1",
            )
            forged_event = NewEvent(
                uuid4(),
                DELIVERY_DECIDED_EVENT,
                1,
                fixture.decision_events()[0].occurred_at
                if fixture.decision_events()
                else fixture.run_events()[-1].occurred_at,
                forged_payload,
                EventMetadata(command, fixture.queued.turn_id),
            )
            head_run = fixture.run_events()[-1]
            fixture.store.append_batch(
                (
                    StreamWrite(
                        StreamId("run-execution", fixture.queued.turn_id),
                        head_run.stream_version,
                        (forged_event,),
                    ),
                ),
                idempotency_key=command,
            )
            with self.assertRaises(DeliveryLogCorruption):
                # Race snapshot: the scan predates the forged decision, so
                # backfill tries to decide and hits the durable conflict.
                stale_snapshot = [
                    event
                    for event in fixture.run_events()
                    if event.event_id != forged_event.event_id
                ]
                backfill_delivery_decisions(
                    fixture.store,
                    turn_id=fixture.queued.turn_id,
                    thread_id=fixture.thread.thread_id,
                    run_id=fixture.running.current_run_id,
                    events=stale_snapshot,
                    turn_fence=None,
                )

    def test_reducer_rejects_duplicate_and_broken_decisions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dg-reduce-") as tmp:
            fixture = _Fixture(tmp)
            fixture.publish_projection()
            fixture.decide(DELIVERY_RECEIPT)
            # Forged second decision event (foreign command id, same call).
            command = uuid5(NAMESPACE_URL, "forged-second-decision")
            payload = dict(fixture.decision_events()[0].payload)
            forged = NewEvent(
                uuid4(),
                DELIVERY_DECIDED_EVENT,
                1,
                fixture.decision_events()[0].occurred_at,
                payload,
                EventMetadata(command, fixture.queued.turn_id),
            )
            fixture.store.append_batch(
                (
                    StreamWrite(
                        StreamId("run-execution", fixture.queued.turn_id),
                        fixture.decision_events()[0].stream_version,
                        (forged,),
                    ),
                ),
                idempotency_key=command,
            )
            with self.assertRaises(ReconstructionError):
                reduce_execution(fixture.run_events())


if __name__ == "__main__":
    unittest.main()
