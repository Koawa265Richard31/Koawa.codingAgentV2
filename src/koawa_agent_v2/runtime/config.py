"""P0 runtime configuration: strict JSON config for the real model runtime.

The config file is trusted local administrator input.  It never contains API
keys; a provider key is loaded only from the environment variable named by
``api_key_env``.
"""

from __future__ import annotations

import json
import math
import os
import re
import warnings
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ..control.durable_json import (
    CONFIG_READ_V1,
    INSTRUCTION_MAX_UTF8_BYTES,
    CanonicalTextError,
    DurableJsonError,
    canonicalize_text,
    strict_json_loads_text,
    validate_runtime_ingress,
)
from ..recovery.redaction import _ASSIGNMENT, _BEARER, _OPENAI_KEY, _SENSITIVE_KEY
from ..model.openai_client import ReasoningEffort, reasoning_family
from ..policy import Decision
from .memory import MemoryConfig, MemoryConfigError

_CONFIG_ERROR = re.compile(r"[a-z][a-z0-9_]{0,127}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_PROFILE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_MCP_SERVER_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
# §8.3: loader / code-injection environment variables are hard-denied for
# every MCP server config (compared casefolded on every platform).
_MCP_INJECTION_ENV_CASEFOLD = frozenset({
    name.casefold()
    for name in (
        "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH", "PYTHONSTARTUP", "PYTHONINSPECT",
        "PYTHONPLUGLIBDIR", "BASH_ENV", "ENV", "PERL5LIB", "RUBYOPT",
        "NODE_OPTIONS",
    )
})

# D25 W1: sandboxed configs accept only a locally resolvable immutable image
# identity; tags and other reference forms never authorize execution.
_IMMUTABLE_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

# D25 W1 zero-secret baseline: credential-bearing names and credential-shaped
# values are rejected at the config boundary for sandboxed servers.
_MCP_SECRET_NAME_TOKENS = (
    "secret", "token", "password", "passwd", "credential", "api_key", "apikey",
)
_SECRET_VALUE_SHAPES = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}|-----BEGIN[ A-Z]+PRIVATE KEY-----)",
    re.ASCII,
)


def _validate_container_working_directory(value: str) -> None:
    """Container-absolute POSIX directory; host meaning can never leak in."""
    if not value.startswith("/") or "\\" in value or "\x00" in value:
        raise RuntimeConfigError("invalid_mcp_container_working_directory")
    if len(value.encode("utf-8")) > 4096:
        raise RuntimeConfigError("invalid_mcp_container_working_directory")
    parts = value.split("/")
    if value != "/" and any(part in ("", ".", "..") for part in parts[1:]):
        raise RuntimeConfigError("invalid_mcp_container_working_directory")

# §6.5: the config file is bounded before parsing (bytes, not characters).
CONFIG_MAX_BYTES = 1_048_576

# I4 顶层 schema：显式值必须为 2；缺失视为 legacy v1（经过单一兼容 translator +
# deprecation）。I6 把 schema 升为 3：v3 的每个 MCP server 必须显式声明
# execution_profile（§8.3），v1/v2 保持 legacy 兼容（profile 缺省 =
# execution_profile None，不自动变成 host_trusted）。
CONFIG_SCHEMA_VERSION = 3

# provider_options 的正向 allowlist（§6.5）：运行时拥有的 key 一律禁止。
_PROVIDER_OPTION_ALLOWLIST = frozenset(
    {
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "seed",
        "parallel_tool_calls",
        "service_tier",
        "stop",
        "response_format",
    }
)


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


class McpExecutionProfile(StrEnum):
    SANDBOXED = "sandboxed"
    HOST_TRUSTED = "host_trusted"


# §8.4 code-bearing argv roles. Only these explicit roles may carry code into
# the child; the runtime must never guess which argv entries are code.
MCP_CODE_ARTIFACT_ROLES = frozenset({
    "executable", "interpreter_script", "jar", "bundle"
})


@dataclass(frozen=True, slots=True, repr=False)
class McpCodeArtifact:
    """One declared code-bearing argv entry (role + position)."""

    role: str
    argv_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or self.role not in MCP_CODE_ARTIFACT_ROLES:
            raise RuntimeConfigError("invalid_mcp_code_artifact")
        if (
            not isinstance(self.argv_index, int)
            or isinstance(self.argv_index, bool)
            or self.argv_index < 0
        ):
            raise RuntimeConfigError("invalid_mcp_code_artifact")

    def __repr__(self) -> str:
        return f"McpCodeArtifact(role={self.role!r}, argv_index={self.argv_index})"


