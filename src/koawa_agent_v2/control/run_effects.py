"""I7 per-Run effect index and terminal exact-head verification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping
from uuid import UUID, uuid5

from .event_store import (
    EventMetadata, NewEvent, StreamId, StreamPrecondition, StreamWrite,
)


@dataclass(frozen=True, slots=True)
class RunEffectRef:
    run_id: UUID
    effect_kind: str
    stream_id: StreamId
    identity_digest: str
    first_version: int
    index_version: int


@dataclass(frozen=True, slots=True)
class RunEffectInspection:
    refs: tuple[RunEffectRef, ...]
    preconditions: tuple[StreamPrecondition, ...]
    open_effects: tuple[RunEffectRef, ...]
    index_version: int

    @property
    def terminal_safe(self) -> bool:
        return not self.open_effects


class RunEffectIndex:
    """Append-only references to effects causally created by one Run."""

    def __init__(self, event_store) -> None:
        self.event_store = event_store

    def link_write(
        self,
        *,
        run_id: UUID,
        effect_kind: str,
        effect_stream: StreamId,
        identity_digest: str,
        first_version: int,
        command_id: UUID,
        actor: str,
    ) -> StreamWrite:
        _kind(effect_kind)
        _digest(identity_digest)
        if not isinstance(first_version, int) or isinstance(first_version, bool) or first_version < 0:
            raise ValueError("first_version must be a non-negative integer")
        stream = self.stream(run_id)
        events = self._read_all(stream)
        expected = events[-1].stream_version if events else -1
        payload = {
            "run_id": str(run_id),
            "effect_kind": effect_kind,
            "stream_category": effect_stream.category,
            "stream_id": str(effect_stream.aggregate_id),
            "identity_digest": identity_digest,
            "first_version": first_version,
        }
        event = NewEvent(
            uuid5(command_id, f"event:run-effect-linked:{effect_stream.key}"),
            "run.effect-linked.v1",
            1,
            self.event_store.database_time(),
            payload,
            EventMetadata(command_id, run_id, run_id=run_id, actor=actor),
        )
        return StreamWrite(stream, expected, (event,))

    def inspect(self, run_id: UUID) -> RunEffectInspection:
        events = self._read_all(self.stream(run_id))
        refs: list[RunEffectRef] = []
        seen: set[tuple[str, UUID]] = set()
        for event in events:
            if event.event_type != "run.effect-linked.v1":
                raise ValueError("run_effect_index_corrupt")
            payload = dict(event.payload)
            if set(payload) != {
                "run_id", "effect_kind", "stream_category", "stream_id",
                "identity_digest", "first_version",
            }:
                raise ValueError("run_effect_index_corrupt")
            if payload["run_id"] != str(run_id):
                raise ValueError("run_effect_index_corrupt")
            kind = _kind(payload["effect_kind"])
            identity = _digest(payload["identity_digest"])
            try:
                stream_id = StreamId(payload["stream_category"], UUID(payload["stream_id"]))
            except (TypeError, ValueError):
                raise ValueError("run_effect_index_corrupt") from None
            key = (stream_id.category, stream_id.aggregate_id)
            if key in seen:
                raise ValueError("run_effect_index_duplicate")
            seen.add(key)
            first_version = payload["first_version"]
            if not isinstance(first_version, int) or isinstance(first_version, bool) or first_version < 0:
                raise ValueError("run_effect_index_corrupt")
            refs.append(
                RunEffectRef(run_id, kind, stream_id, identity, first_version, event.stream_version)
            )
        preconditions: list[StreamPrecondition] = []
        open_effects: list[RunEffectRef] = []
        for ref in refs:
            effect_events = self._read_all(ref.stream_id)
            if not effect_events or effect_events[0].stream_version != ref.first_version:
                raise ValueError("run_effect_reference_missing")
            head = effect_events[-1]
            preconditions.append(StreamPrecondition(ref.stream_id, head.stream_version))
            if not _terminal_event(ref.effect_kind, head.event_type):
                open_effects.append(ref)
        index_version = events[-1].stream_version if events else -1
        preconditions.append(StreamPrecondition(self.stream(run_id), index_version))
        return RunEffectInspection(
            tuple(refs), tuple(preconditions), tuple(open_effects), index_version
        )

    @staticmethod
    def stream(run_id: UUID) -> StreamId:
        return StreamId("run-effect-index", run_id)

    def _read_all(self, stream: StreamId) -> tuple:
        values = []
        cursor = -1
        while True:
            page = self.event_store.read_stream(stream, after_version=cursor, limit=500)
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version


def effect_identity_digest(document: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(document), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8", "strict")
    return hashlib.sha256(encoded).hexdigest()


def _terminal_event(kind: str, event_type: str) -> bool:
    allowed = {
        "tool": frozenset({"tool.execution-succeeded.v1", "tool.execution-failed.v1"}),
        "workspace": frozenset({
            "workspace.effect-applied.v2",
            "workspace.effect-failed-before-effect.v1",
            "workspace.effect-outcome-resolved.v1",
        }),
        "mcp-allocation": frozenset({
            "mcp.process-stopped.v1", "mcp.process-failed-before-start.v1",
        }),
    }
    return event_type in allowed[kind]


def _kind(value: object) -> str:
    if value not in {"tool", "workspace", "mcp-allocation"}:
        raise ValueError("invalid run effect kind")
    return str(value)


def _digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError("invalid run effect identity digest")
    return value


__all__ = [
    "RunEffectIndex", "RunEffectInspection", "RunEffectRef",
    "effect_identity_digest",
]
