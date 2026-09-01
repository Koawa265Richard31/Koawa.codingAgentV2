"""Re-entrant worker for the D15 golden composite E2E (kill + resume)."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from koawa_agent_v2.agents.control import (
    AgentBudgetLimits, AgentControlPlane, Principal as AgentPrincipal,
    terminal_result_identity,
)
from koawa_agent_v2.agents.graph import AgentState, ContextMode
from koawa_agent_v2.agents.messages import MessageKind
from koawa_agent_v2.agents.scheduler import AgentScheduler, ScriptedAgentProvider
from koawa_agent_v2.approval_service import ApprovalService, ApprovalStatus
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.event_store import EventMetadata, NewEvent, StreamId, StreamWrite
from koawa_agent_v2.execution.loop import (
    AgentLoop,
    AgentLoopError,
    ToolExecutionContext,
    ToolExecutionResult,
)
from koawa_agent_v2.execution.worker import TurnWorker
from koawa_agent_v2.ledger import (
    IDEMPOTENT_WRITE_PROFILE,
    LedgerExecutor,
    READ_ONLY_PROFILE,
    ToolLedgerStore,
)
from koawa_agent_v2.mcp import McpSession, StdioTransport, spawn_fixture_command
from koawa_agent_v2.mcp.tool_binding import McpRegistryAdapter
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
    SideEffectClass,
    canonical_arguments,
)
from koawa_agent_v2.recovery import CheckpointStore, RecoveryCoordinator
from koawa_agent_v2.telemetry.trace import TraceStore
from koawa_agent_v2.tools.registry import ToolRegistry
from koawa_agent_v2.tools.schema import ToolSpec
from koawa_agent_v2.workspace.container import (
    ContainerResult,
    DockerContainerRunner,
    InjectedContainerRunner,
)
from koawa_agent_v2.workspace.integration import Artifact, ArtifactIntegrator
from koawa_agent_v2.workspace.store import AgentWorkspaceStore
from koawa_agent_v2.workspace.worktree import WorktreeManager


DB = Path(os.environ["GOLDEN_DB"])
REPO = Path(os.environ["GOLDEN_REPO"])
RESUME = os.environ.get("GOLDEN_RESUME", "0") == "1"
READY_FILE = os.environ.get("GOLDEN_READY_FILE")
STATE_FILE = os.environ.get("GOLDEN_STATE_FILE")
KILL_POINT = os.environ.get("GOLDEN_KILL_POINT", "")
EVIDENCE_FILE = os.environ.get("GOLDEN_EVIDENCE_FILE")


@dataclass(frozen=True, slots=True)
class WriteArgs:
    content: str


@dataclass(frozen=True, slots=True)
class TestArgs:
    expect: str


@dataclass(frozen=True, slots=True)
class DenyArgs:
    marker: str


WRITE_SPEC = ToolSpec(
    "write_patch",
    "Write solution.txt content.",
    WriteArgs,
    {
        "type": "object",
        "properties": {
            "content": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        "required": ["content"],
        "additionalProperties": False,
    },
)
TEST_SPEC = ToolSpec(
    "run_test",
    "Run the verification test in the worktree container.",
    TestArgs,
    {
        "type": "object",
        "properties": {
            "expect": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        "required": ["expect"],
        "additionalProperties": False,
    },
)
DENY_SPEC = ToolSpec(
    "deny_probe",
    "Intentional policy-denied probe; its handler must never run.",
    DenyArgs,
    {
        "type": "object",
        "properties": {"marker": {"type": "string", "minLength": 1, "maxLength": 32}},
        "required": ["marker"],
        "additionalProperties": False,
    },
)


def _header(request, response_id, sequence):
    return StreamHeader(request.model_turn_id, request.provider, response_id, sequence, sequence)


def _completed_stream(request, items, finish_reason, response_id):
    events = [TurnStarted(_header(request, response_id, 0), request.model)]
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
        request.model_turn_id, request.provider, request.model,
        response_id, tuple(items), finish_reason,
    )
    events.append(TurnCompleted(_header(request, response_id, sequence), turn))
    return tuple(events)


class CompositeProvider:
    """Driven by the last tool result's call_id (resume-safe, stateless)."""

    def stream(self, request: ModelRequest):
        last = _last_result(request)
        if last is None:
            call = ToolCallItem(0, "item-deny", "call-deny", "deny_probe", '{"marker":"probe"}')
            yield from _completed_stream(request, (call,), FinishReason.TOOL_CALLS, "r1")
            return
        if last.call_ref.call_id.startswith("call-deny"):
            call = ToolCallItem(0, "item-echo", "call-echo", "server__echo", '{"value":"hi"}')
            yield from _completed_stream(request, (call,), FinishReason.TOOL_CALLS, "r1b")
            return
        call_id = last.call_ref.call_id
        if call_id.startswith("call-echo"):
            call = ToolCallItem(0, "item-p1", "call-p1", "write_patch", '{"content":"v1"}')
            yield from _completed_stream(request, (call,), FinishReason.TOOL_CALLS, "r2")
            return
        if call_id.startswith("call-t") and last.is_error:
            call = ToolCallItem(0, "item-p2", "call-p2", "write_patch", '{"content":"v2"}')
            yield from _completed_stream(request, (call,), FinishReason.TOOL_CALLS, "r4")
            return
        if call_id.startswith("call-p"):
            call = ToolCallItem(0, "item-t", "call-t", "run_test", '{"expect":"v2"}')
            yield from _completed_stream(request, (call,), FinishReason.TOOL_CALLS, "r3")
            return
        item = AssistantTextItem(0, "item-final", "evidence final")
        yield from _completed_stream(request, (item,), FinishReason.STOP, "r5")