@dataclass(frozen=True, slots=True, repr=False)
class McpResourceLimits:
    """I6 §8.3 declarative resource envelope for one MCP process tree.

    These are a declaration, never a substitute for enforcement: the launcher
    must actually enforce every field through a Job Object / cgroup / wrapper
    or fail with ``mcp_host_limits_unsupported`` before spawning (doc §8.4).
    """

    cpus: float = 1.0
    memory_bytes: int = 512 * 1024 * 1024
    pids: int = 256
    tmpfs_bytes: int = 64 * 1024 * 1024
    process_count: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cpus, (int, float))
            or isinstance(self.cpus, bool)
            or not math.isfinite(float(self.cpus))
            or not 0.01 <= float(self.cpus) <= 1024.0
        ):
            raise RuntimeConfigError("invalid_mcp_resource_limits")
        for name, minimum, maximum in (
            ("memory_bytes", 1_048_576, 1_099_511_627_776),
            ("pids", 1, 1_048_576),
            ("tmpfs_bytes", 0, 1_099_511_627_776),
            ("process_count", 1, 65_536),
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not (minimum <= value <= maximum)
            ):
                raise RuntimeConfigError("invalid_mcp_resource_limits")

    def to_document(self) -> dict[str, object]:
        return {
            "cpus": float(self.cpus),
            "memory_bytes": self.memory_bytes,
            "pids": self.pids,
            "tmpfs_bytes": self.tmpfs_bytes,
            "process_count": self.process_count,
        }

    def __repr__(self) -> str:
        return (
            f"McpResourceLimits(cpus={self.cpus!r}, "
            f"memory_bytes={self.memory_bytes}, pids={self.pids})"
        )


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
        # §6.5 fixed ranges: provider deadline 0.1..600s, max stream 1..3600s.
        for name, minimum, maximum in (
            ("timeout_seconds", 0.1, 600.0),
            ("max_stream_seconds", 1.0, 3_600.0),
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < minimum
                or float(value) > maximum
            ):
                raise RuntimeConfigError(f"invalid_{name}")
            object.__setattr__(self, name, float(value))
        # §6.5 fixed ranges: request/response/SSE byte budgets and output tokens.
        for name, minimum, maximum in (
            ("max_output_tokens", 1, 1_048_576),
            ("max_request_bytes", 1_024, 4 * 1024 * 1024),
            ("max_response_bytes", 1_024, 32 * 1024 * 1024),
            ("max_sse_event_bytes", 1_024, 4 * 1024 * 1024),
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
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
        total_argv_bytes = 0
        for value in self.argv:
            if not isinstance(value, str) or not value or "\x00" in value:
                raise RuntimeConfigError("invalid_test_profile")
            entry_bytes = len(value.encode("utf-8"))
            if entry_bytes > 4_096:
                raise RuntimeConfigError("invalid_test_profile")
            total_argv_bytes += entry_bytes
        if total_argv_bytes > 65_536:
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
        if not isinstance(self.environment, tuple) or len(self.environment) > 128:
            raise RuntimeConfigError("invalid_test_profile")
        seen: set[str] = set()
        normalized: list[tuple[str, str]] = []
        total_env_bytes = 0
        for item in self.environment:
            if not isinstance(item, tuple) or len(item) != 2:
                raise RuntimeConfigError("invalid_test_profile")
            name, value = item
            if (
                not isinstance(name, str)
                or name in seen
                or not isinstance(value, str)
                or "\x00" in value
                or len(name.encode("utf-8")) > 128
                or len(value.encode("utf-8")) > 4_096
            ):
                raise RuntimeConfigError("invalid_test_profile")
            total_env_bytes += len(name.encode("utf-8")) + len(value.encode("utf-8"))
            seen.add(name)
            normalized.append((name, value))
        if total_env_bytes > 65_536:
            raise RuntimeConfigError("invalid_test_profile")
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
    # I1 staged deadlines: startup handshake must NOT inherit the short
    # tool-call deadline (Windows cold spawn measured 0.57-0.76s vs old 0.5s).
    process_start_timeout_seconds: float = 30.0
    initialize_timeout_seconds: float = 30.0
    tools_list_timeout_seconds: float = 30.0
    tool_call_timeout_seconds: float = 15.0
    io_poll_timeout_seconds: float = 0.25
    shutdown_timeout_seconds: float = 5.0
    max_pending_requests: int = 64
    max_inbound_messages: int = 1024
    max_list_pages: int = 32
    max_tools: int = 512
    max_notifications_per_window: int = 64
    max_cursor_bytes: int = 4096
    max_frame_bytes: int = 1_048_576
    max_stderr_bytes: int = 262_144
    max_result_bytes: int = 1_048_576
    request_timeout_deprecation: bool = False
    decision: Decision = Decision.ASK
    side_effect_class: str = "read_only"
    recovery_mode: str = "retry"
    # I6 §8.3 activation fields.  None = legacy config (fixture stdio
    # semantics); v3 JSON must always set an explicit profile.
    execution_profile: McpExecutionProfile | None = None
    image_id: str | None = None
    resource_limits: McpResourceLimits | None = None
    read_only_mounts: tuple[tuple[str, str], ...] = ()
    code_artifacts: tuple[McpCodeArtifact, ...] = ()
    # D25 W1: sandboxed containers get their own absolute POSIX working
    # directory; ``cwd`` keeps exclusive host-path semantics and both are
    # never allowed on the same config.
    container_working_directory: str | None = None
    # D25: admin-declared subset of server tool names to bind; None binds all
    # (and any schema outside the modeled subset fails the whole catalog).
    tool_allowlist: tuple[str, ...] | None = None
    # D25 W1: legacy (None-profile) configs are fixture-only.  The flag can
    # never arrive through a file document - the loader does not accept it -
    # so a direct constructor must mark legacy configs explicitly for the
    # normal assembly to keep the old allow decision.
    legacy_fixture: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, str) or not _MCP_SERVER_ID.fullmatch(
            self.server_id
        ):
            raise RuntimeConfigError("invalid_mcp_server")
        object.__setattr__(self, "server_id", self.server_id)
        if not isinstance(self.command, tuple) or not 1 <= len(self.command) <= 128:
            raise RuntimeConfigError("invalid_mcp_server")
        total_command_bytes = 0
        for value in self.command:
            if not isinstance(value, str) or not value or "\x00" in value:
                raise RuntimeConfigError("invalid_mcp_server")
            entry_bytes = len(value.encode("utf-8"))
            if entry_bytes > 4_096:
                raise RuntimeConfigError("invalid_mcp_server")
            total_command_bytes += entry_bytes
        if total_command_bytes > 65_536:
            raise RuntimeConfigError("invalid_mcp_server")
        if self.cwd is not None and not isinstance(self.cwd, Path):
            raise RuntimeConfigError("invalid_mcp_server")
        if not isinstance(self.environment, tuple) or len(self.environment) > 128:
            raise RuntimeConfigError("invalid_mcp_server")
        normalized_environment: list[tuple[str, str]] = []
        total_env_bytes = 0
        seen_env: set[str] = set()
        for item in self.environment:
            if not isinstance(item, tuple) or len(item) != 2:
                raise RuntimeConfigError("invalid_mcp_server")
            name, value = item
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(value, str)
                or "\x00" in value
                or len(name.encode("utf-8")) > 128
                or len(value.encode("utf-8")) > 4_096
            ):
                raise RuntimeConfigError("invalid_mcp_server")
            # §8.3: duplicate env keys are rejected (case-insensitive on
            # Windows); loader / code-injection variables are hard denied.
            env_key = name.casefold() if os.name == "nt" else name
            if env_key in seen_env:
                raise RuntimeConfigError("duplicate_mcp_environment")
            if env_key.casefold() in _MCP_INJECTION_ENV_CASEFOLD:
                raise RuntimeConfigError("mcp_injection_environment_forbidden")
            seen_env.add(env_key)
            total_env_bytes += len(name.encode("utf-8")) + len(value.encode("utf-8"))
            normalized_environment.append((name, value))
        if total_env_bytes > 65_536:
            raise RuntimeConfigError("invalid_mcp_server")
        object.__setattr__(self, "environment", tuple(normalized_environment))
        if (
            not isinstance(self.request_timeout_seconds, (int, float))
            or isinstance(self.request_timeout_seconds, bool)
            or float(self.request_timeout_seconds) <= 0
            or float(self.request_timeout_seconds) > 600
        ):
            raise RuntimeConfigError("invalid_mcp_server")
        for name, minimum, maximum in (
            ("process_start_timeout_seconds", 0.1, 600.0),
            ("initialize_timeout_seconds", 0.1, 600.0),
            ("tools_list_timeout_seconds", 0.1, 600.0),
            ("tool_call_timeout_seconds", 0.1, 600.0),
            ("io_poll_timeout_seconds", 0.01, 5.0),
            ("shutdown_timeout_seconds", 0.1, 60.0),
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < minimum
                or float(value) > maximum
            ):
                raise RuntimeConfigError("invalid_mcp_server")
        for name, maximum in (
            ("max_pending_requests", 4096),
            ("max_inbound_messages", 65536),
            ("max_list_pages", 1024),
            ("max_tools", 16384),
            ("max_notifications_per_window", 65536),
            ("max_cursor_bytes", 1_048_576),
            ("max_frame_bytes", 16 * 1024 * 1024),
            ("max_stderr_bytes", 16 * 1024 * 1024),
            ("max_result_bytes", 16 * 1024 * 1024),
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not (1 <= value <= maximum):
                raise RuntimeConfigError("invalid_mcp_server")
        if not isinstance(self.request_timeout_deprecation, bool):
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
        if self.execution_profile is not None and not isinstance(
            self.execution_profile, McpExecutionProfile
        ):
            raise RuntimeConfigError("invalid_mcp_server")
        if self.image_id is not None and (
            not isinstance(self.image_id, str)
            or not self.image_id.strip()
            or len(self.image_id.encode("utf-8")) > 512
        ):
            raise RuntimeConfigError("invalid_mcp_server")
        if not isinstance(self.legacy_fixture, bool):
            raise RuntimeConfigError("invalid_mcp_server")
        if self.container_working_directory is not None:
            if not isinstance(self.container_working_directory, str):
                raise RuntimeConfigError("invalid_mcp_server")
            _validate_container_working_directory(
                self.container_working_directory
            )
        if self.legacy_fixture and self.execution_profile is not None:
            raise RuntimeConfigError("invalid_mcp_server")
        if self.execution_profile is McpExecutionProfile.SANDBOXED:
            if not self.image_id:
                raise RuntimeConfigError("mcp_sandbox_requires_image")
            # D25 W1 invariants (§4): immutable image identity, container
            # argv/cwd semantics, zero mounts, zero code staging, bounded
            # resources, zero-secret environment - every failure is a stable
            # content-free code.
            if not _IMMUTABLE_IMAGE_DIGEST.fullmatch(self.image_id):
                raise RuntimeConfigError("mcp_sandbox_image_digest_required")
            if not self.command[0].startswith("/") or "\\" in self.command[0]:
                raise RuntimeConfigError("mcp_sandbox_container_command_required")
            if self.cwd is not None:
                raise RuntimeConfigError("mcp_sandbox_host_cwd_forbidden")
            if self.container_working_directory is None:
                raise RuntimeConfigError("mcp_sandbox_container_cwd_required")
            if not isinstance(self.resource_limits, McpResourceLimits):
                raise RuntimeConfigError("mcp_sandbox_limits_required")
            if self.read_only_mounts:
                raise RuntimeConfigError("mcp_sandbox_zero_mounts_required")
            if self.code_artifacts:
                raise RuntimeConfigError("mcp_sandbox_code_artifacts_forbidden")
            for name, value in self.environment:
                casefolded = name.casefold()
                if any(token in casefolded for token in _MCP_SECRET_NAME_TOKENS):
                    raise RuntimeConfigError("mcp_sandbox_environment_secret_forbidden")
                if _SECRET_VALUE_SHAPES.search(value):
                    raise RuntimeConfigError("mcp_sandbox_environment_secret_forbidden")
        elif self.execution_profile is McpExecutionProfile.HOST_TRUSTED:
            if self.image_id is not None:
                raise RuntimeConfigError("mcp_host_trusted_no_image")
            if self.container_working_directory is not None:
                raise RuntimeConfigError("mcp_host_trusted_no_container_cwd")
            if not isinstance(self.resource_limits, McpResourceLimits):
                raise RuntimeConfigError("invalid_mcp_resource_limits")
        else:
            # Legacy (None-profile): container-only fields must not creep in
            # outside an explicitly marked test fixture, and a file-borne
            # image_id means an unmigrated document.
            if self.container_working_directory is not None:
                raise RuntimeConfigError("mcp_container_cwd_requires_sandbox")
        if self.resource_limits is not None and not isinstance(
            self.resource_limits, McpResourceLimits
        ):
            raise RuntimeConfigError("invalid_mcp_resource_limits")
        if not isinstance(self.read_only_mounts, tuple) or len(self.read_only_mounts) > 32:
            raise RuntimeConfigError("invalid_mcp_server")
        for mount in self.read_only_mounts:
            if not isinstance(mount, tuple) or len(mount) != 2:
                raise RuntimeConfigError("invalid_mcp_server")
            source, container_path = mount
            if (
                not isinstance(source, str)
                or not source
                or not isinstance(container_path, str)
                or not container_path.startswith("/")
                or "\x00" in source
                or "\x00" in container_path
                or len(source.encode("utf-8")) > 4096
                or len(container_path.encode("utf-8")) > 4096
            ):
                raise RuntimeConfigError("invalid_mcp_server")
        if not isinstance(self.code_artifacts, tuple) or len(self.code_artifacts) > 16:
            raise RuntimeConfigError("invalid_mcp_server")
        seen_artifacts: set[int] = set()
        for artifact in self.code_artifacts:
            if not isinstance(artifact, McpCodeArtifact):
                raise RuntimeConfigError("invalid_mcp_server")
            index = artifact.argv_index
            if index in seen_artifacts or index >= len(self.command):
                raise RuntimeConfigError("invalid_mcp_server")
            if artifact.role == "executable" and index != 0:
                raise RuntimeConfigError("invalid_mcp_server")
            seen_artifacts.add(index)

    def __repr__(self) -> str:
        return (
            f"McpServerConfig(server_id={self.server_id!r}, "
            f"command_count={len(self.command)}, decision={self.decision.value!r}, "
            f"profile={None if self.execution_profile is None else self.execution_profile.value!r})"
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
    # D16 interactive session: bounded conversation history projection.
    history_max_turns: int = 16
    history_max_chars: int = 32_000
    compact_min_turns: int = 4
    # D23 unified memory envelope configuration (interactive + task paths).
    memory: MemoryConfig = MemoryConfig()
    # D22 F6b: optional summary-fallback model. 仅在回合主体完成但最终回复失败
    # 时，对一次摘要请求使用该模型（request-scoped，不切换会话模型）。
    # None/空 = 关闭（默认）：失败时只输出确定性摘要。
    fallback_summary_model: str | None = None
    # Per-principal tool action budget per run (D9). Sorted (principal, limit)
    # pairs; the interactive default is tight so small models cannot burn the
    # whole turn on repeated failed attempts.
    budget_action_limits: tuple[tuple[str, int], ...] = (("root", 20),)
    # I4 exact-key runtime ingress policy; None = section 6.2 defaults.  When
    # an object is supplied it must contain all eleven documented keys — no
    # partial implicit merging of policies.
    durable_limits: Mapping[str, int] | None = None
    # I4 top-level config schema: 2 when the file explicitly declares it,
    # 1 for legacy v1 input (single compatible translator + deprecation).
    config_schema_version: int = 1

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
        if not isinstance(self.memory, MemoryConfig):
            raise RuntimeConfigError("invalid_runtime_config")
        if not isinstance(self.test_profiles, tuple) or not self.test_profiles:
            raise RuntimeConfigError("test_profiles_required")
        if len(self.test_profiles) > 64:
            raise RuntimeConfigError("invalid_test_profiles")
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
        if len(self.mcp_servers) > 32:
            raise RuntimeConfigError("invalid_mcp_server")
        if len({value.server_id for value in self.mcp_servers}) != len(
            self.mcp_servers
        ):
            raise RuntimeConfigError("duplicate_mcp_server")
        if not isinstance(self.config_schema_version, int) or isinstance(
            self.config_schema_version, bool
        ):
            raise RuntimeConfigError("config_unsupported_schema_version")
        if self.config_schema_version not in (1, 2, 3):
            raise RuntimeConfigError("config_unsupported_schema_version")
        try:
            normalized_limits = validate_runtime_ingress(self.durable_limits)
        except ValueError:
            raise RuntimeConfigError("invalid_durable_limits") from None
        object.__setattr__(
            self, "durable_limits", MappingProxyType(normalized_limits)
        )
        object.__setattr__(
            self, "system_prompt", _non_empty_text(self.system_prompt, "system_prompt")
        )
        if self.fallback_summary_model is not None:
            value = self.fallback_summary_model
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 200
                or any(ord(char) < 32 for char in value)
            ):
                raise RuntimeConfigError("invalid_fallback_summary_model")
        object.__setattr__(
            self, "owner_id", _provider_name(self.owner_id, "owner_id")
        )
        # section 6.5 fixed ranges.
        if (
            not isinstance(self.lease_seconds, int)
            or isinstance(self.lease_seconds, bool)
            or not 3 <= self.lease_seconds <= 3_600
        ):
            raise RuntimeConfigError("invalid_lease_seconds")
        if (
            not isinstance(self.model_rounds, int)
            or isinstance(self.model_rounds, bool)
            or not 1 <= self.model_rounds <= 256
        ):
            raise RuntimeConfigError("invalid_model_rounds")
        if (
            not isinstance(self.max_tool_calls, int)
            or isinstance(self.max_tool_calls, bool)
            or not 1 <= self.max_tool_calls <= 1_024
        ):
            raise RuntimeConfigError("invalid_max_tool_calls")
        if (
            not isinstance(self.history_max_turns, int)
            or isinstance(self.history_max_turns, bool)
            or not 1 <= self.history_max_turns <= 1_024
        ):
            raise RuntimeConfigError("invalid_history_max_turns")
        if (
            not isinstance(self.history_max_chars, int)
            or isinstance(self.history_max_chars, bool)
            or not 1 <= self.history_max_chars <= 4_194_304
        ):
            raise RuntimeConfigError("invalid_history_max_chars")
        if (
            not isinstance(self.compact_min_turns, int)
            or isinstance(self.compact_min_turns, bool)
            or not 1 <= self.compact_min_turns <= max(1, self.history_max_turns)
        ):
            raise RuntimeConfigError("invalid_compact_min_turns")
        if not isinstance(self.budget_action_limits, tuple) or any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not _ENV_NAME.fullmatch(pair[0])
            or not isinstance(pair[1], int)
            or isinstance(pair[1], bool)
            or pair[1] <= 0
            for pair in self.budget_action_limits
        ):
            raise RuntimeConfigError("invalid_budget_action_limits")
        if len(self.budget_action_limits) > 1_024:
            raise RuntimeConfigError("invalid_budget_action_limits")
        if len({pair[0] for pair in self.budget_action_limits}) != len(
            self.budget_action_limits
        ):
            raise RuntimeConfigError("duplicate_budget_principal")
        object.__setattr__(
            self,
            "budget_action_limits",
            tuple(sorted(self.budget_action_limits, key=lambda pair: pair[0])),
        )

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
    """Load and strictly validate a P0 runtime JSON config (I4 strict loader).

    Step order (section 6.5): bound the file bytes, strict UTF-8 decode,
    reject duplicate keys and non-finite numbers during parsing, apply the
    immutable CONFIG_READ_V1 profile, accept only exact top-level key sets,
    preflight every DTO/path, canonicalize free text, validate the optional
    durable-limits ingress policy.  Only after the complete config validates
    may a DB, client or MCP process be created (assembly does that, not this).
    Config errors carry only a stable code and field path — never the secret
    value that triggered them.
    """
    config_path = Path(path)
    try:
        with open(config_path, "rb") as handle:
            raw = handle.read(CONFIG_MAX_BYTES + 1)
    except FileNotFoundError:
        raise RuntimeConfigError("config_file_not_found") from None
    except OSError:
        raise RuntimeConfigError("config_file_invalid") from None
    if len(raw) > CONFIG_MAX_BYTES:
        raise RuntimeConfigError("config_file_too_large")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise RuntimeConfigError("config_file_invalid") from None
    try:
        document = strict_json_loads_text(
            text,
            CONFIG_READ_V1,
            path="config",
            reject_string_controls=False,
        )
    except DurableJsonError as exc:
        raise _config_json_error(exc) from exc
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
        "history_max_turns",
        "history_max_chars",
        "compact_min_turns",
        "budget_action_limits",
        "fallback_summary_model",
        "durable_limits",
        "memory",
        "config_schema_version",
    }
    unknown = set(document) - allowed
    if unknown:
        raise RuntimeConfigError("config_unknown_field")

    # config_schema_version: explicit must be exactly 3 (I6).  Missing means
    # legacy v1 (single compatible translator + deprecation).  Legacy v1/v2
    # documents keep loading MCP servers without an execution_profile; v3
    # requires an explicit profile for every server (§8.3).
    raw_version = document.get("config_schema_version")
    if raw_version is None:
        schema_version = 1
        warnings.warn(
            "runtime config without config_schema_version is legacy v1 and "
            "deprecated; add \"config_schema_version\": 3",
            DeprecationWarning,
            stacklevel=2,
        )
    elif not isinstance(raw_version, int) or isinstance(raw_version, bool) or raw_version != CONFIG_SCHEMA_VERSION:
        raise RuntimeConfigError("config_unsupported_schema_version")
    else:
        schema_version = CONFIG_SCHEMA_VERSION

    strict_v3 = schema_version == CONFIG_SCHEMA_VERSION
    _reject_secret_config_literals(document)
    if "durable_limits" in document:
        try:
            durable_limits = validate_runtime_ingress(document["durable_limits"])
        except ValueError:
            raise RuntimeConfigError("invalid_durable_limits") from None
    else:
        durable_limits = None

    base = config_path.parent
    repo = _absolute_path(document.get("repo"), base, "repo")
    db = _absolute_path(document.get("db"), base, "db")
    provider = _parse_provider(document.get("provider"), strict_options=strict_v3)
    sandbox = _parse_sandbox(document.get("sandbox"))
    test_profiles = _parse_test_profiles(document.get("test_profiles"))
    policy = _parse_policy(document.get("policy"))
    mcp_servers = _parse_mcp_servers(
        document.get("mcp_servers", []), base, strict=strict_v3,
    )
    system_prompt = document.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
    if isinstance(system_prompt, str):
        try:
            canonical_prompt = canonicalize_text(
                system_prompt,
                INSTRUCTION_MAX_UTF8_BYTES,
                name="system_prompt",
            )
        except CanonicalTextError as exc:
            if exc.code == "text_too_large":
                raise RuntimeConfigError("config_text_limit_exceeded") from None
            raise RuntimeConfigError("invalid_system_prompt") from None
        system_prompt = canonical_prompt.value
    owner_id = document.get("owner_id", "runtime-cli")
    lease_seconds = document.get("lease_seconds", 30)
    model_rounds = document.get("model_rounds", 32)
    max_tool_calls = document.get("max_tool_calls", 128)
    history_max_turns = document.get("history_max_turns", 16)
    history_max_chars = document.get("history_max_chars", 32_000)
    compact_min_turns = document.get("compact_min_turns", 4)
    fallback_summary_model = document.get("fallback_summary_model")
    raw_budget = document.get("budget_action_limits", {"root": 20})
    if not isinstance(raw_budget, dict):
        raise RuntimeConfigError("invalid_budget_action_limits")
    budget_action_limits = tuple(
        (str(key), value) for key, value in raw_budget.items()
    )
    raw_memory = document.get("memory")
    try:
        memory = MemoryConfig.from_mapping(raw_memory)
    except MemoryConfigError:
        raise RuntimeConfigError("invalid_memory_config") from None
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
        history_max_turns=history_max_turns,
        history_max_chars=history_max_chars,
        compact_min_turns=compact_min_turns,
        memory=memory,
        budget_action_limits=budget_action_limits,
        fallback_summary_model=fallback_summary_model,
        durable_limits=durable_limits,
        config_schema_version=schema_version,
    )


