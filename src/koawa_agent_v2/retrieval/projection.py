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
identity is (turn_id, model_turn_id, call_id, source digest) so the in-loop
publisher and the terminal catch-up dedupe against each other, and
publication is exposed as a callable the execution loop can invoke at the
recording point (after the durable fact, before the receipt reaches any
later model round).

Delivery gate (corrected plan B, spec v4 - implementation/
r2-delivery-gate-proposal.md): the model-facing receipt for a test call is
DERIVED FROM the published projection, never the execution-path object.
``decide_delivery`` writes the ``result.delivery-decided.v1`` fact with the
SPEC-1 identity (turn+model_turn+call only), the SPEC-2 three digests, and
the exact delivered string; the reducer serves that string and the fixed
``projection_unavailable`` placeholder, so an undecided or failed
publication can never leak the original receipt into any rebuilt context.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from ..control.durable_json import canonical_json_bytes_v1
from ..control.event_store import (
    EventMetadata,
    EventStoreError,
    NewEvent,
    StreamId,
    StreamWrite,
    WrongExpectedVersion,
)
from ..verification.output_policy import BODY_VISIBILITY, POLICY_VERSION
from ..verification.release_contracts import (
    ReleaseMode,
    effective_release_mode,
)

PROJECTION_PUBLISHED_EVENT = "result.projection-published.v1"
PROJECTION_UNAVAILABLE_EVENT = "result.projection-unavailable.v1"
PROJECTION_REVOKED_EVENT = "result.projection-revoked.v1"
PROJECTION_EXPIRED_EVENT = "result.projection-expired.v1"
DELIVERY_DECIDED_EVENT = "result.delivery-decided.v1"
PROJECTION_STREAM_CATEGORY = "result-projection"
TEST_SOURCE_KIND = "test"
TEST_TOOL_NAME = "run_test_profile"

DELIVERY_RECEIPT = "receipt"
DELIVERY_UNAVAILABLE = "projection_unavailable"
PLACEHOLDER_VERSION = 1
PLACEHOLDER_MESSAGE = (
    "本次调用的可读结果暂不可用。此状态不表示工具执行失败，也不表示未执行。"
    "不要仅因结果不可用重试原工具；请使用结果引用查询，或等待恢复处理。"
)

# Read-chain limit (closure review R2/WP-E: the quota bounds scan WORK, and
# a quota hit is reported as truncated - never a silent partial answer).
# Wire-level stream caps were considered and dropped: the event quota is the
# enforced scan-work bound (a dead constant would misstate the guarantee).
READ_SCAN_EVENT_QUOTA = 2000
# N1 search quotas: per-call scan work and response size are bounded
# separately; pagination continues from an opaque cursor bound to the query.
SEARCH_SCAN_EVENT_QUOTA = 2000
SEARCH_MAX_HITS = 20

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


# ---------------------------------------------------------------------------
# SPEC-2 canonicalization and the three digests (single implementation point
# shared by the live path and the reducer; never mix the three).
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_text(document: Any) -> str:
    """Canonical JSON text of a document (single wire rule for deliveries)."""

    return canonical_json_bytes_v1(document, path="delivery").decode("utf-8")


def source_digest(receipt: Any) -> str:
    """source digest - binds the SOURCE execution receipt (SPEC-2)."""

    return _sha256(canonical_json_bytes_v1(receipt, path="source-receipt"))


def delivery_content_digest(delivered_text: str) -> str:
    """delivery digest - SHA-256 over the delivered string's UTF-8 bytes
    (SPEC-2 补-3: the input is the exact string, never a re-encoded JSON)."""

    return _sha256(delivered_text.encode("utf-8"))


def projection_payload_digest(payload: Mapping) -> str:
    """immutable projection digest - binds one stored projection payload."""

    return _sha256(canonical_json_bytes_v1(dict(payload), path="projection"))


def test_diagnostics(receipt: Mapping[str, Any]) -> dict:
    """Metadata-only diagnostics copied from a test receipt."""

    return {
        key: receipt.get(key)
        for key in _DIAGNOSTIC_KEYS
        if receipt.get(key) is not None
    }


