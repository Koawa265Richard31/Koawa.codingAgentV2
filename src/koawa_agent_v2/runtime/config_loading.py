"""运行时配置的严格 JSON 读取与解析。

本模块承载 ``load_runtime_config``、``_parse_*`` 家族、错误码映射、路径
解析(``_absolute_path``)与文件级常量(``CONFIG_MAX_BYTES``、
``CONFIG_SCHEMA_VERSION``、``DEFAULT_SYSTEM_PROMPT``)。配置类型与构造期
校验在 ``config_models``;环境凭据解析在 ``config_credentials``。本模块
单向依赖 ``config_models``,不得依赖 config_credentials 或兼容入口 config。
"""

from __future__ import annotations

import math
import os
import warnings
from pathlib import Path
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
from ..policy import Decision
from ..recovery.redaction import _ASSIGNMENT, _BEARER, _OPENAI_KEY, _SENSITIVE_KEY
from .config_models import (
    McpCodeArtifact,
    McpExecutionProfile,
    McpResourceLimits,
    McpServerConfig,
    PolicyConfig,
    ProviderConfig,
    RepositoryTrustMode,
    RuntimeConfig,
    RuntimeConfigError,
    SandboxConfig,
    SandboxRunner,
    TestProfileConfig,
<<<<<<< main
    _DEFAULT_PROTECTED_PATHS,
=======
>>>>>>> feat/merge-authorization
    _PROFILE_ID,
)
from .memory import MemoryConfig, MemoryConfigError


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


