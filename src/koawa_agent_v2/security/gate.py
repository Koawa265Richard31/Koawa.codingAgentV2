"""RT/J J2 gate: canary detection wired into the executor authorize path.

Advisory detection + escalation: a canary exact-hit on an ALLOW-verdict
action converts it to a durably suspended approval request (five-event
atomic batch).  Detector or store faults fall back to the base verdict
(fail-open, plan §3 J2); a *persisted* escalation is never weakenable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..control.event_store import EventStore
from .state import (
    ESCALATION_PENDING,
    SecurityStateStore,
    derive_canary_token,
    scan_exact_token,
)


@dataclass(frozen=True, slots=True)
class SecurityGate:
    """Exact session-canary gate consulted after first/final resolve."""

    event_store: EventStore
    key: bytes

    def token_for_turn(self, turn_id: Any) -> str:
        return derive_canary_token(self.key, turn_id)

    def hit(self, arguments_json: str, turn_id: Any) -> bool:
        token = self.token_for_turn(turn_id)
        return scan_exact_token(arguments_json, token)

    def sticky_status(self, execution_id) -> str | None:
        store = SecurityStateStore(self.event_store)
        status, _ = store.status(execution_id)
        return status

    def escalate(
        self,
        record,
        action,
        *,
        context,
        approval_service,
        turn_id: Any,
    ) -> dict:
        token = self.token_for_turn(turn_id)
        store = SecurityStateStore(self.event_store)
        execution_id = record.execution_id
        signal_payload = {
            "schema_version": 1,
            "signal_kind": "session_canary_exact",
            "execution_id": str(execution_id),
            "action_digest": action.action_digest,
            "policy_version": action.policy_version,
            "canary_id": token[:16],
            "base_decision": "allow",
            "effect": "escalate_to_ask",
            "detector_version": 1,
            "cause_code": "security_canary_exact",
        }
        escalated_payload = {
            "schema_version": 1,
            "execution_id": str(execution_id),
            "action_digest": action.action_digest,
            "policy_version": action.policy_version,
            "from_decision": "allow",
            "to_decision": "ask",
            "detector_version": 1,
            "reason_code": "security_canary_exact",
        }
        return approval_service.require_escalated_grant(
            record, action, context=context,
            signal_payload=signal_payload,
            escalated_payload=escalated_payload,
            prompt="Approve canary-flagged action?",
            security_head=store.head(execution_id),
        )

    def pending_status(self, execution_id) -> str | None:
        return self.sticky_status(execution_id)
