"""Re-entrant worker for the D15 golden composite E2E (kill + resume)."""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.agents.graph import ContextMode
from koawa_agent_v2.agents.messages import MessageKind
from koawa_agent_v2.agents.scheduler import AgentScheduler, ScriptedAgentProvider
from koawa_agent_v2.approval_service import ApprovalService
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
            call = ToolCallItem(0, "item-echo", "call-echo", "server__echo", '{"value":"hi"}')
            yield from _completed_stream(request, (call,), FinishReason.TOOL_CALLS, "r1")
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


def _build_tools(store, ledger, approvals, session, catalog, worktree, runner, fault_hook, trace, correlation_id):
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

    registry.register(WRITE_SPEC, write_handler)
    registry.register(TEST_SPEC, test_handler)
    principal = Principal("root", ("workspace.write", "mcp.use"))
    engine = PolicyEngine(
        "policy-v1",
        (
            PolicyRule("mcp-allow", Decision.ALLOW, action_kinds=(ActionKind.MCP_TOOL,), principal_ids=("root",)),
            PolicyRule("builtin-allow", Decision.ALLOW, action_kinds=(ActionKind.BUILTIN_TOOL,), principal_ids=("root",)),
        ),
    )

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
        side_effect = (
            SideEffectClass.IDEMPOTENT_WRITE
            if call.name == "write_patch"
            else SideEffectClass.READ_ONLY
        )
        return ResolvedAction(
            kind=ActionKind.BUILTIN_TOOL,
            tool_name=call.name,
            canonical_arguments_json=canonical_arguments(call.arguments_json),
            principal=principal,
            side_effect_class=side_effect,
            sandbox_profile_id="builtin",
            policy_version="policy-v1",
        )

    resolvers = {
        name: mcp_resolver(catalog.bindings[name]) for name in mcp_names
    }
    resolvers["write_patch"] = builtin_resolver
    resolvers["run_test"] = builtin_resolver
    profiles = {name: READ_ONLY_PROFILE for name in mcp_names}
    profiles["write_patch"] = IDEMPOTENT_WRITE_PROFILE
    profiles["run_test"] = READ_ONLY_PROFILE
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
    worktree = managed / str(agent_id)
    if not RESUME:
        workspace_store = AgentWorkspaceStore(store, managed_root=managed)
        WorktreeManager(workspace_store, repo_root=REPO).create(
            agent_id, run_id=run_id, base_commit=_head(REPO), branch="agent/golden", write_agent=True,
        )

    runner = DockerContainerRunner(store, image_id=_IMAGE_ID())

    session = None
    catalog = None
    if not RESUME:
        env = {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
        transport = StdioTransport(spawn_fixture_command(), env=env, cwd=str(REPO))
        session = McpSession("server", transport, request_timeout=5.0, trace_store=trace, correlation_id=run_id)
        catalog = session.connect()

    fault_hook = _make_fault_hook(READY_FILE, KILL_POINT, session) if KILL_POINT else None
    executor = _build_tools(store, ledger, approvals, session, catalog, worktree, runner, fault_hook, trace, run_id)

    if RESUME:
        checkpoints = CheckpointStore(store)
        candidate = RecoveryCoordinator(runtime, checkpoints, owner_id="golden-resume").list_recoverable_turns()[0]
        claimed = RecoveryCoordinator(runtime, checkpoints, owner_id="golden-resume").claim_stale(candidate, force=True)
        result = TurnWorker(
            runtime,
            AgentLoop(CompositeProvider(), tool_executor=executor, trace_store=trace, correlation_id=run_id),
            provider="test", model="model", checkpoint_store=checkpoints, owner_id="golden-resume",
        ).execute(claimed.turn.turn_id, claimed.turn.version)
    else:
        thread = runtime.create_thread("golden")
        queued = runtime.create_turn(thread.thread_id, "golden composite", expected_thread_version=thread.version)
        _write_state(STATE_FILE, {"turn_id": str(queued.turn_id), "agent_id": str(agent_id), "run_id": str(run_id)})
        result = TurnWorker(
            runtime,
            AgentLoop(CompositeProvider(), tool_executor=executor, trace_store=trace, correlation_id=run_id),
            provider="test", model="model", checkpoint_store=CheckpointStore(store), owner_id="golden",
        ).execute(queued.turn_id, queued.version)

    # Read-only sub-agent reviews the final worktree (D11 in the loop).
    subagent_state = None
    if result.turn.status is TurnStatus.COMPLETED:
        root = control.spawn_agent(parent_agent_id=None, task_id="root", principal_id="root", scopes=("read",), context_mode=ContextMode.FRESH)
        child = control.spawn_agent(parent_agent_id=root.agent_id, task_id="review", principal_id="worker", scopes=("read",), context_mode=ContextMode.FRESH)
        control.send_message(child.agent_id, from_agent_id=root.agent_id, kind=MessageKind.TASK, body_ref="review", idempotency_key="review-1")
        AgentScheduler(control, provider=ScriptedAgentProvider({"review": "tool:read_file"}), lease_seconds=30).run_attempt(child.agent_id)
        subagent_state = control.graph.load(child.agent_id).state.value

    solution = ""
    solution_path = Path(worktree) / "solution.txt"
    if solution_path.exists():
        solution = solution_path.read_text(encoding="utf-8").strip()
    evidence = {
        "turn_id": str(result.turn.turn_id),
        "status": result.turn.status.value,
        "turn_error": result.turn.error,
        "solution": solution,
        "trace_streams": sorted({r.stream for r in trace.read(run_id)}),
        "ledger_states": _ledger_states(store),
        "subagent_state": subagent_state,
    }
    EVIDENCE_FILE and Path(EVIDENCE_FILE).write_text(json.dumps(evidence), encoding="utf-8")
    if session is not None:
        session.close()
    return 0 if result.turn.status is TurnStatus.COMPLETED else 2


def _make_fault_hook(ready_file, kill_point, session_to_close):
    fired = False
    def hook(point, record):
        nonlocal fired
        if point == kill_point and record.tool_name == "run_test" and not fired:
            fired = True
            if session_to_close is not None:
                session_to_close.close()
            if ready_file:
                Path(ready_file).write_text(str(record.tool_name), encoding="utf-8")
            threading.Event().wait(3600)
    return hook


def _head(repo):
    import subprocess
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()


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


if __name__ == "__main__":
    raise SystemExit(main())
