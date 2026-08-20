"""D9: durable one-shot approval across a fresh SQLite-backed object graph.

Run from ``v2/`` with::

    $env:PYTHONPATH = "src"
    py -3.14 -B examples/day09_durable_approval.py

The model is scripted and DNS answers are injected constants.  This example
does not open a network connection, contact a real DNS resolver, start Docker,
or pull an image.  All control-plane persistence uses one temporary SQLite
database that is deleted at exit.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias
from uuid import UUID

from koawa_agent_v2.approval_service import ApprovalService, ApprovalStatus
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
    ToolExecutionState,
    ToolLedgerStore,
)
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    OutputKind,
    StreamHeader,
    ToolCallItem,
    ToolResultMessage,
    TurnCompleted,
    TurnStarted,
)
from koawa_agent_v2.policy import (
    ActionKind,
    Decision,
    PolicyEngine,
    PolicyError,
    PolicyRule,
    Principal,
    ResolvedAction,
    ResourceBudgetLimits,
    ResourceRequest,
    SideEffectClass,
    canonical_arguments,
    preflight_resource_request,
    resolve_network_target,
)
from koawa_agent_v2.recovery import CheckpointStore
from koawa_agent_v2.tools.registry import ToolRegistry
from koawa_agent_v2.tools.schema import ToolSpec


StreamScript: TypeAlias = Callable[[ModelRequest], Iterable[ModelStreamEvent]]


@dataclass(frozen=True, slots=True)
class ProbeArguments:
    value: int


PROBE_SPEC = ToolSpec(
    "probe",
    "D9 durable approval example probe",
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
    """Deterministic Provider boundary; it has no external I/O."""

    def __init__(self, *scripts: StreamScript) -> None:
        self._scripts = list(scripts)
        self.requests: list[ModelRequest] = []

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        self.requests.append(request)
        if not self._scripts:
            raise AssertionError("unexpected model request")
        return self._scripts.pop(0)(request)


class ProbeHandler:
    """The only demonstrated side effect: an in-memory invocation counter."""

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


@dataclass(frozen=True, slots=True)
class RuntimeGraph:
    store: SqliteEventStore
    runtime: ThreadRuntime
    checkpoints: CheckpointStore
    ledger: ToolLedgerStore
    approvals: ApprovalService


def _runtime_graph(database: Path, *, actor: str) -> RuntimeGraph:
    store = SqliteEventStore(database)
    runtime = ThreadRuntime(store, actor=actor)
    checkpoints = CheckpointStore(store)
    ledger = ToolLedgerStore(store)
    approvals = ApprovalService(
        store,
        ledger,
        budget_action_limits={"demo-user": 10},
    )
    return RuntimeGraph(store, runtime, checkpoints, ledger, approvals)


def _action_resolver(principal: Principal):
    def resolve(
        call: ToolCallItem,
        _context: ToolExecutionContext,
        profile: object,
        _previous: ResolvedAction | None,
    ) -> ResolvedAction:
        return ResolvedAction(
            kind=ActionKind.BUILTIN_TOOL,
            tool_name=call.name,
            canonical_arguments_json=canonical_arguments(call.arguments_json),
            principal=principal,
            side_effect_class=SideEffectClass(profile.side_effect_class.value),
            sandbox_profile_id="d8-network-none",
            policy_version="demo-policy-v1",
        )

    return resolve


def _ledger_executor(
    graph: RuntimeGraph,
    handler: ProbeHandler,
    principal: Principal,
) -> LedgerExecutor:
    registry = ToolRegistry()
    registry.register(PROBE_SPEC, handler)
    policy = PolicyEngine(
        "demo-policy-v1",
        (
            PolicyRule(
                "ask-probe",
                Decision.ASK,
                action_kinds=(ActionKind.BUILTIN_TOOL,),
                tool_names=("probe",),
                principal_ids=(principal.principal_id,),
                required_scopes=("workspace.read",),
            ),
        ),
    )
    return LedgerExecutor(
        registry,
        graph.ledger,
        {"probe": READ_ONLY_PROFILE},
        policy_engine=policy,
        approval_service=graph.approvals,
        action_resolvers={"probe": _action_resolver(principal)},
    )


def _worker(
    graph: RuntimeGraph,
    client: ScriptedClient,
    executor: LedgerExecutor,
    *,
    owner_id: str,
) -> TurnWorker:
    return TurnWorker(
        graph.runtime,
        AgentLoop(client, tool_executor=executor),
        provider="scripted-provider",
        model="scripted-model",
        checkpoint_store=graph.checkpoints,
        owner_id=owner_id,
    )


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


def _tool_script(response_id: str) -> StreamScript:
    def script(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        call = ToolCallItem(
            0,
            f"item-{response_id}",
            "call-probe",
            "probe",
            '{"value":9}',
        )
        return _completed_stream(
            request,
            (call,),
            FinishReason.TOOL_CALLS,
            response_id,
        )

    return script


def _final_script(text: str, response_id: str) -> StreamScript:
    def script(request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        item = AssistantTextItem(0, f"item-{response_id}", text)
        return _completed_stream(
            request,
            (item,),
            FinishReason.STOP,
            response_id,
        )

    return script


def _create_turn(graph: RuntimeGraph, label: str):
    thread = graph.runtime.create_thread(f"demo-repository-{label}")
    turn = graph.runtime.create_turn(
        thread.thread_id,
        label,
        expected_thread_version=thread.version,
    )
    return turn


def _execution_event_types(graph: RuntimeGraph, turn_id: UUID) -> tuple[str, ...]:
    return tuple(
        event.event_type
        for event in graph.store.read_stream(StreamId("run-execution", turn_id))
    )


def _budget_events(graph: RuntimeGraph) -> tuple[Any, ...]:
    return tuple(
        event
        for event in graph.store.read_all(limit=500)
        if event.event_type == "resource.budget-reserved.v1"
    )


def _pending_record(
    graph: RuntimeGraph,
    turn_id: UUID,
    client: ScriptedClient,
):
    return graph.ledger.load_for_call(
        turn_id,
        client.requests[0].model_turn_id,
        "call-probe",
    )


def _network_and_budget_controls(principal: Principal) -> dict[str, Any]:
    # The injected function returns a documentation-only public address.  It
    # never calls socket/getaddrinfo and no connection is attempted.
    proxied_target = resolve_network_target(
        "https://example.com/api",
        lambda _host: ("93.184.216.34",),
        via_proxy=True,
    )
    allow_network_rule = PolicyRule(
        "allow-fetch",
        Decision.ALLOW,
        action_kinds=(ActionKind.BUILTIN_TOOL,),
        tool_names=("fetch",),
        principal_ids=(principal.principal_id,),
    )
    proxied_action = ResolvedAction(
        ActionKind.BUILTIN_TOOL,
        "fetch",
        "{}",
        principal,
        SideEffectClass.READ_ONLY,
        "d8-network-none",
        "demo-policy-v1",
        network_target=proxied_target,
    )
    network_none = PolicyEngine(
        "demo-policy-v1",
        (allow_network_rule,),
    ).evaluate(proxied_action)

    direct_target = resolve_network_target(
        "https://example.com/api",
        lambda _host: ("93.184.216.34",),
        via_proxy=False,
    )
    direct_action = ResolvedAction(
        ActionKind.BUILTIN_TOOL,
        "fetch",
        "{}",
        principal,
        SideEffectClass.READ_ONLY,
        "d8-network-none",
        "demo-policy-v1",
        network_target=direct_target,
    )
    proxy_bypass = PolicyEngine(
        "demo-policy-v1",
        (allow_network_rule,),
        network_enabled=True,
        proxy_required=True,
        allowed_origins=(direct_target.origin,),
    ).evaluate(direct_action)

    private_dns_code: str | None = None
    try:
        resolve_network_target(
            "https://private.example/api",
            lambda _host: ("127.0.0.1",),
            via_proxy=True,
        )
    except PolicyError as error:
        private_dns_code = error.code

    budget_code: str | None = None
    try:
        preflight_resource_request(
            ResourceRequest(memory_bytes=128 * 1024 * 1024),
            ResourceBudgetLimits(max_memory_bytes=64 * 1024 * 1024),
        )
    except PolicyError as error:
        budget_code = error.code

    assert network_none.decision is Decision.DENY
    assert network_none.code == "network_disabled"
    assert proxy_bypass.decision is Decision.DENY
    assert proxy_bypass.code == "proxy_required"
    assert private_dns_code == "non_global_network_address"
    assert budget_code == "resource_budget_exceeded"
    return {
        "real_network_calls": 0,
        "dns_mode": "injected_answers_only",
        "d8_network_none": {
            "decision": network_none.decision.value,
            "code": network_none.code,
        },
        "direct_proxy_bypass": {
            "decision": proxy_bypass.decision.value,
            "code": proxy_bypass.code,
        },
        "private_dns": {"code": private_dns_code},
        "budget_preflight": {"code": budget_code},
    }


def main() -> int:
    trace: dict[str, Any] = {
        "storage": {"temporary_sqlite": True, "external_services": []}
    }
    try:
        with tempfile.TemporaryDirectory(prefix="koawa-d9-example-") as directory:
            database = Path(directory) / "d9-events.sqlite3"
            principal = Principal("demo-user", ("workspace.read",))
            handler = ProbeHandler()

            first_graph = _runtime_graph(database, actor="demo-before-restart")
            first_executor = _ledger_executor(first_graph, handler, principal)
            first_client = ScriptedClient(_tool_script("tool-before-approval"))
            first_turn = _create_turn(first_graph, "approve one probe")
            waiting = _worker(
                first_graph,
                first_client,
                first_executor,
                owner_id="demo-worker-before-restart",
            ).execute(first_turn.turn_id, first_turn.version).turn
            first_record = _pending_record(
                first_graph, first_turn.turn_id, first_client
            )
            first_pending = first_graph.approvals.load(first_record.execution_id)
            first_events = _execution_event_types(first_graph, first_turn.turn_id)

            assert waiting.status is TurnStatus.WAITING_FOR_APPROVAL
            assert first_record.state is ToolExecutionState.PREPARED
            assert first_pending.status is ApprovalStatus.PENDING
            assert handler.calls == 0
            assert "run.phase-advanced.v1" not in first_events
            trace["ask_before_restart"] = {
                "turn_status": waiting.status.value,
                "ledger_state": first_record.state.value,
                "approval_status": first_pending.status.value,
                "handler_calls": handler.calls,
                "tool_started_persisted": "run.phase-advanced.v1" in first_events,
                "request_id": str(first_pending.request_id),
                "action_digest": first_pending.action_digest,
            }

            # Simulated process restart: every store/runtime/checkpoint/ledger/
            # approval/registry/loop/worker object below is newly constructed.
            restarted = _runtime_graph(database, actor="demo-after-restart")
            reloaded_record = restarted.ledger.load(first_record.execution_id)
            reloaded_pending = restarted.approvals.load(first_record.execution_id)
            reloaded_waiting = restarted.runtime.get_turn(first_turn.turn_id)
            assert reloaded_record == first_record
            assert reloaded_pending.request_id == first_pending.request_id
            assert reloaded_waiting.status is TurnStatus.WAITING_FOR_APPROVAL

            grant = restarted.approvals.resolve(
                reloaded_pending,
                True,
                expected_approval_version=reloaded_pending.version,
                expected_turn_version=reloaded_waiting.version,
                interrupt_id=reloaded_pending.interrupt_id,
                approver_principal_id="demo-operator",
            )
            assert grant.status is ApprovalStatus.GRANTED
            queued_after_grant = restarted.runtime.get_turn(first_turn.turn_id)
            restarted_executor = _ledger_executor(restarted, handler, principal)
            final_client = ScriptedClient(
                _final_script("approved probe completed", "final-after-grant")
            )
            completed = _worker(
                restarted,
                final_client,
                restarted_executor,
                owner_id="demo-worker-after-restart",
            ).execute(
                queued_after_grant.turn_id,
                queued_after_grant.version,
            ).turn
            settled = restarted.ledger.load(first_record.execution_id)
            consumed = restarted.approvals.load(first_record.execution_id)
            budget_events = _budget_events(restarted)

            assert completed.status is TurnStatus.COMPLETED
            assert settled.state is ToolExecutionState.SUCCEEDED
            assert settled.claim_epoch == 1
            assert consumed.status is ApprovalStatus.CONSUMED
            assert consumed.claim_token == settled.claim_token
            assert handler.calls == 1
            assert handler.execution_ids == [settled.execution_id]
            assert len(budget_events) == 1
            resumed_results = [
                item
                for item in final_client.requests[0].input_items
                if isinstance(item, ToolResultMessage)
            ]
            assert len(resumed_results) == 1
            assert resumed_results[0].call_ref.call_id == "call-probe"
            trace["grant_after_fresh_object_graph"] = {
                "fresh_store_runtime_checkpoint_ledger_approval": True,
                "same_request_id": consumed.request_id == first_pending.request_id,
                "turn_status": completed.status.value,
                "approval_status": consumed.status.value,
                "ledger_state": settled.state.value,
                "claim_epoch": settled.claim_epoch,
                "handler_calls": handler.calls,
                "same_pending_call_result_count": len(resumed_results),
                "budget_reserved_events": len(budget_events),
            }

            deny_turn = _create_turn(restarted, "deny one probe")
            deny_executor = _ledger_executor(restarted, handler, principal)
            deny_client = ScriptedClient(_tool_script("tool-before-denial"))
            deny_waiting = _worker(
                restarted,
                deny_client,
                deny_executor,
                owner_id="demo-deny-worker-1",
            ).execute(deny_turn.turn_id, deny_turn.version).turn
            deny_record = _pending_record(restarted, deny_turn.turn_id, deny_client)
            deny_pending = restarted.approvals.load(deny_record.execution_id)
            denied = restarted.approvals.resolve(
                deny_pending,
                False,
                expected_approval_version=deny_pending.version,
                expected_turn_version=deny_waiting.version,
                interrupt_id=deny_pending.interrupt_id,
                approver_principal_id="demo-operator",
            )
            assert denied.status is ApprovalStatus.DENIED
            deny_queued = restarted.runtime.get_turn(deny_turn.turn_id)
            deny_resume_executor = _ledger_executor(restarted, handler, principal)
            deny_final_client = ScriptedClient(
                _final_script("denial observed", "final-after-denial")
            )
            deny_completed = _worker(
                restarted,
                deny_final_client,
                deny_resume_executor,
                owner_id="demo-deny-worker-2",
            ).execute(deny_queued.turn_id, deny_queued.version).turn
            deny_results = [
                item
                for item in deny_final_client.requests[0].input_items
                if isinstance(item, ToolResultMessage)
            ]
            deny_error = json.loads(deny_results[0].content)

            assert deny_completed.status is TurnStatus.COMPLETED
            assert handler.calls == 1
            assert len(deny_results) == 1
            assert deny_results[0].is_error
            assert deny_error["error"] == "policy_denied"
            assert restarted.approvals.load(deny_record.execution_id).status is ApprovalStatus.DENIED
            assert restarted.ledger.load(deny_record.execution_id).state is ToolExecutionState.PREPARED
            assert len(_budget_events(restarted)) == 1
            trace["deny"] = {
                "turn_status": deny_completed.status.value,
                "approval_status": "denied",
                "handler_calls_total": handler.calls,
                "typed_tool_error": deny_error,
                "ledger_state": restarted.ledger.load(deny_record.execution_id).state.value,
                "budget_reserved_events_total": len(_budget_events(restarted)),
            }

            trace["pure_fail_closed_controls"] = _network_and_budget_controls(
                principal
            )
            trace["final"] = {
                "all_assertions_passed": True,
                "approved_handler_executions": handler.calls,
                "denied_handler_executions": 0,
                "real_network_calls": 0,
                "docker_invocations": 0,
            }
    except BaseException as error:
        trace["final"] = {
            "all_assertions_passed": False,
            "error_type": type(error).__name__,
            "error": str(error),
        }
        print(json.dumps(trace, ensure_ascii=False, indent=2, sort_keys=True))
        print(f"D9 example failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(json.dumps(trace, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
