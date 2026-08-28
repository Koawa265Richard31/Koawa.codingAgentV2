"""I7 durable ledger for workspace side effects.

The ledger linearizes effects which cannot be made transactional with the
event store (Git worktree operations, artifact application, delivery, ...).
Only bounded identities and evidence digests are durable; filesystem paths,
diffs and process output never enter these events.
"""

from __future__ import annotations
from koawa_agent_v2.telemetry.faults import FaultPoint

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from ..control.event_store import (
    AppendReceipt,
    EventMetadata,
    IdempotencyConflict,
    NewEvent,
    StoredEvent,
    StreamId,
    StreamWrite,
    WrongExpectedVersion,
)
from ..control.run_effects import RunEffectIndex, effect_identity_digest
from ..telemetry.faults import FaultPort, NO_OP_FAULT_PORT


_SHA256 = re.compile(r"[0-9a-f]{64}")
_STABLE_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
_PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}")
_MAX_RESOURCE_REF_BYTES = 4096
_MAX_RESULT_JSON_BYTES = 8192


class WorkspaceEffectError(RuntimeError):
    """Stable, content-free workspace-effect failure."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _STABLE_CODE.fullmatch(code):
            raise ValueError("invalid workspace effect error code")
        self.code = code
        super().__init__(code)


class WorkspaceEffectConflict(WorkspaceEffectError):
    """One stable command identity was reused for different semantics."""


class WorkspaceEffectKind(StrEnum):
    WORKTREE_ADD = "worktree_add"
    WORKTREE_REMOVE = "worktree_remove"
    ARTIFACT_APPLY = "artifact_apply"
    ARTIFACT_RETEST = "artifact_retest"
    ARTIFACT_DELIVER = "artifact_deliver"
    JOURNAL_EXPORT = "journal_export"


class WorkspaceEffectState(StrEnum):
    INTENDED = "intended"
    CLAIMED = "claimed"
    APPLIED = "applied"
    FAILED_BEFORE_EFFECT = "failed_before_effect"
    OUTCOME_UNKNOWN = "outcome_unknown"


class WorkspaceEffectResultKind(StrEnum):
    SUCCESS = "success"
    KNOWN_NEGATIVE = "known_negative"


class WorkspaceEffectResolvedState(StrEnum):
    APPLIED = "applied"
    FAILED_BEFORE_EFFECT = "failed_before_effect"


@dataclass(frozen=True, slots=True)
class WorkspaceEffectRecord:
    effect_id: UUID
    semantic_command_id: UUID
    kind: WorkspaceEffectKind
    repository_identity_digest: str
    agent_id: UUID | None
    run_id: UUID
    resource_nonce: UUID
    resource_ref: str
    base_digest: str | None
    input_digest: str
    precondition_digest: str
    expected_postcondition_digest: str
    intended_at: datetime
    state: WorkspaceEffectState
    version: int
    last_event_id: UUID
    claim_epoch: int = 0
    claim_token: UUID | None = None
    owner_id: str | None = None
    result_kind: WorkspaceEffectResultKind | None = None
    result_code: str | None = None
    exit_code: int | None = None
    postcondition_digest: str | None = None
    evidence_digest: str | None = None
    error_code: str | None = None
    uncertainty_code: str | None = None
    unknown_event_id: UUID | None = None
    resolved_by_event_id: UUID | None = None
    result: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceEffectWrite:
    """A transition result including the durable idempotency receipt."""

    record: WorkspaceEffectRecord
    receipt: AppendReceipt


def workspace_effect_id(
    kind: WorkspaceEffectKind,
    semantic_command_id: UUID,
) -> UUID:
    if not isinstance(kind, WorkspaceEffectKind):
        raise TypeError("kind must be WorkspaceEffectKind")
    if not isinstance(semantic_command_id, UUID):
        raise TypeError("semantic_command_id must be UUID")
    return uuid5(
        NAMESPACE_URL,
        "koawa-v2:workspace-effect:" + kind.value + ":" + str(semantic_command_id),
    )


def workspace_resource_nonce(effect_id: UUID) -> UUID:
    if not isinstance(effect_id, UUID):
        raise TypeError("effect_id must be UUID")
    return uuid5(effect_id, "resource")


class WorkspaceEffectStore:
    """Event-sourced workspace-effect state machine with exact CAS fences."""

    def __init__(
        self,
        event_store,
        *,
        actor: str = "workspace-effect",
        clock: Callable[[], datetime] | None = None,
        fault_port: FaultPort = NO_OP_FAULT_PORT,
    ) -> None:
        self._store = event_store
        self._actor = _principal(actor, "actor")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not callable(getattr(fault_port, "hit", None)):
            raise TypeError("fault_port must implement FaultPort")
        self._fault_port = fault_port

    def intend(
        self,
        *,
        semantic_command_id: UUID,
        kind: WorkspaceEffectKind,
        repository_identity_digest: str,
        agent_id: UUID | None,
        run_id: UUID,
        resource_ref: str,
        base_digest: str | None,
        input_digest: str,
        precondition_digest: str,
        expected_postcondition_digest: str,
    ) -> WorkspaceEffectWrite:
        effect_id = workspace_effect_id(kind, semantic_command_id)
        payload = {
            "effect_id": str(effect_id),
            "semantic_command_id": str(semantic_command_id),
            "kind": kind.value,
            "repository_identity_digest": _digest(repository_identity_digest),
            "agent_id": str(agent_id) if agent_id is not None else None,
            "run_id": str(_uuid(run_id, "run_id")),
            "resource_nonce": str(workspace_resource_nonce(effect_id)),
            "resource_ref": _resource_ref(resource_ref),
            "base_digest": _optional_digest(base_digest),
            "input_digest": _digest(input_digest),
            "precondition_digest": _digest(precondition_digest),
            "expected_postcondition_digest": _digest(expected_postcondition_digest),
            "intended_at": _timestamp(self._now()),
        }
        if agent_id is not None:
            _uuid(agent_id, "agent_id")
        return self._append(
            effect_id,
            expected_version=-1,
            command_id=semantic_command_id,
            action="intend",
            event_type="workspace.effect-intended.v2",
            schema_version=2,
            payload=payload,
        )

    def claim(
        self,
        effect_id: UUID,
        *,
        expected_version: int,
        owner_id: str,
    ) -> WorkspaceEffectWrite:
        current = self._require(effect_id)
        # A response-loss retry observes CLAIMED (or even a later terminal
        # state) but must address the original claim command and receipt.
        epoch = current.claim_epoch or 1
        command_id = uuid5(effect_id, f"claim:{epoch}")
        claim_token = uuid5(command_id, "claim-token")
        payload = {
            "effect_id": str(effect_id),
            "claim_epoch": epoch,
            "claim_token": str(claim_token),
            "owner_id": _principal(owner_id, "owner_id"),
            "claimed_at": _timestamp(self._now()),
        }
        return self._transition(
            current,
            expected_version=expected_version,
            command_id=command_id,
            action="claim",
            event_type="workspace.effect-claimed.v1",
            payload=payload,
            required_state=WorkspaceEffectState.INTENDED,
        )

    def record_applied(
        self,
        effect_id: UUID,
        *,
        expected_version: int,
        claim_epoch: int,
        claim_token: UUID,
        result_kind: WorkspaceEffectResultKind,
        result_code: str,
        exit_code: int | None,
        postcondition_digest: str,
        evidence_digest: str,
        result: Mapping[str, Any] | None = None,
    ) -> WorkspaceEffectWrite:
        current = self._require(effect_id)
        point = _workspace_fault_point(current.kind, "after_effect_before_ack")
        if point is not None:
            self._fault_port.hit(
                point,
                {"effect_id": str(effect_id), "version": current.version,
                 "kind": current.kind.value},
            )
        payload = {
            "effect_id": str(effect_id),
            "claim_epoch": _epoch(claim_epoch),
            "claim_token": str(_uuid(claim_token, "claim_token")),
            "result_kind": _result_kind(result_kind).value,
            "result_code": _code(result_code, "result_code"),
            "exit_code": _exit_code(exit_code),
            "postcondition_digest": _digest(postcondition_digest),
            "evidence_digest": _digest(evidence_digest),
            "result": (
                None
                if result is None
                else dict(_bounded_result(result) or {})
            ),
            "applied_at": _timestamp(self._now()),
        }
        return self._claimed_terminal(
            current,
            expected_version,
            claim_epoch,
            claim_token,
            uuid5(claim_token, "applied"),
            "apply",
            "workspace.effect-applied.v2",
            2,
            payload,
        )

    def record_failed_before_effect(
        self,
        effect_id: UUID,
        *,
        expected_version: int,
        claim_epoch: int,
        claim_token: UUID,
        error_code: str,
        evidence_digest: str,
    ) -> WorkspaceEffectWrite:
        current = self._require(effect_id)
        payload = {
            "effect_id": str(effect_id),
            "claim_epoch": _epoch(claim_epoch),
            "claim_token": str(_uuid(claim_token, "claim_token")),
            "error_code": _code(error_code, "error_code"),
            "evidence_digest": _digest(evidence_digest),
            "failed_at": _timestamp(self._now()),
        }
        return self._claimed_terminal(
            current,
            expected_version,
            claim_epoch,
            claim_token,
            uuid5(claim_token, "failed-before-effect"),
            "fail-before-effect",
            "workspace.effect-failed-before-effect.v1",
            1,
            payload,
        )

    def record_outcome_unknown(
        self,
        effect_id: UUID,
        *,
        expected_version: int,
        claim_epoch: int,
        claim_token: UUID,
        uncertainty_code: str,
        evidence_digest: str | None,
    ) -> WorkspaceEffectWrite:
        current = self._require(effect_id)
        payload = {
            "effect_id": str(effect_id),
            "claim_epoch": _epoch(claim_epoch),
            "claim_token": str(_uuid(claim_token, "claim_token")),
            "uncertainty_code": _code(uncertainty_code, "uncertainty_code"),
            "evidence_digest": _optional_digest(evidence_digest),
            "observed_at": _timestamp(self._now()),
        }
        return self._claimed_terminal(
            current,
            expected_version,
            claim_epoch,
            claim_token,
            uuid5(claim_token, "outcome-unknown"),
            "outcome-unknown",
            "workspace.effect-outcome-unknown.v1",
            1,
            payload,
        )

    def resolve_unknown(
        self,
        effect_id: UUID,
        *,
        expected_version: int,
        claim_epoch: int,
        claim_token: UUID,
        unknown_event_id: UUID,
        resolved_state: WorkspaceEffectResolvedState,
        result_kind: WorkspaceEffectResultKind | None,
        reconciler_principal: str,
        evidence_kind: str,
        evidence_digest: str,
    ) -> WorkspaceEffectWrite:
        current = self._require(effect_id)
        if not isinstance(resolved_state, WorkspaceEffectResolvedState):
            raise TypeError("resolved_state must be WorkspaceEffectResolvedState")
        if resolved_state is WorkspaceEffectResolvedState.APPLIED:
            if result_kind is None:
                raise WorkspaceEffectError("workspace_effect_result_kind_required")
            normalized_result_kind: str | None = _result_kind(result_kind).value
        else:
            if result_kind is not None:
                raise WorkspaceEffectError("workspace_effect_result_kind_forbidden")
            normalized_result_kind = None
        evidence_kind = _code(evidence_kind, "evidence_kind")
        if evidence_kind in {"operator_text", "free_text", "manual_text"}:
            raise WorkspaceEffectError("workspace_effect_evidence_not_authoritative")
        payload = {
            "effect_id": str(effect_id),
            "claim_epoch": _epoch(claim_epoch),
            "claim_token": str(_uuid(claim_token, "claim_token")),
            "unknown_event_id": str(_uuid(unknown_event_id, "unknown_event_id")),
            "resolved_state": resolved_state.value,
            "result_kind": normalized_result_kind,
            "reconciler_principal": _principal(
                reconciler_principal, "reconciler_principal"
            ),
            "evidence_kind": evidence_kind,
            "evidence_digest": _digest(evidence_digest),
            "resolved_at": _timestamp(self._now()),
        }
        command_id = uuid5(unknown_event_id, "resolve")
        fingerprint = _fingerprint("resolve-unknown", expected_version, payload)
        prior = self._idempotent(command_id, fingerprint, effect_id)
        if prior is not None:
            return prior
        if current.state is not WorkspaceEffectState.OUTCOME_UNKNOWN:
            raise WorkspaceEffectError("workspace_effect_not_unknown")
        self._require_expected(current, expected_version)
        self._require_claim(current, claim_epoch, claim_token)
        if current.unknown_event_id != unknown_event_id:
            raise WorkspaceEffectError("workspace_effect_unknown_event_mismatch")
        return self._append(
            effect_id,
            expected_version=expected_version,
            command_id=command_id,
            action="resolve-unknown",
            event_type="workspace.effect-outcome-resolved.v1",
            schema_version=1,
            payload=payload,
            fingerprint=fingerprint,
        )

    def load(self, effect_id: UUID) -> WorkspaceEffectRecord | None:
        _uuid(effect_id, "effect_id")
        events = self._read_all(self._stream(effect_id))
        if not events:
            return None
        return _reduce(events, expected_effect_id=effect_id)

    def _claimed_terminal(
        self,
        current: WorkspaceEffectRecord,
        expected_version: int,
        claim_epoch: int,
        claim_token: UUID,
        command_id: UUID,
        action: str,
        event_type: str,
        schema_version: int,
        payload: Mapping[str, Any],
    ) -> WorkspaceEffectWrite:
        fingerprint = _fingerprint(action, expected_version, payload)
        prior = self._idempotent(command_id, fingerprint, current.effect_id)
        if prior is not None:
            return prior
        if current.state is not WorkspaceEffectState.CLAIMED:
            raise WorkspaceEffectError("workspace_effect_not_claimed")
        self._require_expected(current, expected_version)
        self._require_claim(current, claim_epoch, claim_token)
        return self._append(
            current.effect_id,
            expected_version=expected_version,
            command_id=command_id,
            action=action,
            event_type=event_type,
            schema_version=schema_version,
            payload=payload,
            fingerprint=fingerprint,
        )

    def _transition(
        self,
        current: WorkspaceEffectRecord,
        *,
        expected_version: int,
        command_id: UUID,
        action: str,
        event_type: str,
        payload: Mapping[str, Any],
        required_state: WorkspaceEffectState,
    ) -> WorkspaceEffectWrite:
        fingerprint = _fingerprint(action, expected_version, payload)
        prior = self._idempotent(command_id, fingerprint, current.effect_id)
        if prior is not None:
            return prior
        if current.state is not required_state:
            raise WorkspaceEffectError("workspace_effect_invalid_transition")
        self._require_expected(current, expected_version)
        return self._append(
            current.effect_id,
            expected_version=expected_version,
            command_id=command_id,
            action=action,
            event_type=event_type,
            schema_version=1,
            payload=payload,
            fingerprint=fingerprint,
        )

    def _append(
        self,
        effect_id: UUID,
        *,
        expected_version: int,
        command_id: UUID,
        action: str,
        event_type: str,
        schema_version: int,
        payload: Mapping[str, Any],
        fingerprint: str | None = None,
    ) -> WorkspaceEffectWrite:
        resolved_fingerprint = fingerprint or _fingerprint(
            action, expected_version, payload
        )
        prior = self._idempotent(command_id, resolved_fingerprint, effect_id)
        if prior is not None:
            return prior
        event = NewEvent(
            uuid5(command_id, "event:" + event_type),
            event_type,
            schema_version,
            self._now(),
            payload,
            EventMetadata(
                command_id,
                effect_id,
                run_id=_payload_run_id(payload),
                actor=self._actor,
            ),
        )
        writes = [StreamWrite(self._stream(effect_id), expected_version, (event,))]
        run_id = _payload_run_id(payload)
        link_index = event_type == "workspace.effect-intended.v2" and run_id is not None
        try:
            for attempt in range(4):
                current_writes = list(writes)
                if link_index:
                    current_writes.append(
                        RunEffectIndex(self._store).link_write(
                            run_id=run_id,
                            effect_kind="workspace",
                            effect_stream=self._stream(effect_id),
                            identity_digest=effect_identity_digest({
                                key: value for key, value in payload.items()
                                if not key.endswith("_at")
                            }),
                            first_version=0,
                            command_id=command_id,
                            actor=self._actor,
                        )
                    )
                try:
                    receipt = self._store.append_batch(
                        tuple(current_writes),
                        idempotency_key=command_id,
                        request_fingerprint=resolved_fingerprint,
                    )
                    break
                except WrongExpectedVersion:
                    if not link_index or attempt == 3:
                        raise
        except IdempotencyConflict:
            raise WorkspaceEffectConflict("workspace_effect_idempotency_conflict") from None
        except WrongExpectedVersion:
            raise WorkspaceEffectError("workspace_effect_version_conflict") from None
        record = self.load(effect_id)
        if record is None:
            raise WorkspaceEffectError("workspace_effect_write_missing")
        phase = {
            "workspace.effect-intended.v2": "after_intent_commit",
            "workspace.effect-claimed.v1": "after_claim_commit",
        }.get(event_type)
        if phase is not None:
            point = _workspace_fault_point(record.kind, phase)
            if point is not None:
                self._fault_port.hit(
                    point,
                    {"effect_id": str(effect_id), "version": record.version,
                     "kind": record.kind.value},
                )
        return WorkspaceEffectWrite(record, receipt)

    def _idempotent(
        self,
        command_id: UUID,
        fingerprint: str,
        effect_id: UUID,
    ) -> WorkspaceEffectWrite | None:
        try:
            receipt = self._store.read_idempotency(
                command_id, request_fingerprint=fingerprint
            )
        except IdempotencyConflict:
            raise WorkspaceEffectConflict("workspace_effect_idempotency_conflict") from None
        if receipt is None:
            return None
        record = self.load(effect_id)
        if record is None:
            raise WorkspaceEffectError("workspace_effect_receipt_without_effect")
        return WorkspaceEffectWrite(record, receipt)

    def _require(self, effect_id: UUID) -> WorkspaceEffectRecord:
        record = self.load(effect_id)
        if record is None:
            raise WorkspaceEffectError("workspace_effect_missing")
        return record

    @staticmethod
    def _require_expected(current: WorkspaceEffectRecord, expected: int) -> None:
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < -1:
            raise TypeError("expected_version must be an integer >= -1")
        if current.version != expected:
            raise WorkspaceEffectError("workspace_effect_version_conflict")

    @staticmethod
    def _require_claim(
        current: WorkspaceEffectRecord, claim_epoch: int, claim_token: UUID
    ) -> None:
        if current.claim_epoch != _epoch(claim_epoch):
            raise WorkspaceEffectError("workspace_effect_claim_epoch_mismatch")
        if current.claim_token != _uuid(claim_token, "claim_token"):
            raise WorkspaceEffectError("workspace_effect_claim_token_mismatch")

    @staticmethod
    def _stream(effect_id: UUID) -> StreamId:
        return StreamId("workspace-effect", _uuid(effect_id, "effect_id"))

    def _read_all(self, stream: StreamId) -> tuple[StoredEvent, ...]:
        values: list[StoredEvent] = []
        cursor = -1
        while True:
            page = self._store.read_stream(stream, after_version=cursor, limit=500)
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)


_INTENDED_KEYS = frozenset({
    "effect_id", "semantic_command_id", "kind", "repository_identity_digest",
    "agent_id", "run_id", "resource_nonce", "resource_ref", "base_digest",
    "input_digest", "precondition_digest", "expected_postcondition_digest",
    "intended_at",
})
_CLAIMED_KEYS = frozenset({
    "effect_id", "claim_epoch", "claim_token", "owner_id", "claimed_at",
})
_APPLIED_KEYS = frozenset({
    "effect_id", "claim_epoch", "claim_token", "result_kind", "result_code",
    "exit_code", "postcondition_digest", "evidence_digest", "result", "applied_at",
})
_FAILED_KEYS = frozenset({
    "effect_id", "claim_epoch", "claim_token", "error_code", "evidence_digest",
    "failed_at",
})
_UNKNOWN_KEYS = frozenset({
    "effect_id", "claim_epoch", "claim_token", "uncertainty_code",
    "evidence_digest", "observed_at",
})
_RESOLVED_KEYS = frozenset({
    "effect_id", "claim_epoch", "claim_token", "unknown_event_id",
    "resolved_state", "result_kind", "reconciler_principal", "evidence_kind",
    "evidence_digest", "resolved_at",
})


def _reduce(
    events: tuple[StoredEvent, ...], *, expected_effect_id: UUID
) -> WorkspaceEffectRecord:
    state: WorkspaceEffectRecord | None = None
    for index, event in enumerate(events):
        if event.stream_version != index:
            raise WorkspaceEffectError("workspace_effect_history_not_contiguous")
        payload = dict(event.payload)
        _exact_effect(payload, expected_effect_id)
        if event.event_type == "workspace.effect-intended.v2":
            _keys(payload, _INTENDED_KEYS)
            if state is not None or index != 0:
                raise WorkspaceEffectError("workspace_effect_invalid_history")
            kind = _enum(WorkspaceEffectKind, payload["kind"], "workspace_effect_invalid_kind")
            semantic = _parse_uuid(payload["semantic_command_id"])
            if workspace_effect_id(kind, semantic) != expected_effect_id:
                raise WorkspaceEffectError("workspace_effect_identity_mismatch")
            nonce = _parse_uuid(payload["resource_nonce"])
            if nonce != workspace_resource_nonce(expected_effect_id):
                raise WorkspaceEffectError("workspace_effect_resource_nonce_mismatch")
            raw_agent = payload["agent_id"]
            agent = None if raw_agent is None else _parse_uuid(raw_agent)
            state = WorkspaceEffectRecord(
                effect_id=expected_effect_id,
                semantic_command_id=semantic,
                kind=kind,
                repository_identity_digest=_digest(payload["repository_identity_digest"]),
                agent_id=agent,
                run_id=_parse_uuid(payload["run_id"]),
                resource_nonce=nonce,
                resource_ref=_resource_ref(payload["resource_ref"]),
                base_digest=_optional_digest(payload["base_digest"]),
                input_digest=_digest(payload["input_digest"]),
                precondition_digest=_digest(payload["precondition_digest"]),
                expected_postcondition_digest=_digest(payload["expected_postcondition_digest"]),
                intended_at=_parse_timestamp(payload["intended_at"]),
                state=WorkspaceEffectState.INTENDED,
                version=index,
                last_event_id=event.event_id,
            )
            continue
        if state is None:
            raise WorkspaceEffectError("workspace_effect_missing_intent")
        if event.event_type == "workspace.effect-claimed.v1":
            _keys(payload, _CLAIMED_KEYS)
            if state.state is not WorkspaceEffectState.INTENDED:
                raise WorkspaceEffectError("workspace_effect_invalid_history")
            epoch = _epoch(payload["claim_epoch"])
            if epoch != state.claim_epoch + 1:
                raise WorkspaceEffectError("workspace_effect_claim_epoch_mismatch")
            _parse_timestamp(payload["claimed_at"])
            state = replace(
                state,
                state=WorkspaceEffectState.CLAIMED,
                version=index,
                last_event_id=event.event_id,
                claim_epoch=epoch,
                claim_token=_parse_uuid(payload["claim_token"]),
                owner_id=_principal(payload["owner_id"], "owner_id"),
            )
        elif event.event_type == "workspace.effect-applied.v2":
            _keys(payload, _APPLIED_KEYS)
            _history_claim(state, payload)
            result_kind = _enum(
                WorkspaceEffectResultKind, payload["result_kind"],
                "workspace_effect_invalid_result_kind",
            )
            _parse_timestamp(payload["applied_at"])
            state = replace(
                state,
                state=WorkspaceEffectState.APPLIED,
                version=index,
                last_event_id=event.event_id,
                result_kind=result_kind,
                result_code=_code(payload["result_code"], "result_code"),
                exit_code=_exit_code(payload["exit_code"]),
                postcondition_digest=_digest(payload["postcondition_digest"]),
                evidence_digest=_digest(payload["evidence_digest"]),
                result=_bounded_result(payload["result"]),
            )
        elif event.event_type == "workspace.effect-failed-before-effect.v1":
            _keys(payload, _FAILED_KEYS)
            _history_claim(state, payload)
            _parse_timestamp(payload["failed_at"])
            state = replace(
                state,
                state=WorkspaceEffectState.FAILED_BEFORE_EFFECT,
                version=index,
                last_event_id=event.event_id,
                error_code=_code(payload["error_code"], "error_code"),
                evidence_digest=_digest(payload["evidence_digest"]),
            )
        elif event.event_type == "workspace.effect-outcome-unknown.v1":
            _keys(payload, _UNKNOWN_KEYS)
            _history_claim(state, payload)
            _parse_timestamp(payload["observed_at"])
            state = replace(
                state,
                state=WorkspaceEffectState.OUTCOME_UNKNOWN,
                version=index,
                last_event_id=event.event_id,
                uncertainty_code=_code(payload["uncertainty_code"], "uncertainty_code"),
                evidence_digest=_optional_digest(payload["evidence_digest"]),
                unknown_event_id=event.event_id,
            )
        elif event.event_type == "workspace.effect-outcome-resolved.v1":
            _keys(payload, _RESOLVED_KEYS)
            if state.state is not WorkspaceEffectState.OUTCOME_UNKNOWN:
                raise WorkspaceEffectError("workspace_effect_invalid_history")
            _history_claim(state, payload, require_claimed_state=False)
            if _parse_uuid(payload["unknown_event_id"]) != state.unknown_event_id:
                raise WorkspaceEffectError("workspace_effect_unknown_event_mismatch")
            resolved = _enum(
                WorkspaceEffectResolvedState, payload["resolved_state"],
                "workspace_effect_invalid_resolved_state",
            )
            if resolved is WorkspaceEffectResolvedState.APPLIED:
                result_kind = _enum(
                    WorkspaceEffectResultKind, payload["result_kind"],
                    "workspace_effect_invalid_result_kind",
                )
                next_state = WorkspaceEffectState.APPLIED
            else:
                if payload["result_kind"] is not None:
                    raise WorkspaceEffectError("workspace_effect_result_kind_forbidden")
                result_kind = None
                next_state = WorkspaceEffectState.FAILED_BEFORE_EFFECT
            _principal(payload["reconciler_principal"], "reconciler_principal")
            evidence_kind = _code(payload["evidence_kind"], "evidence_kind")
            if evidence_kind in {"operator_text", "free_text", "manual_text"}:
                raise WorkspaceEffectError("workspace_effect_evidence_not_authoritative")
            _parse_timestamp(payload["resolved_at"])
            state = replace(
                state,
                state=next_state,
                version=index,
                last_event_id=event.event_id,
                result_kind=result_kind,
                evidence_digest=_digest(payload["evidence_digest"]),
                resolved_by_event_id=event.event_id,
            )
        else:
            raise WorkspaceEffectError("workspace_effect_unknown_event")
    if state is None:
        raise WorkspaceEffectError("workspace_effect_missing_intent")
    return state


def _history_claim(
    state: WorkspaceEffectRecord,
    payload: Mapping[str, Any],
    *,
    require_claimed_state: bool = True,
) -> None:
    if require_claimed_state and state.state is not WorkspaceEffectState.CLAIMED:
        raise WorkspaceEffectError("workspace_effect_invalid_history")
    if _epoch(payload["claim_epoch"]) != state.claim_epoch:
        raise WorkspaceEffectError("workspace_effect_claim_epoch_mismatch")
    if _parse_uuid(payload["claim_token"]) != state.claim_token:
        raise WorkspaceEffectError("workspace_effect_claim_token_mismatch")


def _keys(payload: Mapping[str, Any], expected: frozenset[str]) -> None:
    if frozenset(payload) != expected:
        raise WorkspaceEffectError("workspace_effect_invalid_payload")


def _exact_effect(payload: Mapping[str, Any], expected: UUID) -> None:
    try:
        actual = _parse_uuid(payload["effect_id"])
    except KeyError:
        raise WorkspaceEffectError("workspace_effect_invalid_payload") from None
    if actual != expected:
        raise WorkspaceEffectError("workspace_effect_identity_mismatch")


def _fingerprint(action: str, expected_version: int, payload: Mapping[str, Any]) -> str:
    # Domain timestamps describe the first observed transition, not its
    # semantics.  Omitting them lets a response-loss retry made later recover
    # the original EventStore receipt instead of conflicting on wall-clock time.
    semantic_payload = {
        key: value for key, value in payload.items() if not key.endswith("_at")
    }
    return _canonical_json({
        "action": action,
        "expected_version": expected_version,
        "payload": semantic_payload,
    })


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _bounded_result(value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("result must be a mapping or None")
    try:
        encoded = _canonical_json(dict(value)).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError):
        raise WorkspaceEffectError("workspace_effect_invalid_result") from None
    if len(encoded) > _MAX_RESULT_JSON_BYTES:
        raise WorkspaceEffectError("workspace_effect_result_too_large")
    # A JSON round-trip both rejects non-JSON objects and severs mutable aliases.
    decoded = json.loads(encoded.decode("utf-8"))
    return MappingProxyType(decoded)


def _resource_ref(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise WorkspaceEffectError("workspace_effect_invalid_resource_ref")
    try:
        raw = value.encode("utf-8", "strict")
    except UnicodeError:
        raise WorkspaceEffectError("workspace_effect_invalid_resource_ref") from None
    normalized = value.replace("\\", "/")
    if (
        len(raw) > _MAX_RESOURCE_REF_BYTES
        or normalized.startswith("/")
        or normalized.startswith("//")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part == ".." for part in normalized.split("/"))
    ):
        raise WorkspaceEffectError("workspace_effect_invalid_resource_ref")
    return normalized


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise WorkspaceEffectError("workspace_effect_invalid_digest")
    return value


def _optional_digest(value: Any) -> str | None:
    return None if value is None else _digest(value)


def _code(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _STABLE_CODE.fullmatch(value):
        raise WorkspaceEffectError("workspace_effect_invalid_" + field)
    return value


def _principal(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _PRINCIPAL.fullmatch(value):
        raise WorkspaceEffectError("workspace_effect_invalid_" + field)
    return value


def _uuid(value: Any, field: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(field + " must be UUID")
    return value


def _parse_uuid(value: Any) -> UUID:
    if not isinstance(value, str):
        raise WorkspaceEffectError("workspace_effect_invalid_uuid")
    try:
        parsed = UUID(value)
    except ValueError:
        raise WorkspaceEffectError("workspace_effect_invalid_uuid") from None
    if str(parsed) != value:
        raise WorkspaceEffectError("workspace_effect_invalid_uuid")
    return parsed


def _epoch(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise WorkspaceEffectError("workspace_effect_invalid_claim_epoch")
    return value


def _exit_code(value: Any) -> int | None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise WorkspaceEffectError("workspace_effect_invalid_exit_code")
    return value


def _result_kind(value: Any) -> WorkspaceEffectResultKind:
    if not isinstance(value, WorkspaceEffectResultKind):
        raise TypeError("result_kind must be WorkspaceEffectResultKind")
    return value


def _enum(enum_type, value: Any, code: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        raise WorkspaceEffectError(code) from None


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise WorkspaceEffectError("workspace_effect_invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise WorkspaceEffectError("workspace_effect_invalid_timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise WorkspaceEffectError("workspace_effect_invalid_timestamp")
    if _timestamp(parsed) != value:
        raise WorkspaceEffectError("workspace_effect_invalid_timestamp")
    return parsed


def _payload_run_id(payload: Mapping[str, Any]) -> UUID | None:
    value = payload.get("run_id")
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _workspace_fault_point(
    kind: WorkspaceEffectKind, phase: str
) -> str | None:
    return {
        (WorkspaceEffectKind.WORKTREE_ADD, "after_intent_commit"):
            FaultPoint.S5_WORKSPACE_ADD_AFTER_INTENT_COMMIT,
        (WorkspaceEffectKind.WORKTREE_ADD, "after_claim_commit"):
            FaultPoint.S5_WORKSPACE_ADD_AFTER_CLAIM_COMMIT,
        (WorkspaceEffectKind.WORKTREE_ADD, "after_effect_before_ack"):
            FaultPoint.S5_WORKSPACE_ADD_AFTER_EFFECT_BEFORE_ACK,
        (WorkspaceEffectKind.WORKTREE_REMOVE, "after_intent_commit"):
            FaultPoint.S5_WORKSPACE_REMOVE_AFTER_INTENT_COMMIT,
        (WorkspaceEffectKind.WORKTREE_REMOVE, "after_claim_commit"):
            FaultPoint.S5_WORKSPACE_REMOVE_AFTER_CLAIM_COMMIT,
        (WorkspaceEffectKind.WORKTREE_REMOVE, "after_effect_before_ack"):
            FaultPoint.S5_WORKSPACE_REMOVE_AFTER_EFFECT_BEFORE_ACK,
        (WorkspaceEffectKind.ARTIFACT_APPLY, "after_intent_commit"):
            FaultPoint.S5_WORKSPACE_APPLY_AFTER_INTENT_COMMIT,
        (WorkspaceEffectKind.ARTIFACT_APPLY, "after_claim_commit"):
            FaultPoint.S5_WORKSPACE_APPLY_AFTER_CLAIM_COMMIT,
        (WorkspaceEffectKind.ARTIFACT_APPLY, "after_effect_before_ack"):
            FaultPoint.S5_WORKSPACE_APPLY_AFTER_EFFECT_BEFORE_ACK,
        (WorkspaceEffectKind.ARTIFACT_RETEST, "after_intent_commit"):
            FaultPoint.S5_WORKSPACE_RETEST_AFTER_INTENT_COMMIT,
        (WorkspaceEffectKind.ARTIFACT_RETEST, "after_claim_commit"):
            FaultPoint.S5_WORKSPACE_RETEST_AFTER_CLAIM_COMMIT,
        (WorkspaceEffectKind.ARTIFACT_RETEST, "after_effect_before_ack"):
            FaultPoint.S5_WORKSPACE_RETEST_AFTER_EFFECT_BEFORE_ACK,
        (WorkspaceEffectKind.ARTIFACT_DELIVER, "after_intent_commit"):
            FaultPoint.S5_WORKSPACE_DELIVER_AFTER_INTENT_COMMIT,
        (WorkspaceEffectKind.ARTIFACT_DELIVER, "after_claim_commit"):
            FaultPoint.S5_WORKSPACE_DELIVER_AFTER_CLAIM_COMMIT,
        (WorkspaceEffectKind.ARTIFACT_DELIVER, "after_effect_before_ack"):
            FaultPoint.S5_WORKSPACE_DELIVER_AFTER_EFFECT_BEFORE_ACK,
    }.get((kind, phase))


__all__ = [
    "WorkspaceEffectConflict",
    "WorkspaceEffectError",
    "WorkspaceEffectKind",
    "WorkspaceEffectRecord",
    "WorkspaceEffectResolvedState",
    "WorkspaceEffectResultKind",
    "WorkspaceEffectState",
    "WorkspaceEffectStore",
    "WorkspaceEffectWrite",
    "workspace_effect_id",
    "workspace_resource_nonce",
]