def _config_json_error(exc: DurableJsonError) -> RuntimeConfigError:
    """Map strict-parse failures to stable, content-free config error codes."""
    if exc.code == "duplicate_key":
        return RuntimeConfigError("config_duplicate_key")
    if exc.code == "non_finite_number":
        return RuntimeConfigError("config_non_finite_number")
    if exc.code in ("invalid_json", "invalid_utf8", "invalid_value_type"):
        return RuntimeConfigError("config_file_invalid")
    return RuntimeConfigError("config_json_limit_exceeded")


def _credential_shape(value: str) -> bool:
    """True when a free-text value carries a credential-like literal shape."""
    return bool(
        _BEARER.search(value) or _OPENAI_KEY.search(value) or _ASSIGNMENT.search(value)
    )


def _reject_secret_config_literals(document: Mapping[str, Any]) -> None:
    """Reject credential-like literals in generic config fields.

    provider_options secret-shaped keys, executable argv entries and
    environment entries containing credential literals, and secret-like
    environment names are all rejected with the stable
    config_secret_in_generic_field code (plan §9.3, §6.5).  The error never
    echoes the offending value.
    """
    # provider_options KEYS are checked in _validate_provider_options: legacy
    # v1 keeps the I1-released general map (runtime-owned keys such as
    # max_tokens remain legal for old configurations), while v2 enforces the
    # positive allowlist and secret-shaped key rejection.  Credential-shaped
    # VALUES are always rejected here because they would reach the provider.
    provider = document.get("provider")
    if isinstance(provider, dict):
        raw_options = provider.get("provider_options")
        if isinstance(raw_options, dict):
            for option_value in raw_options.values():
                if isinstance(option_value, str) and _credential_shape(option_value):
                    raise RuntimeConfigError("config_secret_in_generic_field")
    for section, argv_key, env_key in (
        ("test_profiles", "argv", "environment"),
        ("mcp_servers", "command", "environment"),
    ):
        entries = document.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            argv = entry.get(argv_key)
            if isinstance(argv, list):
                for argument in argv:
                    if isinstance(argument, str) and _credential_shape(argument):
                        raise RuntimeConfigError("config_secret_in_generic_field")
            environment = entry.get(env_key)
            if isinstance(environment, list):
                for pair in environment:
                    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                        continue
                    name, value = pair
                    if isinstance(name, str) and _SENSITIVE_KEY.search(name):
                        raise RuntimeConfigError("config_secret_in_generic_field")
                    if isinstance(value, str) and _credential_shape(value):
                        raise RuntimeConfigError("config_secret_in_generic_field")


