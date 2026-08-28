"""I7 reconstructed business truth for App and Unified responses."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ..agents.graph import AgentError
from ..control.event_store import StreamId
from ..control.run_effects import RunEffectIndex
from ..control.models import (
    CompletionEvidenceRef, LegacyRunState, RunState, ThreadState, TurnState,
    TurnStatus,
)


@dataclass(frozen=True, slots=True)
class RuntimeTruthDocument:
    thread: ThreadState
    turn: TurnState
    run: RunState | LegacyRunState | None
    completion_evidence: CompletionEvidenceRef | None
    ledger_uncertain: bool = False
    workspace_uncertain: bool = False
    projection_stale: bool = False

    @property
    def outcome_code(self) -> str:
        if self.ledger_uncertain or self.workspace_uncertain:
            return "runtime_outcome_unknown"
        return f"turn_{self.turn.status.value}"


class RuntimeTruthVerifier:
    def __init__(self, runtime, event_store) -> None:
        self.runtime = runtime
        self.event_store = event_store

    def read(self, turn_id: UUID) -> RuntimeTruthDocument:
        turn = self.runtime.get_turn(turn_id)
        thread = self.runtime.get_thread(turn.thread_id)
        run_id = turn.current_run_id or self._last_run_id(turn.turn_id)
        run = self.runtime.get_run(run_id) if run_id is not None else None
        evidence = run.completion_evidence if isinstance(run, RunState) else None
        ledger_uncertain = False
        workspace_uncertain = False
        if isinstance(run, RunState):
            try:
                inspection = RunEffectIndex(self.event_store).inspect(run.run_id)
                ledger_uncertain = any(
                    ref.effect_kind in {"tool", "mcp-allocation"}
                    for ref in inspection.open_effects
                )
                workspace_uncertain = any(
                    ref.effect_kind == "workspace" for ref in inspection.open_effects
                )
            except (TypeError, ValueError):
                ledger_uncertain = True
                workspace_uncertain = True
        document = RuntimeTruthDocument(
            thread, turn, run, evidence, ledger_uncertain, workspace_uncertain
        )
        self.verify(document)
        return document

    def verify(self, document: RuntimeTruthDocument) -> None:
        turn, thread, run = document.turn, document.thread, document.run
        if turn.is_terminal:
            if thread.active_turn_id is not None:
                raise AgentError("runtime_outcome_unknown")
            if run is not None and not run.is_terminal:
                raise AgentError("runtime_outcome_unknown")
            if document.ledger_uncertain or document.workspace_uncertain:
                raise AgentError("runtime_outcome_unknown")
        elif thread.active_turn_id != turn.turn_id:
            raise AgentError("runtime_outcome_unknown")
        if turn.status is TurnStatus.COMPLETED and isinstance(run, RunState):
            if document.completion_evidence is None:
                raise AgentError("runtime_outcome_unknown")
            self._verify_evidence(run.run_id, turn.turn_id, document.completion_evidence)

    def _verify_evidence(
        self, run_id: UUID, turn_id: UUID, reference: CompletionEvidenceRef
    ) -> None:
        page = self.event_store.read_stream(
            reference.stream_id, after_version=reference.stream_version - 1, limit=1
        )
        if len(page) != 1:
            raise AgentError("runtime_outcome_unknown")
        event = page[0]
        if (
            event.stream_version != reference.stream_version
            or event.event_id != reference.event_id
            or event.event_type != "run.final-output-recorded.v1"
            or event.payload.get("run_id") != str(run_id)
            or event.payload.get("turn_id") != str(turn_id)
            or event.payload.get("evidence_digest") != reference.evidence_digest
        ):
            raise AgentError("runtime_outcome_unknown")

    def _last_run_id(self, turn_id: UUID) -> UUID | None:
        cursor = -1
        result = None
        stream = StreamId("turn", turn_id)
        while True:
            page = self.event_store.read_stream(stream, after_version=cursor, limit=500)
            for event in page:
                if event.event_type == "turn.started.v1":
                    result = UUID(event.payload["run_id"])
            if len(page) < 500:
                return result
            cursor = page[-1].stream_version


__all__ = ["RuntimeTruthDocument", "RuntimeTruthVerifier"]
