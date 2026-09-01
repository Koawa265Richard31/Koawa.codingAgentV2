from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from unittest import mock
from uuid import UUID, uuid4

import koawa_agent_v2.sandbox.runtime as sandbox_runtime
from koawa_agent_v2.control.event_store import EventStoreError
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import AgentLoopCancelled
from koawa_agent_v2.sandbox.protocol import (
    ALLOCATION_ID_LABEL,
    AllocationState,
    MANAGED_LABEL,
    MANAGED_LABEL_VALUE,
    OWNER_EXECUTION_ID_LABEL,
    OWNER_NONCE_LABEL,
    SandboxCommandProfile,
    SandboxError,
    SandboxLimits,
)
from koawa_agent_v2.sandbox.runtime import (
    ContainerReaper,
    DockerCommandRunner,
    DockerSandboxDoctor,
    SandboxAllocationStore,
    resolve_workspace_mount,
)
from koawa_agent_v2.verification.runner import CommandOutcome


IMAGE_ID = (
    "sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"
)
MANAGED_FILTER = "label=io.koawa.v2.managed=true"


def _python_profile(
    profile_id: str,
    script: str,
    *,
    timeout_seconds: float = 10.0,
    max_stdout_bytes: int = 32_768,
    max_stderr_bytes: int = 32_768,
) -> SandboxCommandProfile:
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return SandboxCommandProfile(
        profile_id,
        (
            "/usr/local/bin/python",
            "-c",
            f"import base64;exec(base64.b64decode('{encoded}'))",
        ),
        timeout_seconds=timeout_seconds,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        environment=(
            ("PYTHONDONTWRITEBYTECODE", "1"),
            ("TZ", "UTC"),
        ),
    )


class _Docker:
    """Test-only CLI facade; cleanup always targets a complete container ID."""

    def __init__(self) -> None:
        executable = shutil.which("docker")
        if executable is None:
            raise unittest.SkipTest("docker_executable_unavailable")
        self.executable = str(Path(executable).resolve(strict=True))
        self._owned: set[str] = set()

    def run(
        self,
        *arguments: str,
        timeout: float = 30.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            (self.executable, *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout,
            check=False,
            shell=False,
        )
        if check and completed.returncode != 0:
            self._fail(arguments, completed)
        return completed

    def json(self, *arguments: str, timeout: float = 30.0) -> Any:
        completed = self.run(*arguments, timeout=timeout)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"docker_invalid_json:{arguments[0] if arguments else 'unknown'}"
            ) from exc

    def remember(self, container_id: str) -> str:
        self._require_container_id(container_id)
        self._owned.add(container_id)
        return container_id

    def forget(self, container_id: str) -> None:
        self._require_container_id(container_id)
        self._owned.discard(container_id)

    def inspect(self, container_id: str) -> dict[str, Any]:
        self._require_container_id(container_id)
        values = self.json("container", "inspect", container_id)
        if not isinstance(values, list) or len(values) != 1:
            raise AssertionError("docker_inspect_cardinality")
        value = values[0]
        if not isinstance(value, dict) or value.get("Id") != container_id:
            raise AssertionError("docker_inspect_identity_mismatch")
        return value

    def managed_ids(self) -> tuple[str, ...]:
        completed = self.run(
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            MANAGED_FILTER,
        )
        values = tuple(value for value in completed.stdout.splitlines() if value)
        for value in values:
            self._require_container_id(value)
        return values

    def remove_exact(self, container_id: str) -> None:
        self._require_container_id(container_id)
        completed = self.run(
            "container",
            "rm",
            "--force",
            container_id,
            check=False,
        )
        if completed.returncode != 0 and "No such container" not in completed.stderr:
            self._fail(("container", "rm", "--force", container_id), completed)
        self._owned.discard(container_id)

    def cleanup(self) -> None:
        errors: list[BaseException] = []
        for container_id in tuple(sorted(self._owned)):
            try:
                self.remove_exact(container_id)
            except BaseException as exc:  # cleanup must attempt every exact ID
                errors.append(exc)
        if errors:
            raise AssertionError("docker_exact_cleanup_failed") from errors[0]

    @staticmethod
    def _require_container_id(value: str) -> None:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise AssertionError("invalid_exact_container_id")

    @staticmethod
    def _fail(
        arguments: Sequence[str], completed: subprocess.CompletedProcess[str]
    ) -> None:
        command = arguments[0] if arguments else "unknown"
        stderr = completed.stderr.strip().replace("\r", " ").replace("\n", " ")
        raise AssertionError(
            f"docker_{command}_failed:exit={completed.returncode}:stderr={stderr[:500]}"
        )