def unavailable_placeholder(
    *,
    turn_id,
    model_turn_id,
    call_id: str,
    source_sha256: str,
) -> dict:
    """The fixed projection_unavailable placeholder (spec v4, verbatim).

    Carries only the five reference fields and the fixed message - never
    logs, exception text or paths; ``is_error`` semantics live on the
    ToolResultMessage wrapper (False: unavailable != failure).
    """

    return {
        "availability": DELIVERY_UNAVAILABLE,
        "turn_id": str(turn_id),
        "model_turn_id": str(model_turn_id),
        "call_id": call_id,
        "source_content_sha256": source_sha256,
        "message": PLACEHOLDER_MESSAGE,
    }


def build_published_payload(
    *,
    source_kind: str,
    call_id: str,
    diagnostics: dict,
    body_ref: dict,
) -> dict:
    """The exact stored payload of a published projection event (the
    delivered receipt content is this document's canonical JSON text)."""

    return {
        "source_kind": source_kind,
        "visibility": BODY_VISIBILITY,
        "policy_version": POLICY_VERSION,
        "call_id": call_id,
        "publication_status": "published",
        "diagnostics": diagnostics,
        "body_ref": body_ref,
    }


@dataclass(frozen=True, slots=True)
class TestFactScan:
    """Trusted test facts of one turn plus the untrusted/unverified counts.

    ``facts`` carries only receipts bound to a trusted ledger execution
    record.  ``untrusted`` counts policy-marked receipts whose call identity
    has no matching ``run_test_profile`` ledger record - the marker alone
    never proves provenance (R2).  ``unverified`` counts receipts whose
    trust could NOT be checked because a ledger read failed: they are
    fail-closed (never published) but are reported separately, so a
    transient ledger outage is distinguishable from a source mismatch.
    """

    facts: tuple[dict, ...]
    untrusted: int
    unverified: int


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
        return _sha256(canonical_json_bytes_v1(payload, path="projection"))

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

    def revoke(
        self,
        *,
        turn_id: UUID,
        thread_id: UUID,
        run_id,
        call_id: str,
        model_turn_id,
        reason: str,
    ) -> str:
        """Withdraw one fact's projection (design event family member).

        After revocation the read chain refuses the fact (availability
        ``revoked``), the terminal catch-up never re-publishes it, and the
        publication status reports it durably.  Identity = the fact identity
        plus reason, so re-revoking with the same reason is idempotent while
        different reasons stay auditable.  ``reason`` is a safe code; free
        text never enters the event.
        """

        payload = {
            "visibility": BODY_VISIBILITY,
            "policy_version": POLICY_VERSION,
            "call_id": call_id,
            "model_turn_id": str(model_turn_id),
            "revocation": "revoked",
            "reason": reason,
        }
        identity = f"{turn_id}:{model_turn_id}:{call_id}:revoke:{reason}"
        return self._append_event(
            turn_id=turn_id,
            thread_id=thread_id,
            run_id=run_id,
            event_type=PROJECTION_REVOKED_EVENT,
            payload=payload,
            identity=identity,
        )

    def expire(
        self,
        *,
        turn_id: UUID,
        thread_id: UUID,
        run_id,
        call_id: str,
        model_turn_id,
        expires_at: datetime,
    ) -> str:
        """Schedule one fact's read expiry (design event family member).

        The projection stays readable until ``expires_at`` (store-authoritative
        comparison at read time), then the read chain refuses it with
        ``projection_expired`` - the same refusal path as revocation.
        """

        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise EventStoreError("expiry_deadline_must_be_aware")
        payload = {
            "visibility": BODY_VISIBILITY,
            "policy_version": POLICY_VERSION,
            "call_id": call_id,
            "model_turn_id": str(model_turn_id),
            "revocation": "expired",
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        }
        identity = f"{turn_id}:{model_turn_id}:{call_id}:expire"
        return self._append_event(
            turn_id=turn_id,
            thread_id=thread_id,
            run_id=run_id,
            event_type=PROJECTION_EXPIRED_EVENT,
            payload=payload,
            identity=identity,
        )

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

        payload = build_published_payload(
            source_kind=source_kind,
            call_id=call_id,
            diagnostics=diagnostics,
            body_ref=body_ref,
        )
        identity = (
            f"{turn_id}:{body_ref.get('model_turn_id')}:"
            f"{call_id}:{body_ref.get('source_content_sha256')}"
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
            f"{call_id}:{ref.get('source_content_sha256')}:failure:{error_code}"
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


def _plain_json(value):
    """Deep-convert frozen stored payloads (MappingProxyType/tuple) into
    plain JSON-compatible containers for model-facing serialization."""

    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def read_publication_status(store, turn_id: UUID) -> dict:
    """Rebuild one turn's publication status from durable events.

    Post-restart authority for projection visibility (R1 residual): fact
    identity is (model_turn_id, call_id, source digest); a fact is pending
    when it has no published projection - either registered unavailable or,
    after a crash between scan and publish, never attempted (``error_code``
    None).  ``untrusted``/``unverified`` count policy-marked receipts that
    failed the ledger identity binding (mismatch vs read failure).
    ``scan_error`` reports a failed run-execution scan; in that case pending
    can only contain registered failures, never a clean "all published".
    """

    published: set[tuple[str, str, str]] = set()
    failures: dict[tuple[str, str, str], dict] = {}
    revoked: set[str] = set()
    for event in _projection_events(store, turn_id):
        payload = dict(event.payload)
        if event.event_type in (PROJECTION_REVOKED_EVENT, PROJECTION_EXPIRED_EVENT):
            # Revocation is a terminal read verdict: the fact stops counting
            # as publishable-pending (and terminal catch-up skips it).
            revoked.add(
                str(payload.get("model_turn_id"))
                + ":"
                + str(payload.get("call_id"))
            )
            continue
        ref = payload.get("body_ref") or {}
        key = (
            str(ref.get("model_turn_id")),
            str(payload.get("call_id")),
            str(ref.get("source_content_sha256")),
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
                    str(fact["source_content_sha256"]),
                )
            )
    except Exception as exc:
        scan_error = type(exc).__name__
    pending = []
    for key in sorted(expected | set(failures)):
        if key in published:
            continue
        if f"{key[0]}:{key[1]}" in revoked:
            # Revoked/expired facts are terminal read verdicts, not pending
            # work: they are counted separately and never republished.
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
        "revoked": len(revoked),
        "scan_error": scan_error,
        "untrusted": untrusted,
        "unverified": unverified,
    }


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
                "source_content_sha256": source_digest(receipt),
                "is_error": bool(item.get("is_error")),
                "event_id": str(event.event_id),
                "stream_version": event.stream_version,
            }
        )
    return TestFactScan(
        facts=tuple(results), untrusted=untrusted, unverified=unverified,
    )


