"""D8: run trusted profiles in a real, recoverable Docker sandbox.

Run from ``v2/`` with::

    $env:PYTHONPATH = "src"
    py -3.14 -B examples/day08_docker_sandbox.py

The public path runs a security probe and then starts this file in a hidden
child mode. That child terminates immediately after ``docker create`` but
before the container ID is bound to the allocation stream. The parent opens a
fresh Event Store and reaper, finds the container by its exact durable intent
and labels, and removes it by its complete ID.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn, Sequence
from uuid import UUID, uuid4

from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.sandbox import (
    AllocationState,
    ContainerReaper,
    DockerCommandRunner,
    DockerSandboxDoctor,
    MANAGED_LABEL,
    MANAGED_LABEL_VALUE,
    SandboxAllocationStore,
    SandboxCommandProfile,
    SandboxLimits,
)


DEFAULT_IMAGE_ID = (
    "sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"
)
HOST_SECRET_NAME = "KOAWA_D8_HOST_SECRET"
MANAGED_FILTER = f"label={MANAGED_LABEL}={MANAGED_LABEL_VALUE}"
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")


PROBE_SCRIPT = r"""
import json
import os
import pathlib
import socket


def can_write(path, value):
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(value)
        return True
    except OSError:
        return False


def network_reachable():
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=0.25):
            return True
    except OSError:
        return False


