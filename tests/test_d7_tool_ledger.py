from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

from koawa_agent_v2.control.event_store import WrongExpectedVersion
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import (
    AgentLoop,
    ToolExecutionContext,
    ToolExecutionResult,
)
from koawa_agent_v2.execution.worker import TurnWorker
from koawa_agent_v2.ledger import (
    DurableToolResult,
    IDEMPOTENT_WRITE_PROFILE,
    LedgerExecutor,
    LookupOutcome,
    LookupResult,
    MANUAL_WRITE_PROFILE,
    QUERYABLE_WRITE_PROFILE,
    READ_ONLY_PROFILE,
    ToolExecutionState,
    ToolLedgerConflict,
    ToolLedgerStore,
    ToolOutcomeBlocked,
    ToolRecoveryManager,
    logical_execution_id,
)
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem, ToolDefinition
from koawa_agent_v2.recovery import CheckpointStore


PROBE = ToolDefinition(
    "probe",
    "D7 deterministic test probe",
    '{"type":"object","properties":{"value":{"type":"integer"}}}',
)


class SimulatedProcessDeath(BaseException):
    """Bypass LedgerExecutor's ordinary-exception cleanup like a hard kill."""


class CountingExecutor:
    def __init__(self, result: ToolExecutionResult | None = None) -> None:
        self.result = result or ToolExecutionResult("probe-result")
        self.calls = 0
        self.execution_ids: list[UUID | None] = []

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (PROBE,)

    def execute(
        self,
        call: ToolCallItem,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        self.calls += 1
        self.execution_ids.append(context.execution_id)
        return self.result


class RaisingExecutor(CountingExecutor):
    def execute(
        self,
        call: ToolCallItem,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        self.calls += 1
        self.execution_ids.append(context.execution_id)
        raise RuntimeError("untrusted delegate detail")


class UnusedModelClient:
    def stream(self, request):
        raise AssertionError("model client must not run during construction")


class D7ToolLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "d7.sqlite3"
        self.event_store = SqliteEventStore(self.database)
        self.runtime = ThreadRuntime(self.event_store, actor="d7-test")
        self.ledger = ToolLedgerStore(self.event_store)

    def active_turn(self, label: str = "d7 ledger"):
        thread = self.runtime.create_thread(f"repo-{label}")
        queued = self.runtime.create_turn(
            thread.thread_id,
            label,
            expected_thread_version=thread.version,
        )
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        self.assertIsNotNone(running.current_run_id)
        return running

    def next_run(self, running):
        paused = self.runtime.pause_turn(
            running.turn_id,
            running.version,
            "simulate dead claimant",
            run_id=running.current_run_id,
        )
        queued = self.runtime.request_resume(running.turn_id, paused.version)
        resumed = self.runtime.start_turn(running.turn_id, queued.version)
        self.assertNotEqual(running.current_run_id, resumed.current_run_id)
        return resumed

    def prepare(
        self,
        running,
        *,
        model_turn_id: UUID | None = None,
        call_id: str = "call-1",
        arguments_json: str = '{"value":1}',
        profile=READ_ONLY_PROFILE,
    ):
        model_turn_id = model_turn_id or uuid4()
        record = self.ledger.prepare(
            turn_id=running.turn_id,
            turn_version=running.version,
            run_id=running.current_run_id,
            model_turn_id=model_turn_id,
            call_id=call_id,
            tool_name="probe",
            arguments_json=arguments_json,
            profile=profile,
        )
        return model_turn_id, record

    def context(
        self,
        running,
        model_turn_id: UUID,
        call_id: str = "call-1",
        *,
        recovered_call: bool = False,
    ):
        return ToolExecutionContext(
            running.current_run_id,
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, call_id),
            turn_id=running.turn_id,
            turn_version=running.version,
            recovered_call=recovered_call,
        )

    def call(self, call_id: str = "call-1", arguments: str = '{"value":1}'):
        return ToolCallItem(0, f"item-{call_id}", call_id, "probe", arguments)

    def test_execution_identity_ignores_run_id_and_conflicts_on_semantic_drift(self) -> None:
        first_run = self.active_turn("identity")
        model_turn_id, first = self.prepare(first_run)
        expected_id = logical_execution_id(
            first_run.turn_id,
            model_turn_id,
            "call-1",
        )
        self.assertEqual(expected_id, first.execution_id)

        second_run = self.next_run(first_run)
        _, retried = self.prepare(second_run, model_turn_id=model_turn_id)
        self.assertEqual(first.execution_id, retried.execution_id)
        self.assertEqual(ToolExecutionState.PREPARED, retried.state)

        with self.assertRaises(ToolLedgerConflict) as caught:
            self.prepare(
                second_run,
                model_turn_id=model_turn_id,
                arguments_json='{"value":2}',
            )
        self.assertEqual("tool_execution_identity_conflict", caught.exception.code)

    def test_same_prepare_is_idempotent_under_retry_and_concurrency(self) -> None:
        running = self.active_turn("concurrent-prepare")
        model_turn_id = uuid4()
        barrier = Barrier(2)

        def prepare_once():
            barrier.wait(timeout=5)
            return self.ledger.prepare(
                turn_id=running.turn_id,
                turn_version=running.version,
                run_id=running.current_run_id,
                model_turn_id=model_turn_id,
                call_id="same-call",
                tool_name="probe",
                arguments_json='{"value":1}',
                profile=READ_ONLY_PROFILE,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            records = tuple(pool.map(lambda _index: prepare_once(), range(2)))

        self.assertEqual(records[0].execution_id, records[1].execution_id)
        self.assertEqual(ToolExecutionState.PREPARED, records[0].state)
        retried = self.ledger.prepare(
            turn_id=running.turn_id,
            turn_version=running.version,
            run_id=running.current_run_id,
            model_turn_id=model_turn_id,
            call_id="same-call",
            tool_name="probe",
            arguments_json='{"value":1}',
            profile=READ_ONLY_PROFILE,
        )
        self.assertEqual(records[0], retried)

    def test_state_machine_persists_all_five_public_states(self) -> None:
        running = self.active_turn("states")

        _, prepared = self.prepare(running, call_id="prepared")
        self.assertEqual(ToolExecutionState.PREPARED, prepared.state)

        _, claimed = self.prepare(running, call_id="claimed")
        claimed = self.ledger.claim(
            claimed,
            turn_version=running.version,
            run_id=running.current_run_id,
        )
        self.assertEqual(ToolExecutionState.CLAIMED, claimed.state)
        self.assertEqual(1, claimed.claim_epoch)
        self.assertIsNotNone(claimed.claim_token)

        _, succeeded = self.prepare(running, call_id="succeeded")
        succeeded = self.ledger.claim(
            succeeded,
            turn_version=running.version,
            run_id=running.current_run_id,
        )
        succeeded = self.ledger.commit_result(
            succeeded,
            DurableToolResult("ok"),
        )
        self.assertEqual(ToolExecutionState.SUCCEEDED, succeeded.state)
        self.assertEqual("ok", succeeded.result.content)

        _, failed = self.prepare(running, call_id="failed")
        failed = self.ledger.claim(
            failed,
            turn_version=running.version,
            run_id=running.current_run_id,
        )
        failed = self.ledger.commit_result(
            failed,
            DurableToolResult("stable-tool-error", is_error=True),
        )
        self.assertEqual(ToolExecutionState.FAILED, failed.state)
        self.assertTrue(failed.result.is_error)

        _, unknown = self.prepare(
            running,
            call_id="unknown",
            profile=MANUAL_WRITE_PROFILE,
        )
        unknown = self.ledger.claim(
            unknown,
            turn_version=running.version,
            run_id=running.current_run_id,
        )
        unknown = self.ledger.mark_outcome_unknown(unknown, "test_unknown")
        self.assertEqual(ToolExecutionState.OUTCOME_UNKNOWN, unknown.state)
        self.assertEqual("test_unknown", unknown.unknown_reason)

    def test_claim_epoch_and_token_fence_stale_claimant(self) -> None:
        first_run = self.active_turn("claim-fence")
        model_turn_id, prepared = self.prepare(
            first_run,
            profile=IDEMPOTENT_WRITE_PROFILE,
        )
        first_claim = self.ledger.claim(
            prepared,
            turn_version=first_run.version,
            run_id=first_run.current_run_id,
        )

        second_run = self.next_run(first_run)
        current = self.ledger.load(first_claim.execution_id)
        second_claim = self.ledger.claim(
            current,
            turn_version=second_run.version,
            run_id=second_run.current_run_id,
        )
        self.assertEqual(2, second_claim.claim_epoch)
        self.assertNotEqual(first_claim.claim_token, second_claim.claim_token)
        self.assertNotEqual(first_claim.claimant_run_id, second_claim.claimant_run_id)

        with self.assertRaises(ToolLedgerConflict) as caught:
            self.ledger.commit_result(first_claim, DurableToolResult("late"))
        self.assertEqual("stale_tool_claim", caught.exception.code)
        settled = self.ledger.commit_result(
            second_claim,
            DurableToolResult(f"settled:{model_turn_id}"),
        )
        self.assertEqual(ToolExecutionState.SUCCEEDED, settled.state)

    def test_stale_claim_token_cannot_resolve_unknown(self) -> None:
        first_run = self.active_turn("unknown-token-fence")
        _, prepared = self.prepare(
            first_run,
            profile=IDEMPOTENT_WRITE_PROFILE,
        )
        stale_claim = self.ledger.claim(
            prepared,
            turn_version=first_run.version,
            run_id=first_run.current_run_id,
        )
        second_run = self.next_run(first_run)
        current = self.ledger.load(stale_claim.execution_id)
        current_claim = self.ledger.claim(
            current,
            turn_version=second_run.version,
            run_id=second_run.current_run_id,
        )
        unknown = self.ledger.mark_outcome_unknown(
            current_claim,
            "claim_outcome_unknown",
        )

        for resolve in (
            lambda: self.ledger.resolve_unknown_result(
                stale_claim,
                DurableToolResult("stale-result"),
            ),
            lambda: self.ledger.resolve_unknown_not_applied(stale_claim),
        ):
            with self.assertRaises(ToolLedgerConflict) as caught:
                resolve()
            self.assertEqual("stale_tool_claim", caught.exception.code)

        resolved = self.ledger.resolve_unknown_result(
            unknown,
            DurableToolResult("authoritative-result"),
        )
        self.assertEqual(ToolExecutionState.SUCCEEDED, resolved.state)

    def test_read_only_and_idempotent_claims_replay_after_process_death(self) -> None:
        for label, profile in (
            ("read-only", READ_ONLY_PROFILE),
            ("idempotent", IDEMPOTENT_WRITE_PROFILE),
        ):
            with self.subTest(profile=label):
                first_run = self.active_turn(f"replay-{label}")
                model_turn_id = uuid4()
                call = self.call()
                first_delegate = CountingExecutor()

                def die_after_claim(point, _record):
                    if point == "after_claim":
                        raise SimulatedProcessDeath()

                crashing = LedgerExecutor(
                    first_delegate,
                    self.ledger,
                    {"probe": profile},
                    fault_hook=die_after_claim,
                )
                with self.assertRaises(SimulatedProcessDeath):
                    crashing.execute(
                        call,
                        context=self.context(first_run, model_turn_id),
                    )
                self.assertEqual(0, first_delegate.calls)

                second_run = self.next_run(first_run)
                second_delegate = CountingExecutor()
                recovering = LedgerExecutor(
                    second_delegate,
                    self.ledger,
                    {"probe": profile},
                )
                result = recovering.execute(
                    call,
                    context=self.context(second_run, model_turn_id),
                )
                self.assertEqual("probe-result", result.content)
                self.assertEqual(1, second_delegate.calls)
                expected_execution_id = logical_execution_id(
                    second_run.turn_id,
                    model_turn_id,
                    call.call_id,
                )
                self.assertEqual(
                    [expected_execution_id],
                    second_delegate.execution_ids,
                )
                record = self.ledger.load_for_call(
                    second_run.turn_id,
                    model_turn_id,
                    call.call_id,
                )
                self.assertEqual(ToolExecutionState.SUCCEEDED, record.state)
                self.assertEqual(2, record.claim_epoch)

                bypass_probe = CountingExecutor()
                reuse = LedgerExecutor(
                    bypass_probe,
                    self.ledger,
                    {"probe": profile},
                ).execute(call, context=self.context(second_run, model_turn_id))
                self.assertEqual("probe-result", reuse.content)
                self.assertEqual(0, bypass_probe.calls)

    def test_retry_safe_delegate_exception_is_typed_and_keeps_claimed(self) -> None:
        for label, profile in (
            ("read-only", READ_ONLY_PROFILE),
            ("idempotent", IDEMPOTENT_WRITE_PROFILE),
        ):
            with self.subTest(profile=label):
                running = self.active_turn(f"ordinary-error-{label}")
                model_turn_id = uuid4()
                delegate = RaisingExecutor()
                executor = LedgerExecutor(
                    delegate,
                    self.ledger,
                    {"probe": profile},
                )

                with self.assertRaises(ToolOutcomeBlocked) as caught:
                    executor.execute(
                        self.call(),
                        context=self.context(running, model_turn_id),
                    )
                self.assertEqual("tool_retry_required", caught.exception.code)
                self.assertTrue(caught.exception.recovery_blocked)
                self.assertEqual(1, delegate.calls)
                record = self.ledger.load_for_call(
                    running.turn_id,
                    model_turn_id,
                    "call-1",
                )
                self.assertEqual(ToolExecutionState.CLAIMED, record.state)
                self.assertEqual(1, record.claim_epoch)

    def test_queryable_non_idempotent_recovery_applied_not_applied_and_unknown(self) -> None:
        cases = (
            (
                "applied",
                LookupResult(
                    LookupOutcome.APPLIED,
                    DurableToolResult("authoritative-result"),
                ),
                True,
                ToolExecutionState.SUCCEEDED,
            ),
            (
                "not-applied",
                LookupResult(LookupOutcome.NOT_APPLIED),
                True,
                ToolExecutionState.PREPARED,
            ),
            (
                "unknown",
                LookupResult(LookupOutcome.UNKNOWN),
                False,
                ToolExecutionState.OUTCOME_UNKNOWN,
            ),
        )
        for label, lookup_result, safe, expected_state in cases:
            with self.subTest(outcome=label):
                running = self.active_turn(f"query-{label}")
                model_turn_id, prepared = self.prepare(
                    running,
                    call_id=f"call-{label}",
                    profile=QUERYABLE_WRITE_PROFILE,
                )
                claimed = self.ledger.claim(
                    prepared,
                    turn_version=running.version,
                    run_id=running.current_run_id,
                )
                manager = ToolRecoveryManager(
                    self.ledger,
                    {"probe": lambda _record, value=lookup_result: value},
                )
                pending = (
                    {
                        "model_turn_id": str(model_turn_id),
                        "call_id": f"call-{label}",
                    },
                )
                self.assertEqual(
                    safe,
                    manager.reconcile_pending(running.turn_id, pending),
                )
                recovered = self.ledger.load(claimed.execution_id)
                self.assertEqual(expected_state, recovered.state)
                if label == "applied":
                    self.assertEqual("authoritative-result", recovered.result.content)

    def test_queryable_unknown_converges_on_later_authoritative_lookup(self) -> None:
        running = self.active_turn("query-unknown-converges")
        model_turn_id, prepared = self.prepare(
            running,
            call_id="query-converges",
            profile=QUERYABLE_WRITE_PROFILE,
        )
        claimed = self.ledger.claim(
            prepared,
            turn_version=running.version,
            run_id=running.current_run_id,
        )
        pending = (
            {
                "model_turn_id": str(model_turn_id),
                "call_id": "query-converges",
            },
        )
        first = ToolRecoveryManager(
            self.ledger,
            {"probe": lambda _record: LookupResult(LookupOutcome.UNKNOWN)},
        )
        self.assertFalse(first.reconcile_pending(running.turn_id, pending))
        unknown = self.ledger.load(claimed.execution_id)
        self.assertEqual(ToolExecutionState.OUTCOME_UNKNOWN, unknown.state)

        second = ToolRecoveryManager(
            self.ledger,
            {
                "probe": lambda _record: LookupResult(
                    LookupOutcome.APPLIED,
                    DurableToolResult("eventually-authoritative"),
                )
            },
        )
        self.assertTrue(second.reconcile_pending(running.turn_id, pending))
        settled = self.ledger.load(claimed.execution_id)
        self.assertEqual(ToolExecutionState.SUCCEEDED, settled.state)
        self.assertEqual("eventually-authoritative", settled.result.content)

    def test_recovered_redacted_call_reuses_terminal_or_blocks_when_missing(self) -> None:
        running = self.active_turn("redacted-recovery")
        model_turn_id = uuid4()
        original = self.call(arguments='{"value":1}')
        initial_delegate = CountingExecutor(ToolExecutionResult("persisted-result"))
        initial = LedgerExecutor(
            initial_delegate,
            self.ledger,
            {"probe": READ_ONLY_PROFILE},
        )
        self.assertEqual(
            "persisted-result",
            initial.execute(
                original,
                context=self.context(running, model_turn_id),
            ).content,
        )
        self.assertEqual(1, initial_delegate.calls)

        recovered = self.call(arguments='{"value":"[REDACTED]"}')
        bypass_probe = CountingExecutor()
        reused = LedgerExecutor(
            bypass_probe,
            self.ledger,
            {"probe": READ_ONLY_PROFILE},
        ).execute(
            recovered,
            context=self.context(
                running,
                model_turn_id,
                recovered_call=True,
            ),
        )
        self.assertEqual("persisted-result", reused.content)
        self.assertEqual(0, bypass_probe.calls)

        missing_model_turn_id = uuid4()
        missing = self.call(
            call_id="missing-call",
            arguments='{"value":"[REDACTED]"}',
        )
        with self.assertRaises(ToolOutcomeBlocked) as caught:
            LedgerExecutor(
                bypass_probe,
                self.ledger,
                {"probe": READ_ONLY_PROFILE},
            ).execute(
                missing,
                context=self.context(
                    running,
                    missing_model_turn_id,
                    call_id="missing-call",
                    recovered_call=True,
                ),
            )
        self.assertEqual(
            "recovered_tool_arguments_unavailable",
            caught.exception.code,
        )
        self.assertEqual(0, bypass_probe.calls)
        self.assertIsNone(
            self.ledger.load_for_call(
                running.turn_id,
                missing_model_turn_id,
                "missing-call",
            )
        )

    def test_durable_worker_rejects_non_ledger_tool_executor(self) -> None:
        loop = AgentLoop(
            UnusedModelClient(),
            tool_executor=CountingExecutor(),
        )
        checkpoints = CheckpointStore(self.event_store)
        with self.assertRaisesRegex(ValueError, "durable tool ledger required"):
            TurnWorker(
                self.runtime,
                loop,
                provider="test-provider",
                model="test-model",
                checkpoint_store=checkpoints,
            )

    def test_cancel_and_claim_race_is_decided_by_turn_cas(self) -> None:
        cancel_first = self.active_turn("cancel-first")
        model_turn_id = uuid4()
        self.runtime.cancel_turn(
            cancel_first.turn_id,
            "operator cancelled",
            expected_version=cancel_first.version,
        )
        with self.assertRaises(WrongExpectedVersion):
            self.prepare(cancel_first, model_turn_id=model_turn_id)
        self.assertIsNone(
            self.ledger.load_for_call(cancel_first.turn_id, model_turn_id, "call-1")
        )

        claim_first = self.active_turn("claim-first")
        _, prepared = self.prepare(
            claim_first,
            profile=IDEMPOTENT_WRITE_PROFILE,
        )
        claimed = self.ledger.claim(
            prepared,
            turn_version=claim_first.version,
            run_id=claim_first.current_run_id,
        )
        cancelled = self.runtime.cancel_turn(
            claim_first.turn_id,
            "operator cancelled after claim",
            expected_version=claim_first.version,
        )
        self.assertEqual("paused", cancelled.status.value)
        self.assertEqual(
            "outcome_unknown",
            self.runtime.get_run(claim_first.current_run_id).status.value,
        )
        settled = self.ledger.commit_result(
            claimed,
            DurableToolResult("already-claimed-operation-settled"),
        )
        self.assertEqual(ToolExecutionState.SUCCEEDED, settled.state)
        queued = self.runtime.resolve_runtime_outcome(
            cancelled.turn_id,
            expected_version=cancelled.version,
            run_id=claim_first.current_run_id,
            evidence_kind="effect_reconciliation",
            evidence_digest="a" * 64,
            reconciler="test-recovery",
        )
        cancelled = self.runtime.cancel_turn(
            queued.turn_id,
            "operator cancelled after reconciliation",
            expected_version=queued.version,
        )
        self.assertEqual("cancelled", cancelled.status.value)


if __name__ == "__main__":
    unittest.main()
