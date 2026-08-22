from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from koawa_agent_v2.policy import Decision
from koawa_agent_v2.runtime.config import (
    DEFAULT_SYSTEM_PROMPT,
    ProviderConfig,
    RuntimeConfig,
    RuntimeConfigError,
    SandboxRunner,
    load_runtime_config,
    resolve_api_key,
)


def _write_config(root: Path, document: dict) -> Path:
    path = root / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class RuntimeConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "repo").mkdir()

    def _base_document(self) -> dict:
        return {
            "repo": "repo",
            "db": "agent.sqlite3",
            "provider": {
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key_env": "SF_CodingAgentTestKey",
                "model": "Qwen/Qwen3-8B",
            },
            "sandbox": {
                "runner": "docker",
                "image_id": "sha256:" + "a" * 64,
            },
            "test_profiles": [
                {
                    "profile_id": "python_unittest",
                    "argv": ["/usr/local/bin/python", "-m", "unittest"],
                }
            ],
            "policy": {"patch_decision": "ask"},
        }

    def test_valid_config_resolves_relative_paths(self) -> None:
        path = _write_config(self.root, self._base_document())
        config = load_runtime_config(path)
        self.assertEqual((self.root / "repo").resolve(), config.repo)
        self.assertEqual((self.root / "agent.sqlite3").resolve(), config.db)
        self.assertEqual("Qwen/Qwen3-8B", config.provider.model)
        self.assertEqual(SandboxRunner.DOCKER, config.sandbox.runner)
        self.assertEqual(Decision.ASK, config.policy.patch_decision)
        self.assertEqual(DEFAULT_SYSTEM_PROMPT, config.system_prompt)

    def test_provider_options_are_parsed_and_merged(self) -> None:
        document = self._base_document()
        document["provider"]["provider_options"] = {
            "thinking": {"type": "disabled"},
            "max_tokens": 2048,
        }
        config = load_runtime_config(_write_config(self.root, document))
        options = dict(config.provider.provider_options)
        self.assertEqual({"type": "disabled"}, options["thinking"])
        self.assertEqual(2048, options["max_tokens"])

    def test_provider_options_reject_non_json_safe_value(self) -> None:
        # Direct construction (not via JSON file) must reject values that
        # cannot round-trip losslessly, e.g. dict keys that are not strings.
        with self.assertRaises(RuntimeConfigError) as raised:
            ProviderConfig(
                base_url="https://api.siliconflow.cn/v1",
                api_key_env="SF_CodingAgentTestKey",
                model="test-model",
                provider_options=(("bad", {1: "x"}),),
            )
        self.assertEqual("invalid_provider_options", raised.exception.code)

    def test_provider_options_reject_nan_value(self) -> None:
        document = self._base_document()
        document["provider"]["provider_options"] = {"thinking": float("nan")}
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(_write_config(self.root, document))
        self.assertEqual("invalid_provider_options", raised.exception.code)

    def test_reasoning_effort_parsed_for_known_family(self) -> None:
        document = self._base_document()
        document["provider"]["model"] = "Qwen/Qwen3.5-35B-A3B"
        document["provider"]["reasoning_effort"] = "off"
        config = load_runtime_config(_write_config(self.root, document))
        self.assertEqual("off", config.provider.reasoning_effort)

    def test_reasoning_effort_rejects_invalid_value(self) -> None:
        document = self._base_document()
        document["provider"]["model"] = "Qwen/Qwen3.5-35B-A3B"
        document["provider"]["reasoning_effort"] = "turbo"
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(_write_config(self.root, document))
        self.assertEqual("invalid_reasoning_effort", raised.exception.code)

    def test_budget_action_limits_parsed_and_validated(self) -> None:
        document = self._base_document()
        document["budget_action_limits"] = {"root": 40, "reviewer": 5}
        config = load_runtime_config(_write_config(self.root, document))
        self.assertEqual((("reviewer", 5), ("root", 40)), config.budget_action_limits)

    def test_budget_action_limits_reject_non_positive(self) -> None:
        document = self._base_document()
        document["budget_action_limits"] = {"root": 0}
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(_write_config(self.root, document))
        self.assertEqual("invalid_budget_action_limits", raised.exception.code)

    def test_budget_action_limits_reject_duplicate_principal(self) -> None:
        document = self._base_document()
        document["budget_action_limits"] = {"root": 1}
        config = load_runtime_config(_write_config(self.root, document))
        # duplicate principal cannot be expressed in JSON; validate via direct build
        with self.assertRaises(RuntimeConfigError) as raised:
            RuntimeConfig(
                repo=self.root.resolve(),
                db=(self.root.parent / f"{self.root.name}-db.sqlite3").resolve(),
                provider=config.provider,
                sandbox=config.sandbox,
                test_profiles=config.test_profiles,
                policy=config.policy,
                system_prompt="s",
                budget_action_limits=(("root", 1), ("root", 2)),
            )
        self.assertEqual("duplicate_budget_principal", raised.exception.code)

    def test_reasoning_effort_rejects_unknown_family(self) -> None:
        document = self._base_document()
        document["provider"]["model"] = "some/unknown-model"
        document["provider"]["reasoning_effort"] = "high"
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(_write_config(self.root, document))
        self.assertEqual("reasoning_effort_unsupported", raised.exception.code)

    def test_unknown_field_is_rejected(self) -> None:
        document = self._base_document()
        document["provider"]["password"] = "nope"
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(_write_config(self.root, document))
        self.assertEqual("invalid_provider_config", raised.exception.code)

    def test_db_inside_repo_is_rejected(self) -> None:
        document = self._base_document()
        document["db"] = "repo/agent.sqlite3"
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(_write_config(self.root, document))
        self.assertEqual("db_inside_repo", raised.exception.code)

    def test_missing_test_profiles_is_rejected(self) -> None:
        document = self._base_document()
        document["test_profiles"] = []
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(_write_config(self.root, document))
        self.assertEqual("invalid_test_profiles", raised.exception.code)

    def test_duplicate_test_profile_is_rejected(self) -> None:
        document = self._base_document()
        document["test_profiles"].append(document["test_profiles"][0].copy())
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(_write_config(self.root, document))
        self.assertEqual("duplicate_test_profile", raised.exception.code)

    def test_mcp_server_config_is_parsed_and_validated(self) -> None:
        document = self._base_document()
        document["mcp_servers"] = [
            {
                "server_id": "echo",
                "command": ["python", "fixture_server.py"],
                "cwd": ".",
                "decision": "ask",
                "side_effect_class": "read_only",
                "recovery_mode": "retry",
            }
        ]
        config = load_runtime_config(_write_config(self.root, document))
        self.assertEqual(1, len(config.mcp_servers))
        self.assertEqual("echo", config.mcp_servers[0].server_id)
        self.assertEqual(self.root.resolve(), config.mcp_servers[0].cwd)

    def test_invalid_mcp_retry_for_non_idempotent_write_is_rejected(self) -> None:
        document = self._base_document()
        document["mcp_servers"] = [
            {
                "server_id": "echo",
                "command": ["python", "fixture_server.py"],
                "side_effect_class": "non_idempotent_write",
                "recovery_mode": "retry",
            }
        ]
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(_write_config(self.root, document))
        self.assertEqual("invalid_mcp_server", raised.exception.code)

    def test_resolve_api_key_uses_environment_only(self) -> None:
        config = load_runtime_config(_write_config(self.root, self._base_document()))
        with patch.dict(os.environ, {"SF_CodingAgentTestKey": " sk-key "}):
            with self.assertRaises(RuntimeConfigError) as raised:
                resolve_api_key(config.provider)
            self.assertEqual("api_key_missing", raised.exception.code)
        with patch.dict(os.environ, {"SF_CodingAgentTestKey": "sk-key"}):
            self.assertEqual("sk-key", resolve_api_key(config.provider))


if __name__ == "__main__":
    unittest.main()
