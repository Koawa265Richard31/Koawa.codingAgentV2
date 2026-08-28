"""D15 unified runtime: turn + subagents + context + compaction + trace."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from ..agents.control import AgentBudgetLimits, AgentControlPlane
from ..agents.graph import AgentState, ContextMode
from ..agents.messages import MessageKind
from ..agents.scheduler import AgentScheduler, ScriptedAgentProvider
from ..approval_service import ApprovalService
from ..context.compaction import (
    AuthoritativeProjection,
    Compactor,
    ToolCallPair,
    rebuild_after_restart,
)
from ..context.budget import ContextBudget
from ..context.retrieval import ContextRetriever
from ..control.runtime import ThreadRuntime
from ..control.sqlite_store import SqliteEventStore
from ..ledger import ToolLedgerStore
from ..context.index import RepositoryIndex
from ..telemetry.trace import BestEffortTraceSink, TraceProbe, TraceStore
from .truth import RuntimeTruthDocument, RuntimeTruthVerifier


@dataclass(frozen=True, slots=True)
class UnifiedResult:
    turn_id: UUID
    turn_status: str
    context_items: tuple[str, ...]
    child_states: tuple[str, ...]
    compacted: str
    trace_streams: tuple[str, ...]
    thread_id: UUID | None = None
    run_id: UUID | None = None
    evidence_digest: str | None = None


class UnifiedAgentRuntime:
    """Compose the D1–D15 slices into one runnable durable task flow."""

    def __init__(self, *, db: Path, repo: Path) -> None:
        self.repo = Path(repo)
        self.db = Path(db)
        self.store = SqliteEventStore(self.db)
        self.runtime = ThreadRuntime(self.store, actor="unified")
        self.ledger = ToolLedgerStore(self.store)
        self.approvals = ApprovalService(
            self.store, self.ledger, budget_action_limits={"root": 20}
        )
        self.trace = TraceStore(self.store)
        self.trace_sink = BestEffortTraceSink(self.trace)
        self.correlation_id = uuid4()
        self.control = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(
                max_depth=3, max_total_agents=8, max_concurrent_children=4
            ),
        )
        self.index = RepositoryIndex(self.repo)
        self.retriever = ContextRetriever(
            self.index, budget=ContextBudget(max_chars=40_000)
        )
        self.compactor = Compactor(
            system_instructions="system",
            developer_instructions="developer",
        )

    def execute(self, goal: str) -> UnifiedResult:
        self.trace_sink.emit(
            TraceProbe(
                self.correlation_id,
                "model",
                "run",
                {"kind": "run", "result_code": "ok"},
            )
        )
        files = self.index.list_files()
        context_items = self.retriever.retrieve(query=goal, files=files)
        self.trace_sink.emit(
            TraceProbe(
                self.correlation_id,
                "subagent",
                "spawn",
                {"kind": "spawn", "attempt": 1},
            )
        )
        root = self.control.spawn_agent(
            parent_agent_id=None,
            task_id="root",
            principal_id="root",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        child = self.control.spawn_agent(
            parent_agent_id=root.agent_id,
            task_id=goal,
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        self.control.send_message(
            child.agent_id,
            from_agent_id=root.agent_id,
            kind=MessageKind.TASK,
            body_ref=goal,
            idempotency_key=f"{goal}:1",
        )
        scheduler = AgentScheduler(
            self.control,
            provider=ScriptedAgentProvider({goal: "tool:read_file"}),
            lease_seconds=30,
        )
        scheduler.run_attempt(child.agent_id)
        children = self.control.wait_agents(root.agent_id, timeout_seconds=1)
        child_states = tuple(item["state"] for item in children)

        projection = AuthoritativeProjection(
            user_goal=goal,
            constraints=("no network",),
            changed_files=(),
            test_evidence="context retrieved",
            pending_approval=None,
            unknown_outcome=None,
            active_children=tuple(str(child.agent_id) for _ in children),
            budget="ok",
        )
        compacted = self.compactor.compact(
            summary=f"worked on {goal}",
            projection=projection,
            pairs=(ToolCallPair("call-1", "read_file", True),),
        )
        rebuild_after_restart(
            summary=f"worked on {goal}",
            projection=projection,
            tail_events=("agent.completed.v1",),
        )
        truth = self._durable_turn(goal)
        streams = tuple(
            sorted({record.stream for record in self.trace.read(self.correlation_id)})
        )
        return UnifiedResult(
            turn_id=truth.turn.turn_id,
            turn_status=truth.turn.status.value,
            context_items=tuple(item.path for item in context_items),
            child_states=child_states,
            compacted=compacted,
            trace_streams=streams,
            thread_id=truth.thread.thread_id,
            run_id=None if truth.run is None else truth.run.run_id,
            evidence_digest=(
                None if truth.completion_evidence is None
                else truth.completion_evidence.evidence_digest
            ),
        )

    def _durable_turn(self, goal: str) -> RuntimeTruthDocument:
        thread = self.runtime.create_thread("unified-task")
        queued = self.runtime.create_turn(
            thread.thread_id,
            goal,
            expected_thread_version=thread.version,
        )
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        if running.current_run_id is None:
            raise RuntimeError("unified running Turn has no Run")
        evidence = self.runtime.record_completion_evidence(
            running.turn_id, run_id=running.current_run_id,
            final_text=f"completed:{goal}",
        )
        self.runtime.complete_turn(
            running.turn_id, f"completed:{goal}",
            expected_version=running.version, run_id=running.current_run_id,
            evidence_ref=evidence,
        )
        return RuntimeTruthVerifier(self.runtime, self.store).read(running.turn_id)
