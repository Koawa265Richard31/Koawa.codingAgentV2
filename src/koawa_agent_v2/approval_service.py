"""D9 durable approvals and atomic authorization-to-ledger claims."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .control.event_store import (
    EventMetadata, EventStore, EventStoreError, NewEvent, StreamId,
    StreamPrecondition, StreamWrite, WrongExpectedVersion,
)
from .control.models import (
    RUN_INTERRUPTED, RUN_STARTED, TURN_RECOVERY_QUEUED, TURN_WAITING_FOR_APPROVAL,
    TurnState, TurnStatus, rebuild_turn,
)
from .execution.loop import ToolExecutionContext
from .policy import Decision, PolicyError, PolicyVerdict, ResolvedAction

if TYPE_CHECKING:
    from .ledger.protocol import ToolExecutionRecord
    from .ledger.store import ToolLedgerStore


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class ApprovalError(PolicyError):
    """Stable, content-free approval failure."""


class ApprovalWaiting(ApprovalError):
    """The Turn was durably suspended before a tool claim."""

    approval_waiting = True


class ApprovalDenied(ApprovalError):
    """The exact request was durably denied."""


@dataclass(frozen=True, slots=True, repr=False)
class ApprovalRecord:
    subject_id: UUID
    request_id: UUID
    thread_id: UUID
    turn_id: UUID
    execution_id: UUID
    model_turn_id: UUID
    call_id: str
    interrupt_id: UUID
    action_digest: str
    principal_id: str
    policy_version: str
    capability_scope: tuple[str, ...]
    status: ApprovalStatus
    expires_at: datetime
    version: int
    single_use: bool = True
    consumed_run_id: UUID | None = None
    claim_token: UUID | None = None

    def __repr__(self) -> str:
        return (
            f"ApprovalRecord(request_id={self.request_id}, "
            f"status={self.status.value!r}, version={self.version})"
        )


class ApprovalService:
    """Coordinates approval, durable budget and D7 ledger streams."""

    def __init__(
        self,
        event_store: EventStore,
        ledger: ToolLedgerStore,
        *,
        budget_action_limits: Mapping[str, int],
        approval_ttl_seconds: int = 300,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not hasattr(event_store, "append_batch") or not hasattr(event_store, "read_stream"):
            raise TypeError("event_store must implement EventStore")
        if not hasattr(ledger, "event_store") or not hasattr(ledger, "load"):
            raise TypeError("ledger must implement the ToolLedgerStore boundary")
        if ledger.event_store is not event_store:
            raise ValueError("approval and ledger must share one EventStore")
        if (
            not isinstance(approval_ttl_seconds, int)
            or isinstance(approval_ttl_seconds, bool)
            or not 1 <= approval_ttl_seconds <= 86_400
        ):
            raise ValueError("approval_ttl_seconds must be in 1..86400")
        limits = dict(budget_action_limits)
        if not limits:
            raise ValueError("budget_action_limits must not be empty")
        for principal_id, limit in limits.items():
            _text(principal_id, "principal_id", 128)
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or not 1 <= limit <= 1_000_000
            ):
                raise ValueError("budget action limits must be in 1..1000000")
        self.event_store = event_store
        self.ledger = ledger
        self._budget_action_limits = limits
        self._approval_ttl = timedelta(seconds=approval_ttl_seconds)
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or None")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._allows_explicit_time = clock is not None

    def load(self, subject_id: UUID) -> ApprovalRecord | None:
        if not isinstance(subject_id, UUID):
            raise TypeError("subject_id must be UUID")
        events = self._read_all(_approval_stream(subject_id))
        return None if not events else _rebuild_approval(subject_id, events)

    def require_grant(
        self,
        record: ToolExecutionRecord,
        action: ResolvedAction,
        verdict: PolicyVerdict,
        *,
        context: ToolExecutionContext,
        prompt: str,
        now: datetime | None = None,
    ) -> ApprovalRecord | None:
        """Return a matching grant, deny, or durably suspend the running Turn."""
        self._validate(record, action, verdict, context)
        if verdict.decision is Decision.ALLOW:
            return None
        if verdict.decision is Decision.DENY:
            raise ApprovalDenied(verdict.code)
        if verdict.decision is not Decision.ASK:
            raise ApprovalError("policy_decision_invalid")
        observed_at = self._observed_at(now)
        current = self.load(record.execution_id)
        if current is not None and _same_authority(current, action):
            if current.status is ApprovalStatus.DENIED:
                raise ApprovalDenied("approval_denied")
            if current.status is ApprovalStatus.GRANTED and observed_at < current.expires_at:
                return current
            if current.status is ApprovalStatus.CONSUMED:
                return current
            if current.status is ApprovalStatus.PENDING:
                raise ApprovalWaiting("approval_waiting")
        self._request(
            record, action, context=context, prompt=prompt,
            previous=current, now=observed_at,
        )
        raise ApprovalWaiting("approval_waiting")

    def resolve(
        self,
        pending: ApprovalRecord,
        approved: bool,
        *,
        expected_approval_version: int,
        expected_turn_version: int,
        interrupt_id: UUID,
        approver_principal_id: str,
        command_id: UUID | None = None,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        """Resolve one exact request and queue its Turn in the same transaction."""
        if not isinstance(pending, ApprovalRecord):
            raise TypeError("pending must be ApprovalRecord")
        if not isinstance(approved, bool):
            raise TypeError("approved must be bool")
        _version(expected_approval_version)
        _version(expected_turn_version)
        if not isinstance(interrupt_id, UUID):
            raise TypeError("interrupt_id must be UUID")
        _text(approver_principal_id, "approver_principal_id", 128)
        current = self.load(pending.subject_id)
        if current is None or current.request_id != pending.request_id:
            raise ApprovalError("approval_request_stale")
        if current.version != expected_approval_version:
            raise ApprovalError("approval_version_stale")
        if current.status is not ApprovalStatus.PENDING:
            raise ApprovalError("approval_already_resolved")
        if interrupt_id != current.interrupt_id:
            raise ApprovalError("approval_interrupt_mismatch")
        turn = self._load_turn(current.turn_id)
        if turn.version != expected_turn_version:
            raise ApprovalError("approval_turn_version_stale")
        if (
            turn.status is not TurnStatus.WAITING_FOR_APPROVAL
            or turn.pending_interrupt is None
            or turn.pending_interrupt.interrupt_id != current.interrupt_id
            or turn.pending_interrupt.approval_request_id != current.request_id
        ):
            raise ApprovalError("approval_turn_not_waiting")
        observed_at = self._observed_at(now)
        expired = observed_at >= current.expires_at
        decision = "denied" if expired or not approved else "granted"
        event_type = (
            "approval.expired.v1" if expired else
            "approval.granted.v1" if approved else
            "approval.denied.v1"
        )
        resolved_command = command_id or uuid5(
            NAMESPACE_URL,
            f"koawa-d9:resolve:{current.request_id}:{decision}:{approver_principal_id}",
        )
        fingerprint = _json({
            "action": "resolve_approval",
            "request_id": str(current.request_id),
            "approval_version": expected_approval_version,
            "turn_version": expected_turn_version,
            "interrupt_id": str(interrupt_id),
            "decision": decision,
            "approver_principal_id": approver_principal_id,
        })
        approval_event = _event(
            resolved_command, "approval-resolution", event_type,
            {
                "request_id": str(current.request_id),
                "action_digest": current.action_digest,
                "principal_id": current.principal_id,
                "policy_version": current.policy_version,
                "approver_principal_id": approver_principal_id,
                "reason": "expired" if expired else decision,
            },
            current.thread_id, current.turn_id, observed_at,
        )
        turn_event = _event(
            resolved_command, "turn-recovery-queued", TURN_RECOVERY_QUEUED,
            {
                "interrupt_id": str(current.interrupt_id),
                "response": None,
                "approval_request_id": str(current.request_id),
                "approval_decision": decision,
            },
            current.thread_id, current.turn_id, observed_at,
        )
        self.event_store.append_batch(
            (
                StreamWrite(
                    _approval_stream(current.subject_id), current.version,
                    (approval_event,),
                ),
                StreamWrite(
                    StreamId("turn", current.turn_id), turn.version,
                    (turn_event,),
                ),
            ),
            idempotency_key=resolved_command,
            request_fingerprint=fingerprint,
        )
        updated = self.load(current.subject_id)
        if updated is None:
            raise ApprovalError("approval_resolution_missing")
        return updated

    def claim(
        self,
        record: ToolExecutionRecord,
        action: ResolvedAction,
        verdict: PolicyVerdict,
        approval: ApprovalRecord | None,
        *,
        context: ToolExecutionContext,
        now: datetime | None = None,
    ) -> ToolExecutionRecord:
        """Reserve budget, consume a grant and claim D7 in one append batch."""
        from .ledger.protocol import (
            RecoveryMode,
            ToolExecutionState,
            ToolLedgerConflict,
            ToolOutcomeBlocked,
        )

        self._validate(record, action, verdict, context)
        observed_at = self._observed_at(now)
        current = self.ledger.load(record.execution_id)
        if current is None:
            raise ToolLedgerConflict("tool_execution_missing")
        if current.state in (ToolExecutionState.SUCCEEDED, ToolExecutionState.FAILED):
            return current
        if current.state is ToolExecutionState.OUTCOME_UNKNOWN:
            raise ToolOutcomeBlocked("tool_outcome_unknown")
        if context.turn_id is None or context.turn_version is None:
            raise ApprovalError("durable_turn_identity_required")
        active_turn = self._load_turn(context.turn_id)
        consume = False
        if verdict.decision is Decision.ASK:
            if approval is None:
                raise ApprovalError("approval_grant_required")
            live = self.load(approval.subject_id)
            if live is None or live.request_id != approval.request_id:
                raise ApprovalError("approval_request_stale")
            if not _same_authority(live, action):
                raise ApprovalError("approval_action_mismatch")
            if live.status is ApprovalStatus.GRANTED:
                if observed_at >= live.expires_at:
                    raise ApprovalError("approval_expired")
                consume = True
            elif not (
                live.status is ApprovalStatus.CONSUMED
                and live.execution_id == current.execution_id
            ):
                raise ApprovalError("approval_grant_required")
            elif live.status is ApprovalStatus.CONSUMED:
                # The ledger and approval reads above are separate calls.  If
                # another transaction committed between them, refresh the
                # ledger before deciding from the newly observed CONSUMED fact.
                refreshed = self.ledger.load(record.execution_id)
                if refreshed is None:
                    raise ToolLedgerConflict("tool_execution_missing")
                current = refreshed
                if current.state in (
                    ToolExecutionState.SUCCEEDED,
                    ToolExecutionState.FAILED,
                ):
                    if live.claim_token == current.claim_token:
                        return current
                    raise ApprovalError("approval_grant_required")
                if not (
                    current.profile.recovery_mode is RecoveryMode.RETRY
                    and current.state is ToolExecutionState.CLAIMED
                    and live.claim_token == current.claim_token
                    and live.consumed_run_id == current.claimant_run_id
                    and self._claim_matches(current, action)
                ):
                    raise ApprovalError("approval_grant_required")
            approval = live
        elif verdict.decision is Decision.DENY:
            raise ApprovalDenied(verdict.code)
        elif approval is not None:
            raise ApprovalError("unexpected_approval_grant")
        if current.state is ToolExecutionState.CLAIMED:
            if current.claimant_run_id == context.run_id:
                if self._claim_matches(current, action):
                    return current
                raise ToolOutcomeBlocked("tool_claim_already_active")
            if current.profile.recovery_mode is not RecoveryMode.RETRY:
                raise ToolOutcomeBlocked("tool_claim_requires_recovery")
            event_type = "tool.execution-reclaimed.v1"
            reserve_budget = False
        elif current.state is ToolExecutionState.PREPARED:
            event_type = "tool.execution-claimed.v1"
            reserve_budget = True
        else:
            raise ApprovalError("tool_ledger_state_invalid")
        principal_id = action.principal.principal_id
        limit = self._budget_action_limits.get(principal_id)
        if limit is None:
            raise ApprovalError("resource_budget_principal_missing")
        budget_stream = _budget_stream(principal_id)
        budget_events = self._read_all(budget_stream)
        budget_version = -1 if not budget_events else budget_events[-1].stream_version
        try:
            if reserve_budget and len(budget_events) >= limit:
                raise ApprovalError("resource_budget_exceeded")
            command_document = {
                "action": "authorized_tool_claim",
                "execution_id": str(current.execution_id),
                "ledger_version": current.version,
                "claim_epoch": current.claim_epoch + 1,
                "run_id": str(context.run_id),
                "turn_version": context.turn_version,
                "action_digest": action.action_digest,
                "principal_id": principal_id,
                "policy_version": action.policy_version,
                "approval_request_id": (
                    str(approval.request_id) if consume and approval else None
                ),
                "approval_version": (
                    approval.version if consume and approval else None
                ),
                "budget_version": budget_version if reserve_budget else None,
            }
            fingerprint = _json(command_document)
            command_id = uuid5(
                NAMESPACE_URL,
                "koawa-d9:claim:" + current.execution_id.hex + ":"
                + hashlib.sha256(fingerprint.encode()).hexdigest(),
            )
            claim_token = uuid5(command_id, "claim-token")
            claim_event = _event(
                command_id, "tool-claim", event_type,
                {
                    "execution_id": str(current.execution_id),
                    "claimant_run_id": str(context.run_id),
                    "claim_epoch": current.claim_epoch + 1,
                    "claim_token": str(claim_token),
                    "action_digest": action.action_digest,
                    "principal_id": principal_id,
                    "policy_version": action.policy_version,
                    "approval_request_id": (
                        str(approval.request_id) if consume and approval else None
                    ),
                },
                active_turn.thread_id, current.turn_id, observed_at,
                run_id=context.run_id,
            )
            writes = [
                StreamWrite(
                    StreamId("tool-execution", current.execution_id),
                    current.version, (claim_event,),
                )
            ]
            if consume and approval is not None:
                consumed = _event(
                    command_id, "approval-consumed", "approval.consumed.v1",
                    {
                        "request_id": str(approval.request_id),
                        "execution_id": str(current.execution_id),
                        "action_digest": action.action_digest,
                        "principal_id": principal_id,
                        "policy_version": action.policy_version,
                        "consumer_run_id": str(context.run_id),
                        "claim_token": str(claim_token),
                        "claim_epoch": current.claim_epoch + 1,
                    },
                    active_turn.thread_id, current.turn_id, observed_at,
                    run_id=context.run_id,
                )
                writes.append(StreamWrite(
                    _approval_stream(approval.subject_id),
                    approval.version, (consumed,),
                ))
            if reserve_budget:
                reserved = _event(
                    command_id, "budget-reserved", "resource.budget-reserved.v1",
                    {
                        "principal_id": principal_id,
                        "execution_id": str(current.execution_id),
                        "action_digest": action.action_digest,
                        "reserved_actions": 1,
                        "limit_actions": limit,
                    },
                    active_turn.thread_id, current.turn_id, observed_at,
                    run_id=context.run_id,
                )
                writes.append(StreamWrite(
                    budget_stream, budget_version, (reserved,),
                ))
            self.event_store.append_batch(
                tuple(writes),
                idempotency_key=command_id,
                request_fingerprint=fingerprint,
                preconditions=(StreamPrecondition(
                    StreamId("turn", context.turn_id),
                    context.turn_version,
                    "turn.started.v1",
                    {"run_id": str(context.run_id)},
                ),),
            )
        except (WrongExpectedVersion, ApprovalError) as error:
            if (
                isinstance(error, ApprovalError)
                and error.code != "resource_budget_exceeded"
            ):
                raise
            # A concurrent claim for the same grant/execution committed first.
            # The losing snapshot differed only in budget/approval version, so
            # this is a semantic duplicate, not a different command: converge
            # on the winner's claim instead of leaking a second budget event.
            refreshed = self.ledger.load(current.execution_id)
            if (
                refreshed is not None
                and refreshed.state is ToolExecutionState.CLAIMED
                and refreshed.claim_token is not None
                and refreshed.claimant_run_id == context.run_id
                and self._claim_matches(refreshed, action)
            ):
                return refreshed
            raise
        claimed = self.ledger.load(current.execution_id)
        if claimed is None:
            raise ApprovalError("authorized_claim_missing")
        return claimed

    def begin_execution(
        self,
        record: ToolExecutionRecord,
        action: ResolvedAction,
        *,
        context: ToolExecutionContext,
    ) -> None:
        """Persist a one-shot gate for one exact claim before handler entry."""
        from .ledger.protocol import (
            ToolExecutionState, ToolLedgerConflict, ToolOutcomeBlocked,
        )

        if not isinstance(action, ResolvedAction):
            raise TypeError("action must be ResolvedAction")
        current = self.ledger.load(record.execution_id)
        if (
            current is None
            or current.state is not ToolExecutionState.CLAIMED
            or current.claim_token is None
            or current.claim_token != record.claim_token
            or current.claimant_run_id != context.run_id
            or not self._claim_matches(current, action)
        ):
            raise ToolLedgerConflict("authorized_execution_fence_rejected")
        command_id = uuid4()
        observed_at = self._observed_at(None)
        active_turn = self._load_turn(current.turn_id)
        event = _event(
            command_id,
            "execution-attempt",
            "tool.execution-attempted.v1",
            {
                "execution_id": str(current.execution_id),
                "claim_token": str(current.claim_token),
                "claim_epoch": current.claim_epoch,
                "claimant_run_id": str(context.run_id),
                "action_digest": action.action_digest,
                "principal_id": action.principal.principal_id,
                "policy_version": action.policy_version,
            },
            active_turn.thread_id,
            current.turn_id,
            observed_at,
            run_id=context.run_id,
        )
        fingerprint = _json({
            "action": "begin_authorized_execution",
            "claim_token": str(current.claim_token),
            "action_digest": action.action_digest,
        })
        try:
            self.event_store.append_batch(
                (StreamWrite(
                    StreamId("tool-attempt", current.claim_token), -1, (event,),
                ),),
                idempotency_key=command_id,
                request_fingerprint=fingerprint,
                preconditions=(StreamPrecondition(
                    StreamId("turn", current.turn_id),
                    context.turn_version,
                    "turn.started.v1",
                    {"run_id": str(context.run_id)},
                ),),
            )
        except WrongExpectedVersion:
            raise ToolOutcomeBlocked("tool_claim_already_executed") from None
        except EventStoreError:
            raise ToolOutcomeBlocked(
                "authorized_execution_fence_rejected"
            ) from None

    def _observed_at(self, explicit: datetime | None) -> datetime:
        if explicit is not None:
            if not self._allows_explicit_time:
                raise ApprovalError("untrusted_approval_time_override")
            return _now(explicit)
        return _now(self._clock())

    def _request(
        self,
        record: ToolExecutionRecord,
        action: ResolvedAction,
        *,
        context: ToolExecutionContext,
        prompt: str,
        previous: ApprovalRecord | None,
        now: datetime,
    ) -> None:
        _text(prompt, "prompt", 2048)
        if context.turn_id is None or context.turn_version is None:
            raise ApprovalError("durable_turn_identity_required")
        turn = self._load_turn(context.turn_id)
        if (
            turn.version != context.turn_version
            or turn.status is not TurnStatus.RUNNING
            or turn.current_run_id != context.run_id
        ):
            raise ApprovalError("approval_turn_fence_rejected")
        approval_version = -1 if previous is None else previous.version
        request_id = uuid5(
            NAMESPACE_URL,
            f"koawa-d9:request:{record.execution_id}:{approval_version + 1}:"
            f"{action.action_digest}",
        )
        interrupt_id = uuid5(request_id, "interrupt")
        command_id = uuid5(request_id, "request-command")
        expires_at = now + self._approval_ttl
        fingerprint = _json({
            "action": "request_approval",
            "execution_id": str(record.execution_id),
            "approval_version": approval_version,
            "turn_version": turn.version,
            "run_id": str(context.run_id),
            "request_id": str(request_id),
            "interrupt_id": str(interrupt_id),
            "action_digest": action.action_digest,
            "principal_id": action.principal.principal_id,
            "policy_version": action.policy_version,
        })
        approval_events = []
        if previous is not None and previous.status in (
            ApprovalStatus.PENDING, ApprovalStatus.GRANTED,
        ):
            approval_events.append(_event(
                command_id, "approval-invalidated", "approval.expired.v1",
                {
                    "request_id": str(previous.request_id),
                    "action_digest": previous.action_digest,
                    "principal_id": previous.principal_id,
                    "policy_version": previous.policy_version,
                    "reason": "action_or_policy_drift",
                },
                record.turn_id, record.turn_id, now, run_id=context.run_id,
            ))
        approval_events.append(_event(
            command_id, "approval-requested", "approval.requested.v1",
            {
                "schema_version": 1,
                "request_id": str(request_id),
                "subject_id": str(record.execution_id),
                "thread_id": str(turn.thread_id),
                "turn_id": str(record.turn_id),
                "execution_id": str(record.execution_id),
                "model_turn_id": str(record.model_turn_id),
                "call_id": record.call_id,
                "interrupt_id": str(interrupt_id),
                "action_digest": action.action_digest,
                "principal_id": action.principal.principal_id,
                "policy_version": action.policy_version,
                "capability_scope": list(action.principal.scopes),
                "expires_at": expires_at.isoformat(),
                "single_use": True,
            },
            turn.thread_id, record.turn_id, now, run_id=context.run_id,
        ))
        turn_event = _event(
            command_id, "turn-waiting-for-approval", TURN_WAITING_FOR_APPROVAL,
            {
                "interrupt_id": str(interrupt_id),
                "prompt": prompt,
                "approval_request_id": str(request_id),
            },
            turn.thread_id, turn.turn_id, now, run_id=context.run_id,
        )
        run_events = self._read_all(StreamId("run", context.run_id))
        if not run_events or run_events[0].event_type != RUN_STARTED:
            raise ApprovalError("legacy_active_run_restart_required")
        run_head = run_events[-1]
        if run_head.event_type != RUN_STARTED:
            raise ApprovalError("approval_run_fence_rejected")
        run_event = _event(
            command_id, "run-interrupted", RUN_INTERRUPTED,
            {"run_id": str(context.run_id), "thread_id": str(turn.thread_id),
             "turn_id": str(turn.turn_id), "detail": TURN_WAITING_FOR_APPROVAL},
            turn.thread_id, turn.turn_id, now, run_id=context.run_id,
        )
        self.event_store.append_batch(
            (
                StreamWrite(
                    _approval_stream(record.execution_id), approval_version,
                    tuple(approval_events),
                ),
                StreamWrite(
                    StreamId("turn", turn.turn_id), turn.version, (turn_event,),
                ),
                StreamWrite(
                    StreamId("run", context.run_id), run_head.stream_version, (run_event,),
                ),
            ),
            idempotency_key=command_id,
            request_fingerprint=fingerprint,
        )

    def require_escalated_grant(
        self,
        record,
        action: ResolvedAction,
        *,
        context: ToolExecutionContext,
        signal_payload: Mapping[str, Any],
        escalated_payload: Mapping[str, Any],
        prompt: str,
        security_head: int | None = None,
    ) -> dict:
        """RT/J J2: five-event atomic escalation command (plan §3 J2).

        Rescanned via Edit (scanner-hook compliance).

        security-state ×2 (signal + escalated) + approval.requested + turn
        waiting + run interrupted, exact heads on all four streams, one
        append_batch.  Raises ApprovalWaiting after a successful commit; the
        caller must not execute the action in that case.  Sticky: a prior
        escalation for the same (execution, action digest, policy version)
        reuses its request id and never re-fires from scratch.
        """
        _text(prompt, "prompt", 2048)
        if context.turn_id is None or context.turn_version is None:
            raise ApprovalError("durable_turn_identity_required")
        turn = self._load_turn(context.turn_id)
        if (
            turn.version != context.turn_version
            or turn.status is not TurnStatus.RUNNING
            or turn.current_run_id != context.run_id
        ):
            raise ApprovalError("approval_turn_fence_rejected")

        from .security.state import (
            POLICY_ESCALATED_EVENT,
            SECURITY_SIGNAL_EVENT,
            SecurityStateStore,
            security_stream,
        )

        sec_store = SecurityStateStore(self.event_store)
        sec_stream = security_stream(record.execution_id)
        request_id = uuid5(
            NAMESPACE_URL,
            f"koawa-j2:request:{record.execution_id}:{action.action_digest}:"
            f"{action.policy_version}",
        )
        interrupt_id = uuid5(request_id, "interrupt")
        command_id = uuid5(request_id, "j2-command")
        now = self._observed_at(None)
        expires_at = now + self._approval_ttl

        signal_event = _event(
            command_id, "security-signal", SECURITY_SIGNAL_EVENT,
            dict(signal_payload), turn.thread_id, record.turn_id, now,
            run_id=context.run_id,
        )
        escalated_event = _event(
            command_id, "policy-escalated", POLICY_ESCALATED_EVENT,
            dict(escalated_payload), turn.thread_id, record.turn_id, now,
            run_id=context.run_id,
        )
        approval_events = [_event(
            command_id, "approval-requested", "approval.requested.v1",
            {
                "schema_version": 1,
                "request_id": str(request_id),
                "subject_id": str(record.execution_id),
                "thread_id": str(turn.thread_id),
                "turn_id": str(record.turn_id),
                "execution_id": str(record.execution_id),
                "model_turn_id": str(record.model_turn_id),
                "call_id": record.call_id,
                "interrupt_id": str(interrupt_id),
                "action_digest": action.action_digest,
                "principal_id": action.principal.principal_id,
                "policy_version": action.policy_version,
                "capability_scope": list(action.principal.scopes),
                "reason_code": "security_canary_exact",
                "expires_at": expires_at.isoformat(),
                "single_use": True,
            },
            turn.thread_id, record.turn_id, now, run_id=context.run_id,
        )]
        turn_event = _event(
            command_id, "turn-waiting-for-approval", "turn.waiting-for-approval.v1",
            {
                "interrupt_id": str(interrupt_id),
                "prompt": prompt,
                "approval_request_id": str(request_id),
                "reason_code": "security_canary_exact",
            },
            turn.thread_id, turn.turn_id, now, run_id=context.run_id,
        )
        run_events = self._read_all(StreamId("run", context.run_id))
        if not run_events or run_events[0].event_type != "run.started.v1":
            raise ApprovalError("legacy_active_run_restart_required")
        run_head = run_events[-1]
        run_event = _event(
            command_id, "run-interrupted", "run.interrupted.v1",
            {"run_id": str(context.run_id), "thread_id": str(turn.thread_id),
             "turn_id": str(turn.turn_id),
             "detail": "security_canary_exact"},
            turn.thread_id, turn.turn_id, now, run_id=context.run_id,
        )

        sec_head = sec_store.head(record.execution_id) if security_head is None else security_head
        fingerprint = _json({
            "action": "j2_escalate",
            "execution_id": str(record.execution_id),
            "security_head": sec_head,
            "approval_version": -1,
            "turn_version": turn.version,
            "run_version": run_head.stream_version,
            "request_id": str(request_id),
            "action_digest": action.action_digest,
            "policy_version": action.policy_version,
        })
        self.event_store.append_batch(
            (
                StreamWrite(sec_stream, sec_head, (signal_event, escalated_event)),
                StreamWrite(
                    _approval_stream(record.execution_id), -1,
                    tuple(approval_events),
                ),
                StreamWrite(
                    StreamId("turn", turn.turn_id), turn.version, (turn_event,),
                ),
                StreamWrite(
                    StreamId("run", context.run_id), run_head.stream_version,
                    (run_event,),
                ),
            ),
            idempotency_key=command_id,
            request_fingerprint=fingerprint,
        )
        return {
            "request_id": str(request_id),
            "interrupt_id": str(interrupt_id),
            "security_head": sec_head + 2,
        }

    def _validate(
        self,

        record: ToolExecutionRecord,
        action: ResolvedAction,
        verdict: PolicyVerdict,
        context: ToolExecutionContext,
    ) -> None:
        from .ledger.protocol import ToolExecutionRecord, canonical_arguments_digest

        if not isinstance(record, ToolExecutionRecord):
            raise TypeError("record must be ToolExecutionRecord")
        if not isinstance(action, ResolvedAction):
            raise TypeError("action must be ResolvedAction")
        if not isinstance(verdict, PolicyVerdict):
            raise TypeError("verdict must be PolicyVerdict")
        if not isinstance(context, ToolExecutionContext):
            raise TypeError("context must be ToolExecutionContext")
        if verdict.action_digest != action.action_digest:
            raise ApprovalError("policy_action_digest_mismatch")
        if verdict.policy_version != action.policy_version:
            raise ApprovalError("policy_version_mismatch")
        if action.tool_name != record.tool_name:
            raise ApprovalError("policy_tool_identity_mismatch")
        arguments_sha256, arguments_bytes = canonical_arguments_digest(
            action.canonical_arguments_json
        )
        if (
            arguments_sha256 != record.arguments_sha256
            or arguments_bytes != record.arguments_bytes
        ):
            raise ApprovalError("policy_arguments_identity_mismatch")
        if context.turn_id != record.turn_id:
            raise ApprovalError("policy_turn_identity_mismatch")

    def _load_turn(self, turn_id: UUID) -> TurnState:
        events = self._read_all(StreamId("turn", turn_id))
        if not events:
            raise ApprovalError("approval_turn_missing")
        return rebuild_turn(turn_id, events)

    def _claim_matches(
        self,
        record: ToolExecutionRecord,
        action: ResolvedAction,
    ) -> bool:
        events = self._read_all(StreamId("tool-execution", record.execution_id))
        if not events:
            return False
        latest = events[-1]
        return (
            latest.event_type in (
                "tool.execution-claimed.v1",
                "tool.execution-reclaimed.v1",
            )
            and latest.payload.get("claim_token") == str(record.claim_token)
            and latest.payload.get("action_digest") == action.action_digest
            and latest.payload.get("principal_id") == action.principal.principal_id
            and latest.payload.get("policy_version") == action.policy_version
        )

    def _read_all(self, stream: StreamId) -> tuple:
        values = []
        cursor = -1
        while True:
            page = self.event_store.read_stream(
                stream, after_version=cursor, limit=500,
            )
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version


def _rebuild_approval(subject_id: UUID, events: tuple) -> ApprovalRecord:
    state = None
    for event in events:
        payload = event.payload
        if event.event_type == "approval.requested.v1":
            if state is not None and state.status not in (
                ApprovalStatus.DENIED,
                ApprovalStatus.EXPIRED,
                ApprovalStatus.CONSUMED,
            ):
                raise EventStoreError("invalid approval request transition")
            state = ApprovalRecord(
                subject_id, UUID(payload["request_id"]),
                UUID(payload["thread_id"]), UUID(payload["turn_id"]),
                UUID(payload["execution_id"]), UUID(payload["model_turn_id"]),
                payload["call_id"], UUID(payload["interrupt_id"]),
                payload["action_digest"], payload["principal_id"],
                payload["policy_version"], tuple(payload["capability_scope"]),
                ApprovalStatus.PENDING,
                datetime.fromisoformat(payload["expires_at"]),
                event.stream_version, bool(payload["single_use"]),
            )
            if state.subject_id != UUID(payload["subject_id"]):
                raise EventStoreError("approval subject identity mismatch")
            continue
        if state is None or event.stream_version != state.version + 1:
            raise EventStoreError("invalid approval event order")
        if (
            payload.get("request_id") != str(state.request_id)
            or payload.get("action_digest") != state.action_digest
        ):
            raise EventStoreError("approval identity mismatch")
        if event.event_type == "approval.granted.v1":
            if state.status is not ApprovalStatus.PENDING:
                raise EventStoreError("invalid approval grant transition")
            state = _replace(state, status=ApprovalStatus.GRANTED, version=event.stream_version)
        elif event.event_type == "approval.denied.v1":
            if state.status is not ApprovalStatus.PENDING:
                raise EventStoreError("invalid approval deny transition")
            state = _replace(state, status=ApprovalStatus.DENIED, version=event.stream_version)
        elif event.event_type == "approval.expired.v1":
            if state.status not in (ApprovalStatus.PENDING, ApprovalStatus.GRANTED):
                raise EventStoreError("invalid approval expiry transition")
            state = _replace(state, status=ApprovalStatus.EXPIRED, version=event.stream_version)
        elif event.event_type == "approval.consumed.v1":
            if state.status is not ApprovalStatus.GRANTED:
                raise EventStoreError("invalid approval consume transition")
            state = _replace(
                state, status=ApprovalStatus.CONSUMED,
                version=event.stream_version,
                consumed_run_id=UUID(payload["consumer_run_id"]),
                claim_token=UUID(payload["claim_token"]),
            )
        else:
            raise EventStoreError("unknown approval event")
    if state is None:
        raise EventStoreError("empty approval stream")
    return state


def _replace(record: ApprovalRecord, **changes: Any) -> ApprovalRecord:
    values = {name: getattr(record, name) for name in record.__dataclass_fields__}
    values.update(changes)
    return ApprovalRecord(**values)


def _same_authority(record: ApprovalRecord, action: ResolvedAction) -> bool:
    return (
        record.execution_id == record.subject_id
        and record.action_digest == action.action_digest
        and record.principal_id == action.principal.principal_id
        and record.policy_version == action.policy_version
        and record.capability_scope == action.principal.scopes
    )


def _approval_stream(subject_id: UUID) -> StreamId:
    return StreamId("approval", subject_id)


def _budget_stream(principal_id: str) -> StreamId:
    return StreamId(
        "resource-budget",
        uuid5(NAMESPACE_URL, f"koawa-d9:budget:{principal_id}"),
    )


def _event(
    command_id: UUID,
    slot: str,
    event_type: str,
    payload: Mapping[str, Any],
    thread_id: UUID,
    turn_id: UUID,
    occurred_at: datetime,
    *,
    run_id: UUID | None = None,
) -> NewEvent:
    return NewEvent(
        uuid5(command_id, "event:" + slot), event_type, 1, occurred_at, payload,
        EventMetadata(
            command_id, turn_id, thread_id=thread_id, turn_id=turn_id,
            run_id=run_id, actor="approval-service",
        ),
    )


def _now(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return result.astimezone(timezone.utc)


def _version(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("expected version must be an integer >= 0")
    return value


def _text(value: Any, name: str, maximum: int) -> str:
    if (
        not isinstance(value, str) or not value.strip()
        or len(value) > maximum or "\x00" in value
    ):
        raise ValueError(f"{name} must be non-empty bounded text")
    return value


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


__all__ = [
    "ApprovalDenied", "ApprovalError", "ApprovalRecord", "ApprovalService",
    "ApprovalStatus", "ApprovalWaiting",
]
