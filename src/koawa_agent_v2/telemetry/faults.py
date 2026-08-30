"""I8 production fault-port contract and the single stable point registry."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from types import MappingProxyType
from typing import Mapping, Protocol, TypeAlias
from uuid import UUID


JsonPrimitive: TypeAlias = str | int | float | bool | None
MAX_FAULT_FACTS = 24
MAX_FAULT_FACT_BYTES = 4096


def _error(code: str):
    # Fault names can be imported by control/schema without initializing the
    # agent runtime, trace store or recovery subsystem.
    from ..agents.graph import AgentError
    return AgentError(code)


class FaultPointClass(StrEnum):
    BEFORE_APPEND = "before_append"
    AFTER_COMMIT = "after_commit"
    AFTER_READ = "after_read"
    IN_TRANSACTION = "in_transaction"
    EXTERNAL_ENTERED = "external_entered"
    EXTERNAL_RETURNED = "external_returned"
    EXTERNAL_AFTER_SEND = "external_after_send"
    EXTERNAL_AFTER_EFFECT = "external_after_effect"
    EXTERNAL_PARTIAL = "external_partial"
    BEFORE_EXTERNAL_COMMIT = "before_external_commit"
    SYNTHETIC_CONFLICT = "synthetic_conflict"
    DIAGNOSTIC_FAILURE = "diagnostic_failure"


@dataclass(frozen=True, slots=True)
class FaultEventDelta:
    counters: Mapping[str, int]
    stream_categories: tuple[str, ...] = ()
    per_fact: Mapping[str, int] = field(default_factory=dict)
    variants: Mapping[str, Mapping[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        counters = dict(self.counters)
        per_fact = dict(self.per_fact)
        variants = {key: MappingProxyType(dict(value)) for key, value in self.variants.items()}
        if "durable_events" not in counters:
            raise ValueError("durable_events counter is required")
        for document in (counters, per_fact, *variants.values()):
            if any(
                not isinstance(key, str) or not key
                or type(value) is not int or value < 0
                for key, value in document.items()
            ):
                raise ValueError("fault event counters must be non-negative integers")
        categories = tuple(self.stream_categories)
        if len(categories) != len(set(categories)) or any(
            not isinstance(value, str) or not value for value in categories
        ):
            raise ValueError("stream categories must be a unique tuple")
        object.__setattr__(self, "counters", MappingProxyType(counters))
        object.__setattr__(self, "stream_categories", categories)
        object.__setattr__(self, "per_fact", MappingProxyType(per_fact))
        object.__setattr__(self, "variants", MappingProxyType(variants))


@dataclass(frozen=True, slots=True)
class FaultSpec:
    name: str
    point_class: FaultPointClass
    marker_timing: str
    expected_event_delta: FaultEventDelta
    recovery_oracle: str

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.lower():
            raise ValueError("fault name must be stable lowercase text")
        if not isinstance(self.point_class, FaultPointClass):
            raise TypeError("point_class must be FaultPointClass")
        if not self.marker_timing or not self.recovery_oracle:
            raise ValueError("fault spec text fields must be non-empty")
        if not isinstance(self.expected_event_delta, FaultEventDelta):
            raise TypeError("expected_event_delta must be FaultEventDelta")


class FaultPort(Protocol):
    def hit(self, point: str, facts: Mapping[str, JsonPrimitive]) -> None: ...


class NoOpFaultPort:
    def hit(self, point: str, facts: Mapping[str, JsonPrimitive]) -> None:
        require_fault_point(point)
        validate_fault_facts(facts)


class InjectedFault(RuntimeError):
    def __init__(self, point: str) -> None:
        self.point = point
        super().__init__("injected_fault:" + point)


@dataclass(slots=True)
class RecordingFaultPort:
    raise_at: frozenset[str] = frozenset()
    hits: list[tuple[str, Mapping[str, JsonPrimitive]]] = field(default_factory=list)

    def __post_init__(self):
        self.raise_at = frozenset(self.raise_at)
        for point in self.raise_at:
            require_fault_point(point)

    def hit(self, point: str, facts: Mapping[str, JsonPrimitive]) -> None:
        require_fault_point(point)
        frozen = validate_fault_facts(facts)
        self.hits.append((point, frozen))
        if point in self.raise_at:
            raise InjectedFault(point)


_LEGACY_FAILURE_POINTS = frozenset({
    "bad_sse", "terminal_after_event", "checkpoint_write", "ledger_claim",
    "db_version_conflict", "mcp_eof", "mcp_timeout", "container_kill",
    "approval_loss", "approval_tamper", "subagent_orphan", "worktree_conflict",
})


def _class(name: str) -> FaultPointClass:
    # No substring/prefix inference: similar spellings can denote different
    # observable boundaries (notably after_cache_commit/after_terminal_commit).
    return _EXACT_POINT_CLASSES[name]


_CLASS_GROUPS = {
    FaultPointClass.BEFORE_APPEND: """
        d11.enqueue.before_append d11.deliver.before_append d11.result.before_append
        d11.ack.before_append d11.resume.before_append d11.cancel.before_append
        d11.resources.baseline.before_append d11.spawn.before_append
        d11.heartbeat.before_append d11.orphan.before_append d11.takeover.before_append
        d11.terminal.before_append s3.event.after_validate_before_begin
        s4.activation.before_grant_append s5.run.before_terminal_append
    """,
    FaultPointClass.AFTER_COMMIT: """
        d11.enqueue.after_commit d11.deliver.after_commit d11.result.after_commit
        d11.ack.after_commit d11.unresolved.after_commit d11.waiting.after_commit
        d11.resume.after_commit d11.cancel.after_commit d11.resources.baseline.after_commit
        d11.spawn.after_commit d11.heartbeat.after_commit d11.orphan.after_commit
        d11.takeover.after_commit d11.terminal.after_commit s3.checkpoint.after_cache_commit
        s4.activation.after_request_commit s4.activation.after_grant_commit
        s4.allocation.after_intent_commit s4.allocation.after_claim_commit
        s4.allocation.after_started_commit s4.allocation.after_ready_commit
        s4.mcp.list.after_catalog_commit s5.run.after_terminal_commit
        s5.workspace.add.after_intent_commit s5.workspace.add.after_claim_commit
        s5.workspace.remove.after_intent_commit s5.workspace.remove.after_claim_commit
        s5.workspace.apply.after_intent_commit s5.workspace.apply.after_claim_commit
        s5.workspace.retest.after_intent_commit s5.workspace.retest.after_claim_commit
        s5.workspace.deliver.after_intent_commit s5.workspace.deliver.after_claim_commit
    """,
    FaultPointClass.AFTER_READ: """
        d11.spawn.after_read d11.terminal.after_read s3.checkpoint.after_source_read
        s3.checkpoint.after_verify_before_tail s3.export.after_source_scan
        s4.mcp.refresh.after_list_before_publish
    """,
    FaultPointClass.IN_TRANSACTION: """
        s3.event.mid_batch_before_receipt s3.checkpoint.before_cache_commit
        s3.migration.after_ddl s3.migration.before_user_version
        s3.migration.after_user_version_before_commit
    """,
    FaultPointClass.EXTERNAL_ENTERED: "d11.provider.entered",
    FaultPointClass.EXTERNAL_RETURNED: """
        d11.provider.returned s4.mcp.initialize.after_result_before_commit
        s4.mcp.list.after_page_before_cursor s4.mcp.call.after_result_before_ledger
    """,
    FaultPointClass.EXTERNAL_AFTER_SEND: """
        s4.mcp.initialize.after_send_before_result s4.mcp.call.after_send_before_result
    """,
    FaultPointClass.EXTERNAL_AFTER_EFFECT: """
        s4.launch.after_external_create_before_started s4.mcp.close.after_terminate_before_stopped
        s5.workspace.add.after_effect_before_ack s5.workspace.remove.after_effect_before_ack
        s5.workspace.apply.after_effect_before_ack s5.workspace.retest.after_effect_before_ack
        s5.workspace.deliver.after_effect_before_ack
    """,
    FaultPointClass.EXTERNAL_PARTIAL: "s3.export.mid_destination_import",
    FaultPointClass.BEFORE_EXTERNAL_COMMIT: "s3.export.after_verify_before_rename",
    FaultPointClass.SYNTHETIC_CONFLICT: "s5.trace.cas_conflict",
    FaultPointClass.DIAGNOSTIC_FAILURE: "s5.trace.drop",
}
_EXACT_POINT_CLASSES = {
    name: point_class
    for point_class, names in _CLASS_GROUPS.items()
    for name in names.split()
}
if len(_EXACT_POINT_CLASSES) != sum(len(names.split()) for names in _CLASS_GROUPS.values()):
    raise RuntimeError("duplicate fault point class declaration")


_POINTS: tuple[tuple[str, int, str], ...] = (
    ("d11.enqueue.before_append", 0, "safe_retry"),
    ("d11.enqueue.after_commit", 1, "receipt_replay"),
    ("d11.deliver.before_append", 0, "safe_retry"),
    ("d11.deliver.after_commit", 1, "same_delivery"),
    ("d11.provider.entered", 0, "provider_uncertainty"),
    ("d11.provider.returned", 0, "result_unresolved"),
    ("d11.result.before_append", 0, "provider_evidence_or_unresolved"),
    ("d11.result.after_commit", 1, "ack_only"),
    ("d11.ack.before_append", 0, "ack_retry"),
    ("d11.ack.after_commit", 1, "receipt_replay"),
    ("d11.unresolved.after_commit", 1, "waiting"),
    ("d11.waiting.after_commit", 1, "operator_resolution"),
    ("d11.resume.before_append", 0, "stay_waiting"),
    ("d11.resume.after_commit", 1, "same_fresh_run"),
    ("d11.cancel.before_append", 0, "decision_retry"),
    ("d11.cancel.after_commit", 1, "receipt_replay"),
    ("d11.resources.baseline.before_append", 0, "recompute_snapshot"),
    ("d11.resources.baseline.after_commit", 1, "receipt_replay"),
    ("d11.spawn.after_read", 0, "full_reread"),
    ("d11.spawn.before_append", 0, "full_retry"),
    ("d11.spawn.after_commit", 4, "receipt_replay"),
    ("d11.heartbeat.before_append", 0, "same_run_retry"),
    ("d11.heartbeat.after_commit", 1, "receipt_replay"),
    ("d11.orphan.before_append", 0, "rediscover"),
    ("d11.orphan.after_commit", 1, "takeover_race"),
    ("d11.takeover.before_append", 0, "full_reread"),
    ("d11.takeover.after_commit", 1, "same_run_receipt"),
    ("d11.terminal.after_read", 0, "full_reread"),
    ("d11.terminal.before_append", 0, "full_retry"),
    ("d11.terminal.after_commit", 4, "receipt_replay"),
    ("s3.event.after_validate_before_begin", 0, "retry"),
    ("s3.event.mid_batch_before_receipt", 0, "transaction_rollback"),
    ("s3.checkpoint.after_source_read", 0, "recompute"),
    ("s3.checkpoint.before_cache_commit", 0, "full_replay"),
    ("s3.checkpoint.after_cache_commit", 0, "cache_discardable"),
    ("s3.checkpoint.after_verify_before_tail", 0, "full_verify"),
    ("s3.migration.after_ddl", 0, "old_or_new_atomic"),
    ("s3.migration.before_user_version", 0, "old_or_new_atomic"),
    ("s3.migration.after_user_version_before_commit", 0, "old_or_new_atomic"),
    ("s3.export.after_source_scan", 0, "source_unchanged"),
    ("s3.export.mid_destination_import", 0, "discard_partial"),
    ("s3.export.after_verify_before_rename", 0, "atomic_rename_retry"),
    ("s4.activation.after_request_commit", 1, "show_pending"),
    ("s4.activation.before_grant_append", 0, "resolve_retry"),
    ("s4.activation.after_grant_commit", 1, "receipt_replay"),
    ("s4.allocation.after_intent_commit", 1, "claim_or_stop"),
    ("s4.allocation.after_claim_commit", 1, "inspect_external_identity"),
    ("s4.launch.after_external_create_before_started", 0, "observed_or_unknown"),
    ("s4.allocation.after_started_commit", 1, "reap_exact_identity"),
    ("s4.allocation.after_ready_commit", 1, "normal_close_or_reap"),
    ("s4.mcp.initialize.after_send_before_result", 0, "unknown_and_close"),
    ("s4.mcp.initialize.after_result_before_commit", 0, "safe_session_rebuild"),
    ("s4.mcp.list.after_page_before_cursor", 0, "restart_bounded_list"),
    ("s4.mcp.list.after_catalog_commit", 0, "use_snapshot"),
    ("s4.mcp.call.after_send_before_result", 0, "ledger_unknown"),
    ("s4.mcp.call.after_result_before_ledger", 0, "reconcile_or_unknown"),
    ("s4.mcp.refresh.after_list_before_publish", 0, "discard"),
    ("s4.mcp.close.after_terminate_before_stopped", 0, "inspect_reap_or_unknown"),
    ("s5.run.before_terminal_append", 0, "full_reread"),
    ("s5.run.after_terminal_commit", 3, "receipt_replay"),
    ("s5.workspace.add.after_intent_commit", 2, "claim"),
    ("s5.workspace.add.after_claim_commit", 1, "inspect_path"),
    ("s5.workspace.add.after_effect_before_ack", 0, "postcondition_or_unknown"),
    ("s5.workspace.remove.after_intent_commit", 2, "claim"),
    ("s5.workspace.remove.after_claim_commit", 1, "inspect_path_metadata"),
    ("s5.workspace.remove.after_effect_before_ack", 0, "postcondition_or_unknown"),
    ("s5.workspace.apply.after_intent_commit", 2, "claim"),
    ("s5.workspace.apply.after_claim_commit", 1, "verify_prestate"),
    ("s5.workspace.apply.after_effect_before_ack", 0, "postcondition_or_unknown"),
    ("s5.workspace.retest.after_intent_commit", 2, "claim"),
    ("s5.workspace.retest.after_claim_commit", 1, "test_identity"),
    ("s5.workspace.retest.after_effect_before_ack", 0, "positive_or_negative_evidence"),
    ("s5.workspace.deliver.after_intent_commit", 2, "claim"),
    ("s5.workspace.deliver.after_claim_commit", 1, "verify_repo_prestate"),
    ("s5.workspace.deliver.after_effect_before_ack", 0, "postcondition_or_unknown"),
    ("s5.trace.cas_conflict", 0, "bounded_trace_retry"),
    ("s5.trace.drop", 0, "business_unchanged"),
)

_STREAM_CATEGORIES: dict[str, tuple[str, ...]] = {}
for _name in (
    "d11.enqueue.after_commit", "d11.deliver.after_commit",
    "d11.result.after_commit", "d11.ack.after_commit",
    "d11.unresolved.after_commit",
):
    _STREAM_CATEGORIES[_name] = ("mailbox",)
for _name in (
    "d11.waiting.after_commit", "d11.resume.after_commit",
    "d11.cancel.after_commit", "d11.heartbeat.after_commit",
    "d11.orphan.after_commit",
):
    _STREAM_CATEGORIES[_name] = ("agent",)
_STREAM_CATEGORIES.update({
    "d11.resources.baseline.after_commit": ("agent-capacity",),
    "d11.spawn.after_commit": ("agent", "agent-capacity", "agent-budget"),
    "d11.takeover.after_commit": ("agent", "mailbox"),
    "d11.terminal.after_commit": (
        "agent", "agent-capacity", "agent-budget", "mailbox",
    ),
    "s4.activation.after_request_commit": ("mcp-activation",),
    "s4.activation.after_grant_commit": ("mcp-activation",),
    "s4.allocation.after_intent_commit": ("mcp-allocation",),
    "s4.allocation.after_claim_commit": ("mcp-allocation",),
    "s4.allocation.after_started_commit": ("mcp-allocation",),
    "s4.allocation.after_ready_commit": ("mcp-allocation",),
    "s5.run.after_terminal_commit": ("thread", "turn", "run"),
})
for _kind in ("add", "remove", "apply", "retest", "deliver"):
    _STREAM_CATEGORIES[f"s5.workspace.{_kind}.after_intent_commit"] = (
        "workspace-effect", "run-effect-index",
    )
    _STREAM_CATEGORIES[f"s5.workspace.{_kind}.after_claim_commit"] = (
        "workspace-effect",
    )

_PER_FACT_DELTAS = {
    "d11.takeover.after_commit": {"message_count": 1},
}
_DELTA_VARIANTS = {
    "d11.spawn.after_commit": {
        "root_spawn_durable_events": 1,
        "child_spawn_durable_events": 4,
    },
    "d11.terminal.after_commit": {
        "root_terminal_durable_events": 1,
        "child_parent_terminal_durable_events": 3,
        "child_parent_active_durable_events": 4,
    },
}


def _event_delta(name: str, durable_events: int) -> FaultEventDelta:
    counters = {"durable_events": durable_events}
    if name == "s4.mcp.list.after_catalog_commit":
        counters["catalog_snapshots"] = 1
    if name == "s3.checkpoint.after_cache_commit":
        counters["projection_rows"] = 1
    return FaultEventDelta(
        counters,
        _STREAM_CATEGORIES.get(name, ()),
        _PER_FACT_DELTAS.get(name, {}),
        {key: {"durable_events": value} for key, value in _DELTA_VARIANTS.get(name, {}).items()},
    )


FAULT_REGISTRY: tuple[FaultSpec, ...] = tuple(
    FaultSpec(
        name, _class(name), _class(name).value,
        _event_delta(name, delta), oracle,
    )
    for name, delta, oracle in _POINTS
)
FAULT_SPECS = MappingProxyType({spec.name: spec for spec in FAULT_REGISTRY})
if set(FAULT_SPECS) != set(_EXACT_POINT_CLASSES):
    raise RuntimeError("fault point class registry mismatch")
FaultPoint = StrEnum("FaultPoint", {
    spec.name.upper().replace(".", "_"): spec.name for spec in FAULT_REGISTRY
}, module=__name__)
FAILURE_POINTS = frozenset(FAULT_SPECS) | _LEGACY_FAILURE_POINTS
NO_OP_FAULT_PORT: FaultPort = NoOpFaultPort()
_ACTIVE_FAULT_PORT: ContextVar[FaultPort] = ContextVar("koawa_fault_port", default=NO_OP_FAULT_PORT)


@contextmanager
def using_fault_port(port: FaultPort):
    """Explicit test-only injection; no config/env switch can enable faults."""
    if not callable(getattr(port, "hit", None)):
        raise TypeError("fault port must implement hit")
    token = _ACTIVE_FAULT_PORT.set(port)
    try:
        yield port
    finally:
        _ACTIVE_FAULT_PORT.reset(token)


def emit_fault(point: str, facts: Mapping[str, JsonPrimitive], *, port=None) -> None:
    require_fault_point(point)
    frozen = validate_fault_facts(facts)
    (port if port is not None else _ACTIVE_FAULT_PORT.get()).hit(point, frozen)


def require_fault_point(point: str) -> FaultSpec:
    try:
        return FAULT_SPECS[point]
    except (KeyError, TypeError):
        raise _error("unknown_failure_point") from None


_UUID_FACTS = frozenset({"agent_id", "parent_agent_id", "root_agent_id", "run_id", "turn_id",
                       "correlation_id", "effect_id", "allocation_id", "message_id", "reservation_id"})
_COUNTER_FACTS = frozenset({"attempt", "delivery_attempt", "claim_epoch", "message_count", "index", "count",
                            "page", "tool_count", "generation"})
_VERSION_FACTS = frozenset({"version", "stream_version", "turn_version", "expected_version"})
_CODE_FACTS = frozenset({"kind", "phase", "code"})
_FACT_KEYS = _UUID_FACTS | _COUNTER_FACTS | _VERSION_FACTS | _CODE_FACTS | {
    "request_id", "server_id", "is_error", "message_ids_digest",
}


@lru_cache(maxsize=1)
def _fact_profile():
    from ..control.durable_json import DurableJsonLimits
    return DurableJsonLimits(max_utf8_bytes=MAX_FAULT_FACT_BYTES, max_depth=2,
                             max_nodes=MAX_FAULT_FACTS + 1, max_string_utf8_bytes=512,
                             max_object_members=MAX_FAULT_FACTS, max_array_items=1,
                             max_key_utf8_bytes=64)


def _uuid_fact(value) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def validate_fault_facts(
    facts: Mapping[str, JsonPrimitive],
) -> Mapping[str, JsonPrimitive]:
    from ..control.durable_json import DurableJsonError, canonical_json_bytes_v1
    if not isinstance(facts, Mapping) or len(facts) > MAX_FAULT_FACTS:
        raise _error("invalid_fault_facts")
    copied = dict(facts)
    try:
        canonical_json_bytes_v1(copied, _fact_profile(), path="fault-facts")
        for key, value in copied.items():
            valid = False
            if key in _UUID_FACTS:
                valid = _uuid_fact(value)
            elif key in _COUNTER_FACTS | _VERSION_FACTS:
                minimum = -1 if key in _VERSION_FACTS else 0
                valid = type(value) is int and minimum <= value <= 2**63 - 1
            elif key == "request_id":
                valid = (type(value) is int and 0 <= value <= 2**63 - 1) or (value is not None and _uuid_fact(value))
            elif key in _CODE_FACTS:
                valid = isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_.:-]{0,127}", value) is not None
            elif key == "server_id":
                valid = isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is not None
            elif key == "is_error":
                valid = type(value) is bool
            elif key == "message_ids_digest":
                valid = isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None
            if key not in _FACT_KEYS or not valid:
                raise _error("invalid_fault_facts")
    except (DurableJsonError, ValueError, UnicodeError, TypeError):
        raise _error("invalid_fault_facts") from None
    return MappingProxyType(copied)


class LegacyFaultAdapter:
    """Validate old D11 metadata; new ports receive only bounded primitives."""
    def __init__(self, callback, port=None):
        if not callable(callback) or (port is not None and not callable(getattr(port, "hit", None))):
            raise TypeError("invalid fault injection port")
        self.callback, self.port = callback, port

    def __call__(self, point, facts):
        if not isinstance(facts, Mapping) or len(facts) > MAX_FAULT_FACTS:
            raise _error("invalid_fault_facts")
        primitive = dict(facts)
        ids = primitive.pop("message_ids", None)
        if ids is not None:
            if not isinstance(ids, (tuple, list)) or any(value is None or not _uuid_fact(value) for value in ids):
                raise _error("invalid_fault_facts")
            primitive["message_count"] = len(ids)
            digest = hashlib.sha256()
            for value in ids:
                digest.update(value.encode("ascii") + b"\n")
            primitive["message_ids_digest"] = digest.hexdigest()
        emit_fault(point, primitive, port=self.port)
        # Kept only for pre-I8 callers; the typed port never receives this list.
        self.callback(point, facts)


def adapt_fault_callback(callback, port=None):
    if isinstance(callback, LegacyFaultAdapter) and port is None:
        return callback
    return LegacyFaultAdapter(callback, port)


@dataclass(frozen=True, slots=True)
class FaultInjector:
    """D14 deterministic selector retained for compatibility and evals."""

    seed: str
    script: tuple[str, ...] = ()
    triggered: list[str] = field(default_factory=list, compare=False)

    def should_fail(self, point: str) -> bool:
        if point not in FAILURE_POINTS:
            raise _error("unknown_failure_point")
        enabled = point in self.script if self.script else int(
            hashlib.sha256(f"{self.seed}:{point}".encode()).hexdigest()[:2], 16
        ) % 5 == 0
        if enabled:
            self.triggered.append(point)
        return enabled

    def to_document(self) -> dict:
        return {"seed": self.seed, "script": list(self.script),
                "triggered": list(self.triggered)}


def classify_failure(code: str) -> str:
    if "timeout" in code or "slow" in code:
        return "timeout"
    if "conflict" in code or "version" in code or "drift" in code:
        return "concurrency"
    if "denied" in code or "forbidden" in code or "required" in code:
        return "policy"
    if "unknown" in code or "orphan" in code or "crash" in code:
        return "uncertainty"
    if "malformed" in code or "invalid" in code:
        return "contract"
    return "other"


__all__ = [
    "FAILURE_POINTS", "FAULT_REGISTRY", "FAULT_SPECS", "FaultInjector",
    "FaultEventDelta", "FaultPointClass", "FaultPort", "FaultSpec", "InjectedFault",
    "JsonPrimitive", "NO_OP_FAULT_PORT", "NoOpFaultPort",
    "RecordingFaultPort", "classify_failure", "require_fault_point",
    "validate_fault_facts",
    "FaultPoint", "adapt_fault_callback", "emit_fault", "using_fault_port",
]
