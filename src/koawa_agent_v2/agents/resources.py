"""I3 durable resource projections: parent capacity and root budget streams.

Section 5.2/5.3 of the stabilization implementation document: every non-root
spawn atomically reserves one slot on the parent capacity stream and one
slot on the root budget stream, sharing one reservation id.  Reserved and
released events use strict exact keys; duplicate reserve/release, release of
an unknown reservation and root/parent drift all corrupt the stream (they are
never hidden with max(value, 0)).  Legacy v1 events keep replaying:
budget.reserved.v1/budget.released.v1 build reservations whose id equals the
child agent id; capacity baselines carry exactly the same identity subset.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from .graph import AgentError
from ..control.event_store import StreamId


_RELEASE_STATES = frozenset({"completed", "failed", "cancelled"})


def capacity_stream(parent_agent_id: UUID) -> StreamId:
    """Stream that tracks how many of a parent's child slots are active."""

    return StreamId("agent-capacity", parent_agent_id)


def budget_stream(root_agent_id: UUID) -> StreamId:
    """Stream that tracks total active non-root agents under one root."""

    return StreamId("agent-budget", root_agent_id)


@dataclass(frozen=True, slots=True)
class ResourceReservation:
    """One active child slot: identical identity on capacity and budget."""

    reservation_id: UUID
    child_agent_id: UUID
    parent_agent_id: UUID
    root_agent_id: UUID

    def to_document(self) -> dict[str, str]:
        return {
            "reservation_id": str(self.reservation_id),
            "child_agent_id": str(self.child_agent_id),
            "parent_agent_id": str(self.parent_agent_id),
            "root_agent_id": str(self.root_agent_id),
        }


@dataclass(frozen=True, slots=True)
class ParentCapacity:
    """Projection of one agent-capacity-{parent} stream."""

    parent_agent_id: UUID
    version: int
    active_reservations: tuple[ResourceReservation, ...]

    def find(self, reservation_id: UUID) -> ResourceReservation | None:
        for reservation in self.active_reservations:
            if reservation.reservation_id == reservation_id:
                return reservation
        return None

    @property
    def active_count(self) -> int:
        return len(self.active_reservations)

    @property
    def exists(self) -> bool:
        """True when the capacity stream has at least one committed event."""

        return self.version >= 0


@dataclass(frozen=True, slots=True)
class RootAgentBudget:
    """Projection of one agent-budget-{root} stream."""

    root_agent_id: UUID
    version: int
    active_reservations: tuple[ResourceReservation, ...]

    def find(self, reservation_id: UUID) -> ResourceReservation | None:
        for reservation in self.active_reservations:
            if reservation.reservation_id == reservation_id:
                return reservation
        return None

    @property
    def active_count(self) -> int:
        return len(self.active_reservations)