def _last_result(request):
    for item in reversed(request.input_items):
        if isinstance(item, ToolResultMessage):
            return item
    return None


def _build_tools(
    store, ledger, approvals, session, catalog, worktree, runner, fault_hook,
    trace, correlation_id, observations=None,
):
    observations = observations if observations is not None else {}
    registry = ToolRegistry()
    mcp_bindings = {}
    mcp_names = () if catalog is None else sorted(catalog.bindings)
    for name in mcp_names:
        binding = catalog.bindings[name]
        registry.register(binding.spec, session.handler(binding))
        mcp_bindings[name] = binding

    def write_handler(args: WriteArgs, *, context):
        (Path(worktree) / "solution.txt").write_text(args.content + "\n", encoding="utf-8")
        return ToolExecutionResult(f"wrote {args.content}")

    def test_handler(args: TestArgs, *, context):
        result: ContainerResult = runner.run(
            Path(worktree),
            ["/bin/sh", "-c", f"grep -q {args.expect} solution.txt"],
        )
        return ToolExecutionResult(
            "test passed" if result.exit_code == 0 else "test failed",
            result.exit_code != 0,
        )

    def deny_handler(args: DenyArgs, *, context):
        observations["deny_handler_calls"] = observations.get("deny_handler_calls", 0) + 1
        return ToolExecutionResult("unexpected deny probe execution")

    registry.register(WRITE_SPEC, write_handler)
    registry.register(TEST_SPEC, test_handler)
    registry.register(DENY_SPEC, deny_handler)
    principal = Principal("root", ("workspace.write", "mcp.use"))
    engine = PolicyEngine(
        "policy-v1",
        (
            PolicyRule(
                "deny-probe", Decision.DENY,
                action_kinds=(ActionKind.BUILTIN_TOOL,),
                tool_names=("deny_probe",), principal_ids=("root",),
            ),
            PolicyRule("mcp-allow", Decision.ALLOW, action_kinds=(ActionKind.MCP_TOOL,), principal_ids=("root",)),
            PolicyRule("builtin-allow", Decision.ALLOW, action_kinds=(ActionKind.BUILTIN_TOOL,), principal_ids=("root",)),
            PolicyRule(
                "write-approval", Decision.ASK,
                action_kinds=(ActionKind.BUILTIN_TOOL,), tool_names=("write_patch",),
                principal_ids=("root",),
            ),
        ),
    )

    write_resolution_count = 0

    def mcp_resolver(binding):
        def resolve(call, context, profile, previous):
            return ResolvedAction(
                kind=ActionKind.MCP_TOOL,
                tool_name=call.name,
                canonical_arguments_json=canonical_arguments(call.arguments_json),
                principal=principal,
                side_effect_class=SideEffectClass.READ_ONLY,
                sandbox_profile_id="mcp",
                policy_version="policy-v1",
                mcp_server_id=session.server_id,
                mcp_session_generation=session.generation,
                mcp_schema_hash=binding.schema_hash,
            )
        return resolve

    def builtin_resolver(call, context, profile, previous):
        nonlocal write_resolution_count
        side_effect = (
            SideEffectClass.IDEMPOTENT_WRITE
            if call.name == "write_patch"
            else SideEffectClass.READ_ONLY
        )
        if call.name == "write_patch":
            write_resolution_count += 1
            # First authorization asks for A.  On resume the existing grant
            # matches A, but the second resolution observes B and forces a
            # fresh ASK.  The following resume keeps B stable and can claim.
            sandbox_profile_id = (
                "builtin-write-stable" if write_resolution_count <= 2
                else "builtin-write-drifted"
            )
        else:
            sandbox_profile_id = "builtin"
        return ResolvedAction(
            kind=ActionKind.BUILTIN_TOOL,
            tool_name=call.name,
            canonical_arguments_json=canonical_arguments(call.arguments_json),
            principal=principal,
            side_effect_class=side_effect,
            sandbox_profile_id=sandbox_profile_id,
            policy_version="policy-v1",
        )

    resolvers = {
        name: mcp_resolver(catalog.bindings[name]) for name in mcp_names
    }
    resolvers["write_patch"] = builtin_resolver
    resolvers["run_test"] = builtin_resolver
    resolvers["deny_probe"] = builtin_resolver
    profiles = {name: READ_ONLY_PROFILE for name in mcp_names}
    profiles["write_patch"] = IDEMPOTENT_WRITE_PROFILE
    profiles["run_test"] = READ_ONLY_PROFILE
    profiles["deny_probe"] = READ_ONLY_PROFILE
    delegate = McpRegistryAdapter(registry, mcp_bindings)
    return LedgerExecutor(
        delegate,
        ledger,
        profiles,
        policy_engine=engine,
        approval_service=approvals,
        action_resolvers=resolvers,
        fault_hook=fault_hook,
        trace_store=trace,
        correlation_id=correlation_id,
    )


