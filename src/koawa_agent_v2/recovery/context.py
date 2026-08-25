"""Rebuild canonical execution projection solely from typed run-execution facts.

reduce_execution is the single canonical reducer for the run-execution stream
(section 7.5): it verifies stream version continuity, schema-suffix
consistency, aggregate/metadata/payload identity and per-run segment integrity,
derives counters/pending/final/phase exclusively from events, and fails closed
on a corrupt event log.  No checkpoint field is ever trusted as a truth input;
checkpoints are only compared against this reducer's output.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from uuid import UUID

from ..control.durable_json import EVENT_PAYLOAD_READ_V1, canonical_json_bytes_v1
from ..control.event_store import StoredEvent
from .protocol import (
    PROJECTION_KEYS,
    RunPhase,
)

# The v2 seed projection is the 9-key document (no last_run_id; the run is
# carried by the seed identity), while the checkpoint wire projection adds
# last_run_id.
SEED_PROJECTION_KEYS = PROJECTION_KEYS - {"last_run_id"}

SEED_EVENT_TYPE = "run.context-seeded.v2"
LEGACY_SEED_EVENT_TYPE = "run.context-seeded.v1"

_MODEL_TURN_EVENT = "model.turn-completed.v1"
_TOOL_RESULT_EVENT = "tool.result-recorded.v1"
_PHASE_ADVANCE_EVENT = "run.phase-advanced.v1"

_CONTEXT_KINDS = frozenset(
    {
        "instruction",
        "user",
        "assistant",
        "reasoning_summary",
        "tool_call",
        "tool_result",
    }
)

_REQUEST_SEMANTICS_KEYS = frozenset(
    {
        "protocol_version",
        "provider",
        "model",
        "max_output_tokens",
        "input_items",
        "tool_definitions",
        "tool_catalog_digest",
    }
)

_RESUME_KEYS = frozenset(
    {"turn_event_id", "turn_event_type", "context_item", "content_digest"}
)


class ReconstructionError(RuntimeError):
    """Content-free corruption failure; carries no user text."""


@dataclass(frozen=True, slots=True)
class ExecutionProjection:
    """The canonical projection of one execution fact stream.

    context holds redacted canonical context documents; counters and phase are
    derived exclusively from events (never spliced from a checkpoint).
    """

    context: tuple[Mapping[str, Any], ...]
    model_round: int
    tool_count: int
    output_chars: int
    input_tokens: int
    output_tokens: int
    phase: RunPhase
    execution_version: int
    last_run_id: UUID
    pending_tool_calls: tuple[Mapping[str, Any], ...] = ()
    final_text: str | None = None


# Backwards-compatible alias for callers that predate the rename.
ReconstructedContext = ExecutionProjection


# ---------------------------------------------------------------------------
# projection document + digest
# ---------------------------------------------------------------------------


def projection_document(projection: ExecutionProjection) -> dict[str, Any]:
    """The exact canonical 10-key projection document (section 7.4)."""
    return {
        "context": [dict(item) for item in projection.context],
        "final_text": projection.final_text,
        "input_tokens": projection.input_tokens,
        "last_run_id": str(projection.last_run_id),
        "model_round": projection.model_round,
        "output_chars": projection.output_chars,
        "output_tokens": projection.output_tokens,
        "pending_tool_calls": [dict(item) for item in projection.pending_tool_calls],
        "phase": projection.phase.value,
        "tool_count": projection.tool_count,
    }


def projection_digest(projection: ExecutionProjection) -> str:
    """lower_hex(SHA256(canonical_json_bytes_v1(projection_document)))."""
    return hashlib.sha256(
        canonical_json_bytes_v1(projection_document(projection), path="projection")
    ).hexdigest()


def _canonical_equal(left: Any, right: Any) -> bool:
    return json.dumps(
        _plain(left), sort_keys=True, separators=(",", ":")
    ) == json.dumps(_plain(right), sort_keys=True, separators=(",", ":"))


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    return value


# ---------------------------------------------------------------------------
# seed document validation
# ---------------------------------------------------------------------------


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReconstructionError("corrupt execution seed: " + name)
    return dict(value)


def _require_seed_v2(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("seed_schema_version") != 2:
        raise ReconstructionError("corrupt execution seed schema version")
    expected = {
        "seed_schema_version",
        "thread_id",
        "turn_id",
        "run_id",
        "attempt",
        "turn_stream_version",
        "request_semantics",
        "projection",
        "resume",
    }
    if set(payload) != expected:
        raise ReconstructionError("corrupt execution seed key set")
    request = _require_object(payload["request_semantics"], "request_semantics")
    if set(request) != _REQUEST_SEMANTICS_KEYS:
        raise ReconstructionError("corrupt seed request_semantics key set")
    seed_projection = _require_object(payload["projection"], "projection")
    if set(seed_projection) != SEED_PROJECTION_KEYS:
        raise ReconstructionError("corrupt seed projection key set")
    for name in (
        "model_round",
        "tool_count",
        "output_chars",
        "input_tokens",
        "output_tokens",
    ):
        value = seed_projection[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReconstructionError("corrupt seed projection counter " + name)
    if seed_projection["phase"] not in set(RunPhase):
        raise ReconstructionError("corrupt seed projection phase")
    if (
        seed_projection["final_text"] is not None
        and not isinstance(seed_projection["final_text"], str)
    ):
        raise ReconstructionError("corrupt seed projection final_text")
    resume = payload.get("resume")
    if resume is not None:
        resume = _require_object(resume, "resume")
        if set(resume) != _RESUME_KEYS:
            raise ReconstructionError("corrupt seed resume key set")
    return {
        "thread_id": str(payload["thread_id"]),
        "turn_id": str(payload["turn_id"]),
        "run_id": str(payload["run_id"]),
        "attempt": payload["attempt"],
        "turn_stream_version": payload["turn_stream_version"],
        "request_semantics": request,
        "projection": {
            "context": list(seed_projection["context"]),
            "model_round": seed_projection["model_round"],
            "tool_count": seed_projection["tool_count"],
            "output_chars": seed_projection["output_chars"],
            "input_tokens": seed_projection["input_tokens"],
            "output_tokens": seed_projection["output_tokens"],
            "phase": RunPhase(seed_projection["phase"]),
            "pending_tool_calls": list(seed_projection["pending_tool_calls"]),
            "final_text": seed_projection["final_text"],
        },
        "resume": resume,
    }


def _require_legacy_seed_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the historical flat v1 seed (first legacy segment only)."""
    allowed = {
        "thread_id",
        "turn_id",
        "run_id",
        "context",
        "model_round",
        "tool_count",
        "output_chars",
        "input_tokens",
        "output_tokens",
        "phase",
        "pending_tool_calls",
        "final_text",
        "seed_semantics_version",
        "provider",
        "model",
        "max_output_tokens",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise ReconstructionError("corrupt legacy seed unknown key")
    phase_value = payload.get("phase", RunPhase.READY_FOR_MODEL.value)
    if phase_value not in set(RunPhase):
        raise ReconstructionError("corrupt legacy seed phase")
    return {
        "context": list(payload.get("context", ())),
        "model_round": int(payload.get("model_round", 0)),
        "tool_count": int(payload.get("tool_count", 0)),
        "output_chars": int(payload.get("output_chars", 0)),
        "input_tokens": int(payload.get("input_tokens", 0)),
        "output_tokens": int(payload.get("output_tokens", 0)),
        "phase": RunPhase(phase_value),
        "pending_tool_calls": list(payload.get("pending_tool_calls", ())),
        "final_text": payload.get("final_text"),
    }


def _validate_context_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ReconstructionError("corrupt context item type")
    kind = item.get("kind")
    if kind not in _CONTEXT_KINDS:
        raise ReconstructionError("corrupt context item kind")
    return dict(item)


def _validate_context_items(items: Sequence[Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(_validate_context_item(item) for item in items)



# ---------------------------------------------------------------------------
# canonical reducer
# ---------------------------------------------------------------------------


def _seed_projection(seed: dict[str, Any]) -> ExecutionProjection:
    projection = seed["projection"]
    return ExecutionProjection(
        context=tuple(projection["context"]),
        model_round=projection["model_round"],
        tool_count=projection["tool_count"],
        output_chars=projection["output_chars"],
        input_tokens=projection["input_tokens"],
        output_tokens=projection["output_tokens"],
        phase=projection["phase"],
        execution_version=-1,
        last_run_id=UUID(seed["run_id"]),
        pending_tool_calls=tuple(projection["pending_tool_calls"]),
        final_text=projection["final_text"],
    )


def reduce_execution(
    events: Sequence[StoredEvent],
    initial: ExecutionProjection | None = None,
) -> ExecutionProjection:
    """Reduce run-execution facts into the canonical projection.

    Rules (section 7.5): stream versions contiguous; event_type schema suffix
    equals schema_version; payload/metadata thread/turn/run agree with the
    stream; each run segment begins with exactly one seed (v2; v1 only as the
    first legacy segment); a non-first seed must equal the previous projection
    plus its referenced resume item (no context overwrite, no counter reset);
    model_round strictly increments by one per model turn; tool_count derives
    from result pairing; output/usage/pending/final/phase are recomputed from
    events; unpaired tool calls stay pending; corrupt logs fail closed.
    """
    if not events and initial is not None:
        return initial
    if initial is None:
        if not events:
            raise ReconstructionError("empty execution stream")
        version = -1
        context: list[Mapping[str, Any]] = []
        model_round = 0
        tool_count = 0
        output_chars = 0
        input_tokens = 0
        output_tokens = 0
        phase = RunPhase.READY_FOR_MODEL
        last_run_id: UUID | None = None
        pending: list[Mapping[str, Any]] = []
        final_text: str | None = None
        pinned_semantics: dict[str, Any] | None = None
        active_run: UUID | None = None
        previous_seed: dict[str, Any] | None = None
        first_seed: bool = True
        seeded_runs: set[UUID] = set()
    else:
        version = initial.execution_version
        context = list(initial.context)
        model_round = initial.model_round
        tool_count = initial.tool_count
        output_chars = initial.output_chars
        input_tokens = initial.input_tokens
        output_tokens = initial.output_tokens
        phase = initial.phase
        last_run_id = initial.last_run_id
        pending = list(initial.pending_tool_calls)
        final_text = initial.final_text
        pinned_semantics = None
        active_run = initial.last_run_id
        # A verified initial projection already begins inside a seeded segment,
        # so a later resume seed is a valid subsequent seed.
        previous_seed = {"initial": True}
        first_seed = False

    for event in events:
        if event.stream_version != version + 1:
            raise ReconstructionError("execution stream version gap")
        version = event.stream_version
        declared = _declared_schema_version(event.event_type)
        if declared != event.schema_version:
            raise ReconstructionError("execution fact schema suffix mismatch")
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        run_id = _event_run_id(event, payload)
        if run_id is None:
            raise ReconstructionError("execution fact missing run_id")
        if event.event_type == SEED_EVENT_TYPE:
            seed = _require_seed_v2(payload)
            parsed_run = UUID(seed["run_id"])
            if parsed_run != run_id:
                raise ReconstructionError("seed run_id mismatch")
            if parsed_run in seeded_runs:
                raise ReconstructionError("forged second seed for a run segment")
            seeded_runs.add(parsed_run)
            if first_seed:
                # A first seed may reference a resume Turn event when the turn
                # was waiting before any fact was recorded (legacy path); its
                # projection is authoritative because there is no prior truth.
                projection = _seed_projection(seed)
                if not _canonical_equal(
                    seed["request_semantics"]["input_items"],
                    seed["projection"]["context"],
                ):
                    raise ReconstructionError("seed input_items/context mismatch")
                context = list(projection.context)
                model_round = projection.model_round
                tool_count = projection.tool_count
                output_chars = projection.output_chars
                input_tokens = projection.input_tokens
                output_tokens = projection.output_tokens
                phase = projection.phase
                pending = list(projection.pending_tool_calls)
                final_text = projection.final_text
                last_run_id = parsed_run
                active_run = parsed_run
                pinned_semantics = _pin_from_request(seed["request_semantics"])
                first_seed = False
            else:
                # Subsequent seed must be derived from the previous projection:
                # either a no-resume seed whose projection equals the previous
                # one field-for-field, or a resume seed that appends exactly
                # one referenced resume item.  Context can never be overwritten
                # and counters can never be reset.
                resume = seed["resume"]
                if resume is None:
                    if not _canonical_equal(seed["projection"], _previous_projection_document(context, model_round, tool_count, output_chars, input_tokens, output_tokens, phase, pending, final_text)):
                        raise ReconstructionError("subsequent seed changed projection")
                    last_run_id = parsed_run
                    active_run = parsed_run
                    previous_seed = seed
                    continue
                if previous_seed is None:
                    raise ReconstructionError("seed without leading segment seed")
                resume_item = _validate_context_item(resume["context_item"])
                previous_context = [dict(item) for item in context]
                seed_context = list(seed["projection"]["context"])
                appended = _canonical_equal(
                    seed_context, previous_context + [resume_item]
                )
                repinned = (
                    bool(previous_context)
                    and _canonical_equal(seed_context, previous_context)
                    and _canonical_equal(previous_context[-1], resume_item)
                )
                if not (appended or repinned):
                    raise ReconstructionError("resume seed overwrote context")
                if not _canonical_equal(
                    seed["projection"]["context"][-1], resume_item
                ):
                    raise ReconstructionError("resume seed context item mismatch")
                if (
                    seed["projection"]["model_round"] != model_round
                    or seed["projection"]["tool_count"] != tool_count
                    or seed["projection"]["output_chars"] != output_chars
                    or seed["projection"]["input_tokens"] != input_tokens
                    or seed["projection"]["output_tokens"] != output_tokens
                ):
                    raise ReconstructionError("resume seed reset counters")
                if pinned_semantics is not None:
                    current = _pin_from_request(seed["request_semantics"])
                    if current != pinned_semantics:
                        raise ReconstructionError(
                            "resume seed changed request semantics"
                        )
                if not isinstance(resume["content_digest"], str) or not resume_item:
                    raise ReconstructionError("resume seed invalid digest")
                computed = hashlib.sha256(
                    canonical_json_bytes_v1(resume_item, path="resume-item")
                ).hexdigest()
                if computed != resume["content_digest"]:
                    raise ReconstructionError("resume seed content digest mismatch")
                new_phase = RunPhase(seed["projection"]["phase"])
                if new_phase not in (
                    RunPhase.READY_FOR_MODEL,
                    RunPhase.READY_FOR_TOOL,
                ):
                    raise ReconstructionError("resume seed invalid phase")
                # The validated seed projection context is authoritative
                # (prev + [item], or the idempotent re-pin of that same item).
                context = [dict(item) for item in seed_context]
                pending = list(seed["projection"]["pending_tool_calls"])
                phase = new_phase
                last_run_id = parsed_run
                active_run = parsed_run
            previous_seed = seed
            continue
        if event.event_type == LEGACY_SEED_EVENT_TYPE:
            if not first_seed or version != 0:
                raise ReconstructionError("legacy v1 seed outside first segment")
            if run_id in seeded_runs:
                raise ReconstructionError("forged legacy seed")
            seed = _require_legacy_seed_v1(payload)
            context = _validate_context_items(seed["context"])
            model_round = seed["model_round"]
            tool_count = seed["tool_count"]
            output_chars = seed["output_chars"]
            input_tokens = seed["input_tokens"]
            output_tokens = seed["output_tokens"]
            phase = seed["phase"]
            pending = list(seed["pending_tool_calls"])
            final_text = seed["final_text"]
            last_run_id = run_id
            active_run = run_id
            pinned_semantics = None
            first_seed = False
            previous_seed = {"legacy": True}
            seeded_runs.add(run_id)
            continue
        if active_run is None or run_id != active_run:
            raise ReconstructionError("execution fact outside its run segment")
        try:
            if event.event_type == _MODEL_TURN_EVENT:
                projected = _validate_context_items(payload.get("context_items", ()))
                declared_round = int(payload["model_round"])
                if declared_round != model_round + 1:
                    raise ReconstructionError("model_round must increment by one")
                model_round = declared_round
                context.extend(projected)
                output_chars = int(payload["output_chars"])
                if "input_tokens" in payload:
                    input_tokens = int(payload["input_tokens"])
                if "output_tokens" in payload:
                    output_tokens = int(payload["output_tokens"])
                phase = RunPhase(payload["next_phase"])
                pending = [item for item in projected if item.get("kind") == "tool_call"]
                model_turn = payload.get("model_turn")
                if isinstance(model_turn, Mapping):
                    final_text = model_turn.get("final_text") or None
            elif event.event_type == _TOOL_RESULT_EVENT:
                declared_count = int(payload["tool_count"])
                if declared_count != tool_count + 1:
                    raise ReconstructionError("tool_count must derive from results")
                tool_count = declared_count
                item = _validate_context_item(payload.get("context_item"))
                context.append(item)
                pending = [
                    existing
                    for existing in pending
                    if not (
                        existing.get("model_turn_id") == item.get("model_turn_id")
                        and existing.get("call_id") == item.get("call_id")
                    )
                ]
                phase = RunPhase.READY_FOR_TOOL if pending else RunPhase.READY_FOR_MODEL
            elif event.event_type == _PHASE_ADVANCE_EVENT:
                phase = RunPhase(payload["phase"])
            else:
                raise ReconstructionError("unknown execution fact: " + event.event_type)
        except (KeyError, TypeError, ValueError) as exc:
            raise ReconstructionError("corrupt execution fact") from exc
    if last_run_id is None:
        raise ReconstructionError("empty execution stream")
    return ExecutionProjection(
        context=tuple(context),
        model_round=model_round,
        tool_count=tool_count,
        output_chars=output_chars,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        phase=phase,
        execution_version=version,
        last_run_id=last_run_id,
        pending_tool_calls=tuple(pending),
        final_text=final_text,
    )


def _declared_schema_version(event_type: str) -> int:
    if not isinstance(event_type, str) or ".v" not in event_type:
        raise ReconstructionError("execution fact event type without schema suffix")
    suffix = event_type.rsplit(".v", 1)[1]
    try:
        return int(suffix)
    except ValueError as exc:
        raise ReconstructionError(
            "execution fact event type schema suffix corrupt"
        ) from exc


def _event_run_id(event: StoredEvent, payload: Mapping[str, Any]) -> UUID | None:
    raw = payload.get("run_id")
    if raw is None:
        raw = event.metadata.run_id
    if raw is None:
        return None
    try:
        value = raw if isinstance(raw, UUID) else UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        raise ReconstructionError("execution fact run_id corrupt") from None
    if event.metadata.run_id is not None and event.metadata.run_id != value:
        raise ReconstructionError("execution fact run_id metadata mismatch")
    return value


def _pin_from_request(request: Mapping[str, Any]) -> tuple[Any, ...]:
    """The reducer-verified request semantics of a segment seed."""
    return (
        request.get("provider"),
        request.get("model"),
        request.get("max_output_tokens"),
        request.get("tool_catalog_digest"),
    )


# reduce_execution is the canonical reducer; keep the historical name as alias.
reconstruct_execution = reduce_execution

def _previous_projection_document(
    context,
    model_round,
    tool_count,
    output_chars,
    input_tokens,
    output_tokens,
    phase,
    pending,
    final_text,
) -> dict[str, Any]:
    """The 9-key seed projection of the current reduction state."""
    return {
        "context": [dict(item) for item in context],
        "final_text": final_text,
        "input_tokens": input_tokens,
        "model_round": model_round,
        "output_chars": output_chars,
        "output_tokens": output_tokens,
        "pending_tool_calls": [dict(item) for item in pending],
        "phase": phase.value,
        "tool_count": tool_count,
    }