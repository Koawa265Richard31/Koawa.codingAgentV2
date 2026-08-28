"""Real activation commit/response-loss windows; no launcher is permitted."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from uuid import UUID

from koawa_agent_v2.control.durable_json import canonical_json_bytes_v1
from koawa_agent_v2.mcp.activation import ActivationService, McpActivationError, resolve_launch_identity
from koawa_agent_v2.runtime.config import McpExecutionProfile, McpResourceLimits, McpServerConfig
from koawa_agent_v2.telemetry.faults import FAULT_SPECS, NoOpFaultPort
from scripts.stability_benchmark import atomic_json
from scripts.stability_scenarios import event_digest, store_at


ACTIVATION_POINTS = (
    "s4.activation.after_request_commit",
    "s4.activation.before_grant_append",
    "s4.activation.after_grant_commit",
)
START = datetime(2030, 1, 1, tzinfo=timezone.utc)


def launch_identity(root):
    # Real executable fingerprint; no fake grant DTO or external process.
    config = McpServerConfig(
        server_id="activation", command=(str(Path(sys.executable).resolve()), "-B"),
        execution_profile=McpExecutionProfile.HOST_TRUSTED, resource_limits=McpResourceLimits(),
    )
    return resolve_launch_identity(config, base_dir=root)


def plan(service, identity, decision):
    return service.plan_start(identity, principal_id="fixture-principal",
                              scope="mcp.host_process.execute", decision=decision)


class ActivationKillPort(NoOpFaultPort):
    def __init__(self, root, request):
        self.root, self.request = root, request

    def hit(self, point, facts):
        super().hit(point, facts)
        if point != self.request["point"]:
            return
        store = store_at(self.root / "runtime.db")
        service = ActivationService(store, clock=lambda: START)
        events = store.read_all()
        delta = 0 if point == ACTIVATION_POINTS[1] else 1
        assert len(events) == self.request["baseline_count"] + delta
        assert all(event.stream_id.category == "mcp-activation" for event in events)
        if delta:
            last = events[-1]
            assert last.commit_size == 1 and last.commit_index == 0
            assert last.stream_version == facts["stream_version"]
            assert str(last.stream_id.aggregate_id) == facts["request_id"]
        expected = "granted" if point == ACTIVATION_POINTS[2] else "requested"
        view = service._reconstruct(UUID(facts["request_id"]))
        if self.request["variant"] == "policy" and point == ACTIVATION_POINTS[1]:
            assert view is None and not events
        else:
            assert view.status == expected
        atomic_json(self.root / "ready.json", {
            "point": point, "point_class": FAULT_SPECS[point].point_class.value,
            "variant": self.request["variant"], "crash_pid": os.getpid(),
            "request_id": facts["request_id"], "event_digest": event_digest(store),
            "visible_count": len(events), "activation_delta": delta,
        })
        Event().wait()


def crash_activation(root, point, variant):
    if point not in ACTIVATION_POINTS or variant not in ("operator", "policy"):
        raise ValueError("unsupported activation fault")
    if variant == "policy" and point == ACTIVATION_POINTS[0]:
        raise ValueError("policy allow does not request approval")
    store = store_at(root / "runtime.db")
    identity = launch_identity(root)
    service = ActivationService(store, clock=lambda: START)
    view = None
    if variant == "operator" and point != ACTIVATION_POINTS[0]:
        view = plan(service, identity, "ask")
    request = {"point": point, "variant": variant, "baseline_count": len(store.read_all()),
               "launch_digest": identity.config_digest}
    atomic_json(root / "request.json", request)
    service = ActivationService(store, clock=lambda: START, fault_port=ActivationKillPort(root, request))
    if view is not None:
        service.resolve_activation(view.request_id, True, approver_principal_id="fixture-operator")
    else:
        plan(service, identity, "allow" if variant == "policy" else "ask")
    raise AssertionError("activation missed production hook")


def recover_activation(root):
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    marker = json.loads((root / "ready.json").read_text(encoding="utf-8"))
    store = store_at(root / "runtime.db")
    assert event_digest(store) == marker["event_digest"]
    identity = launch_identity(root)
    assert identity.config_digest == request["launch_digest"]
    now = START + timedelta(seconds=10)
    service = ActivationService(store, clock=lambda: now)
    point, variant = marker["point"], marker["variant"]
    request_id = UUID(marker["request_id"])
    prior = service._reconstruct(request_id)
    if point == ACTIVATION_POINTS[0]:
        result = plan(service, identity, "ask")
        assert result.status == "requested" and service.pending_requests() == (result,)
    elif variant == "policy":
        result = plan(service, identity, "allow")
    else:
        result = service.resolve_activation(request_id, True, approver_principal_id="fixture-operator")
    first_digest = event_digest(store)
    now += timedelta(seconds=20)
    for _ in range(2):
        if point == ACTIVATION_POINTS[0]:
            again = plan(service, identity, "ask")
        elif variant == "policy":
            again = plan(service, identity, "allow")
        else:
            again = service.resolve_activation(request_id, True, approver_principal_id="fixture-operator")
        assert again == result and event_digest(store) == first_digest
    if point == ACTIVATION_POINTS[2]:
        assert result == prior, "response loss renewed or changed a committed authorization"
    if result.status == "granted":
        assert not service.pending_requests()
        # Conflicting decisions and a different approver are not receipt replay.
        for approved, principal in ((False, "fixture-operator"), (True, "other-operator")):
            try:
                service.resolve_activation(request_id, approved, approver_principal_id=principal)
            except McpActivationError as exc:
                assert exc.code == "activation_already_resolved"
            else:
                raise AssertionError("conflicting authorization accepted")
        assert event_digest(store) == first_digest
    events = store.read_all()
    expected_count = 1 if point == ACTIVATION_POINTS[0] or variant == "policy" else 2
    assert len(events) == expected_count
    assert all(event.stream_id.category == "mcp-activation" for event in events)
    assert not service._tickets, "authorization replay created a launch ticket"
    normalized = [{"stream": e.stream_id.key, "version": e.stream_version, "type": e.event_type,
                   "payload": json.loads(canonical_json_bytes_v1(e.payload)),
                   "commit_index": e.commit_index, "commit_size": e.commit_size} for e in events]
    atomic_json(root / "recovered.json", {
        "point": point, "variant": variant, "status": result.status,
        "recovery_pid": os.getpid(), "normalized_events": normalized,
    })
