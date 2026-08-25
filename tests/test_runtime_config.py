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
    McpServerConfig,
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
        # I4 strict loader: json.dumps writes NaN; the strict parser rejects
        # the non-finite token before provider_options are even interpreted.
        document = self._base_document()
        document["provider"]["provider_options"] = {"thinking": float("nan")}
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(_write_config(self.root, document))
        self.assertEqual("config_non_finite_number", raised.exception.code)

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



class McpServerDeadlinesConfigTest(unittest.TestCase):
    """I1 3.3: staged MCP deadlines, limits, and legacy-timeout mapping."""

    _DEADLINE_RANGES = {
        "process_start_timeout_seconds": (0.1, 600.0),
        "initialize_timeout_seconds": (0.1, 600.0),
        "tools_list_timeout_seconds": (0.1, 600.0),
        "tool_call_timeout_seconds": (0.1, 600.0),
        "io_poll_timeout_seconds": (0.01, 5.0),
        "shutdown_timeout_seconds": (0.1, 60.0),
    }
    _LIMIT_RANGES = {
        "max_pending_requests": (1, 4096),
        "max_inbound_messages": (1, 65_536),
        "max_list_pages": (1, 1024),
        "max_tools": (1, 16_384),
        "max_notifications_per_window": (1, 65_536),
        "max_cursor_bytes": (1, 1_048_576),
        "max_frame_bytes": (1, 16 * 1024 * 1024),
        "max_stderr_bytes": (1, 16 * 1024 * 1024),
        "max_result_bytes": (1, 16 * 1024 * 1024),
    }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "repo").mkdir()

    def _base(self, server: dict | None = None) -> dict:
        if server is None:
            server = {"server_id": "echo", "command": ["python", "-m", "unittest"]}
        return {
            "repo": "repo",
            "db": "agent.sqlite3",
            "provider": {
                "base_url": "https://api.example.test/v1",
                "api_key_env": "EnvKey",
                "model": "test-model",
            },
            "sandbox": {"runner": "host", "host_trust": "builtin_fixture"},
            "test_profiles": [
                {
                    "profile_id": "unit",
                    "argv": ["python", "-m", "unittest"],
                    "timeout_seconds": 60,
                }
            ],
            "policy": {"patch_decision": "allow"},
            "system_prompt": "s",
            "mcp_servers": [server],
        }

    def test_staged_deadline_and_limit_defaults(self) -> None:
        # 3.3 table: independent per-phase defaults, new limits, no deprecation.
        config = load_runtime_config(_write_config(self.root, self._base()))
        server = config.mcp_servers[0]
        self.assertEqual(30.0, server.process_start_timeout_seconds)
        self.assertEqual(30.0, server.initialize_timeout_seconds)
        self.assertEqual(30.0, server.tools_list_timeout_seconds)
        self.assertEqual(15.0, server.tool_call_timeout_seconds)
        self.assertEqual(0.25, server.io_poll_timeout_seconds)
        self.assertEqual(5.0, server.shutdown_timeout_seconds)
        self.assertEqual(15.0, server.request_timeout_seconds)
        self.assertFalse(server.request_timeout_deprecation)
        self.assertEqual(64, server.max_pending_requests)
        self.assertEqual(1024, server.max_inbound_messages)
        self.assertEqual(32, server.max_list_pages)
        self.assertEqual(512, server.max_tools)
        self.assertEqual(64, server.max_notifications_per_window)
        self.assertEqual(4096, server.max_cursor_bytes)
        self.assertEqual(1_048_576, server.max_frame_bytes)
        self.assertEqual(262_144, server.max_stderr_bytes)
        self.assertEqual(1_048_576, server.max_result_bytes)

    def test_explicit_staged_values_take_effect(self) -> None:
        server = {
            "server_id": "echo",
            "command": ["python", "-m", "unittest"],
            "process_start_timeout_seconds": 60.0,
            "initialize_timeout_seconds": 45.0,
            "tools_list_timeout_seconds": 40.0,
            "tool_call_timeout_seconds": 20.0,
            "io_poll_timeout_seconds": 1.0,
            "shutdown_timeout_seconds": 10.0,
            "max_pending_requests": 8,
            "max_inbound_messages": 2048,
            "max_list_pages": 4,
            "max_tools": 64,
            "max_notifications_per_window": 5,
            "max_cursor_bytes": 512,
            "max_frame_bytes": 4096,
            "max_stderr_bytes": 2048,
            "max_result_bytes": 8192,
        }
        parsed = load_runtime_config(_write_config(self.root, self._base(server))).mcp_servers[0]
        self.assertEqual(60.0, parsed.process_start_timeout_seconds)
        self.assertEqual(45.0, parsed.initialize_timeout_seconds)
        self.assertEqual(40.0, parsed.tools_list_timeout_seconds)
        self.assertEqual(20.0, parsed.tool_call_timeout_seconds)
        self.assertEqual(1.0, parsed.io_poll_timeout_seconds)
        self.assertEqual(10.0, parsed.shutdown_timeout_seconds)
        self.assertEqual(8, parsed.max_pending_requests)
        self.assertEqual(2048, parsed.max_inbound_messages)
        self.assertEqual(4, parsed.max_list_pages)
        self.assertEqual(64, parsed.max_tools)
        self.assertEqual(5, parsed.max_notifications_per_window)
        self.assertEqual(512, parsed.max_cursor_bytes)
        self.assertEqual(4096, parsed.max_frame_bytes)
        self.assertEqual(2048, parsed.max_stderr_bytes)
        self.assertEqual(8192, parsed.max_result_bytes)

    def test_startup_and_tool_call_timeouts_are_independent(self) -> None:
        # 3.7: a short tool-call deadline must not constrain the cold-start
        # handshake deadline (startup/initialize/list use new defaults).
        server = {
            "server_id": "echo",
            "command": ["python", "-m", "unittest"],
            "tool_call_timeout_seconds": 1.0,
        }
        parsed = load_runtime_config(_write_config(self.root, self._base(server))).mcp_servers[0]
        self.assertEqual(1.0, parsed.tool_call_timeout_seconds)
        self.assertEqual(30.0, parsed.process_start_timeout_seconds)
        self.assertEqual(30.0, parsed.initialize_timeout_seconds)
        self.assertEqual(30.0, parsed.tools_list_timeout_seconds)

    def test_each_deadline_accepts_boundary_and_rejects_over_boundary(self) -> None:
        # 3.3/3.7: boundaries accepted; out-of-range rejected as invalid_mcp_server.
        for name, (minimum, maximum) in self._DEADLINE_RANGES.items():
            for boundary in (minimum, maximum):
                with self.subTest(name=name, boundary=boundary):
                    server = {"server_id": "echo", "command": ["python", "-m", "unittest"], name: boundary}
                    parsed = load_runtime_config(_write_config(self.root, self._base(server))).mcp_servers[0]
                    self.assertEqual(boundary, getattr(parsed, name))
            for rejected in (minimum - 1.0, maximum + 1.0):
                with self.subTest(name=name, rejected=rejected):
                    server = {"server_id": "echo", "command": ["python", "-m", "unittest"], name: rejected}
                    with self.assertRaises(RuntimeConfigError) as raised:
                        load_runtime_config(_write_config(self.root, self._base(server)))
                    self.assertEqual("invalid_mcp_server", raised.exception.code)

    def test_each_limit_accepts_boundary_and_rejects_over_boundary(self) -> None:
        # 3.3: limit boundaries accepted; zero/over-max rejected as invalid_mcp_server.
        for name, (minimum, maximum) in self._LIMIT_RANGES.items():
            for boundary in (minimum, maximum):
                with self.subTest(name=name, boundary=boundary):
                    server = {"server_id": "echo", "command": ["python", "-m", "unittest"], name: boundary}
                    parsed = load_runtime_config(_write_config(self.root, self._base(server))).mcp_servers[0]
                    self.assertEqual(boundary, getattr(parsed, name))
            for rejected in (0, maximum + 1):
                with self.subTest(name=name, rejected=rejected):
                    server = {"server_id": "echo", "command": ["python", "-m", "unittest"], name: rejected}
                    with self.assertRaises(RuntimeConfigError) as raised:
                        load_runtime_config(_write_config(self.root, self._base(server)))
                    self.assertEqual("invalid_mcp_server", raised.exception.code)

    def test_legacy_request_timeout_maps_only_to_tool_call(self) -> None:
        # 3.3: one-compat-version legacy field maps to tool_call_timeout_seconds
        # and flips request_timeout_deprecation; other phases keep new defaults.
        server = {
            "server_id": "echo",
            "command": ["python", "-m", "unittest"],
            "request_timeout_seconds": 7.5,
        }
        parsed = load_runtime_config(_write_config(self.root, self._base(server))).mcp_servers[0]
        self.assertTrue(parsed.request_timeout_deprecation)
        self.assertEqual(7.5, parsed.request_timeout_seconds)
        self.assertEqual(7.5, parsed.tool_call_timeout_seconds)
        self.assertEqual(30.0, parsed.process_start_timeout_seconds)
        self.assertEqual(30.0, parsed.initialize_timeout_seconds)
        self.assertEqual(30.0, parsed.tools_list_timeout_seconds)
        self.assertEqual(0.25, parsed.io_poll_timeout_seconds)
        self.assertEqual(5.0, parsed.shutdown_timeout_seconds)

    def test_legacy_timeout_with_any_new_timeout_is_ambiguous(self) -> None:
        # 3.3: legacy field together with any new timeout is rejected.
        for name in self._DEADLINE_RANGES:
            with self.subTest(name=name):
                server = {
                    "server_id": "echo",
                    "command": ["python", "-m", "unittest"],
                    "request_timeout_seconds": 7.5,
                    name: 3.0,
                }
                with self.assertRaises(RuntimeConfigError) as raised:
                    load_runtime_config(_write_config(self.root, self._base(server)))
                self.assertEqual("ambiguous_mcp_timeout_config", raised.exception.code)

    def test_unknown_top_level_field_is_still_rejected(self) -> None:
        # No regression: unknown config keys keep failing with config_unknown_field.
        document = self._base()
        document["unknown_option"] = 1
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(_write_config(self.root, document))
        self.assertEqual("config_unknown_field", raised.exception.code)

    def test_unknown_mcp_server_field_is_still_rejected(self) -> None:
        # No regression: unknown per-server keys fail as invalid_mcp_server.
        server = {
            "server_id": "echo",
            "command": ["python", "-m", "unittest"],
            "bogus_field": 1,
        }
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(_write_config(self.root, self._base(server)))
        self.assertEqual("invalid_mcp_server", raised.exception.code)

    def test_direct_construction_rejects_bool_deadline_values(self) -> None:
        # 3.3: values must be finite floats, not bools.
        for name in self._DEADLINE_RANGES:
            with self.subTest(name=name):
                with self.assertRaises(RuntimeConfigError) as raised:
                    McpServerConfig(
                        server_id="echo",
                        command=("python", "-m", "unittest"),
                        **{name: True},
                    )
                self.assertEqual("invalid_mcp_server", raised.exception.code)

    def test_direct_construction_rejects_non_finite_floats(self) -> None:
        # 3.3: inf/-inf/NaN deadlines are rejected.
        for name, value in (
            ("process_start_timeout_seconds", float("inf")),
            ("initialize_timeout_seconds", float("-inf")),
            ("tools_list_timeout_seconds", float("nan")),
        ):
            with self.subTest(name=name):
                with self.assertRaises(RuntimeConfigError) as raised:
                    McpServerConfig(
                        server_id="echo",
                        command=("python", "-m", "unittest"),
                        **{name: value},
                    )
                self.assertEqual("invalid_mcp_server", raised.exception.code)

    def test_direct_construction_rejects_negative_and_zero(self) -> None:
        # 3.3: negative/zero deadlines and limits are rejected.
        for name, value in (
            ("process_start_timeout_seconds", -1.0),
            ("tool_call_timeout_seconds", 0.0),
            ("shutdown_timeout_seconds", 0.0),
            ("max_tools", -3),
            ("max_pending_requests", 0),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(RuntimeConfigError) as raised:
                    McpServerConfig(
                        server_id="echo",
                        command=("python", "-m", "unittest"),
                        **{name: value},
                    )
                self.assertEqual("invalid_mcp_server", raised.exception.code)

    def test_direct_construction_rejects_bool_limit_values(self) -> None:
        # 3.3: limits must be ints, not bools.
        for name in self._LIMIT_RANGES:
            with self.subTest(name=name):
                with self.assertRaises(RuntimeConfigError) as raised:
                    McpServerConfig(
                        server_id="echo",
                        command=("python", "-m", "unittest"),
                        **{name: True},
                    )
                self.assertEqual("invalid_mcp_server", raised.exception.code)

    def test_direct_construction_accepts_valid_staged_deadlines(self) -> None:
        # Sanity: a valid direct construction round-trips its staged values.
        server = McpServerConfig(
            server_id="echo",
            command=("python", "-m", "unittest"),
            process_start_timeout_seconds=12.0,
            io_poll_timeout_seconds=0.25,
        )
        self.assertEqual(12.0, server.process_start_timeout_seconds)
        self.assertEqual(0.25, server.io_poll_timeout_seconds)
        self.assertEqual(15.0, server.tool_call_timeout_seconds)


class StrictConfigLoaderTest(unittest.TestCase):
    """I4 6.5: bounded file bytes, strict parse, exact keys, secret rejection."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "repo").mkdir()

    def write_bytes(self, data: bytes) -> Path:
        path = self.root / "config.json"
        path.write_bytes(data)
        return path

    def base_document(self) -> dict:
        return {
            "config_schema_version": 2,
            "repo": "repo",
            "db": "agent.sqlite3",
            "provider": {
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key_env": "SF_CodingAgentTestKey",
                "model": "Qwen/Qwen3-8B",
            },
            "sandbox": {
                "runner": "host",
                "host_trust": "builtin_fixture",
            },
            "policy": {"patch_decision": "allow"},
            "test_profiles": [
                {"profile_id": "unit", "argv": ["/usr/local/bin/python", "-m", "unittest"]},
            ],
        }

    def load(self, document: dict):
        path = self.write_bytes(json.dumps(document).encode("utf-8"))
        return load_runtime_config(path)

    def test_config_file_too_large_is_rejected(self) -> None:
        from koawa_agent_v2.runtime.config import CONFIG_MAX_BYTES

        path = self.write_bytes(b" " * (CONFIG_MAX_BYTES + 1))
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(path)
        self.assertEqual("config_file_too_large", raised.exception.code)

    def test_nested_duplicate_key_is_rejected(self) -> None:
        """A duplicate key at ANY depth is rejected before JSON folding."""
        path = self.write_bytes(
            '{"config_schema_version":2,"provider":{"base_url":"https://x","api_key_env":"K","model":"m",'
            '"provider_options":{"temperature":0.7,"temperature":0.8}},"repo":"repo","db":"d.sqlite3",'
            '"sandbox":{"runner":"host"},"test_profiles":[{"profile_id":"u","argv":["p"]}]}'.encode("utf-8"),
        )
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(path)
        self.assertEqual("config_duplicate_key", raised.exception.code)

    def test_non_finite_number_token_is_rejected(self) -> None:
        """NaN/Infinity tokens never silently round-trip into a config."""
        path = self.write_bytes(b'{"config_schema_version":2,"temperature":NaN}')
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(path)
        self.assertEqual("config_non_finite_number", raised.exception.code)

    def test_bad_utf8_file_is_rejected(self) -> None:
        path = self.write_bytes(b"{\xff\xfe\x00 invalid utf8 }")
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(path)
        self.assertEqual("config_file_invalid", raised.exception.code)

    def test_config_json_limit_plus_one_is_rejected(self) -> None:
        """CONFIG_READ_V1 depth cap: a deeply nested document is rejected."""
        deep = {"nested": 1}
        for _ in range(30):
            deep = {"nested": deep}
        document = self.base_document()
        document["provider"]["provider_options"] = deep
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("config_json_limit_exceeded", raised.exception.code)

    def test_config_schema_version_present_but_not_two_is_rejected(self) -> None:
        for wrong in (1, 3, "2", True):
            document = self.base_document()
            document["config_schema_version"] = wrong
            with self.assertRaises(RuntimeConfigError) as raised:
                self.load(document)
            self.assertEqual("config_unsupported_schema_version", raised.exception.code)

    def test_legacy_v1_config_loads_with_deprecation(self) -> None:
        """Missing config_schema_version follows the single v1 translator."""
        import warnings

        document = self.base_document()
        del document["config_schema_version"]
        document["provider"]["provider_options"] = {"thinking": {"type": "disabled"}}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            config = self.load(document)
        self.assertEqual(1, config.config_schema_version)
        self.assertGreaterEqual(len(caught), 1)
        self.assertIsInstance(caught[0].message, DeprecationWarning)
        self.assertEqual(
            {"type": "disabled"},
            dict(config.provider.provider_options)["thinking"],
        )

    def test_v2_provider_options_allowlist_and_secret_key_rejection(self) -> None:
        """v2 enforces the positive allowlist and rejects secret-shaped keys."""
        document = self.base_document()
        document["provider"]["provider_options"] = {"thinking": {"type": "disabled"}}
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("invalid_provider_options", raised.exception.code)
        document = self.base_document()
        document["provider"]["provider_options"] = {"api_key": "sk-abc1234567890"}
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("config_secret_in_generic_field", raised.exception.code)
        document = self.base_document()
        document["provider"]["provider_options"] = {
            "temperature": 0.7,
            "parallel_tool_calls": True,
            "stop": ["END", "STOP"],
            "response_format": {"type": "json_object"},
        }
        config = self.load(document)
        self.assertEqual(0.7, dict(config.provider.provider_options)["temperature"])

    def test_v2_provider_option_value_semantics_are_enforced(self) -> None:
        """temp out of -2..2 and wrong-typed options are rejected."""
        document = self.base_document()
        document["provider"]["provider_options"] = {"temperature": 3.5}
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("invalid_provider_options", raised.exception.code)
        document = self.base_document()
        document["provider"]["provider_options"] = {"seed": "not-an-int"}
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("invalid_provider_options", raised.exception.code)

    def test_credential_literals_in_argv_and_env_are_rejected(self) -> None:
        """Executable args and env entries never carry credential literals."""
        document = self.base_document()
        document["mcp_servers"] = [
            {"server_id": "echo", "command": ["python", "sk-abc1234567890xyz"]},
        ]
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("config_secret_in_generic_field", raised.exception.code)
        document = self.base_document()
        document["test_profiles"][0]["environment"] = [["API_KEY", "whatever"]]
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("config_secret_in_generic_field", raised.exception.code)
        document = self.base_document()
        document["test_profiles"][0]["environment"] = [["PLAIN", "bearer abcdefghijklmnop"]]
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("config_secret_in_generic_field", raised.exception.code)

    def test_durable_limits_ingress_exact_keys_and_bounds(self) -> None:
        """All eleven keys required; partial policies never merge implicitly."""
        from koawa_agent_v2.control.durable_json import INGRESS_DEFAULTS

        document = self.base_document()
        document["durable_limits"] = dict(INGRESS_DEFAULTS)
        config = self.load(document)
        self.assertEqual(
            INGRESS_DEFAULTS["user_input_max_utf8_bytes"],
            config.durable_limits["user_input_max_utf8_bytes"],
        )
        partial = dict(INGRESS_DEFAULTS)
        del partial["user_input_max_utf8_bytes"]
        document = self.base_document()
        document["durable_limits"] = partial
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("invalid_durable_limits", raised.exception.code)
        out_of_range = dict(INGRESS_DEFAULTS)
        out_of_range["event_payload_max_depth"] = 33
        document = self.base_document()
        document["durable_limits"] = out_of_range
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("invalid_durable_limits", raised.exception.code)

    def test_system_prompt_canonicalized_and_bounded(self) -> None:
        """system_prompt goes through CanonicalText and is bounded."""
        document = self.base_document()
        document["system_prompt"] = "You are safe. sk-abc1234567890xyz now."
        config = self.load(document)
        self.assertNotIn("sk-abc1234567890xyz", config.system_prompt)
        self.assertIn("[REDACTED]", config.system_prompt)
        document = self.base_document()
        document["system_prompt"] = "x" * 200_000
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("config_text_limit_exceeded", raised.exception.code)

    def test_section_limits_round_robin(self) -> None:
        """§6.5 collection/number caps reject out-of-range values stably."""
        document = self.base_document()
        document["model_rounds"] = 257
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("invalid_model_rounds", raised.exception.code)
        document = self.base_document()
        document["lease_seconds"] = 2
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("invalid_lease_seconds", raised.exception.code)
        document = self.base_document()
        document["history_max_chars"] = 5_000_000
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("invalid_history_max_chars", raised.exception.code)
        document = self.base_document()
        document["compact_min_turns"] = 2
        document["history_max_turns"] = 1
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("invalid_compact_min_turns", raised.exception.code)
        document = self.base_document()
        document["provider"]["max_output_tokens"] = 2_000_000
        with self.assertRaises(RuntimeConfigError) as raised:
            self.load(document)
        self.assertEqual("invalid_max_output_tokens", raised.exception.code)

    def test_config_failure_creates_no_db_and_no_mcp_process(self) -> None:
        """A rejected config never creates the DB file or spawns anything."""
        import subprocess

        document = self.base_document()
        document["provider"]["provider_options"] = {"api_key": "sk-abc1234567890"}
        path = self.write_bytes(json.dumps(document).encode("utf-8"))
        with patch.object(subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeConfigError):
                load_runtime_config(path)
            popen.assert_not_called()
        database = (self.root / "agent.sqlite3").resolve()
        self.assertFalse(database.exists())

if __name__ == "__main__":
    unittest.main()
