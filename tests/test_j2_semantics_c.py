"""PSEC semantics-C wiring tests: canary key config, hit_multi, executor.

Three layers shipped for T7-b (multi-hop relay + non-action channels):
- runtime.config.resolve_canary_key: env-var-name config; None = gate
  inactive; configured-but-missing env var fails closed (canary_key_missing).
- SecurityGate.hit_multi: own-turn token first, then ancestor seeds in the
  caller-provided order (nearest ancestor first).
- LedgerExecutor._j2_check: an ancestor seed hit on an ALLOW action escalates
  through the same five-event batch as the own-turn J2 path, with
  signal_kind=ancestor_seed_exact and seed_source/ancestor_turn_id payload
  fields (legacy own-turn payloads are byte-identical to the J2 shape).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from koawa_agent_v2.approval_service import (
    ApprovalService,
    ApprovalStatus,
    ApprovalWaiting,
)
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import ToolExecutionContext, ToolExecutionResult
from koawa_agent_v2.ledger import (
    LedgerExecutor,
    READ_ONLY_PROFILE,
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
from koawa_agent_v2.runtime.config import RuntimeConfigError, resolve_canary_key
from koawa_agent_v2.security import SecurityGate, derive_canary_token
from koawa_agent_v2.security.state import ESCALATION_PENDING, SecurityStateStore
from koawa_agent_v2.tools.registry import ToolRegistry
from koawa_agent_v2.tools.schema import ToolSpec

KEY = b"semantics-c-canary-key"
_UNIQUE_ENV = "KOAWA_TEST_CANARY_KEY_SEMANTICS_C"


@dataclass(frozen=True, slots=True)
class DeliverArguments:
    payload: str


DELIVER_SPEC = ToolSpec(
    "local_deliver",
    "semantics-C deliver probe",
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


class ResolveCanaryKeyTest(unittest.TestCase):
    def test_absent_config_keeps_gate_inactive(self) -> None:
        config = SimpleNamespace(canary_key_env=None)
        self.assertIsNone(resolve_canary_key(config))

    def test_missing_env_var_fails_closed(self) -> None:
        os.environ.pop(_UNIQUE_ENV, None)
        config = SimpleNamespace(canary_key_env=_UNIQUE_ENV)
        with self.assertRaises(RuntimeConfigError) as raised:
            resolve_canary_key(config)
        self.assertEqual("canary_key_missing", raised.exception.code)

    def test_present_env_var_returns_key_bytes(self) -> None:
        try:
            os.environ[_UNIQUE_ENV] = "a-canary-key-of-sufficient-length"
            config = SimpleNamespace(canary_key_env=_UNIQUE_ENV)
            self.assertEqual(
                b"a-canary-key-of-sufficient-length", resolve_canary_key(config)
            )
        finally:
            os.environ.pop(_UNIQUE_ENV, None)

    def test_trivial_key_is_rejected(self) -> None:
        try:
            os.environ[_UNIQUE_ENV] = "short"
            config = SimpleNamespace(canary_key_env=_UNIQUE_ENV)
            with self.assertRaises(RuntimeConfigError) as raised:
                resolve_canary_key(config)
            self.assertEqual("canary_key_invalid", raised.exception.code)
        finally:
            os.environ.pop(_UNIQUE_ENV, None)


class HitMultiPrecedenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        store = SqliteEventStore(Path(self._tmp.name) / "hitmulti.sqlite3")
        self.gate = SecurityGate(store, KEY)
        self.own = uuid4()
        self.nearest = uuid4()
        self.farthest = uuid4()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_own_turn_token_wins_over_ancestors(self) -> None:
        own_token = derive_canary_token(KEY, self.own)
        arguments = json.dumps({"payload": f"see {own_token}"})
        self.assertEqual(
            ("own_turn", self.own),
            self.gate.hit_multi(arguments, self.own, (self.nearest, self.farthest)),
        )

    def test_ancestor_seed_hits_when_own_token_absent(self) -> None:
        nearest_token = derive_canary_token(KEY, self.nearest)
        arguments = json.dumps({"payload": f"relay {nearest_token}"})
        self.assertEqual(
            ("ancestor_turn", self.nearest),
            self.gate.hit_multi(arguments, self.own, (self.nearest, self.farthest)),
        )

    def test_no_match_returns_none(self) -> None:
        arguments = json.dumps({"payload": "benign"})
        self.assertIsNone(
            self.gate.hit_multi(arguments, self.own, (self.nearest, self.farthest))
        )

    def test_plain_hit_stays_own_turn_only(self) -> None:
        """Backward compatibility: hit() must not gain ancestor awareness."""
        nearest_token = derive_canary_token(KEY, self.nearest)
        arguments = json.dumps({"payload": f"relay {nearest_token}"})
        self.assertFalse(self.gate.hit(arguments, self.own))


class ExecutorAncestorEscalationTest(unittest.TestCase):
    """Ancestor seed hit on an ALLOW action escalates exactly like own-turn J2."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteEventStore(Path(self._tmp.name) / "semsc.sqlite3")
        self.runtime = ThreadRuntime(self.store, actor="semsc")
        self.ledger = ToolLedgerStore(self.store)
        self.approvals = ApprovalService(
            self.store, self.ledger, budget_action_limits={"root": 100},
        )
        self.principal = Principal("root", ("workspace.read",))
        self.gate = SecurityGate(event_store=self.store, key=KEY)
        self.handler_calls: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _executor(self) -> LedgerExecutor:
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
                sandbox_profile_id="semsc",
                policy_version="policy-v1",
            )

        return LedgerExecutor(
            registry, self.ledger,
            {"local_deliver": READ_ONLY_PROFILE},
            policy_engine=engine, approval_service=self.approvals,
            action_resolvers={"local_deliver": resolve},
            security_gate=self.gate,
        )

    def _deliver(self, arguments, *, context: ToolExecutionContext):
        self.handler_calls.append(arguments.payload)
        return ToolExecutionResult("delivered")

    def _running(self, label: str):
        thread = self.runtime.create_thread(f"semsc-{label}")
        queued = self.runtime.create_turn(
            thread.thread_id, label,
            expected_thread_version=thread.version,
        )
        return self.runtime.start_turn(queued.turn_id, queued.version)

    def _authorize(self, executor, running, payload: str, call_id: str,
                   ancestor_turn_ids: tuple[UUID, ...] = ()):
        model_turn_id = uuid4()
        call = ToolCallItem(
            0, f"item-{call_id}", call_id, "local_deliver",
            json.dumps({"payload": payload}),
        )
        context = ToolExecutionContext(
            running.current_run_id, model_turn_id, 1,
            ModelCallRef(model_turn_id, call.call_id),
            turn_id=running.turn_id, turn_version=running.version,
            ancestor_turn_ids=ancestor_turn_ids,
        )
        ticket = executor.authorize(call, context=context)
        return ticket, context

    def _security_payloads(self):
        from collections.abc import Mapping

        return [
            dict(event.payload)
            for event in self.store.read_all()
            if isinstance(event.payload, Mapping) and "signal_kind" in event.payload
        ]

    def test_ancestor_seed_escalates_with_ancestor_signal(self) -> None:
        parent_turn = uuid4()
        parent_token = derive_canary_token(KEY, parent_turn)
        running = self._running("anc1")
        executor = self._executor()
        with self.assertRaises(ApprovalWaiting):
            self._authorize(
                executor, running, f"report {parent_token}", "c1",
                ancestor_turn_ids=(parent_turn,),
            )
        self.assertEqual([], self.handler_calls)
        subject = request = None
        cursor = 0
        while True:
            page = self.store.read_all(after_position=cursor, limit=500)
            for event in page:
                if event.event_type == "approval.requested.v1":
                    subject = UUID(event.payload["subject_id"])
                    request = UUID(event.payload["request_id"])
                if event.event_type == "turn.waiting-for-approval.v1":
                    request = UUID(event.payload["approval_request_id"])
            if len(page) < 500:
                break
            cursor = page[-1].global_position
        self.assertIsNotNone(subject)
        pending = self.approvals.load(subject)
        self.assertIsNotNone(pending)
        self.assertEqual(ApprovalStatus.PENDING, pending.status)
        store_status, _version = SecurityStateStore(self.store).status(subject)
        self.assertEqual(ESCALATION_PENDING, store_status)
        ancestor_signals = [
            payload for payload in self._security_payloads()
            if payload["signal_kind"] == "ancestor_seed_exact"
        ]
        self.assertEqual(1, len(ancestor_signals))
        signal = ancestor_signals[0]
        self.assertEqual("ancestor_turn", signal["seed_source"])
        self.assertEqual(str(parent_turn), signal["ancestor_turn_id"])
        self.assertEqual(
            derive_canary_token(KEY, parent_turn)[:16], signal["canary_id"],
        )

    def test_benign_payload_with_ancestors_does_not_escalate(self) -> None:
        parent_turn = uuid4()
        running = self._running("anc2")
        executor = self._executor()
        ticket, _context = self._authorize(
            executor, running, "benign payload", "c1",
            ancestor_turn_ids=(parent_turn,),
        )
        executed = executor.execute_authorized(ticket)
        self.assertFalse(executed.is_error)
        self.assertEqual(["benign payload"], self.handler_calls)
        self.assertEqual([], self._security_payloads())


if __name__ == "__main__":
    unittest.main()
