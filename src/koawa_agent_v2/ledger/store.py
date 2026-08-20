"""Append-only D7 Tool Ledger built on the existing transactional Event Store."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from ..control.event_store import (
    EventMetadata,
    EventStore,
    EventStoreError,
    NewEvent,
    StreamId,
    StreamPrecondition,
    StreamWrite,
    WrongExpectedVersion,
)
from ..recovery.redaction import redact_text
from .protocol import (
    DurableToolResult,
    RecoveryMode,
    SideEffectClass,
    ToolExecutionRecord,
    ToolExecutionState,
    ToolLedgerConflict,
    ToolLedgerError,
    ToolOutcomeBlocked,
    ToolRecoveryProfile,
    canonical_arguments_digest,
    logical_execution_id,
    result_digest,
)


class ToolLedgerStore:
    """Persist logical calls and physical claims as versioned JSON facts."""

    def __init__(self, event_store: EventStore) -> None:
        if not hasattr(event_store, "append_batch") or not hasattr(event_store, "read_stream"):
            raise TypeError("event_store must implement EventStore")
        self.event_store = event_store

    def load(self, execution_id: UUID) -> ToolExecutionRecord | None:
        if not isinstance(execution_id, UUID):
            raise TypeError("execution_id must be UUID")
        events = self._read_all(StreamId("tool-execution", execution_id))
        if not events:
            return None
        record = _reconstruct(events)
        if record.execution_id != execution_id:
            raise EventStoreError("tool ledger stream identity mismatch")
        return record

    def load_for_call(
        self,
        turn_id: UUID,
        model_turn_id: UUID,
        call_id: str,
    ) -> ToolExecutionRecord | None:
        return self.load(logical_execution_id(turn_id, model_turn_id, call_id))

    def prepare(
        self,
        *,
        turn_id: UUID,
        turn_version: int,
        run_id: UUID,
        model_turn_id: UUID,
        call_id: str,
        tool_name: str,
        arguments_json: str,
        profile: ToolRecoveryProfile,
        binding_digest: str | None = None,
    ) -> ToolExecutionRecord:
        execution_id = logical_execution_id(
            turn_id, model_turn_id, call_id, binding_digest=binding_digest,
        )
        arguments_sha256, arguments_bytes = canonical_arguments_digest(arguments_json)
        semantic = {
            "execution_id": str(execution_id),
            "turn_id": str(turn_id),
            "model_turn_id": str(model_turn_id),
            "call_id": call_id,
            "tool_name": tool_name,
            "arguments_sha256": arguments_sha256,
            "arguments_bytes": arguments_bytes,
            "side_effect_class": profile.side_effect_class.value,
            "recovery_mode": profile.recovery_mode.value,
            "binding_digest": binding_digest,
        }
        existing = self.load(execution_id)
        if existing is not None:
            _assert_semantics(existing, semantic)
            return existing
        try:
            self._append(
                execution_id,
                -1,
                "tool.execution-prepared.v1",
                semantic,
                turn_id=turn_id,
                run_id=run_id,
                turn_version=turn_version,
                fence_turn=True,
                actor="ledger",
            )
        except WrongExpectedVersion:
            existing = self.load(execution_id)
            if existing is None:
                raise
            _assert_semantics(existing, semantic)
            return existing
        prepared = self.load(execution_id)
        if prepared is None:  # pragma: no cover - append receipt without its event
            raise ToolLedgerError("ledger_prepare_missing")
        return prepared

    def claim(
        self,
        record: ToolExecutionRecord,
        *,
        turn_version: int,
        run_id: UUID,
    ) -> ToolExecutionRecord:
        current = self._require_current(record.execution_id)
        if current.state in (ToolExecutionState.SUCCEEDED, ToolExecutionState.FAILED):
            return current
        if current.state is ToolExecutionState.OUTCOME_UNKNOWN:
            raise ToolOutcomeBlocked("tool_outcome_unknown")
        if current.state is ToolExecutionState.CLAIMED:
            if current.claimant_run_id == run_id:
                raise ToolOutcomeBlocked("tool_claim_already_active")
            if current.profile.recovery_mode is not RecoveryMode.RETRY:
                raise ToolOutcomeBlocked("tool_claim_requires_recovery")
            event_type = "tool.execution-reclaimed.v1"
        elif current.state is ToolExecutionState.PREPARED:
            event_type = "tool.execution-claimed.v1"
        else:  # pragma: no cover - exhaustive enum guard
            raise ToolLedgerError("invalid_ledger_state")
        claim_token = uuid4()
        self._append(
            current.execution_id,
            current.version,
            event_type,
            {
                "execution_id": str(current.execution_id),
                "claimant_run_id": str(run_id),
                "claim_epoch": current.claim_epoch + 1,
                "claim_token": str(claim_token),
            },
            turn_id=current.turn_id,
            run_id=run_id,
            turn_version=turn_version,
            fence_turn=True,
            actor="ledger",
        )
        return self._require_current(current.execution_id)

    def commit_result(
        self,
        record: ToolExecutionRecord,
        result: DurableToolResult,
        *,
        actor: str = "ledger",
    ) -> ToolExecutionRecord:
        current = self._require_claim(record)
        if not isinstance(result, DurableToolResult):
            raise TypeError("result must be DurableToolResult")
        persisted_content = redact_text(result.content)
        content_sha256, content_bytes = result_digest(result.content)
        state = ToolExecutionState.FAILED if result.is_error else ToolExecutionState.SUCCEEDED
        self._append(
            current.execution_id,
            current.version,
            f"tool.execution-{state.value}.v1",
            {
                "execution_id": str(current.execution_id),
                "claim_token": str(current.claim_token),
                "result": {"content": persisted_content, "is_error": result.is_error},
                "result_sha256": content_sha256,
                "result_bytes": content_bytes,
            },
            turn_id=current.turn_id,
            run_id=current.claimant_run_id,
            actor=actor,
        )
        return self._require_current(current.execution_id)

    def release_not_applied(
        self,
        record: ToolExecutionRecord,
        *,
        actor: str = "recovery",
    ) -> ToolExecutionRecord:
        current = self._require_claim(record)
        self._append(
            current.execution_id,
            current.version,
            "tool.execution-not-applied.v1",
            {
                "execution_id": str(current.execution_id),
                "claim_token": str(current.claim_token),
                "prior_claim_epoch": current.claim_epoch,
            },
            turn_id=current.turn_id,
            run_id=current.claimant_run_id,
            actor=actor,
        )
        return self._require_current(current.execution_id)

    def resolve_unknown_result(
        self,
        record: ToolExecutionRecord,
        result: DurableToolResult,
        *,
        actor: str = "operator",
    ) -> ToolExecutionRecord:
        """Resolve an UNKNOWN claim from authoritative query or operator evidence."""
        current = self._require_unknown(record)
        if not isinstance(result, DurableToolResult):
            raise TypeError("result must be DurableToolResult")
        persisted_content = redact_text(result.content)
        content_sha256, content_bytes = result_digest(result.content)
        state = (
            ToolExecutionState.FAILED
            if result.is_error
            else ToolExecutionState.SUCCEEDED
        )
        self._append(
            current.execution_id,
            current.version,
            f"tool.execution-{state.value}.v1",
            {
                "execution_id": str(current.execution_id),
                "claim_token": str(current.claim_token),
                "result": {
                    "content": persisted_content,
                    "is_error": result.is_error,
                },
                "result_sha256": content_sha256,
                "result_bytes": content_bytes,
            },
            turn_id=current.turn_id,
            run_id=current.claimant_run_id,
            actor=actor,
        )
        return self._require_current(current.execution_id)

    def resolve_unknown_not_applied(
        self,
        record: ToolExecutionRecord,
        *,
        actor: str = "operator",
    ) -> ToolExecutionRecord:
        """Return UNKNOWN to PREPARED only after a terminal negative lookup."""
        current = self._require_unknown(record)
        self._append(
            current.execution_id,
            current.version,
            "tool.execution-not-applied.v1",
            {
                "execution_id": str(current.execution_id),
                "claim_token": str(current.claim_token),
                "prior_claim_epoch": current.claim_epoch,
            },
            turn_id=current.turn_id,
            run_id=current.claimant_run_id,
            actor=actor,
        )
        return self._require_current(current.execution_id)

    def mark_outcome_unknown(
        self,
        record: ToolExecutionRecord,
        reason: str,
        *,
        actor: str = "recovery",
    ) -> ToolExecutionRecord:
        if not isinstance(reason, str) or not reason or len(reason) > 128:
            raise ValueError("reason must be stable bounded text")
        current = self._require_current(record.execution_id)
        if current.state is ToolExecutionState.OUTCOME_UNKNOWN:
            if (
                record.claim_token is None
                or current.claim_token != record.claim_token
            ):
                raise ToolLedgerConflict("stale_tool_claim")
            return current
        current = self._require_claim(record)
        self._append(
            current.execution_id,
            current.version,
            "tool.execution-outcome-unknown.v1",
            {
                "execution_id": str(current.execution_id),
                "claim_token": str(current.claim_token),
                "reason": reason,
            },
            turn_id=current.turn_id,
            run_id=current.claimant_run_id,
            actor=actor,
        )
        return self._require_current(current.execution_id)

    def _require_current(self, execution_id: UUID) -> ToolExecutionRecord:
        value = self.load(execution_id)
        if value is None:
            raise ToolLedgerError("tool_execution_missing")
        return value

    def _require_claim(self, record: ToolExecutionRecord) -> ToolExecutionRecord:
        current = self._require_current(record.execution_id)
        if current.state is not ToolExecutionState.CLAIMED:
            raise ToolLedgerConflict("tool_claim_not_active")
        if record.claim_token is None or current.claim_token != record.claim_token:
            raise ToolLedgerConflict("stale_tool_claim")
        return current

    def _require_unknown(self, record: ToolExecutionRecord) -> ToolExecutionRecord:
        current = self._require_current(record.execution_id)
        if current.state is not ToolExecutionState.OUTCOME_UNKNOWN:
            raise ToolLedgerConflict("tool_outcome_not_unknown")
        if record.claim_token is None or current.claim_token != record.claim_token:
            raise ToolLedgerConflict("stale_tool_claim")
        return current

    def _append(
        self,
        execution_id: UUID,
        expected_version: int,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        turn_id: UUID,
        run_id: UUID | None,
        actor: str,
        turn_version: int | None = None,
        fence_turn: bool = False,
    ) -> None:
        fingerprint = json.dumps(
            {
                "actor": actor,
                "event_type": event_type,
                "execution_id": str(execution_id),
                "expected_version": expected_version,
                "payload": dict(payload),
                "run_id": None if run_id is None else str(run_id),
                "turn_id": str(turn_id),
                "turn_version": turn_version,
                "fence_turn": fence_turn,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        fingerprint_hash = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        command_id = uuid5(
            NAMESPACE_URL,
            (
                f"koawa-d7:{execution_id}:{expected_version + 1}:"
                f"{event_type}:{fingerprint_hash}"
            ),
        )
        event = NewEvent(
            uuid5(command_id, "event"),
            event_type,
            1,
            datetime.now(timezone.utc),
            payload,
            EventMetadata(
                command_id,
                turn_id,
                turn_id=turn_id,
                run_id=run_id,
                actor=actor,
            ),
        )
        preconditions = ()
        if fence_turn:
            if turn_version is None or run_id is None:
                raise ValueError("fenced ledger append requires turn_version and run_id")
            preconditions = (
                StreamPrecondition(
                    StreamId("turn", turn_id),
                    turn_version,
                    "turn.started.v1",
                    {"run_id": str(run_id)},
                ),
            )
        self.event_store.append_batch(
            (
                StreamWrite(
                    StreamId("tool-execution", execution_id),
                    expected_version,
                    (event,),
                ),
            ),
            idempotency_key=command_id,
            request_fingerprint=fingerprint,
            preconditions=preconditions,
        )

    def _read_all(self, stream: StreamId) -> tuple:
        values = []
        cursor = -1
        while True:
            page = self.event_store.read_stream(stream, after_version=cursor, limit=500)
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version


def _assert_semantics(record: ToolExecutionRecord, semantic: Mapping[str, Any]) -> None:
    actual = (
        str(record.execution_id),
        str(record.turn_id),
        str(record.model_turn_id),
        record.call_id,
        record.tool_name,
        record.arguments_sha256,
        record.arguments_bytes,
        record.profile.side_effect_class.value,
        record.profile.recovery_mode.value,
        record.binding_digest,
    )
    expected = (
        semantic["execution_id"],
        semantic["turn_id"],
        semantic["model_turn_id"],
        semantic["call_id"],
        semantic["tool_name"],
        semantic["arguments_sha256"],
        semantic["arguments_bytes"],
        semantic["side_effect_class"],
        semantic["recovery_mode"],
        semantic.get("binding_digest"),
    )
    if actual != expected:
        raise ToolLedgerConflict("tool_execution_identity_conflict")


def _reconstruct(events: tuple) -> ToolExecutionRecord:
    record: ToolExecutionRecord | None = None
    for event in events:
        payload = event.payload
        if event.event_type == "tool.execution-prepared.v1":
            if record is not None or event.stream_version != 0:
                raise EventStoreError("invalid tool ledger prepare history")
            record = ToolExecutionRecord(
                execution_id=UUID(payload["execution_id"]),
                turn_id=UUID(payload["turn_id"]),
                model_turn_id=UUID(payload["model_turn_id"]),
                call_id=payload["call_id"],
                tool_name=payload["tool_name"],
                arguments_sha256=payload["arguments_sha256"],
                arguments_bytes=int(payload["arguments_bytes"]),
                profile=ToolRecoveryProfile(
                    SideEffectClass(payload["side_effect_class"]),
                    RecoveryMode(payload["recovery_mode"]),
                ),
                binding_digest=payload.get("binding_digest"),
                state=ToolExecutionState.PREPARED,
                version=event.stream_version,
            )
            continue
        if record is None or event.stream_version != record.version + 1:
            raise EventStoreError("invalid tool ledger event order")
        if payload.get("execution_id") != str(record.execution_id):
            raise EventStoreError("tool ledger event identity mismatch")
        if event.event_type in (
            "tool.execution-claimed.v1",
            "tool.execution-reclaimed.v1",
        ):
            if (
                event.event_type == "tool.execution-claimed.v1"
                and record.state is not ToolExecutionState.PREPARED
            ):
                raise EventStoreError("invalid tool claim transition")
            if (
                event.event_type == "tool.execution-reclaimed.v1"
                and (
                    record.state is not ToolExecutionState.CLAIMED
                    or record.profile.recovery_mode is not RecoveryMode.RETRY
                )
            ):
                raise EventStoreError("invalid tool reclaim transition")
            epoch = int(payload["claim_epoch"])
            if epoch != record.claim_epoch + 1:
                raise EventStoreError("invalid tool claim epoch")
            record = _replace(
                record,
                state=ToolExecutionState.CLAIMED,
                version=event.stream_version,
                claim_epoch=epoch,
                claimant_run_id=UUID(payload["claimant_run_id"]),
                claim_token=UUID(payload["claim_token"]),
                result=None,
                result_sha256=None,
                result_bytes=None,
                unknown_reason=None,
            )
        elif event.event_type == "tool.execution-not-applied.v1":
            _require_event_claim(record, payload, allow_unknown=True)
            record = _replace(
                record,
                state=ToolExecutionState.PREPARED,
                version=event.stream_version,
                claimant_run_id=None,
                claim_token=None,
                unknown_reason=None,
            )
        elif event.event_type in (
            "tool.execution-succeeded.v1",
            "tool.execution-failed.v1",
        ):
            _require_event_claim(record, payload, allow_unknown=True)
            result = payload["result"]
            state = (
                ToolExecutionState.SUCCEEDED
                if event.event_type == "tool.execution-succeeded.v1"
                else ToolExecutionState.FAILED
            )
            record = _replace(
                record,
                state=state,
                version=event.stream_version,
                result=DurableToolResult(result["content"], bool(result["is_error"])),
                result_sha256=payload["result_sha256"],
                result_bytes=int(payload["result_bytes"]),
                unknown_reason=None,
            )
        elif event.event_type == "tool.execution-outcome-unknown.v1":
            _require_event_claim(record, payload)
            record = _replace(
                record,
                state=ToolExecutionState.OUTCOME_UNKNOWN,
                version=event.stream_version,
                unknown_reason=payload["reason"],
            )
        else:
            raise EventStoreError("unknown tool ledger event")
    if record is None:  # pragma: no cover - guarded by caller
        raise EventStoreError("empty tool ledger stream")
    return record


def _require_event_claim(
    record: ToolExecutionRecord,
    payload: Mapping[str, Any],
    *,
    allow_unknown: bool = False,
) -> None:
    allowed_states = (
        (ToolExecutionState.CLAIMED, ToolExecutionState.OUTCOME_UNKNOWN)
        if allow_unknown
        else (ToolExecutionState.CLAIMED,)
    )
    if (
        record.state not in allowed_states
        or record.claim_token is None
        or str(record.claim_token) != payload.get("claim_token")
    ):
        raise EventStoreError("stale tool claim event")


def _replace(record: ToolExecutionRecord, **changes: Any) -> ToolExecutionRecord:
    values = {
        name: getattr(record, name)
        for name in record.__dataclass_fields__
    }
    values.update(changes)
    return ToolExecutionRecord(**values)