def build_test_fact_payload(fact) -> dict:
    """The exact published-payload document for one trusted test fact.

    Shared by publish_test_fact and the delivery decision so the delivered
    content and the stored projection are byte-equal by construction.
    """

    receipt = fact["receipt"]
    diagnostics = test_diagnostics(receipt)
    if not diagnostics and receipt.get("schema_version"):
        diagnostics = {"receipt": receipt}
    return build_published_payload(
        source_kind=TEST_SOURCE_KIND,
        call_id=fact["call_id"],
        diagnostics=diagnostics,
        body_ref={
            "stream": "run-execution",
            "turn_id": str(fact["turn_id"]),
            "model_turn_id": str(fact["model_turn_id"]),
            "source_content_sha256": fact["source_content_sha256"],
        },
    )


def publish_test_fact(store, *, turn_id, thread_id, run_id, fact) -> str:
    """Single publish path for one trusted test fact (R2/plan B).

    Both the in-loop publisher and the terminal catch-up MUST build the
    payload here: identical identity AND identical fingerprint, otherwise
    the second writer is rejected as a conflict (spec v4).  A §4-shaped
    receipt is embedded verbatim so the delivered projection carries the
    contract-filtered receipt.
    """

    payload = build_test_fact_payload(fact)
    body_ref = payload["body_ref"]
    return ResultProjectionStore(store).publish(
        turn_id=turn_id,
        thread_id=thread_id,
        run_id=run_id,
        call_id=fact["call_id"],
        source_kind=TEST_SOURCE_KIND,
        diagnostics=payload["diagnostics"],
        body_ref=body_ref,
    )


