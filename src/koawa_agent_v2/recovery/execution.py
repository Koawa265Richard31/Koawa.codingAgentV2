"""Typed execution facts, v2 execution seed DTO and durable recorder.

The recorder appends canonical facts to the run-execution stream and publishes
a verified checkpoint cache only after the canonical reducer reproduces the
projection from the committed segment (section 7.5).  Writes use the typed
EventStore transaction; no recovery code touches SQL.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from ..control.event_store import (
    EventMetadata,
    EventStoreError,
    NewEvent,
    StreamId,
    StreamPrecondition,
    StreamWrite,
    WrongExpectedVersion,
)
from ..control.durable_json import canonical_json_bytes_v1
from ..model.protocol import (
    AssistantMessage,
    AssistantTextItem,
    BlockedItem,
    InstructionMessage,
    InstructionRole,
    ModelCallRef,
    ModelContextItem,
    ModelTurn,
    PublicReasoningSummaryItem,
    ReasoningSummaryEcho,
    ToolCallEcho,
    ToolCallItem,
    ToolDefinition,
    ToolResultMessage,
    UserMessage,
)
from .context import (
    COMPACTION_COMPACTED_EVENT,
    COMPACTION_INTENDED_EVENT,
    ExecutionProjection,
    ReconstructionError,
    reduce_execution,
)
from .protocol import (
    LIVE_RUN_TURN_EVENT_TYPES,
    REDUCER_NAME,
    REDUCER_VERSION,
    CheckpointError,
    RunPhase,
)
from .redaction import redact_arguments_json, redact_json_value, redact_text

SEED_EVENT_TYPE = "run.context-seeded.v2"
LEGACY_SEED_EVENT_TYPE = "run.context-seeded.v1"
SEED_SCHEMA_VERSION = 2
SEED_SEMANTICS_VERSION = 2
SEED_PROTOCOL_VERSION = 1

_MODEL_TURN_EVENT = "model.turn-completed.v1"
_TOOL_RESULT_EVENT = "tool.result-recorded.v1"
_PHASE_ADVANCE_EVENT = "run.phase-advanced.v1"


# ---------------------------------------------------------------------------
# context document serialization (unchanged canonical redacted shapes)
# ---------------------------------------------------------------------------


def _context_digest_document(context: Sequence[Mapping[str, Any]]) -> str:
    """Canonical SHA256 of the recorder's redacted context documents.

    Must match the reducer's _context_digest over the same documents so the
    intended/compacted facts verify on replay (D23 §5.7).
    """
    return hashlib.sha256(
        canonical_json_bytes_v1(
            [dict(item) for item in context], path="context"
        )
    ).hexdigest()


def context_document(item: ModelContextItem) -> dict[str, Any]:
    """Serialize only the durable, redacted model-context contract."""
    if isinstance(item, InstructionMessage):
        return {
            "kind": "instruction",
            "role": item.role.value,
            "content": redact_text(item.content),
        }
    if isinstance(item, UserMessage):
        return {
            "kind": "user",
            "input_id": item.input_id,
            "content": redact_text(item.content),
            "source_interrupt_id": item.source_interrupt_id,
        }
    if isinstance(item, AssistantMessage):
        return {
            "kind": "assistant",
            "provider": item.source_provider,
            "model_turn_id": str(item.model_turn_id),
            "item": {
                "item_id": item.item.item_id,
                "index": item.item.canonical_index,
                "text": redact_text(item.item.text),
            },
        }
    if isinstance(item, ReasoningSummaryEcho):
        return {
            "kind": "reasoning_summary",
            "provider": item.source_provider,
            "model_turn_id": str(item.model_turn_id),
            "item": {
                "item_id": item.item.item_id,
                "index": item.item.canonical_index,
                "summary": redact_text(item.item.summary),
            },
        }
    if isinstance(item, ToolCallEcho):
        return {
            "kind": "tool_call",
            "provider": item.source_provider,
            "model_turn_id": str(item.call_ref.model_turn_id),
            "call_id": item.call_ref.call_id,
            "item": {
                "item_id": item.item.item_id,
                "index": item.item.canonical_index,
                "name": item.item.name,
                "arguments_json": redact_arguments_json(item.item.arguments_json),
            },
        }
    if isinstance(item, ToolResultMessage):
        return {
            "kind": "tool_result",
            "model_turn_id": str(item.call_ref.model_turn_id),
            "call_id": item.call_ref.call_id,
            "content": redact_text(item.content),
            "is_error": item.is_error,
        }
    raise TypeError("unsupported context item")


def context_from_document(d: Mapping[str, Any]) -> ModelContextItem:
    kind = d.get("kind")
    if kind == "instruction":
        return InstructionMessage(InstructionRole(d["role"]), d["content"])
    if kind == "user":
        return UserMessage(d["input_id"], d["content"], d.get("source_interrupt_id"))
    if kind == "assistant":
        x = d["item"]
        return AssistantMessage(
            d["provider"],
            UUID(d["model_turn_id"]),
            AssistantTextItem(x["index"], x["item_id"], x["text"]),
        )
    if kind == "reasoning_summary":
        x = d["item"]
        return ReasoningSummaryEcho(
            d["provider"],
            UUID(d["model_turn_id"]),
            PublicReasoningSummaryItem(x["index"], x["item_id"], x["summary"]),
        )
    if kind == "tool_call":
        x = d["item"]
        ref = ModelCallRef(UUID(d["model_turn_id"]), d["call_id"])
        return ToolCallEcho(
            d["provider"],
            ref,
            ToolCallItem(x["index"], x["item_id"], d["call_id"], x["name"], x["arguments_json"]),
        )
    if kind == "tool_result":
        return ToolResultMessage(
            ModelCallRef(UUID(d["model_turn_id"]), d["call_id"]),
            d["content"],
            d["is_error"],
        )
    raise ValueError("unknown context document")


def model_turn_document(turn: ModelTurn) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    for item in turn.output_items:
        base = {"item_id": item.item_id, "canonical_index": item.canonical_index}
        if isinstance(item, AssistantTextItem):
            output.append({**base, "kind": "assistant_text", "text": redact_text(item.text)})
        elif isinstance(item, PublicReasoningSummaryItem):
            output.append(
                {**base, "kind": "public_reasoning_summary", "summary": redact_text(item.summary)}
            )
        elif isinstance(item, ToolCallItem):
            output.append(
                {
                    **base,
                    "kind": "tool_call",
                    "call_id": item.call_id,
                    "name": item.name,
                    "arguments_json": redact_arguments_json(item.arguments_json),
                }
            )
        elif isinstance(item, BlockedItem):
            output.append(
                {
                    **base,
                    "kind": "blocked",
                    "blocked_kind": item.blocked_kind.value,
                    "payload_length": item.payload_length,
                    "sha256": item.sha256,
                }
            )
        else:
            raise TypeError("unsupported model output item")
    usage = None
    if turn.usage is not None:
        usage = {
            "input_tokens": turn.usage.input_tokens,
            "output_tokens": turn.usage.output_tokens,
            "total_tokens": turn.usage.total_tokens,
        }
    return {
        "protocol_version": turn.protocol_version,
        "model_turn_id": str(turn.model_turn_id),
        "provider": turn.provider,
        "model": turn.model,
        "provider_response_id": turn.provider_response_id,
        "finish_reason": turn.finish_reason.value,
        "final_text": redact_text(turn.final_text),
        "output_items": output,
        "usage": usage,
    }



# ---------------------------------------------------------------------------
# tool catalog digest
# ---------------------------------------------------------------------------


def tool_definition_document(tool: ToolDefinition) -> dict[str, Any]:
    """Exact per-tool document for the seed request semantics."""
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def tool_catalog_digest(definitions: Sequence[ToolDefinition]) -> str:
    """lower_hex(SHA256(canonical bytes of the sorted definition documents))."""
    documents = [
        tool_definition_document(item)
        for item in sorted(definitions, key=lambda item: item.name)
    ]
    return hashlib.sha256(
        canonical_json_bytes_v1(documents, path="tool-catalog")
    ).hexdigest()


# ---------------------------------------------------------------------------
# v2 execution seed DTO (section 6.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionSeedDTO:
    """The immutable v2 seed exchanged with ThreadRuntime.start_turn.

    Identity fields (thread/turn/run/attempt/turn_stream_version) are filled by
    the runtime from the authoritative Turn state; the caller cannot fabricate
    them.  request_semantics pins provider/model/max tokens and the tool
    catalog; projection is the canonical 10-key document; resume references the
    typed Turn resume event that produced this seed.
    """

    request_semantics: Mapping[str, Any]
    projection: Mapping[str, Any]
    resume: Mapping[str, Any] | None
    thread_id: UUID | None = None
    turn_id: UUID | None = None
    run_id: UUID | None = None
    attempt: int | None = None
    turn_stream_version: int | None = None

    def with_identity(
        self,
        *,
        thread_id: UUID,
        turn_id: UUID,
        run_id: UUID,
        attempt: int,
        turn_stream_version: int,
    ) -> "ExecutionSeedDTO":
        return ExecutionSeedDTO(
            request_semantics=self.request_semantics,
            projection=self.projection,
            resume=self.resume,
            thread_id=thread_id,
            turn_id=turn_id,
            run_id=run_id,
            attempt=attempt,
            turn_stream_version=turn_stream_version,
        )

    def to_document_partial(self) -> dict[str, Any]:
        """Wire minus the runtime-owned identity fields (for fingerprints)."""
        return {
            "seed_schema_version": SEED_SCHEMA_VERSION,
            "request_semantics": dict(self.request_semantics),
            "projection": dict(self.projection),
            "resume": None if self.resume is None else dict(self.resume),
        }

    def to_document(self) -> dict[str, Any]:
        if (
            self.thread_id is None
            or self.turn_id is None
            or self.run_id is None
            or self.attempt is None
            or self.turn_stream_version is None
        ):
            raise ValueError("ExecutionSeedDTO identity fields are not filled")
        return {
            "seed_schema_version": SEED_SCHEMA_VERSION,
            "thread_id": str(self.thread_id),
            "turn_id": str(self.turn_id),
            "run_id": str(self.run_id),
            "attempt": self.attempt,
            "turn_stream_version": self.turn_stream_version,
            "request_semantics": dict(self.request_semantics),
            "projection": dict(self.projection),
            "resume": None if self.resume is None else dict(self.resume),
        }


def resume_document(
    turn_event_id: UUID,
    turn_event_type: str,
    context_item: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the seed resume block for one typed Turn resume event."""
    item = dict(context_item)
    return {
        "turn_event_id": str(turn_event_id),
        "turn_event_type": turn_event_type,
        "context_item": item,
        "content_digest": hashlib.sha256(
            canonical_json_bytes_v1(item, path="resume-item")
        ).hexdigest(),
    }


