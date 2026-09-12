"""Discovery and safe stale-run takeover orchestration (section 7.6)."""

from __future__ import annotations
from koawa_agent_v2.telemetry.faults import FaultPoint

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from ..control.event_store import StreamId
from ..control.models import TurnStatus
from ..control.runtime import ThreadRuntime
from ..control.schema import inject_fault
from .context import (
    ExecutionProjection,
    ReconstructionError,
    projection_digest,
    projection_document,
    reduce_execution,
)
from .protocol import (
    REDUCER_NAME,
    REDUCER_VERSION,
    CheckpointError,
    RunPhase,
    stored_event_hash_v2,
)
from .store import CheckpointStore, LeaseConflict, RecoverableTurn


class AutomaticRecoveryBlocked(RuntimeError):
    """Recovery requires a D7 ledger or an operator decision."""


class ToolRecoveryPort(Protocol):
    def reconcile_pending(
        self,
        turn_id: UUID,
        pending_tool_calls: Sequence[Mapping[str, Any]],
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class RecoveryClaim:
    turn: object
    lease: object | None
    context: ExecutionProjection


def _requeue_command_id(turn_id: UUID, run_id: UUID) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"koawa-d6:requeue:{turn_id}:{run_id}",
    )



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
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must be non-empty")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self.runtime = runtime
        self.checkpoints = checkpoints
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        self.tool_recovery = tool_recovery

    def list_recoverable_turns(self) -> tuple[RecoverableTurn, ...]:
        return self.checkpoints.list_recoverable()

    def reconstruct(self, item: RecoverableTurn) -> ExecutionProjection:
        """Rebuild the canonical projection, using the cache only when every
        field equals the reducer output on the covered segment.

        A fabricated checkpoint (even with a valid coverage hash) fails the
        field-for-field comparison and falls back to a full replay; a v1 or
        unparsable checkpoint is always a cache miss.
        """
        stream = StreamId("run-execution", item.turn_id)
        events = self._read_events(stream, -1)
        if not events:
            turn = self.runtime.get_turn(item.turn_id)
            return ExecutionProjection(
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
        full = reduce_execution(events)
        try:
            checkpoint = self.checkpoints.load(item.turn_id)
        except CheckpointError:
            checkpoint = None
        if checkpoint is not None and self._valid_cache(checkpoint, item, events, full):
            inject_fault(FaultPoint.S3_CHECKPOINT_AFTER_VERIFY_BEFORE_TAIL)
            covered = events[: checkpoint.covered_stream_version + 1]
            covered_projection = reduce_execution(covered)
            tail = events[checkpoint.covered_stream_version + 1 :]
            reduced = reduce_execution(tail, initial=covered_projection)
            # The final result must be field-for-field equivalent to the full
            # replay (immutability of events + deterministic reducer).
            if (projection_document(reduced) == projection_document(full)):
                return reduced
        return full

    def _valid_cache(self, checkpoint, item: RecoverableTurn, events, full) -> bool:
        if (
            checkpoint.reducer_name != REDUCER_NAME
            or checkpoint.reducer_version != REDUCER_VERSION
            or checkpoint.source_category != "run-execution"
            or checkpoint.source_aggregate_id != item.turn_id
            or checkpoint.turn_id != item.turn_id
            or checkpoint.run_id != item.run_id
            or checkpoint.turn_stream_version != item.turn_version
            or not (0 <= checkpoint.covered_stream_version <= full.execution_version)
        ):
            return False
        covered_event = events[checkpoint.covered_stream_version]
        if (
            covered_event.event_id != checkpoint.covered_event_id
            or covered_event.global_position != checkpoint.covered_global_position
            or covered_event.commit_id != checkpoint.covered_commit_id
            or stored_event_hash_v2(covered_event) != checkpoint.covered_event_hash
        ):
            return False
        covered_projection = reduce_execution(events[: checkpoint.covered_stream_version + 1])
        if (
            projection_document(covered_projection) != dict(checkpoint.projection)
            or projection_digest(covered_projection) != checkpoint.projection_digest
        ):
            return False
        return True

    def claim_stale(self, item: RecoverableTurn, *, force: bool = False) -> RecoveryClaim:
        if self._live_recovery_claim(item.turn_id):
            raise LeaseConflict("recovery run lease is still active")
        try:
            context = self.reconstruct(item)
        except ReconstructionError as exc:
            raise AutomaticRecoveryBlocked("corrupt execution log") from exc
        uncertain_phase = context.phase in (
            RunPhase.TOOL_IN_PROGRESS,
            RunPhase.BLOCKED_UNCERTAIN_SIDE_EFFECT,
        )
        if uncertain_phase and self.tool_recovery is None:
            raise AutomaticRecoveryBlocked(
                "possible side effect requires D7 ledger or operator"
            )
        current = self.runtime.get_turn(item.turn_id)
        if (
            current.status is TurnStatus.QUEUED
            and current.current_run_id is None
            and current.version == item.turn_version
        ):
            queued = current
        elif (
            current.status is not TurnStatus.RUNNING
            or current.current_run_id != item.run_id
            or current.version != item.turn_version
        ):
            raise AutomaticRecoveryBlocked("recoverable index no longer matches the turn")
        else:
            # Fence the old Run through the typed Turn command; the projection
            # adapter updates the recoverable/lease index with the same
            # append transaction.  The coordinator never writes SQL.
            self.runtime.requeue_stale_run(
                item.turn_id,
                expected_version=current.version,
                abandoned_run_id=item.run_id,
                command_id=_requeue_command_id(item.turn_id, item.run_id),
            )
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

    def _live_recovery_claim(self, turn_id: UUID) -> bool:
        # A live recovery-lease overlay fences every stale takeover, even
        # with force: another process may still be recovering this run.
        from datetime import timezone

        stream = StreamId("recovery-lease", turn_id)
        cursor = -1
        head = None
        while True:
            page = self.checkpoints.event_store.read_stream(
                stream, after_version=cursor, limit=500
            )
            if not page:
                break
            head = page[-1]
            cursor = head.stream_version
            if len(page) < 500:
                break
        if head is None:
            return False
        if head.event_type not in (
            "turn.recovery-lease-claimed.v1",
            "turn.recovery-lease-heartbeated.v1",
        ):
            return False
        # Token-less heads are the D1 worker's own run lease (durable starts
        # establish one per run); they are not a recovery overlay and must
        # never fence a stale takeover.  This mirrors the runtime-side
        # _live_recovery_claim, which also ignores token-less heads.
        if head.payload.get("claim_token") is None:
            return False
        expiry = head.payload.get("lease_expires_at")
        if not isinstance(expiry, str):
            return False
        parsed = datetime.fromisoformat(expiry)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) > self.checkpoints.database_time()

    def _read_events(self, stream: StreamId, after_version: int) -> tuple:
        values = []
        cursor = after_version
        while True:
            page = self.checkpoints.event_store.read_stream(
                stream, after_version=cursor, limit=500
            )
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version
