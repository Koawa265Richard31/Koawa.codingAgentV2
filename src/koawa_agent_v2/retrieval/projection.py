"""Hardening 2026-09-19 (WP-D): durable metadata-only result projections.

For every test-result fact of a terminal run, publish one typed
``result.projection-published.v1`` event on the ``result-projection`` stream:
metadata only (profile, outcome, exit code, byte counts) plus a body_ref
pointing at the durable fact.  stdout/stderr bodies never enter the
projection - the model-visible receipt (verification/output_policy.py) is
gated separately.  Publication is idempotent per (turn, call, fact digest);
derived indexes (WP-E) can be rebuilt from these events.

R1 (closure review 2026-09-25): the stream head is observed by walking
ascending pages to the end, concurrent head advances are retried instead of
aborting, and a fact whose publication still fails is registered durably as
an unavailable projection.  Publication failure never rewrites or
re-executes the completed tool result.

R2 (closure review 2026-09-25): scan and read page-walk the full streams
(no silent 1000-event caps), and test facts are trusted only when bound to
a ledger execution record for ``run_test_profile`` on the same turn - the
receipt's own policy marker is provenance decoration, not proof.  Fact
identity is (turn_id, call_id, content_sha256) so the in-loop publisher and
the terminal catch-up dedupe against each other, and publication is exposed
as a callable the execution loop can invoke at the recording point (after
the durable fact, before the receipt reaches any later model round).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from ..control.event_store import (
    EventMetadata,
    EventStoreError,
    NewEvent,
    StreamId,
    StreamWrite,
    WrongExpectedVersion,
)
from ..verification.output_policy import BODY_VISIBILITY, POLICY_VERSION

PROJECTION_PUBLISHED_EVENT = "result.projection-published.v1"
PROJECTION_UNAVAILABLE_EVENT = "result.projection-unavailable.v1"
PROJECTION_STREAM_CATEGORY = "result-projection"
TEST_SOURCE_KIND = "test"
TEST_TOOL_NAME = "run_test_profile"

# read_stream serves ascending pages from an exclusive cursor; walking to a
# short page is the protocol-level way to observe the true head (R2: scans
# and reads never cap the stream silently).
_PAGE_SIZE = 500
# Each CAS retry re-reads the head, so the loop only needs to outpace the
# concurrent writers on this one stream; exhausted retries surface as an
# error the caller must register, never as a silent skip.
_MAX_CAS_ATTEMPTS = 8

# Metadata-only diagnostics copied from a test receipt into the projection.
_DIAGNOSTIC_KEYS = (
    "exit_code",
    "outcome",
    "duration_ms",
    "stdout_bytes",
    "stderr_bytes",
    "stdout_truncated",
    "stderr_truncated",
)


@dataclass(frozen=True, slots=True)
class TestFactScan:
    """Trusted test facts of one turn plus the untrusted/unverified counts.

    ``facts`` carries only receipts bound to a trusted ledger execution
    record.  ``untrusted`` counts policy-marked receipts whose call
    identity has no matching ``run_test_profile`` ledger record - the
    marker alone never proves provenance (R2).  ``unverified`` counts
    receipts whose trust could NOT be checked because a ledger read failed:
    they are fail-closed (never published) but are reported separately, so
    a transient ledger outage is distinguishable from a source mismatch
    (re-verification 2026-09-25).
    """

    facts: tuple[dict, ...]
    untrusted: int
    unverified: int


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    """Canonical content digest of one receipt (identity + body_ref)."""

    return hashlib.sha256(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def test_diagnostics(receipt: Mapping[str, Any]) -> dict:
    return {
        key: receipt.get(key)
        for key in _DIAGNOSTIC_KEYS
        if receipt.get(key) is not None
    }


class ResultProjectionStore:
    """Publish and read metadata-only result projections (WP-D)."""

    def __init__(self, event_store) -> None:
        self.event_store = event_store

    def stream(self, turn_id: UUID) -> StreamId:
        return StreamId(PROJECTION_STREAM_CATEGORY, turn_id)

    def _head_version(self, turn_id: UUID) -> int:
        cursor = -1
        while True:
            page = self.event_store.read_stream(
                self.stream(turn_id), after_version=cursor, limit=_PAGE_SIZE
            )
            if not page:
                return cursor
            cursor = page[-1].stream_version
            if len(page) < _PAGE_SIZE:
                return cursor

    def _append_guarded(
        self,
        *,
        turn_id: UUID,
        thread_id: UUID,
        run_id,
        event: NewEvent,
        command: UUID,
        fingerprint: str,
    ) -> None:
        """Append one projection event at the current head, retrying CAS.

        ``IdempotencyConflict`` is NOT retried: the same identity with
        different content is a rejected command, and the store already turns
        same-identity retries into the original receipt before the version
        check, so a concurrent duplicate publish resolves as success.
        """

        for _attempt in range(_MAX_CAS_ATTEMPTS):
            head = self._head_version(turn_id)
            try:
                self.event_store.append_batch(
                    (StreamWrite(self.stream(turn_id), head, (event,)),),
                    idempotency_key=command,
                    request_fingerprint=fingerprint,
                )
                return
            except WrongExpectedVersion:
                continue
        raise EventStoreError(
            f"projection append retry exhausted for {self.stream(turn_id)}"
        )

    @staticmethod
    def _fingerprint(payload: dict) -> str:
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _append_event(
        self,
        *,
        turn_id: UUID,
        thread_id: UUID,
        run_id,
        event_type: str,
        payload: dict,
        identity: str,
    ) -> str:
        command = uuid5(NAMESPACE_URL, f"result-projection:{identity}")
        fingerprint = self._fingerprint(payload)
        event = NewEvent(
            uuid5(command, "event"),
            event_type,
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
        self._append_guarded(
            turn_id=turn_id,
            thread_id=thread_id,
            run_id=run_id,
            event=event,
            command=command,
            fingerprint=fingerprint,
        )
        return fingerprint

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
        """Append one projection event; idempotent per identity inputs.

        Identity is the FULL call identity plus the content digest
        (re-verification 2026-09-25): ``call_id`` is only unique within its
        model turn, so ``model_turn_id`` rides the identity via body_ref -
        two rounds reusing a call_id with equal receipts are two facts.
        """

        payload = {
            "source_kind": source_kind,
            "visibility": BODY_VISIBILITY,
            "policy_version": POLICY_VERSION,
            "call_id": call_id,
            "publication_status": "published",
            "diagnostics": diagnostics,
            "body_ref": body_ref,
        }
        identity = (
            f"{turn_id}:{body_ref.get('model_turn_id')}:"
            f"{call_id}:{body_ref.get('content_sha256')}"
        )
        return self._append_event(
            turn_id=turn_id,
            thread_id=thread_id,
            run_id=run_id,
            event_type=PROJECTION_PUBLISHED_EVENT,
            payload=payload,
            identity=identity,
        )

    def register_publication_failure(
        self,
        *,
        turn_id: UUID,
        thread_id: UUID,
        run_id,
        call_id: str | None,
        body_ref: dict | None,
        error_code: str,
    ) -> str:
        """Durable pending marker for a fact whose publication failed.

        ``error_code`` must be a safe class/code name; raw exception text
        (paths, store payloads) never enters the event.  Identity mirrors
        ``publish`` (turn + model turn + call + digest) plus the error code,
        so the same failure retried is idempotent while a different failure
        for the same fact is recorded separately.
        """

        ref = body_ref or {}
        identity = (
            f"{turn_id}:{ref.get('model_turn_id')}:"
            f"{call_id}:{ref.get('content_sha256')}:failure:{error_code}"
        )
        payload = {
            "visibility": BODY_VISIBILITY,
            "policy_version": POLICY_VERSION,
            "call_id": call_id,
            "publication_status": "failed",
            "error_code": error_code,
            "body_ref": body_ref,
        }
        return self._append_event(
            turn_id=turn_id,
            thread_id=thread_id,
            run_id=run_id,
            event_type=PROJECTION_UNAVAILABLE_EVENT,
            payload=payload,
            identity=identity,
        )

    def read(self, turn_id: UUID) -> list[dict]:
        """All projection payloads of one turn, page-walked in order (R2)."""

        return [dict(event.payload) for event in _projection_events(
            self.event_store, turn_id
        )]


def _projection_events(store, turn_id: UUID):
    """Yield every projection-stream event in version order (page-walked)."""

    cursor = -1
    while True:
        page = store.read_stream(
            StreamId(PROJECTION_STREAM_CATEGORY, turn_id),
            after_version=cursor,
            limit=_PAGE_SIZE,
        )
        if not page:
            return
        yield from page
        cursor = page[-1].stream_version
        if len(page) < _PAGE_SIZE:
            return


def read_publication_status(store, turn_id: UUID) -> dict:
    """Rebuild one turn's publication status from durable events.

    Post-restart authority for projection visibility (R1 residual): fact
    identity is (call_id, body_ref.content_sha256); a fact is pending when
    it has no published projection - either registered unavailable or,
    after a crash between scan and publish, never attempted (``error_code``
    None).  ``untrusted`` counts policy-marked receipts that failed the
    ledger identity binding and are therefore not publishable facts.
    ``scan_error`` reports a failed run-execution scan; in that case pending
    can only contain registered failures, never a clean "all published".
    """

    published: set[tuple[str, str, str]] = set()
    failures: dict[tuple[str, str, str], dict] = {}
    for event in _projection_events(store, turn_id):
        payload = dict(event.payload)
        ref = payload.get("body_ref") or {}
        key = (
            str(ref.get("model_turn_id")),
            str(payload.get("call_id")),
            str(ref.get("content_sha256")),
        )
        if event.event_type == PROJECTION_PUBLISHED_EVENT:
            published.add(key)
        elif event.event_type == PROJECTION_UNAVAILABLE_EVENT:
            failures[key] = {
                "model_turn_id": ref.get("model_turn_id"),
                "call_id": payload.get("call_id"),
                "error_code": payload.get("error_code"),
            }
    scan_error = None
    untrusted = 0
    unverified = 0
    expected: set[tuple[str, str, str]] = set()
    try:
        scan = scan_test_results(store, turn_id)
        untrusted = scan.untrusted
        unverified = scan.unverified
        for fact in scan.facts:
            expected.add(
                (
                    str(fact["model_turn_id"]),
                    str(fact["call_id"]),
                    str(fact["content_sha256"]),
                )
            )
    except Exception as exc:
        scan_error = type(exc).__name__
    pending = []
    for key in sorted(expected | set(failures)):
        if key in published:
            continue
        info = failures.get(key)
        pending.append(
            {
                "model_turn_id": key[0],
                "call_id": key[1],
                "error_code": None if info is None else info.get("error_code"),
            }
        )
    return {
        "published": len(published),
        "failed": len(pending),
        "pending": pending,
        "scan_error": scan_error,
        "untrusted": untrusted,
        "unverified": unverified,
    }


def _plain_json(value):
    """Deep-convert frozen stored payloads (MappingProxyType/tuple) into
    plain JSON-compatible containers for model-facing serialization."""

    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def lookup_projection(
    store,
    turn_id: UUID,
    call_id: str,
    model_turn_id: str | None = None,
) -> dict:
    """Read one call's published projection by reference (R2 slice b).

    Page-walks the projection stream and returns the LATEST event matching
    the reference.  A full reference (call_id + model_turn_id) resolves
    exactly; a shorthand (call_id only) is accepted ONLY when it matches a
    single distinct (model_turn_id, digest) reference - multiple matches
    return ``ambiguous_reference`` with the candidate list, never a guess
    (re-verification round 3: an old published round must not be presented
    as the result a newer failed round refers to).  Availability is
    explicit: ``published`` carries the stored metadata-only payload;
    ``projection_unavailable`` reports the durable failure marker's error
    code; ``not_found`` means no event exists.  Raw run-execution bodies
    are never returned.
    """

    per_reference: dict[tuple[str, str], dict[str, dict]] = {}
    for event in _projection_events(store, turn_id):
        payload = dict(event.payload)
        if str(payload.get("call_id")) != str(call_id):
            continue
        ref = payload.get("body_ref") or {}
        key = (
            str(ref.get("model_turn_id")),
            str(ref.get("content_sha256")),
        )
        bucket = per_reference.setdefault(
            key, {"published": None, "unavailable": None},
        )
        if event.event_type == PROJECTION_PUBLISHED_EVENT:
            bucket["published"] = payload
        elif event.event_type == PROJECTION_UNAVAILABLE_EVENT:
            bucket["unavailable"] = payload

    if model_turn_id is not None:
        selected = {
            key: bucket
            for key, bucket in per_reference.items()
            if key[0] == str(model_turn_id)
        }
    else:
        selected = per_reference

    def _resolve(bucket):
        if bucket["published"] is not None:
            return {
                "availability": "published",
                "projection": _plain_json(bucket["published"]),
                "error_code": None,
            }
        if bucket["unavailable"] is not None:
            return {
                "availability": "projection_unavailable",
                "projection": None,
                "error_code": bucket["unavailable"].get("error_code"),
            }
        return {
            "availability": "not_found",
            "projection": None,
            "error_code": None,
        }

    if not selected:
        return {
            "availability": "not_found",
            "projection": None,
            "error_code": None,
        }
    if len(selected) > 1:
        return {
            "availability": "ambiguous_reference",
            "projection": None,
            "error_code": None,
            "matches": [
                {
                    "model_turn_id": key[0],
                    "content_sha256": key[1],
                    "availability": _resolve(bucket)["availability"],
                }
                for key, bucket in sorted(selected.items())
            ],
        }
    key, bucket = next(iter(selected.items()))
    resolved = _resolve(bucket)
    resolved["model_turn_id"] = key[0]
    return resolved


def _iter_stream(store, stream: StreamId):
    cursor = -1
    while True:
        page = store.read_stream(stream, after_version=cursor, limit=_PAGE_SIZE)
        if not page:
            return
        yield from page
        cursor = page[-1].stream_version
        if len(page) < _PAGE_SIZE:
            return


def scan_test_results(store, turn_id: UUID) -> TestFactScan:
    """Extract trusted test-result facts from one turn's run-execution stream.

    A receipt qualifies only when BOTH hold (R2):
    - its own policy marker (``test_output_policy``) matches the current
      contract version, and
    - the same (turn, model turn, call) identity has a trusted ledger
      execution record naming ``run_test_profile`` - the marker is written
      by the output gate, but the LEDGER binds the caller identity.
    Page-walked: no silent truncation on long streams.
    """

    from ..ledger import ToolLedgerStore

    ledger = ToolLedgerStore(store)
    untrusted = 0
    unverified = 0
    results: list[dict] = []
    for event in _iter_stream(store, StreamId("run-execution", turn_id)):
        if event.event_type != "tool.result-recorded.v1":
            continue
        payload = dict(event.payload)
        item = payload.get("context_item")
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except (TypeError, ValueError):
                continue
        # Stored payloads are frozen as read-only mappings, so the check
        # must accept any Mapping - isinstance(item, dict) alone would skip
        # every production-recorded fact (closure review R1).
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        try:
            receipt = json.loads(content) if isinstance(content, str) else None
        except (TypeError, ValueError):
            receipt = None
        if not isinstance(receipt, dict):
            continue
        if receipt.get("test_output_policy") != POLICY_VERSION:
            continue
        call_id = str(item.get("call_id"))
        try:
            model_turn_id = UUID(str(item.get("model_turn_id")))
        except ValueError:
            model_turn_id = None
        record = None
        verify_failed = False
        if model_turn_id is not None:
            try:
                record = ledger.load_for_call(
                    turn_id, model_turn_id, call_id,
                )
            except Exception:
                # Fail-closed, but reported separately from a source
                # mismatch: the caller cannot tell whether the fact is
                # publishable until the ledger is readable again.
                verify_failed = True
        if verify_failed:
            unverified += 1
            continue
        if record is None or record.tool_name != TEST_TOOL_NAME:
            # A policy marker without the bound execution identity is not a
            # provable test source; it is never published as one.
            untrusted += 1
            continue
        results.append(
            {
                "call_id": call_id,
                "model_turn_id": str(model_turn_id),
                "tool_name": TEST_TOOL_NAME,
                "receipt": receipt,
                "content_sha256": receipt_digest(receipt),
                "is_error": bool(item.get("is_error")),
                "event_id": str(event.event_id),
                "event_version": event.stream_version,
            }
        )
    return TestFactScan(
        facts=tuple(results), untrusted=untrusted, unverified=unverified,
    )


def make_test_receipt_publisher(store, ledger, runtime) -> Callable:
    """Build the in-loop publication hook (R2 ordering).

    The loop invokes the hook after a tool result's durable fact is
    committed and BEFORE the receipt is appended to any later model-round
    context, so the published projection precedes every subsequent read.
    The hook never raises and never re-executes tools: a publication failure
    registers the durable unavailable marker, and the terminal catch-up
    (``AppRuntime._publish_result_projections``) stays authoritative for
    recovery.
    """

    projection_store = ResultProjectionStore(store)

    def publisher(tool_name: str, result_message, execution_context) -> None:
        turn_id = getattr(execution_context, "turn_id", None)
        call_id = None
        body_ref: dict | None = None
        try:
            if tool_name != TEST_TOOL_NAME or turn_id is None:
                return
            try:
                receipt = json.loads(result_message.content)
            except (TypeError, ValueError, AttributeError):
                return
            if not isinstance(receipt, dict):
                return
            if receipt.get("test_output_policy") != POLICY_VERSION:
                return
            call_id = result_message.call_ref.call_id
            # Digest and body_ref are computed before anything can fail so a
            # failure marker carries the SAME identity the terminal
            # catch-up would register for this fact.  model_turn_id is part
            # of the identity: call_id is only unique within its turn.
            body_ref = {
                "stream": "run-execution",
                "turn_id": str(turn_id),
                "model_turn_id": str(result_message.call_ref.model_turn_id),
                "content_sha256": receipt_digest(receipt),
            }
            record = ledger.load_for_call(
                turn_id,
                result_message.call_ref.model_turn_id,
                call_id,
            )
            if record is None or record.tool_name != TEST_TOOL_NAME:
                return
            turn = runtime.get_turn(turn_id)
            projection_store.publish(
                turn_id=turn_id,
                thread_id=turn.thread_id,
                run_id=execution_context.run_id,
                call_id=call_id,
                source_kind=TEST_SOURCE_KIND,
                diagnostics=test_diagnostics(receipt),
                body_ref=body_ref,
            )
        except Exception as exc:
            try:
                projection_store.register_publication_failure(
                    turn_id=turn_id,
                    thread_id=(
                        runtime.get_turn(turn_id).thread_id
                        if turn_id is not None else None
                    ),
                    run_id=getattr(execution_context, "run_id", None),
                    call_id=call_id,
                    body_ref=body_ref,
                    error_code=type(exc).__name__,
                )
            except Exception:
                # Registration itself failed (e.g. store outage): the
                # terminal catch-up re-runs the whole scan for this turn.
                return

    return publisher