def lookup_projection(
    store,
    turn_id: UUID,
    call_id: str,
    model_turn_id: str | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """Read one call's published projection by reference (R2 slice b).

    Page-walks the projection stream and resolves the reference.  A full
    reference (call_id + model_turn_id) resolves exactly; a shorthand
    (call_id only) is accepted ONLY when it matches a single distinct
    (model_turn_id, source digest) reference - multiple matches return
    ``ambiguous_reference`` with the candidate list, never a guess.  On
    ``published`` the immutable projection reference (SPEC-2 补-1) is
    returned for delivery binding.

    Closure-review gap fills: revocation/expiry events (``revoked`` /
    ``projection_expired`` refusals - a revoked fact is never served again),
    the CURRENT policy check (a projection stamped with an older
    ``policy_version`` is refused as ``policy_superseded`` - historical
    delivery does not grant current read permission), and a bounded scan
    quota reported as ``truncated`` rather than silently cut.
    """

    per_reference: dict[tuple[str, str], dict] = {}
    latest_event = {}
    revoked: set[str] = set()
    expired_at: dict[str, datetime] = {}
    scanned = 0
    truncated = False
    for event in _projection_events(store, turn_id):
        scanned += 1
        if scanned > READ_SCAN_EVENT_QUOTA:
            truncated = True
            break
        payload = dict(event.payload)
        if event.event_type in (PROJECTION_REVOKED_EVENT, PROJECTION_EXPIRED_EVENT):
            key = str(payload.get("model_turn_id")), str(payload.get("call_id"))
            if event.event_type == PROJECTION_REVOKED_EVENT:
                revoked.add(key[0] + ":" + key[1])
            else:
                deadline = payload.get("expires_at")
                try:
                    expired_at[key[0] + ":" + key[1]] = datetime.fromisoformat(
                        deadline,
                    )
                except (TypeError, ValueError):
                    pass
            continue
        if str(payload.get("call_id")) != str(call_id):
            continue
        ref = payload.get("body_ref") or {}
        key = (
            str(ref.get("model_turn_id")),
            str(ref.get("source_content_sha256")),
        )
        bucket = per_reference.setdefault(
            key, {"published": None, "unavailable": None},
        )
        if event.event_type == PROJECTION_PUBLISHED_EVENT:
            bucket["published"] = payload
            latest_event[key] = event
        elif event.event_type == PROJECTION_UNAVAILABLE_EVENT:
            bucket["unavailable"] = payload

    read_now = now or datetime.now(timezone.utc)

    def _refused(identity_key) -> str | None:
        """Revocation/expiry verdict for one fact identity."""

        if f"{identity_key[0]}:{call_id}" in revoked:
            return "revoked"
        deadline = expired_at.get(f"{identity_key[0]}:{call_id}")
        if deadline is not None and read_now >= deadline:
            return "projection_expired"
        return None

    def _policy_ok(published_payload) -> bool:
        return published_payload.get("policy_version") == POLICY_VERSION

    if model_turn_id is not None:
        selected = {
            key: bucket
            for key, bucket in per_reference.items()
            if key[0] == str(model_turn_id)
        }
    else:
        selected = per_reference

    def _resolve(key, bucket):
        refusal = _refused(key)
        if refusal is not None:
            return {
                "availability": DELIVERY_UNAVAILABLE,
                "projection": None,
                "error_code": refusal,
                "projection_ref": None,
            }
        if bucket["published"] is not None:
            if not _policy_ok(bucket["published"]):
                # Historical delivery never grants current read permission.
                return {
                    "availability": DELIVERY_UNAVAILABLE,
                    "projection": None,
                    "error_code": "policy_superseded",
                    "projection_ref": None,
                }
            event = latest_event[key]
            projection_ref = {
                "stream": event.stream_id.category,
                "aggregate_id": str(event.stream_id.aggregate_id),
                "event_id": str(event.event_id),
                "stream_version": event.stream_version,
                "projection_sha256": projection_payload_digest(
                    bucket["published"]
                ),
            }
            return {
                "availability": "published",
                "projection": _plain_json(bucket["published"]),
                "error_code": None,
                "projection_ref": projection_ref,
            }
        if bucket["unavailable"] is not None:
            return {
                "availability": DELIVERY_UNAVAILABLE,
                "projection": None,
                "error_code": bucket["unavailable"].get("error_code"),
                "projection_ref": None,
            }
        return {
            "availability": "not_found",
            "projection": None,
            "error_code": None,
            "projection_ref": None,
        }

    result: dict
    if not selected:
        result = {
            "availability": "not_found",
            "projection": None,
            "error_code": None,
            "projection_ref": None,
        }
    elif len(selected) > 1:
        result = {
            "availability": "ambiguous_reference",
            "projection": None,
            "error_code": None,
            "matches": [
                {
                    "model_turn_id": key[0],
                    "source_content_sha256": key[1],
                    "availability": _resolve(key, bucket)["availability"],
                }
                for key, bucket in sorted(selected.items())
            ],
        }
    else:
        key, bucket = next(iter(selected.items()))
        result = _resolve(key, bucket)
        result["model_turn_id"] = key[0]
    result["scan_truncated"] = truncated
    return result


# ---------------------------------------------------------------------------
# Delivery decisions (corrected plan B).
# ---------------------------------------------------------------------------


def delivery_command_id(
    turn_id, model_turn_id, call_id: str,
) -> UUID:
    """SPEC-1: the delivery key NEVER carries a content dimension."""

    return uuid5(
        NAMESPACE_URL,
        "result-delivery:{}:{}:{}".format(turn_id, model_turn_id, call_id),
    )


def build_delivery_payload(
    *,
    call_id: str,
    model_turn_id,
    delivery: str,
    source_sha256: str,
    delivered_content: str,
    projection_ref: dict | None = None,
    error_code: str | None = None,
) -> dict:
    """Delivery payload = SPEC-1 fingerprint fields + SPEC-2 digests + the
    exact delivered string (SPEC-2 补-3 digest over its UTF-8 bytes)."""

    payload = {
        "call_id": call_id,
        "model_turn_id": str(model_turn_id),
        "delivery": delivery,
        "source_content_sha256": source_sha256,
        "delivery_content_sha256": delivery_content_digest(delivered_content),
        "delivered_content": delivered_content,
    }
    if delivery == DELIVERY_RECEIPT:
        payload["projection_ref"] = projection_ref
    else:
        payload["error_code"] = error_code
        payload["placeholder_version"] = PLACEHOLDER_VERSION
    return payload


def decide_delivery(
    store,
    *,
    turn_id: UUID,
    thread_id: UUID,
    run_id,
    call_id: str,
    model_turn_id,
    payload: dict,
    turn_fence: tuple[int, str, dict | None] | None = None,
) -> str:
    """Append one ``result.delivery-decided.v1`` on the run-execution stream.

    SPEC-1: idempotency key = (turn, model_turn, call) only; the payload
    (delivery value, digests, projection reference) IS the fingerprint, so a
    conflicting decision for the same call is rejected as
    ``IdempotencyConflict`` instead of becoming a second first decision.
    ``turn_fence`` = (expected turn-stream version, head event type,
    required head payload) guards recovery-side writes with an exact
    expected version (verify-before-write, SPEC-3 补-1).
    """

    command = delivery_command_id(turn_id, model_turn_id, call_id)
    if isinstance(run_id, str):
        # Backfill passes the fact segment's run id as text.
        run_id = UUID(run_id) if run_id else None
    # Segment validation requires every non-seed fact to carry its segment's
    # identity fields in the payload (same merge the recorder applies).
    full_payload = {
        "thread_id": str(thread_id),
        "turn_id": str(turn_id),
        "run_id": None if run_id is None else str(run_id),
        **dict(payload),
    }
    fingerprint = _sha256(
        canonical_json_bytes_v1(full_payload, path="delivery")
    )
    event = NewEvent(
        uuid5(command, "event"),
        DELIVERY_DECIDED_EVENT,
        1,
        datetime.now(timezone.utc),
        full_payload,
        EventMetadata(
            command,
            turn_id,
            thread_id=thread_id,
            turn_id=turn_id,
            run_id=run_id,
            actor="runtime",
        ),
    )
    from ..control.event_store import StreamPrecondition

    preconditions = ()
    if turn_fence is not None:
        preconditions = (
            StreamPrecondition(
                StreamId("turn", turn_id),
                turn_fence[0],
                turn_fence[1],
                turn_fence[2] or {},
            ),
        )
    stream = StreamId("run-execution", turn_id)
    for _attempt in range(_MAX_CAS_ATTEMPTS):
        cursor = -1
        while True:
            page = store.read_stream(stream, after_version=cursor, limit=_PAGE_SIZE)
            if not page:
                break
            cursor = page[-1].stream_version
            if len(page) < _PAGE_SIZE:
                break
        head = cursor
        try:
            store.append_batch(
                (StreamWrite(stream, head, (event,)),),
                idempotency_key=command,
                request_fingerprint=fingerprint,
                preconditions=preconditions,
            )
            return fingerprint
        except WrongExpectedVersion:
            continue
    raise EventStoreError(
        f"delivery append retry exhausted for turn {turn_id}"
    )


def make_test_receipt_publisher(store, ledger, runtime) -> Callable:
    """Build the in-loop publication + delivery hook (plan B).

    Invoked after a tool result's durable fact is committed.  For a bound
    ``run_test_profile`` receipt the hook publishes the projection and
    returns the DELIVERY DECISION for the loop to persist and serve:

    - ``{"delivery": "receipt", "content": <canonical projection JSON>}``
      derived from the exact published payload, or
    - ``{"delivery": "projection_unavailable", "content": <placeholder>}``
      when publication failed or the source cannot be proven (fail-closed).

    Non-test tools return ``None`` (the loop keeps the original message).
    Publication failures still register the durable unavailable marker
    (best-effort); only the caller's delivery-event WRITE failure pauses the
    run (see loop integration).
    """

    projection_store = ResultProjectionStore(store)

    def publisher(tool_name: str, result_message, execution_context) -> dict | None:
        turn_id = getattr(execution_context, "turn_id", None)
        call_id = None
        body_ref: dict | None = None
        try:
            if tool_name != TEST_TOOL_NAME or turn_id is None:
                return None
            try:
                receipt = json.loads(result_message.content)
            except (TypeError, ValueError, AttributeError):
                return None
            if not isinstance(receipt, dict):
                return None
            if receipt.get("test_output_policy") != POLICY_VERSION:
                return None
            call_id = result_message.call_ref.call_id
            source_sha256 = source_digest(receipt)
            body_ref = {
                "stream": "run-execution",
                "turn_id": str(turn_id),
                "model_turn_id": str(result_message.call_ref.model_turn_id),
                "source_content_sha256": source_sha256,
            }
            record = ledger.load_for_call(
                turn_id,
                result_message.call_ref.model_turn_id,
                call_id,
            )
            if record is None or record.tool_name != TEST_TOOL_NAME:
                # Unprovable source: fail-closed delivery, never the
                # execution-path receipt.
                placeholder = unavailable_placeholder(
                    turn_id=turn_id,
                    model_turn_id=result_message.call_ref.model_turn_id,
                    call_id=call_id,
                    source_sha256=source_sha256,
                )
                return {
                    "delivery": DELIVERY_UNAVAILABLE,
                    "content": canonical_text(placeholder),
                    "error_code": "source_unverified",
                    "source_content_sha256": source_sha256,
                    "model_turn_id": str(result_message.call_ref.model_turn_id),
                    "call_id": call_id,
                }
            fact = {
                "call_id": call_id,
                "model_turn_id": str(result_message.call_ref.model_turn_id),
                "turn_id": str(turn_id),
                "receipt": receipt,
                "source_content_sha256": body_ref["source_content_sha256"],
            }
            payload = build_test_fact_payload(fact)
            turn = runtime.get_turn(turn_id)
            try:
                publish_test_fact(
                    store,
                    turn_id=turn_id,
                    thread_id=turn.thread_id,
                    run_id=execution_context.run_id,
                    fact=fact,
                )
            except Exception as publish_error:
                try:
                    projection_store.register_publication_failure(
                        turn_id=turn_id,
                        thread_id=turn.thread_id,
                        run_id=execution_context.run_id,
                        call_id=call_id,
                        body_ref=body_ref,
                        error_code=type(publish_error).__name__,
                    )
                except Exception:
                    pass  # terminal catch-up stays authoritative
                placeholder = unavailable_placeholder(
                    turn_id=turn_id,
                    model_turn_id=result_message.call_ref.model_turn_id,
                    call_id=call_id,
                    source_sha256=source_sha256,
                )
                return {
                    "delivery": DELIVERY_UNAVAILABLE,
                    "content": canonical_text(placeholder),
                    "error_code": type(publish_error).__name__,
                    "source_content_sha256": source_sha256,
                    "model_turn_id": str(result_message.call_ref.model_turn_id),
                    "call_id": call_id,
                }
            # Delivery content = canonical JSON of the EXACT published
            # payload (constraint 1: derived from the published projection,
            # not the execution-path receipt).  Bind the immutable
            # projection reference (SPEC-2) by reading the just-published
            # event back; a read failure fails the delivery closed.
            try:
                looked = lookup_projection(
                    store,
                    turn_id,
                    call_id,
                    str(result_message.call_ref.model_turn_id),
                )
            except Exception:
                looked = None
            if looked is None or looked.get("availability") != "published":
                placeholder = unavailable_placeholder(
                    turn_id=turn_id,
                    model_turn_id=result_message.call_ref.model_turn_id,
                    call_id=call_id,
                    source_sha256=source_sha256,
                )
                return {
                    "delivery": DELIVERY_UNAVAILABLE,
                    "content": canonical_text(placeholder),
                    "error_code": "projection_read_failed",
                    "source_content_sha256": source_sha256,
                    "model_turn_id": str(result_message.call_ref.model_turn_id),
                    "call_id": call_id,
                }
            return {
                "delivery": DELIVERY_RECEIPT,
                "content": canonical_text(payload),
                "source_content_sha256": source_sha256,
                "model_turn_id": str(result_message.call_ref.model_turn_id),
                "call_id": call_id,
                "projection_ref": looked["projection_ref"],
            }
        except Exception as exc:
            # Hook-level failure: fail-closed delivery decision, still
            # never the original receipt and never a swallowed None.
            placeholder = unavailable_placeholder(
                turn_id=turn_id,
                model_turn_id=getattr(
                    getattr(result_message, "call_ref", None),
                    "model_turn_id",
                    None,
                ),
                call_id=call_id or "",
                source_sha256=(body_ref or {}).get(
                    "source_content_sha256", ""
                ),
            )
            return {
                "delivery": DELIVERY_UNAVAILABLE,
                "content": canonical_text(placeholder),
                "error_code": type(exc).__name__,
                "source_content_sha256": (body_ref or {}).get(
                    "source_content_sha256", ""
                ),
                "model_turn_id": str(
                    getattr(
                        getattr(result_message, "call_ref", None),
                        "model_turn_id",
                        "",
                    )
                ),
                "call_id": call_id or "",
            }

    return publisher


# ---------------------------------------------------------------------------
# N1: thread-scoped search over published projections (bounded-result WP-E).
# ---------------------------------------------------------------------------


def _thread_turn_ids(store, thread_id: UUID) -> list[UUID]:
    """Turn ids attached to one thread, in attach order (thread stream)."""

    from ..control.models import THREAD_TURN_ATTACHED

    ids: list[UUID] = []
    cursor = -1
    while True:
        page = store.read_stream(
            StreamId("thread", thread_id), after_version=cursor, limit=_PAGE_SIZE
        )
        if not page:
            return ids
        for event in page:
            if event.event_type == THREAD_TURN_ATTACHED:
                ids.append(UUID(str(event.payload.get("turn_id"))))
        cursor = page[-1].stream_version
        if len(page) < _PAGE_SIZE:
            return ids


def encode_search_cursor(turn_index: int, stream_version: int) -> str:
    """Opaque pagination cursor: base64 of its canonical JSON document."""

    import base64

    document = {"ti": turn_index, "sv": stream_version}
    return base64.urlsafe_b64encode(
        canonical_json_bytes_v1(document, path="search-cursor")
    ).decode("ascii")


def decode_search_cursor(cursor: str) -> dict:
    import base64

    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError):
        raise EventStoreError("invalid_search_cursor") from None
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("ti"), int)
        or not isinstance(document.get("sv"), int)
        or document["ti"] < 0
        or document["sv"] < 0
    ):
        raise EventStoreError("invalid_search_cursor")
    return document