DEFAULT_SYSTEM_PROMPT = """You are KoawaAgent V2, a local durable coding agent.
Always reply to the user in Chinese.
Before every tool call, write one short Chinese progress line explaining what you are about to do and why.
After a tool result arrives, write one short Chinese progress line with the outcome and your next step.
Do not wait until the final answer to report progress.

Work inside the provided Git repository only.

Protocol:
1. Inspect with read_file, list_files, or search_text before editing.
2. Edit with apply_patch. Its patch_json is strict JSON: {"schema_version":1,"changes":[...]} where each change is exactly ADD {"operation":"add","path":...,"content":...,"newline":"lf","utf8_bom":false}, UPDATE {"operation":"update","path":...,"base_sha256":"<sha256 from read_file>","hunks":[{"old_start":<1-based line>,"old_lines":["<exact existing line, copied>"],"new_lines":["<replacement>"]}]}, or DELETE {"operation":"delete","path":...,"base_sha256":"<sha256 from read_file>"}. UPDATE/DELETE require the exact base_sha256 returned by read_file; old_lines must repeat existing lines character-for-character; never add extra fields.
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
        "required_test_profiles",
        "policy",
        "mcp_servers",
        "system_prompt",
        "owner_id",
        "lease_seconds",
        "model_rounds",
        "max_tool_calls",
        "verification_max_test_runs",
        "history_max_turns",
        "history_max_chars",
        "compact_min_turns",
        "budget_action_limits",
        "fallback_summary_model",
        "durable_limits",
        "memory",
        "canary_key_env",
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
    required_test_profiles = (
        _parse_required_test_profiles(document["required_test_profiles"])
        if "required_test_profiles" in document
        else None
    )
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
    raw_verification_runs = document.get("verification_max_test_runs")
    if raw_verification_runs is not None and (
        not isinstance(raw_verification_runs, int)
        or isinstance(raw_verification_runs, bool)
        or not 1 <= raw_verification_runs <= 64
    ):
        raise RuntimeConfigError("invalid_verification_max_test_runs")
    history_max_turns = document.get("history_max_turns", 16)
    history_max_chars = document.get("history_max_chars", 32_000)
    compact_min_turns = document.get("compact_min_turns", 4)
    fallback_summary_model = document.get("fallback_summary_model")
    canary_key_env = document.get("canary_key_env")
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
        required_test_profiles=required_test_profiles,
        policy=policy,
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        owner_id=owner_id,
        lease_seconds=lease_seconds,
        model_rounds=model_rounds,
        max_tool_calls=max_tool_calls,
        verification_max_test_runs=raw_verification_runs,
        history_max_turns=history_max_turns,
        history_max_chars=history_max_chars,
        compact_min_turns=compact_min_turns,
        memory=memory,
        budget_action_limits=budget_action_limits,
        fallback_summary_model=fallback_summary_model,
        canary_key_env=canary_key_env,
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
<<<<<<< main
    policy = document.get("policy")
    if isinstance(policy, dict):
        for key in ("dependency_allowed_packages", "dependency_index_urls"):
            values = policy.get(key)
            if isinstance(values, list) and any(
                isinstance(value, str) and _credential_shape(value)
                for value in values
            ):
                raise RuntimeConfigError("config_secret_in_generic_field")
=======
>>>>>>> feat/merge-authorization
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
<<<<<<< main
    allowed = {
        "runner", "image_id", "docker_executable", "host_trust",
        "allow_unisolated_execution",
    }
=======
    allowed = {"runner", "image_id", "docker_executable", "host_trust"}
>>>>>>> feat/merge-authorization
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
<<<<<<< main
        allow_unisolated = value.get("allow_unisolated_execution", False)
        if not isinstance(allow_unisolated, bool):
            raise RuntimeConfigError("invalid_sandbox_config")
        if runner is SandboxRunner.HOST and not allow_unisolated:
            raise RuntimeConfigError("unisolated_execution_forbidden")
=======
>>>>>>> feat/merge-authorization
        return SandboxConfig(
            runner=runner,
            image_id=value.get("image_id"),
            docker_executable=value.get("docker_executable", "docker"),
            host_trust=host_trust,
<<<<<<< main
            allow_unisolated_execution=allow_unisolated,
=======
>>>>>>> feat/merge-authorization
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
            "release_fields",
            "sensitive",
            "workspace_manifest",
            "profile_version",
            "audience_policy_ref",
            "input_policy_ref",
            "candidate_admission_ref",
            "environment_ref",
            "release_mode",
            "producer_rules",
            "scratch_policy_ref",
            "budget_policy_ref",
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
                    release_fields=(
                        tuple(item["release_fields"])
                        if item.get("release_fields") is not None
                        else None
                    ),
                    sensitive=bool(item.get("sensitive", False)),
                    workspace_manifest=tuple(
                        item.get("workspace_manifest", ()),
                    ),
                    profile_version=str(item.get("profile_version", "")),
                    audience_policy_ref=item.get("audience_policy_ref"),
                    input_policy_ref=item.get("input_policy_ref"),
                    candidate_admission_ref=item.get("candidate_admission_ref"),
                    environment_ref=item.get("environment_ref"),
                    release_mode=item.get("release_mode"),
                    producer_rules=tuple(item.get("producer_rules", ())),
                    scratch_policy_ref=item.get("scratch_policy_ref"),
                    budget_policy_ref=item.get("budget_policy_ref"),
                )
            )
        except RuntimeConfigError:
            raise
        except (TypeError, ValueError):
            raise RuntimeConfigError("invalid_test_profile") from None
    return tuple(profiles)


def _parse_required_test_profiles(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise RuntimeConfigError("invalid_required_test_profiles")
    if any(
        not isinstance(profile_id, str)
        or not _PROFILE_ID.fullmatch(profile_id)
        for profile_id in value
    ):
        raise RuntimeConfigError("invalid_required_test_profiles")
    if len(set(value)) != len(value):
        raise RuntimeConfigError("duplicate_required_test_profile")
    return tuple(value)


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
        "protected_paths",
<<<<<<< main
        "dependency_allowed_packages",
        "dependency_allow_install",
        "dependency_max_packages",
        "dependency_index_urls",
    }
    _reject_unknown(value, allowed, "invalid_policy_config")
    for key in ("protected_paths", "dependency_allowed_packages", "dependency_index_urls"):
        if key in value and not isinstance(value[key], list):
            raise RuntimeConfigError("invalid_policy_config")
=======
    }
    _reject_unknown(value, allowed, "invalid_policy_config")
>>>>>>> feat/merge-authorization
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
<<<<<<< main
            protected_paths=tuple(
                value.get("protected_paths", _DEFAULT_PROTECTED_PATHS)
            ),
            dependency_allowed_packages=tuple(value.get("dependency_allowed_packages", ())),
            dependency_allow_install=value.get("dependency_allow_install", False),
            dependency_max_packages=value.get("dependency_max_packages", 8),
            dependency_index_urls=tuple(value.get("dependency_index_urls", ())),
=======
            protected_paths=tuple(value.get("protected_paths", ())),
>>>>>>> feat/merge-authorization
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
