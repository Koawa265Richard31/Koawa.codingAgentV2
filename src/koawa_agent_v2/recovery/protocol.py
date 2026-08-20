"""D6 checkpoint wire contract. Checkpoints are verified projections, never truth."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping
from uuid import UUID


class RunPhase(StrEnum):
    READY_FOR_MODEL = "ready_for_model"
    READY_FOR_TOOL = "ready_for_tool"
    TOOL_IN_PROGRESS = "tool_in_progress"
    READY_TO_FINALIZE = "ready_to_finalize"
    BLOCKED_UNCERTAIN_SIDE_EFFECT = "blocked_uncertain_side_effect"


class CheckpointError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Checkpoint:
    thread_id: UUID
    turn_id: UUID
    run_id: UUID
    turn_version: int
    execution_version: int
    schema_version: int
    model_round: int
    tool_count: int
    output_chars: int
    input_tokens: int
    output_tokens: int
    phase: RunPhase
    covered_global_position: int
    covered_commit_id: UUID
    covered_event_hash: str
    context: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise CheckpointError("unsupported checkpoint schema")
        for name in ("turn_version", "execution_version", "model_round", "tool_count", "output_chars", "input_tokens", "output_tokens", "covered_global_position"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CheckpointError(f"invalid checkpoint {name}")
        if not isinstance(self.phase, RunPhase):
            raise CheckpointError("invalid checkpoint phase")
        if len(self.covered_event_hash) != 64:
            raise CheckpointError("invalid checkpoint event hash")

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": 1, "thread_id": str(self.thread_id), "turn_id": str(self.turn_id),
            "run_id": str(self.run_id), "turn_version": self.turn_version,
            "execution_version": self.execution_version, "model_round": self.model_round,
            "tool_count": self.tool_count, "output_chars": self.output_chars,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "phase": self.phase.value, "covered_global_position": self.covered_global_position,
            "covered_commit_id": str(self.covered_commit_id), "covered_event_hash": self.covered_event_hash,
            "context": [dict(item) for item in self.context],
        }

    @classmethod
    def parse(cls, value: str) -> "Checkpoint":
        try:
            d = json.loads(value)
            if set(d) != {"schema_version", "thread_id", "turn_id", "run_id", "turn_version", "execution_version", "model_round", "tool_count", "output_chars", "input_tokens", "output_tokens", "phase", "covered_global_position", "covered_commit_id", "covered_event_hash", "context"}:
                raise ValueError
            return cls(UUID(d["thread_id"]), UUID(d["turn_id"]), UUID(d["run_id"]), d["turn_version"], d["execution_version"], d["schema_version"], d["model_round"], d["tool_count"], d["output_chars"], d["input_tokens"], d["output_tokens"], RunPhase(d["phase"]), d["covered_global_position"], UUID(d["covered_commit_id"]), d["covered_event_hash"], tuple(d["context"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CheckpointError("invalid checkpoint document") from exc


def event_hash(event_type: str, payload: Mapping[str, Any], stream_version: int, commit_id: UUID) -> str:
    raw = json.dumps({"event_type": event_type, "payload": _plain(payload), "stream_version": stream_version, "commit_id": str(commit_id)}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping): return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)): return [_plain(item) for item in value]
    return value
