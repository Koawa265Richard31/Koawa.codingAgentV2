"""Bounded subprocess pipe transport for workspace Git/container helpers."""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from ..agents.graph import AgentError


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def run_bounded(
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None,
    timeout: float,
    output_limit: int,
    failure_code: str,
    output_limit_code: str,
    input_bytes: bytes | None = None,
) -> BoundedProcessResult:
    if output_limit < 1 or timeout <= 0:
        raise ValueError("invalid subprocess bounds")
    try:
        process = subprocess.Popen(
            list(arguments), cwd=cwd, env=environment,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        raise AgentError(failure_code) from None
    values = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    pipe_error = threading.Event()

    def drain(name, pipe):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    return
                if len(values[name]) + len(chunk) > output_limit:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return
                values[name].extend(chunk)
        except (OSError, ValueError):
            pipe_error.set()

    def write_input():
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except BrokenPipeError:
            return
        except (OSError, ValueError):
            pipe_error.set()

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    writer = None
    if input_bytes is not None:
        writer = threading.Thread(target=write_input, daemon=True)
    for thread in readers:
        thread.start()
    if writer is not None:
        writer.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise AgentError(failure_code) from None
    finally:
        for thread in readers:
            thread.join(timeout=5)
        if writer is not None:
            writer.join(timeout=5)
        for pipe in (process.stdout, process.stderr):
            pipe.close()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
    if any(thread.is_alive() for thread in readers) or (writer is not None and writer.is_alive()):
        raise AgentError(failure_code)
    if overflow.is_set():
        raise AgentError(output_limit_code)
    if pipe_error.is_set():
        raise AgentError(failure_code)
    return BoundedProcessResult(
        process.returncode, bytes(values["stdout"]), bytes(values["stderr"]),
    )


__all__ = ["BoundedProcessResult", "run_bounded"]
