"""D25 W1: sandboxed config typing and container launch identity contracts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.mcp.activation import resolve_launch_identity
from koawa_agent_v2.runtime.config import (
    McpExecutionProfile,
    McpResourceLimits,
    McpServerConfig,
    RuntimeConfigError,
    load_runtime_config,
)

DIGEST = "sha256:" + "a" * 64
DIGEST2 = "sha256:" + "b" * 64


def _sandboxed(**overrides) -> McpServerConfig:
    values = dict(
        server_id="svc",
        command=("/usr/local/bin/node", "server.js"),
        environment=(("MCP_MODE", "stdio"),),
        execution_profile=McpExecutionProfile.SANDBOXED,
        image_id=DIGEST,
        resource_limits=McpResourceLimits(),
        container_working_directory="/work",
    )
    values.update(overrides)
    return McpServerConfig(**values)


class SandboxConfigValidationTest(unittest.TestCase):
    def test_valid_sandboxed_config_round_trips(self) -> None:
        config = _sandboxed()
        self.assertEqual(McpExecutionProfile.SANDBOXED, config.execution_profile)
        self.assertIsNone(config.cwd)
        self.assertEqual("/work", config.container_working_directory)
        self.assertFalse(config.legacy_fixture)

    def test_tag_and_malformed_image_ids_rejected(self) -> None:
        for bad in ("node:20", "node", "sha256:xyz", "docker://node",
                    "SHA256:" + "a" * 64, "sha256:" + "A" * 64):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeConfigError) as raised:
                    _sandboxed(image_id=bad)
                self.assertEqual(
                    "mcp_sandbox_image_digest_required",
                    raised.exception.args[0],
                )

    def test_relative_and_windows_argv0_rejected(self) -> None:
        for bad in (("node", "x.js"), (".\\node.exe", "x"), ("C:/node", "x")):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeConfigError) as raised:
                    _sandboxed(command=bad)
                self.assertEqual(
                    "mcp_sandbox_container_command_required",
                    raised.exception.args[0],
                )

    def test_host_cwd_forbidden_container_cwd_required(self) -> None:
        with self.assertRaises(RuntimeConfigError) as raised:
            _sandboxed(cwd=Path("/tmp"))
        self.assertEqual("mcp_sandbox_host_cwd_forbidden", raised.exception.args[0])
        with self.assertRaises(RuntimeConfigError) as raised:
            _sandboxed(container_working_directory=None)
        self.assertEqual(
            "mcp_sandbox_container_cwd_required", raised.exception.args[0]
        )

    def test_container_cwd_shape_rejects_relative_and_traversal(self) -> None:
        for bad in ("work", "/work/../etc", "/work/./x", "/work//x", "C:\\work", "/work\x00"):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeConfigError) as raised:
                    _sandboxed(container_working_directory=bad)
                self.assertEqual(
                    "invalid_mcp_container_working_directory",
                    raised.exception.args[0],
                )

    def test_limits_mounts_and_artifacts_contracts(self) -> None:
        with self.assertRaises(RuntimeConfigError) as raised:
            _sandboxed(resource_limits=None)
        self.assertEqual("mcp_sandbox_limits_required", raised.exception.args[0])
        with self.assertRaises(RuntimeConfigError) as raised:
            _sandboxed(read_only_mounts=(("/tmp/x", "/data"),))
        self.assertEqual("mcp_sandbox_zero_mounts_required", raised.exception.args[0])
        from koawa_agent_v2.runtime.config import McpCodeArtifact

        with self.assertRaises(RuntimeConfigError) as raised:
            _sandboxed(code_artifacts=(McpCodeArtifact("bundle", 1),))
        self.assertEqual(
            "mcp_sandbox_code_artifacts_forbidden", raised.exception.args[0]
        )

    def test_secret_environment_rejected(self) -> None:
        for env in (
            (("API_KEY", "value"),),
            (("MCP_TOKEN", "value"),),
            (("MY_SECRET", "value"),),
            (("PASSWORD", "x"),),
            (("PLAIN_NAME", "sk-abcdef123456"),),
            (("PLAIN_NAME", "Bearer abcdef.ghijkl"),),
        ):
            with self.subTest(env=env[0][0]):
                with self.assertRaises(RuntimeConfigError) as raised:
                    _sandboxed(environment=env)
                self.assertEqual(
                    "mcp_sandbox_environment_secret_forbidden",
                    raised.exception.args[0],
                )

    def test_container_cwd_profile_boundaries(self) -> None:
        with self.assertRaises(RuntimeConfigError) as raised:
            _sandboxed(
                execution_profile=McpExecutionProfile.HOST_TRUSTED,
                image_id=None,
            )
        self.assertEqual(
            "mcp_host_trusted_no_container_cwd", raised.exception.args[0]
        )
        with self.assertRaises(RuntimeConfigError) as raised:
            McpServerConfig(
                server_id="legacy",
                command=("python", "srv.py"),
                container_working_directory="/work",
            )
        self.assertEqual(
            "mcp_container_cwd_requires_sandbox", raised.exception.args[0]
        )

    def test_legacy_fixture_marker_only_for_none_profile(self) -> None:
        config = McpServerConfig(
            server_id="legacy",
            command=("python", "srv.py"),
            legacy_fixture=True,
        )
        self.assertIsNone(config.execution_profile)
        with self.assertRaises(RuntimeConfigError):
            _sandboxed(legacy_fixture=True)

    def test_loader_rejects_legacy_fixture_flag_and_accepts_container_cwd(self) -> None:
        import json

        document = {
            "config_schema_version": 3,
            "repo": ".",
            "db": "./agent.sqlite3",
            "provider": {
                "base_url": "https://provider.example/v1",
                "api_key_env": "K",
                "model": "m",
                "provider": "openai_compatible",
            },
            "sandbox": {"runner": "host", "host_trust": "builtin_fixture"},
            "test_profiles": [
                {"profile_id": "p", "argv": ["python", "-c", "pass"]}
            ],
            "policy": {"patch_decision": "ask"},
            "mcp_servers": [],
        }
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            document["repo"] = str(base)
            document["db"] = str(Path(base).parent / "outside-db.sqlite3")
            path = base / "config.json"
            document["mcp_servers"] = [
                {
                    "server_id": "svc",
                    "command": ["/usr/local/bin/node", "server.js"],
                    "execution_profile": "sandboxed",
                    "image_id": DIGEST,
                    "resource_limits": {"cpus": 1.0},
                    "container_working_directory": "/work",
                }
            ]
            path.write_text(json.dumps(document), encoding="utf-8")
            loaded = load_runtime_config(path)
            server = loaded.mcp_servers[0]
            self.assertEqual("/work", server.container_working_directory)
            self.assertFalse(server.legacy_fixture)
            document["mcp_servers"][0]["legacy_fixture"] = True
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(RuntimeConfigError):
                load_runtime_config(path)


class SandboxLaunchIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_identity_built_without_any_host_lookup(self) -> None:
        import koawa_agent_v2.mcp.activation as activation

        original = activation._resolve_executable

        def forbidden(*_args, **_kwargs):
            raise AssertionError("host executable lookup during sandboxed identity")

        activation._resolve_executable = forbidden
        try:
            identity = resolve_launch_identity(_sandboxed(), base_dir=self.base)
        finally:
            activation._resolve_executable = original
        self.assertEqual("sandboxed", identity.execution_profile)
        self.assertEqual((), identity.code_artifacts)
        self.assertEqual((), identity.read_only_mounts)
        self.assertIsNotNone(identity.image_digest)
        self.assertIsNotNone(identity.resource_digest)

    def test_identity_digest_sensitivity_and_order_independence(self) -> None:
        first = resolve_launch_identity(_sandboxed(), base_dir=self.base)
        again = resolve_launch_identity(_sandboxed(), base_dir=self.base)
        self.assertEqual(first.config_digest, again.config_digest)
        other_image = resolve_launch_identity(
            _sandboxed(image_id=DIGEST2), base_dir=self.base
        )
        self.assertNotEqual(first.config_digest, other_image.config_digest)
        self.assertNotEqual(first.image_digest, other_image.image_digest)
        other_cwd = resolve_launch_identity(
            _sandboxed(container_working_directory="/srv"),
            base_dir=self.base,
        )
        self.assertNotEqual(first.config_digest, other_cwd.config_digest)
        # Environment entries are semantically order-independent: the identity
        # must not change when the same pairs arrive in a different order.
        reordered = resolve_launch_identity(
            _sandboxed(environment=(("MCP_MODE", "stdio"), ("EXTRA", "1"))),
            base_dir=self.base,
        )
        flipped = resolve_launch_identity(
            _sandboxed(environment=(("EXTRA", "1"), ("MCP_MODE", "stdio"))),
            base_dir=self.base,
        )
        self.assertEqual(reordered.config_digest, flipped.config_digest)
        # ... but a value change is a real identity change.
        changed_value = resolve_launch_identity(
            _sandboxed(environment=(("MCP_MODE", "http"),)),
            base_dir=self.base,
        )
        self.assertNotEqual(first.config_digest, changed_value.config_digest)

    def test_host_trusted_identity_unchanged(self) -> None:
        executable = self.base / "srv.py"
        executable.write_text("print('server')\n", encoding="utf-8")
        config = McpServerConfig(
            server_id="host",
            command=(sys_executable(), str(executable)),
            execution_profile=McpExecutionProfile.HOST_TRUSTED,
            resource_limits=McpResourceLimits(),
        )
        identity = resolve_launch_identity(config, base_dir=self.base)
        self.assertEqual("host_trusted", identity.execution_profile)
        self.assertTrue(identity.code_artifacts)
        self.assertIsNone(identity.image_digest)
        # host-trusted keeps a real host code-artifact identity for argv[0]:
        # an executable role bound to a platform file id + content hash.
        first = identity.code_artifacts[0]
        self.assertEqual("executable", first.role)
        self.assertEqual(0, first.argv_index)
        self.assertTrue(first.source.platform_file_id)
        self.assertEqual(64, len(first.source.content_sha256))


def sys_executable() -> str:
    import sys

    return sys.executable


if __name__ == "__main__":
    unittest.main()