def _validate_provider_options(
    raw: Mapping[str, Any],
    *,
    strict: bool,
) -> None:
    """Validate the provider_options container.

    Secret-shaped keys are always forbidden (config_secret_in_generic_field).
    In v2 (config_schema_version 2) the positive allowlist and value
    semantics from §6.5 apply; legacy v1 keeps the I1-released general
    JSON-safe map so existing configurations continue to load.
    """
    if not strict:
        return
    for option_key in raw:
        if _SENSITIVE_KEY.search(str(option_key)):
            raise RuntimeConfigError("config_secret_in_generic_field")
        if option_key not in _PROVIDER_OPTION_ALLOWLIST:
            raise RuntimeConfigError("invalid_provider_options")
        value = raw[option_key]
        if option_key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or not -2.0 <= float(value) <= 2.0
            ):
                raise RuntimeConfigError("invalid_provider_options")
        elif option_key == "seed":
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not (-(2**63) <= value <= 2**63 - 1)
            ):
                raise RuntimeConfigError("invalid_provider_options")
        elif option_key == "parallel_tool_calls":
            if not isinstance(value, bool):
                raise RuntimeConfigError("invalid_provider_options")
        elif option_key == "service_tier":
            if (
                not isinstance(value, str)
                or not 1 <= len(value.encode("utf-8")) <= 64
            ):
                raise RuntimeConfigError("invalid_provider_options")
        elif option_key == "stop":
            if (
                not isinstance(value, list)
                or len(value) > 16
                or any(
                    not isinstance(item, str)
                    or not 1 <= len(item.encode("utf-8")) <= 256
                    for item in value
                )
            ):
                raise RuntimeConfigError("invalid_provider_options")
        elif option_key == "response_format":
            if (
                not isinstance(value, dict)
                or set(value) != {"type"}
                or value.get("type") not in ("text", "json_object")
            ):
                raise RuntimeConfigError("invalid_provider_options")


