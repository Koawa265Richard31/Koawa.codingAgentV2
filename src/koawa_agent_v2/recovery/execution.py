"""Typed execution facts and checkpoint projection used by the D6 loop."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from ..control.event_store import EventMetadata, NewEvent, StreamId, StreamPrecondition, StreamWrite
from ..model.protocol import AssistantMessage, AssistantTextItem, BlockedItem, InstructionMessage, InstructionRole, ModelCallRef, ModelContextItem, ModelTurn, PublicReasoningSummaryItem, ReasoningSummaryEcho, ToolCallEcho, ToolCallItem, ToolResultMessage, UserMessage
from ..control.sqlite_store import SqliteEventStore
from .protocol import Checkpoint, RunPhase, event_hash
from .redaction import redact_arguments_json, redact_json_value, redact_text
from .store import CheckpointStore


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
    if kind == "instruction": return InstructionMessage(InstructionRole(d["role"]), d["content"])
    if kind == "user": return UserMessage(d["input_id"], d["content"], d.get("source_interrupt_id"))
    if kind == "assistant":
        x=d["item"]; return AssistantMessage(d["provider"], UUID(d["model_turn_id"]), AssistantTextItem(x["index"], x["item_id"], x["text"]))
    if kind == "reasoning_summary":
        x=d["item"]; return ReasoningSummaryEcho(d["provider"], UUID(d["model_turn_id"]), PublicReasoningSummaryItem(x["index"], x["item_id"], x["summary"]))
    if kind == "tool_call":
        x=d["item"]; ref=ModelCallRef(UUID(d["model_turn_id"]), d["call_id"]); return ToolCallEcho(d["provider"], ref, ToolCallItem(x["index"], x["item_id"], d["call_id"], x["name"], x["arguments_json"]))
    if kind == "tool_result": return ToolResultMessage(ModelCallRef(UUID(d["model_turn_id"]), d["call_id"]), d["content"], d["is_error"])
    raise ValueError("unknown context document")


def model_turn_document(turn: ModelTurn) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    for item in turn.output_items:
        base = {"item_id": item.item_id, "canonical_index": item.canonical_index}
        if isinstance(item, AssistantTextItem): output.append({**base, "kind": "assistant_text", "text": redact_text(item.text)})
        elif isinstance(item, PublicReasoningSummaryItem): output.append({**base, "kind": "public_reasoning_summary", "summary": redact_text(item.summary)})
        elif isinstance(item, ToolCallItem): output.append({**base, "kind": "tool_call", "call_id": item.call_id, "name": item.name, "arguments_json": redact_arguments_json(item.arguments_json)})
        elif isinstance(item, BlockedItem): output.append({**base, "kind": "blocked", "blocked_kind": item.blocked_kind.value, "payload_length": item.payload_length, "sha256": item.sha256})
        else: raise TypeError("unsupported model output item")
    usage = None if turn.usage is None else {"input_tokens": turn.usage.input_tokens, "output_tokens": turn.usage.output_tokens, "total_tokens": turn.usage.total_tokens}
    return {"protocol_version": turn.protocol_version, "model_turn_id": str(turn.model_turn_id), "provider": turn.provider, "model": turn.model, "provider_response_id": turn.provider_response_id, "finish_reason": turn.finish_reason.value, "final_text": redact_text(turn.final_text), "output_items": output, "usage": usage}


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
) -> dict[str, Any]:
    """Build the complete replay seed committed atomically with ``turn.started``."""

    return {
        "context": [context_document(item) for item in initial_context],
        "model_round": model_round,
        "tool_count": tool_count,
        "output_chars": output_chars,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "phase": phase.value,
        "pending_tool_calls": redact_json_value(list(pending_calls)),
        "final_text": None if final_text is None else redact_text(final_text),
    }


class DurableExecutionRecorder:
    """Appends facts with a live Turn fence and derives a verified checkpoint."""
    def __init__(self, store: SqliteEventStore, checkpoints: CheckpointStore, *, thread_id: UUID, turn_id: UUID, run_id: UUID, turn_version: int, initial_context: Sequence[ModelContextItem], model_round: int = 0, tool_count: int = 0, output_chars: int = 0, input_tokens: int = 0, output_tokens: int = 0, pending_calls: Sequence[Mapping[str, Any]] = (), phase: RunPhase | None = None) -> None:
        self.store, self.checkpoints = store, checkpoints
        self.thread_id, self.turn_id, self.run_id, self.turn_version = thread_id, turn_id, run_id, turn_version
        self.context = [context_document(x) for x in initial_context]
        self.model_round, self.tool_count, self.output_chars = model_round, tool_count, output_chars
        self.input_tokens, self.output_tokens = input_tokens, output_tokens
        self.pending_calls = [dict(item) for item in redact_json_value(list(pending_calls))]
        self.phase = phase or (RunPhase.READY_FOR_TOOL if self.pending_calls else RunPhase.READY_FOR_MODEL)
        if not self.store.read_stream(StreamId("run-execution", turn_id), limit=1):
            self._append(
                "run.context-seeded.v1",
                execution_seed(
                    initial_context,
                    model_round=model_round,
                    tool_count=tool_count,
                    output_chars=output_chars,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    phase=self.phase,
                    pending_calls=pending_calls,
                ),
            )

    def model_completed(self, turn: ModelTurn, projected: Sequence[ModelContextItem], model_round: int, output_chars: int, has_tools: bool) -> None:
        docs = [context_document(x) for x in projected]
        self.context.extend(docs); self.model_round = model_round; self.output_chars = output_chars
        if turn.usage is not None:
            self.input_tokens += turn.usage.input_tokens
            self.output_tokens += turn.usage.output_tokens
        self.phase = RunPhase.READY_FOR_TOOL if has_tools else RunPhase.READY_TO_FINALIZE
        self.pending_calls = [item for item in docs if item["kind"] == "tool_call"]
        self._append("model.turn-completed.v1", {"model_turn": model_turn_document(turn), "context_items": docs, "model_round": model_round, "output_chars": output_chars, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "next_phase": self.phase.value})

    def tool_started(self, call_id: str, tool_name: str) -> None:
        self.phase = RunPhase.TOOL_IN_PROGRESS
        self._append("run.phase-advanced.v1", {"phase": self.phase.value, "call_id": call_id, "tool_name": tool_name})

    def tool_completed(self, result: ToolResultMessage, tool_count: int) -> None:
        doc = context_document(result); self.context.append(doc); self.tool_count = tool_count
        self.pending_calls = [item for item in self.pending_calls if not (item["model_turn_id"] == doc["model_turn_id"] and item["call_id"] == doc["call_id"])]
        self.phase = RunPhase.READY_FOR_TOOL if self.pending_calls else RunPhase.READY_FOR_MODEL
        self._append("tool.result-recorded.v1", {"context_item": doc, "tool_count": tool_count})

    def _append(self, event_type: str, payload: Mapping[str, Any]) -> None:
        stream = StreamId("run-execution", self.turn_id)
        version = -1
        while True:
            page = self.store.read_stream(stream, after_version=version, limit=500)
            if not page: break
            version = page[-1].stream_version
            if len(page) < 500: break
        command_id = uuid5(NAMESPACE_URL, f"koawa-d6:{self.run_id}:{version + 1}:{event_type}")
        full = {"thread_id": str(self.thread_id), "turn_id": str(self.turn_id), "run_id": str(self.run_id), **payload}
        event = NewEvent(uuid5(command_id, "event"), event_type, 1, datetime.now(timezone.utc), full, EventMetadata(command_id, self.turn_id, self.thread_id, self.turn_id, self.run_id, "worker"))
        receipt = self.store.append_batch((StreamWrite(stream, version, (event,)),), idempotency_key=command_id, preconditions=(StreamPrecondition(StreamId("turn", self.turn_id), self.turn_version, "turn.started.v1", {"run_id": str(self.run_id)}),))
        saved = self.store.read_stream(stream, after_version=receipt.streams[0].last_version - 1, limit=1)[0]
        checkpoint_phase = RunPhase.BLOCKED_UNCERTAIN_SIDE_EFFECT if self.phase is RunPhase.TOOL_IN_PROGRESS else self.phase
        cp = Checkpoint(self.thread_id, self.turn_id, self.run_id, self.turn_version, saved.stream_version, 1, self.model_round, self.tool_count, self.output_chars, self.input_tokens, self.output_tokens, checkpoint_phase, saved.global_position, saved.commit_id, event_hash(saved.event_type, dict(saved.payload), saved.stream_version, saved.commit_id), tuple(self.context))
        self.checkpoints.save(cp)