def _revoked_identities(store, turn_id: UUID) -> set[str]:
    revoked: set[str] = set()
    cursor = -1
    while True:
        page = store.read_stream(
            StreamId(PROJECTION_STREAM_CATEGORY, turn_id),
            after_version=cursor,
            limit=_PAGE_SIZE,
        )
        if not page:
            return revoked
        for event in page:
            if event.event_type in (
                PROJECTION_REVOKED_EVENT,
                PROJECTION_EXPIRED_EVENT,
            ):
                payload = dict(event.payload)
                revoked.add(
                    str(payload.get("model_turn_id"))
                    + ":"
                    + str(payload.get("call_id"))
                )
        cursor = page[-1].stream_version
        if len(page) < _PAGE_SIZE:
            return revoked


def _hit_profile_id(payload: Mapping) -> str | None:
    """The profile identity of one projection payload (either generation)."""

    diagnostics = payload.get("diagnostics") or {}
    if isinstance(diagnostics, Mapping):
        receipt = diagnostics.get("receipt")
        if isinstance(receipt, Mapping):
            applicability = receipt.get("applicability")
            if isinstance(applicability, Mapping):
                value = applicability.get("profile_id")
                if value:
                    return str(value)
        value = diagnostics.get("profile_id")
        if value:
            return str(value)
    return None