class D8DockerPrerequisiteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.docker = _Docker()
        self.addCleanup(self.docker.cleanup)

    def test_doctor_accepts_available_linux_daemon_and_exact_image_id(self) -> None:
        report = DockerSandboxDoctor(self.docker.executable).check(IMAGE_ID)
        if not report.ready:
            self.skipTest(report.error_code or "docker_doctor_not_ready")
        self.assertTrue(report.ready)
        self.assertEqual("linux", report.server_os)
        self.assertEqual("amd64", report.server_architecture)
        self.assertEqual(IMAGE_ID, report.image_id)
        self.assertIsNotNone(report.client_version)
        self.assertIsNotNone(report.server_version)

    def test_doctor_rejects_mutable_tag_without_daemon_side_effect(self) -> None:
        report = DockerSandboxDoctor(self.docker.executable).check("python:3.12-slim")
        self.assertFalse(report.ready)
        self.assertEqual("immutable_image_id_required", report.error_code)

    def test_workspace_mount_is_canonical_and_rejects_link_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-d8-mount-") as directory:
            base = Path(directory)
            workspace = base / "workspace"
            outside = base / "outside"
            workspace.mkdir()
            outside.mkdir()
            (workspace / "inside.txt").write_text("inside", encoding="utf-8")
            resolved, digest = resolve_workspace_mount(workspace)
            self.assertEqual(workspace.resolve(strict=True), resolved)
            self.assertEqual(64, len(digest))
            self.assertTrue(all(value in "0123456789abcdef" for value in digest))

            link = workspace / "outside-link"
            try:
                os.symlink(outside, link, target_is_directory=True)
            except OSError:
                # A directory junction needs no SeCreateSymbolicLinkPrivilege and
                # exercises the same Windows reparse-point boundary.
                created = subprocess.run(
                    (
                        os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
                        "/d",
                        "/c",
                        "mklink",
                        "/J",
                        str(link),
                        str(outside),
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    shell=False,
                )
                if created.returncode != 0:
                    self.fail("junction_creation_failed")
            try:
                with self.assertRaises(SandboxError) as caught:
                    resolve_workspace_mount(workspace)
                self.assertEqual("workspace_mount_link_escape", caught.exception.code)
            finally:
                if os.name == "nt":
                    # Junctions have directory semantics on Windows.
                    os.rmdir(link)
                else:
                    # POSIX directory symlinks are unlinkable entries, not
                    # directories; rmdir raises NotADirectoryError here.
                    link.unlink()

    def test_workspace_mount_rejects_same_volume_hard_link_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-d8-hardlink-") as directory:
            base = Path(directory)
            workspace = base / "workspace"
            workspace.mkdir()
            outside = base / "outside-secret.txt"
            outside.write_text("outside", encoding="utf-8")
            linked = workspace / "linked-secret.txt"
            try:
                os.link(outside, linked)
            except OSError as exc:
                self.skipTest(f"hardlink_creation_unavailable:{exc.winerror}")
            self.assertEqual(outside.stat().st_ino, linked.stat().st_ino)
            with self.assertRaises(SandboxError) as caught:
                resolve_workspace_mount(workspace)
            self.assertEqual("workspace_mount_hardlink_escape", caught.exception.code)


class D8DockerRunnerIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="koawa-d8-docker-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        (self.workspace / "fixture.txt").write_text("fixture", encoding="utf-8")
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git" / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n",
            encoding="utf-8",
        )
        (self.base / "outside-sentinel.txt").write_text(
            "host-only", encoding="utf-8"
        )
        self.docker = _Docker()
        self.addCleanup(self.docker.cleanup)
        doctor = DockerSandboxDoctor(self.docker.executable).check(IMAGE_ID)
        if not doctor.ready:
            self.skipTest(doctor.error_code or "docker_doctor_not_ready")
        self.initial_managed = self._stable_managed_ids()

    def _store(self, name: str) -> SqliteEventStore:
        return SqliteEventStore(self.base / f"{name}.sqlite3")

    def _assert_container_absent(self, container_id: str | None) -> None:
        self.assertIsNotNone(container_id)
        completed = self.docker.run(
            "container", "inspect", container_id, check=False
        )
        self.assertNotEqual(0, completed.returncode)
        self.docker.forget(container_id)

    def _assert_no_new_managed(self) -> None:
        deadline = time.monotonic() + 3.0
        while True:
            current = set(self.docker.managed_ids())
            if current == self.initial_managed:
                return
            if time.monotonic() >= deadline:
                self.assertEqual(self.initial_managed, current)
            time.sleep(0.05)

    def _stable_managed_ids(self) -> set[str]:
        previous: set[str] | None = None
        stable_samples = 0
        deadline = time.monotonic() + 3.0
        while True:
            current = set(self.docker.managed_ids())
            if current == previous:
                stable_samples += 1
            else:
                previous = current
                stable_samples = 0
            if stable_samples >= 2 or time.monotonic() >= deadline:
                return current
            time.sleep(0.05)

    def _manual_create(
        self,
        labels: Sequence[tuple[str, str]],
        *,
        name: str | None = None,
    ) -> str:
        arguments = [
            "container",
            "create",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=bind,src={self.workspace.resolve(strict=True)},dst=/workspace,readonly",
            "--workdir",
            "/workspace",
        ]
        if name is not None:
            arguments[2:2] = ("--name", name)
        for name, value in labels:
            arguments.extend(("--label", f"{name}={value}"))
        arguments.extend(("--entrypoint", "/bin/true", IMAGE_ID))
        created = self.docker.run(*arguments)
        return self.docker.remember(created.stdout.strip())

    def test_real_runner_success_and_container_attack_evidence(self) -> None:
        script = r'''
import json
import os
import pathlib
import socket
import sys

def can_write(path, value):
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(value)
        return True
    except OSError:
        return False

def can_read_directory(path):
    try:
        os.listdir(path)
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
    "home": os.environ.get("HOME"),
    "root_readable": can_read_directory("/root"),
    "rootfs_writable": can_write("/etc/koawa-d8-probe", "x"),
    "workspace_writable": can_write("/workspace/fixture.txt", "x"),
    "git_writable": can_write("/workspace/.git/config", "x"),
    "tmp_writable": can_write("/tmp/koawa-d8-probe", "x"),
    "docker_socket": pathlib.Path("/var/run/docker.sock").exists(),
    "outside_visible": pathlib.Path("/outside-sentinel.txt").exists(),
    "network_reachable": network_reachable(),
}
print(json.dumps(facts, sort_keys=True))
print("stderr-evidence", file=sys.stderr)
'''
        profile = _python_profile("security", script)
        limits = SandboxLimits(
            cpus=0.5,
            memory_bytes=64 * 1024 * 1024,
            pids_limit=32,
            tmpfs_bytes=8 * 1024 * 1024,
        )
        observed: dict[str, Any] = {}

        def inspect_after_create(point, allocation, container_id) -> None:
            if point != "after_create_before_bind":
                return
            self.assertIsNotNone(container_id)
            self.docker.remember(container_id)
            observed["allocation"] = allocation
            observed["container_id"] = container_id
            observed["inspect"] = self.docker.inspect(container_id)

        runner = DockerCommandRunner(
            self.workspace,
            (profile,),
            self._store("success"),
            IMAGE_ID,
            docker_executable=self.docker.executable,
            limits=limits,
            fault_hook=inspect_after_create,
        )
        secret_name = "KOAWA_D8_HOST_SECRET"
        previous_secret = os.environ.get(secret_name)
        os.environ[secret_name] = "host-must-not-enter-container"
        try:
            result = runner.run("security", execution_id=uuid4())
        finally:
            if previous_secret is None:
                os.environ.pop(secret_name, None)
            else:
                os.environ[secret_name] = previous_secret

        self.assertEqual(CommandOutcome.PASSED, result.outcome)
        self.assertEqual(0, result.exit_code)
        self.assertEqual("docker", result.backend)
        self.assertEqual(IMAGE_ID, result.immutable_image_id)
        self.assertEqual(profile.profile_digest, result.profile_digest)
        self.assertEqual(observed["container_id"], result.container_id)
        self.assertFalse(result.stdout_truncated)
        self.assertFalse(result.stderr_truncated)
        self.assertEqual("stderr-evidence\n", result.stderr)
        facts = json.loads(result.stdout)
        self.assertEqual(65532, facts["uid"])
        self.assertEqual(65532, facts["gid"])
        self.assertEqual("/workspace", facts["cwd"])
        self.assertIsNone(facts["host_secret"])
        self.assertFalse(facts["root_readable"])
        self.assertFalse(facts["rootfs_writable"])
        self.assertFalse(facts["workspace_writable"])
        self.assertFalse(facts["git_writable"])
        self.assertTrue(facts["tmp_writable"])
        self.assertFalse(facts["docker_socket"])
        self.assertFalse(facts["outside_visible"])
        self.assertFalse(facts["network_reachable"])

        inspected = observed["inspect"]
        config = inspected["Config"]
        host = inspected["HostConfig"]
        self.assertEqual(IMAGE_ID, inspected["Image"])
        self.assertEqual("65532:65532", config["User"])
        self.assertEqual("/workspace", config["WorkingDir"])
        self.assertEqual("/usr/local/bin/python", inspected["Path"])
        self.assertNotIn(
            "KOAWA_D8_HOST_SECRET=host-must-not-enter-container",
            config["Env"],
        )
        self.assertIn("TZ=UTC", config["Env"])
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertEqual("none", host["NetworkMode"])
        self.assertEqual(["ALL"], host["CapDrop"])
        self.assertIn("no-new-privileges", host["SecurityOpt"])
        self.assertEqual(32, host["PidsLimit"])
        self.assertEqual(500_000_000, host["NanoCpus"])
        self.assertEqual(64 * 1024 * 1024, host["Memory"])
        self.assertEqual(64 * 1024 * 1024, host["MemorySwap"])
        self.assertEqual("none", host["LogConfig"]["Type"])
        self.assertIn("/tmp", host["Tmpfs"])
        tmpfs = host["Tmpfs"]["/tmp"]
        for option in ("rw", "noexec", "nosuid", "nodev", "size=8388608"):
            self.assertIn(option, tmpfs)
        mounts = [
            mount
            for mount in inspected["Mounts"]
            if mount.get("Destination") == "/workspace"
        ]
        self.assertEqual(1, len(mounts))
        self.assertEqual("bind", mounts[0]["Type"])
        self.assertFalse(mounts[0]["RW"])
        self.assertEqual(self.workspace.resolve(strict=True), Path(mounts[0]["Source"]))
        self._assert_container_absent(result.container_id)
        self.assertEqual((), runner.allocation_store.list_open())
        self._assert_no_new_managed()

    def test_failure_timeout_output_pids_and_oom_are_typed_and_cleaned(self) -> None:
        profiles = (
            _python_profile(
                "failed",
                "import sys;print('expected-failure',file=sys.stderr);raise SystemExit(7)",
            ),
            _python_profile(
                "timeout",
                "import subprocess,time;subprocess.Popen(['/bin/sleep','30']);print('child-started',flush=True);time.sleep(30)",
                timeout_seconds=2.0,
            ),
            _python_profile(
                "output",
                "import sys,time;sys.stdout.write('X'*200000);sys.stdout.flush();time.sleep(30)",
                timeout_seconds=5.0,
                max_stdout_bytes=1024,
            ),
            _python_profile(
                "pids",
                r'''
import json
import subprocess
children = []
failure = None
for _ in range(100):
    try:
        children.append(subprocess.Popen(["/bin/sleep", "10"]))
    except OSError as error:
        failure = type(error).__name__
        break
for child in children:
    child.terminate()
for child in children:
    child.wait()
print(json.dumps({"launched": len(children), "failure": failure}))
''',
            ),
            _python_profile(
                "oom",
                "import time;value=bytearray(512*1024*1024);time.sleep(1);print(len(value))",
                timeout_seconds=10.0,
            ),
        )
        runner = DockerCommandRunner(
            self.workspace,
            profiles,
            self._store("limits"),
            IMAGE_ID,
            docker_executable=self.docker.executable,
            limits=SandboxLimits(
                cpus=0.5,
                memory_bytes=64 * 1024 * 1024,
                pids_limit=16,
                tmpfs_bytes=8 * 1024 * 1024,
            ),
        )
        execution_id = uuid4()

        failed = runner.run("failed", execution_id=execution_id)
        self.assertEqual(CommandOutcome.FAILED, failed.outcome)
        self.assertEqual(7, failed.exit_code)
        self.assertIn("expected-failure", failed.stderr)
        self._assert_container_absent(failed.container_id)

        timed_out = runner.run("timeout", execution_id=execution_id)
        self.assertEqual(CommandOutcome.TIMED_OUT, timed_out.outcome)
        self.assertIn("child-started", timed_out.stdout)
        self._assert_container_absent(timed_out.container_id)

        output = runner.run("output", execution_id=execution_id)
        self.assertEqual(CommandOutcome.OUTPUT_LIMIT, output.outcome)
        self.assertTrue(output.stdout_truncated)
        self.assertEqual(1024, len(output.stdout.encode("utf-8")))
        self.assertGreater(output.stdout_bytes, 1024)
        self._assert_container_absent(output.container_id)

        pids = runner.run("pids", execution_id=execution_id)
        self.assertEqual(CommandOutcome.PASSED, pids.outcome)
        pid_facts = json.loads(pids.stdout)
        self.assertLess(pid_facts["launched"], 100)
        self.assertIsNotNone(pid_facts["failure"])
        self._assert_container_absent(pids.container_id)

        oom = runner.run("oom", execution_id=execution_id)
        self.assertEqual(CommandOutcome.OOM_KILLED, oom.outcome)
        self.assertNotEqual(0, oom.exit_code)
        self._assert_container_absent(oom.container_id)
        self.assertEqual((), runner.allocation_store.list_open())
        self._assert_no_new_managed()

    def test_cancellation_kills_container_tree_persists_identity_and_propagates(self) -> None:
        profile = _python_profile(
            "cancel",
            "import subprocess,time;subprocess.Popen(['/bin/sleep','30']);print('ready',flush=True);time.sleep(30)",
            timeout_seconds=30.0,
        )
        created: dict[str, Any] = {}

        def capture(point, allocation, container_id) -> None:
            if point == "after_create_before_bind":
                self.assertIsNotNone(container_id)
                self.docker.remember(container_id)
                created["allocation_id"] = allocation.allocation_id
                created["container_id"] = container_id

        runner = DockerCommandRunner(
            self.workspace,
            (profile,),
            self._store("cancel"),
            IMAGE_ID,
            docker_executable=self.docker.executable,
            fault_hook=capture,
        )
        checks = 0
        cancellation = AgentLoopCancelled()

        def cancel_after_start() -> None:
            nonlocal checks
            checks += 1
            if checks >= 3:
                raise cancellation

        with self.assertRaises(AgentLoopCancelled) as caught:
            runner.run(
                "cancel",
                progress_guard=cancel_after_start,
                execution_id=uuid4(),
            )
        self.assertIs(cancellation, caught.exception)
        self.assertGreaterEqual(checks, 3)
        allocation = runner.allocation_store.load(created["allocation_id"])
        self.assertIsNotNone(allocation)
        self.assertEqual(AllocationState.RELEASED, allocation.state)
        self.assertEqual(CommandOutcome.CANCELLED.value, allocation.outcome)
        self.assertEqual(created["container_id"], allocation.container_id)
        self._assert_container_absent(created["container_id"])
        self.assertEqual((), runner.allocation_store.list_open())
        self._assert_no_new_managed()

    def test_os_process_crash_after_create_before_bind_is_exactly_reaped(self) -> None:
        database = self.base / "crash.sqlite3"
        signal_path = self.base / "created-container-id.txt"
        execution_id = uuid4()
        worker = Path(__file__).parent / "fixtures" / "d8_kill_worker.py"
        completed = subprocess.run(
            (
                sys.executable,
                "-B",
                str(worker),
                str(database),
                str(self.workspace),
                self.docker.executable,
                IMAGE_ID,
                str(signal_path),
                str(execution_id),
            ),
            cwd=Path(__file__).parent.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
            check=False,
            shell=False,
        )
        # Even an unexpected worker failure is followed by exact-ID cleanup.
        for container_id in set(self.docker.managed_ids()) - self.initial_managed:
            self.docker.remember(container_id)
        self.assertEqual(73, completed.returncode, completed.stderr)
        self.assertTrue(signal_path.is_file())
        container_id = signal_path.read_text(encoding="ascii").strip()
        self.docker.remember(container_id)

        allocation_store = SandboxAllocationStore(SqliteEventStore(database))
        open_allocations = allocation_store.list_open()
        self.assertEqual(1, len(open_allocations))
        allocation = open_allocations[0]
        self.assertEqual(execution_id, allocation.owner_execution_id)
        self.assertEqual(AllocationState.INTENDED, allocation.state)
        self.assertIsNone(allocation.container_id)
        inspected = self.docker.inspect(container_id)
        self.assertFalse(inspected["State"]["Running"])
        self.assertEqual(
            dict(allocation.expected_labels), inspected["Config"]["Labels"]
        )

        report = ContainerReaper(
            allocation_store, self.docker.executable
        ).reap(owner_execution_id=execution_id, force=True)
        self.assertEqual((container_id,), report.removed)
        self.assertEqual((allocation.allocation_id,), report.released)
        self.assertEqual((), report.refused)
        self.assertEqual((), report.errors)
        recovered = allocation_store.load(allocation.allocation_id)
        self.assertIsNotNone(recovered)
        self.assertEqual(AllocationState.RELEASED, recovered.state)
        self.assertEqual("reaped", recovered.outcome)
        self.assertEqual(container_id, recovered.container_id)
        self._assert_container_absent(container_id)
        self.assertEqual((), allocation_store.list_open())
        self._assert_no_new_managed()

    def test_reaper_refuses_unknown_and_tampered_owner_nonce_containers(self) -> None:
        allocation_store = SandboxAllocationStore(self._store("tampered"))
        owner_execution_id = uuid4()
        _, mount_digest = resolve_workspace_mount(self.workspace)
        allocation = allocation_store.intent(
            owner_execution_id=owner_execution_id,
            image_id=IMAGE_ID,
            mount_digest=mount_digest,
            profile_digest="a" * 64,
            command_digest="b" * 64,
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        tampered_labels = tuple(
            (
                name,
                "f" * 64 if name == OWNER_NONCE_LABEL else value,
            )
            for name, value in allocation.expected_labels
        )
        tampered_id = self._manual_create(tampered_labels)
        unknown_allocation_id = uuid4()
        unknown_id = self._manual_create(
            (
                (MANAGED_LABEL, MANAGED_LABEL_VALUE),
                (ALLOCATION_ID_LABEL, str(unknown_allocation_id)),
            )
        )
        deadline = time.monotonic() + 3.0
        expected_visible = {tampered_id, unknown_id}
        while not expected_visible.issubset(set(self.docker.managed_ids())):
            if time.monotonic() >= deadline:
                self.fail("managed_container_listing_not_visible")
            time.sleep(0.05)

        reaper = ContainerReaper(allocation_store, self.docker.executable)
        tampered_report = reaper.reap(
            allocation_id=allocation.allocation_id,
            force=True,
        )
        self.assertEqual((), tampered_report.removed)
        self.assertEqual((tampered_id,), tampered_report.refused)
        self.assertEqual((), tampered_report.released)
        self.assertEqual((), tampered_report.errors)
        unknown_report = reaper.reap(
            allocation_id=unknown_allocation_id,
            force=True,
        )
        self.assertEqual((), unknown_report.removed)
        self.assertEqual((unknown_id,), unknown_report.refused)
        self.assertEqual((), unknown_report.released)
        self.assertEqual((), unknown_report.errors)
        self.assertEqual(tampered_id, self.docker.inspect(tampered_id)["Id"])
        self.assertEqual(unknown_id, self.docker.inspect(unknown_id)["Id"])
        still_intended = allocation_store.load(allocation.allocation_id)
        self.assertIsNotNone(still_intended)
        self.assertEqual(AllocationState.INTENDED, still_intended.state)
        self.assertIsNone(still_intended.container_id)

        # Refusal is fail-closed; only the test fixture owner performs exact cleanup.
        self.docker.remove_exact(tampered_id)
        self.docker.remove_exact(unknown_id)
        self._assert_no_new_managed()

    def test_bound_allocation_reaps_only_persisted_id_and_refuses_label_clone(self) -> None:
        allocation_store = SandboxAllocationStore(self._store("bound-clone"))
        owner_execution_id = uuid4()
        _, mount_digest = resolve_workspace_mount(self.workspace)
        allocation = allocation_store.intent(
            owner_execution_id=owner_execution_id,
            image_id=IMAGE_ID,
            mount_digest=mount_digest,
            profile_digest="1" * 64,
            command_digest="2" * 64,
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        expected_name = f"koawa-v2-{allocation.allocation_id.hex}"
        original_id = self._manual_create(
            allocation.expected_labels,
            name=expected_name,
        )
        allocation = allocation_store.bind(allocation, original_id)
        clone_id = self._manual_create(
            allocation.expected_labels,
            name=f"koawa-v2-clone-{allocation.allocation_id.hex}",
        )
        try:
            self.assertEqual(
                f"/{expected_name}", self.docker.inspect(original_id)["Name"]
            )
            self.assertNotEqual(
                f"/{expected_name}", self.docker.inspect(clone_id)["Name"]
            )

            report = ContainerReaper(
                allocation_store, self.docker.executable
            ).reap(allocation_id=allocation.allocation_id, force=True)
            self.assertEqual((original_id,), report.removed)
            self.assertEqual((clone_id,), report.refused)
            self.assertEqual((allocation.allocation_id,), report.released)
            self.assertEqual((), report.errors)
            recovered = allocation_store.load(allocation.allocation_id)
            self.assertIsNotNone(recovered)
            self.assertEqual(AllocationState.RELEASED, recovered.state)
            self.assertEqual(original_id, recovered.container_id)
            self._assert_container_absent(original_id)
            self.assertEqual(clone_id, self.docker.inspect(clone_id)["Id"])
        finally:
            self.docker.remove_exact(original_id)
            self.docker.remove_exact(clone_id)
        self._assert_no_new_managed()

    def test_bound_container_label_identity_mismatch_is_refused_without_release(self) -> None:
        _, mount_digest = resolve_workspace_mount(self.workspace)
        cases = (
            (OWNER_EXECUTION_ID_LABEL, lambda: str(uuid4())),
            (ALLOCATION_ID_LABEL, lambda: str(uuid4())),
            (OWNER_NONCE_LABEL, lambda: "0" * 64),
        )
        for index, (tampered_key, tampered_value) in enumerate(cases):
            with self.subTest(label=tampered_key):
                allocation_store = SandboxAllocationStore(
                    self._store(f"bound-mismatch-{index}")
                )
                allocation = allocation_store.intent(
                    owner_execution_id=uuid4(),
                    image_id=IMAGE_ID,
                    mount_digest=mount_digest,
                    profile_digest="3" * 64,
                    command_digest="4" * 64,
                    deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                )
                labels = tuple(
                    (
                        name,
                        tampered_value() if name == tampered_key else value,
                    )
                    for name, value in allocation.expected_labels
                )
                container_id = self._manual_create(
                    labels,
                    name=f"koawa-v2-{allocation.allocation_id.hex}",
                )
                try:
                    bound = allocation_store.bind(allocation, container_id)

                    report = ContainerReaper(
                        allocation_store, self.docker.executable
                    ).reap(allocation_id=allocation.allocation_id, force=True)
                    self.assertEqual((), report.removed)
                    self.assertEqual((container_id,), report.refused)
                    self.assertEqual((), report.released)
                    self.assertEqual((), report.errors)
                    unchanged = allocation_store.load(allocation.allocation_id)
                    self.assertIsNotNone(unchanged)
                    self.assertEqual(AllocationState.BOUND, unchanged.state)
                    self.assertEqual(bound.version, unchanged.version)
                    self.assertEqual(
                        container_id, self.docker.inspect(container_id)["Id"]
                    )
                finally:
                    self.docker.remove_exact(container_id)
        self._assert_no_new_managed()

    def test_create_timeout_or_empty_identity_stays_intended_and_open(self) -> None:
        profile = _python_profile("create-failure", "print('must-not-run')")
        cases = (
            (
                "timed-out",
                sandbox_runtime._CliResult(None, b"", b"create-timeout", True),
            ),
            (
                "empty-identity",
                sandbox_runtime._CliResult(0, b"", b"", False),
            ),
        )
        real_run_cli = sandbox_runtime._run_cli
        for index, (case_name, create_result) in enumerate(cases):
            with self.subTest(case=case_name):
                runner = DockerCommandRunner(
                    self.workspace,
                    (profile,),
                    self._store(f"create-failure-{index}"),
                    IMAGE_ID,
                    docker_executable=self.docker.executable,
                )

                def fake_run_cli(executable, argv, *, timeout_seconds):
                    if tuple(argv[:2]) == ("container", "create"):
                        return create_result
                    return real_run_cli(
                        executable,
                        argv,
                        timeout_seconds=timeout_seconds,
                    )

                with mock.patch.object(
                    sandbox_runtime,
                    "_run_cli",
                    side_effect=fake_run_cli,
                ):
                    result = runner.run(
                        "create-failure",
                        execution_id=uuid4(),
                    )
                self.assertEqual(CommandOutcome.START_FAILED, result.outcome)
                self.assertIsNone(result.container_id)
                self.assertIsNone(result.exit_code)
                open_allocations = runner.allocation_store.list_open()
                self.assertEqual(1, len(open_allocations))
                self.assertEqual(result.allocation_id, open_allocations[0].allocation_id)
                self.assertEqual(AllocationState.INTENDED, open_allocations[0].state)
                self.assertIsNone(open_allocations[0].outcome)
        self._assert_no_new_managed()

    def test_reaper_inspect_control_plane_errors_do_not_publish_not_found(self) -> None:
        cases = (
            (
                "daemon-error",
                sandbox_runtime._CliResult(
                    1,
                    b"",
                    b"cannot connect to docker daemon",
                    False,
                ),
            ),
            (
                "invalid-json",
                sandbox_runtime._CliResult(0, b"{not-json", b"", False),
            ),
        )
        real_run_cli = sandbox_runtime._run_cli
        _, mount_digest = resolve_workspace_mount(self.workspace)
        for index, (case_name, inspect_result) in enumerate(cases):
            with self.subTest(case=case_name):
                allocation_store = SandboxAllocationStore(
                    self._store(f"inspect-error-{index}")
                )
                allocation = allocation_store.intent(
                    owner_execution_id=uuid4(),
                    image_id=IMAGE_ID,
                    mount_digest=mount_digest,
                    profile_digest="5" * 64,
                    command_digest="6" * 64,
                    deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                )

                def fake_run_cli(executable, argv, *, timeout_seconds):
                    if tuple(argv[:2]) == ("container", "inspect"):
                        return inspect_result
                    return real_run_cli(
                        executable,
                        argv,
                        timeout_seconds=timeout_seconds,
                    )

                with mock.patch.object(
                    sandbox_runtime,
                    "_run_cli",
                    side_effect=fake_run_cli,
                ):
                    report = ContainerReaper(
                        allocation_store,
                        self.docker.executable,
                    ).reap(allocation_id=allocation.allocation_id, force=True)
                self.assertEqual((), report.removed)
                self.assertEqual((), report.refused)
                self.assertEqual((), report.released)
                self.assertEqual(("docker_reaper_inspect_failed",), report.errors)
                unchanged = allocation_store.load(allocation.allocation_id)
                self.assertIsNotNone(unchanged)
                self.assertEqual(AllocationState.INTENDED, unchanged.state)
                self.assertEqual(0, unchanged.version)
                self.assertIsNone(unchanged.outcome)

    def test_cancellation_survives_finish_append_failure_and_later_recovery(self) -> None:
        profile = _python_profile(
            "cancel-persistence",
            "import subprocess,time;subprocess.Popen(['/bin/sleep','30']);print('ready',flush=True);time.sleep(30)",
            timeout_seconds=30.0,
        )
        created: dict[str, Any] = {}

        def capture(point, allocation, container_id) -> None:
            if point == "after_create_before_bind":
                self.assertIsNotNone(container_id)
                self.docker.remember(container_id)
                created["allocation_id"] = allocation.allocation_id
                created["container_id"] = container_id
                created["name"] = self.docker.inspect(container_id)["Name"]

        runner = DockerCommandRunner(
            self.workspace,
            (profile,),
            self._store("cancel-persistence"),
            IMAGE_ID,
            docker_executable=self.docker.executable,
            fault_hook=capture,
        )
        cancellation = AgentLoopCancelled()
        checks = 0

        def cancel_after_start() -> None:
            nonlocal checks
            checks += 1
            if checks >= 3:
                raise cancellation

        with mock.patch.object(
            runner.allocation_store,
            "finish",
            side_effect=EventStoreError("injected append failure"),
        ):
            with self.assertRaises(AgentLoopCancelled) as caught:
                runner.run(
                    "cancel-persistence",
                    progress_guard=cancel_after_start,
                    execution_id=uuid4(),
                )
        self.assertIs(cancellation, caught.exception)
        self.assertEqual(
            f"/koawa-v2-{created['allocation_id'].hex}",
            created["name"],
        )
        open_allocation = runner.allocation_store.load(created["allocation_id"])
        self.assertIsNotNone(open_allocation)
        self.assertEqual(AllocationState.STARTED, open_allocation.state)
        self.assertEqual(created["container_id"], open_allocation.container_id)
        self.assertIsNone(open_allocation.outcome)
        self._assert_container_absent(created["container_id"])

        report = runner.reaper.reap(
            allocation_id=created["allocation_id"],
            force=True,
        )
        self.assertEqual((), report.removed)
        self.assertEqual((), report.refused)
        self.assertEqual((created["allocation_id"],), report.released)
        self.assertEqual((), report.errors)
        recovered = runner.allocation_store.load(created["allocation_id"])
        self.assertIsNotNone(recovered)
        self.assertEqual(AllocationState.RELEASED, recovered.state)
        self.assertEqual("container_not_found", recovered.outcome)
        self._assert_no_new_managed()


if __name__ == "__main__":
    unittest.main()
