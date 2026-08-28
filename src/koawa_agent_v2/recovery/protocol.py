"""D6 checkpoint v2 wire contract (section 7.4).

Checkpoints are verified projections, never truth: every cached checkpoint is
re-derived from the covered event segment by the canonical reducer before it
is trusted.  The single identity serializer is
control.durable_json.canonical_json_bytes_v1; no checkpoint-specific
serializer exists anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from ..control.durable_json import (
    CHECKPOINT_READ_V2,
    DurableJsonError,
    canonical_json_bytes_v1,
    strict_json_loads_bytes,
    strict_json_loads_text,
)
from ..control.event_store import StoredEvent


class RunPhase(StrEnum):
    READY_FOR_MODEL = "ready_for_model"
    READY_FOR_TOOL = "ready_for_tool"
    TOOL_IN_PROGRESS = "tool_in_progress"
    READY_TO_FINALIZE = "ready_to_finalize"
    BLOCKED_UNCERTAIN_SIDE_EFFECT = "blocked_uncertain_side_effect"


class CheckpointError(RuntimeError):
    """Content-free checkpoint failure carrying a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else code)


LIVE_RUN_TURN_EVENT_TYPES = frozenset(
    {
        "turn.started.v1",
        "turn.recovery-lease-claimed.v1",
        "turn.recovery-lease-heartbeated.v1",
        "turn.recovery-lease-released.v1",
    }
)


REDUCER_NAME = "run-execution"
REDUCER_VERSION = 2

KOAWA_WIRE_NAMESPACE = uuid5(NAMESPACE_URL, "https://koawa-agent.dev/wire/v2")
CHECKPOINT_ID_NAMESPACE = uuid5(KOAWA_WIRE_NAMESPACE, "checkpoint-id")

PROJECTION_KEYS = frozenset(
    {
        "context",
        "final_text",
        "input_tokens",
        "last_run_id",
        "model_round",
        "output_chars",
        "output_tokens",
        "pending_tool_calls",
        "phase",
        "tool_count",
    }
)

