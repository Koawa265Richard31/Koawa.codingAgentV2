"""D25 W2: trusted Docker primitives shared by long-lived MCP containers.

These helpers deliberately reuse the D8 batch-runner's in-place trusted
helpers (``_run_cli`` / ``_resolve_executable`` / ``_remove_exact``) instead
of copying them, and add the long-lived-container contracts D25 needs:
an interactive no-TTY create argv builder with a zero-mount policy, and an
exact inspect-contract validator.  Docker argv is always produced here -
never by model/MCP-supplied values (§4.5).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime import SandboxError

# Reuse the D8 trusted helpers in place; they are package-internal contracts.
from .runtime import _remove_exact, _resolve_executable, _run_cli  # noqa: F401

_USER = "65532:65532"
_TMPFS_MOUNT = "/tmp"


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """Everything needed to create one long-lived MCP container."""

    image_id: str
    argv: tuple[str, ...]
    container_working_directory: str
    environment: tuple[tuple[str, str], ...]
    cpus: float
    memory_bytes: int
    pids_limit: int
    tmpfs_bytes: int
    container_name: str
    labels: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("image_id", self.image_id),
            ("container_working_directory", self.container_working_directory),
            ("container_name", self.container_name),
        ):
            if not isinstance(value, str) or not value:
                raise SandboxError("mcp_container_spec_invalid")
        if not isinstance(self.argv, tuple) or not self.argv or not all(
            isinstance(item, str) and item for item in self.argv
        ):
            raise SandboxError("mcp_container_spec_invalid")
        if not self.argv[0].startswith("/") or "\\" in self.argv[0]:
            raise SandboxError("mcp_container_spec_invalid")
        if not isinstance(self.environment, tuple) or any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not isinstance(pair[1], str)
            for pair in self.environment
        ):
            raise SandboxError("mcp_container_spec_invalid")
        if not isinstance(self.labels, tuple) or any(
            not isinstance(pair, tuple) or len(pair) != 2 for pair in self.labels
        ):
            raise SandboxError("mcp_container_spec_invalid")
        for value in (self.memory_bytes, self.pids_limit, self.tmpfs_bytes):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SandboxError("mcp_container_spec_invalid")
        if (
            not isinstance(self.cpus, (int, float))
            or isinstance(self.cpus, bool)
            or self.cpus <= 0
        ):
            raise SandboxError("mcp_container_spec_invalid")


def create_arguments(spec: ContainerSpec) -> tuple[str, ...]:
    """Trusted create argv: zero mounts, no TTY, interactive stdin, non-root.

    Differences from the D8 batch runner are deliberate: no workspace bind
    (D25 zero-mount invariant), ``--interactive`` so the MCP server keeps a
    usable stdin across the session.
    """
    values = [
        "container",
        "create",
        "--name",
        spec.container_name,
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--user",
        _USER,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(spec.pids_limit),
        "--cpus",
        format(spec.cpus, "g"),
        "--memory",
        str(spec.memory_bytes),
        "--memory-swap",
        str(spec.memory_bytes),
        "--tmpfs",
        (
            f"{_TMPFS_MOUNT}:rw,noexec,nosuid,nodev,"
            f"size={spec.tmpfs_bytes},mode=1777"
        ),
        "--workdir",
        spec.container_working_directory,
        "--interactive",
        "--init",
        "--log-driver",
        "none",
    ]
    for name, value in spec.labels:
        values.extend(("--label", f"{name}={value}"))
    for name, value in spec.environment:
        values.extend(("--env", f"{name}={value}"))
    values.extend(
        (
            "--entrypoint",
            spec.argv[0],
            spec.image_id,
            *spec.argv[1:],
        )
    )
    return tuple(values)


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else value


def inspect_and_validate(
    document: Mapping[str, Any],
    spec: ContainerSpec,
) -> None:
    """Exact-contract comparison of one ``docker container inspect`` document.

    Any missing, extra or tampered field is a contract mismatch: the caller
    must never hand back a usable endpoint for a container that does not
    match the trusted spec byte-for-byte on security-relevant fields.
    """
    def _fail() -> None:
        raise SandboxError("mcp_container_contract_mismatch")

    config = document.get("Config")
    host = document.get("HostConfig")
    if not isinstance(config, Mapping) or not isinstance(host, Mapping):
        _fail()
    if config.get("Image") != spec.image_id:
        _fail()
    if config.get("Tty") is not False or config.get("OpenStdin") is not True:
        _fail()
    if _first(config.get("Entrypoint")) != spec.argv[0]:
        _fail()
    cmd = config.get("Cmd")
    expected_cmd = list(spec.argv[1:])
    if cmd != expected_cmd and (cmd is None and expected_cmd):
        _fail()
    if isinstance(cmd, list) and cmd != expected_cmd:
        _fail()
    if config.get("WorkingDir") != spec.container_working_directory:
        _fail()
    if config.get("User") != _USER:
        _fail()
    labels = config.get("Labels")
    if not isinstance(labels, Mapping):
        _fail()
    for name, value in spec.labels:
        if labels.get(name) != value:
            _fail()
    if host.get("NetworkMode") != "none":
        _fail()
    if host.get("ReadonlyRootfs") is not True:
        _fail()
    cap_drop = host.get("CapDrop")
    if cap_drop != ["ALL"]:
        _fail()
    security_opt = host.get("SecurityOpt") or []
    if "no-new-privileges" not in security_opt:
        _fail()
    if host.get("Memory") != spec.memory_bytes:
        _fail()
    if host.get("MemorySwap") != spec.memory_bytes:
        _fail()
    nano_cpus = host.get("NanoCpus")
    if not isinstance(nano_cpus, int) or nano_cpus != int(spec.cpus * 1_000_000_000):
        _fail()
    if host.get("PidsLimit") != spec.pids_limit:
        _fail()
    mounts = host.get("Mounts")
    if mounts not in (None, [],) or config.get("Mounts") not in (None, []):
        _fail()


def stop_and_remove(
    docker: Path,
    container_id: str,
    *,
    stop_timeout_seconds: float,
    cli_timeout_seconds: float,
) -> None:
    """Bounded stop + exact remove; failure raises (never silently ignored)."""
    stop = _run_cli(
        docker,
        ("container", "stop", "--time", str(int(max(1, round(stop_timeout_seconds)))), container_id),
        timeout_seconds=cli_timeout_seconds,
    )
    if stop.returncode != 0:
        raise SandboxError("mcp_container_stop_failed")
    if not _remove_exact(docker, container_id):
        raise SandboxError("mcp_container_remove_failed")