def main() -> int:
    store = SqliteEventStore(DB)
    runtime = ThreadRuntime(store, actor="golden")
    ledger = ToolLedgerStore(store)
    approvals = ApprovalService(store, ledger, budget_action_limits={"root": 20})
    trace = TraceStore(store)
    control = AgentControlPlane(store, limits=AgentBudgetLimits(max_total_agents=8))
    managed = REPO.parent / "managed"
    managed.mkdir(exist_ok=True)

    state = _load_state(STATE_FILE) if RESUME else None
    agent_id = UUID(state["agent_id"]) if state else uuid4()
    run_id = UUID(state["run_id"]) if state else uuid4()
    base_commit = state.get("base_commit") if state else _head(REPO)
    worktree = Path(state["worktree"]) if state else managed / str(agent_id)
    if not RESUME:
        workspace_store = AgentWorkspaceStore(store, managed_root=managed)
        worktree = WorktreeManager(workspace_store, repo_root=REPO).create(
            agent_id, run_id=run_id, base_commit=_head(REPO), branch="agent/golden", write_agent=True,
        )
        worktree = Path(worktree)

    runner = DockerContainerRunner(store, image_id=_IMAGE_ID())
    observations = {}

    session = None
    catalog = None
    if not RESUME:
        env = {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
        transport = StdioTransport(spawn_fixture_command(), env=env, cwd=str(REPO))
        session = McpSession("server", transport, request_timeout=5.0, trace_store=trace, correlation_id=run_id)
        catalog = session.connect()

    fault_hook = (
        _make_fault_hook(READY_FILE, KILL_POINT, session, store)
        if KILL_POINT else None
    )
    executor = _build_tools(
        store, ledger, approvals, session, catalog, worktree, runner, fault_hook,
        trace, run_id, observations,
    )
    completion_gate = CompositeCompletionGate(
        store, control, managed, repo=REPO, base_commit=base_commit,
        image_id=_IMAGE_ID(),
    )

    if RESUME:
        checkpoints = CheckpointStore(store)
        candidate = _wait_for_recoverable(
            RecoveryCoordinator(runtime, checkpoints, owner_id="golden-resume")
        )
        claimed = RecoveryCoordinator(runtime, checkpoints, owner_id="golden-resume").claim_stale(candidate, force=True)
        turn_worker = TurnWorker(
            runtime,
            AgentLoop(
                CompositeProvider(), tool_executor=executor, trace_store=trace,
                correlation_id=run_id, completion_gate=completion_gate,
            ),
            provider="test", model="model", checkpoint_store=checkpoints, owner_id="golden-resume",
        )
        result, approval_facts = _execute_with_approval_resumes(
            turn_worker, runtime, approvals, store,
            claimed.turn.turn_id, claimed.turn.version,
        )
    else:
        thread = runtime.create_thread("golden")
        queued = runtime.create_turn(thread.thread_id, "golden composite", expected_thread_version=thread.version)
        _write_state(STATE_FILE, {
            "turn_id": str(queued.turn_id), "agent_id": str(agent_id),
            "run_id": str(run_id), "worktree": str(worktree),
            "base_commit": _head(REPO),
        })
        turn_worker = TurnWorker(
            runtime,
            AgentLoop(
                CompositeProvider(), tool_executor=executor, trace_store=trace,
                correlation_id=run_id, completion_gate=completion_gate,
            ),
            provider="test", model="model", checkpoint_store=CheckpointStore(store), owner_id="golden",
            lease_seconds=5,
        )
        result, approval_facts = _execute_with_approval_resumes(
            turn_worker, runtime, approvals, store, queued.turn_id, queued.version,
        )

    # The post-recovery portion is itself durable: one read child and three
    # independently allocated write children produce accepted artifacts.  The
    # third child deliberately overlaps the first so conflict handling is
    # exercised before the non-overlapping pair is integrated and delivered.
    artifact_facts = completion_gate.facts
    # Approval evidence is rebuilt from the append-only streams, so the
    # resume process contributes the first process's grant/drift request too.
    approval_facts = _approval_facts_for_turn(store, result.turn.turn_id)
    artifact_facts["approval_facts"] = approval_facts
    artifact_facts["deny_handler_calls"] = observations.get("deny_handler_calls", 0)
    subagent_state = artifact_facts.get("read_agent_state") if artifact_facts else None

    solution = ""
    solution_path = Path(worktree) / "solution.txt"
    if solution_path.exists():
        solution = solution_path.read_text(encoding="utf-8").strip()
    if result.turn.status is TurnStatus.COMPLETED:
        # The root Turn worktree is also an effect-ledgered resource.  Reap it
        # only after reading the delivered evidence and after the child
        # artifacts have been integrated.
        WorktreeManager(
            AgentWorkspaceStore(store, managed_root=managed), repo_root=REPO,
        ).reap(agent_id, run_id=run_id, reason="golden-complete")
    # Re-open every projection from the durable store.  This is intentionally
    # assembled after all local handles/caches have gone out of scope: the
    # final oracle is EventStore + ledger + workspace inventory + Turn/Run,
    # never model text or state.json.
    rebuilt_store = SqliteEventStore(DB)
    rebuilt_turn = ThreadRuntime(rebuilt_store, actor="golden-oracle").get_turn(result.turn.turn_id)
    all_events = list(rebuilt_store.read_all(after_position=0, limit=10_000))
    execution_events = list(
        rebuilt_store.read_stream(
            StreamId("run-execution", result.turn.turn_id),
            after_version=-1, limit=10_000,
        )
    )
    replayed_trace = TraceStore(rebuilt_store).read(run_id)
    artifact_event_stream = (
        StreamId("golden-artifact", UUID(artifact_facts["enclosing_turn_run_id"]))
        if artifact_facts and artifact_facts.get("enclosing_turn_run_id") else None
    )
    artifact_events = (
        list(rebuilt_store.read_stream(artifact_event_stream, after_version=-1, limit=50))
        if artifact_event_stream is not None else []
    )
    run_started = [
        event.payload.get("run_id") for event in all_events
        if event.event_type == "run.started.v1" and event.payload.get("run_id")
    ]
    rebuilt_run = (
        ThreadRuntime(rebuilt_store, actor="golden-oracle").get_run(UUID(run_started[-1]))
        if run_started else None
    )
    rebuilt_inventory = {}
    for event in all_events:
        if event.event_type == "workspace.inventory-active.v2":
            rebuilt_inventory[event.payload.get("allocation_id")] = (
                event.payload.get("resource_ref"), "active"
            )
        elif event.event_type == "workspace.inventory-state.v2":
            allocation = event.payload.get("allocation_id")
            if allocation in rebuilt_inventory:
                rebuilt_inventory[allocation] = (
                    rebuilt_inventory[allocation][0], event.payload.get("state")
                )
    event_types = [event.event_type for event in all_events]
    approval_event_ids = [
        {
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "request_id": event.payload.get("request_id"),
        }
        for event in all_events
        if event.event_type in (
            "approval.requested.v1", "approval.granted.v1",
            "approval.denied.v1", "approval.expired.v1",
        )
    ]
    replayed_approval_facts = _approval_facts_for_turn(
        rebuilt_store, result.turn.turn_id,
    )
    evidence = {
        "turn_id": str(result.turn.turn_id),
        "status": result.turn.status.value,
        "turn_error": result.turn.error,
        "solution": solution,
        "trace_streams": sorted({r.stream for r in trace.read(run_id)}),
        "ledger_states": _ledger_states(store),
        "subagent_state": subagent_state,
        "artifact_facts": artifact_facts,
        "oracle": {
            "turn_status": rebuilt_turn.status.value,
            "run_id": str(rebuilt_turn.current_run_id) if rebuilt_turn.current_run_id else None,
            "event_count": len(all_events),
            "event_types": sorted(set(event_types)),
            "execution_event_count": len(execution_events),
            "checkpoint_event_types": sorted({event.event_type for event in execution_events}),
            "trace_record_count": len(replayed_trace),
            "trace_streams": sorted({record.stream for record in replayed_trace}),
            "run_replay": None if rebuilt_run is None else {
                "run_id": str(rebuilt_run.run_id),
                "status": rebuilt_run.status.value,
                "turn_id": str(rebuilt_run.turn_id),
            },
            "artifact_event_count": len(artifact_events),
            "artifact_fact_replay": (
                _plain_json(artifact_events[-1].payload) if artifact_events else None
            ),
            "approval_events": approval_event_ids,
            "approval_facts_replay": replayed_approval_facts,
            "ledger_states": _ledger_states(rebuilt_store),
            "workspace_inventory": sorted(
                resource for resource, state_value in rebuilt_inventory.values()
                if resource and state_value == "active"
            ),
            "workspace_records": len(
                AgentWorkspaceStore(rebuilt_store, managed_root=managed).list(agent_id)
            ),
        },
    }
    EVIDENCE_FILE and Path(EVIDENCE_FILE).write_text(json.dumps(evidence), encoding="utf-8")
    if session is not None:
        session.close()
    return 0 if result.turn.status is TurnStatus.COMPLETED else 2


def _wait_for_recoverable(coordinator, *, timeout_seconds: float = 15.0):
    """Wait for the killed worker's lease to become stale before takeover.

    Recovery discovery intentionally excludes active leases.  The parent test
    kills the first process and starts this process immediately, so a single
    list call races the short lease expiry.  Each poll asks the
    coordinator again (and therefore uses the event-store database clock),
    while the bound keeps a broken projection from hanging the fixture.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        candidates = coordinator.list_recoverable_turns()
        if candidates:
            return candidates[0]
        time.sleep(0.05)
    raise RuntimeError("golden_recoverable_turn_not_visible_after_expiry")


def _make_fault_hook(ready_file, kill_point, session_to_close, store):
    fired = False
    def hook(point, record):
        nonlocal fired
        if point == kill_point and record.tool_name == "run_test" and not fired:
            fired = True
            execution_events = store.read_stream(
                StreamId("run-execution", record.turn_id),
                after_version=-1, limit=500,
            )
            checkpointed = (
                any(
                    event.event_type == "run.context-seeded.v2"
                    and event.payload.get("run_id") == str(record.claimant_run_id)
                    for event in execution_events
                )
                and any(
                    event.event_type == "run.phase-advanced.v1"
                    and event.payload.get("phase") == "tool_in_progress"
                    and event.payload.get("run_id") == str(record.claimant_run_id)
                    for event in execution_events
                )
            )
            if not checkpointed:
                raise RuntimeError("golden_kill_before_checkpoint")
            if session_to_close is not None:
                session_to_close.close()
            if ready_file:
                Path(ready_file).write_text(str(record.tool_name), encoding="utf-8")
            threading.Event().wait(3600)
    return hook


def _execute_with_approval_resumes(worker, runtime, approvals, store, turn_id, version):
    """Drive real durable approval interrupts without leaving the Turn."""
    result = worker.execute(turn_id, version)
    approval_facts = []
    while result.turn.status is TurnStatus.WAITING_FOR_APPROVAL:
        pending = _pending_approval_for_turn(store, approvals, result.turn.turn_id)
        if pending is None:
            raise RuntimeError("golden_approval_pending_missing")
        waiting = runtime.get_turn(result.turn.turn_id)
        updated = approvals.resolve(
            pending, True,
            expected_approval_version=pending.version,
            expected_turn_version=waiting.version,
            interrupt_id=pending.interrupt_id,
            approver_principal_id="golden-operator",
            command_id=uuid4(),
        )
        resolution = [
            event for event in store.read_stream(
                StreamId("approval", pending.subject_id), after_version=-1, limit=100,
            )
            if event.event_type in (
                "approval.granted.v1", "approval.denied.v1", "approval.expired.v1",
            )
        ][-1]
        approval_facts.append({
            "request_id": str(updated.request_id),
            "subject_id": str(updated.subject_id),
            "decision": updated.status.value,
            "request_event_id": str(next(
                event.event_id for event in store.read_stream(
                    StreamId("approval", pending.subject_id), after_version=-1, limit=100,
                ) if event.event_type == "approval.requested.v1"
                and event.payload.get("request_id") == str(updated.request_id)
            )),
            "resolution_event_id": str(resolution.event_id),
            "run_id": str(result.turn.current_run_id),
        })
        queued = runtime.get_turn(result.turn.turn_id)
        result = worker.execute(queued.turn_id, queued.version)
    return result, approval_facts


def _pending_approval_for_turn(store, approvals, turn_id):
    candidates = {}
    for event in store.read_all(after_position=0, limit=10_000):
        if event.event_type != "approval.requested.v1":
            continue
        if event.payload.get("turn_id") != str(turn_id):
            continue
        subject_id = UUID(event.payload["subject_id"])
        record = approvals.load(subject_id)
        if record is not None and record.status is ApprovalStatus.PENDING:
            candidates[record.request_id] = record
    if not candidates:
        return None
    return candidates[sorted(candidates, key=str)[-1]]


def _approval_facts_for_turn(store, turn_id):
    """Rebuild every approval request/resolution pair for one durable Turn."""
    requested = []
    for event in store.read_all(after_position=0, limit=10_000):
        if (
            event.event_type == "approval.requested.v1"
            and event.payload.get("turn_id") == str(turn_id)
        ):
            requested.append(event)
    facts = []
    for request in requested:
        subject_id = UUID(request.payload["subject_id"])
        related = [
            event for event in store.read_stream(
                StreamId("approval", subject_id), after_version=-1, limit=100,
            )
            if event.payload.get("request_id") == request.payload.get("request_id")
            and event.event_type in (
                "approval.granted.v1", "approval.denied.v1", "approval.expired.v1",
            )
        ]
        resolution_facts = [
            {
                "decision": event.event_type.rsplit(".", 2)[1],
                "event_id": str(event.event_id),
                "run_id": (
                    None if event.metadata.run_id is None
                    else str(event.metadata.run_id)
                ),
            }
            for event in related
        ]
        facts.append({
            "request_id": request.payload["request_id"],
            "subject_id": request.payload["subject_id"],
            "action_digest": request.payload["action_digest"],
            "requested_run_id": (
                None if request.metadata.run_id is None else str(request.metadata.run_id)
            ),
            "request_event_id": str(request.event_id),
            "decision": (
                "granted" if any(item["decision"] == "granted" for item in resolution_facts)
                else (resolution_facts[-1]["decision"] if resolution_facts else None)
            ),
            "resolution_events": resolution_facts,
        })
    return facts


class CompositeCompletionGate:
    """Run delivery before the enclosing durable Turn may complete."""

    def __init__(self, store, control, managed, *, repo, base_commit, image_id):
        self._store = store
        self._control = control
        self._managed = managed
        self._repo = repo
        self._base_commit = base_commit
        self._image_id = image_id
        self.facts = {}

    def assert_complete(self, run_id):
        if not self.facts:
            try:
                self.facts = _run_subagents_and_delivery(
                    self._store, self._control, self._managed,
                    repo=self._repo, base_commit=self._base_commit,
                    image_id=self._image_id, turn_run_id=run_id,
                )
            except Exception as exc:
                self.facts = {
                    "completion_error": getattr(exc, "code", type(exc).__name__),
                }
                raise


def _run_subagents_and_delivery(
    store, control, managed, *, base_commit, image_id, repo=REPO, turn_run_id=None,
):
    """Run real durable children and integrate their independently fenced worktrees."""
    root = control.spawn_agent(
        parent_agent_id=None, task_id="golden-root", principal_id="root",
        scopes=("read", "workspace.write"), context_mode=ContextMode.FRESH,
    )
    read = control.spawn_agent(
        parent_agent_id=root.agent_id, task_id="review",
        principal_id="reviewer", scopes=("read",), context_mode=ContextMode.FRESH,
    )
    writer_a = control.spawn_agent(
        parent_agent_id=root.agent_id, task_id="write-alpha",
        principal_id="writer-a", scopes=("read", "workspace.write"),
        context_mode=ContextMode.FRESH,
    )
    writer_b = control.spawn_agent(
        parent_agent_id=root.agent_id, task_id="write-beta",
        principal_id="writer-b", scopes=("read", "workspace.write"),
        context_mode=ContextMode.FRESH,
    )
    writer_conflict = control.spawn_agent(
        parent_agent_id=root.agent_id, task_id="write-conflict",
        principal_id="writer-c", scopes=("read", "workspace.write"),
        context_mode=ContextMode.FRESH,
    )
    control.send_message(
        read.agent_id, from_agent_id=root.agent_id, kind=MessageKind.TASK,
        body_ref="review", idempotency_key="golden-review-1",
    )
    for child, task in (
        (writer_a, "write-alpha"), (writer_b, "write-beta"),
        (writer_conflict, "write-conflict"),
    ):
        control.send_message(
            child.agent_id, from_agent_id=root.agent_id, kind=MessageKind.TASK,
            body_ref=task, idempotency_key=f"golden-{task}-1",
        )
    control.send_message(
        root.agent_id, from_agent_id=None, kind=MessageKind.TASK,
        body_ref="golden-root-complete", idempotency_key="golden-root-1",
    )

    workspace = AgentWorkspaceStore(store, managed_root=managed)
    manager = WorktreeManager(workspace, repo_root=repo)

    class WritingAgentProvider:
        """Provider-side write action: the agent allocates and edits its tree."""
        def __init__(self, agent_id, filename, content):
            self.agent_id, self.filename, self.content = agent_id, filename, content

        def run(self, task, *, tool_allowlist):
            record = control.graph.load(self.agent_id)
            if record is None or record.run_id is None:
                raise RuntimeError("writing_agent_run_missing")
            tree = manager.create(
                self.agent_id, run_id=record.run_id, base_commit=base_commit,
                branch=f"agent/golden-{self.filename}", write_agent=True,
            )
            Path(tree, self.filename).write_text(self.content, encoding="utf-8")
            return "workspace-written"

    scheduler = lambda script: AgentScheduler(
        control, provider=ScriptedAgentProvider(script), lease_seconds=30,
    )
    records = {}
    artifacts = {}

    def capture_and_reap(label, child, evidence):
        record = control.graph.load(child.agent_id)
        attempt_run_id = _agent_attempt_run_id(store, child.agent_id)
        if record is None or attempt_run_id is None or record.state is not AgentState.COMPLETED:
            raise RuntimeError(f"golden_writer_not_completed:{label}")
        records[label] = (record, attempt_run_id)
        artifacts[label] = Artifact(
            child.agent_id, attempt_run_id, base_commit, base_commit,
            manager.diff(child.agent_id, base_commit=base_commit, run_id=attempt_run_id),
            evidence, image_id,
        )
        manager.reap(child.agent_id, run_id=attempt_run_id, reason="golden-captured")

    read_result = scheduler({"review": "tool:read_file"}).run_attempt(read.agent_id)
    a_result = AgentScheduler(
        control, provider=WritingAgentProvider(writer_a.agent_id, "alpha.txt", "alpha-from-agent-a\n"),
        lease_seconds=30,
    ).run_attempt(writer_a.agent_id)
    capture_and_reap("a", writer_a, "docker:a:passed")
    b_result = AgentScheduler(
        control, provider=WritingAgentProvider(writer_b.agent_id, "beta.txt", "beta-from-agent-b\n"),
        lease_seconds=30,
    ).run_attempt(writer_b.agent_id)
    capture_and_reap("b", writer_b, "docker:b:passed")
    c_result = AgentScheduler(
        control, provider=WritingAgentProvider(writer_conflict.agent_id, "alpha.txt", "alpha-conflict\n"),
        lease_seconds=30,
    ).run_attempt(writer_conflict.agent_id)
    capture_and_reap("c", writer_conflict, "docker:c:passed")
    root_result = scheduler({"golden-root-complete": "ok"}).run_attempt(root.agent_id)

    # Exercise a genuine stale attempt: an old AgentScheduler process enters
    # provider code and dies after delivery, then a fresh lease owner takes it
    # over.  The old run's late result must be fenced before any terminal
    # mailbox fact is accepted.
    late_agent = control.spawn_agent(
        parent_agent_id=None, task_id="late-worker", principal_id="late-worker",
        scopes=("read",), context_mode=ContextMode.FRESH,
    )
    late_message = control.send_message(
        late_agent.agent_id, from_agent_id=None, kind=MessageKind.TASK,
        body_ref="late-worker", idempotency_key="golden-late-worker-1",
    )

    class LateWorkerProcessDeath(BaseException):
        pass

    class CrashingAgentProvider:
        def run(self, task, *, tool_allowlist):
            raise LateWorkerProcessDeath()

    try:
        AgentScheduler(
            control, provider=CrashingAgentProvider(), lease_seconds=30,
        ).run_attempt(late_agent.agent_id)
    except LateWorkerProcessDeath:
        pass
    late_running = control.graph.load(late_agent.agent_id)
    if late_running is None or late_running.run_id is None:
        raise RuntimeError("golden_late_worker_did_not_enter_provider")
    late_old_run_id = late_running.run_id
    original_clock = control._clock
    control._clock = lambda: original_clock() + timedelta(hours=1)
    try:
        orphans = control.discover_orphans()
        if not any(item.agent_id == late_agent.agent_id for item in orphans):
            raise RuntimeError("golden_late_worker_was_not_orphaned")
        late_orphan = control.graph.load(late_agent.agent_id)
        late_taken_over = control.start_attempt(
            late_agent.agent_id, expected_version=late_orphan.version,
            lease_seconds=30,
        )
        late_takeover_run_id = late_taken_over.run_id
    finally:
        control._clock = original_clock
    late_message = control.mailbox.load(late_agent.agent_id)[0]
    try:
        control.record_message_result(
            late_agent.agent_id, late_message.message_id, run_id=late_old_run_id,
            expected_delivery_attempt=late_message.delivery_attempt, outcome="late",
        )
    except Exception as exc:
        late_result_fence = getattr(exc, "code", type(exc).__name__)
    else:
        raise RuntimeError("golden_late_worker_result_was_accepted")
    cancelled_late = control.cancel_message(
        late_agent.agent_id, late_message.message_id,
        expected_delivery_attempt=late_message.delivery_attempt,
        decision_id=uuid4(), actor=AgentPrincipal("golden-operator", ("agents.resolve:any",)),
        approval_id=None, reason="late-worker-test-cleanup",
    )
    late_final = control.graph.load(late_agent.agent_id)
    snapshot = control.mailbox.snapshot(late_agent.agent_id)
    result_ref, result_digest = terminal_result_identity(
        late_agent.agent_id, late_final.run_id, AgentState.CANCELLED,
        "late-worker-test-cleanup", snapshot.messages,
    )
    control.terminal(
        late_agent.agent_id, run_id=late_final.run_id,
        expected_attempt=late_final.attempt, state=AgentState.CANCELLED,
        reason="late-worker-test-cleanup", result_ref=result_ref,
        result_digest=result_digest, source_message_ids=(),
    )

    # A malformed model turn must be rejected before the registry/transport is
    # entered.  Keep a count in the oracle rather than trusting the exception
    # message alone.
    class BadModel:
        def stream(self, request):
            bad = ToolCallItem(0, "bad-item", "bad-call", "not_registered", "{}")
            yield from _completed_stream(request, (bad,), FinishReason.TOOL_CALLS, "bad")

    class NoopExecutor:
        def __init__(self):
            self.calls = 0

        def definitions(self):
            return ()

        def execute(self, call, *, context):
            self.calls += 1
            raise AssertionError("bad model entered tool executor")

    bad_executor = NoopExecutor()
    try:
        AgentLoop(BadModel(), tool_executor=bad_executor).run(
            run_id=uuid4(), input_items=(), provider="test", model="bad",
        )
    except AgentLoopError as exc:
        if exc.code != "unknown_tool_requested":
            raise
    else:
        raise RuntimeError("golden_bad_model_was_accepted")
    bad_calls = bad_executor.calls

    integrator = ArtifactIntegrator(
        repo_root=repo, integration_root=managed / "golden-integration",
        runner=DockerContainerRunner(store, image_id=image_id),
    )
    accepted = [
        integrator.accept(artifacts[label], expected_run_id=records[label][1],
                          expected_base_commit=base_commit)
        for label in ("a", "b", "c")
    ]
    conflict_code = None
    try:
        integrator.integrate([artifacts["a"], artifacts["c"]],
                             test_argv=["/bin/sh", "-c", "true"])
    except Exception as exc:
        conflict_code = getattr(exc, "code", type(exc).__name__)
    if conflict_code != "artifact_conflict":
        raise RuntimeError(f"golden_expected_artifact_conflict:{conflict_code}")
    test_result, integrated_head = integrator.integrate(
        [artifacts["a"], artifacts["b"]],
        test_argv=["/bin/sh", "-c", "grep -q alpha-from-agent-a alpha.txt && grep -q beta-from-agent-b beta.txt"],
    )
    integrator.deliver([artifacts["a"], artifacts["b"]], user_base_commit=base_commit)
    # A result from an old worker/run is fenced at the control-plane boundary;
    # no late mailbox result can become durable after terminalization.
    delivered_diff = subprocess.run(
        ["git", "-C", str(repo), "diff", "--binary", base_commit],
        capture_output=True, text=True, check=True,
    ).stdout
    facts = {
        "read_agent_state": read_result.state.value,
        "read_agent_terminal": read_result.summary,
        "writer_states": [a_result.state.value, b_result.state.value, c_result.state.value],
        "root_agent_state": root_result.state.value,
        "accepted_artifacts": accepted,
        "artifact_conflict": conflict_code,
        "integrated_head": integrated_head,
        "integration_test_exit": test_result.exit_code,
        "bad_model_tool_calls": bad_calls,
        "late_result_fence": late_result_fence,
        "late_result_run_id": str(late_old_run_id),
        "late_takeover_run_id": str(late_takeover_run_id),
        "delivered_files": {
            "alpha.txt": (repo / "alpha.txt").read_text(encoding="utf-8").strip(),
            "beta.txt": (repo / "beta.txt").read_text(encoding="utf-8").strip(),
        },
        "artifact_diff_sha256": {
            label: hashlib.sha256(artifacts[label].diff.encode("utf-8")).hexdigest()
            for label in ("a", "b", "c")
        },
        "writer_run_ids": {
            label: str(records[label][1]) for label in ("a", "b", "c")
        },
        "delivered_diff_sha256": hashlib.sha256(delivered_diff.encode("utf-8")).hexdigest(),
        "delivered_diff_files": sorted(
            line for line in subprocess.run(
                ["git", "-C", str(repo), "diff", "--name-only", base_commit],
                capture_output=True, text=True, check=True,
            ).stdout.splitlines() if line
        ),
        "enclosing_turn_run_id": None if turn_run_id is None else str(turn_run_id),
    }
    if turn_run_id is not None:
        _record_artifact_fact(store, turn_run_id, facts)
    return facts


def _record_artifact_fact(store, turn_run_id, facts):
    """Persist delivery/diff evidence so the restart oracle can replay it."""
    stream = StreamId("golden-artifact", turn_run_id)
    existing = store.read_stream(stream, after_version=-1, limit=10)
    if existing:
        return
    command_id = uuid5(NAMESPACE_URL, f"koawa-v2:golden-artifact:{turn_run_id}")
    event = NewEvent(
        uuid5(command_id, "delivered"), "golden.artifact-delivered.v1", 1,
        datetime.now(timezone.utc),
        {
            "turn_run_id": str(turn_run_id),
            "artifact_ids": list(facts["accepted_artifacts"]),
            "artifact_diff_sha256": dict(facts["artifact_diff_sha256"]),
            "delivered_diff_sha256": facts["delivered_diff_sha256"],
            "delivered_diff_files": list(facts["delivered_diff_files"]),
            "artifact_conflict": facts["artifact_conflict"],
            "integrated_head": facts["integrated_head"],
            "integration_test_exit": facts["integration_test_exit"],
        },
        EventMetadata(
            command_id, turn_run_id, run_id=turn_run_id, actor="golden-artifact",
        ),
    )
    store.append_batch(
        (StreamWrite(stream, -1, (event,)),), idempotency_key=command_id,
    )


def _head(repo):
    import subprocess
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()


def _agent_attempt_run_id(store, agent_id):
    """Read the terminal attempt's run identity from its append-only stream."""
    events = store.read_stream(StreamId("agent", agent_id), after_version=-1, limit=500)
    started = [
        event.payload.get("run_id")
        for event in events
        if event.event_type in ("agent.started.v1", "agent.started.v2") and event.payload.get("run_id")
    ]
    return UUID(started[-1]) if started else None


def _IMAGE_ID():
    return "sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"


def _write_state(path, state):
    Path(path).write_text(json.dumps(state), encoding="utf-8")


def _load_state(path):
    if path and Path(path).exists():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return None


def _ledger_states(store):
    from koawa_agent_v2.ledger import ToolLedgerStore
    ledger = ToolLedgerStore(store)
    states = []
    for e in store.read_all(after_position=0, limit=5000):
        if e.event_type in ("tool.execution-claimed.v1", "tool.execution-succeeded.v1", "tool.execution-failed.v1"):
            states.append(e.event_type)
    return states


def _plain_json(value):
    """Convert immutable event payload containers into JSON-native values."""
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
