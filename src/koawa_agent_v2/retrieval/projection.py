"""Hardening 2026-09-19 (WP-D): durable metadata-only result projections.

For every test-result fact of a terminal run, publish one typed
``result.projection-published.v1`` event on the ``result-projection`` stream:
metadata only (profile, outcome, exit code, byte counts) plus a body_ref
pointing at the durable fact.  stdout/stderr bodies never enter the
projection - the model-visible receipt (verification/output_policy.py) is
gated separately.  Publication is idempotent per (turn, call, fact event);
derived indexes (WP-E) can be rebuilt from these events.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from ..control.event_store import (
    EventMetadata,
    NewEvent,
    StreamId,
    StreamWrite,
)
from ..verification.output_policy import BODY_VISIBILITY, POLICY_VERSION

PROJECTION_PUBLISHED_EVENT = "result.projection-published.v1"
PROJECTION_STREAM_CATEGORY = "result-projection"
TEST_SOURCE_KIND = "test"


class ResultProjectionStore:
    """Publish and read metadata-only result projections (WP-D)."""

    def __init__(self, event_store) -> None:
        self.event_store = event_store

    def stream(self, turn_id: UUID) -> StreamId:
        return StreamId(PROJECTION_STREAM_CATEGORY, turn_id)

    def _head_version(self, turn_id: UUID) -> int:
        page = self.event_store.read_stream(
            self.stream(turn_id), after_version=-1, limit=2
        )
        return page[-1].stream_version if page else -1

    def publish(
        self,
        *,
        turn_id: UUID,
        thread_id: UUID,
        run_id: UUID,
        call_id: str,
        source_kind: str,
        diagnostics: dict,
        body_ref: dict,
    ) -> str:
        """Append one projection event; idempotent per identity inputs."""
        identity = f"{turn_id}:{call_id}:{body_ref.get('event_id')}"
        command = uuid5(NAMESPACE_URL, f"result-projection:{identity}")
        payload = {
            "source_kind": source_kind,
            "visibility": BODY_VISIBILITY,
            "policy_version": POLICY_VERSION,
            "call_id": call_id,
            "diagnostics": diagnostics,
            "body_ref": body_ref,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        event = NewEvent(
            uuid5(command, "event"),
            PROJECTION_PUBLISHED_EVENT,
            1,
            datetime.now(timezone.utc),
            payload,
            EventMetadata(
                command,
                turn_id,
                thread_id=thread_id,
                turn_id=turn_id,
                run_id=run_id,
                actor="runtime",
            ),
        )
        head = self._head_version(turn_id)
        self.event_store.append_batch(
            (StreamWrite(self.stream(turn_id), head, (event,)),),
            idempotency_key=command,
            request_fingerprint=fingerprint,
        )
        return fingerprint

    def read(self, turn_id: UUID) -> list[dict]:
        page = self.event_store.read_stream(
            self.stream(turn_id), after_version=-1, limit=1000
        )
        return [dict(event.payload) for event in page]


def scan_test_results(store, turn_id: UUID) -> list[dict]:
    """Extract test-result facts from one turn's run-execution stream.

    Identification is by the receipt's own policy marker
    (``test_output_policy``), written by the output gate - no prepared-event
    dependency (production flows do not put prepared events on this stream).
    """
    facts = store.read_stream(
        StreamId("run-execution", turn_id), after_version=-1, limit=1000
    )
    results: list[dict] = []
    for event in facts:
        if event.event_type != "tool.result-recorded.v1":
            continue
        payload = dict(event.payload)
        item = payload.get("context_item")
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except (TypeError, ValueError):
                continue
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        try:
            receipt = json.loads(content) if isinstance(content, str) else None
        except (TypeError, ValueError):
            receipt = None
        if not isinstance(receipt, dict):
            continue
        if "test_output_policy" not in receipt:
            continue
        results.append(
            {
                "call_id": str(item.get("call_id")),
                "tool_name": "run_test_profile",
                "receipt": receipt,
                "is_error": bool(item.get("is_error")),
                "event_id": str(event.event_id),
                "event_version": event.stream_version,
            }
        )
    return results
