"""Stdlib OS resource measurements and the I8 24h soak oracle.

Linux and Windows report actual resident memory, OS thread counts and open
handles/fds. Unsupported platforms fail explicitly, never substitute zeros.
"""
from __future__ import annotations

import math
import os
import statistics
from pathlib import Path


SAMPLE_KEYS = frozenset({"elapsed_seconds", "rss_bytes", "threads", "handles_or_fds", "db_bytes", "wal_bytes"})
SOAK_SECONDS = 24 * 3600
WARMUP_SECONDS = 30 * 60
SAMPLE_INTERVAL_SECONDS = 60


def _windows_metrics() -> tuple[int, int, int]:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel.GetCurrentProcess()

    class Memory(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("peak_rss", "rss", "peak_paged", "paged",
                                                "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]
    memory = Memory()
    memory.cb = ctypes.sizeof(memory)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(process, ctypes.byref(memory), memory.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    handles = wintypes.DWORD()
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    if not kernel.GetProcessHandleCount(process, ctypes.byref(handles)):
        raise ctypes.WinError(ctypes.get_last_error())

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("thread_id", wintypes.DWORD), ("owner_id", wintypes.DWORD),
                    ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                    ("flags", wintypes.DWORD)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    count = 0
    try:
        entry = ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        found = kernel.Thread32First(snapshot, ctypes.byref(entry))
        if not found:
            raise ctypes.WinError(ctypes.get_last_error())
        while found:
            if entry.owner_id == os.getpid():
                count += 1
            found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(snapshot)
    if count < 1:
        raise RuntimeError("resource_thread_count_missing")
    return int(memory.rss), count, handles.value


def _linux_metrics() -> tuple[int, int, int]:
    status = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            status[key] = value.strip()
    rss = int(status["VmRSS"].split()[0]) * 1024
    threads = int(status["Threads"])
    fds = 0
    for item in os.listdir("/proc/self/fd"):
        try:
            os.readlink(f"/proc/self/fd/{item}")
        except FileNotFoundError:
            continue  # directory enumeration's own transient fd
        fds += 1
    return rss, threads, fds


def snapshot(elapsed_seconds: float, databases: tuple[Path, ...] = ()) -> dict:
    if isinstance(elapsed_seconds, bool) or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError("invalid resource sample time")
    if os.name == "nt":
        rss, threads, handles = _windows_metrics()
    elif Path("/proc/self/status").is_file():
        rss, threads, handles = _linux_metrics()
    else:
        raise RuntimeError("resource_sampling_unsupported")
    def size(path):
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0
    return {"elapsed_seconds": elapsed_seconds, "rss_bytes": rss, "threads": threads,
            "handles_or_fds": handles, "db_bytes": sum(size(path) for path in databases),
            "wal_bytes": sum(size(Path(str(path) + "-wal")) for path in databases)}


def least_squares_slope(times: list[float], values: list[float]) -> float:
    if len(times) != len(values) or len(times) < 2 or any(not math.isfinite(x) for x in (*times, *values)):
        raise ValueError("invalid regression samples")
    mean_x, mean_y = statistics.mean(times), statistics.mean(values)
    variance = sum((value - mean_x) ** 2 for value in times)
    if variance == 0:
        raise ValueError("regression requires distinct sample times")
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(times, values)) / variance


def assess_soak(samples: list[dict], *, reference_qualified: bool = False) -> dict:
    if type(reference_qualified) is not bool:
        raise ValueError("reference_qualified must be bool")
    for sample in samples:
        if set(sample) != SAMPLE_KEYS:
            raise ValueError("invalid resource sample keys")
        for key, value in sample.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid resource sample value")
            if key != "elapsed_seconds" and type(value) is not int:
                raise ValueError("resource counts must be integers")
    times = [sample["elapsed_seconds"] for sample in samples]
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError("resource times must be strictly increasing")
    baseline = [sample for sample in samples if sample["elapsed_seconds"] <= WARMUP_SECONDS]
    measured = [sample for sample in samples if sample["elapsed_seconds"] >= WARMUP_SECONDS]
    reasons = []
    if not times or times[0] > 1 or times[-1] < SOAK_SECONDS:
        reasons.append("duration_under_24h_or_missing_start")
    if not baseline or len(measured) < 2:
        reasons.append("warmup_or_measured_samples_missing")
    if any(right - left > SAMPLE_INTERVAL_SECONDS + 1 for left, right in zip(times, times[1:])):
        reasons.append("resource_sampling_gap")
    # Each scheduled observation must be on the minute grid (one-second
    # scheduler tolerance). Dense/adaptive samples bias medians and slopes,
    # even when none of their gaps exceeds 60 seconds. Permit only one extra
    # final endpoint, after the 24h measurement and before the next minute.
    regular = times
    if len(times) >= 2 and times[-2] >= SOAK_SECONDS - 1 and 0 < times[-1] - times[-2] < SAMPLE_INTERVAL_SECONDS - 1:
        regular = times[:-1]
    if any(abs(elapsed - index * SAMPLE_INTERVAL_SECONDS) > 1
           for index, elapsed in enumerate(regular)):
        reasons.append("resource_sampling_cadence")
    metrics = {}
    if baseline and len(measured) >= 2:
        hours = [sample["elapsed_seconds"] / 3600 for sample in measured]
        for key, divisor, slope_limit, delta_limit in (
            ("rss_bytes", 1024 ** 2, 1, 64), ("threads", 1, .1, 2), ("handles_or_fds", 1, .1, 8),
        ):
            warmup_median = statistics.median(sample[key] / divisor for sample in baseline)
            values = [sample[key] / divisor for sample in measured]
            slope = least_squares_slope(hours, values)
            delta = values[-1] - warmup_median
            metrics[key] = {"warmup_median": warmup_median, "end": values[-1], "absolute_delta": delta,
                            "slope_per_hour": slope, "slope_limit": slope_limit, "delta_limit": delta_limit,
                            "within_limits": slope <= slope_limit and delta <= delta_limit}
    complete = not reasons
    enforced = complete and reference_qualified
    observed = all(metric["within_limits"] for metric in metrics.values()) if metrics else None
    return {"protocol_complete": complete, "qualification_reasons": reasons,
            "soak_hours": times[-1] / 3600 if times else 0,
            "warmup_seconds": WARMUP_SECONDS, "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
            "sample_count": len(samples), "metrics": metrics, "observed_within_limits": observed,
            "threshold_enforced": enforced, "passed": observed if enforced else None}
