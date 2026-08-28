"""I8 stateful load cases. All worker IPC and reports are JSON, never pickle."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
for directory in (REPO, REPO / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from scripts.stability_scenarios import NOW, event_digest, read_events, store_at
from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane, terminal_result_identity
from koawa_agent_v2.agents.graph import AgentError, AgentState
from koawa_agent_v2.agents.resources import budget_stream, capacity_stream, rebuild_budget, rebuild_capacity
from koawa_agent_v2.control.event_store import StreamId


EXPECTED_SPAWN_REJECTIONS = frozenset({
    "agent_concurrency_exceeded", "agent_total_exceeded", "agent_spawn_retry_exhausted",
})


def _positive(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


def _control(path: Path, capacity: int, budget: int, clock=lambda: NOW):
    return AgentControlPlane(store_at(path), limits=AgentBudgetLimits(4, budget, capacity), clock=clock)


def _root(control):
    return control.spawn_agent(parent_agent_id=None, task_id="load-root", principal_id="benchmark",
                               scopes=("read",), semantic_idempotency_key="load-root")


def seed_load(path: Path, *, capacity: int, budget: int) -> dict:
    control = _control(path, capacity, budget)
    root = _root(control)
    if control.graph.children(root.agent_id):
        raise ValueError("load seed must not contain existing children")
    return {"root_id": str(root.agent_id), "capacity": capacity, "budget": budget,
            "dataset_digest": event_digest(control.event_store)}


def _spawn(control, root_id, index):
    return control.spawn_agent(
        parent_agent_id=root_id, task_id=f"load-{index}", principal_id="benchmark",
        scopes=("read",), semantic_idempotency_key=f"load-{index}",
    )


def resources(control, root_id) -> dict:
    capacity = rebuild_capacity(root_id, read_events(control.event_store, capacity_stream(root_id)))
    def parent_for(child_id):
        child = control.graph.load(child_id)
        return None if child is None else child.parent_agent_id
    budget = rebuild_budget(root_id, read_events(control.event_store, budget_stream(root_id)),
                            resolve_parent=parent_for)
    capacity_ids = {item.reservation_id for item in capacity.active_reservations}
    budget_ids = {item.reservation_id for item in budget.active_reservations}
    children = control.graph.children(root_id)
    active = {item.agent_id for item in children if item.state not in (
        AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELLED)}
    if capacity_ids != budget_ids or {item.child_agent_id for item in budget.active_reservations} != active:
        raise AssertionError("load_resource_drift")
    return {"capacity": capacity.active_count, "budget": budget.active_count,
            "active_children": len(active), "children": len(children)}


def settle(control, child):
    if child.state is AgentState.CREATED:
        child = control.start_attempt(child.agent_id, expected_version=child.version)
    ref, digest = terminal_result_identity(child.agent_id, child.run_id, AgentState.CANCELLED, "cancelled", ())
    kwargs = dict(run_id=child.run_id, expected_attempt=child.attempt, state=AgentState.CANCELLED,
                  reason="cancelled", result_ref=ref, result_digest=digest, source_message_ids=())
    control.terminal(child.agent_id, **kwargs)
    before = control.graph.load(child.agent_id).version
    control.terminal(child.agent_id, **kwargs)
    if control.graph.load(child.agent_id).version != before:
        raise AssertionError("load_terminal_receipt_duplicated")


def _spawn_worker(request):
    control = _control(Path(request["path"]), request["capacity"], request["budget"])
    with socket.create_connection(("127.0.0.1", request["port"]), timeout=60) as barrier:
        barrier.sendall(json.dumps({"index": request["index"], "token": request["token"]}).encode() + b"\n")
        if barrier.recv(1) != b"G":
            raise RuntimeError("load_barrier_aborted")
    started = time.perf_counter_ns()
    try:
        child = _spawn(control, UUID(request["root_id"]), request["index"])
        outcome = {"status": "created", "agent_id": str(child.agent_id)}
    except AgentError as error:
        if error.code not in EXPECTED_SPAWN_REJECTIONS:
            raise
        outcome = {"status": "rejected", "code": error.code}
    print(json.dumps({"index": request["index"], "pid": os.getpid(),
                      "duration_ns": time.perf_counter_ns() - started, **outcome}), flush=True)


def concurrent_spawn(path: Path, *, workers: int, capacity: int, budget: int) -> dict:
    _positive(workers, "workers", 100)
    _positive(capacity, "capacity", 100)
    _positive(budget, "budget", 100)
    control = _control(path, capacity, budget)
    root = _root(control)
    seed_digest = event_digest(control.event_store)
    processes, barriers, reports = [], [], []
    token = os.urandom(16).hex()  # transient handshake only, never a durable credential
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(workers)
            listener.settimeout(60)
            for index in range(workers):
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--worker"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                processes.append(process)
                request = {"path": str(path.resolve()), "port": listener.getsockname()[1],
                           "token": token, "root_id": str(root.agent_id), "index": index,
                           "capacity": capacity, "budget": budget}
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
            ready = set()
            deadline = time.monotonic() + 60
            while len(ready) < workers:
                listener.settimeout(max(.001, deadline - time.monotonic()))
                barrier, _ = listener.accept()
                barriers.append(barrier)
                barrier.settimeout(max(.001, deadline - time.monotonic()))
                with barrier.makefile("rb") as reader:
                    raw = reader.readline(4097)
                message = json.loads(raw)
                if (message.get("token") != token or type(message.get("index")) is not int
                        or message["index"] not in range(workers) or message["index"] in ready):
                    raise AssertionError("load_barrier_identity_invalid")
                ready.add(message["index"])
            # Every OS process is ready before ANY command enters the race.
            started = time.perf_counter_ns()
            for barrier in barriers:
                barrier.sendall(b"G")
            deadline = time.monotonic() + 120
            for process in processes:
                output, error = process.communicate(timeout=max(.001, deadline - time.monotonic()))
                if process.returncode:
                    raise RuntimeError(f"load_worker_failed:{process.returncode}")
                reports.append(json.loads(output))
            duration = time.perf_counter_ns() - started
    finally:
        for barrier in barriers:
            barrier.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
    if {item["index"] for item in reports} != set(range(workers)) or len({item["pid"] for item in reports}) != workers:
        raise AssertionError("load_worker_identity_mismatch")
    created = [item for item in reports if item["status"] == "created"]
    if not 1 <= len(created) <= min(capacity, budget):
        raise AssertionError("load_budget_limit_violated")
    peak = resources(control, root.agent_id)
    if peak["children"] != len(created) or peak["active_children"] != len(created):
        raise AssertionError("load_spawn_receipt_disagrees_with_truth")
    for report in created:
        before = control.graph.load(UUID(report["agent_id"]))
        replay = _spawn(control, root.agent_id, report["index"])
        if replay != before:
            raise AssertionError("load_spawn_receipt_not_idempotent")
        settle(control, replay)
    final = resources(control, root.agent_id)
    if any(final[key] for key in ("capacity", "budget", "active_children")):
        raise AssertionError("load_resource_not_released")
    return {"duration_ns": duration, "workers": workers, "ready_before_release": workers,
            "worker_reports": sorted(reports, key=lambda item: item["index"]),
            "created": len(created), "rejections": dict(Counter(item["code"] for item in reports if item["status"] == "rejected")),
            "limits": {"capacity": capacity, "budget": budget}, "peak_resources": peak,
            "final_resources": final, "seed_digest": seed_digest, "final_event_digest": event_digest(control.event_store)}


def heartbeat_takeover(path: Path, *, cycles: int) -> dict:
    _positive(cycles, "cycles", 1000)
    clock = [NOW]
    control = _control(path, 1, 1, lambda: clock[0])
    root = _root(control)
    # The immutable input is the root-only seed. Actual run IDs are generated
    # by production and intentionally remain random in the raw event digest.
    seed_digest = event_digest(control.event_store)
    child = _spawn(control, root.agent_id, 0)
    running = control.start_attempt(child.agent_id, expected_version=child.version, lease_seconds=1)
    timings, transitions = [], []
    for index in range(cycles):
        started = time.perf_counter_ns()
        old_run, old_attempt = running.run_id, running.attempt
        beat = control.heartbeat(child.agent_id, run_id=old_run, attempt=old_attempt,
                                 lease_seconds=1, beat_number=index)
        # Receipt replay is checked before expiry, with no extra heartbeat fact.
        if control.heartbeat(child.agent_id, run_id=old_run, attempt=old_attempt,
                             lease_seconds=1, beat_number=index).version != beat.version:
            raise AssertionError("load_heartbeat_receipt_duplicated")
        clock[0] += timedelta(seconds=2)
        control.discover_orphans()
        orphan = control.graph.load(child.agent_id)
        if orphan.state is not AgentState.ORPHANED:
            raise AssertionError("load_expired_lease_not_orphaned")
        running = control.start_attempt(child.agent_id, expected_version=orphan.version, lease_seconds=1)
        if running.attempt != old_attempt + 1 or running.run_id == old_run:
            raise AssertionError("load_takeover_not_fresh")
        try:
            control.heartbeat(child.agent_id, run_id=old_run, attempt=old_attempt,
                              lease_seconds=1, beat_number=1_000_000 + index)
        except AgentError as error:
            if error.code != "agent_lease_lost":
                raise
        else:
            raise AssertionError("load_stale_owner_not_fenced")
        if control.graph.load(child.agent_id).version != running.version:
            raise AssertionError("load_fenced_owner_wrote_event")
        current_resources = resources(control, root.agent_id)
        if current_resources["active_children"] != 1:
            raise AssertionError("load_takeover_resource_drift")
        transitions.append({"attempt": running.attempt, "version": running.version,
                            "state": running.state.value, "old_owner_fenced": True,
                            "resources": current_resources})
        timings.append(time.perf_counter_ns() - started)
    settle(control, running)
    final = resources(control, root.agent_id)
    if any(final[key] for key in ("capacity", "budget", "active_children")):
        raise AssertionError("load_takeover_resource_not_released")
    return {"duration_ns": sum(timings), "cycles": cycles, "cycle_raw_ns": timings,
            "transitions": transitions,
            "stale_owners_fenced": cycles, "attempts": running.attempt,
            "final_resources": final, "seed_digest": seed_digest,
            "final_event_digest": event_digest(control.event_store)}


def mcp_pending_storm(*, pending: int, notifications: int) -> dict:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock, enumerate as threads
    from koawa_agent_v2.mcp import McpSession, McpSessionError, StdioTransport
    from koawa_agent_v2.mcp.protocol import notification_payload
    from koawa_agent_v2.telemetry.faults import NoOpFaultPort

    _positive(pending, "pending", 100)
    _positive(notifications, "notifications", 65536)
    baseline_threads = {thread.ident for thread in threads()}
    stats = {"sent": 0, "peak_pending": 0, "overflow_rejected": 0, "notifications_throttled": 0}
    lock = Lock()

    class TraceCounter:
        def emit(self, probe):
            if probe.kind == "notification_throttled":
                with lock:
                    stats["notifications_throttled"] += 1

    class PendingGate(NoOpFaultPort):
        def hit(self, point, facts):
            super().hit(point, facts)
            if point != "s4.mcp.call.after_send_before_result":
                return
            with lock:
                stats["sent"] += 1
                with session._pending_lock:
                    stats["peak_pending"] = max(stats["peak_pending"], len(session._pending))
                release = stats["sent"] == pending
            if release:
                # The N+1st call must be rejected before it can reach fixture.
                try:
                    session.call(binding, '{"index":0}')
                except McpSessionError as error:
                    if error.code != "mcp_pending_limit_exceeded":
                        raise
                    stats["overflow_rejected"] += 1
                else:
                    raise AssertionError("load_pending_limit_not_enforced")
                transport.send(notification_payload("benchmark/release"))

    transport = StdioTransport(
        [sys.executable, str(REPO / "scripts/stability_mcp_fixture.py"),
         "--pending", str(pending), "--notifications", str(notifications)],
        env={"PYTHONPATH": str(REPO / "src")}, cwd=str(REPO),
    )
    session = McpSession("loadprobe", transport, max_pending_requests=pending,
                         max_notifications_per_window=8, tool_call_timeout_seconds=30,
                         trace_sink=TraceCounter(), fault_port=PendingGate())
    try:
        catalog = session.connect()
        binding = next(iter(catalog.bindings.values()))
        started = time.perf_counter_ns()
        with ThreadPoolExecutor(max_workers=pending, thread_name_prefix="stability-mcp-call") as executor:
            futures = [executor.submit(session.call, binding, json.dumps({"index": index})) for index in range(pending)]
            results = [future.result(timeout=45) for future in futures]
        for index, result in enumerate(results):
            if result.is_error or result.uncertain:
                raise AssertionError("load_mcp_result_not_known")
            if json.loads(result.content) != {"index": index, "received_calls": pending, "notifications": notifications}:
                raise AssertionError("load_mcp_response_routed_incorrectly")
        with session._pending_lock:
            if session._pending:
                raise AssertionError("load_mcp_pending_leak")
        if stats["peak_pending"] != pending or stats["overflow_rejected"] != 1:
            raise AssertionError("load_mcp_peak_not_observed")
        if not session.pending_refresh or session.unknown_response_count:
            raise AssertionError("load_mcp_notification_or_routing_lost")
        refreshed = session.refresh()
        if refreshed.generation != catalog.generation + 1 or session.pending_refresh:
            raise AssertionError("load_mcp_refresh_not_converged")
        duration = time.perf_counter_ns() - started
    finally:
        session.close()
    leaked = [thread.name for thread in threads() if thread.ident not in baseline_threads and
              (thread.name.startswith("stability-mcp-") or thread.name.startswith("mcp-"))]
    if leaked:
        raise AssertionError("load_mcp_thread_leak")
    return {"duration_ns": duration, "pending": pending, "notifications": notifications,
            "completed": len(results), "pending_after_close": len(session._pending),
            "unknown_responses": session.unknown_response_count,
            "session_closed": session.state == session.CLOSED, **stats}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--case", choices=("spawn", "heartbeat", "mcp"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--workers", type=int, default=100)
    parser.add_argument("--capacity", type=int, default=8)
    parser.add_argument("--budget", type=int, default=8)
    parser.add_argument("--cycles", type=int, default=1000)
    parser.add_argument("--pending", type=int, default=100)
    parser.add_argument("--notifications", type=int, default=2048)
    args = parser.parse_args()
    if args.worker:
        _spawn_worker(json.loads(sys.stdin.readline()))
    else:
        import hashlib
        import tempfile
        from scripts.stability_benchmark import atomic_json, canonical_bytes, _git_commit
        if args.case is None or args.report is None:
            parser.error("--case and --report are required")
        with tempfile.TemporaryDirectory(prefix="koawa-stability-load-") as raw:
            path = Path(raw) / "load.sqlite3"
            if args.case == "spawn":
                result = concurrent_spawn(path, workers=args.workers, capacity=args.capacity, budget=args.budget)
            elif args.case == "heartbeat":
                result = heartbeat_takeover(path, cycles=args.cycles)
            else:
                result = mcp_pending_storm(pending=args.pending, notifications=args.notifications)
        report = {"protocol_version": "stability-load-v1", "measurement_mode": "single_workload",
                  "case": args.case, "commit": _git_commit(REPO), "result": result,
                  "threshold_enforced": False, "release_pass": None}
        report["report_digest"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
        atomic_json(args.report, report)
        print(json.dumps({"report": str(args.report.resolve()), "case": args.case,
                          "duration_ms": result["duration_ns"] / 1_000_000, "threshold_enforced": False}))