def rebuild_capacity(
    parent_agent_id: UUID, events: Sequence
) -> ParentCapacity:
    """Replay an agent-capacity stream into its active reservation set.

    The first event must be either agent.capacity-baseline-imported.v1 (legacy
    parent) or agent.capacity-reserved.v1.  Duplicate reserve, release of an
    unknown id, parent/root drift and any unknown event all fail closed as
    agent_capacity_projection_corrupt; nothing is clamped to zero.
    """

    reservations: dict[UUID, ResourceReservation] = {}
    baseline_seen = False
    for index, event in enumerate(events):
        payload = event.payload
        event_type = event.event_type
        if event_type == "agent.capacity-baseline-imported.v1":
            if index != 0 or event.stream_version != 0 or baseline_seen:
                raise AgentError("agent_capacity_projection_corrupt")
            _exact_keys(
                payload,
                {"parent_agent_id", "reservations", "source_global_position", "source_digest"},
            )
            if payload["parent_agent_id"] != str(parent_agent_id):
                raise AgentError("agent_capacity_projection_corrupt")
            items = payload["reservations"]
            if not isinstance(items, (list, tuple)):
                raise AgentError("agent_capacity_projection_corrupt")
            for item in items:
                _exact_keys(item, {"reservation_id", "child_agent_id", "root_agent_id"})
                rid = _uuid(item, "reservation_id")
                child = _uuid(item, "child_agent_id")
                root = _uuid(item, "root_agent_id")
                if rid != child:
                    # legacy baseline reservations are pinned to the child id
                    raise AgentError("agent_capacity_projection_corrupt")
                if rid in reservations:
                    raise AgentError("agent_capacity_projection_corrupt")
                reservations[rid] = ResourceReservation(rid, child, parent_agent_id, root)
            baseline_seen = True
        elif event_type == "agent.capacity-reserved.v1":
            _exact_keys(
                payload,
                {"parent_agent_id", "root_agent_id", "child_agent_id", "reservation_id"},
            )
            if payload["parent_agent_id"] != str(parent_agent_id):
                raise AgentError("agent_capacity_projection_corrupt")
            rid = _uuid(payload, "reservation_id")
            if rid in reservations:
                raise AgentError("agent_capacity_projection_corrupt")
            child = _uuid(payload, "child_agent_id")
            root = _uuid(payload, "root_agent_id")
            reservations[rid] = ResourceReservation(rid, child, parent_agent_id, root)
        elif event_type == "agent.capacity-released.v1":
            _exact_keys(
                payload,
                {
                    "parent_agent_id",
                    "root_agent_id",
                    "child_agent_id",
                    "reservation_id",
                    "terminal_state",
                    "terminal_run_id",
                    "released_at",
                },
            )
            if payload["parent_agent_id"] != str(parent_agent_id):
                raise AgentError("agent_capacity_projection_corrupt")
            rid = _uuid(payload, "reservation_id")
            active = reservations.get(rid)
            if active is None:
                raise AgentError("agent_capacity_projection_corrupt")
            if (
                payload["child_agent_id"] != str(active.child_agent_id)
                or payload["root_agent_id"] != str(active.root_agent_id)
            ):
                raise AgentError("agent_capacity_projection_corrupt")
            del reservations[rid]
        else:
            raise AgentError("agent_capacity_projection_corrupt")
    version = events[-1].stream_version if events else -1
    return ParentCapacity(
        parent_agent_id,
        version,
        tuple(
            sorted(reservations.values(), key=lambda item: str(item.reservation_id))
        ),
    )


