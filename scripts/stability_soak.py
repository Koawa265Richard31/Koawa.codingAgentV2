#!/usr/bin/env python3
"""I8 long-lived mixed workload and one-minute OS resource sampler."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for directory in (REPO, REPO / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from scripts.stability_benchmark import _environment, _git_commit, atomic_json, canonical_bytes
from scripts.stability_load import _control, _root, _spawn, concurrent_spawn, event_digest, mcp_pending_storm, resources
from scripts.stability_resources import assess_soak, snapshot
from scripts.stability_scenarios import NOW, seed_execution, operation
from koawa_agent_v2.agents.control import terminal_result_identity
from koawa_agent_v2.agents.graph import AgentError, AgentState
from koawa_agent_v2.agents.messages import MessageKind, MessageStatus


def _complete_task(control, root, index, clock, *, takeover):
    child = _spawn(control, root.agent_id, index)
    message = control.send_message(child.agent_id, from_agent_id=root.agent_id, kind=MessageKind.TASK,
                                   body_ref="soak-task", idempotency_key=f"soak-{index}")
    running = control.start_attempt(child.agent_id, expected_version=child.version, lease_seconds=30)
    if takeover:
        old = running
        control.heartbeat(child.agent_id, run_id=old.run_id, attempt=old.attempt, lease_seconds=30)
        clock[0] += timedelta(seconds=31)
        control.discover_orphans()
        orphan = control.graph.load(child.agent_id)
        running = control.start_attempt(child.agent_id, expected_version=orphan.version, lease_seconds=30)
        try:
            control.heartbeat(child.agent_id, run_id=old.run_id, attempt=old.attempt, beat_number=1)
        except AgentError as error:
            if error.code != "agent_lease_lost":
                raise
        else:
            raise AssertionError("soak_stale_owner_not_fenced")
    delivered = control.deliver_message(child.agent_id, message.message_id, run_id=running.run_id)
    control.record_message_result(child.agent_id, message.message_id, run_id=running.run_id,
                                  expected_delivery_attempt=delivered.delivery_attempt, outcome="ok")
    control.ack_message(child.agent_id, message.message_id, run_id=running.run_id)
    ref, digest = terminal_result_identity(child.agent_id, running.run_id, AgentState.COMPLETED, None,
                                           control.mailbox.load(child.agent_id))
    control.terminal(child.agent_id, run_id=running.run_id, expected_attempt=running.attempt,
                     state=AgentState.COMPLETED, reason=None, result_ref=ref, result_digest=digest,
                     source_message_ids=(message.message_id,))


def run_soak(*, duration_seconds: float, sample_interval: float, reference_qualified: bool = False, quick: bool = False) -> dict:
    if isinstance(duration_seconds, bool) or not math.isfinite(duration_seconds) or duration_seconds <= 0 or duration_seconds > 86400:
        raise ValueError("invalid soak duration")
    if isinstance(sample_interval, bool) or not math.isfinite(sample_interval) or not 0 < sample_interval <= 60:
        raise ValueError("invalid soak sample interval")
    if type(quick) is not bool or type(reference_qualified) is not bool:
        raise ValueError("soak flags must be bool")
    samples, sample_errors = [], []
    stop, lock = threading.Event(), threading.Lock()
    counts = {"agent_tasks": 0, "takeovers": 0, "concurrent_spawn_waves": 0, "checkpoint_rebuilds": 0, "mcp_storms": 0}
    shape = {"execution_events": 40 if quick else 10000, "spawn_workers": 4 if quick else 100,
             "mcp_pending": 4 if quick else 100, "notifications": 128 if quick else 2048}
    with tempfile.TemporaryDirectory(prefix="koawa-stability-soak-") as raw:
        root_path = Path(raw)
        database = root_path / "runtime.sqlite3"
        clock = [NOW]
        control = _control(database, 2, 2, lambda: clock[0])
        root = _root(control)
        started = time.monotonic()

        def collect():
            next_sample = started
            while not stop.is_set():
                delay = next_sample - time.monotonic()
                if delay > 0 and stop.wait(delay):
                    break
                elapsed = time.monotonic() - started
                try:
                    item = snapshot(elapsed, tuple(root_path.glob("*.sqlite3")))
                    with lock:
                        samples.append(item)
                except Exception as error:
                    with lock:
                        sample_errors.append(type(error).__name__)
                    stop.set()
                    break
                next_sample += sample_interval

        sampler = threading.Thread(target=collect, name="stability-soak-sampler", daemon=False)
        sampler.start()
        index = 0
        execution_manifest = None
        try:
            while time.monotonic() - started < duration_seconds and not stop.is_set():
                takeover = quick or index % 5 == 4
                _complete_task(control, root, index, clock, takeover=takeover)
                counts["agent_tasks"] += 1
                counts["takeovers"] += int(takeover)
                if quick or index % 10 == 9:
                    execution_path = root_path / "execution.sqlite3"
                    if execution_manifest is None:
                        execution_manifest = seed_execution(execution_path, shape["execution_events"])
                    operation("verified_event_rebuild_10k", execution_path, execution_manifest)()
                    counts["checkpoint_rebuilds"] += 1
                if quick or index % 25 == 24:
                    concurrent_spawn(root_path / f"spawn-{index}.sqlite3", workers=shape["spawn_workers"], capacity=2, budget=2)
                    counts["concurrent_spawn_waves"] += 1
                    result = mcp_pending_storm(pending=shape["mcp_pending"], notifications=shape["notifications"])
                    if result["pending_after_close"] or not result["session_closed"]:
                        raise AssertionError("soak_mcp_resource_leak")
                    counts["mcp_storms"] += 1
                index += 1
        finally:
            stop.set()
            sampler.join(timeout=max(10, sample_interval + 5))
        if sampler.is_alive():
            raise RuntimeError("soak_sampler_thread_stuck")
        elapsed = time.monotonic() - started
        # Preserve a final endpoint even when stop happens just before a minute boundary.
        with lock:
            if not samples or elapsed - samples[-1]["elapsed_seconds"] > .001:
                samples.append(snapshot(elapsed, tuple(root_path.glob("*.sqlite3"))))
            captured = list(samples)
        state = resources(control, root.agent_id)
        if any(state[name] for name in ("capacity", "budget", "active_children")):
            raise AssertionError("soak_agent_resource_drift")
        children = control.graph.children(root.agent_id)
        if any(child.state not in (AgentState.COMPLETED, AgentState.CANCELLED) for child in children):
            raise AssertionError("soak_nonterminal_child")
        root_messages = control.mailbox.load(root.agent_id)
        if any(message.status is not MessageStatus.QUEUED for message in root_messages):
            raise AssertionError("soak_stuck_parent_delivery")
        database_digest = event_digest(control.event_store)
    assessment = assess_soak(captured, reference_qualified=reference_qualified and not quick)
    workload_complete = all(count > 0 for count in counts.values()) and not sample_errors
    passed = (assessment["passed"] and workload_complete) if assessment["threshold_enforced"] else None
    return {"duration_seconds": elapsed, "requested_duration_seconds": duration_seconds,
            "measurement_mode": "quick_test" if quick else "full", "workload_shape": shape,
            "workload_complete": workload_complete,
            "sample_errors": sample_errors, "samples": captured, "assessment": assessment,
            "operation_counts": counts, "final_resources": state, "children": len(children),
            "queued_parent_results": len(root_messages), "event_digest": database_digest,
            "threshold_enforced": assessment["threshold_enforced"], "passed": passed}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--sample-seconds", type=float, default=60)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--reference-environment-digest")
    args = parser.parse_args()
    if not 0 < args.hours <= 24 or not 1 <= args.sample_seconds <= 60:
        parser.error("hours must be in (0,24], sample-seconds in [1,60]")
    environment = _environment(60 if args.reference_environment_digest else .2)
    identity = environment["identity"]
    hardware = all((identity["filesystem"], identity["power_profile"], identity["local_ssd"], identity["exclusive_cpus"]))
    reference = bool(not args.quick and args.hours == 24 and args.sample_seconds == 60 and hardware and
                     args.reference_environment_digest == environment["environment_digest"] and
                     environment["background_system_cpu_ratio"] is not None and
                     environment["background_system_cpu_ratio"] < .05)
    result = run_soak(duration_seconds=args.hours * 3600, sample_interval=args.sample_seconds,
                      reference_qualified=reference, quick=args.quick)
    report = {"protocol_version": "stability-soak-v1", "commit": _git_commit(REPO),
              "environment": environment, "reference_qualified": reference, "result": result}
    report["report_digest"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    atomic_json(args.report, report)
    print(json.dumps({"report": str(args.report.resolve()), "reference_qualified": reference,
                      "passed": result["passed"], "operations": result["operation_counts"]}))
    return 1 if result["sample_errors"] or (reference and not result["passed"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
