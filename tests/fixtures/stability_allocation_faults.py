"""Allocation/process lifecycle OS-kill windows with external lock evidence."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from uuid import UUID, uuid5, NAMESPACE_URL

from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.mcp.activation import ActivationService, resolve_launch_identity
from koawa_agent_v2.runtime.config import McpExecutionProfile, McpResourceLimits, McpServerConfig
from koawa_agent_v2.telemetry.faults import FAULT_SPECS, NoOpFaultPort
from scripts.stability_benchmark import atomic_json


ALLOCATION_POINTS = (
    "s4.allocation.after_intent_commit",
    "s4.allocation.after_claim_commit",
    "s4.launch.after_external_create_before_started",
    "s4.allocation.after_started_commit",
    "s4.allocation.after_ready_commit",
    "s4.mcp.close.after_terminate_before_stopped",
)
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def _identity(root: Path):
    config = McpServerConfig(
        server_id="allocation", command=(str(Path(sys.executable).resolve()), "-B"),
        execution_profile=McpExecutionProfile.HOST_TRUSTED,
        resource_limits=McpResourceLimits(),
    )
    return resolve_launch_identity(config, base_dir=root)


def _lock_held(path: Path) -> bool:
    handle = path.open("a+b")
    try:
        handle.seek(0)
        if path.stat().st_size == 0:
            handle.write(b"x")
            handle.flush()
            handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _spawn_child(root: Path):
    nonce = uuid5(NAMESPACE_URL, "koawa-allocation-child-v1")
    lock = root / "child.lock"
    marker = root / "child.json"
    child = Path(__file__).with_name("allocation_child.py")
    process = subprocess.Popen(
        [sys.executable, str(child), str(lock), str(marker), str(nonce)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )
    deadline = time.monotonic() + 10
    while not marker.exists():
        if process.poll() is not None:
            raise AssertionError("allocation child exited early")
        if time.monotonic() >= deadline:
            process.kill()
            raise AssertionError("allocation child marker timeout")
        time.sleep(.01)
    document = json.loads(marker.read_text(encoding="utf-8"))
    assert document == {"pid": process.pid, "nonce": str(nonce)}
    assert _lock_held(lock)
    return process


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _lock_held(Path.cwd() / "__never_used__"):
            break
        time.sleep(.01)


class AllocationKillPort(NoOpFaultPort):
    def __init__(self, root: Path, point: str):
        self.root, self.point = root, point

    def hit(self, point, facts):
        super().hit(point, facts)
        if point != self.point:
            return
        service = ActivationService(SqliteEventStore(self.root / "runtime.db"), clock=lambda: NOW)
        allocation_id = UUID(facts["allocation_id"])
        view = service.get_allocation(allocation_id)
        expected = {
            ALLOCATION_POINTS[0]: service.ALLOCATION_INTENDED,
            ALLOCATION_POINTS[1]: service.ALLOCATION_CLAIMED,
            ALLOCATION_POINTS[2]: service.ALLOCATION_CLAIMED,
            ALLOCATION_POINTS[3]: service.ALLOCATION_STARTED,
            ALLOCATION_POINTS[4]: service.ALLOCATION_READY,
            ALLOCATION_POINTS[5]: service.ALLOCATION_READY,
        }[point]
        assert view.status == expected
        child = None
        if (self.root / "child.json").exists():
            child = json.loads((self.root / "child.json").read_text(encoding="utf-8"))
        child_alive = _lock_held(self.root / "child.lock") if child else False
        if point in ALLOCATION_POINTS[2:5]:
            assert child_alive
        if point == ALLOCATION_POINTS[5]:
            assert not child_alive
        atomic_json(self.root / "ready.json", {
            "point": point, "point_class": FAULT_SPECS[point].point_class.value,
            "crash_pid": os.getpid(), "allocation_id": str(allocation_id),
            "allocation_version": view.version, "allocation_status": view.status,
            "child": child, "child_alive": child_alive,
        })
        Event().wait()


def crash_allocation(root: Path, point: str) -> None:
    if point not in ALLOCATION_POINTS:
        raise ValueError("unsupported allocation point")
    store = SqliteEventStore(root / "runtime.db")
    identity = _identity(root)
    base = ActivationService(store, clock=lambda: NOW)
    granted = base.plan_start(
        identity, principal_id="root", scope="mcp.host_process.execute",
        decision="allow",
    )
    service = ActivationService(
        store, clock=lambda: NOW, fault_port=AllocationKillPort(root, point),
    )
    if point == ALLOCATION_POINTS[0]:
        intent = service.intend(granted)
    else:
        intent = base.intend(granted)
        if point == ALLOCATION_POINTS[1]:
            service.claim(intent, granted, principal_id="root")
        else:
            ticket = base.claim(intent, granted, principal_id="root")
            child = _spawn_child(root)
            if point == ALLOCATION_POINTS[2]:
                service.record_started(ticket)
            elif point == ALLOCATION_POINTS[3]:
                service.record_started(ticket)
            else:
                base.record_started(ticket)
                if point == ALLOCATION_POINTS[4]:
                    service.record_ready(ticket)
                else:
                    base.record_ready(ticket)
                    child.terminate()
                    child.wait(timeout=10)
                    assert not _lock_held(root / "child.lock")
                    service.record_stopped(ticket)
    atomic_json(root / "request.json", {
        "point": point, "allocation_id": str(intent.allocation_id),
        "launch_digest": identity.config_digest,
    })
    raise AssertionError("allocation operation missed kill point")


def recover_allocation(root: Path) -> None:
    marker = json.loads((root / "ready.json").read_text(encoding="utf-8"))
    store = SqliteEventStore(root / "runtime.db")
    service = ActivationService(store, clock=lambda: NOW)
    allocation_id = UUID(marker["allocation_id"])
    before = service.get_allocation(allocation_id)
    child = marker.get("child")
    if child is not None and _lock_held(root / "child.lock"):
        os.kill(child["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 10
        while _lock_held(root / "child.lock") and time.monotonic() < deadline:
            time.sleep(.01)
        assert not _lock_held(root / "child.lock")
    evidence = hashlib.sha256(json.dumps(
        {"allocation_id": str(allocation_id), "process_absent": True},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    point = marker["point"]
    if point in ALLOCATION_POINTS[:2]:
        result = service.reconcile_allocation(
            allocation_id, expected_version=before.version,
            outcome="failed_before_start", evidence_kind="process_absent",
            evidence_digest=evidence, reconciler_principal_id="fixture-reconciler",
        )
    elif point == ALLOCATION_POINTS[2]:
        unknown = service.reconcile_allocation(
            allocation_id, expected_version=before.version,
            outcome="outcome_unknown", evidence_kind="owned_process_reaped",
            evidence_digest=evidence, reconciler_principal_id="fixture-reconciler",
        )
        result = service.reconcile_allocation(
            allocation_id, expected_version=unknown.version,
            outcome="stopped", evidence_kind="owned_process_reaped",
            evidence_digest=evidence, reconciler_principal_id="fixture-reconciler",
        )
    else:
        result = service.reconcile_allocation(
            allocation_id, expected_version=before.version,
            outcome="stopped", evidence_kind="owned_process_reaped",
            evidence_digest=evidence, reconciler_principal_id="fixture-reconciler",
        )
    normalized = [
        [event.stream_id.category, event.stream_version, event.event_type]
        for event in store.read_all()
    ]
    atomic_json(root / "recovered.json", {
        "point": point, "recovery_pid": os.getpid(), "status": result.status,
        "normalized_events": normalized, "child_alive": _lock_held(root / "child.lock")
        if child is not None else False,
    })
