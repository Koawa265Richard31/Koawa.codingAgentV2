"""P0 runtime configuration: strict JSON config for the real model runtime.

The config file is trusted local administrator input.  It never contains API
keys; a provider key is loaded only from the environment variable named by
``api_key_env``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..model.openai_client import ReasoningEffort, reasoning_family
from ..policy import Decision

_CONFIG_ERROR = re.compile(r"[a-z][a-z0-9_]{0,127}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_PROFILE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_MCP_SERVER_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")


class RuntimeConfigError(RuntimeError):
    """Stable, content-free configuration failure safe to print to an operator."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _CONFIG_ERROR.fullmatch(code):
            raise ValueError("invalid runtime config error code")
        self.code = code
        super().__init__(code)


class SandboxRunner(StrEnum):
    DOCKER = "docker"
    HOST = "host"


class RepositoryTrustMode(StrEnum):
    UNTRUSTED = "untrusted"
    BUILTIN_FIXTURE = "builtin_fixture"
    USER_CONFIRMED = "user_confirmed"


@dataclass(frozen=True, slots=True, repr=False)
class ProviderConfig:
    base_url: str
    api_key_env: str
    model: str
    provider: str = "siliconflow"
    timeout_seconds: float = 120.0
    max_stream_seconds: float = 600.0
    max_output_tokens: int = 4096
    max_request_bytes: int = 2 * 1024 * 1024
    max_response_bytes: int = 16 * 1024 * 1024
    max_sse_event_bytes: int = 2 * 1024 * 1024
    # Provider-specific request body overrides (e.g. {"thinking":
    # {"type": "disabled"}} for SiliconFlow Qwen3.5). Stored as sorted
    # (key, value) pairs; values must be JSON-safe.
    provider_options: tuple[tuple[str, object], ...] = ()
    # User-facing reasoning intensity knob (off|low|medium|high). The client
    # translates it per provider/model family; unknown families fail closed
    # and direct the operator to provider_options. None = leave provider default.
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _non_empty_text(self.base_url, "base_url"))
        if not self.base_url.startswith(("http://", "https://")):
            raise RuntimeConfigError("invalid_provider_base_url")
        if not _ENV_NAME.fullmatch(self.api_key_env):
            raise RuntimeConfigError("invalid_api_key_env")
        object.__setattr__(self, "model", _non_empty_text(self.model, "model"))
        object.__setattr__(
            self, "provider", _provider_name(self.provider, "provider")
        )
        for name, value in (
            ("timeout_seconds", self.timeout_seconds),
            ("max_stream_seconds", self.max_stream_seconds),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or float(value) <= 0
                or float(value) > 86_400
            ):
                raise RuntimeConfigError(f"invalid_{name}")
            object.__setattr__(self, name, float(value))
        for name, value in (
            ("max_output_tokens", self.max_output_tokens),
            ("max_request_bytes", self.max_request_bytes),
            ("max_response_bytes", self.max_response_bytes),
            ("max_sse_event_bytes", self.max_sse_event_bytes),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > 128 * 1024 * 1024
            ):
                raise RuntimeConfigError(f"invalid_{name}")
        if not isinstance(self.provider_options, tuple) or any(
            not isinstance(pair, tuple) or len(pair) != 2 for pair in self.provider_options
        ):
            raise RuntimeConfigError("invalid_provider_options")
        normalized: list[tuple[str, object]] = []
        seen_keys: set[str] = set()
        for key, value in self.provider_options:
            if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
                raise RuntimeConfigError("invalid_provider_options")
            if key in seen_keys:
                raise RuntimeConfigError("duplicate_provider_option")
            if not _json_safe(value):
                raise RuntimeConfigError("invalid_provider_options")
            seen_keys.add(key)
            normalized.append((key, value))
        object.__setattr__(
            self, "provider_options", tuple(sorted(normalized, key=lambda pair: pair[0]))
        )
        if self.reasoning_effort is not None:
            try:
                ReasoningEffort(self.reasoning_effort)
            except ValueError:
                raise RuntimeConfigError("invalid_reasoning_effort") from None
            if reasoning_family(self.provider, self.model) is None:
                raise RuntimeConfigError("reasoning_effort_unsupported")

    def __repr__(self) -> str:
        return (
            f"ProviderConfig(base_url={self.base_url!r}, "
            f"api_key_env={self.api_key_env!r}, model={self.model!r}, "
            f"provider={self.provider!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class TestProfileConfig:
    profile_id: str
    argv: tuple[str, ...]
    timeout_seconds: float = 120.0
    max_stdout_bytes: int = 256_000
    max_stderr_bytes: int = 256_000
    environment: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not _PROFILE_ID.fullmatch(self.profile_id):
            raise RuntimeConfigError("invalid_test_profile")
        if not isinstance(self.argv, tuple) or not 1 <= len(self.argv) <= 128:
            raise RuntimeConfigError("invalid_test_profile")
        for value in self.argv:
            if (
                not isinstance(value, str)
                or not value
                or "\x00" in value
                or len(value) > 16_384
            ):
                raise RuntimeConfigError("invalid_test_profile")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or float(self.timeout_seconds) <= 0
            or float(self.timeout_seconds) > 3_600
        ):
            raise RuntimeConfigError("invalid_test_profile")
        for value in (self.max_stdout_bytes, self.max_stderr_bytes):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > 16 * 1024 * 1024
            ):
                raise RuntimeConfigError("invalid_test_profile")
        if not isinstance(self.environment, tuple):
            raise RuntimeConfigError("invalid_test_profile")
        seen: set[str] = set()
        normalized: list[tuple[str, str]] = []
        for item in self.environment:
            if not isinstance(item, tuple) or len(item) != 2:
                raise RuntimeConfigError("invalid_test_profile")
            name, value = item
            if (
                not isinstance(name, str)
                or name in seen
                or not isinstance(value, str)
                or "\x00" in value
                or len(value) > 16_384
            ):
                raise RuntimeConfigError("invalid_test_profile")
            seen.add(name)
            normalized.append((name, value))
        object.__setattr__(self, "environment", tuple(normalized))

    def __repr__(self) -> str:
        return (
            f"TestProfileConfig(profile_id={self.profile_id!r}, "
            f"argv_count={len(self.argv)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class McpServerConfig:
    server_id: str
    command: tuple[str, ...]
    cwd: Path | None = None
    environment: tuple[tuple[str, str], ...] = ()
    request_timeout_seconds: float = 15.0
    decision: Decision = Decision.ASK
    side_effect_class: str = "read_only"
    recovery_mode: str = "retry"

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, str) or not _MCP_SERVER_ID.fullmatch(
            self.server_id
        ):
            raise RuntimeConfigError("invalid_mcp_server")
        object.__setattr__(self, "server_id", self.server_id)
        if not isinstance(self.command, tuple) or not 1 <= len(self.command) <= 128:
            raise RuntimeConfigError("invalid_mcp_server")
        for value in self.command:
            if (
                not isinstance(value, str)
                or not value
                or "\x00" in value
                or len(value) > 16_384
            ):
                raise RuntimeConfigError("invalid_mcp_server")
        if self.cwd is not None and not isinstance(self.cwd, Path):
            raise RuntimeConfigError("invalid_mcp_server")
        if not isinstance(self.environment, tuple):
            raise RuntimeConfigError("invalid_mcp_server")
        normalized_environment: list[tuple[str, str]] = []
        for item in self.environment:
            if not isinstance(item, tuple) or len(item) != 2:
                raise RuntimeConfigError("invalid_mcp_server")
            name, value = item
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(value, str)
                or "\x00" in value
                or len(value) > 16_384
            ):
                raise RuntimeConfigError("invalid_mcp_server")
            normalized_environment.append((name, value))
        object.__setattr__(self, "environment", tuple(normalized_environment))
        if (
            not isinstance(self.request_timeout_seconds, (int, float))
            or isinstance(self.request_timeout_seconds, bool)
            or float(self.request_timeout_seconds) <= 0
            or float(self.request_timeout_seconds) > 600
        ):
            raise RuntimeConfigError("invalid_mcp_server")
        if not isinstance(self.decision, Decision):
            raise RuntimeConfigError("invalid_mcp_server")
        if self.side_effect_class not in {
            "read_only",
            "idempotent_write",
            "non_idempotent_write",
        }:
            raise RuntimeConfigError("invalid_mcp_server")
        if self.recovery_mode not in {"retry", "authoritative_query", "manual"}:
            raise RuntimeConfigError("invalid_mcp_server")
        if (
            self.side_effect_class == "non_idempotent_write"
            and self.recovery_mode == "retry"
        ):
            raise RuntimeConfigError("invalid_mcp_server")

    def __repr__(self) -> str:
        return (
            f"McpServerConfig(server_id={self.server_id!r}, "
            f"command_count={len(self.command)}, decision={self.decision.value!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SandboxConfig:
    runner: SandboxRunner = SandboxRunner.DOCKER
    image_id: str | None = None
    docker_executable: str = "docker"
    host_trust: RepositoryTrustMode = RepositoryTrustMode.USER_CONFIRMED

    def __post_init__(self) -> None:
        if not isinstance(self.runner, SandboxRunner):
            raise RuntimeConfigError("invalid_sandbox_runner")
        if self.runner is SandboxRunner.DOCKER:
            object.__setattr__(
                self,
                "image_id",
                _non_empty_text(
                    None if self.image_id is None else self.image_id,
                    "image_id",
                ),
            )
        elif self.image_id is not None:
            raise RuntimeConfigError("image_id_requires_docker")
        object.__setattr__(
            self,
            "docker_executable",
            _non_empty_text(self.docker_executable, "docker_executable"),
        )
        if not isinstance(self.host_trust, RepositoryTrustMode):
            raise RuntimeConfigError("invalid_host_trust")

    def __repr__(self) -> str:
        return (
            f"SandboxConfig(runner={self.runner.value!r}, "
            f"image_id_present={self.image_id is not None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PolicyConfig:
    policy_version: str = "policy-v1"
    read_decision: Decision = Decision.ALLOW
    patch_decision: Decision = Decision.ALLOW
    test_decision: Decision = Decision.ALLOW
    principal_scopes: tuple[str, ...] = (
        "workspace.read",
        "workspace.write",
        "sandbox.test",
        "mcp.use",
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_version",
            _provider_name(self.policy_version, "policy_version"),
        )
        for name in ("read_decision", "patch_decision", "test_decision"):
            value = getattr(self, name)
            if not isinstance(value, Decision):
                raise RuntimeConfigError(f"invalid_{name}")
        if not isinstance(self.principal_scopes, tuple) or not all(
            isinstance(value, str) and value for value in self.principal_scopes
        ):
            raise RuntimeConfigError("invalid_principal_scopes")
        object.__setattr__(
            self, "principal_scopes", tuple(dict.fromkeys(self.principal_scopes))
        )

    def __repr__(self) -> str:
        return (
            f"PolicyConfig(version={self.policy_version!r}, "
            f"read={self.read_decision.value!r}, "
            f"patch={self.patch_decision.value!r}, "
            f"test={self.test_decision.value!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeConfig:
    repo: Path
    db: Path
    provider: ProviderConfig
    sandbox: SandboxConfig
    test_profiles: tuple[TestProfileConfig, ...]
    policy: PolicyConfig
    system_prompt: str
    mcp_servers: tuple[McpServerConfig, ...] = ()
    owner_id: str = "runtime-cli"
    lease_seconds: int = 30
    model_rounds: int = 32
    max_tool_calls: int = 128

    def __post_init__(self) -> None:
        if not isinstance(self.repo, Path) or not self.repo.is_absolute():
            raise RuntimeConfigError("invalid_repo_path")
        if not isinstance(self.db, Path) or not self.db.is_absolute():
            raise RuntimeConfigError("invalid_db_path")
        # Runtime state (event store/ledger/approvals) must never live inside the
        # verified repo: the GitFacade baseline would capture it as an untracked
        # dirty path and the D5 finalization gate would reject every run.
        if self.db.is_relative_to(self.repo):
            raise RuntimeConfigError("db_inside_repo")
        for value in (self.provider, self.sandbox, self.policy):
            expected = (
                ProviderConfig
                if value is self.provider
                else SandboxConfig
                if value is self.sandbox
                else PolicyConfig
            )
            if not isinstance(value, expected):
                raise RuntimeConfigError("invalid_runtime_config")
        if not isinstance(self.test_profiles, tuple) or not self.test_profiles:
            raise RuntimeConfigError("test_profiles_required")
        if any(not isinstance(value, TestProfileConfig) for value in self.test_profiles):
            raise RuntimeConfigError("invalid_test_profile")
        if len({value.profile_id for value in self.test_profiles}) != len(
            self.test_profiles
        ):
            raise RuntimeConfigError("duplicate_test_profile")
        if not isinstance(self.mcp_servers, tuple) or any(
            not isinstance(value, McpServerConfig) for value in self.mcp_servers
        ):
            raise RuntimeConfigError("invalid_mcp_server")
        if len({value.server_id for value in self.mcp_servers}) != len(
            self.mcp_servers
        ):
            raise RuntimeConfigError("duplicate_mcp_server")
        object.__setattr__(
            self, "system_prompt", _non_empty_text(self.system_prompt, "system_prompt")
        )
        object.__setattr__(
            self, "owner_id", _provider_name(self.owner_id, "owner_id")
        )
        for name, value in (("lease_seconds", self.lease_seconds),):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > 3_600
            ):
                raise RuntimeConfigError(f"invalid_{name}")
        for name, value in (
            ("model_rounds", self.model_rounds),
            ("max_tool_calls", self.max_tool_calls),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise RuntimeConfigError(f"invalid_{name}")

    def __repr__(self) -> str:
        return (
            f"RuntimeConfig(repo={str(self.repo)!r}, db={str(self.db)!r}, "
            f"provider={self.provider!r}, sandbox={self.sandbox!r}, "
            f"test_profile_count={len(self.test_profiles)})"
        )


DEFAULT_SYSTEM_PROMPT = """You are KoawaAgent V2, a local durable coding agent.
Always reply to the user in Chinese.
Before every tool call, write one short Chinese progress line explaining what you are about to do and why.
After a tool result arrives, write one short Chinese progress line with the outcome and your next step.
Do not wait until the final answer to report progress.

Work inside the provided Git repository only.

Protocol:
1. Inspect with read_file, list_files, or search_text before editing.
2. Edit with apply_patch. UPDATE and DELETE require the exact base_sha256 returned by read_file.
3. After editing, run one of the pre-registered test profiles with run_test_profile.
4. Inspect git_status and git_diff.
5. Call finalize_task only after tests pass and status/diff describe the current generation.
6. If finalize_task returns an error, repair the problem and gather fresh evidence.
7. Never guess shell syntax or files not returned by read_file/list_files/search_text.

Final answer must summarize changed files, test evidence, and any residual risks in Chinese."""


def load_runtime_config(path: str | Path) -> RuntimeConfig:
    """Load and strictly validate a P0 runtime JSON config."""
    config_path = Path(path)
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeConfigError("config_file_not_found") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeConfigError("config_file_invalid") from None
    if not isinstance(document, dict):
        raise RuntimeConfigError("config_file_invalid")
    allowed = {
        "repo",
        "db",
        "provider",
        "sandbox",
        "test_profiles",
        "policy",
        "mcp_servers",
        "system_prompt",
        "owner_id",
        "lease_seconds",
        "model_rounds",
        "max_tool_calls",
    }
    unknown = set(document) - allowed
    if unknown:
        raise RuntimeConfigError("config_unknown_field")
    base = config_path.parent
    repo = _absolute_path(document.get("repo"), base, "repo")
    db = _absolute_path(document.get("db"), base, "db")
    provider = _parse_provider(document.get("provider"))
    sandbox = _parse_sandbox(document.get("sandbox"))
    test_profiles = _parse_test_profiles(document.get("test_profiles"))
    policy = _parse_policy(document.get("policy"))
    mcp_servers = _parse_mcp_servers(document.get("mcp_servers", []), base)
    system_prompt = document.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
    owner_id = document.get("owner_id", "runtime-cli")
    lease_seconds = document.get("lease_seconds", 30)
    model_rounds = document.get("model_rounds", 32)
    max_tool_calls = document.get("max_tool_calls", 128)
    return RuntimeConfig(
        repo=repo,
        db=db,
        provider=provider,
        sandbox=sandbox,
        test_profiles=test_profiles,
        policy=policy,
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        owner_id=owner_id,
        lease_seconds=lease_seconds,
        model_rounds=model_rounds,
        max_tool_calls=max_tool_calls,
    )


def resolve_api_key(provider: ProviderConfig) -> str:
    """Read the configured provider key from the process environment only."""
    value = os.environ.get(provider.api_key_env)
    if value is None or not value or value != value.strip():
        raise RuntimeConfigError("api_key_missing")
    if "\r" in value or "\n" in value:
        raise RuntimeConfigError("api_key_invalid")
    return value


def _parse_provider(value: Any) -> ProviderConfig:
    if not isinstance(value, dict):
        raise RuntimeConfigError("invalid_provider_config")
    allowed = {
        "base_url",
        "api_key_env",
        "model",
        "provider",
        "timeout_seconds",
        "max_stream_seconds",
        "max_output_tokens",
        "max_request_bytes",
        "max_response_bytes",
        "max_sse_event_bytes",
        "provider_options",
        "reasoning_effort",
    }
    _reject_unknown(value, allowed, "invalid_provider_config")
    try:
        raw_options = value.get("provider_options")
        options: tuple[tuple[str, object], ...] = ()
        if raw_options is not None:
            if not isinstance(raw_options, dict):
                raise RuntimeConfigError("invalid_provider_options")
            options = tuple((str(key), item) for key, item in raw_options.items())
        return ProviderConfig(
            base_url=value.get("base_url", ""),
            api_key_env=value.get("api_key_env", ""),
            model=value.get("model", ""),
            provider=value.get("provider", "siliconflow"),
            timeout_seconds=value.get("timeout_seconds", 120.0),
            max_stream_seconds=value.get("max_stream_seconds", 600.0),
            max_output_tokens=value.get("max_output_tokens", 4096),
            max_request_bytes=value.get("max_request_bytes", 2 * 1024 * 1024),
            max_response_bytes=value.get("max_response_bytes", 16 * 1024 * 1024),
            max_sse_event_bytes=value.get("max_sse_event_bytes", 2 * 1024 * 1024),
            provider_options=options,
            reasoning_effort=value.get("reasoning_effort"),
        )
    except RuntimeConfigError:
        raise
    except (TypeError, ValueError):
        raise RuntimeConfigError("invalid_provider_config") from None


def _parse_sandbox(value: Any) -> SandboxConfig:
    if not isinstance(value, dict):
        raise RuntimeConfigError("invalid_sandbox_config")
    allowed = {"runner", "image_id", "docker_executable", "host_trust"}
    _reject_unknown(value, allowed, "invalid_sandbox_config")
    try:
        runner_value = value.get("runner", "docker")
        runner = (
            runner_value
            if isinstance(runner_value, SandboxRunner)
            else SandboxRunner(runner_value)
        )
        host_trust_value = value.get("host_trust", "user_confirmed")
        host_trust = (
            host_trust_value
            if isinstance(host_trust_value, RepositoryTrustMode)
            else RepositoryTrustMode(host_trust_value)
        )
        return SandboxConfig(
            runner=runner,
            image_id=value.get("image_id"),
            docker_executable=value.get("docker_executable", "docker"),
            host_trust=host_trust,
        )
    except RuntimeConfigError:
        raise
    except (TypeError, ValueError):
        raise RuntimeConfigError("invalid_sandbox_config") from None


def _parse_test_profiles(value: Any) -> tuple[TestProfileConfig, ...]:
    if not isinstance(value, list) or not value:
        raise RuntimeConfigError("invalid_test_profiles")
    profiles: list[TestProfileConfig] = []
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeConfigError("invalid_test_profile")
        allowed = {
            "profile_id",
            "argv",
            "timeout_seconds",
            "max_stdout_bytes",
            "max_stderr_bytes",
            "environment",
        }
        _reject_unknown(item, allowed, "invalid_test_profile")
        try:
            argv = tuple(item.get("argv", ()))
            environment = tuple(tuple(pair) for pair in item.get("environment", ()))
            profiles.append(
                TestProfileConfig(
                    profile_id=item.get("profile_id", ""),
                    argv=argv,
                    timeout_seconds=item.get("timeout_seconds", 120.0),
                    max_stdout_bytes=item.get("max_stdout_bytes", 256_000),
                    max_stderr_bytes=item.get("max_stderr_bytes", 256_000),
                    environment=environment,
                )
            )
        except RuntimeConfigError:
            raise
        except (TypeError, ValueError):
            raise RuntimeConfigError("invalid_test_profile") from None
    return tuple(profiles)


def _parse_mcp_servers(value: Any, base: Path) -> tuple[McpServerConfig, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimeConfigError("invalid_mcp_server")
    servers: list[McpServerConfig] = []
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeConfigError("invalid_mcp_server")
        allowed = {
            "server_id",
            "command",
            "cwd",
            "environment",
            "request_timeout_seconds",
            "decision",
            "side_effect_class",
            "recovery_mode",
        }
        _reject_unknown(item, allowed, "invalid_mcp_server")
        try:
            command = tuple(item.get("command", ()))
            cwd_value = item.get("cwd")
            cwd = (
                None
                if cwd_value is None
                else _absolute_path(cwd_value, base, "mcp_cwd")
            )
            environment = tuple(
                tuple(pair) for pair in item.get("environment", ())
            )
            servers.append(
                McpServerConfig(
                    server_id=item.get("server_id", ""),
                    command=command,
                    cwd=cwd,
                    environment=environment,
                    request_timeout_seconds=item.get(
                        "request_timeout_seconds", 15.0
                    ),
                    decision=_decision(item.get("decision", "ask")),
                    side_effect_class=item.get("side_effect_class", "read_only"),
                    recovery_mode=item.get("recovery_mode", "retry"),
                )
            )
        except RuntimeConfigError:
            raise
        except (TypeError, ValueError):
            raise RuntimeConfigError("invalid_mcp_server") from None
    return tuple(servers)


def _parse_policy(value: Any) -> PolicyConfig:
    if not isinstance(value, dict):
        raise RuntimeConfigError("invalid_policy_config")
    allowed = {
        "policy_version",
        "read_decision",
        "patch_decision",
        "test_decision",
        "principal_scopes",
    }
    _reject_unknown(value, allowed, "invalid_policy_config")
    try:
        return PolicyConfig(
            policy_version=value.get("policy_version", "policy-v1"),
            read_decision=_decision(value.get("read_decision", "allow")),
            patch_decision=_decision(value.get("patch_decision", "allow")),
            test_decision=_decision(value.get("test_decision", "allow")),
            principal_scopes=tuple(
                value.get(
                    "principal_scopes",
                    (
                        "workspace.read",
                        "workspace.write",
                        "sandbox.test",
                        "mcp.use",
                    ),
                )
            ),
        )
    except RuntimeConfigError:
        raise
    except (TypeError, ValueError):
        raise RuntimeConfigError("invalid_policy_config") from None


def _decision(value: Any) -> Decision:
    if isinstance(value, Decision):
        return value
    if not isinstance(value, str):
        raise RuntimeConfigError("invalid_policy_decision")
    try:
        return Decision(value)
    except ValueError:
        raise RuntimeConfigError("invalid_policy_decision") from None


def _absolute_path(value: Any, base: Path, code: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeConfigError(f"invalid_{code}_path")
    expanded = os.path.expandvars(value)
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _reject_unknown(value: dict[str, Any], allowed: set[str], code: str) -> None:
    if set(value) - allowed:
        raise RuntimeConfigError(code)


def _json_safe(value: Any) -> bool:
    """Return True when value survives lossless JSON round-tripping."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, (list, tuple)):
        return all(_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _json_safe(item) for key, item in value.items()
        )
    return False


def _non_empty_text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RuntimeConfigError(f"invalid_{code}")
    if any(
        (ord(char) < 32 and char not in "\n\t") or ord(char) == 127
        for char in value
    ):
        raise RuntimeConfigError(f"invalid_{code}")
    return value


def _provider_name(value: Any, code: str) -> str:
    text = _non_empty_text(value, code)
    if len(text) > 64 or not all(char.isalnum() or char in "_-" for char in text):
        raise RuntimeConfigError(f"invalid_{code}")
    return text