def execution_seed(
    initial_context: Sequence[ModelContextItem],
    *,
    model_round: int = 0,
    tool_count: int = 0,
    output_chars: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    phase: RunPhase = RunPhase.READY_FOR_MODEL,
    pending_calls: Sequence[Mapping[str, Any]] = (),
    final_text: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
    tool_definitions: Sequence[ToolDefinition] = (),
    resume: Mapping[str, Any] | None = None,
) -> ExecutionSeedDTO:
    """Build the full v2 seed: canonical context, pinned semantics, catalog."""
    if not isinstance(model_round, int) or isinstance(model_round, bool) or model_round < 0:
        raise ValueError("model_round must be a non-negative integer")
    if not isinstance(tool_count, int) or isinstance(tool_count, bool) or tool_count < 0:
        raise ValueError("tool_count must be a non-negative integer")
    if not isinstance(output_chars, int) or isinstance(output_chars, bool) or output_chars < 0:
        raise ValueError("output_chars must be a non-negative integer")
    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 0:
        raise ValueError("input_tokens must be a non-negative integer")
    if not isinstance(output_tokens, int) or isinstance(output_tokens, bool) or output_tokens < 0:
        raise ValueError("output_tokens must be a non-negative integer")
    if phase not in set(RunPhase):
        raise ValueError("phase must be a RunPhase")
    context_documents = [context_document(item) for item in initial_context]
    pending = redact_json_value(list(pending_calls))
    projection = {
        "context": [dict(item) for item in context_documents],
        "model_round": model_round,
        "tool_count": tool_count,
        "output_chars": output_chars,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "phase": phase.value,
        "pending_tool_calls": list(pending),
        "final_text": None if final_text is None else redact_text(final_text),
    }
    tool_documents = [
        tool_definition_document(item)
        for item in sorted(tool_definitions, key=lambda item: item.name)
    ]
    request_semantics = {
        "protocol_version": SEED_PROTOCOL_VERSION,
        "provider": provider,
        "model": model,
        "max_output_tokens": max_output_tokens,
        "input_items": [dict(item) for item in context_documents],
        "tool_definitions": tool_documents,
        "tool_catalog_digest": tool_catalog_digest(tool_definitions),
    }
    return ExecutionSeedDTO(
        request_semantics=request_semantics,
        projection=projection,
        resume=None if resume is None else dict(resume),
    )



