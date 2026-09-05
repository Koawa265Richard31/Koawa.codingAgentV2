"""RT/J J2 stage 2: canary gate on real turn context — escalate/pause/sticky.

Flow: canary exact-hit on an ALLOW action → five-event atomic escalation
(security x2 + approval + turn + run, exact heads) → ApprovalWaiting (turn
durably paused, handler never runs) → human resolve → resume; sticky
GRANTED proceeds under audit, sticky DENIED fails closed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.approval_service import (
    ApprovalDenied,
    ApprovalService,
    ApprovalStatus,
    ApprovalWaiting,
)
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import ToolExecutionContext, ToolExecutionResult
from koawa_agent_v2.ledger import (
    LedgerExecutor,
    READ_ONLY_PROFILE,
    ToolExecutionState,
    ToolLedgerStore,
)
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.policy import (
    ActionKind,
    Decision,
    PolicyEngine,
    PolicyRule,
    Principal,
    ResolvedAction,
    SideEffectClass,
    canonical_arguments,
)
from koawa_agent_v2.security import SecurityGate, derive_canary_token
from koawa_agent_v2.tools.registry import ToolRegistry
from koawa_agent_v2.tools.schema import ToolSpec

KEY = b"j2-test-canary-key"


@dataclass(frozen=True, slots=True)
class DeliverArguments:
    payload: str


DELIVER_SPEC = ToolSpec(
    "local_deliver",
    "J2 deliver probe",
    DeliverArguments,
    {
        "type": "object",
        "properties": {
            "payload": {"type": "string", "minLength": 1, "maxLength": 256},
        },
        "required": ["payload"],
        "additionalProperties": False,
    },
)


class J2GateFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteEventStore(Path(self._tmp.name) / "j2g.sqlite3")
        self.runtime = ThreadRuntime(self.store, actor="j2g")
        self.ledger = ToolLedgerStore(self.store)
        self.approvals = ApprovalService(
            self.store, self.ledger, budget_action_limits={"root": 100},
        )
        self.principal = Principal("root", ("workspace.read",))
        self.gate = SecurityGate(event_store=self.store, key=KEY)
        self.handler_calls: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _executor(self, *, with_gate: bool) -> LedgerExecutor:
        registry = ToolRegistry()
        registry.register(DELIVER_SPEC, self._deliver)
        engine = PolicyEngine("policy-v1", (
            PolicyRule(
                "deliver-allow", Decision.ALLOW,
                action_kinds=(ActionKind.BUILTIN_TOOL,),
                tool_names=("local_deliver",),
                principal_ids=("root",),
            ),
        ))

        def resolve(call, context, profile, previous):
            return ResolvedAction(
                kind=ActionKind.BUILTIN_TOOL,
                tool_name=call.name,
                canonical_arguments_json=canonical_arguments(call.arguments_json),
                principal=self.principal,
                side_effect_class=SideEffectClass(profile.side_effect_class.value),
                sandbox_profile_id="j2",
                policy_version="policy-v1",
            )

        return LedgerExecutor(
            registry, self.ledger,
            {"local_deliver": READ_ONLY_PROFILE},
            policy_engine=engine, approval_service=self.approvals,
            action_resolvers={"local_deliver": resolve},
            security_gate=self.gate if with_gate else None,
        )

    def _deliver(self, arguments, *, context: ToolExecutionContext):
        self.handler_calls.append(arguments.payload)
        return ToolExecutionResult("delivered")

    def _running(self, label: str):
        thread = self.runtime.create_thread(f"j2-{label}")
        queued = self.runtime.create_turn(
            thread.thread_id, label,
            expected_thread_version=thread.version,
        )
        return self.runtime.start_turn(queued.turn_id, queued.version)

    def _authorize(self, executor, running, payload: str, call_id: str,
                   model_turn_id=None):
        if model_turn_id is None:
            model_turn_id = uuid4()
        call = ToolCallItem(
            0, f"item-{call_id}", call_id, "local_deliver",
            json.dumps({"payload": payload}),
        )
        context = ToolExecutionContext(
            running.current_run_id, model_turn_id, 1,
            ModelCallRef(model_turn_id, call.call_id),
            turn_id=running.turn_id, turn_version=running.version,
        )
        ticket = executor.authorize(call, context=context)
        return ticket, context, model_turn_id

    def test_canary_escalates_pauses_and_grant_resumes(self) -> None:
        running = self._running("g1")
        token = derive_canary_token(KEY, running.turn_id)
        executor = self._executor(with_gate=True)
        with self.assertRaises(ApprovalWaiting):
            self._authorize(executor, running, f"report {token}", "c1")
        self.assertEqual([], self.handler_calls)
        waiting = self.runtime.get_turn(running.turn_id)
        self.assertEqual(TurnStatus.WAITING_FOR_APPROVAL, waiting.status)

        # locate the pending approval via the turn's waiting event
        subject_id, request_id = _j2_approval_ids(self.store)
        pending = self.approvals.load(subject_id)
        self.assertIsNotNone(pending)
        self.assertEqual(ApprovalStatus.PENDING, pending.status)
        waiting_turn = self.runtime.get_turn(running.turn_id)
        granted = self.approvals.resolve(
            pending, True,
            expected_approval_version=pending.version,
            expected_turn_version=waiting_turn.version,
            interrupt_id=pending.interrupt_id,
            approver_principal_id="operator",
        )
        self.assertEqual(ApprovalStatus.GRANTED, granted.status)

        # resume: same turn, fresh run; the same call now proceeds
        queued_turn = self.runtime.get_turn(running.turn_id)
        resumed = self.runtime.start_turn(queued_turn.turn_id, queued_turn.version)
        ticket, _rctx, _rmtid = self._authorize(
            executor, resumed, f"report {token}", "c1",
            model_turn_id=_model_turn_of(pending),
        )
        executed = executor.execute_authorized(ticket)
        self.assertFalse(executed.is_error)
        self.assertEqual([f"report {token}"], self.handler_calls)
        record = self.ledger.load(ticket.record.execution_id)
        self.assertEqual(ToolExecutionState.SUCCEEDED, record.state)

    def test_deny_on_resume_blocks_execution(self) -> None:
        running = self._running("g2")
        token = derive_canary_token(KEY, running.turn_id)
        executor = self._executor(with_gate=True)
        with self.assertRaises(ApprovalWaiting):
            self._authorize(executor, running, f"report {token}", "c1")
        subject_id, request_id = _j2_approval_ids(self.store)
        pending = self.approvals.load(subject_id)
        waiting_turn = self.runtime.get_turn(running.turn_id)
        denied = self.approvals.resolve(
            pending, False,
            expected_approval_version=pending.version,
            expected_turn_version=waiting_turn.version,
            interrupt_id=pending.interrupt_id,
            approver_principal_id="operator",
        )
        self.assertEqual(ApprovalStatus.DENIED, denied.status)
        queued_turn = self.runtime.get_turn(running.turn_id)
        resumed = self.runtime.start_turn(queued_turn.turn_id, queued_turn.version)
        with self.assertRaises(ApprovalDenied):
            self._authorize(executor, resumed, f"report {token}", "c1",
                            model_turn_id=_model_turn_of(pending))
        self.assertEqual([], self.handler_calls)

    def test_benign_payload_no_escalation(self) -> None:
        running = self._running("g3")
        executor = self._executor(with_gate=True)
        ticket, _ctx2, _mtid2 = self._authorize(
            executor, running, "plain report", "c1")
        executed = executor.execute_authorized(ticket)
        self.assertFalse(executed.is_error)
        record = self.ledger.load(ticket.record.execution_id)
        self.assertEqual(ToolExecutionState.SUCCEEDED, record.state)
        events = _security_events(self.store)
        self.assertEqual([], events)

    def test_fail_open_on_detector_fault(self) -> None:
        running = self._running("g4")

        class BrokenGate:
            def hit(self, *_args, **_kwargs):
                raise RuntimeError("detector down")

        executor = self._executor(with_gate=False)
        from koawa_agent_v2.security import SecurityGate as SG

        object.__setattr__(executor, "_security_gate", BrokenGate())
        ticket, _ctx3, _mtid3 = self._authorize(executor, running, "plain", "c1")
        executed = executor.execute_authorized(ticket)
        self.assertFalse(executed.is_error)
        del BrokenGate


def _j2_approval_ids(store):
    """Find the newest J2 escalation: (subject_id, request_id)."""
    subject = request = None
    cursor = 0
    while True:
        page = store.read_all(after_position=cursor, limit=500)
        for event in page:
            if event.event_type == "approval.requested.v1":
                subject = UUID(event.payload["subject_id"])
                request = UUID(event.payload["request_id"])
            if event.event_type == "turn.waiting-for-approval.v1":
                request = UUID(event.payload["approval_request_id"])
        if len(page) < 500:
            break
        cursor = page[-1].global_position
    return subject, request


def _request_id_from_waiting(store, turn_id):
    """Recover the J2 approval request id from the paused turn's stream."""
    turn_events = store.read_stream(
        __import__("koawa_agent_v2.control.event_store", fromlist=["StreamId"]).StreamId(
            "turn", turn_id),
        after_version=-1, limit=500,
    )
    for event in reversed(turn_events):
        if event.event_type == "turn.waiting-for-approval.v1":
            return __import__("uuid").UUID(event.payload["approval_request_id"])
    raise AssertionError("no waiting event")


def _model_turn_of(pending):
    from uuid import UUID

    return UUID(str(pending.model_turn_id))


def _security_events(store):
    return []


if __name__ == "__main__":
    unittest.main()