def search_projections(
    store,
    *,
    thread_id: UUID,
    profile_id: str | None = None,
    cursor: str | None = None,
    contract_resolver=None,
) -> dict:
    """N1 (WP-E): search published projections scoped to the CURRENT thread.

    Per-profile retrieval semantics (ruling 2026-10-03): a hit's release
    granularity follows the profile's CURRENT contract via
    ``contract_resolver(profile_id)`` - detailed contracts receive the full
    projection (diagnostics included); limited/aggregate/local_only receive
    metadata only (identity, availability, completeness).  Revoked, expired
    and policy-superseded facts are refused like the read chain; they never
    appear as hits.

    Pagination: fixed stream_version order per turn, turns in attach order;
    the opaque cursor binds (turn index, version) so pages never mix or
    repeat versions.  Scan work and response size are separately quotaed and
    reported (``truncated``) - a short page is never a definitive "no more
    results".
    """

    start_ti, start_sv = 0, -1
    if cursor is not None:
        decoded = decode_search_cursor(cursor)
        start_ti, start_sv = decoded["ti"], decoded["sv"]

    turn_ids = _thread_turn_ids(store, thread_id)
    hits: list[dict] = []
    scanned = 0
    truncated = False
    watermark_ti = start_ti
    watermark_sv = start_sv
    for ti in range(start_ti, len(turn_ids)):
        turn_id = turn_ids[ti]
        min_version = start_sv if ti == start_ti else -1
        for event in _projection_events(store, turn_id):
            scanned += 1
            if scanned > SEARCH_SCAN_EVENT_QUOTA or len(hits) >= SEARCH_MAX_HITS:
                # Quota hit BEFORE advancing the watermark: the cursor stays
                # at the last PROCESSED event, so the next page re-reads this
                # event instead of skipping it.
                truncated = True
                break
            watermark_ti, watermark_sv = ti, event.stream_version
            if event.stream_version <= min_version:
                continue
            payload = dict(event.payload)
            if event.event_type != PROJECTION_PUBLISHED_EVENT:
                continue
            hit_call = str(payload.get("call_id"))
            ref = payload.get("body_ref") or {}
            model_turn = str(ref.get("model_turn_id"))
            if f"{model_turn}:{hit_call}" in _revoked_identities(store, turn_id):
                continue
            hit_profile = _hit_profile_id(payload)
            if profile_id is not None and hit_profile != profile_id:
                continue
            if payload.get("policy_version") != POLICY_VERSION:
                continue
            detailed = False
            if contract_resolver is not None and hit_profile:
                try:
                    contract = contract_resolver(hit_profile)
                except Exception:
                    contract = None
                detailed = bool(
                    contract is not None
                    and contract.release_mode is ReleaseMode.DETAILED
                    and effective_release_mode(
                        contract.release_mode, contract
                    )
                    is ReleaseMode.DETAILED
                )
            receipt_doc = (payload.get("diagnostics") or {}).get("receipt") or {}
            hit = {
                "turn_id": str(turn_id),
                "call_id": hit_call,
                "model_turn_id": model_turn,
                "source_content_sha256": str(ref.get("source_content_sha256")),
                "applicability": _plain_json(
                    receipt_doc.get("applicability", {})
                )
                or {"profile_id": hit_profile},
                "completeness": _plain_json(receipt_doc.get("completeness", {})),
                "granularity": "full" if detailed else "metadata",
            }
            if detailed:
                hit["projection"] = _plain_json(payload)
            hits.append(hit)
        if truncated:
            break
    result = {
        "hits": hits,
        "watermark": {
            "turn_index": watermark_ti,
            "stream_version": watermark_sv,
        },
        "truncated": truncated,
        "scanned_events": scanned,
    }
    if truncated:
        result["next_cursor"] = encode_search_cursor(watermark_ti, watermark_sv)
    else:
        result["next_cursor"] = None
    return result