# ---------------------------------------------------------------------------
# seed semantics + segment validation
# ---------------------------------------------------------------------------


def resolve_seed_semantics(events: Sequence[Any]) -> dict[str, Any]:
    """Recover request semantics pinned by the latest v2 seed in the facts.

    Legacy v1 seeds without a pinning block return an empty dict and the
    caller keeps its current RuntimeConfig values.
    """
    for event in reversed(tuple(events)):
        if event.event_type != SEED_EVENT_TYPE:
            continue
        payload = event.payload
        request = payload.get("request_semantics")
        if not isinstance(request, Mapping):
            return {}
        return {
            "provider": request.get("provider"),
            "model": request.get("model"),
            "max_output_tokens": request.get("max_output_tokens"),
            "tool_catalog_digest": request.get("tool_catalog_digest"),
        }
    return {}


def validate_execution_segments(events: Sequence[Any]) -> None:
    """Verify per-run segment integrity of the execution fact stream.

    Every segment begins with exactly one seed (run.context-seeded.v2; a
    legacy v1 seed is allowed only as the very first fact); each non-seed fact
    must carry the same run_id as its segment seed.  A forged second seed, a
    missing leading seed, or a foreign-run fact fails closed.
    """
    active_run: Any = None
    seeded_runs: set[Any] = set()
    first_event = True
    for event in events:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        run_id = payload.get("run_id")
        if event.event_type == SEED_EVENT_TYPE:
            if run_id in seeded_runs:
                raise ReconstructionError("forged second seed for a run segment")
            seeded_runs.add(run_id)
            active_run = run_id
            first_event = False
            continue
        if event.event_type == LEGACY_SEED_EVENT_TYPE:
            if not first_event:
                raise ReconstructionError("legacy v1 seed outside the first segment")
            if run_id in seeded_runs:
                raise ReconstructionError("forged legacy seed")
            seeded_runs.add(run_id)
            active_run = run_id
            first_event = False
            continue
        first_event = False
        if active_run is None or run_id != active_run or run_id not in seeded_runs:
            raise ReconstructionError("execution fact outside a seeded run segment")


