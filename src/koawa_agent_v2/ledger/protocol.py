"""D7 durable tool-execution identity and recovery protocol."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5


_STABLE_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")


class ToolLedgerError(RuntimeError):
    """Stable, content-free D7 contract failure."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _STABLE_CODE.fullmatch(code):
            raise ValueError("invalid tool ledger error code")
        self.code = code
        super().__init__(code)


class ToolLedgerConflict(ToolLedgerError):
    """The same logical invocation was observed with different semantics."""


class ToolOutcomeBlocked(ToolLedgerError):
    """Automatic execution is forbidden because the prior outcome is uncertain."""

    recovery_blocked = True


class ToolExecutionState(Enum):
    PREPARED = "prepared"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class SideEffectClass(Enum):
    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    NON_IDEMPOTENT_WRITE = "non_idempotent_write"


class RecoveryMode(Enum):
    RETRY = "retry"
    AUTHORITATIVE_QUERY = "authoritative_query"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class ToolRecoveryProfile:
    """Deterministic Runtime declaration; the model cannot lower this risk."""

    side_effect_class: SideEffectClass
    recovery_mode: RecoveryMode

    def __post_init__(self) -> None:
        if not isinstance(self.side_effect_class, SideEffectClass):
            raise TypeError("side_effect_class must be SideEffectClass")
        if not isinstance(self.recovery_mode, RecoveryMode):
            raise TypeError("recovery_mode must be RecoveryMode")
        if (
            self.side_effect_class is SideEffectClass.NON_IDEMPOTENT_WRITE
            and self.recovery_mode is RecoveryMode.RETRY
        ):
            raise ValueError("non-idempotent writes cannot use blind retry")


READ_ONLY_PROFILE = ToolRecoveryProfile(
    SideEffectClass.READ_ONLY,
    RecoveryMode.RETRY,
)
IDEMPOTENT_WRITE_PROFILE = ToolRecoveryProfile(
    SideEffectClass.IDEMPOTENT_WRITE,
    RecoveryMode.RETRY,
)
QUERYABLE_WRITE_PROFILE = ToolRecoveryProfile(
    SideEffectClass.NON_IDEMPOTENT_WRITE,
    RecoveryMode.AUTHORITATIVE_QUERY,
)
MANUAL_WRITE_PROFILE = ToolRecoveryProfile(
    SideEffectClass.NON_IDEMPOTENT_WRITE,
    RecoveryMode.MANUAL,
)


@dataclass(frozen=True, slots=True, repr=False)
class LogicalExecutionIdentity:
    """I6 §8.8 / plan §10.3: ONE typed key for prepare/claim/load/recovery.

    Every boundary that derives an execution key must carry the exact same
    binding_digest (or None for builtin tools); a missing-field tuple lookup
    is a bug, not a compatibility path.
    """

    turn_id: UUID
    model_turn_id: UUID
    call_id: str
    binding_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.turn_id, UUID) or not isinstance(
            self.model_turn_id, UUID,
        ):
            raise TypeError("turn_id and model_turn_id must be UUID")
        if not isinstance(self.call_id, str) or not self.call_id:
            raise ValueError("call_id must be non-empty")
        if self.binding_digest is not None and (
            not isinstance(self.binding_digest, str)
            or len(self.binding_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.binding_digest
            )
        ):
            raise ValueError("binding_digest must be 64 hex chars")

    def derive(self) -> UUID:
        """The one stable execution key for this logical identity."""
        return logical_execution_id(
            self.turn_id,
            self.model_turn_id,
            self.call_id,
            binding_digest=self.binding_digest,
        )

    def __repr__(self) -> str:
        return (
            f"LogicalExecutionIdentity(turn_id={self.turn_id}, "
            f"model_turn_id={self.model_turn_id}, call_id={self.call_id!r}, "
            f"binding_digest={self.binding_digest})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class DurableToolResult:
    """A bounded, redacted result that may be reused after process restart."""

    content: str
    is_error: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or "\x00" in self.content:
            raise ValueError("tool result content is invalid")
        try:
            self.content.encode("utf-8", "strict")
        except UnicodeError:
            raise ValueError("tool result content is invalid") from None
        if not isinstance(self.is_error, bool):
            raise TypeError("is_error must be bool")

    def __repr__(self) -> str:
        return (
            f"DurableToolResult(content_length={len(self.content)}, "
            f"is_error={self.is_error})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ToolExecutionRecord:
    execution_id: UUID
    turn_id: UUID
    model_turn_id: UUID
    call_id: str
    tool_name: str
    arguments_sha256: str
    arguments_bytes: int
    profile: ToolRecoveryProfile
    state: ToolExecutionState
    version: int
    binding_digest: str | None = None
    claim_epoch: int = 0
    claimant_run_id: UUID | None = None
    claim_token: UUID | None = None
    result: DurableToolResult | None = None
    result_sha256: str | None = None
    result_bytes: int | None = None
    unknown_reason: str | None = None

    def __repr__(self) -> str:
        return (
            f"ToolExecutionRecord(execution_id={self.execution_id}, "
            f"state={self.state.value!r}, version={self.version}, "
            f"claim_epoch={self.claim_epoch}, result_present={self.result is not None})"
        )


class LookupOutcome(Enum):
    APPLIED = "applied"
    # A terminal negative: the old request did not apply and cannot apply later.
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LookupResult:
    outcome: LookupOutcome
    result: DurableToolResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, LookupOutcome):
            raise TypeError("outcome must be LookupOutcome")
        if self.outcome is LookupOutcome.APPLIED and self.result is None:
            raise ValueError("applied lookup requires a durable result")
        if self.outcome is not LookupOutcome.APPLIED and self.result is not None:
            raise ValueError("only applied lookup may carry a result")


class AuthoritativeLookup(Protocol):
    def __call__(self, record: ToolExecutionRecord) -> LookupResult: ...


def logical_execution_id(
    turn_id: UUID,
    model_turn_id: UUID,
    call_id: str,
    binding_digest: str | None = None,
) -> UUID:
    """Derive the stable logical key without claimant ``run_id``.

    Tool name, arguments hash, and recovery profile are immutable record fields.
    Reusing the same ModelCallRef with different semantics therefore conflicts
    instead of silently creating a second side effect.
    """

    if not isinstance(turn_id, UUID) or not isinstance(model_turn_id, UUID):
        raise TypeError("turn_id and model_turn_id must be UUID")
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("call_id must be non-empty")
    if binding_digest is not None and (
        not isinstance(binding_digest, str)
        or len(binding_digest) != 64
        or any(character not in "0123456789abcdef" for character in binding_digest)
    ):
        raise ValueError("binding_digest must be a 64-character hex digest")
    suffix = "" if binding_digest is None else f":binding:{binding_digest}"
    return uuid5(
        NAMESPACE_URL,
        f"koawa-agent-v2:{turn_id}:tool-call:{model_turn_id}:{call_id}{suffix}",
    )


def canonical_arguments_digest(arguments_json: str) -> tuple[str, int]:
    """Hash a strict, versioned canonical JSON object without persisting it."""

    if not isinstance(arguments_json, str):
        raise TypeError("arguments_json must be str")
    try:
        document = json.loads(
            arguments_json,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ToolLedgerError("invalid_tool_arguments") from None
    if not isinstance(document, dict):
        raise ToolLedgerError("invalid_tool_arguments")
    canonical = json.dumps(
        {"schema_version": 1, "arguments": document},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", "strict")
    return hashlib.sha256(canonical).hexdigest(), len(canonical)


def result_digest(content: str) -> tuple[str, int]:
    raw = content.encode("utf-8", "strict")
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
