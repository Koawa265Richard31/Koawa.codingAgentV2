"""Discovery and safe stale-run takeover orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence
from uuid import UUID, uuid4

from ..control.event_store import StreamId
from ..control.models import TurnStatus
from ..control.runtime import ThreadRuntime
from .context import ReconstructedContext, checkpoint_state, reconstruct_execution
from .protocol import CheckpointError, RunPhase, event_hash
from .store import CheckpointStore, RecoverableTurn, RunLease


class AutomaticRecoveryBlocked(RuntimeError): pass


class ToolRecoveryPort(Protocol):
    def reconcile_pending(
        self,
        turn_id: UUID,
        pending_tool_calls: Sequence[Mapping[str, object]],
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class RecoveryClaim:
    turn: object
    lease: RunLease | None
    context: ReconstructedContext


class RecoveryCoordinator:
    def __init__(
        self,
        runtime: ThreadRuntime,
        checkpoints: CheckpointStore,
        *,
        owner_id: str,
        lease_seconds: int = 30,
        tool_recovery: ToolRecoveryPort | None = None,
    ) -> None:
        if tool_recovery is not None and not callable(
            getattr(tool_recovery, "reconcile_pending", None)
        ):
            raise TypeError("tool_recovery must implement reconcile_pending")
        self.runtime, self.checkpoints, self.owner_id, self.lease_seconds = runtime, checkpoints, owner_id, lease_seconds
        self.tool_recovery = tool_recovery

    def list_recoverable_turns(self) -> tuple[RecoverableTurn, ...]:
        return self.checkpoints.list_recoverable_turns()

    def reconstruct(self, item: RecoverableTurn) -> ReconstructedContext:
        stream = StreamId("run-execution", item.turn_id)
        try:
            cp = self.checkpoints.load(item.turn_id)
        except CheckpointError:
            cp = None
        if cp is not None:
            covered_page = self.checkpoints.event_store.read_stream(stream, after_version=cp.execution_version - 1, limit=1)
            if (cp.turn_id != item.turn_id or cp.thread_id != item.thread_id or
                cp.turn_version != item.turn_version or len(covered_page) != 1):
                cp = None
            else:
                covered = covered_page[0]
                if (cp.covered_global_position != covered.global_position or
                    cp.covered_commit_id != covered.commit_id or
                    cp.covered_event_hash != event_hash(covered.event_type, dict(covered.payload), covered.stream_version, covered.commit_id)):
                    cp = None
        if cp is not None:
            base = checkpoint_state(context=cp.context, model_round=cp.model_round,
                tool_count=cp.tool_count, output_chars=cp.output_chars,
                input_tokens=cp.input_tokens, output_tokens=cp.output_tokens,
                phase=cp.phase, execution_version=cp.execution_version, run_id=cp.run_id)
            return reconstruct_execution(self._read_events(stream, cp.execution_version), initial=base)
        events = self._read_events(stream, -1)
        if not events:
            # Legacy D1 callers can start a Turn before constructing the D6
            # recorder. With no execution fact, no model/tool fact was durably
            # accepted, so the original user input is the only safe fallback.
            turn = self.runtime.get_turn(item.turn_id)
            return ReconstructedContext(
                context=(
                    {
                        "kind": "user",
                        "input_id": f"turn:{item.turn_id}:original",
                        "content": turn.user_input,
                        "source_interrupt_id": None,
                    },
                ),
                model_round=0,
                tool_count=0,
                output_chars=0,
                input_tokens=0,
                output_tokens=0,
                phase=RunPhase.READY_FOR_MODEL,
                execution_version=-1,
                last_run_id=item.run_id,
            )
        return reconstruct_execution(events)

    def _read_events(self, stream: StreamId, after_version: int) -> tuple:
        values = []
        cursor = after_version
        while True:
            page = self.checkpoints.event_store.read_stream(stream, after_version=cursor, limit=500)
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version

    def claim_stale(self, item: RecoverableTurn, *, force: bool = False) -> RecoveryClaim:
        context = self.reconstruct(item)
        uncertain_phase = context.phase in (
            RunPhase.TOOL_IN_PROGRESS,
            RunPhase.BLOCKED_UNCERTAIN_SIDE_EFFECT,
        )
        if uncertain_phase and self.tool_recovery is None:
            raise AutomaticRecoveryBlocked(
                "possible side effect requires D7 ledger or operator"
            )
        if not uncertain_phase and not item.automatic:
            raise AutomaticRecoveryBlocked("possible side effect requires D7 ledger or operator")
        current = self.runtime.get_turn(item.turn_id)
        if (
            current.status is TurnStatus.QUEUED
            and current.current_run_id is None
            and current.version == item.turn_version
        ):
            # A prior coordinator may have committed the stale requeue and crashed
            # before ledger reconciliation or Worker dispatch.
            queued = current
        elif (
            current.status is not TurnStatus.RUNNING
            or current.current_run_id != item.run_id
            or current.version != item.turn_version
        ):
            raise AutomaticRecoveryBlocked("recoverable index no longer matches the turn")
        else:
            # Fence the old Run before a query is allowed to release its claim.
            # NOT_APPLIED lookup adapters additionally promise that the external
            # request cannot apply later.
            self.checkpoints.abandon_stale_run(item, force=force)
            queued = self.runtime.get_turn(item.turn_id)
        if uncertain_phase and (
            self.tool_recovery is None
            or not self.tool_recovery.reconcile_pending(
                item.turn_id,
                context.pending_tool_calls,
            )
        ):
            raise AutomaticRecoveryBlocked(
                "possible side effect requires D7 ledger or operator"
            )
        return RecoveryClaim(queued, None, context)
