from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Barrier
from typing import TypeAlias
from uuid import UUID, uuid4

from koawa_agent_v2.approval_service import (
    ApprovalService,
    ApprovalStatus,
)
from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import (
    AgentLoop,
    ToolExecutionContext,
    ToolExecutionResult,
)
from koawa_agent_v2.execution.worker import TurnWorker
from koawa_agent_v2.ledger import (
    LedgerExecutor,
    READ_ONLY_PROFILE,
    ToolLedgerError,
    ToolExecutionState,
    ToolLedgerStore,
    ToolOutcomeBlocked,
    logical_execution_id,
)
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelCallRef,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    OutputKind,
    StreamHeader,
    ToolCallEcho,
    ToolCallItem,
    ToolResultMessage,
    TurnCompleted,
    TurnStarted,
)
from koawa_agent_v2.policy import (
    ActionKind,
    Decision,
    PolicyEngine,
    PolicyRule,
    Principal,
    ResolvedAction,
    ResolvedResource,
    SideEffectClass as PolicySideEffectClass,
    canonical_arguments,
)
from koawa_agent_v2.recovery import CheckpointStore
from koawa_agent_v2.tools.errors import ToolRegistryError
from koawa_agent_v2.tools.registry import ToolRegistry
from koawa_agent_v2.tools.schema import ToolSpec


StreamScript: TypeAlias = Callable[[ModelRequest], Iterable[ModelStreamEvent]]
ActionResolver: TypeAlias = Callable[
    [ToolCallItem, ToolExecutionContext, object, ResolvedAction | None],
    ResolvedAction,
]


@dataclass(frozen=True, slots=True)
class ProbeArguments:
    value: int


PROBE_SPEC = ToolSpec(
    "probe",
    "D9 integration policy probe",
    ProbeArguments,
    {
        "type": "object",
        "properties": {
            "value": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
            }
        },
        "required": ["value"],
        "additionalProperties": False,
    },
)


class ScriptedClient:
    def __init__(self, *scripts: StreamScript) -> None:
        self._scripts = list(scripts)
        self.requests: list[ModelRequest] = []

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        self.requests.append(request)
        if not self._scripts:
            raise AssertionError("unexpected model request")
        return self._scripts.pop(0)(request)


