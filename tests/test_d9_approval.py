from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

from koawa_agent_v2.approval_service import (
    ApprovalError,
    ApprovalService,
    ApprovalStatus,
    ApprovalWaiting,
)
from koawa_agent_v2.control.event_store import StreamId, WrongExpectedVersion
from koawa_agent_v2.control.models import InvalidTransition, TurnStatus
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.ledger import (
    READ_ONLY_PROFILE,
    ToolExecutionState,
    ToolLedgerStore,
    ToolOutcomeBlocked,
)
from koawa_agent_v2.model.protocol import ModelCallRef
from koawa_agent_v2.policy import (
    ActionKind,
    Decision,
    PolicyVerdict,
    Principal,
    ResolvedAction,
    ResolvedResource,
    SideEffectClass,
)


class D9DurableApprovalTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "d9-approval.sqlite3"
        self.event_store = SqliteEventStore(self.database)
        self.runtime = ThreadRuntime(self.event_store, actor="d9-test")
        self.ledger = ToolLedgerStore(self.event_store)
        self.service = self._new_service(self.event_store, limit=8)
        self.now = datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc)

    def _new_service(
        self,
        store: SqliteEventStore,
        *,
        limit: int,
    ) -> ApprovalService:
        return ApprovalService(
            store,
            ToolLedgerStore(store),
            budget_action_limits={"primary": limit, "other": limit},
            approval_ttl_seconds=30,
            clock=lambda: self.now,
        )

    def _action(
        self,
        *,
        principal_id: str = "primary",
        policy_version: str = "policy-v1",
        arguments: str = '{"secret":"TOP-SECRET","value":1}',
    ) -> ResolvedAction:
        return ResolvedAction(
            kind=ActionKind.BUILTIN_TOOL,
            tool_name="probe",
            canonical_arguments_json=arguments,
            principal=Principal(principal_id, ("tool:probe",)),
            side_effect_class=SideEffectClass.READ_ONLY,
            sandbox_profile_id="locked-down",
            policy_version=policy_version,
        )

    def _verdict(
        self,
        action: ResolvedAction,
        decision: Decision = Decision.ASK,
        *,
        digest: str | None = None,
        policy_version: str | None = None,
    ) -> PolicyVerdict:
        code = {
            Decision.ALLOW: "allowed",
            Decision.DENY: "rule_denied",
            Decision.ASK: "approval_required",
        }[decision]
        return PolicyVerdict(
            decision,
            code,
            policy_version or action.policy_version,
            digest or action.action_digest,
        )

    def _running_turn(self, label: str):
        thread = self.runtime.create_thread(f"repo-{label}")
        queued = self.runtime.create_turn(
            thread.thread_id,
            label,
            expected_thread_version=thread.version,
        )
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        self.assertIsNotNone(running.current_run_id)
        return running

    def _bundle(
        self,
        label: str,
        *,
        call_id: str = "call-1",
        action: ResolvedAction | None = None,
    ):
        running = self._running_turn(label)
        model_turn_id = uuid4()
        selected = action or self._action()
        record = self.ledger.prepare(
            turn_id=running.turn_id,
            turn_version=running.version,
            run_id=running.current_run_id,
            model_turn_id=model_turn_id,
            call_id=call_id,
            tool_name="probe",
            arguments_json=selected.canonical_arguments_json,
            profile=READ_ONLY_PROFILE,
        )
        context = ToolExecutionContext(
            running.current_run_id,
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, call_id),
            turn_id=running.turn_id,
            turn_version=running.version,
            execution_id=record.execution_id,
        )
        return running, record, selected, self._verdict(selected), context

    def _request(self, label: str):
        running, record, action, verdict, context = self._bundle(label)
        with self.assertRaises(ApprovalWaiting) as raised:
            self.service.require_grant(
                record,
                action,
                verdict,
                context=context,
                prompt="Allow the exact probe action?",
                now=self.now,
            )
        self.assertEqual("approval_waiting", raised.exception.code)
        pending = self.service.load(record.execution_id)
        self.assertIsNotNone(pending)
        self.assertEqual(ApprovalStatus.PENDING, pending.status)
        waiting = self.runtime.get_turn(running.turn_id)
        self.assertEqual(TurnStatus.WAITING_FOR_APPROVAL, waiting.status)
        return waiting, record, action, verdict, context, pending

    def _resolve_and_restart(self, label: str):
        waiting, record, action, verdict, old_context, pending = self._request(label)
        granted = self.service.resolve(
            pending,
            True,
            expected_approval_version=pending.version,
            expected_turn_version=waiting.version,
            interrupt_id=pending.interrupt_id,
            approver_principal_id="operator",
            now=self.now + timedelta(seconds=1),
        )
        queued = self.runtime.get_turn(waiting.turn_id)
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        context = ToolExecutionContext(
            running.current_run_id,
            record.model_turn_id,
            old_context.model_round,
            old_context.call_ref,
            turn_id=running.turn_id,
            turn_version=running.version,
            execution_id=record.execution_id,
            recovered_call=True,
        )
        return running, record, action, verdict, context, granted

    def _events(self, category: str, aggregate_id: UUID):
        return self.event_store.read_stream(
            StreamId(category, aggregate_id), after_version=-1, limit=500
        )

    def _global_events(self):
        events = []
        cursor = 0
        while True:
            page = self.event_store.read_all(after_position=cursor, limit=500)
            events.extend(page)
            if len(page) < 500:
                return tuple(events)
            cursor = page[-1].global_position

    def test_request_and_turn_wait_are_one_safe_commit_and_restart_loads(self) -> None:
        waiting, record, action, _, _, pending = self._request("request")

        approval_event = self._events("approval", record.execution_id)[-1]
        turn_event = self._events("turn", waiting.turn_id)[-1]
        self.assertEqual("approval.requested.v1", approval_event.event_type)
        self.assertEqual("turn.waiting-for-approval.v1", turn_event.event_type)
        self.assertEqual(approval_event.commit_id, turn_event.commit_id)
        self.assertEqual(2, approval_event.commit_size)
        self.assertEqual(str(pending.request_id), turn_event.payload["approval_request_id"])
        self.assertEqual(action.action_digest, approval_event.payload["action_digest"])

        persisted = json.dumps(dict(approval_event.payload), sort_keys=True)
        self.assertNotIn("TOP-SECRET", persisted)
        self.assertNotIn('"secret"', persisted)
        self.assertNotIn("canonical_arguments_json", persisted)

        restarted_store = SqliteEventStore(self.database)
        restarted = self._new_service(restarted_store, limit=8)
        self.assertEqual(pending, restarted.load(record.execution_id))

    def test_production_clock_rejects_per_command_time_override(self) -> None:
        _, record, action, verdict, context = self._bundle("clock-override")
        production_clock = ApprovalService(
            self.event_store,
            self.ledger,
            budget_action_limits={"primary": 8, "other": 8},
            approval_ttl_seconds=30,
        )

        with self.assertRaises(ApprovalError) as raised:
            production_clock.require_grant(
                record,
                action,
                verdict,
                context=context,
                prompt="Must use the trusted production clock",
                now=self.now,
            )

        self.assertEqual("untrusted_approval_time_override", raised.exception.code)
        self.assertIsNone(production_clock.load(record.execution_id))

    def test_durable_pending_rejects_legacy_boolean_resume(self) -> None:
        waiting, _, _, _, _, pending = self._request("legacy-bool")
        with self.assertRaisesRegex(
            InvalidTransition,
            "durable approval interrupts must be resolved",
        ):
            self.runtime.request_resume(
                waiting.turn_id,
                waiting.version,
                interrupt_id=pending.interrupt_id,
                response=True,
            )

    def test_resolve_rejects_wrong_request_interrupt_type_and_versions(self) -> None:
        waiting, _, _, _, _, pending = self._request("bad-resolution")

        cases = (
            (
                "request",
                replace(pending, request_id=uuid4()),
                pending.interrupt_id,
                pending.version,
                waiting.version,
                "approval_request_stale",
            ),
            (
                "interrupt",
                pending,
                uuid4(),
                pending.version,
                waiting.version,
                "approval_interrupt_mismatch",
            ),
            (
                "approval-version",
                pending,
                pending.interrupt_id,
                pending.version + 1,
                waiting.version,
                "approval_version_stale",
            ),
            (
                "turn-version",
                pending,
                pending.interrupt_id,
                pending.version,
                waiting.version + 1,
                "approval_turn_version_stale",
            ),
        )
        for name, supplied, interrupt_id, approval_version, turn_version, code in cases:
            with self.subTest(name=name), self.assertRaises(ApprovalError) as raised:
                self.service.resolve(
                    supplied,
                    True,
                    expected_approval_version=approval_version,
                    expected_turn_version=turn_version,
                    interrupt_id=interrupt_id,
                    approver_principal_id="operator",
                    now=self.now + timedelta(seconds=1),
                )
            self.assertEqual(code, raised.exception.code)

        with self.assertRaises(TypeError):
            self.service.resolve(
                pending,
                1,
                expected_approval_version=pending.version,
                expected_turn_version=waiting.version,
                interrupt_id=pending.interrupt_id,
                approver_principal_id="operator",
            )

    def test_grant_deny_and_expiry_queue_recovery_in_same_commit(self) -> None:
        cases = (
            ("grant", True, timedelta(seconds=1), ApprovalStatus.GRANTED, "granted"),
            ("deny", False, timedelta(seconds=1), ApprovalStatus.DENIED, "denied"),
            ("expire", True, timedelta(seconds=31), ApprovalStatus.EXPIRED, "denied"),
        )
        for label, approved, advance, status, decision in cases:
            with self.subTest(label=label):
                waiting, record, _, _, _, pending = self._request(label)
                resolved = self.service.resolve(
                    pending,
                    approved,
                    expected_approval_version=pending.version,
                    expected_turn_version=waiting.version,
                    interrupt_id=pending.interrupt_id,
                    approver_principal_id="operator",
                    now=self.now + advance,
                )
                queued = self.runtime.get_turn(waiting.turn_id)
                self.assertEqual(status, resolved.status)
                self.assertEqual(TurnStatus.QUEUED, queued.status)
                self.assertEqual(pending.request_id, queued.last_resume_approval_request_id)
                self.assertEqual(decision, queued.last_resume_approval_decision)
                self.assertIsNone(queued.last_resume_response)

                approval_event = self._events("approval", record.execution_id)[-1]
                turn_event = self._events("turn", waiting.turn_id)[-1]
                self.assertEqual(approval_event.commit_id, turn_event.commit_id)
                self.assertEqual(2, approval_event.commit_size)

    def test_duplicate_answer_is_rejected_without_second_resolution(self) -> None:
        waiting, record, _, _, _, pending = self._request("duplicate-answer")
        self.service.resolve(
            pending,
            True,
            expected_approval_version=pending.version,
            expected_turn_version=waiting.version,
            interrupt_id=pending.interrupt_id,
            approver_principal_id="operator",
            now=self.now + timedelta(seconds=1),
        )
        with self.assertRaises(ApprovalError) as raised:
            self.service.resolve(
                pending,
                True,
                expected_approval_version=pending.version,
                expected_turn_version=waiting.version,
                interrupt_id=pending.interrupt_id,
                approver_principal_id="operator",
                now=self.now + timedelta(seconds=1),
            )
        self.assertEqual("approval_version_stale", raised.exception.code)
        resolution_events = [
            event
            for event in self._events("approval", record.execution_id)
            if event.event_type
            in ("approval.granted.v1", "approval.denied.v1", "approval.expired.v1")
        ]
        self.assertEqual(1, len(resolution_events))

    def test_claim_rejects_wrong_principal_policy_and_digest(self) -> None:
        _, record, action, verdict, context, granted = self._resolve_and_restart(
            "wrong-authority"
        )
        wrong_principal = replace(
            action,
            principal=Principal("other", ("tool:probe",)),
        )
        wrong_policy = replace(action, policy_version="policy-v2")
        cases = (
            ("principal", wrong_principal, self._verdict(wrong_principal), "approval_action_mismatch"),
            ("policy", wrong_policy, self._verdict(wrong_policy), "approval_action_mismatch"),
            (
                "digest",
                action,
                self._verdict(action, digest="0" * 64),
                "policy_action_digest_mismatch",
            ),
        )
        for name, supplied_action, supplied_verdict, code in cases:
            with self.subTest(name=name), self.assertRaises(ApprovalError) as raised:
                self.service.claim(
                    record,
                    supplied_action,
                    supplied_verdict,
                    granted,
                    context=context,
                    now=self.now + timedelta(seconds=2),
                )
            self.assertEqual(code, raised.exception.code)

    def test_single_use_consume_budget_and_d7_claim_are_one_commit(self) -> None:
        _, record, action, verdict, context, granted = self._resolve_and_restart(
            "atomic-claim"
        )
        claimed = self.service.claim(
            record,
            action,
            verdict,
            granted,
            context=context,
            now=self.now + timedelta(seconds=2),
        )
        consumed = self.service.load(record.execution_id)
        self.assertEqual(ToolExecutionState.CLAIMED, claimed.state)
        self.assertEqual(ApprovalStatus.CONSUMED, consumed.status)
        self.assertEqual(context.run_id, consumed.consumed_run_id)
        self.assertEqual(claimed.claim_token, consumed.claim_token)

        approval_event = self._events("approval", record.execution_id)[-1]
        ledger_event = self._events("tool-execution", record.execution_id)[-1]
        budget_event = next(
            event
            for event in reversed(self._global_events())
            if event.event_type == "resource.budget-reserved.v1"
            and event.payload["execution_id"] == str(record.execution_id)
        )
        self.assertEqual(
            {approval_event.commit_id, ledger_event.commit_id, budget_event.commit_id},
            {approval_event.commit_id},
        )
        self.assertEqual(3, ledger_event.commit_size)

    def test_concurrent_claims_create_only_one_consumption_and_budget_event(self) -> None:
        for iteration in range(8):
            with self.subTest(iteration=iteration):
                _, record, action, verdict, context, granted = (
                    self._resolve_and_restart(f"concurrent-claim-{iteration}")
                )
                barrier = Barrier(2)

                def consume_once():
                    store = SqliteEventStore(self.database)
                    service = self._new_service(store, limit=8)
                    barrier.wait()
                    try:
                        return service.claim(
                            record,
                            action,
                            verdict,
                            granted,
                            context=context,
                            now=self.now + timedelta(seconds=2),
                        )
                    except ToolOutcomeBlocked as error:
                        return error

                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = tuple(pool.map(lambda _: consume_once(), range(2)))
                records = [
                    item
                    for item in results
                    if not isinstance(item, ToolOutcomeBlocked)
                ]
                self.assertGreaterEqual(len(records), 1)
                self.assertTrue(
                    all(item.state is ToolExecutionState.CLAIMED for item in records)
                )
                self.assertEqual(1, len({item.claim_token for item in records}))
                events = self._global_events()
                for event_type in (
                    "tool.execution-claimed.v1",
                    "approval.consumed.v1",
                    "resource.budget-reserved.v1",
                ):
                    matching = [
                        event
                        for event in events
                        if event.event_type == event_type
                        and event.payload.get("execution_id")
                        == str(record.execution_id)
                    ]
                    self.assertEqual(1, len(matching), event_type)

    def test_grant_is_expired_at_the_exact_deadline_before_claim(self) -> None:
        _, record, action, verdict, context, granted = self._resolve_and_restart(
            "exact-expiry"
        )

        with self.assertRaises(ApprovalError) as raised:
            self.service.claim(
                record,
                action,
                verdict,
                granted,
                context=context,
                now=granted.expires_at,
            )

        self.assertEqual("approval_expired", raised.exception.code)
        self.assertEqual(ToolExecutionState.PREPARED, self.ledger.load(record.execution_id).state)
        self.assertEqual(ApprovalStatus.GRANTED, self.service.load(record.execution_id).status)

    def test_cancel_first_prevents_claim_and_preserves_unconsumed_grant(self) -> None:
        running, record, action, verdict, context, granted = self._resolve_and_restart(
            "cancel-first"
        )
        self.runtime.cancel_turn(
            running.turn_id,
            "operator cancellation",
            expected_version=running.version,
        )
        with self.assertRaises(WrongExpectedVersion):
            self.service.claim(
                record,
                action,
                verdict,
                granted,
                context=context,
                now=self.now + timedelta(seconds=2),
            )
        self.assertEqual(ApprovalStatus.GRANTED, self.service.load(record.execution_id).status)
        self.assertEqual(ToolExecutionState.PREPARED, self.ledger.load(record.execution_id).state)

    def test_claim_first_survives_later_cancel_as_an_auditable_claim(self) -> None:
        running, record, action, verdict, context, granted = self._resolve_and_restart(
            "claim-first"
        )
        claimed = self.service.claim(
            record,
            action,
            verdict,
            granted,
            context=context,
            now=self.now + timedelta(seconds=2),
        )
        cancelled = self.runtime.cancel_turn(
            running.turn_id,
            "operator cancellation",
            expected_version=running.version,
        )
        self.assertEqual(TurnStatus.CANCELLED, cancelled.status)
        self.assertEqual(ToolExecutionState.CLAIMED, claimed.state)
        self.assertEqual(ApprovalStatus.CONSUMED, self.service.load(record.execution_id).status)

    def test_budget_exceeded_leaves_second_execution_prepared(self) -> None:
        service = self._new_service(self.event_store, limit=1)
        running, first, action, _, first_context = self._bundle("budget", call_id="one")
        allow = self._verdict(action, Decision.ALLOW)
        service.claim(first, action, allow, None, context=first_context, now=self.now)

        second = self.ledger.prepare(
            turn_id=running.turn_id,
            turn_version=running.version,
            run_id=running.current_run_id,
            model_turn_id=uuid4(),
            call_id="two",
            tool_name="probe",
            arguments_json=action.canonical_arguments_json,
            profile=READ_ONLY_PROFILE,
        )
        second_context = ToolExecutionContext(
            running.current_run_id,
            second.model_turn_id,
            1,
            ModelCallRef(second.model_turn_id, second.call_id),
            turn_id=running.turn_id,
            turn_version=running.version,
            execution_id=second.execution_id,
        )
        with self.assertRaises(ApprovalError) as raised:
            service.claim(
                second,
                action,
                allow,
                None,
                context=second_context,
                now=self.now,
            )
        self.assertEqual("resource_budget_exceeded", raised.exception.code)
        self.assertEqual(ToolExecutionState.PREPARED, self.ledger.load(second.execution_id).state)

    def test_action_drift_invalidates_grant_and_reasks(self) -> None:
        running, record, action, _, context, granted = self._resolve_and_restart(
            "drift"
        )
        drifted = replace(
            action,
            resources=(
                ResolvedResource(
                    "workspace_path",
                    "src/current.py",
                    "D:/repo/src/current.py",
                    "sha256:" + "2" * 64,
                ),
            ),
        )
        drifted_verdict = self._verdict(drifted)
        with self.assertRaises(ApprovalWaiting):
            self.service.require_grant(
                record,
                drifted,
                drifted_verdict,
                context=context,
                prompt="Allow the changed probe action?",
                now=self.now + timedelta(seconds=2),
            )
        replacement = self.service.load(record.execution_id)
        self.assertEqual(ApprovalStatus.PENDING, replacement.status)
        self.assertNotEqual(granted.request_id, replacement.request_id)
        self.assertNotEqual(granted.action_digest, replacement.action_digest)
        waiting = self.runtime.get_turn(running.turn_id)
        self.assertEqual(TurnStatus.WAITING_FOR_APPROVAL, waiting.status)
        self.assertEqual(replacement.request_id, waiting.pending_interrupt.approval_request_id)

        approval_events = self._events("approval", record.execution_id)
        expired, requested = approval_events[-2:]
        turn_event = self._events("turn", running.turn_id)[-1]
        self.assertEqual("approval.expired.v1", expired.event_type)
        self.assertEqual("approval.requested.v1", requested.event_type)
        self.assertEqual({expired.commit_id, requested.commit_id, turn_event.commit_id}, {expired.commit_id})
        self.assertEqual(3, expired.commit_size)


if __name__ == "__main__":
    unittest.main()
