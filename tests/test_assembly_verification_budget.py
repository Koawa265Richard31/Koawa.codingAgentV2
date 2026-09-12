"""Audit F7 + F2 regressions.

F7: production assembly must derive VerificationLimits.max_test_runs from
the configured required profiles (the JSON config admits 64 profiles while
the default budget was 4, making 5+ profiles structurally unsatisfiable).
F2: the strict JSON loader must admit the canary_key_env activation field
(the parser already read it; only the unknown-field whitelist rejected it).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.runtime import assembly as assembly_module
from koawa_agent_v2.runtime.app import AppRuntime
from koawa_agent_v2.runtime.config import load_runtime_config


def _git_init(root: Path) -> None:
    subprocess.run(
        ("git", "-C", str(root), "init", "-q"),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def _write_config(root: Path, *, required: list[str], canary: str | None) -> Path:
    document = {
        "config_schema_version": 3,
        "repo": "repo",
        "db": "state.sqlite3",
        "provider": {
            "base_url": "https://provider.example/v1",
            "api_key_env": "KOAWA_PROVIDER_KEY",
            "model": "test-model",
            "provider": "siliconflow",
        },
        "sandbox": {"runner": "host"},
        "test_profiles": [
            {
                "profile_id": name,
                "argv": ["python", "-c", "pass"],
                "timeout_seconds": 10,
                "max_stdout_bytes": 1024,
                "max_stderr_bytes": 1024,
            }
            for name in ("p1", "p2", "p3", "p4", "p5")
        ],
        "required_test_profiles": required,
        "policy": {
            "policy_version": "policy-v1",
            "read_decision": "allow",
            "patch_decision": "allow",
            "test_decision": "allow",
            "principal_scopes": ["workspace.read"],
        },
        "mcp_servers": [],
    }
    if canary is not None:
        document["canary_key_env"] = canary
    path = root / "cfg.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class AssemblyVerificationBudgetTest(unittest.TestCase):
    def test_required_profiles_drive_derived_test_budget(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-f7-") as directory:
            root = Path(directory)
            (root / "repo").mkdir()
            _git_init(root / "repo")
            config_path = _write_config(
                root, required=["p1", "p2", "p3", "p4", "p5"], canary=None
            )
            app = AppRuntime.from_config_file(config_path)
            self.addCleanup(getattr(app, "close", lambda: None))
            captured: dict[str, object] = {}
            original = assembly_module.build_verified_coding_tool_registry

            def capture(*args, **kwargs):
                captured["verification_limits"] = kwargs.get("verification_limits")
                return original(*args, **kwargs)

            previous = os.environ.get("KOAWA_PROVIDER_KEY")
            os.environ["KOAWA_PROVIDER_KEY"] = "k" * 40
            assembly_module.build_verified_coding_tool_registry = capture
            try:
                app._ensure_execution_plane()
            finally:
                assembly_module.build_verified_coding_tool_registry = original
                if previous is None:
                    os.environ.pop("KOAWA_PROVIDER_KEY", None)
                else:
                    os.environ["KOAWA_PROVIDER_KEY"] = previous
            limits = captured["verification_limits"]
            self.assertIsNotNone(limits)
            self.assertEqual(20, limits.max_test_runs)


class CanaryKeyEnvLoaderTest(unittest.TestCase):
    def test_loader_admits_canary_key_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-f2-") as directory:
            root = Path(directory)
            (root / "repo").mkdir()
            _git_init(root / "repo")
            config_path = _write_config(
                root,
                required=["p1"],
                canary="KOAWA_TEST_CANARY_KEY",
            )
            config = load_runtime_config(config_path)
            self.assertEqual("KOAWA_TEST_CANARY_KEY", config.canary_key_env)

    def test_loader_still_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-f2-") as directory:
            root = Path(directory)
            (root / "repo").mkdir()
            _git_init(root / "repo")
            config_path = _write_config(root, required=["p1"], canary=None)
            document = json.loads(config_path.read_text(encoding="utf-8"))
            document["totally_unknown"] = 1
            config_path.write_text(json.dumps(document), encoding="utf-8")
            from koawa_agent_v2.runtime.config import RuntimeConfigError

            with self.assertRaises(RuntimeConfigError) as raised:
                load_runtime_config(config_path)
            self.assertEqual("config_unknown_field", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