MAX_COUNTER = (1 << 63) - 1
_COUNTER_NAMES = (
    "model_round",
    "tool_count",
    "output_chars",
    "input_tokens",
    "output_tokens",
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# ---------------------------------------------------------------------------
# identity helpers
# ---------------------------------------------------------------------------


def _as_uuid_value(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _require_uuid_text(value: Any, name: str) -> str:
    raw = str(value).lower()
    if _UUID_RE.fullmatch(raw) is None:
        raise CheckpointError("checkpoint_invalid_uuid", name)
    return raw


def _require_counter(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CheckpointError("checkpoint_invalid_counter", name)
    if value < 0 or value > MAX_COUNTER:
        raise CheckpointError("checkpoint_invalid_counter", name)
    return value


def _require_hex64(value: Any, name: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise CheckpointError("checkpoint_invalid_hash", name)
    return value


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CheckpointError("checkpoint_invalid_time", "")
    try:
        parsed = (
            datetime.fromisoformat(value)
            if value.endswith("Z")
            else datetime.fromisoformat(value)
        )
    except ValueError as exc:
        raise CheckpointError("checkpoint_invalid_time", "") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CheckpointError("checkpoint_invalid_time", "")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def stored_event_hash_v2_from_document(document: Mapping[str, Any]) -> str:
    """SHA-256 (lower hex) of the canonical stored-event v2 document."""
    import hashlib

    return hashlib.sha256(
        canonical_json_bytes_v1(dict(document), path="stored-event")
    ).hexdigest()


def stored_event_hash_v2(event: StoredEvent) -> str:
    """Build the exact stored-event v2 document for a committed event."""
    document = {
        "domain": "koawa.stored-event.v2",
        "event_id": str(event.event_id),
        "stream": {
            "category": event.stream_id.category,
            "aggregate_id": str(event.stream_id.aggregate_id),
        },
        "stream_version": event.stream_version,
        "global_position": event.global_position,
        "commit": {
            "id": str(event.commit_id),
            "index": event.commit_index,
            "size": event.commit_size,
        },
        "event_type": event.event_type,
        "schema_version": event.schema_version,
        "occurred_at": _utc_text(event.occurred_at),
        "payload": event.payload,
        "metadata": {
            "command_id": str(event.metadata.command_id),
            "correlation_id": str(event.metadata.correlation_id),
            "causation_id": (
                None
                if event.metadata.causation_id is None
                else str(event.metadata.causation_id)
            ),
            "thread_id": (
                None if event.metadata.thread_id is None else str(event.metadata.thread_id)
            ),
            "turn_id": (
                None if event.metadata.turn_id is None else str(event.metadata.turn_id)
            ),
            "run_id": (
                None if event.metadata.run_id is None else str(event.metadata.run_id)
            ),
            "actor": event.metadata.actor,
        },
    }
    return stored_event_hash_v2_from_document(document)


def checkpoint_identity_document(
    *,
    reducer_name: str,
    reducer_version: int,
    source_category: str,
    source_aggregate_id: UUID,
    covered_stream_version: int,
    covered_event_id: UUID,
    covered_global_position: int,
    covered_commit_id: UUID,
    covered_event_hash: str,
    projection_digest: str,
) -> dict[str, Any]:
    """Exact checkpoint identity document (section 7.4)."""
    return {
        "domain": "koawa.checkpoint-id.v2",
        "reducer": {"name": reducer_name, "version": reducer_version},
        "source": {
            "category": source_category,
            "aggregate_id": _require_uuid_text(source_aggregate_id, "source_aggregate_id"),
            "covered_stream_version": _require_counter(
                covered_stream_version, "covered_stream_version"
            ),
            "covered_event_id": _require_uuid_text(covered_event_id, "covered_event_id"),
            "covered_global_position": _require_counter(
                covered_global_position, "covered_global_position"
            ),
            "covered_commit_id": _require_uuid_text(covered_commit_id, "covered_commit_id"),
            "covered_event_hash": _require_hex64(covered_event_hash, "covered_event_hash"),
        },
        "projection_digest": _require_hex64(projection_digest, "projection_digest"),
    }


def checkpoint_id_for_identity(identity: Mapping[str, Any]) -> UUID:
    """checkpoint_id = UUIDv5(CHECKPOINT_ID_NAMESPACE, canonical identity bytes)."""
    return uuid5(
        CHECKPOINT_ID_NAMESPACE,
        canonical_json_bytes_v1(dict(identity), path="checkpoint-identity").decode(
            "utf-8"
        ),
    )


# ---------------------------------------------------------------------------
# checkpoint v2 wire
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Validated v2 checkpoint: reducer + source + fence + projection digest.

    The projection mapping is the canonical 10-key projection document.  The
    checkpoint_id must equal the UUIDv5 of the identity document; created_at
    and previous do not participate in the identity.
    """

    checkpoint_id: UUID
    reducer_name: str
    reducer_version: int
    source_category: str
    source_aggregate_id: UUID
    covered_stream_version: int
    covered_event_id: UUID
    covered_global_position: int
    covered_commit_id: UUID
    covered_event_hash: str
    thread_id: UUID
    turn_id: UUID
    run_id: UUID
    turn_stream_version: int
    projection: Mapping[str, Any]
    projection_digest: str
    previous: None
    created_at: datetime

    @classmethod
    def build(
        cls,
        *,
        reducer_name: str = "run-execution",
        reducer_version: int = 2,
        source_category: str,
        source_aggregate_id: UUID,
        covered_stream_version: int,
        covered_event_id: UUID,
        covered_global_position: int,
        covered_commit_id: UUID,
        covered_event_hash: str,
        thread_id: UUID,
        turn_id: UUID,
        run_id: UUID,
        turn_stream_version: int,
        projection: Mapping[str, Any],
        projection_digest: str,
        created_at: datetime,
    ) -> "Checkpoint":
        identity = checkpoint_identity_document(
            reducer_name=reducer_name,
            reducer_version=reducer_version,
            source_category=source_category,
            source_aggregate_id=source_aggregate_id,
            covered_stream_version=covered_stream_version,
            covered_event_id=covered_event_id,
            covered_global_position=covered_global_position,
            covered_commit_id=covered_commit_id,
            covered_event_hash=covered_event_hash,
            projection_digest=projection_digest,
        )
        return cls(
            checkpoint_id=checkpoint_id_for_identity(identity),
            reducer_name=reducer_name,
            reducer_version=reducer_version,
            source_category=source_category,
            source_aggregate_id=_as_uuid_value(source_aggregate_id),
            covered_stream_version=covered_stream_version,
            covered_event_id=_as_uuid_value(covered_event_id),
            covered_global_position=covered_global_position,
            covered_commit_id=_as_uuid_value(covered_commit_id),
            covered_event_hash=covered_event_hash,
            thread_id=_as_uuid_value(thread_id),
            turn_id=_as_uuid_value(turn_id),
            run_id=_as_uuid_value(run_id),
            turn_stream_version=turn_stream_version,
            projection=dict(projection),
            projection_digest=projection_digest,
            previous=None,
            created_at=created_at,
        )

    def document(self) -> dict[str, Any]:
        """Exact canonical wire document of this checkpoint."""
        return {
            "checkpoint_schema_version": 2,
            "checkpoint_id": str(self.checkpoint_id),
            "reducer": {
                "name": self.reducer_name,
                "version": self.reducer_version,
            },
            "source": {
                "stream": {
                    "category": self.source_category,
                    "aggregate_id": str(self.source_aggregate_id),
                },
                "covered_stream_version": self.covered_stream_version,
                "covered_event_id": str(self.covered_event_id),
                "covered_global_position": self.covered_global_position,
                "covered_commit_id": str(self.covered_commit_id),
                "covered_event_hash": self.covered_event_hash,
            },
            "fence": {
                "thread_id": str(self.thread_id),
                "turn_id": str(self.turn_id),
                "run_id": str(self.run_id),
                "turn_stream_version": self.turn_stream_version,
            },
            "projection": dict(self.projection),
            "projection_digest": self.projection_digest,
            "previous": self.previous,
            "created_at": _utc_text(self.created_at),
        }

    def wire_bytes(self) -> bytes:
        """Canonical UTF-8 bytes of the wire document for the cache BLOB."""
        return canonical_json_bytes_v1(self.document(), path="checkpoint")

    @classmethod
    def parse(cls, value: bytes | str | bytearray) -> "Checkpoint":
        """Strict bounded parse + exact key set + identity re-computation.

        Unsupported schema versions (including v1) raise
        CheckpointError("checkpoint_schema_unsupported") so callers can treat
        them as cache misses.
        """
        try:
            if isinstance(value, bytes):
                document = strict_json_loads_bytes(
                    bytes(value)[: CHECKPOINT_READ_V2.max_utf8_bytes + 1],
                    CHECKPOINT_READ_V2,
                    path="checkpoint",
                )
            else:
                document = strict_json_loads_text(
                    value,
                    CHECKPOINT_READ_V2,
                    path="checkpoint",
                )
            return cls.from_document(document)
        except DurableJsonError as exc:
            raise CheckpointError("checkpoint_invalid_json", exc.code) from exc

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "Checkpoint":
        try:
            if document.get("checkpoint_schema_version") != 2:
                raise CheckpointError("checkpoint_schema_unsupported", "")
            expected_keys = {
                "checkpoint_schema_version",
                "checkpoint_id",
                "reducer",
                "source",
                "fence",
                "projection",
                "projection_digest",
                "previous",
                "created_at",
            }
            if set(document) != expected_keys:
                raise CheckpointError("checkpoint_invalid_document", "key set")
            reducer = dict(document["reducer"])
            if set(reducer) != {"name", "version"}:
                raise CheckpointError("checkpoint_invalid_document", "reducer")
            if not isinstance(reducer["name"], str) or not reducer["name"]:
                raise CheckpointError("checkpoint_invalid_document", "reducer name")
            reducer_version = _require_counter(reducer["version"], "reducer version")
            source = dict(document["source"])
            stream = dict(source["stream"])
            if set(source) != {
                "stream",
                "covered_stream_version",
                "covered_event_id",
                "covered_global_position",
                "covered_commit_id",
                "covered_event_hash",
            } or set(stream) != {"category", "aggregate_id"}:
                raise CheckpointError("checkpoint_invalid_document", "source")
            category = stream["category"]
            if not isinstance(category, str) or not category:
                raise CheckpointError("checkpoint_invalid_document", "category")
            fence = dict(document["fence"])
            if set(fence) != {
                "thread_id",
                "turn_id",
                "run_id",
                "turn_stream_version",
            }:
                raise CheckpointError("checkpoint_invalid_document", "fence")
            projection = dict(document["projection"])
            if set(projection) != PROJECTION_KEYS:
                raise CheckpointError("checkpoint_invalid_document", "projection")
            for name in _COUNTER_NAMES:
                _require_counter(projection[name], "projection." + name)
            phase = projection["phase"]
            if phase not in set(RunPhase):
                raise CheckpointError("checkpoint_invalid_document", "phase")
            final_text = projection["final_text"]
            if final_text is not None and not isinstance(final_text, str):
                raise CheckpointError("checkpoint_invalid_document", "final_text")
            projection_digest = _require_hex64(
                document["projection_digest"], "projection_digest"
            )
            identity = checkpoint_identity_document(
                reducer_name=reducer["name"],
                reducer_version=reducer_version,
                source_category=category,
                source_aggregate_id=UUID(_require_uuid_text(stream["aggregate_id"], "aggregate_id")),
                covered_stream_version=_require_counter(
                    source["covered_stream_version"], "covered_stream_version"
                ),
                covered_event_id=UUID(
                    _require_uuid_text(source["covered_event_id"], "covered_event_id")
                ),
                covered_global_position=_require_counter(
                    source["covered_global_position"], "covered_global_position"
                ),
                covered_commit_id=UUID(
                    _require_uuid_text(source["covered_commit_id"], "covered_commit_id")
                ),
                covered_event_hash=_require_hex64(
                    source["covered_event_hash"], "covered_event_hash"
                ),
                projection_digest=projection_digest,
            )
            expected_id = checkpoint_id_for_identity(identity)
            supplied_id = UUID(_require_uuid_text(document["checkpoint_id"], "checkpoint_id"))
            if supplied_id != expected_id:
                raise CheckpointError("checkpoint_identity_mismatch", "")
            if document.get("previous") is not None:
                raise CheckpointError("checkpoint_invalid_document", "previous")
            return cls(
                checkpoint_id=supplied_id,
                reducer_name=reducer["name"],
                reducer_version=reducer_version,
                source_category=category,
                source_aggregate_id=UUID(stream["aggregate_id"]),
                covered_stream_version=_require_counter(
                    source["covered_stream_version"], "covered_stream_version"
                ),
                covered_event_id=UUID(source["covered_event_id"]),
                covered_global_position=_require_counter(
                    source["covered_global_position"], "covered_global_position"
                ),
                covered_commit_id=UUID(source["covered_commit_id"]),
                covered_event_hash=_require_hex64(
                    source["covered_event_hash"], "covered_event_hash"
                ),
                thread_id=UUID(_require_uuid_text(fence["thread_id"], "thread_id")),
                turn_id=UUID(_require_uuid_text(fence["turn_id"], "turn_id")),
                run_id=UUID(_require_uuid_text(fence["run_id"], "run_id")),
                turn_stream_version=_require_counter(
                    fence["turn_stream_version"], "turn_stream_version"
                ),
                projection=projection,
                projection_digest=projection_digest,
                previous=None,
                created_at=_parse_utc(document["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointError("checkpoint_invalid_document", "") from exc