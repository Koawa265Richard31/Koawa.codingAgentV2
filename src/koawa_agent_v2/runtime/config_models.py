"""运行时配置的配置类型与构造期校验。

本模块承载原 ``runtime.config`` 的全部配置类型(dataclass、StrEnum)、稳定
错误类 ``RuntimeConfigError``、共享校验函数(``_non_empty_text`` 等)与
类型校验所需的正则。严格 JSON 读取与 ``_parse_*`` 家族在 ``config_loading``;
环境凭据解析在 ``config_credentials``;``config.py`` 只作兼容再导出。
依赖方向固定::

    config_credentials -> config_models
    config_loading -> config_models
    config -> {config_models, config_loading, config_credentials}

本模块不得反向依赖 config_loading、config_credentials 或 config。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ..control.durable_json import validate_runtime_ingress
from ..model.openai_client import ReasoningEffort, reasoning_family
from ..policy import Decision
from .memory import MemoryConfig


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
    # R3/contracts §2: per-profile release contract (deployment input).
    release_fields: tuple[str, ...] | None = None
    sensitive: bool = False
    profile_version: str = ""
    audience_policy_ref: str | None = None
    input_policy_ref: str | None = None
    candidate_admission_ref: str | None = None
    environment_ref: str | None = None
    release_mode: str | None = None
    producer_rules: tuple[str, ...] = ()
    scratch_policy_ref: str | None = None
    budget_policy_ref: str | None = None
    # WP-C v1: fixed input manifest - non-empty runs in an isolated
    # ephemeral workspace (read-only candidate copies + private scratch).
    workspace_manifest: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _PROFILE_ID.fullmatch(self.profile_id):
            raise RuntimeConfigError("invalid_test_profile")
        from ..verification.output_policy import ReleaseRule

        try:
            ReleaseRule(fields=self.release_fields, sensitive=self.sensitive)
        except (ValueError, TypeError):
            raise RuntimeConfigError("invalid_release_contract") from None
        if self.sensitive and not self.candidate_admission_ref:
            # §2: without an admission reference, sensitive validation stays
            # closed (capability refusal, not a hard config error at parse).
            pass
        import os as _os
        from pathlib import Path as _Path

        for item in self.workspace_manifest:
            if (
                not isinstance(item, str)
                or not item
                or _os.sep in item
                or item.startswith("/")
                or ".." in _Path(item.replace("/", _os.sep)).parts
            ):
                raise RuntimeConfigError("invalid_workspace_manifest")
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
    # Policy gate: paths the patch tool must never modify.  Patterns are
    # exact paths, directory prefixes ending in "/", or basename prefixes
    # (e.g. "test_").  Removing the opportunity is stronger than auditing
    # a diff afterwards: the edit is refused at the tool boundary.
    protected_paths: tuple[str, ...] = ()

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
        if not isinstance(self.protected_paths, tuple):
            raise RuntimeConfigError("invalid_protected_paths")
        for pattern in self.protected_paths:
            if (
                not isinstance(pattern, str)
                or not pattern
                or pattern.startswith("/")
                or "\\" in pattern
                or ".." in pattern.split("/")
                or len(pattern) > 256
            ):
                raise RuntimeConfigError("invalid_protected_paths")
        object.__setattr__(
            self,
            "protected_paths",
            tuple(dict.fromkeys(self.protected_paths)),
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
    required_test_profiles: tuple[str, ...] | None = None
    mcp_servers: tuple[McpServerConfig, ...] = ()
    owner_id: str = "runtime-cli"
    lease_seconds: int = 30
    model_rounds: int = 32
    max_tool_calls: int = 128
    # Verification budget: how many test-profile runs one turn may reserve.
    # None = derived from the required-profile count (min(32, max(4, 4n))).
    # Hard real-world issues need more repair cycles than the derivation
    # grants; the knob stays explicit rather than silently raising limits.
    verification_max_test_runs: int | None = None
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
    # RT/J J2 / PSEC semantics-C: optional canary gate activation.  The value
    # is the NAME of an environment variable holding the HMAC canary key —
    # never the key itself.  None (default) = gate inactive (executor skips
    # J2 checks).  A configured-but-missing variable is a hard error at
    # resolve time (fail closed): a typo must not silently disable security.
    canary_key_env: str | None = None
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
        if self.canary_key_env is not None:
            if not isinstance(self.canary_key_env, str) or not _ENV_NAME.fullmatch(
                self.canary_key_env
            ):
                raise RuntimeConfigError("invalid_canary_key_env")
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
        if (
            self.sandbox.runner is SandboxRunner.HOST
            and any(value.sensitive for value in self.test_profiles)
        ):
            # WP-C: sensitive profiles require the sandboxed runner; the
            # host runner is never a silent fallback.
            raise RuntimeConfigError("sensitive_profile_requires_sandbox")
        if any(not isinstance(value, TestProfileConfig) for value in self.test_profiles):
            raise RuntimeConfigError("invalid_test_profile")
        if len({value.profile_id for value in self.test_profiles}) != len(
            self.test_profiles
        ):
            raise RuntimeConfigError("duplicate_test_profile")
        registered_profile_ids = tuple(
            value.profile_id for value in self.test_profiles
        )
        required_profiles = self.required_test_profiles
        if required_profiles is None:
            if len(registered_profile_ids) != 1:
                raise RuntimeConfigError("required_test_profiles_required")
            required_profiles = registered_profile_ids
        elif not isinstance(required_profiles, tuple):
            raise RuntimeConfigError("invalid_required_test_profiles")
        if not required_profiles or len(required_profiles) > 64:
            raise RuntimeConfigError("invalid_required_test_profiles")
        if any(
            not isinstance(profile_id, str)
            or not _PROFILE_ID.fullmatch(profile_id)
            for profile_id in required_profiles
        ):
            raise RuntimeConfigError("invalid_required_test_profiles")
        if len(set(required_profiles)) != len(required_profiles):
            raise RuntimeConfigError("duplicate_required_test_profile")
        if any(
            profile_id not in registered_profile_ids
            for profile_id in required_profiles
        ):
            raise RuntimeConfigError("required_test_profile_not_found")
        object.__setattr__(
            self, "required_test_profiles", tuple(sorted(required_profiles))
        )
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