def resolve_api_key(provider: ProviderConfig) -> str:
    """Read the configured provider key from the process environment only."""
    value = os.environ.get(provider.api_key_env)
    if value is None or not value or value != value.strip():
        raise RuntimeConfigError("api_key_missing")
    if "\r" in value or "\n" in value:
        raise RuntimeConfigError("api_key_invalid")
    return value


def _parse_provider(value: Any, *, strict_options: bool = False) -> ProviderConfig:
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
            _validate_provider_options(raw_options, strict=strict_options)
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


def _parse_mcp_servers(
    value: Any, base: Path, *, strict: bool = False,
) -> tuple[McpServerConfig, ...]:
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
            "process_start_timeout_seconds",
            "initialize_timeout_seconds",
            "tools_list_timeout_seconds",
            "tool_call_timeout_seconds",
            "io_poll_timeout_seconds",
            "shutdown_timeout_seconds",
            "max_pending_requests",
            "max_inbound_messages",
            "max_list_pages",
            "max_tools",
            "max_notifications_per_window",
            "max_cursor_bytes",
            "max_frame_bytes",
            "max_stderr_bytes",
            "max_result_bytes",
            "decision",
            "side_effect_class",
            "recovery_mode",
            "execution_profile",
            "image_id",
            "resource_limits",
            "read_only_mounts",
            "code_artifacts",
            "container_working_directory",
            "tool_allowlist",
        }
        _reject_unknown(item, allowed, "invalid_mcp_server")
        # §8.3: v3 JSON must declare an explicit execution_profile for every
        # server; a missing profile cannot silently become host_trusted or
        # sandboxed.  Legacy v1/v2 keeps the pre-I6 behavior (None profile).
        if strict and "execution_profile" not in item:
            raise RuntimeConfigError("mcp_profile_migration_required")
        if strict and "request_timeout_seconds" in item:
            raise RuntimeConfigError("ambiguous_mcp_timeout_config")
        _new_timeouts = {
            "process_start_timeout_seconds",
            "initialize_timeout_seconds",
            "tools_list_timeout_seconds",
            "tool_call_timeout_seconds",
            "io_poll_timeout_seconds",
            "shutdown_timeout_seconds",
        }
        if "request_timeout_seconds" in item and _new_timeouts & set(item):
            raise RuntimeConfigError("ambiguous_mcp_timeout_config")
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
            read_only_mounts = tuple(
                tuple(pair) for pair in item.get("read_only_mounts", ())
            )
            code_artifacts = _mcp_code_artifacts(item.get("code_artifacts"))
            resource_limits = _mcp_resource_limits(item.get("resource_limits"))
            legacy_timeout = item.get("request_timeout_seconds")
            deprecation = legacy_timeout is not None
            tool_call_timeout = item.get(
                "tool_call_timeout_seconds",
                15.0 if legacy_timeout is None else legacy_timeout,
            )
            servers.append(
                McpServerConfig(
                    server_id=item.get("server_id", ""),
                    command=command,
                    cwd=cwd,
                    environment=environment,
                    request_timeout_seconds=(
                        15.0 if legacy_timeout is None else legacy_timeout
                    ),
                    process_start_timeout_seconds=item.get(
                        "process_start_timeout_seconds", 30.0
                    ),
                    initialize_timeout_seconds=item.get(
                        "initialize_timeout_seconds", 30.0
                    ),
                    tools_list_timeout_seconds=item.get(
                        "tools_list_timeout_seconds", 30.0
                    ),
                    tool_call_timeout_seconds=tool_call_timeout,
                    io_poll_timeout_seconds=item.get(
                        "io_poll_timeout_seconds", 0.25
                    ),
                    shutdown_timeout_seconds=item.get(
                        "shutdown_timeout_seconds", 5.0
                    ),
                    max_pending_requests=item.get("max_pending_requests", 64),
                    max_inbound_messages=item.get("max_inbound_messages", 1024),
                    max_list_pages=item.get("max_list_pages", 32),
                    max_tools=item.get("max_tools", 512),
                    max_notifications_per_window=item.get(
                        "max_notifications_per_window", 64
                    ),
                    max_cursor_bytes=item.get("max_cursor_bytes", 4096),
                    max_frame_bytes=item.get("max_frame_bytes", 1_048_576),
                    max_stderr_bytes=item.get("max_stderr_bytes", 262_144),
                    max_result_bytes=item.get("max_result_bytes", 1_048_576),
                    request_timeout_deprecation=deprecation,
                    decision=_decision(item.get("decision", "ask")),
                    side_effect_class=item.get("side_effect_class", "read_only"),
                    recovery_mode=item.get("recovery_mode", "retry"),
                    execution_profile=_mcp_profile(item.get("execution_profile")),
                    image_id=item.get("image_id"),
                    resource_limits=resource_limits,
                    read_only_mounts=read_only_mounts,
                    code_artifacts=code_artifacts,
                    container_working_directory=item.get(
                        "container_working_directory"
                    ),
                    tool_allowlist=(
                        None
                        if item.get("tool_allowlist") is None
                        else tuple(item.get("tool_allowlist"))
                    ),
                )
            )
        except RuntimeConfigError:
            raise
        except (TypeError, ValueError):
            raise RuntimeConfigError("invalid_mcp_server") from None
    return tuple(servers)


