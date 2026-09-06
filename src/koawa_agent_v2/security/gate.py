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

    def hit_multi(
        self,
        arguments_json: str,
        turn_id: Any,
        ancestor_turn_ids: Any = (),
    ) -> tuple[str, Any] | None:
        """PSEC semantics-C (T7-b): exact scan against the own turn token plus
        any ancestor seed tokens (delegation chain).  Returns None when
        nothing matched, else ("own_turn", turn_id) or
        ("ancestor_turn", matched_ancestor_turn_id) — nearest ancestor wins.
        Tokens are derived per call from the trusted key; none of them are
        persisted or exposed (digest-only contract)."""
        token = self.token_for_turn(turn_id)
        if scan_exact_token(arguments_json, token):
            return ("own_turn", turn_id)
        for ancestor_turn_id in ancestor_turn_ids:
            ancestor_token = self.token_for_turn(ancestor_turn_id)
            if scan_exact_token(arguments_json, ancestor_token):
                return ("ancestor_turn", ancestor_turn_id)
        return None

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
        matched: tuple[str, Any] | None = None,
    ) -> dict:
        """Escalate an ALLOW verdict to a durably suspended approval.

        matched=None (default) is the J2 own-turn form and produces the exact
        legacy payload.  matched=("ancestor_turn", ancestor_turn_id) is the
        PSEC semantics-C form from gate.hit_multi: the payload gains
        seed_source/ancestor_turn_id and a distinct signal_kind, and the
        canary_id is derived from the ancestor token.
        """
        seed_source, matched_turn_id = (
            matched if matched is not None else ("own_turn", turn_id)
        )
        token = self.token_for_turn(matched_turn_id)
        store = SecurityStateStore(self.event_store)
        execution_id = record.execution_id
        signal_payload = {
            "schema_version": 1,
            "signal_kind": (
                "session_canary_exact"
                if seed_source == "own_turn"
                else "ancestor_seed_exact"
            ),
            "execution_id": str(execution_id),
            "action_digest": action.action_digest,
            "policy_version": action.policy_version,
            "canary_id": token[:16],
            "base_decision": "allow",
            "effect": "escalate_to_ask",
            "detector_version": 1,
            "cause_code": "security_canary_exact",
        }
        if seed_source != "own_turn":
            signal_payload["seed_source"] = seed_source
            signal_payload["ancestor_turn_id"] = str(matched_turn_id)
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
