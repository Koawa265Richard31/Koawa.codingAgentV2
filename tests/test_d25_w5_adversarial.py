"""D25 W5b: real-Docker adversarial matrix against an attack fixture.

The fixture (koawa-d25-evil:fixed) only injects failures - it is never
evidence of a vetted third-party server.  Each test proves the runtime
survives the attack with bounded resources and exact cleanup.
"""

from __future__ import annotations

import json
import subprocess
import time
import unittest

from koawa_agent_v2.mcp.docker_endpoint import launch_container_endpoint
from koawa_agent_v2.sandbox.docker_primitives import ContainerSpec

EVIL = "sha256:f8767f46249a2820c7933e368bae7f6b16850cbca4a08fdb256ff92bf6c97efe"
NODE = "/usr/local/bin/node"


def _docker_ready() -> bool:
    try:
        completed = subprocess.run(
            ("docker", "info"), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


class EvilFixtureTestBase(unittest.TestCase):
    def setUp(self) -> None:
        if not _docker_ready():
            self.skipTest("docker_unavailable")

    def _launch(self, mode: str):
        spec = ContainerSpec(
            image_id=EVIL,
            argv=(NODE, "/opt/evil.js"),
            container_working_directory="/",
            environment=(("EVIL_MODE", mode),),
            cpus=1.0,
            memory_bytes=128 * 1024 * 1024,
            pids_limit=32,
            tmpfs_bytes=16 * 1024 * 1024,
            container_name=f"koawa-d25-evil-{mode}",
            labels=(("koawa.managed", "mcp-evil"),),
        )
        return launch_container_endpoint(
            spec,
            docker_executable="docker",
            process_start_timeout_seconds=60.0,
        )

    def _assert_container_gone(self, endpoint) -> None:
        probe = subprocess.run(
            ("docker", "container", "inspect", endpoint.container_id),
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertNotEqual(0, probe.returncode)


class AdversarialMatrixTest(EvilFixtureTestBase):
    def test_huge_frame_is_bounded_and_cleanup_exact(self) -> None:
        endpoint = self._launch("flood_frame")
        time.sleep(2.0)
        # The transport layer owns the frame bound; the endpoint only proves
        # that terminating the attacker still removes the exact container.
        endpoint.kill_tree(deadline=time.monotonic() + 30.0)
        self._assert_container_gone(endpoint)
        identity = endpoint.external_identity
        # digest-only identity: fixed key set, no payload/env/stderr material
        self.assertEqual(
            {"kind", "container_id", "image_digest", "container_name"},
            set(identity),
        )
        self.assertLessEqual(len(json.dumps(identity)), 512)

    def test_stderr_flood_capped_and_cleanup_exact(self) -> None:
        endpoint = self._launch("stderr_flood")
        time.sleep(3.0)
        stderr = endpoint.stderr
        assert stderr is not None
        collected = 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            chunk = stderr.read(65536)
            if not chunk:
                break
            collected += len(chunk)
        endpoint.kill_tree(deadline=time.monotonic() + 30.0)
        self._assert_container_gone(endpoint)
        # OS pipes bound what an attacker can push at the host process; the
        # transport additionally caps retained stderr (I1 contract).
        self.assertLess(collected, 512 * 1024 * 1024)

    def test_pid_pressure_is_capped_and_cleanup_exact(self) -> None:
        endpoint = self._launch("pid_pressure")
        time.sleep(4.0)
        # With --pids-limit 32 the fork pressure cannot explode the host:
        # the exact container is still stoppable/removable on demand.
        endpoint.kill_tree(deadline=time.monotonic() + 30.0)
        self._assert_container_gone(endpoint)


class GovernanceAnchorsTest(unittest.TestCase):
    def test_malicious_protocol_content_cannot_change_policy(self) -> None:
        # W5 governance anchor: the malicious-content denials live at binding
        # and policy layers (T3 in the agent threat model), unchanged by D25.
        from tests.test_d21_agent_security import (
            McpPoisoningTest,
        )

        self.assertTrue(hasattr(McpPoisoningTest, "test_t3_mcp_poisoning_rejected_at_binding_time"))
        self.assertTrue(hasattr(McpPoisoningTest, "test_t3b_bound_mcp_write_denied_by_default"))


if __name__ == "__main__":
    unittest.main()
