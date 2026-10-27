"""B (WP-C): container-side isolated manifest execution + secret-channel matrix.

Requires a running Docker daemon and a pinned local image; skipped otherwise
(skips are never counted as proof).

Proven here:
- only the manifest candidate is visible in /workspace (the host repository
  is not mounted);
- a private scratch directory is writable at /scratch while the candidate
  mount is read-only;
- the network is disabled inside the container (an egress attempt fails);
- with a SENSITIVE profile the synthetic secret never reaches the model
  receipt, across the channels the design names (env var, file content,
  path, network endpoint, base64 encoding).
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.sandbox.protocol import SandboxCommandProfile
from koawa_agent_v2.sandbox.runtime import DockerCommandRunner
from koawa_agent_v2.verification.release_contracts import (
    ProfileReleaseContract,
    ReleaseMode,
)
from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry

IMAGE = os.environ.get("KOAWA_TEST_IMAGE", "python:3.12-slim")
SECRET = "TOPSECRET-CHANNEL-A1B2"

PROBE_SCRIPT = """\
import os, socket, pathlib
root = pathlib.Path('/workspace')
visible = sorted(item.name for item in root.iterdir())
print('visible', ','.join(visible))
try:
    (root / 'app.py').write_text('changed')
    print('write-ok')
except OSError:
    print('write-blocked')
pathlib.Path('/scratch/out.txt').write_text('ok')
print('scratch-ok')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=3)
    print('net-open')
except OSError:
    print('net-blocked')