def rebuild_budget(
    root_agent_id: UUID,
    events: Sequence,
    *,
    resolve_parent: Callable[[UUID], UUID | None],
) -> RootAgentBudget:
    """Replay an agent-budget stream into its active reservation set.

    Legacy budget.reserved.v1/released.v1 build reservations whose id equals
    the child agent id; the parent is resolved from the child's own agent
    stream.  budget.reserved.v2/released.v2 use the shared spawn reservation
    id.  budget.legacy-reconciled.v1 releases only entries provable terminal
    from Agent events.  Duplicate/unknown/identity drift corrupt the stream.
    """

    reservations: dict[UUID, ResourceReservation] = {}
    for event in events:
        payload = event.payload
        event_type = event.event_type
        if event_type == "budget.reserved.v1":
            _exact_keys(payload, {"root_agent_id", "child_agent_id", "total_agents"})
            if payload["root_agent_id"] != str(root_agent_id):
                raise AgentError("agent_budget_projection_corrupt")
            child = _uuid(payload, "child_agent_id")
            rid = child
            if rid in reservations:
                raise AgentError("agent_budget_projection_corrupt")
            parent = resolve_parent(child)
            if parent is None:
                raise AgentError("agent_budget_projection_corrupt")
            reservations[rid] = ResourceReservation(rid, child, parent, root_agent_id)
        elif event_type == "budget.released.v1":
            _exact_keys(payload, {"root_agent_id", "child_agent_id", "total_agents"})
            if payload["root_agent_id"] != str(root_agent_id):
                raise AgentError("agent_budget_projection_corrupt")
            child = _uuid(payload, "child_agent_id")
            active = reservations.get(child)
            if active is None:
                raise AgentError("agent_budget_projection_corrupt")
            del reservations[child]
        elif event_type == "budget.reserved.v2":
            _exact_keys(
                payload,
                {"parent_agent_id", "root_agent_id", "child_agent_id", "reservation_id"},
            )
            if payload["root_agent_id"] != str(root_agent_id):
                raise AgentError("agent_budget_projection_corrupt")
            rid = _uuid(payload, "reservation_id")
            if rid in reservations:
                raise AgentError("agent_budget_projection_corrupt")
            child = _uuid(payload, "child_agent_id")
            parent = _uuid(payload, "parent_agent_id")
            if parent is None or parent == child:
                raise AgentError("agent_budget_projection_corrupt")
            reservations[rid] = ResourceReservation(rid, child, parent, root_agent_id)
        elif event_type == "budget.released.v2":
            _exact_keys(
                payload,
                {
                    "parent_agent_id",
                    "root_agent_id",
                    "child_agent_id",
                    "reservation_id",
                    "terminal_state",
                    "terminal_run_id",
                    "released_at",
                },
            )
            if payload["root_agent_id"] != str(root_agent_id):
                raise AgentError("agent_budget_projection_corrupt")
            rid = _uuid(payload, "reservation_id")
            active = reservations.get(rid)
            if active is None:
                raise AgentError("agent_budget_projection_corrupt")
            if (
                payload["child_agent_id"] != str(active.child_agent_id)
                or payload["parent_agent_id"] != str(active.parent_agent_id)
            ):
                raise AgentError("agent_budget_projection_corrupt")
            del reservations[rid]
        elif event_type == "budget.legacy-reconciled.v1":
            _exact_keys(
                payload,
                {
                    "root_agent_id",
                    "releases",
                    "source_global_position",
                    "source_digest",
                    "reconciled_at",
                },
            )
            if payload["root_agent_id"] != str(root_agent_id):
                raise AgentError("agent_budget_projection_corrupt")
            releases = payload["releases"]
            if not isinstance(releases, (list, tuple)):
                raise AgentError("agent_budget_projection_corrupt")
            for item in releases:
                _exact_keys(
                    item,
                    {
                        "reservation_id",
                        "child_agent_id",
                        "parent_agent_id",
                        "terminal_state",
                        "terminal_run_id",
                    },
                )
                rid = _uuid(item, "reservation_id")
                active = reservations.get(rid)
                if active is None:
                    raise AgentError("agent_budget_projection_corrupt")
                if (
                    item["child_agent_id"] != str(active.child_agent_id)
                    or item["parent_agent_id"] != str(active.parent_agent_id)
                ):
                    raise AgentError("agent_budget_projection_corrupt")
                del reservations[rid]
        else:
            raise AgentError("agent_budget_projection_corrupt")
    version = events[-1].stream_version if events else -1
    return RootAgentBudget(
        root_agent_id,
        version,
        tuple(
            sorted(reservations.values(), key=lambda item: str(item.reservation_id))
        ),
    )


def canonical_json(value: Any) -> str:
    """Deterministic compact JSON used for digests and fingerprints."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_digest(canonical_text: str) -> str:
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def _uuid(payload: Mapping[str, Any], key: str) -> UUID | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentError("agent_capacity_projection_corrupt")
    try:
        return UUID(value)
    except ValueError:
        raise AgentError("agent_capacity_projection_corrupt") from None


def _exact_keys(payload: Mapping[str, Any], keys: set[str]) -> None:
    """Reject unknown or missing fields: corruption, not forward-compat."""

    if not isinstance(payload, Mapping):
        raise AgentError("agent_capacity_projection_corrupt")
    if set(payload.keys()) != keys:
        raise AgentError("agent_capacity_projection_corrupt")


__all__ = [
    "ParentCapacity",
    "ResourceReservation",
    "RootAgentBudget",
    "budget_stream",
    "canonical_json",
    "capacity_stream",
    "rebuild_budget",
    "rebuild_capacity",
    "sha256_digest",
]