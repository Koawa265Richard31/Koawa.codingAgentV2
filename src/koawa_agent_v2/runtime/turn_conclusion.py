"""D23-A: authoritative per-turn conclusion event and source-head verifier.

A TurnConclusion is a bounded, authoritative summary of one completed Turn,
rebuilt only from durable facts (I7 RuntimeTruth, D6 execution facts, tool
ledger, workspace effect index, approval, verification evidence) - never from
final text alone.  It is persisted on a dedicated ``turn-memory-{turn_id}``
stream (``memory.turn-conclusion-recorded.v1``); the terminal Turn stream is
never appended to.  Same ``turn_id + source_heads_digest`` maps to a
deterministic command identity; a changed source head forces a new revision
and the old revision is marked stale (never injected).

This module implements D23-A only: the DTO, the authoritative rebuild, the
durable event and the source verifier.  Session projection (failed-turn echo,
conclusion block, dedup, MemoryEnvelope) is D23-B.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from ..control.durable_json import canonical_json_bytes_v1
from ..control.event_store import (
    AppendReceipt,
    EventMetadata,
    EventStoreError,
    NewEvent,
    StreamId,
    StreamPrecondition,
    StreamWrite,
    WrongExpectedVersion,
)
from ..control.models import CompletionEvidenceRef, RunState, TurnState
from ..control.run_effects import RunEffectIndex, RunEffectInspection
from ..recovery.redaction import redact_text

MEMORY_STREAM_CATEGORY = "turn-memory"
CONCLUSION_EVENT_TYPE = "memory.turn-conclusion-recorded.v1"
CONCLUSION_SCHEMA_VERSION = 1
REQUEST_SUMMARY_MAX_CHARS = 512
MAX_ERROR_CODES = 16
MAX_TOOLS = 64
MAX_FILES = 64
MAX_OBLIGATIONS = 32
MAX_UNCERTAINTIES = 16


class TurnConclusionError(RuntimeError):
    """Stable, content-free conclusion failure; carries a code only."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Exact reference to one piece of test/completion evidence."""

    stream_id: StreamId
    stream_version: int
    event_id: UUID
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class TurnConclusion:
    """Bounded authoritative conclusion of one terminal Turn (D23 §4.1)."""

    thread_id: UUID
    turn_id: UUID
    run_id: UUID | None
    turn_status: str
    run_status: str | None
    request_summary: str
    error_codes: tuple[str, ...]
    successful_tools: tuple[str, ...]
    changed_files: tuple[str, ...]
    test_evidence_refs: tuple[EvidenceRef, ...]
    open_obligations: tuple[str, ...]
    uncertainty_codes: tuple[str, ...]
    authoritative_digest: str
    untrusted_summary: str | None
    source_heads_digest: str

    def document(self) -> dict[str, Any]:
        """Canonical bounded payload (never raw args/results/reasoning)."""
        return {
            "thread_id": str(self.thread_id),
            "turn_id": str(self.turn_id),
            "run_id": str(self.run_id) if self.run_id is not None else None,
            "turn_status": self.turn_status,
            "run_status": self.run_status,
            "request_summary": self.request_summary,
            "error_codes": list(self.error_codes),
            "successful_tools": list(self.successful_tools),
            "changed_files": list(self.changed_files),
            "test_evidence_refs": [
                {
                    "stream_category": ref.stream_id.category,
                    "stream_id": str(ref.stream_id.aggregate_id),
                    "stream_version": ref.stream_version,
                    "event_id": str(ref.event_id),
                    "evidence_digest": ref.evidence_digest,
                }
                for ref in self.test_evidence_refs
            ],
            "open_obligations": list(self.open_obligations),
            "uncertainty_codes": list(self.uncertainty_codes),
            "authoritative_digest": self.authoritative_digest,
            "untrusted_summary": self.untrusted_summary,
            "source_heads_digest": self.source_heads_digest,
        }

    @classmethod
    def from_document(cls, payload: Mapping[str, Any]) -> "TurnConclusion":
        try:
            refs = tuple(
                EvidenceRef(
                    StreamId(
                        ref["stream_category"], UUID(ref["stream_id"])
                    ),
                    ref["stream_version"],
                    UUID(ref["event_id"]),
                    ref["evidence_digest"],
                )
                for ref in payload["test_evidence_refs"]
            )
            run_id = UUID(payload["run_id"]) if payload.get("run_id") else None
            return cls(
                thread_id=UUID(payload["thread_id"]),
                turn_id=UUID(payload["turn_id"]),
                run_id=run_id,
                turn_status=payload["turn_status"],
                run_status=payload.get("run_status"),
                request_summary=payload["request_summary"],
                error_codes=tuple(payload["error_codes"]),
                successful_tools=tuple(payload["successful_tools"]),
                changed_files=tuple(payload["changed_files"]),
                test_evidence_refs=refs,
                open_obligations=tuple(payload["open_obligations"]),
                uncertainty_codes=tuple(payload["uncertainty_codes"]),
                authoritative_digest=payload["authoritative_digest"],
                untrusted_summary=payload.get("untrusted_summary"),
                source_heads_digest=payload["source_heads_digest"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TurnConclusionError("turn_conclusion_payload_invalid") from None


def _digest(value: Any) -> str:
    return hashlib.sha256(
        canonical_json_bytes_v1(value, path="conclusion")
    ).hexdigest()


def _bounded(values: Sequence[str], limit: int, name: str) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise TurnConclusionError(f"{name}_invalid")
        if value in seen:
            continue
        seen.append(value)
    if len(seen) > limit:
        raise TurnConclusionError(f"{name}_limit")
    return tuple(seen)


def _tool_error_codes(turn: TurnState, run: RunState | None) -> tuple[str, ...]:
    """Stable error codes from the Turn error text and failed-run detail.

    Worker failures persist ``d2:<code>``; operator terminations persist plain
    reason text.  A successful run's detail is its summary, never an error
    code.  Only codes matching the stable code shape are kept.
    """
    candidates = [turn.error]
    if run is not None and run.status.value != "completed":
        candidates.append(run.detail)
    codes: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        for part in candidate.split(","):
            code = part.strip()
            if code.startswith("d2:"):
                code = code[3:]
            if not code or not code.replace("_", "").replace("-", "").isalnum():
                continue
            codes.append(code)
    return _bounded(codes, MAX_ERROR_CODES, "error_codes")


def _changed_files_from_result(content: str) -> tuple[str, ...]:
    """D22 F4: changed files come from apply_patch success results.

    Only the JSON keys changed_paths/changes[].path are authoritative; the
    content has already been redacted at the ledger boundary.
    """
    try:
        parsed = json.loads(content)
    except Exception:
        return ()
    if not isinstance(parsed, dict):
        return ()
    files: set[str] = set()
    for key in ("changed_paths",):
        value = parsed.get(key)
        if isinstance(value, list):
            for path in value:
                if isinstance(path, str) and path:
                    files.add(path)
    changes = parsed.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            path = change.get("path")
            if isinstance(path, str) and path:
                files.add(path)
    return tuple(sorted(files))


class TurnConclusionStore:
    """Build, persist and verify TurnConclusion facts (D23-A)."""

    def __init__(self, event_store, runtime) -> None:
        if not hasattr(event_store, "append_batch") or not hasattr(
            event_store, "read_stream"
        ):
            raise TypeError("event_store must implement EventStore")
        self.event_store = event_store
        self.runtime = runtime

    # -- streams -----------------------------------------------------------

    def stream(self, turn_id: UUID) -> StreamId:
        return StreamId(MEMORY_STREAM_CATEGORY, turn_id)

    def _memory_version(self, turn_id: UUID) -> int:
        events = self.event_store.read_stream(
            self.stream(turn_id), after_version=-1, limit=500
        )
        return events[-1].stream_version if events else -1

    # -- source heads ------------------------------------------------------

    def source_heads(
        self,
        turn: TurnState,
        run: RunState | None,
        inspection: RunEffectInspection | None,
    ) -> tuple[StreamPrecondition, ...]:
        """Fences for one conclusion: turn head, run head, evidence head and
        every current effect head (run-effect-index included by inspect)."""
        preconditions: list[StreamPrecondition] = [
            StreamPrecondition(StreamId("turn", turn.turn_id), turn.version)
        ]
        if run is not None:
            preconditions.append(StreamPrecondition(StreamId("run", run.run_id), run.version))
        if run is not None and run.completion_evidence is not None:
            evidence = run.completion_evidence
            preconditions.append(
                StreamPrecondition(
                    evidence.stream_id, evidence.stream_version
                )
            )
        if inspection is not None:
            preconditions.extend(inspection.preconditions)
        return tuple(preconditions)

    @staticmethod
    def source_heads_digest(
        preconditions: Sequence[StreamPrecondition],
    ) -> str:
        heads = sorted(
            (item.stream_id.key, item.expected_version) for item in preconditions
        )
        return _digest(heads)

    # -- authoritative rebuild --------------------------------------------

    def _last_run_id(self, turn: TurnState) -> UUID | None:
        if turn.current_run_id is not None:
            return turn.current_run_id
        cursor = -1
        result: UUID | None = None
        stream = StreamId("turn", turn.turn_id)
        while True:
            page = self.event_store.read_stream(
                stream, after_version=cursor, limit=500
            )
            for event in page:
                if event.event_type == "turn.started.v1":
                    try:
                        result = UUID(event.payload["run_id"])
                    except (KeyError, TypeError, ValueError):
                        raise TurnConclusionError("turn_stream_corrupt") from None
            if len(page) < 500:
                return result
            cursor = page[-1].stream_version

    def _successful_tools(self, turn_id: UUID) -> tuple[str, ...]:
        """Tool names with a durable non-error result, from D6 facts.

        Pairs model.turn-completed.v1 tool calls (name) with
        tool.result-recorded.v1 results (is_error) on the run-execution
        stream; unpaired calls and error results are excluded.
        """
        calls: dict[tuple[str, str], str] = {}
        results: dict[tuple[str, str], bool] = {}
        try:
            events = self.event_store.read_stream(
                StreamId("run-execution", turn_id), after_version=-1, limit=500
            )
        except Exception:
            return ()
        for event in events:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            if event.event_type == "model.turn-completed.v1":
                try:
                    model_turn = payload["model_turn"]
                    model_turn_id = str(
                        UUID(model_turn["model_turn_id"])
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                for item in model_turn.get("output_items", ()):
                    if not isinstance(item, Mapping) or item.get("kind") != "tool_call":
                        continue
                    call_id = item.get("call_id")
                    name = item.get("name")
                    if isinstance(call_id, str) and isinstance(name, str):
                        calls[(model_turn_id, call_id)] = name
            elif event.event_type == "tool.result-recorded.v1":
                item = payload.get("context_item")
                if not isinstance(item, Mapping):
                    continue
                call_id = item.get("call_id")
                model_turn_id = item.get("model_turn_id")
                if isinstance(call_id, str) and isinstance(model_turn_id, str):
                    results[(model_turn_id, call_id)] = bool(item.get("is_error"))
        names = {
            name
            for key, name in calls.items()
            if key in results and not results[key]
        }
        return _bounded(sorted(names), MAX_TOOLS, "successful_tools")

    def _changed_files(self, turn_id: UUID, successful_tools: tuple[str, ...]) -> tuple[str, ...]:
        """Authoritative changed files: apply_patch success results only."""
        files: set[str] = set()
        try:
            events = self.event_store.read_stream(
                StreamId("run-execution", turn_id), after_version=-1, limit=500
            )
        except Exception:
            return ()
        for event in events:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            if event.event_type != "tool.result-recorded.v1":
                continue
            item = payload.get("context_item")
            if not isinstance(item, Mapping) or bool(item.get("is_error")):
                continue
            if "apply_patch" not in successful_tools:
                continue
            content = item.get("content")
            if isinstance(content, str):
                files.update(_changed_files_from_result(content))
        return tuple(sorted(files))[:MAX_FILES]

    def build(self, turn_id: UUID | str) -> TurnConclusion:
        """Rebuild the authoritative conclusion for a terminal Turn.

        Refuses non-terminal Turns and any source that cannot be read
        (fail-closed, content-free errors).
        """
        resolved = UUID(str(turn_id))
        try:
            turn = self.runtime.get_turn(resolved)
        except Exception:
            raise TurnConclusionError("turn_not_found") from None
        if not turn.is_terminal:
            raise TurnConclusionError("turn_not_terminal")
        thread = self.runtime.get_thread(turn.thread_id)
        run_id = self._last_run_id(turn)
        run: RunState | None = None
        inspection: RunEffectInspection | None = None
        if run_id is not None:
            try:
                run = self.runtime.get_run(run_id)
                inspection = RunEffectIndex(self.event_store).inspect(run_id)
            except Exception:
                raise TurnConclusionError("turn_conclusion_source_unavailable") from None

        preconditions = self.source_heads(turn, run, inspection)
        heads_digest = self.source_heads_digest(preconditions)

        request_summary = redact_text(turn.user_input)
        if len(request_summary) > REQUEST_SUMMARY_MAX_CHARS:
            request_summary = request_summary[:REQUEST_SUMMARY_MAX_CHARS]

        error_codes = _tool_error_codes(turn, run)
        successful_tools = self._successful_tools(resolved)
        changed_files = self._changed_files(resolved, successful_tools)

        evidence_refs: tuple[EvidenceRef, ...] = ()
        if run is not None and run.completion_evidence is not None:
            evidence = run.completion_evidence
            evidence_refs = (
                EvidenceRef(
                    evidence.stream_id,
                    evidence.stream_version,
                    evidence.event_id,
                    evidence.evidence_digest,
                ),
            )

        obligations: list[str] = []
        uncertainties: list[str] = []
        if inspection is not None:
            for ref in inspection.open_effects:
                obligations.append(f"{ref.effect_kind}:{ref.stream_id.key}")
            if any(
                ref.effect_kind in {"tool", "mcp-allocation"}
                for ref in inspection.open_effects
            ):
                uncertainties.append("tool_outcome_unknown")
            if any(
                ref.effect_kind == "workspace" for ref in inspection.open_effects
            ):
                uncertainties.append("workspace_outcome_unknown")
        if turn.pending_interrupt is not None:
            obligations.append(
                f"interrupt:{turn.pending_interrupt.interrupt_id}"
            )
        open_obligations = _bounded(
            obligations, MAX_OBLIGATIONS, "open_obligations"
        )
        uncertainty_codes = _bounded(
            uncertainties, MAX_UNCERTAINTIES, "uncertainty_codes"
        )

        conclusion = TurnConclusion(
            thread_id=turn.thread_id,
            turn_id=resolved,
            run_id=run_id,
            turn_status=turn.status.value,
            run_status=run.status.value if run is not None else None,
            request_summary=request_summary,
            error_codes=error_codes,
            successful_tools=successful_tools,
            changed_files=changed_files,
            test_evidence_refs=evidence_refs,
            open_obligations=open_obligations,
            uncertainty_codes=uncertainty_codes,
            authoritative_digest="",
            untrusted_summary=None,
            source_heads_digest=heads_digest,
        )
        document = conclusion.document()
        document["untrusted_summary"] = None
        document["authoritative_digest"] = ""
        digest = _digest(document)
        object.__setattr__(conclusion, "authoritative_digest", digest)
        return conclusion

    # -- persistence -------------------------------------------------------

    def persist(
        self,
        conclusion: TurnConclusion,
        *,
        command_id: UUID | None = None,
    ) -> AppendReceipt:
        """Append the conclusion to the turn-memory stream.

        Command identity is deterministic in (turn_id, source_heads_digest);
        retries return the original receipt.  A changed source head must be
        rebuilt first (new digest -> new revision); the old revision is then
        stale and never injected.
        """
        if not isinstance(conclusion, TurnConclusion):
            raise TypeError("conclusion must be TurnConclusion")
        command = command_id or uuid5(
            NAMESPACE_URL,
            f"turn-conclusion:{conclusion.turn_id}:{conclusion.source_heads_digest}",
        )
        payload = conclusion.document()
        event = NewEvent(
            uuid5(command, "event:" + CONCLUSION_EVENT_TYPE),
            CONCLUSION_EVENT_TYPE,
            CONCLUSION_SCHEMA_VERSION,
            datetime.now(timezone.utc),
            payload,
            EventMetadata(
                command,
                conclusion.turn_id,
                thread_id=conclusion.thread_id,
                turn_id=conclusion.turn_id,
                run_id=conclusion.run_id,
                actor="runtime",
            ),
        )
        try:
            return self.event_store.append_batch(
                (
                    StreamWrite(
                        self.stream(conclusion.turn_id),
                        self._memory_version(conclusion.turn_id),
                        (event,),
                    ),
                ),
                idempotency_key=command,
                request_fingerprint=conclusion.authoritative_digest,
            )
        except WrongExpectedVersion:
            raise TurnConclusionError("turn_conclusion_version_conflict") from None
        except EventStoreError as exc:
            raise TurnConclusionError("turn_conclusion_persist_failed") from exc

    # -- reading -----------------------------------------------------------

    def load(self, turn_id: UUID | str) -> tuple[TurnConclusion, ...]:
        """All recorded conclusions for the Turn, newest last."""
        resolved = UUID(str(turn_id))
        events = self.event_store.read_stream(
            self.stream(resolved), after_version=-1, limit=500
        )
        conclusions: list[TurnConclusion] = []
        for event in events:
            if event.event_type != CONCLUSION_EVENT_TYPE:
                raise TurnConclusionError("turn_memory_stream_corrupt")
            conclusions.append(
                TurnConclusion.from_document(event.payload)
            )
        return tuple(conclusions)

    def is_stale(self, conclusion: TurnConclusion) -> bool:
        """True when the conclusion's source heads no longer match current.

        A stale conclusion must never be injected into a prompt (D23 §4.2).
        """
        if not isinstance(conclusion, TurnConclusion):
            raise TypeError("conclusion must be TurnConclusion")
        try:
            turn = self.runtime.get_turn(conclusion.turn_id)
            run = None
            inspection = None
            if conclusion.run_id is not None:
                run = self.runtime.get_run(conclusion.run_id)
                inspection = RunEffectIndex(self.event_store).inspect(conclusion.run_id)
            current = self.source_heads_digest(
                self.source_heads(turn, run, inspection)
            )
        except Exception:
            return True
        return current != conclusion.source_heads_digest


__all__ = [
    "CONCLUSION_EVENT_TYPE",
    "EvidenceRef",
    "TurnConclusion",
    "TurnConclusionError",
    "TurnConclusionStore",
]