# ---------------------------------------------------------------------------
# live projection helper used by the recorder
# ---------------------------------------------------------------------------


def live_projection_from_state(
    *,
    context: Sequence[Mapping[str, Any]],
    model_round: int,
    tool_count: int,
    output_chars: int,
    input_tokens: int,
    output_tokens: int,
    phase: RunPhase,
    pending_calls: Sequence[Mapping[str, Any]],
    final_text: str | None,
    execution_version: int,
    last_run_id: UUID,
) -> ExecutionProjection:
    return ExecutionProjection(
        context=tuple(dict(item) for item in context),
        model_round=model_round,
        tool_count=tool_count,
        output_chars=output_chars,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        phase=phase,
        execution_version=execution_version,
        last_run_id=last_run_id,
        pending_tool_calls=tuple(dict(item) for item in pending_calls),
        final_text=final_text,
    )



# ---------------------------------------------------------------------------
# durable execution recorder
# ---------------------------------------------------------------------------


class DurableExecutionRecorder:
    """Appends canonical facts with a live Turn fence and a verified cache."""

    def __init__(
        self,
        store,
        checkpoints,
        *,
        thread_id: UUID,
        turn_id: UUID,
        run_id: UUID,
        turn_version: int,
        initial_context: Sequence[ModelContextItem],
        model_round: int = 0,
        tool_count: int = 0,
        output_chars: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        pending_calls: Sequence[Mapping[str, Any]] = (),
        phase: RunPhase | None = None,
        provider: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        tool_definitions: Sequence[Any] = (),
        attempt: int | None = None,
    ) -> None:
        self.store = store
        self.checkpoints = checkpoints
        self.thread_id, self.turn_id, self.run_id = thread_id, turn_id, run_id
        self.turn_version = turn_version
        self.context = [context_document(x) for x in initial_context]
        # Parallel to context: the run-execution stream version that produced
        # each item (seed items start at 0); compaction replaces by this range.
        self._source_versions = [0] * len(self.context)
        self.model_round, self.tool_count, self.output_chars = (
            model_round,
            tool_count,
            output_chars,
        )
        self.input_tokens, self.output_tokens = input_tokens, output_tokens
        self.pending_calls = [dict(item) for item in redact_json_value(list(pending_calls))]
        self.phase = phase or (
            RunPhase.READY_FOR_TOOL if self.pending_calls else RunPhase.READY_FOR_MODEL
        )
        self.final_text: str | None = None
        self._attempt = attempt if attempt is not None else 1
        if not self.store.read_stream(StreamId("run-execution", turn_id), limit=1):
            seed = execution_seed(
                initial_context,
                model_round=model_round,
                tool_count=tool_count,
                output_chars=output_chars,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                phase=self.phase,
                pending_calls=pending_calls,
                provider=provider,
                model=model,
                max_output_tokens=max_output_tokens,
                tool_definitions=tool_definitions,
            )
            dto = seed.with_identity(
                thread_id=thread_id,
                turn_id=turn_id,
                run_id=run_id,
                attempt=self._attempt,
                turn_stream_version=turn_version,
            )
            self._append_seed(dto.to_document())

    def _append_seed(self, document: Mapping[str, Any]) -> None:
        self._append_typed(SEED_EVENT_TYPE, document)

    def model_completed(
        self,
        turn: ModelTurn,
        projected: Sequence[ModelContextItem],
        model_round: int,
        output_chars: int,
        has_tools: bool,
    ) -> None:
        docs = [context_document(x) for x in projected]
        # The event appended below is the next stream version after the head.
        new_version = (
            self._stream_head_version(StreamId("run-execution", self.turn_id)) + 1
        )
        self.context.extend(docs)
        self._source_versions.extend([new_version] * len(docs))
        self.model_round = model_round
        self.output_chars = output_chars
        if turn.usage is not None:
            self.input_tokens += turn.usage.input_tokens
            self.output_tokens += turn.usage.output_tokens
        self.phase = (
            RunPhase.READY_FOR_TOOL if has_tools else RunPhase.READY_TO_FINALIZE
        )
        self.pending_calls = [item for item in docs if item["kind"] == "tool_call"]
        self.final_text = redact_text(turn.final_text) or None
        self._append_typed(
            _MODEL_TURN_EVENT,
            {
                "model_turn": model_turn_document(turn),
                "context_items": docs,
                "model_round": model_round,
                "output_chars": output_chars,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "next_phase": self.phase.value,
            },
        )

    def tool_started(self, call_id: str, tool_name: str) -> None:
        self.phase = RunPhase.TOOL_IN_PROGRESS
        self._append_typed(
            _PHASE_ADVANCE_EVENT,
            {"phase": self.phase.value, "call_id": call_id, "tool_name": tool_name},
        )

    def tool_completed(self, result: ToolResultMessage, tool_count: int) -> None:
        doc = context_document(result)
        new_version = (
            self._stream_head_version(StreamId("run-execution", self.turn_id)) + 1
        )
        self.context.append(doc)
        self._source_versions.append(new_version)
        self.tool_count = tool_count
        self.pending_calls = [
            item
            for item in self.pending_calls
            if not (
                item["model_turn_id"] == doc["model_turn_id"]
                and item["call_id"] == doc["call_id"]
            )
        ]
        self.phase = (
            RunPhase.READY_FOR_TOOL if self.pending_calls else RunPhase.READY_FOR_MODEL
        )
        self._append_typed(
            _TOOL_RESULT_EVENT,
            {"context_item": doc, "tool_count": tool_count},
        )

    def compact(
        self,
        *,
        epoch: int,
        source_first_version: int,
        source_last_version: int,
        replacement: Mapping[str, Any],
        resulting_context_digest: str,
        target_chars: int,
        summary_receipt_digest: str | None = None,
        source_event_ids_digest: str | None = None,
    ) -> None:
        """D23 §5.7: record an in-run compaction (intended + compacted).

        Both facts are appended on the run-execution stream with exact
        expected versions and deterministic command identities; a response
        loss retry returns the same receipts without duplicating facts.
        ``source_event_ids_digest`` is computed by the recorder from its own
        source-range context when the caller omits it.
        """
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ValueError("epoch must be a positive integer")
        if (
            not isinstance(source_first_version, int)
            or isinstance(source_first_version, bool)
            or source_first_version < 0
        ):
            raise ValueError("source_first_version must be an integer >= 0")
        if (
            not isinstance(source_last_version, int)
            or isinstance(source_last_version, bool)
            or source_last_version < source_first_version
        ):
            raise ValueError("source_last_version must cover source_first_version")
        if not isinstance(replacement, Mapping):
            # Audit F12: the loop hands the replacement over as a
            # ModelContextItem (UserMessage); normalize it into the canonical
            # context document so the direct production binding
            # loop -> recorder works without an adapter.
            try:
                replacement = context_document(replacement)
            except Exception:
                raise TypeError("replacement must be a context document") from None
        if not isinstance(target_chars, int) or isinstance(target_chars, bool) or target_chars < 1:
            raise ValueError("target_chars must be a positive integer")
        if summary_receipt_digest is not None and (
            not isinstance(summary_receipt_digest, str)
            or not summary_receipt_digest
        ):
            raise ValueError("summary_receipt_digest must be non-empty or None")
        if source_event_ids_digest is None:
            source_event_ids_digest = self._range_identity_digest(
                source_first_version, source_last_version
            )
        if not isinstance(source_event_ids_digest, str) or not source_event_ids_digest:
            raise ValueError("source_event_ids_digest must be non-empty")
        if not resulting_context_digest:
            resulting_context_digest = self._range_resulting_digest(
                source_first_version, source_last_version, replacement
            )
        if not isinstance(resulting_context_digest, str) or not resulting_context_digest:
            raise ValueError("resulting_context_digest must be non-empty")

        prior_context_digest = _context_digest_document(self.context)
        stream = StreamId("run-execution", self.turn_id)
        head = self._stream_head_version(stream)

        intended_command = uuid5(
            NAMESPACE_URL,
            f"koawa-d23:{self.run_id}:compaction-intended:{epoch}",
        )
        intended_event = NewEvent(
            uuid5(intended_command, "event"),
            COMPACTION_INTENDED_EVENT,
            1,
            datetime.now(timezone.utc),
            {
                "thread_id": str(self.thread_id),
                "turn_id": str(self.turn_id),
                "run_id": str(self.run_id),
                "epoch": epoch,
                "source_first_version": source_first_version,
                "source_last_version": source_last_version,
                "event_ids_digest": source_event_ids_digest,
                "prior_context_digest": prior_context_digest,
                "target_chars": target_chars,
            },
            EventMetadata(
                intended_command,
                self.turn_id,
                self.thread_id,
                self.turn_id,
                self.run_id,
                "worker",
            ),
        )
        self._append_fenced(stream, head, (intended_event,), intended_command)

        compacted_command = uuid5(
            NAMESPACE_URL,
            f"koawa-d23:{self.run_id}:compaction-compacted:{epoch}",
        )
        compacted_event = NewEvent(
            uuid5(compacted_command, "event"),
            COMPACTION_COMPACTED_EVENT,
            1,
            datetime.now(timezone.utc),
            {
                "thread_id": str(self.thread_id),
                "turn_id": str(self.turn_id),
                "run_id": str(self.run_id),
                "epoch": epoch,
                "replacement_item": dict(replacement),
                "event_ids_digest": source_event_ids_digest,
                "resulting_context_digest": resulting_context_digest,
                "summary_receipt_digest": summary_receipt_digest,
            },
            EventMetadata(
                compacted_command,
                self.turn_id,
                self.thread_id,
                self.turn_id,
                self.run_id,
                "worker",
            ),
        )
        self._append_fenced(stream, head + 1, (compacted_event,), compacted_command)

        # Replace exactly the source-range items in the in-memory projection so
        # the live state matches what the reducer reproduces from the facts
        # (seed anchors before the range are preserved).  Items are selected by
        # version membership, not by positional break: a replacement item
        # already carries a LATER version than the range yet may sit BEFORE it
        # in the list (older compaction), so ordering by version is unsafe.
        # Select the version-contiguous run covering [source_first_version,
        # source_last_version].  An earlier replacement may sit before the
        # range in the list with a version inside it; it is an anchor and must
        # be skipped, never swept into the source.  Version gaps caused by
        # phase-advance facts are fine; the run ends at source_last_version.
        indices: list[int] = []
        previous: int | None = None
        for index, (item, version_at) in enumerate(
            zip(self.context, self._source_versions)
        ):
            if item.get("kind") in {"instruction", "user"}:
                continue  # anchors (earlier replacements) are never sources
            if version_at < source_first_version:
                continue
            if version_at > source_last_version:
                continue  # a later fact or replacement outside the range
            if previous is not None and version_at < previous:
                raise CheckpointError("compaction_source_range_not_contiguous")
            indices.append(index)
            previous = version_at
        if not indices or indices != list(range(indices[0], indices[-1] + 1)):
            raise CheckpointError("compaction_source_range_missing")
        start, end = indices[0], indices[-1] + 1
        if self._source_versions[indices[0]] != source_first_version:
            raise CheckpointError("compaction_source_range_missing")
        if self._source_versions[indices[-1]] != source_last_version:
            raise CheckpointError("compaction_source_range_missing")
        if not self.context[start:end]:
            raise CheckpointError("compaction_source_range_missing")
        # Mirror the reducer's anchor rule: the source may never contain an
        # instruction/user item (an earlier replacement) or an unpaired call.
        for item in self.context[start:end]:
            if item.get("kind") in {"instruction", "user"}:
                raise CheckpointError("compaction_source_contains_anchor")
        calls = {
            (item.get("model_turn_id"), item.get("call_id"))
            for item in self.context[start:end]
            if item.get("kind") == "tool_call"
        }
        for item in self.context[start:end]:
            if item.get("kind") == "tool_result":
                calls.discard((item.get("model_turn_id"), item.get("call_id")))
        if calls:
            raise CheckpointError("compaction_source_contains_open_call")
        self.context[start:end] = [dict(replacement)]
        # The replacement occupies the COMPACTED event version (intended is
        # head+1, compacted is head+2), so later facts keep larger versions
        # and the source-range ordering stays monotonic.
        self._source_versions[start:end] = [head + 2]
        return start, end

    def source_versions_for(self, first: int, last: int) -> tuple[int, int]:
        """Map projection indices to the recorder's own stream versions.

        The loop projection and the recorder projection are identical at the
        pre-request safe point (the recorder has recorded every item the loop
        sees), so the caller's indices apply directly to ``_source_versions``.
        The span is clamped to the version-contiguous run starting at
        ``first``: an earlier compaction's replacement may carry a version
        larger than later items, and sweeping it in would violate the
        reducer's anchor rule.
        """
        if (
            not isinstance(first, int) or isinstance(first, bool) or first < 0
            or not isinstance(last, int) or isinstance(last, bool)
            or last < first or last >= len(self._source_versions)
        ):
            raise CheckpointError("compaction_source_range_missing")
        first_version = self._source_versions[first]
        end = first
        previous = first_version
        for index in range(first + 1, last + 1):
            if self._source_versions[index] < previous:
                break
            previous = self._source_versions[index]
            end = index
        return first_version, self._source_versions[end]

    def synced_context(self):
        """The authoritative projection context as ModelContextItems."""
        return [context_from_document(doc) for doc in self.context]

    def _range_identity_digest(
        self, source_first_version: int, source_last_version: int
    ) -> str:
        """Identity digest of the source-range context items (D23 §5.7).

        Mirrors the reducer's _selected_event_ids_digest over the same
        documents so the intended/compacted facts verify on replay.  Anchors
        (earlier replacements) inside the version span are excluded exactly
        like the reducer excludes them.
        """
        selected = [
            item
            for item, version_at in zip(self.context, self._source_versions)
            if source_first_version <= version_at <= source_last_version
            and item.get("kind") not in {"instruction", "user"}
        ]
        identities = [
            (item.get("kind"), item.get("model_turn_id"), item.get("call_id"))
            for item in selected
        ]
        return hashlib.sha256(
            canonical_json_bytes_v1(identities, path="compaction-source")
        ).hexdigest()

    def _range_resulting_digest(
        self,
        source_first_version: int,
        source_last_version: int,
        replacement: Mapping[str, Any],
    ) -> str:
        """Resulting-context digest after replacing the source range (D23 §5.7).

        Mirrors the reducer's resulting-context verification so the compacted
        fact matches replay: the replacement occupies the position of the
        first removed non-anchor item; anchors inside the version span are
        left untouched.
        """
        updated: list[dict[str, Any]] = []
        replaced = False
        for item, version_at in zip(self.context, self._source_versions):
            in_range = source_first_version <= version_at <= source_last_version
            if in_range and item.get("kind") not in {"instruction", "user"}:
                if not replaced:
                    updated.append(dict(replacement))
                    replaced = True
                continue
            updated.append(dict(item))
        if not replaced:
            updated.append(dict(replacement))
        return _context_digest_document(updated)

    def _stream_head_version(self, stream: StreamId) -> int:
        cursor = -1
        while True:
            page = self.store.read_stream(stream, after_version=cursor, limit=500)
            if not page:
                return cursor
            cursor = page[-1].stream_version
            if len(page) < 500:
                return cursor

    def _append_fenced(
        self,
        stream: StreamId,
        expected_version: int,
        events: tuple[NewEvent, ...],
        command_id: UUID,
    ) -> None:
        for _attempt in range(2):
            head_fence = self._turn_head_fence()
            if head_fence is None:
                raise EventStoreError("recorder turn fence: turn stream missing")
            fence_version, head_type, head_run = head_fence
            if (
                head_type not in LIVE_RUN_TURN_EVENT_TYPES
                or head_run != str(self.run_id)
            ):
                raise EventStoreError("recorder turn fence is no longer active")
            try:
                self.store.append_batch(
                    (StreamWrite(stream, expected_version, events),),
                    idempotency_key=command_id,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("turn", self.turn_id),
                            fence_version,
                            head_type,
                            {"run_id": str(self.run_id)},
                        ),
                    ),
                )
                return
            except WrongExpectedVersion:
                continue
        raise CheckpointError("execution_fence_retry_exhausted")

    def _append_typed(self, event_type: str, payload: Mapping[str, Any]) -> None:
        stream = StreamId("run-execution", self.turn_id)
        version = -1
        while True:
            page = self.store.read_stream(stream, after_version=version, limit=500)
            if not page:
                break
            version = page[-1].stream_version
            if len(page) < 500:
                break
        command_id = uuid5(NAMESPACE_URL, f"koawa-d6:{self.run_id}:{version + 1}:{event_type}")
        full = {
            "thread_id": str(self.thread_id),
            "turn_id": str(self.turn_id),
            "run_id": str(self.run_id),
            **dict(payload),
        }
        declared = int(event_type.rsplit(".v", 1)[1])
        event = NewEvent(
            uuid5(command_id, "event"),
            event_type,
            declared,
            datetime.now(timezone.utc),
            full,
            EventMetadata(
                command_id,
                self.turn_id,
                self.thread_id,
                self.turn_id,
                self.run_id,
                "worker",
            ),
        )
        # The Turn fence must accept any same-run live event (typed lease
        # heartbeats/claims advance the stream head), not only turn.started.
        received = None
        for _attempt in range(2):
            head_fence = self._turn_head_fence()
            if head_fence is None:
                raise EventStoreError("recorder turn fence: turn stream missing")
            fence_version, head_type, head_run = head_fence
            if (
                head_type not in LIVE_RUN_TURN_EVENT_TYPES
                or head_run != str(self.run_id)
            ):
                raise EventStoreError("recorder turn fence is no longer active")
            try:
                receipt = self.store.append_batch(
                    (StreamWrite(stream, version, (event,)),),
                    idempotency_key=command_id,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("turn", self.turn_id),
                            fence_version,
                            head_type,
                            {"run_id": str(self.run_id)},
                        ),
                    ),
                )
                received = receipt
                break
            except WrongExpectedVersion:
                # A heartbeat landed between read and commit; retry once.
                continue
        if received is None:
            raise CheckpointError("execution_fence_retry_exhausted")
        saved = self.store.read_stream(
            stream, after_version=receipt.streams[0].last_version - 1, limit=1
        )[0]
        projection = live_projection_from_state(
            context=self.context,
            model_round=self.model_round,
            tool_count=self.tool_count,
            output_chars=self.output_chars,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            phase=self.phase,
            pending_calls=self.pending_calls,
            final_text=self.final_text,
            execution_version=saved.stream_version,
            last_run_id=self.run_id,
        )
        self.checkpoints.publish_from_source(
            thread_id=self.thread_id,
            turn_id=self.turn_id,
            run_id=self.run_id,
            turn_version=self._turn_head_version(),
            source_event=saved,
            projection=projection,
        )

    def _turn_head_version(self) -> int:
        """Current Turn stream head version (fence for typed-heartbeat turns)."""
        cursor = -1
        while True:
            page = self.store.read_stream(
                StreamId("turn", self.turn_id), after_version=cursor, limit=500
            )
            if not page:
                return cursor
            cursor = page[-1].stream_version
            if len(page) < 500:
                return cursor

    def _turn_head_fence(self):
        """Live head (version, event_type, run_id) of the Turn stream."""
        cursor = -1
        head = None
        while True:
            page = self.store.read_stream(
                StreamId("turn", self.turn_id), after_version=cursor, limit=500
            )
            if not page:
                break
            head = page[-1]
            cursor = head.stream_version
            if len(page) < 500:
                break
        if head is None:
            return None
        raw_run = head.payload.get("run_id") if isinstance(head.payload, Mapping) else None
        return (head.stream_version, head.event_type, str(raw_run) if raw_run is not None else None)