def _mcp_profile(value: Any) -> McpExecutionProfile | None:
    """Parse one explicit execution_profile; None stays legacy (unset)."""
    if value is None:
        return None
    if isinstance(value, McpExecutionProfile):
        return value
    if not isinstance(value, str):
        raise RuntimeConfigError("invalid_mcp_server")
    try:
        return McpExecutionProfile(value)
    except ValueError:
        raise RuntimeConfigError("invalid_mcp_server") from None


def _mcp_resource_limits(value: Any) -> McpResourceLimits | None:
    if value is None:
        return None
    if isinstance(value, McpResourceLimits):
        return value
    if not isinstance(value, dict):
        raise RuntimeConfigError("invalid_mcp_resource_limits")
    allowed = {"cpus", "memory_bytes", "pids", "tmpfs_bytes", "process_count"}
    _reject_unknown(value, allowed, "invalid_mcp_resource_limits")
    try:
        return McpResourceLimits(
            cpus=value.get("cpus", 1.0),
            memory_bytes=value.get("memory_bytes", 512 * 1024 * 1024),
            pids=value.get("pids", 256),
            tmpfs_bytes=value.get("tmpfs_bytes", 64 * 1024 * 1024),
            process_count=value.get("process_count", 1),
        )
    except RuntimeConfigError:
        raise
    except (TypeError, ValueError):
        raise RuntimeConfigError("invalid_mcp_resource_limits") from None


def _mcp_code_artifacts(value: Any) -> tuple[McpCodeArtifact, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimeConfigError("invalid_mcp_server")
    artifacts: list[McpCodeArtifact] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise RuntimeConfigError("invalid_mcp_server")
        allowed = {"role", "argv_index"}
        _reject_unknown(entry, allowed, "invalid_mcp_server")
        artifacts.append(
            McpCodeArtifact(
                entry.get("role", ""),
                entry.get("argv_index", -1),
            )
        )
    return tuple(artifacts)


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