class CountingHandler:
    def __init__(self) -> None:
        self.calls = 0
        self.values: list[int] = []
        self.execution_ids: list[UUID | None] = []

    def __call__(
        self,
        arguments: ProbeArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        self.calls += 1
        self.values.append(arguments.value)
        self.execution_ids.append(context.execution_id)
        return ToolExecutionResult(f"probe:{arguments.value}")


def _header(
    request: ModelRequest,
    response_id: str,
    sequence: int,
) -> StreamHeader:
    return StreamHeader(
        request.model_turn_id,
        request.provider,
        response_id,
        sequence,
        sequence,
    )


def _completed_stream(
    request: ModelRequest,
    items: Sequence[AssistantTextItem | ToolCallItem],
    finish_reason: FinishReason,
    response_id: str,
) -> tuple[ModelStreamEvent, ...]:
    events: list[ModelStreamEvent] = [
        TurnStarted(_header(request, response_id, 0), request.model)
    ]
    sequence = 1
    for item in items:
        if isinstance(item, ToolCallItem):
            started = ItemStarted(
                _header(request, response_id, sequence),
                item.canonical_index,
                item.item_id,
                OutputKind.TOOL_CALL,
                item.call_id,
                item.name,
            )
        else:
            started = ItemStarted(
                _header(request, response_id, sequence),
                item.canonical_index,
                item.item_id,
                OutputKind.ASSISTANT_TEXT,
            )
        events.append(started)
        sequence += 1
        events.append(ItemCompleted(_header(request, response_id, sequence), item))
        sequence += 1
    turn = ModelTurn(
        request.model_turn_id,
        request.provider,
        request.model,
        response_id,
        tuple(items),
        finish_reason,
    )
    events.append(TurnCompleted(_header(request, response_id, sequence), turn))
    return tuple(events)


def _tool_script(
    *,
    arguments_json: str = '{"value":7}',
    response_id: str = "response-tool",
) -> StreamScript:
    def script(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        call = ToolCallItem(
            0,
            f"item-{response_id}",
            "call-probe",
            "probe",
            arguments_json,
        )
        return _completed_stream(
            request,
            (call,),
            FinishReason.TOOL_CALLS,
            response_id,
        )

    return script


def _final_script(
    text: str = "done",
    response_id: str = "response-final",
) -> StreamScript:
    def script(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        item = AssistantTextItem(0, f"item-{response_id}", text)
        return _completed_stream(
            request,
            (item,),
            FinishReason.STOP,
            response_id,
        )

    return script


class D9PolicyIntegrationTest(unittest.TestCase):
    """Exercise the real Registry -> Policy/Approval -> Ledger -> D6 chain."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "d9-integration.sqlite3"
        self.store = SqliteEventStore(self.database)
        self.runtime = ThreadRuntime(self.store, actor="d9-integration")
        self.checkpoints = CheckpointStore(self.store)
        self.ledger = ToolLedgerStore(self.store)
        self.approvals = ApprovalService(
            self.store,
            self.ledger,
            budget_action_limits={"root": 100},
        )
        self.principal = Principal("root", ("workspace.read",))

    def create_turn(self, label: str):
        thread = self.runtime.create_thread(f"repo-{label}")
        turn = self.runtime.create_turn(
            thread.thread_id,
            label,
            expected_thread_version=thread.version,
        )
        return thread, turn

    def resolver(
        self,
        call: ToolCallItem,
        _context: ToolExecutionContext,
        profile: object,
        _previous: ResolvedAction | None,
    ) -> ResolvedAction:
        return ResolvedAction(
            kind=ActionKind.BUILTIN_TOOL,
            tool_name=call.name,
            canonical_arguments_json=canonical_arguments(call.arguments_json),
            principal=self.principal,
            side_effect_class=PolicySideEffectClass(profile.side_effect_class.value),
            sandbox_profile_id="d8-readonly",
            policy_version="policy-v1",
        )

    def build_executor(
        self,
        handler: CountingHandler,
        decision: Decision,
        *,
        resolver: ActionResolver | None = None,
        ledger: ToolLedgerStore | None = None,
        approvals: ApprovalService | None = None,
    ) -> tuple[LedgerExecutor, ToolRegistry]:
        registry = ToolRegistry()
        registry.register(PROBE_SPEC, handler)
        engine = PolicyEngine(
            "policy-v1",
            (
                PolicyRule(
                    "probe-rule",
                    decision,
                    action_kinds=(ActionKind.BUILTIN_TOOL,),
                    tool_names=("probe",),
                    principal_ids=("root",),
                    required_scopes=("workspace.read",),
                ),
            ),
        )
        executor = LedgerExecutor(
            registry,
            ledger or self.ledger,
            {"probe": READ_ONLY_PROFILE},
            policy_engine=engine,
            approval_service=approvals or self.approvals,
            action_resolvers={"probe": resolver or self.resolver},
        )
        return executor, registry

    def worker(
        self,
        client: ScriptedClient,
        executor: LedgerExecutor,
        *,
        runtime: ThreadRuntime | None = None,
        checkpoints: CheckpointStore | None = None,
        owner_id: str = "d9-worker",
    ) -> TurnWorker:
        return TurnWorker(
            runtime or self.runtime,
            AgentLoop(client, tool_executor=executor),
            provider="test-provider",
            model="test-model",
            checkpoint_store=checkpoints or self.checkpoints,
            owner_id=owner_id,
        )

    def execution_events(self, turn_id: UUID) -> tuple[str, ...]:
        return tuple(
            event.event_type
            for event in self.store.read_stream(StreamId("run-execution", turn_id))
        )

    def record_for_first_request(self, turn_id: UUID, client: ScriptedClient):
        model_turn_id = client.requests[0].model_turn_id
        return self.ledger.load_for_call(turn_id, model_turn_id, "call-probe")

    def direct_call(self, label: str):
        _, queued = self.create_turn(label)
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        model_turn_id = uuid4()
        call = ToolCallItem(
            0, f"item-{label}", f"call-{label}", "probe", '{"value":7}',
        )
        context = ToolExecutionContext(
            running.current_run_id,
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, call.call_id),
            turn_id=running.turn_id,
            turn_version=running.version,
        )
        return running, call, context

    def test_invalid_schema_returns_before_approval_ledger_and_tool_started(self) -> None:
        _, queued = self.create_turn("schema-gate")
        handler = CountingHandler()
        executor, _ = self.build_executor(handler, Decision.ASK)
        client = ScriptedClient(
            _tool_script(arguments_json='{"value":"not-an-integer"}'),
            _final_script("schema rejected"),
        )

        result = self.worker(client, executor).execute(queued.turn_id, queued.version)

        self.assertEqual(TurnStatus.COMPLETED, result.turn.status)
        self.assertEqual(0, handler.calls)
        self.assertIsNone(self.record_for_first_request(queued.turn_id, client))
        execution_id = logical_execution_id(
            queued.turn_id,
            client.requests[0].model_turn_id,
            "call-probe",
        )
        self.assertIsNone(self.approvals.load(execution_id))
        self.assertNotIn("run.phase-advanced.v1", self.execution_events(queued.turn_id))
        tool_results = [
            item
            for item in client.requests[1].input_items
            if isinstance(item, ToolResultMessage)
        ]
        self.assertEqual(1, len(tool_results))
        self.assertTrue(tool_results[0].is_error)

    def test_bound_registry_rejects_raw_execute_bypass(self) -> None:
        _, queued = self.create_turn("registry-bypass")
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        handler = CountingHandler()
        _, registry = self.build_executor(handler, Decision.ALLOW)
        model_turn_id = uuid4()
        call = ToolCallItem(
            0,
            "item-bypass",
            "call-bypass",
            "probe",
            '{"value":1}',
        )
        context = ToolExecutionContext(
            running.current_run_id,
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, call.call_id),
            turn_id=running.turn_id,
            turn_version=running.version,
        )

        with self.assertRaises(ToolRegistryError) as caught:
            registry.execute(call, context=context)

        self.assertEqual("policy_authorization_required", caught.exception.code)
        self.assertEqual(0, handler.calls)

    def test_prepared_and_authorized_replacements_are_rejected_and_single_use(self) -> None:
        handler = CountingHandler()
        registry = ToolRegistry()
        registry.register(PROBE_SPEC, handler)
        _, call, context = self.direct_call("prepared-identity")
        prepared = registry.prepare(call)
        authority = object()
        registry.bind_policy_authority(authority)
        forged_prepared = replace(prepared, _arguments=ProbeArguments(99))

        with self.assertRaises(ToolRegistryError) as prepared_error:
            registry.invoke_prepared(
                forged_prepared, context=context, authority=authority,
            )
        self.assertEqual(
            "invalid_prepared_invocation", prepared_error.exception.code,
        )
        result = registry.invoke_prepared(
            prepared, context=context, authority=authority,
        )
        self.assertEqual("probe:7", result.content)
        with self.assertRaises(ToolRegistryError) as reused_prepared:
            registry.invoke_prepared(
                prepared, context=context, authority=authority,
            )
        self.assertEqual(
            "invalid_prepared_invocation", reused_prepared.exception.code,
        )

        durable_handler = CountingHandler()
        executor, _ = self.build_executor(durable_handler, Decision.ALLOW)
        _, durable_call, durable_context = self.direct_call("ticket-identity")
        ticket = executor.authorize(durable_call, context=durable_context)
        forged_ticket = replace(ticket)
        with self.assertRaises(ToolLedgerError) as ticket_error:
            executor.execute_authorized(forged_ticket)
        self.assertEqual(
            "invalid_policy_authorization", ticket_error.exception.code,
        )
        self.assertEqual(
            "probe:7", executor.execute_authorized(ticket).content,
        )
        with self.assertRaises(ToolLedgerError) as reused_ticket:
            executor.execute_authorized(ticket)
        self.assertEqual(
            "invalid_policy_authorization", reused_ticket.exception.code,
        )
        self.assertEqual(1, durable_handler.calls)

    def test_two_same_run_executors_can_enter_only_one_handler(self) -> None:
        handler = CountingHandler()
        first_executor, _ = self.build_executor(handler, Decision.ALLOW)
        second_executor, _ = self.build_executor(handler, Decision.ALLOW)
        _, call, context = self.direct_call("same-run-handler")
        first_ticket = first_executor.authorize(call, context=context)
        second_ticket = second_executor.authorize(call, context=context)
        barrier = Barrier(2)

        def invoke(item):
            executor, ticket = item
            barrier.wait()
            try:
                return executor.execute_authorized(ticket)
            except ToolOutcomeBlocked as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(
                invoke,
                (
                    (first_executor, first_ticket),
                    (second_executor, second_ticket),
                ),
            ))
        completed = [
            item for item in results if isinstance(item, ToolExecutionResult)
        ]
        blocked = [item for item in results if isinstance(item, ToolOutcomeBlocked)]
        self.assertEqual(1, len(completed))
        self.assertEqual(1, len(blocked))
        self.assertEqual("tool_claim_already_executed", blocked[0].code)
        self.assertEqual(1, handler.calls)
        record = self.ledger.load(first_ticket.record.execution_id)
        self.assertEqual(ToolExecutionState.SUCCEEDED, record.state)

    def test_terminal_result_is_not_released_after_principal_or_policy_drift(self) -> None:
        handler = CountingHandler()
        executor, _ = self.build_executor(handler, Decision.ALLOW)
        _, call, context = self.direct_call("terminal-authority")
        initial = executor.execute_authorized(
            executor.authorize(call, context=context)
        )
        self.assertEqual("probe:7", initial.content)

        def principal_drift(item, call_context, profile, previous):
            action = self.resolver(item, call_context, profile, previous)
            return replace(
                action,
                principal=Principal("intruder", ("workspace.read",)),
            )

        def policy_drift(item, call_context, profile, previous):
            action = self.resolver(item, call_context, profile, previous)
            return replace(action, policy_version="policy-v2")

        for label, resolver in (
            ("principal", principal_drift),
            ("policy", policy_drift),
        ):
            with self.subTest(label=label):
                replay, _ = self.build_executor(
                    handler, Decision.ALLOW, resolver=resolver,
                )
                result = replay.execute_authorized(
                    replay.authorize(call, context=context)
                )
                self.assertTrue(result.is_error)
                self.assertNotEqual("probe:7", result.content)
                self.assertEqual(
                    "policy_authorization_evidence_mismatch",
                    json.loads(result.content)["code"],
                )
        self.assertEqual(1, handler.calls)

    def test_ask_persists_wait_before_tool_started_and_handler(self) -> None:
        _, queued = self.create_turn("ask")
        handler = CountingHandler()
        executor, _ = self.build_executor(handler, Decision.ASK)
        client = ScriptedClient(_tool_script())

        result = self.worker(client, executor).execute(queued.turn_id, queued.version)

        self.assertEqual(TurnStatus.WAITING_FOR_APPROVAL, result.turn.status)
        self.assertIsNone(result.loop_result)
        self.assertEqual(0, handler.calls)
        self.assertNotIn("run.phase-advanced.v1", self.execution_events(queued.turn_id))
        record = self.record_for_first_request(queued.turn_id, client)
        self.assertEqual(ToolExecutionState.PREPARED, record.state)
        pending = self.approvals.load(record.execution_id)
        self.assertEqual(ApprovalStatus.PENDING, pending.status)
        self.assertEqual(
            pending.request_id,
            result.turn.pending_interrupt.approval_request_id,
        )

    def test_grant_survives_restart_resumes_same_call_once_and_is_consumed(self) -> None:
        _, queued = self.create_turn("grant-restart")
        handler = CountingHandler()
        first_executor, _ = self.build_executor(handler, Decision.ASK)
        first_client = ScriptedClient(_tool_script())
        waiting = self.worker(first_client, first_executor).execute(
            queued.turn_id,
            queued.version,
        ).turn
        record = self.record_for_first_request(queued.turn_id, first_client)
        pending = self.approvals.load(record.execution_id)
        granted = self.approvals.resolve(
            pending,
            True,
            expected_approval_version=pending.version,
            expected_turn_version=waiting.version,
            interrupt_id=pending.interrupt_id,
            approver_principal_id="operator",
        )
        self.assertEqual(ApprovalStatus.GRANTED, granted.status)

        restarted_store = SqliteEventStore(self.database)
        restarted_runtime = ThreadRuntime(restarted_store, actor="d9-restarted")
        restarted_checkpoints = CheckpointStore(restarted_store)
        restarted_ledger = ToolLedgerStore(restarted_store)
        restarted_approvals = ApprovalService(
            restarted_store,
            restarted_ledger,
            budget_action_limits={"root": 100},
        )
        restarted_executor, _ = self.build_executor(
            handler,
            Decision.ASK,
            ledger=restarted_ledger,
            approvals=restarted_approvals,
        )
        final_client = ScriptedClient(_final_script("approved and executed"))
        resumed = restarted_runtime.get_turn(queued.turn_id)

        completed = self.worker(
            final_client,
            restarted_executor,
            runtime=restarted_runtime,
            checkpoints=restarted_checkpoints,
            owner_id="d9-restarted-worker",
        ).execute(resumed.turn_id, resumed.version)

        self.assertEqual(TurnStatus.COMPLETED, completed.turn.status)
        self.assertEqual(1, handler.calls)
        self.assertEqual([7], handler.values)
        settled = restarted_ledger.load(record.execution_id)
        self.assertEqual(ToolExecutionState.SUCCEEDED, settled.state)
        self.assertEqual(1, settled.claim_epoch)
        consumed = restarted_approvals.load(record.execution_id)
        self.assertEqual(ApprovalStatus.CONSUMED, consumed.status)
        self.assertEqual(settled.claim_token, consumed.claim_token)
        results = [
            item
            for item in final_client.requests[0].input_items
            if isinstance(item, ToolResultMessage)
        ]
        self.assertEqual(1, len(results))
        self.assertEqual("call-probe", results[0].call_ref.call_id)

    def test_denial_resumes_same_call_with_paired_typed_error_and_no_handler(self) -> None:
        _, queued = self.create_turn("deny")
        handler = CountingHandler()
        first_executor, _ = self.build_executor(handler, Decision.ASK)
        first_client = ScriptedClient(_tool_script())
        waiting = self.worker(first_client, first_executor).execute(
            queued.turn_id,
            queued.version,
        ).turn
        record = self.record_for_first_request(queued.turn_id, first_client)
        pending = self.approvals.load(record.execution_id)
        denied = self.approvals.resolve(
            pending,
            False,
            expected_approval_version=pending.version,
            expected_turn_version=waiting.version,
            interrupt_id=pending.interrupt_id,
            approver_principal_id="operator",
        )
        self.assertEqual(ApprovalStatus.DENIED, denied.status)
        queued_again = self.runtime.get_turn(queued.turn_id)
        second_executor, _ = self.build_executor(handler, Decision.ASK)
        final_client = ScriptedClient(_final_script("denial observed"))

        result = self.worker(
            final_client,
            second_executor,
            owner_id="d9-denied-worker",
        ).execute(queued_again.turn_id, queued_again.version)

        self.assertEqual(TurnStatus.COMPLETED, result.turn.status)
        self.assertEqual(0, handler.calls)
        calls = [
            item
            for item in final_client.requests[0].input_items
            if isinstance(item, ToolCallEcho)
        ]
        results = [
            item
            for item in final_client.requests[0].input_items
            if isinstance(item, ToolResultMessage)
        ]
        self.assertEqual(1, len(calls))
        self.assertEqual(1, len(results))
        self.assertEqual(calls[0].call_ref, results[0].call_ref)
        self.assertTrue(results[0].is_error)
        error = json.loads(results[0].content)
        self.assertEqual("policy_denied", error["error"])
        self.assertEqual(ApprovalStatus.DENIED, self.approvals.load(record.execution_id).status)
        self.assertEqual(ToolExecutionState.PREPARED, self.ledger.load(record.execution_id).state)

    def test_resolver_drift_after_grant_creates_fresh_ask(self) -> None:
        _, queued = self.create_turn("resolver-drift")
        handler = CountingHandler()

        def drifting_resolver(
            call: ToolCallItem,
            _context: ToolExecutionContext,
            profile: object,
            previous: ResolvedAction | None,
        ) -> ResolvedAction:
            identity = "inode-a" if previous is None else "inode-b"
            return ResolvedAction(
                kind=ActionKind.BUILTIN_TOOL,
                tool_name=call.name,
                canonical_arguments_json=canonical_arguments(call.arguments_json),
                principal=self.principal,
                side_effect_class=PolicySideEffectClass(profile.side_effect_class.value),
                sandbox_profile_id="d8-readonly",
                policy_version="policy-v1",
                resources=(
                    ResolvedResource(
                        "workspace_path",
                        "value.txt",
                        "/workspace/value.txt",
                        identity,
                    ),
                ),
            )

        first_executor, _ = self.build_executor(
            handler,
            Decision.ASK,
            resolver=drifting_resolver,
        )
        first_client = ScriptedClient(_tool_script())
        first_wait = self.worker(first_client, first_executor).execute(
            queued.turn_id,
            queued.version,
        ).turn
        record = self.record_for_first_request(queued.turn_id, first_client)
        first_pending = self.approvals.load(record.execution_id)
        self.approvals.resolve(
            first_pending,
            True,
            expected_approval_version=first_pending.version,
            expected_turn_version=first_wait.version,
            interrupt_id=first_pending.interrupt_id,
            approver_principal_id="operator",
        )
        queued_again = self.runtime.get_turn(queued.turn_id)
        second_executor, _ = self.build_executor(
            handler,
            Decision.ASK,
            resolver=drifting_resolver,
        )
        unused_client = ScriptedClient(_final_script("must not reach model"))

        second_wait = self.worker(
            unused_client,
            second_executor,
            owner_id="d9-drift-worker",
        ).execute(queued_again.turn_id, queued_again.version).turn

        self.assertEqual(TurnStatus.WAITING_FOR_APPROVAL, second_wait.status)
        self.assertEqual(0, handler.calls)
        self.assertEqual([], unused_client.requests)
        second_pending = self.approvals.load(record.execution_id)
        self.assertEqual(ApprovalStatus.PENDING, second_pending.status)
        self.assertNotEqual(first_pending.request_id, second_pending.request_id)
        self.assertNotEqual(first_pending.action_digest, second_pending.action_digest)
        self.assertEqual(
            second_pending.request_id,
            second_wait.pending_interrupt.approval_request_id,
        )

    def test_allow_executes_without_approval(self) -> None:
        _, queued = self.create_turn("allow")
        handler = CountingHandler()
        executor, _ = self.build_executor(handler, Decision.ALLOW)
        client = ScriptedClient(_tool_script(), _final_script("allowed"))

        result = self.worker(client, executor).execute(queued.turn_id, queued.version)

        self.assertEqual(TurnStatus.COMPLETED, result.turn.status)
        self.assertEqual(1, handler.calls)
        record = self.record_for_first_request(queued.turn_id, client)
        self.assertEqual(ToolExecutionState.SUCCEEDED, record.state)
        self.assertIsNone(self.approvals.load(record.execution_id))

    def test_legacy_true_resume_does_not_authorize_durable_ask(self) -> None:
        _, queued = self.create_turn("legacy-true")
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        legacy_wait = self.runtime.wait_for_approval(
            running.turn_id,
            "legacy approval",
            expected_version=running.version,
            run_id=running.current_run_id,
        )
        legacy_queued = self.runtime.request_resume(
            legacy_wait.turn_id,
            legacy_wait.version,
            interrupt_id=legacy_wait.pending_interrupt.interrupt_id,
            response=True,
        )
        handler = CountingHandler()
        executor, _ = self.build_executor(handler, Decision.ASK)
        client = ScriptedClient(_tool_script(response_id="response-after-legacy"))

        durable_wait = self.worker(
            client,
            executor,
            owner_id="d9-legacy-worker",
        ).execute(legacy_queued.turn_id, legacy_queued.version).turn

        self.assertEqual(TurnStatus.WAITING_FOR_APPROVAL, durable_wait.status)
        self.assertEqual(0, handler.calls)
        record = self.record_for_first_request(queued.turn_id, client)
        pending = self.approvals.load(record.execution_id)
        self.assertEqual(ApprovalStatus.PENDING, pending.status)
        self.assertEqual(
            pending.request_id,
            durable_wait.pending_interrupt.approval_request_id,
        )
        self.assertNotEqual(
            legacy_wait.pending_interrupt.interrupt_id,
            durable_wait.pending_interrupt.interrupt_id,
        )


if __name__ == "__main__":
    unittest.main()