facts = {
    "uid": os.getuid(),
    "gid": os.getgid(),
    "cwd": os.getcwd(),
    "host_secret": os.environ.get("KOAWA_D8_HOST_SECRET"),
    "host_home": os.environ.get("USERPROFILE"),
    "docker_socket_visible": pathlib.Path("/var/run/docker.sock").exists(),
    "outside_visible": pathlib.Path("/outside-sentinel.txt").exists(),
    "network_reachable": network_reachable(),
    "rootfs_writable": can_write("/etc/koawa-d8-probe", "x"),
    "workspace_writable": can_write("/workspace/fixture.txt", "x"),
    "git_writable": can_write("/workspace/.git/config", "x"),
    "tmp_writable": can_write("/tmp/koawa-d8-probe", "x"),
}
print(json.dumps(facts, sort_keys=True))
"""


def _docker_executable() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("docker_executable_unavailable")
    return str(Path(executable).resolve(strict=True))


def _run_docker(
    docker_executable: str,
    *arguments: str,
    timeout_seconds: float = 30.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        (docker_executable, *arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        shell=False,
    )
    if check and completed.returncode != 0:
        command = arguments[0] if arguments else "unknown"
        raise RuntimeError(f"docker_{command}_failed:{completed.stderr.strip()}")
    return completed


def _docker_json(docker_executable: str, *arguments: str) -> Any:
    completed = _run_docker(docker_executable, *arguments)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("docker_invalid_json") from error


def _require_container_id(value: str) -> str:
    if _CONTAINER_ID.fullmatch(value) is None:
        raise RuntimeError("docker_incomplete_container_id")
    return value


def _managed_container_ids(docker_executable: str) -> set[str]:
    completed = _run_docker(
        docker_executable,
        "container",
        "ls",
        "--all",
        "--quiet",
        "--no-trunc",
        "--filter",
        MANAGED_FILTER,
    )
    return {
        _require_container_id(value.strip())
        for value in completed.stdout.splitlines()
        if value.strip()
    }


def _inspect_container(
    docker_executable: str,
    container_id: str,
) -> dict[str, Any]:
    values = _docker_json(
        docker_executable,
        "container",
        "inspect",
        _require_container_id(container_id),
    )
    if not isinstance(values, list) or len(values) != 1:
        raise RuntimeError("docker_inspect_cardinality")
    value = values[0]
    if not isinstance(value, dict) or value.get("Id") != container_id:
        raise RuntimeError("docker_inspect_identity_mismatch")
    return value


def _container_exists(docker_executable: str, container_id: str) -> bool:
    completed = _run_docker(
        docker_executable,
        "container",
        "inspect",
        _require_container_id(container_id),
        check=False,
    )
    return completed.returncode == 0


def _workspace_mount_is_read_only(inspected: dict[str, Any]) -> bool:
    mounts = [
        mount
        for mount in inspected.get("Mounts", [])
        if mount.get("Destination") == "/workspace"
    ]
    return (
        len(mounts) == 1
        and mounts[0].get("Type") == "bind"
        and mounts[0].get("RW") is False
    )


def _assert_probe(
    facts: dict[str, Any],
    inspected: dict[str, Any],
) -> None:
    host = inspected["HostConfig"]
    config = inspected["Config"]
    required = {
        "nonroot": facts.get("uid") == 65532 and facts.get("gid") == 65532,
        "network_none": (
            host.get("NetworkMode") == "none"
            and facts.get("network_reachable") is False
        ),
        "readonly_rootfs": (
            host.get("ReadonlyRootfs") is True
            and facts.get("rootfs_writable") is False
        ),
        "readonly_workspace": (
            _workspace_mount_is_read_only(inspected)
            and facts.get("workspace_writable") is False
            and facts.get("git_writable") is False
        ),
        "tmpfs_writable": (
            "/tmp" in host.get("Tmpfs", {})
            and facts.get("tmp_writable") is True
        ),
        "host_secret_not_inherited": (
            facts.get("host_secret") is None
            and not any(
                value.startswith(f"{HOST_SECRET_NAME}=")
                for value in config.get("Env", [])
            )
        ),
        "host_home_not_inherited": facts.get("host_home") is None,
        "docker_socket_absent": facts.get("docker_socket_visible") is False,
        "outside_path_absent": facts.get("outside_visible") is False,
    }
    failed = sorted(name for name, passed in required.items() if not passed)
    if failed:
        raise RuntimeError(f"sandbox_probe_failed:{','.join(failed)}")


def _crash_child(arguments: Sequence[str]) -> NoReturn:
    if len(arguments) != 6:
        raise SystemExit("invalid hidden child arguments")
    database = Path(arguments[0])
    workspace = Path(arguments[1])
    docker_executable = arguments[2]
    image_id = arguments[3]
    signal_path = Path(arguments[4])
    execution_id = UUID(arguments[5])

    def die_after_create(point, allocation, container_id) -> None:
        if point != "after_create_before_bind":
            return
        if container_id is None:
            os._exit(72)
        signal_path.write_text(
            json.dumps(
                {
                    "allocation_id": str(allocation.allocation_id),
                    "container_id": container_id,
                },
                sort_keys=True,
            ),
            encoding="ascii",
        )
        os._exit(73)

    profile = SandboxCommandProfile(
        "crash-recovery",
        (
            "/usr/local/bin/python",
            "-c",
            "import time;time.sleep(60)",
        ),
        timeout_seconds=30.0,
        environment=(("PYTHONDONTWRITEBYTECODE", "1"), ("TZ", "UTC")),
    )
    runner = DockerCommandRunner(
        workspace,
        (profile,),
        SqliteEventStore(database),
        image_id,
        docker_executable=docker_executable,
        fault_hook=die_after_create,
    )
    runner.run("crash-recovery", execution_id=execution_id)
    raise SystemExit("hidden child did not crash at fault hook")


def _cleanup_owned(
    docker_executable: str,
    owned_container_ids: set[str],
) -> tuple[str, ...]:
    failures: list[str] = []
    for container_id in sorted(owned_container_ids):
        if not _container_exists(docker_executable, container_id):
            continue
        completed = _run_docker(
            docker_executable,
            "container",
            "rm",
            "--force",
            container_id,
            check=False,
        )
        if completed.returncode != 0:
            failures.append(container_id)
    return tuple(failures)


def main() -> int:
    image_id = os.environ.get("KOAWA_D8_IMAGE_ID", DEFAULT_IMAGE_ID)
    try:
        docker_executable = _docker_executable()
    except RuntimeError as error:
        print(f"D8 doctor failed: {error}", file=sys.stderr)
        return 2
    doctor = DockerSandboxDoctor(docker_executable).check(image_id)
    if not doctor.ready:
        print(
            f"D8 doctor failed: {doctor.error_code or 'docker_doctor_not_ready'}",
            file=sys.stderr,
        )
        return 2

    baseline = _managed_container_ids(docker_executable)
    owned_container_ids: set[str] = set()
    output: dict[str, Any] = {"doctor": doctor.to_document()}
    run_error: BaseException | None = None
    cleanup_failures: tuple[str, ...] = ()
    probe_owner_id: UUID | None = None
    crash_execution_id: UUID | None = None
    temporary = tempfile.TemporaryDirectory(prefix="koawa-d8-example-")
    try:
        base = Path(temporary.name)
        workspace = base / "workspace"
        controller = base / "controller"
        workspace.mkdir()
        controller.mkdir()
        (workspace / "fixture.txt").write_text("fixture", encoding="utf-8")
        (workspace / ".git").mkdir()
        (workspace / ".git" / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n",
            encoding="utf-8",
        )
        (base / "outside-sentinel.txt").write_text(
            "host-only", encoding="utf-8"
        )
        output["storage_boundary"] = {
            "workspace": str(workspace),
            "controller_database": str(controller / "events.sqlite3"),
            "database_outside_workspace": (
                workspace.resolve(strict=True)
                not in (controller / "events.sqlite3").resolve().parents
            ),
        }

        encoded_probe = base64.b64encode(
            PROBE_SCRIPT.encode("utf-8")
        ).decode("ascii")
        profile = SandboxCommandProfile(
            "security-probe",
            (
                "/usr/local/bin/python",
                "-I",
                "-c",
                f"import base64;exec(base64.b64decode('{encoded_probe}'))",
            ),
            timeout_seconds=10.0,
            max_stdout_bytes=32_768,
            max_stderr_bytes=32_768,
            environment=(
                ("PYTHONDONTWRITEBYTECODE", "1"),
                ("TZ", "UTC"),
            ),
        )
        observed: dict[str, Any] = {}

        def inspect_after_create(point, allocation, container_id) -> None:
            del allocation
            if point != "after_create_before_bind":
                return
            if container_id is None:
                raise RuntimeError("probe_container_id_missing")
            owned_container_ids.add(_require_container_id(container_id))
            observed["inspect"] = _inspect_container(
                docker_executable, container_id
            )

        store = SqliteEventStore(controller / "events.sqlite3")
        runner = DockerCommandRunner(
            workspace,
            (profile,),
            store,
            image_id,
            docker_executable=docker_executable,
            limits=SandboxLimits(
                cpus=0.5,
                memory_bytes=64 * 1024 * 1024,
                pids_limit=32,
                tmpfs_bytes=8 * 1024 * 1024,
            ),
            fault_hook=inspect_after_create,
        )
        previous_secret = os.environ.get(HOST_SECRET_NAME)
        os.environ[HOST_SECRET_NAME] = "host-value-must-not-enter-container"
        try:
            result = runner.run("security-probe", execution_id=uuid4())
        finally:
            if previous_secret is None:
                os.environ.pop(HOST_SECRET_NAME, None)
            else:
                os.environ[HOST_SECRET_NAME] = previous_secret
        if not result.passed:
            raise RuntimeError(f"sandbox_probe_outcome:{result.outcome.value}")
        facts = json.loads(result.stdout)
        inspected = observed["inspect"]
        _assert_probe(facts, inspected)
        if result.allocation_id is None:
            raise RuntimeError("probe_allocation_id_missing")
        probe_allocation = runner.allocation_store.load(result.allocation_id)
        if (
            probe_allocation is None
            or probe_allocation.state is not AllocationState.RELEASED
        ):
            raise RuntimeError("probe_allocation_not_released")
        probe_owner_id = probe_allocation.owner_execution_id
        output["security_probe"] = {
            "backend": result.backend,
            "image_id": result.immutable_image_id,
            "profile_id": result.profile_id,
            "profile_digest": result.profile_digest,
            "allocation_id": str(result.allocation_id),
            "container_id": result.container_id,
            "allocation_state": probe_allocation.state.value,
            "outcome": result.outcome.value,
            "facts": facts,
            "container_policy": {
                "user": inspected["Config"].get("User"),
                "network_mode": inspected["HostConfig"].get("NetworkMode"),
                "readonly_rootfs": inspected["HostConfig"].get(
                    "ReadonlyRootfs"
                ),
                "workspace_readonly": _workspace_mount_is_read_only(
                    inspected
                ),
                "tmpfs": inspected["HostConfig"].get("Tmpfs", {}).get(
                    "/tmp"
                ),
            },
        }

        crash_database = controller / "crash.sqlite3"
        crash_signal = controller / "crash-container.json"
        crash_execution_id = uuid4()
        child = subprocess.run(
            (
                sys.executable,
                "-B",
                str(Path(__file__).resolve(strict=True)),
                "--crash-child",
                str(crash_database),
                str(workspace),
                docker_executable,
                image_id,
                str(crash_signal),
                str(crash_execution_id),
            ),
            cwd=Path(__file__).resolve(strict=True).parent.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45.0,
            check=False,
            shell=False,
        )
        if crash_signal.is_file():
            crash_identity = json.loads(crash_signal.read_text(encoding="ascii"))
            crash_container_id = _require_container_id(
                crash_identity["container_id"]
            )
            owned_container_ids.add(crash_container_id)
        else:
            raise RuntimeError(
                f"crash_child_signal_missing:exit={child.returncode}:"
                f"{child.stderr.strip()}"
            )
        if child.returncode != 73:
            raise RuntimeError(
                f"crash_child_exit:{child.returncode}:{child.stderr.strip()}"
            )

        # These are new objects in the parent, backed only by the child-written DB.
        restarted_store = SandboxAllocationStore(
            SqliteEventStore(crash_database)
        )
        open_allocations = restarted_store.list_open()
        if len(open_allocations) != 1:
            raise RuntimeError("crash_allocation_cardinality")
        intended = open_allocations[0]
        if (
            intended.state is not AllocationState.INTENDED
            or intended.container_id is not None
            or intended.owner_execution_id != crash_execution_id
            or str(intended.allocation_id) != crash_identity["allocation_id"]
        ):
            raise RuntimeError("crash_intent_not_durable")
        crash_inspect = _inspect_container(
            docker_executable, crash_container_id
        )
        label_match = (
            crash_inspect["Config"].get("Labels", {})
            == dict(intended.expected_labels)
        )
        if not label_match:
            raise RuntimeError("crash_container_labels_mismatch")

        reaper = ContainerReaper(restarted_store, docker_executable)
        reap_report = reaper.reap(
            owner_execution_id=crash_execution_id,
            force=True,
        )
        recovered = restarted_store.load(intended.allocation_id)
        if (
            reap_report.removed != (crash_container_id,)
            or reap_report.released != (intended.allocation_id,)
            or reap_report.refused
            or reap_report.errors
            or recovered is None
            or recovered.state is not AllocationState.RELEASED
            or recovered.container_id != crash_container_id
            or _container_exists(docker_executable, crash_container_id)
        ):
            raise RuntimeError("crash_recovery_failed")
        output["create_before_bind_recovery"] = {
            "child_exit_code": child.returncode,
            "before_restart": {
                "allocation_id": str(intended.allocation_id),
                "state": intended.state.value,
                "bound_container_id": intended.container_id,
                "container_id_from_exact_labels": crash_container_id,
                "labels_match_intent": label_match,
            },
            "reaper": reap_report.to_document(),
            "after_reap": {
                "state": recovered.state.value,
                "outcome": recovered.outcome,
                "container_id": recovered.container_id,
                "container_exists": False,
            },
        }
    except BaseException as error:
        run_error = error
    finally:
        # Learn only our exact owner-labelled IDs if a failure happened before a
        # fault hook could report them. Unrelated managed containers are never removed.
        try:
            owner_ids = {
                str(value)
                for value in (crash_execution_id, probe_owner_id)
                if value is not None
            }
            for container_id in _managed_container_ids(docker_executable) - baseline:
                inspected = _inspect_container(docker_executable, container_id)
                labels = inspected["Config"].get("Labels", {})
                if labels.get("io.koawa.v2.owner") in owner_ids:
                    owned_container_ids.add(container_id)
            cleanup_failures = _cleanup_owned(
                docker_executable, owned_container_ids
            )
        finally:
            final_managed = _managed_container_ids(docker_executable)
            output["managed_containers"] = {
                "baseline_count": len(baseline),
                "final_count": len(final_managed),
                "no_new_managed_containers": final_managed == baseline,
                "exact_id_cleanup_failures": list(cleanup_failures),
            }
            temporary.cleanup()

    if run_error is not None:
        print(f"D8 example failed: {run_error}", file=sys.stderr)
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    if cleanup_failures or output["managed_containers"][
        "no_new_managed_containers"
    ] is not True:
        print("D8 example failed: managed container residue", file=sys.stderr)
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 1

    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--crash-child":
        _crash_child(sys.argv[2:])
    raise SystemExit(main())
