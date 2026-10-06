"""Plan B recovery: delivery-decision backfill (spec v4, SPEC-3).

A committed test fact without a delivery decision is ``delivery_pending`` -
a legitimate crash intermediate, NOT corruption.  Backfill runs inside the
formal recovery entry (resume), BEFORE any context is reduced or rebuilt:

1. verify-before-write: protocol gate (legacy test facts refuse recovery),
   then per pending call a projection lookup by the FULL call identity plus
   source digest (never "latest"; multiple inconsistent candidates are an
   integrity failure, never a guess);
2. confirmed-absent  -> persist ``projection_unavailable / not_published``;
   lookup failure    -> ``delivery_paused`` (no "not published" judgment is
   persisted for a transient outage);
   already decided   -> verify and replay the ORIGINAL decision, never
   recompute from the current projection (SPEC-1 replay constraint).

Every write rides the SPEC-1 identity (turn+model_turn+call) with an exact
turn-head fence; tools are never re-executed and the original receipt never
reaches a rebuilt model context (the reducer serves the placeholder until
the decision lands).
"""
from __future__ import annotations

from typing import Mapping, Sequence
from uuid import UUID


class DeliveryRecoveryError(Exception):
    """Base for the three explicitly separated delivery recovery states."""

    code = "delivery_error"
    reason = ""

    def __init__(self, reason: str = "", detail: Mapping | None = None) -> None:
        self.reason = reason or self.code
        self.detail = dict(detail or {})
        super().__init__(self.reason)


class DeliveryRecoveryPaused(DeliveryRecoveryError):
    """delivery_paused - backfill could not verify; nothing persisted."""

    def __init__(self, reason: str, detail: Mapping | None = None) -> None:
        self.code = "delivery_paused"
        super().__init__(reason, detail)


class DeliveryProtocolMismatch(DeliveryRecoveryError):
    """protocol_version_mismatch - facts predate the delivery protocol."""

    def __init__(self, detail: Mapping | None = None) -> None:
        self.code = "protocol_version_mismatch"
        super().__init__(self.code, detail)


class DeliveryLogCorruption(DeliveryRecoveryError):
    """log_corruption - durable conflicting decisions / broken identity."""

    def __init__(self, reason: str, detail: Mapping | None = None) -> None:
        self.code = "log_corruption"
        super().__init__(reason, detail)


def backfill_delivery_decisions(
    store,
    *,
    turn_id: UUID,
    thread_id: UUID,
    run_id,
    events: Sequence,
    turn_fence: tuple[int, str, Mapping | None] | None = None,
) -> dict:
    """Backfill missing first-delivery decisions for one turn's facts."""

    from ..control.event_store import IdempotencyConflict
    from ..retrieval.projection import (
        DELIVERY_RECEIPT,
        DELIVERY_UNAVAILABLE,
        build_delivery_payload,
        canonical_text,
        decide_delivery,
        lookup_projection,
        unavailable_placeholder,
    )
    from .context import pending_delivery_calls

    state = pending_delivery_calls(events)
    if state["legacy_test_facts"]:
        raise DeliveryProtocolMismatch(
            {"turn_id": str(turn_id),
             "legacy_test_facts": state["legacy_test_facts"]},
        )
    backfilled = 0
    replayed = 0
    for model_turn_id, call_id, source_sha256, fact_run_id in state["pending"]:
        try:
            looked = lookup_projection(
                store, turn_id, call_id, model_turn_id,
            )
        except Exception as exc:
            raise DeliveryRecoveryPaused(
                "projection_lookup_failed",
                {"call_id": call_id, "error": type(exc).__name__},
            ) from None
        if looked.get("availability") == "ambiguous_reference":
            # Multiple candidates for one call identity = integrity failure;
            # never pick on the caller's behalf.
            raise DeliveryLogCorruption(
                "ambiguous_delivery_reference",
                {"call_id": call_id, "matches": looked.get("matches")},
            )
        if looked.get("availability") == "published":
            payload = build_delivery_payload(
                call_id=call_id,
                model_turn_id=model_turn_id,
                delivery=DELIVERY_RECEIPT,
                source_sha256=source_sha256,
                delivered_content=canonical_text(looked["projection"]),
                projection_ref=looked.get("projection_ref"),
            )
        else:
            placeholder = unavailable_placeholder(
                turn_id=turn_id,
                model_turn_id=model_turn_id,
                call_id=call_id,
                source_sha256=source_sha256,
            )
            error_code = looked.get("error_code") or "not_published"
            if looked.get("scan_truncated"):
                # Quota hit during backfill: paused beats a wrong verdict.
                raise DeliveryRecoveryPaused(
                    "projection_scan_quota_exceeded",
                    {"call_id": call_id},
                )
            payload = build_delivery_payload(
                call_id=call_id,
                model_turn_id=model_turn_id,
                delivery=DELIVERY_UNAVAILABLE,
                source_sha256=source_sha256,
                delivered_content=canonical_text(placeholder),
                error_code=error_code,
            )
        try:
            decide_delivery(
                store,
                turn_id=turn_id,
                thread_id=thread_id,
                run_id=fact_run_id or run_id,
                call_id=call_id,
                model_turn_id=model_turn_id,
                payload=payload,
                turn_fence=turn_fence,
            )
            backfilled += 1
        except IdempotencyConflict:
            # A concurrent recovery committed a decision first: replay the
            # ORIGINAL unless it binds a different source digest (corruption
            # per SPEC-1 acceptance: divergent content rejected).
            existing = _existing_decision(
                store, turn_id, model_turn_id, call_id,
            )
            if existing is None:
                raise DeliveryRecoveryPaused(
                    "delivery_decision_lost",
                    {"call_id": call_id},
                ) from None
            if existing.get("source_content_sha256") != source_sha256:
                raise DeliveryLogCorruption(
                    "delivery_source_digest_conflict",
                    {"call_id": call_id},
                )
            replayed += 1
    return {
        "backfilled": backfilled,
        "replayed": replayed,
        "pending": len(state["pending"]),
    }


def _existing_decision(
    store, turn_id: UUID, model_turn_id: str, call_id: str,
) -> dict | None:
    from .context import _DELIVERY_DECIDED_EVENT

    from ..control.event_store import StreamId

    stream = StreamId("run-execution", turn_id)
    cursor = -1
    while True:
        page = store.read_stream(stream, after_version=cursor, limit=500)
        if not page:
            return None
        for event in page:
            if event.event_type != _DELIVERY_DECIDED_EVENT:
                continue
            payload = dict(event.payload)
            if (
                str(payload.get("model_turn_id")) == str(model_turn_id)
                and str(payload.get("call_id")) == str(call_id)
            ):
                return payload
        cursor = page[-1].stream_version
        if len(page) < 500:
            return None
