"""RT/J J2: session-canary exact detection and security-state store.

Contract (plan v1.1 §3 J2):
- Canary tokens are HMAC-SHA256-derived high-entropy strings; only an exact
  byte occurrence inside canonicalized/resolved arguments is a hit.  No
  encoding/variant claims are made.
- The security-state aggregate is execution-scoped
  (uuid5 of ``koawa-v2:security-execution:v1:{execution_id}``) and persists
  escalations across runs (sticky).  Payloads are digest-only: never the
  token value, never raw arguments.
- The gate is advisory-only in the sense of plan §3 J2: a detector/local
  fault falls back to the base policy verdict; a *persisted* escalation is
  never weakenable (PENDING waits, DENIED denies, GRANTED must be consumed,
  EXPIRED requires a fresh explicit approval).
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from ..control.event_store import (
    EventMetadata,
    EventStore,
    NewEvent,
    StreamId,
    StreamPrecondition,
    StreamWrite,
)

SECURITY_SIGNAL_EVENT = "security.signal.v1"
POLICY_ESCALATED_EVENT = "policy.escalated.v1"
SECURITY_SCHEMA_VERSION = 1

ESCALATION_PENDING = "PENDING"
ESCALATION_DENIED = "DENIED"
ESCALATION_GRANTED = "GRANTED"
ESCALATION_CONSUMED = "CONSUMED"
ESCALATION_EXPIRED = "EXPIRED"

# Status derivation from the security stream.  PENDING comes from the
# escalation record; terminal states (GRANTED/DENIED/CONSUMED) are owned by
# the approval stream and read through ApprovalService (joint ownership,
# plan §3 J2: security + approval + turn + run exact head/CAS).
_STATUS_EVENTS = {
    POLICY_ESCALATED_EVENT: ESCALATION_PENDING,
}


def derive_canary_token(key: bytes, turn_id: UUID) -> str:
    """High-entropy session canary token (HMAC-SHA256, 32 hex chars)."""
    return hmac.new(key, str(turn_id).encode("ascii"), hashlib.sha256).hexdigest()[:32]


def scan_exact_token(text: str, token: str) -> bool:
    """Byte-exact occurrence check.  No encoding or variant claims (§5.3)."""
    if not token:
        return False
    return token in text


def security_aggregate_id(execution_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"koawa-v2:security-execution:v1:{execution_id}")


def security_stream(execution_id: UUID) -> StreamId:
    return StreamId("security-state", security_aggregate_id(execution_id))


def escalation_id(execution_id: UUID, action_digest: str, policy_version: str) -> UUID:
    return uuid5(execution_id, f"j2:{action_digest}:{policy_version}")


@dataclass(frozen=True, slots=True)
class SecurityEscalation:
    execution_id: UUID
    status: str
    version: int
    action_digest: str
    policy_version: str
    reason_code: str


class SecurityStateStore:
    """Execution-scoped sticky escalation state with exact-version CAS."""

    def __init__(self, event_store: EventStore) -> None:
        if not all(hasattr(event_store, n) for n in ("append_batch", "read_stream")):
            raise TypeError("event_store must implement EventStore")
        self._store = event_store

    def _events(self, execution_id: UUID):
        cursor = -1
        while True:
            page = self._store.read_stream(
                security_stream(execution_id), after_version=cursor, limit=500
            )
            yield from page
            if len(page) < 500:
                return
            cursor = page[-1].stream_version

    def head(self, execution_id: UUID) -> int:
        last = -1
        for event in self._events(execution_id):
            last = event.stream_version
        return last

    def status(self, execution_id: UUID) -> tuple[str | None, int]:
        """Sticky status: latest status-bearing event wins (§3 J2)."""
        status = None
        version = -1
        for event in self._events(execution_id):
            version = event.stream_version
            mapped = _STATUS_EVENTS.get(event.event_type)
            if mapped is not None:
                status = mapped
        return status, version

    def propose(
        self,
        execution_id: UUID,
        *,
        signal_payload: Mapping[str, Any],
        escalated_payload: Mapping[str, Any],
        expected_head: int | None = None,
    ) -> int:
        """Append signal + escalated atomically (exact CAS, two CAS tries)."""
        command = uuid5(
            security_aggregate_id(execution_id),
            f"propose:{expected_head}",
        )
        signal = NewEvent(
            uuid5(command, "event:" + SECURITY_SIGNAL_EVENT),
            SECURITY_SIGNAL_EVENT,
            SECURITY_SCHEMA_VERSION,
            datetime.now(timezone.utc),
            dict(signal_payload),
            EventMetadata(command, command, actor="j2-detector"),
        )
        escalated = NewEvent(
            uuid5(command, "event:" + POLICY_ESCALATED_EVENT),
            POLICY_ESCALATED_EVENT,
            SECURITY_SCHEMA_VERSION,
            datetime.now(timezone.utc),
            dict(escalated_payload),
            EventMetadata(command, command, actor="j2-detector"),
        )
        head = self.head(execution_id) if expected_head is None else expected_head
        attempts = 0
        while attempts < 2:
            attempts += 1
            try:
                self._store.append_batch(
                    (
                        StreamWrite(
                            stream_id=security_stream(execution_id),
                            expected_version=head,
                            events=(signal, escalated),
                        ),
                    ),
                    idempotency_key=command,
                )
                return self.head(execution_id)
            except Exception as error:
                code = getattr(error, "code", "") or type(error).__name__
                if attempts >= 2 or "conflict" not in code.lower():
                    raise
                head = self.head(execution_id)
        return self.head(execution_id)
