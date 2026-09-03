"""D25 W2: Docker MCP endpoint contracts with a fake trusted adapter."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from koawa_agent_v2.mcp.docker_endpoint import (
    DockerAdapter,
    DockerEndpointError,
    launch_container_endpoint,
)
from koawa_agent_v2.sandbox.docker_primitives import (
    ContainerSpec,
    create_arguments,
    inspect_and_validate,
)
from koawa_agent_v2.sandbox.runtime import SandboxError

DIGEST = "sha256:" + "a" * 64


def _spec(**overrides) -> ContainerSpec:
    values = dict(
        image_id=DIGEST,
        argv=("/usr/local/bin/node", "server.js"),
        container_working_directory="/work",
        environment=(("MCP_MODE", "stdio"),),
        cpus=1.0,
        memory_bytes=256 * 1024 * 1024,
        pids_limit=128,
        tmpfs_bytes=32 * 1024 * 1024,
        container_name="koawa-mcp-test",
        labels=(("koawa.managed", "mcp"), ("koawa.allocation", "alx")),
    )
    values.update(overrides)
    return ContainerSpec(**values)


def _inspect_document(spec: ContainerSpec, container_id: str) -> dict:
    return {
        "Id": container_id,
        "Config": {
            "Image": spec.image_id,
            "Tty": False,
            "OpenStdin": True,
            "Entrypoint": [spec.argv[0]],
            "Cmd": list(spec.argv[1:]),
            "WorkingDir": spec.container_working_directory,
            "User": "65532:65532",
            "Labels": dict(spec.labels),
        },
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": spec.memory_bytes,
            "MemorySwap": spec.memory_bytes,
            "NanoCpus": int(spec.cpus * 1_000_000_000),
            "PidsLimit": spec.pids_limit,
            "Mounts": [],
        },
    }


class _FakeAttach:
    def __init__(self, *, exit_after: int | None = None) -> None:
        self.pid = 4242
        self.poll_value: int | None = None
        self.killed = False
        self.handles_closed = False
        self.exit_after = exit_after
        self.waits = 0

    @property
    def stdin(self):
        return None

    @property
    def stdout(self):
        return None

    @property
    def stderr(self):
        return None

    def poll(self):
        return self.poll_value

    def wait_bounded(self, deadline):
        self.waits += 1
        if self.exit_after is not None and self.waits >= self.exit_after:
            self.poll_value = 0
            return 0
        raise DockerEndpointError("mcp_container_wait_timeout")

    def kill(self):
        self.killed = True
        self.poll_value = -9

    def close_handles(self):
        self.handles_closed = True


class FakeDocker(DockerAdapter):
    def __init__(self, *, attach: _FakeAttach, tamper=None, fail_stop=False):
        self.created: list[tuple[str, ...]] = []
        self.container_id = "c" * 64
        self.removed = False
        self.stopped = False
        self._attach = attach
        self._tamper = tamper or {}
        self._fail_stop = fail_stop

    def create(self, docker, arguments, *, timeout):
        self.created.append(arguments)
        return self.container_id

    def inspect(self, docker, container_id, *, timeout):
        spec = self._spec
        document = _inspect_document(spec, container_id)
        for dotted, value in self._tamper.items():
            section, key = dotted.split(".", 1)
            if value is None:
                document[section].pop(key, None)
            else:
                document[section][key] = value
        return document

    def start_attach(self, docker, container_id):
        return self._attach

    def stop_and_remove(self, docker, container_id, *, stop_timeout_seconds, cli_timeout_seconds):
        if self._fail_stop:
            raise SandboxError("mcp_container_stop_failed")
        self.stopped = True
        self.removed = True


class CreateArgumentsTest(unittest.TestCase):
    def test_exact_trusted_argv(self) -> None:
        spec = _spec()
        argv = create_arguments(spec)
        text = " ".join(argv)
        for required in (
            "--pull never", "--network none", "--read-only",
            "--cap-drop ALL", "no-new-privileges", "--user 65532:65532",
            "--interactive", "--workdir /work", "--entrypoint /usr/local/bin/node",
        ):
            self.assertIn(required, text)
        self.assertNotIn("--mount", text)
        self.assertNotIn("--publish", text)
        self.assertNotIn("--privileged", text)
        self.assertEqual(DIGEST, argv[-2])
        self.assertEqual("server.js", argv[-1])
        for label, value in spec.labels:
            self.assertIn(f"--label {label}={value}", text)


class InspectValidationTest(unittest.TestCase):
    def test_tamper_matrix_all_fail_closed(self) -> None:
        base = _spec()
        tampered = [
            ("Config.Image", DIGEST[:-1] + "b"),
            ("Config.Tty", True),
            ("Config.OpenStdin", False),
            ("Config.Entrypoint", ["/bin/sh"]),
            ("Config.WorkingDir", "/other"),
            ("Config.User", "0:0"),
            ("HostConfig.NetworkMode", "bridge"),
            ("HostConfig.ReadonlyRootfs", False),
            ("HostConfig.CapDrop", []),
            ("HostConfig.SecurityOpt", []),
            ("HostConfig.Memory", 1),
            ("HostConfig.NanoCpus", 2_000_000_000),
            ("HostConfig.PidsLimit", 9999),
            ("HostConfig.Mounts", [{"Type": "bind", "Source": "/host"}]),
            ("Config.Labels", {}),
        ]
        for dotted, value in tampered:
            with self.subTest(dotted=dotted):
                document = _inspect_document(base, "c" * 64)
                section, key = dotted.split(".", 1)
                document[section][key] = value
                with self.assertRaises(SandboxError) as raised:
                    inspect_and_validate(document, base)
                self.assertEqual(
                    "mcp_container_contract_mismatch", raised.exception.args[0]
                )

    def test_clean_document_passes(self) -> None:
        inspect_and_validate(_inspect_document(_spec(), "c" * 64), _spec())


class LaunchEndpointTest(unittest.TestCase):
    def test_success_binds_identity_and_exact_create_argv(self) -> None:
        fake = FakeDocker(attach=_FakeAttach())
        fake._spec = _spec()
        endpoint = launch_container_endpoint(
            _spec(),
            docker_executable="docker",
            process_start_timeout_seconds=30.0,
            adapter=fake,
        )
        self.assertEqual("c" * 64, endpoint.container_id)
        self.assertEqual(4242, endpoint.pid)
        identity = endpoint.external_identity
        self.assertEqual(
            {"kind", "container_id", "image_digest", "container_name"},
            set(identity),
        )
        self.assertNotIn("argv", json.dumps(identity))
        self.assertNotIn("stdio", json.dumps(identity))
        argv = fake.created[0]
        self.assertIn("--interactive", argv)
        self.assertNotIn("--mount", argv)

    def test_tampered_inspect_never_returns_endpoint_and_cleans_up(self) -> None:
        fake = FakeDocker(
            attach=_FakeAttach(),
            tamper={"HostConfig.NetworkMode": "bridge"},
        )
        fake._spec = _spec()
        with self.assertRaises(SandboxError) as raised:
            launch_container_endpoint(
                _spec(),
                docker_executable="docker",
                process_start_timeout_seconds=30.0,
                adapter=fake,
            )
        self.assertEqual(
            "mcp_container_contract_mismatch", raised.exception.args[0]
        )
        self.assertTrue(fake.stopped)
        self.assertTrue(fake.removed)

    def test_terminate_requires_both_attach_and_container(self) -> None:
        attach = _FakeAttach()
        fake = FakeDocker(attach=attach)
        fake._spec = _spec()
        endpoint = launch_container_endpoint(
            _spec(),
            docker_executable="docker",
            process_start_timeout_seconds=30.0,
            adapter=fake,
        )
        endpoint.terminate_tree(deadline=monotonic_deadline(5.0))
        self.assertTrue(fake.stopped)
        self.assertTrue(fake.removed)
        self.assertTrue(attach.handles_closed)
        self.assertIsNotNone(attach.poll_value)
        # second terminate is idempotent
        endpoint.terminate_tree(deadline=monotonic_deadline(5.0))

    def test_terminate_cleanup_failure_is_stable_error(self) -> None:
        attach = _FakeAttach(exit_after=1)
        fake = FakeDocker(attach=attach, fail_stop=True)
        fake._spec = _spec()
        endpoint = launch_container_endpoint(
            _spec(),
            docker_executable="docker",
            process_start_timeout_seconds=30.0,
            adapter=fake,
        )
        with self.assertRaises(DockerEndpointError) as raised:
            endpoint.terminate_tree(deadline=monotonic_deadline(5.0))
        self.assertEqual(
            "mcp_container_stop_failed", raised.exception.args[0]
        )

    def test_attach_client_death_is_reflected_by_poll(self) -> None:
        attach = _FakeAttach()
        attach.poll_value = 1
        fake = FakeDocker(attach=attach)
        fake._spec = _spec()
        endpoint = launch_container_endpoint(
            _spec(),
            docker_executable="docker",
            process_start_timeout_seconds=30.0,
            adapter=fake,
        )
        self.assertEqual(1, endpoint.poll())


def monotonic_deadline(seconds: float) -> float:
    import time

    return time.monotonic() + seconds


if __name__ == "__main__":
    unittest.main()
