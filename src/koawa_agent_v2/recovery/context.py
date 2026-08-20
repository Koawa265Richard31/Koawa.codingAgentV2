"""Rebuild canonical model context solely from typed run-execution facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from uuid import UUID

from ..control.event_store import StoredEvent
from .protocol import RunPhase


class ReconstructionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReconstructedContext:
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


def reconstruct_execution(events: Sequence[StoredEvent], *, initial: ReconstructedContext | None = None) -> ReconstructedContext:
    context: list[Mapping[str, Any]] = list(initial.context) if initial else []
    model_round = initial.model_round if initial else 0
    tool_count = initial.tool_count if initial else 0
    output_chars = initial.output_chars if initial else 0
    input_tokens = initial.input_tokens if initial else 0
    output_tokens = initial.output_tokens if initial else 0
    phase = initial.phase if initial else RunPhase.READY_FOR_MODEL
    run_id: UUID | None = initial.last_run_id if initial else None
    version = initial.execution_version if initial else -1
    pending = list(initial.pending_tool_calls) if initial else []
    final_text = initial.final_text if initial else None
    for event in events:
        if event.stream_version != version + 1:
            raise ReconstructionError("execution stream version gap")
        version = event.stream_version
        try:
            run_id = UUID(str(event.payload["run_id"]))
            if event.event_type == "run.context-seeded.v1":
                context = list(event.payload["context"])
                model_round = int(event.payload.get("model_round", 0))
                tool_count = int(event.payload.get("tool_count", 0))
                output_chars = int(event.payload.get("output_chars", 0))
                input_tokens = int(event.payload.get("input_tokens", 0))
                output_tokens = int(event.payload.get("output_tokens", 0))
                phase = RunPhase(
                    event.payload.get("phase", RunPhase.READY_FOR_MODEL.value)
                )
                pending = list(event.payload.get("pending_tool_calls", ()))
                final_text = event.payload.get("final_text")
            elif event.event_type == "model.turn-completed.v1":
                context.extend(event.payload["context_items"])
                model_round = int(event.payload["model_round"])
                output_chars = int(event.payload["output_chars"])
                input_tokens = int(event.payload.get("input_tokens", input_tokens))
                output_tokens = int(event.payload.get("output_tokens", output_tokens))
                phase = RunPhase(event.payload["next_phase"])
                pending = [item for item in event.payload["context_items"] if item.get("kind") == "tool_call"]
                final_text = event.payload["model_turn"].get("final_text") or None
            elif event.event_type == "tool.result-recorded.v1":
                context.append(event.payload["context_item"])
                tool_count = int(event.payload["tool_count"])
                ref = event.payload["context_item"]
                pending = [item for item in pending if not (item.get("model_turn_id") == ref.get("model_turn_id") and item.get("call_id") == ref.get("call_id"))]
                phase = RunPhase.READY_FOR_TOOL if pending else RunPhase.READY_FOR_MODEL
            elif event.event_type == "run.phase-advanced.v1":
                phase = RunPhase(event.payload["phase"])
            else:
                raise ReconstructionError(f"unknown execution fact: {event.event_type}")
        except (KeyError, TypeError, ValueError) as exc:
            raise ReconstructionError("corrupt execution fact") from exc
    if run_id is None:
        raise ReconstructionError("empty execution stream")
    return ReconstructedContext(tuple(context), model_round, tool_count, output_chars, input_tokens, output_tokens, phase, version, run_id, tuple(pending), final_text)


def checkpoint_state(*, context: Sequence[Mapping[str, Any]], model_round: int, tool_count: int, output_chars: int, input_tokens: int, output_tokens: int, phase: RunPhase, execution_version: int, run_id: UUID) -> ReconstructedContext:
    results = {(item.get("model_turn_id"), item.get("call_id")) for item in context if item.get("kind") == "tool_result"}
    pending = tuple(item for item in context if item.get("kind") == "tool_call" and (item.get("model_turn_id"), item.get("call_id")) not in results)
    final_text = None
    if phase is RunPhase.READY_TO_FINALIZE:
        last_id = next((item.get("model_turn_id") for item in reversed(context) if item.get("kind") == "assistant"), None)
        if last_id is not None:
            final_text = "".join(item.get("item", {}).get("text", "") for item in context if item.get("kind") == "assistant" and item.get("model_turn_id") == last_id)
    return ReconstructedContext(tuple(context), model_round, tool_count, output_chars, input_tokens, output_tokens, phase, execution_version, run_id, pending, final_text)
