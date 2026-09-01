#!/usr/bin/env python3
"""I8 capacity evidence collector; developer evidence is not a release pass."""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for directory in (REPO, REPO / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from scripts import stability_scenarios as scenarios
from scripts import stability_load as load
from scripts import stability_resources as resources
from scripts import stability_reference as reference

PROTOCOL_VERSION = "stability-benchmark-v1"
DATA_SEED = 0x4B4F4157415632
THRESHOLDS_MS = {
    "verified_event_rebuild_10k": 1000.0,
    "mailbox_next": 100.0,
    "uncontended_spawn_transaction": 100.0,
    "wait_agents_100": 250.0,
}
# A supplied digest must not promote partial measurements to release proof.
PENDING_SCENARIOS = ("soak_24h",)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8", "strict")


def nearest_rank(values: list[float], percentile: float) -> float:
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("samples must be nonempty finite nonnegative numbers")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    return sorted(values)[math.ceil(percentile * len(values)) - 1]


def summary(values_ns: list[int]) -> dict:
    values_ms = [value / 1_000_000 for value in values_ns]
    return {"samples": len(values_ms), "raw_ns": values_ns,
            "p50_ms": nearest_rank(values_ms, .5), "p95_ms": nearest_rank(values_ms, .95),
            "max_ms": max(values_ms)}


def atomic_json(path: Path, document: dict) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _cpu_times() -> tuple[int, int] | None:
    """System busy/total ticks, NOT this sleeping collector's process CPU."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        def ticks(value):
            return (value.dwHighDateTime << 32) | value.dwLowDateTime
        total = ticks(kernel) + ticks(user)
        return total - ticks(idle), total
    stat = Path("/proc/stat")
    if stat.is_file():
        fields = [int(x) for x in stat.read_text().splitlines()[0].split()[1:9]]
        total = sum(fields)
        return total - fields[3] - fields[4], total
    return None


def _memory_bytes() -> int | None:
    if os.name == "nt":
        import ctypes
        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in (
                    "total", "available", "page_total", "page_available", "virtual_total",
                    "virtual_available", "extended_available")]
        status = Status()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total)
    if hasattr(os, "sysconf"):
        try:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            pass
    return None


def _affinity_cpus() -> list[int] | None:
    if hasattr(os, "sched_getaffinity"):
        try:
            return sorted(os.sched_getaffinity(0))
        except OSError:
            return None
    if os.name == "nt":
        import ctypes
        # Python 3.14 removed ctypes.wintypes.DWORD_PTR; a pointer-sized mask
        # is equivalent for GetProcessAffinityMask.
        mask_type = getattr(ctypes, "c_size_t")
        process_mask, system_mask = mask_type(), mask_type()
        if ctypes.windll.kernel32.GetProcessAffinityMask(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(process_mask), ctypes.byref(system_mask),
        ):
            return [index for index in range(process_mask.value.bit_length())
                    if process_mask.value & (1 << index)]
    return None


def _git_commit(repo: Path) -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
    value = result.stdout.strip().lower()
    return value if len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None


def _environment(sample_seconds: float, *, reference_attestation: dict | None = None) -> dict:
    first = _cpu_times()
    started = time.perf_counter()
    deadline = started + sample_seconds
    while time.perf_counter() < deadline:
        time.sleep(max(0, min(.05, deadline - time.perf_counter())))
    last = _cpu_times()
    busy_ratio = None
    if first is not None and last is not None and last[1] > first[1]:
        busy_ratio = (last[0] - first[0]) / (last[1] - first[1])
    identity = reference.merge_identity({
        "python": platform.python_version(), "python_implementation": platform.python_implementation(),
        "python_build": list(platform.python_build()), "python_debug": hasattr(sys, "gettotalrefcount"),
        "os": platform.platform(), "machine": platform.machine(), "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(), "memory_bytes": _memory_bytes(),
        "affinity_cpus": _affinity_cpus(),
        "sqlite_version": sqlite3.sqlite_version,
        "filesystem": None, "power_profile": None, "local_ssd": None, "exclusive_cpus": None,
    }, reference_attestation)
    return {"identity": identity,
            "environment_digest": hashlib.sha256(canonical_bytes(identity)).hexdigest(),
            "background_system_cpu_ratio": busy_ratio,
            "background_sample_seconds": time.perf_counter() - started,
            "pythonhashseed": os.environ.get("PYTHONHASHSEED"), "gc_enabled": gc.isenabled(),
            "tracing": sys.gettrace() is not None, "profiling": sys.getprofile() is not None}


@dataclass(frozen=True)
class Shape:
    event_count: int
    mailbox_agents: int
    mailbox_events: int
    wait_agents: int
    samples: int
    rebuild_samples: int
    batches: int
    warmups: int
    spawn_workers: int
    heartbeat_cycles: int
    mcp_pending: int
    mcp_notifications: int


def _timed(operation) -> int:
    started = time.perf_counter_ns()
    operation()
    return time.perf_counter_ns() - started


def _cold_sample(name: str, path: Path, manifest: dict) -> int:
    # Child's imports and startup are outside the duration reported by child.
    request = {"name": name, "path": str(path), "manifest": manifest}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--sample"],
        input=json.dumps(request), capture_output=True, text=True, timeout=120, check=True,
    )
    document = json.loads(result.stdout)
    if set(document) != {"duration_ns"} or type(document["duration_ns"]) is not int or document["duration_ns"] < 0:
        raise RuntimeError("invalid_cold_sample")
    return document["duration_ns"]


def _qualification_reasons(
    environment: dict,
    *,
    quick: bool,
    reference_digest: str | None,
    reference_attestation: dict | None,
) -> list[str]:
    reasons = reference.identity_qualification_reasons(
        environment["identity"],
        reference_digest=reference_digest,
        environment_digest=environment["environment_digest"],
        attestation=reference_attestation,
    )
    if quick:
        reasons.append("quick_test_shape")
    if (
        environment["background_sample_seconds"] < 60
        or environment["background_system_cpu_ratio"] is None
        or environment["background_system_cpu_ratio"] >= .05
    ):
        reasons.append("background_cpu_not_qualified")
    if environment["disk_free_bytes"] < 5 * environment["dataset_bytes"]:
        reasons.append("insufficient_disk_space")
    return reasons


def _apply_result_gates(results: dict, *, threshold_enforced: bool, batches: int) -> None:
    for result in results.values():
        if not threshold_enforced:
            result["passed"] = None
            continue
        modes_complete = all(
            len(mode["batches"]) == batches and all(batch["samples"] > 0 for batch in mode["batches"])
            for mode in result["modes"].values()
        )
        threshold = result["threshold_ms"]
        under_threshold = threshold is None or all(
            mode["median_batch_p95_ms"] is not None
            and mode["median_batch_p95_ms"] < threshold
            for mode in result["modes"].values()
        )
        result["passed"] = bool(modes_complete and under_threshold)


def run(
    repo: Path,
    *,
    quick: bool,
    reference_digest: str | None,
    reference_attestation: dict | None = None,
) -> dict:
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise RuntimeError("PYTHONHASHSEED must be explicitly set to 0 before interpreter startup")
    shape = (Shape(40, 5, 4, 5, 3, 3, 1, 1, 4, 3, 4, 128) if quick
             else Shape(10000, 1000, 100, 100, 30, 20, 3, 5, 100, 1000, 100, 2048))
    environment = _environment(
        .2 if quick else 60, reference_attestation=reference_attestation,
    )
    results, datasets, errors = {}, {}, []
    with tempfile.TemporaryDirectory(prefix="koawa-stability-benchmark-") as directory:
        root = Path(directory)
        resource_started = time.perf_counter()
        resource_samples = [resources.snapshot(0)]
        definitions = {
            "execution": (scenarios.seed_execution, (shape.event_count,)),
            "mailbox": (scenarios.seed_agents, (shape.mailbox_agents, shape.mailbox_events)),
            "wait": (scenarios.seed_agents, (shape.wait_agents, 0)),
            "spawn": (scenarios.seed_spawn, ()),
        }
        for name, (seed, arguments) in definitions.items():
            datasets[name] = seed(root / f"{name}.sqlite3", *arguments)
        capacity = min(8, max(1, shape.spawn_workers // 2))
        datasets["load_spawn"] = load.seed_load(root / "load_spawn.sqlite3", capacity=capacity, budget=capacity)
        datasets["load_heartbeat"] = load.seed_load(root / "load_heartbeat.sqlite3", capacity=1, budget=1)
        pragmas = scenarios.pragmas(scenarios.store_at(root / "execution.sqlite3"))
        environment["dataset_bytes"] = sum(path.stat().st_size for path in root.iterdir() if path.is_file())
        environment["disk_free_bytes"] = shutil.disk_usage(root).free
        definitions = {
            "verified_event_rebuild_10k": "execution", "mailbox_next": "mailbox",
            "mailbox_list": "mailbox", "agent_list": "mailbox",
            "wait_agents_100": "wait", "uncontended_spawn_transaction": "spawn",
        }
        for name, dataset in definitions.items():
            source = root / f"{dataset}.sqlite3"
            manifest = datasets[dataset]
            modes = ("write",) if name == "uncontended_spawn_transaction" else ("cold", "warm")
            results[name] = {"threshold_ms": THRESHOLDS_MS.get(name), "modes": {}, "passed": None}
            for mode in modes:
                batches = []
                for batch in range(shape.batches):
                    values = []
                    count = shape.rebuild_samples if dataset == "execution" else shape.samples
                    try:
                        for index in range(-shape.warmups, count):
                            if mode == "write":
                                target = root / f"sample-{batch}-{index}.sqlite3"
                                scenarios.clone_database(source, target)
                                if scenarios.event_digest(scenarios.store_at(target)) != manifest["dataset_digest"]:
                                    raise RuntimeError("benchmark_seed_copy_mismatch")
                                duration = _timed(scenarios.operation(name, target, manifest))
                            elif mode == "cold":
                                duration = _cold_sample(name, source, manifest)
                            else:
                                duration = _timed(scenarios.operation(name, source, manifest))
                            if index >= 0:
                                values.append(duration)
                        batches.append(summary(values))
                    except Exception as exc:
                        errors.append({"scenario": name, "mode": mode, "batch": batch,
                                       "error": type(exc).__name__, "code": getattr(exc, "code", None)})
                        break
                p95s = [item["p95_ms"] for item in batches]
                results[name]["modes"][mode] = {
                    "batches": batches,
                    "median_batch_p95_ms": statistics.median(p95s) if p95s else None,
                }
        load_cases = {
            "concurrent_spawn_100": ("load_spawn", lambda path: load.concurrent_spawn(
                path, workers=shape.spawn_workers, capacity=capacity, budget=capacity)),
            "heartbeat_takeover_1000": ("load_heartbeat", lambda path: load.heartbeat_takeover(
                path, cycles=shape.heartbeat_cycles)),
            "mcp_pending_100_notification_storm": (None, lambda path: load.mcp_pending_storm(
                pending=shape.mcp_pending, notifications=shape.mcp_notifications)),
        }
        for name, (dataset, execute) in load_cases.items():
            batches, observations = [], []
            for batch in range(shape.batches):
                values, observed = [], []
                try:
                    for index in range(-shape.warmups, shape.samples):
                        target = root / f"{name}-{batch}-{index}.sqlite3"
                        if dataset is not None:
                            scenarios.clone_database(root / f"{dataset}.sqlite3", target)
                            if scenarios.event_digest(scenarios.store_at(target)) != datasets[dataset]["dataset_digest"]:
                                raise RuntimeError("benchmark_load_seed_copy_mismatch")
                        result = execute(target)
                        if index >= 0:
                            values.append(result["duration_ns"])
                            observed.append(result)
                    batches.append(summary(values))
                    observations.append(observed)
                except Exception as exc:
                    errors.append({"scenario": name, "mode": "load", "batch": batch,
                                   "error": type(exc).__name__, "code": getattr(exc, "code", None)})
                    break
            p95s = [item["p95_ms"] for item in batches]
            results[name] = {"threshold_ms": None, "passed": None, "modes": {"load": {
                "batches": batches, "observations_by_batch": observations,
                "median_batch_p95_ms": statistics.median(p95s) if p95s else None,
            }}}
        resource_samples.append(resources.snapshot(time.perf_counter() - resource_started,
                                                    tuple(root.glob("*.sqlite3"))))
    reasons = _qualification_reasons(
        environment,
        quick=quick,
        reference_digest=reference_digest,
        reference_attestation=reference_attestation,
    )
    environment_qualified = not reasons
    threshold_enforced = environment_qualified and not quick
    _apply_result_gates(results, threshold_enforced=threshold_enforced, batches=shape.batches)
    document = {
        "protocol_version": PROTOCOL_VERSION, "measurement_mode": "quick_test" if quick else "full",
        "commit": _git_commit(repo), "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": environment, "reference_environment_digest": reference_digest,
        "environment_qualified": environment_qualified, "qualification_reasons": reasons,
        "threshold_enforced": threshold_enforced, "release_pass": None,
        "reference_attestation_digest": (
            environment["identity"].get("reference_attestation_digest")
        ),
        "reference_attestation": reference_attestation,
        "dataset": {"seed": DATA_SEED, **asdict(shape)}, "datasets": datasets,
        "dataset_digest": hashlib.sha256(canonical_bytes(datasets)).hexdigest(),
        "sqlite_pragmas": pragmas, "results": results, "errors": errors,
        "pending_scenarios": list(PENDING_SCENARIOS),
        "resource_growth": {"collection_kind": "benchmark_endpoints_not_soak",
                            "samples": resource_samples, "soak_hours": 0, "passed": None,
                            "assessment": resources.assess_soak(resource_samples)},
    }
    document["report_digest"] = hashlib.sha256(canonical_bytes(document)).hexdigest()
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--reference-environment-digest")
    parser.add_argument("--reference-attestation", type=Path)
    parser.add_argument("--sample", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.sample:
        request = json.load(sys.stdin)
        duration = _timed(scenarios.operation(request["name"], Path(request["path"]), request["manifest"]))
        print(json.dumps({"duration_ns": duration}))
        return 0
    if arguments.report is None:
        parser.error("--report is required")
    attestation = (
        reference.load_attestation(arguments.reference_attestation)
        if arguments.reference_attestation is not None else None
    )
    document = run(
        REPO,
        quick=arguments.quick,
        reference_digest=arguments.reference_environment_digest,
        reference_attestation=attestation,
    )
    atomic_json(arguments.report, document)
    print(json.dumps({"report": str(arguments.report.resolve()), "digest": document["report_digest"],
                      "threshold_enforced": document["threshold_enforced"], "errors": document["errors"]}, sort_keys=True))
    return 1 if document["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