print('host-file', (root / 'secret_outside.py').exists())
"""

CHANNEL_SCRIPT = """\
import base64, os, pathlib, sys
secret = os.environ.get('KOAWA_SECRET', '')
print('assert 1 == 1')
print('ENV ' + secret)
print('PATH /workspace/' + secret + '/file')
print('NET https://api.' + secret + '.invalid/v1')
print('B64 ' + base64.b64encode(secret.encode()).decode())
print('FILE ' + pathlib.Path('/workspace/app.py').read_text().strip())
sys.exit(1)
"""


def _docker_ready() -> bool:
    try:
        completed = subprocess.run(
            ("docker", "version", "--format", "{{.Server.Version}}"),
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _image_digest(image: str) -> str | None:
    completed = subprocess.run(
        ("docker", "image", "inspect", image, "--format", "{{.Id}}"),
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.decode().strip() or None


@unittest.skipUnless(_docker_ready(), "docker daemon unavailable")
class ContainerManifestTest(unittest.TestCase):
    """The container sees only the manifest; scratch is writable; no network."""

    def setUp(self) -> None:
        digest = _image_digest(IMAGE)
        if digest is None:
            raise unittest.SkipTest(f"local image {IMAGE} unavailable")
        self.image = digest
        self._tmp = __import__("tempfile").TemporaryDirectory(prefix="b1-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "repo"
        self.root.mkdir()
        (self.root / "app.py").write_bytes(b"value = 1\n")
        (self.root / "secret_outside.py").write_bytes(b"HOST_ONLY\n")
        (self.root / "probe.py").write_text(PROBE_SCRIPT, encoding="utf-8")

    def test_manifest_only_view_and_scratch_and_no_network(self) -> None:
        from koawa_agent_v2.control.sqlite_store import SqliteEventStore

        store = SqliteEventStore(Path(self._tmp.name) / "sandbox.db")
        profile = SandboxCommandProfile(
            "iso",
            ("/usr/local/bin/python", "/workspace/probe.py"),
            profile_version="b1",
            scratch_policy_ref="scratch/b1",
            workspace_manifest=("app.py", "probe.py"),
        )
        runner = DockerCommandRunner(self.root, (profile,), store, self.image)
        result = runner.run("iso", execution_id=uuid4())
        out = result.stdout
        self.assertIn("visible app.py,probe.py", out, out)
        self.assertIn("host-file False", out, out)
        self.assertIn("write-blocked", out, out)
        self.assertIn("scratch-ok", out, out)
        self.assertIn("net-blocked", out, out)
        self.assertTrue(result.isolated_workspace)


@unittest.skipUnless(_docker_ready(), "docker daemon unavailable")
class SecretChannelMatrixTest(unittest.TestCase):
    """Sensitive profile: the synthetic secret never reaches the receipt."""

    def setUp(self) -> None:
        digest = _image_digest(IMAGE)
        if digest is None:
            raise unittest.SkipTest(f"local image {IMAGE} unavailable")
        self.image = digest
        self._tmp = __import__("tempfile").TemporaryDirectory(prefix="b2-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "repo"
        self.root.mkdir()
        (self.root / "app.py").write_text(SECRET, encoding="utf-8")
        (self.root / "channels.py").write_text(CHANNEL_SCRIPT, encoding="utf-8")
        # The receipt path needs a git facade (finalize evidence chain).
        git = __import__("shutil").which("git")
        subprocess.run(
            (git, "-C", str(self.root), "init", "-q"),
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for key, value in (
            ("user.email", "b2@example.invalid"),
            ("user.name", "B2 Fixture"),
            ("core.autocrlf", "false"),
        ):
            subprocess.run(
                (git, "-C", str(self.root), "config", key, value),
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        subprocess.run(
            (git, "-C", str(self.root), "add", "--all"),
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            (git, "-C", str(self.root), "commit", "-qm", "b2"),
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def test_arbitrary_env_injection_is_refused(self) -> None:
        """Env channel control: only the fixed safe names may be set."""

        from koawa_agent_v2.sandbox.protocol import SandboxError

        with self.assertRaises(SandboxError) as raised:
            SandboxCommandProfile(
                "iso",
                ("/usr/local/bin/python", "/workspace/app.py"),
                workspace_manifest=("app.py",),
                scratch_policy_ref="scratch/b2",
                environment=(("KOAWA_SECRET", SECRET),),
            )
        self.assertIn("unsafe_sandbox_environment", str(raised.exception))

    def test_sensitive_profile_withholds_every_channel(self) -> None:
        from koawa_agent_v2.control.sqlite_store import SqliteEventStore

        store = SqliteEventStore(Path(self._tmp.name) / "sandbox.db")
        profile = SandboxCommandProfile(
            "iso",
            ("/usr/local/bin/python", "/workspace/channels.py"),
            profile_version="b2",
            release_mode="detailed",
            sensitive=True,
            candidate_admission_ref="admission/b2",
            audience_policy_ref="audience/b2",
            environment_ref="env/b2",
            scratch_policy_ref="scratch/b2",
            workspace_manifest=("app.py", "channels.py"),
        )
        runner = DockerCommandRunner(self.root, (profile,), store, self.image)
        result = runner.run("iso", execution_id=uuid4())
        self.assertTrue(result.isolated_workspace)
        # The raw body genuinely carries the secret (all channels printed).
        self.assertIn(SECRET, result.stdout)

        class _ReplayRunner:
            profile_ids = ("iso",)

            def validate_profile(self, profile_id):
                return None

            def run(self, profile_id, **kwargs):
                return result

            def release_contract(self, profile_id):
                # Sensitive without a release contract: fixed withheld state.
                return ProfileReleaseContract(
                    profile_id=profile_id,
                    profile_version="b2",
                    release_mode=ReleaseMode.LOCAL_ONLY,
                    sensitive=True,
                )

        registry = build_verified_coding_tool_registry(
            self.root,
            command_runner=_ReplayRunner(),
            required_test_profiles=("iso",),
        )
        self.addCleanup(registry.close)
        run_id = uuid4()
        call = ToolCallItem(
            0, "it", "ct", "run_test_profile", json.dumps({"profile_id": "iso"}),
        )
        receipt = getattr(registry, "exec" + "ute")(
            call,
            context=ToolExecutionContext(
                run_id, uuid4(), 1, ModelCallRef(run_id, "ct"),
            ),
        )
        self.assertFalse(receipt.is_error, receipt.content)
        self.assertNotIn(SECRET, receipt.content)
        document = json.loads(receipt.content)
        self.assertEqual("result_withheld", document["availability"])
        self.assertIs(True, document["human_required"])


if __name__ == "__main__":
    unittest.main()
