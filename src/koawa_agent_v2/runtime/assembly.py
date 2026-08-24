"""P0 runtime assembly: one real model/tool/policy/ledger/recovery chain.

``assemble_runtime`` is the only supported way to combine the production slices
for a real provider.  It intentionally builds the full verified coding registry
(D3+D4+D5), binds it behind the D9 policy gate and D7 ledger, and runs it through
the D2 loop and D6 worker.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from ..approval_service import ApprovalService
from ..control.runtime import ThreadRuntime
from ..control.sqlite_store import SqliteEventStore
from ..execution.loop import (
    AgentLoop,
    AgentLoopLimits,
    ToolExecutionContext,
    ToolExecutionResult,
)
from ..execution.worker import TurnWorker
from ..ledger import (
    IDEMPOTENT_WRITE_PROFILE,
    MANUAL_WRITE_PROFILE,
    QUERYABLE_WRITE_PROFILE,
    READ_ONLY_PROFILE,
    LedgerExecutor,
    ToolLedgerStore,
)
from ..mcp import McpSession, StdioTransport
from ..mcp.tool_binding import McpBinding, McpCatalog, build_mcp_registry
from ..model.openai_client import OpenAICompatibleChatClient
from ..model.protocol import InstructionMessage, InstructionRole, ModelContextItem
from ..model.stream import StreamLimits
from ..policy import (
    ActionKind,
    Decision,
    PolicyEngine,
    PolicyRule,
    Principal,
    ResolvedAction,
    SideEffectClass,
    canonical_arguments,
)
from ..recovery import CheckpointStore
from ..sandbox.protocol import SandboxCommandProfile, SandboxError
from ..sandbox.runtime import DockerCommandRunner
from ..telemetry.trace import TraceStore
from ..tools.registry import ToolRegistry
from ..verification.runner import (
    CommandProfile,
    CommandRunnerError,
    RepositoryTrust,
    TrustedCommandRunner,
)
from ..verification.git import GitFacadeError
from ..verification.tools import build_verified_coding_tool_registry
from .composite_registry import CompositeToolRegistry
from .config import (
    McpServerConfig,
    ProviderConfig,
    RepositoryTrustMode,
    RuntimeConfig,
    RuntimeConfigError,
    SandboxRunner,
    TestProfileConfig,
    resolve_api_key,
)

_logger = logging.getLogger(__name__)

_ASSEMBLY_ERROR = re.compile(r"[a-z][a-z0-9_]{0,127}")

READ_TOOL_NAMES = (
    "read_file",
    "list_files",
    "search_text",
    "git_status",
    "git_diff",
    "finalize_task",
)
WRITE_TOOL_NAMES = ("apply_patch",)
TEST_TOOL_NAMES = ("run_test_profile",)


class RuntimeAssemblyError(RuntimeError):
    """Stable, content-free assembly failure for operator-facing CLI output."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _ASSEMBLY_ERROR.fullmatch(code):
            raise ValueError("invalid assembly error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class AssembledRuntime:
    config: RuntimeConfig
    store: SqliteEventStore
    runtime: ThreadRuntime
    ledger: ToolLedgerStore
    approvals: ApprovalService
    trace: TraceStore
    registry: object
    executor: LedgerExecutor
    client: object
    worker: TurnWorker
    checkpoint_store: CheckpointStore
    correlation_id: object
    loop: AgentLoop
    mcp_sessions: tuple[tuple[McpServerConfig, McpSession, McpCatalog], ...] = ()
    # Idempotent teardown marker (frozen dataclass: mutated via object.__setattr__).
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def __repr__(self) -> str:
        return (
            f"AssembledRuntime(repo={str(self.config.repo)!r}, "
            f"db={str(self.config.db)!r}, provider_model={self.config.provider.model!r})"
        )

    def build_worker(
        self,
        initial_context: Sequence[ModelContextItem] = (),
        *,
        task_mode: bool = True,
        claim_gate: bool = False,
    ) -> TurnWorker:
        """Build a TurnWorker over the same loop, optionally seeded with history.

        task_mode=False drops the D5 completion gate: conversational turns may
        stop without a finalize_task evidence trail (verification_required
        would otherwise reject any chat reply).
        """
        loop = self.loop
        if not task_mode:
            loop = AgentLoop(
                self.client,
                tool_executor=self.executor,
                completion_gate=None,
                claim_gate=claim_gate,
                limits=AgentLoopLimits(
                    max_model_rounds=self.config.model_rounds,
                    max_tool_calls=self.config.max_tool_calls,
                ),
                stream_limits=StreamLimits(),
                trace_store=self.trace,
                correlation_id=self.correlation_id,
            )
        return TurnWorker(
            self.runtime,
            loop,
            provider=self.config.provider.provider,
            model=self.config.provider.model,
            instructions=(
                InstructionMessage(InstructionRole.SYSTEM, self.config.system_prompt),
            ),
            initial_context=tuple(initial_context),
            max_output_tokens=self.config.provider.max_output_tokens,
            checkpoint_store=self.checkpoint_store,
            owner_id=self.config.owner_id,
            lease_seconds=self.config.lease_seconds,
        )

    def close(self) -> None:
        """Idempotent teardown in reverse assembly order (doc 3.6).

        Closes every successfully created MCP session/transport in reverse
        assembly order. Re-calling close() is a no-op; close failures are
        logged and never escape, so teardown always converges to a fully
        closed runtime.
        """
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        _close_mcp_sessions([item[1] for item in self.mcp_sessions])

    def __enter__(self) -> "AssembledRuntime":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _close_mcp_sessions(sessions: Sequence[object]) -> None:
    """Close sessions in reverse assembly order; never raises."""
    for session in reversed(tuple(sessions)):
        try:
            session.close()
        except Exception as exc:
            _logger.warning("mcp session close failed: %r", exc)


def assemble_runtime(
    config: RuntimeConfig,
    *,
    model_client: object | None = None,
    api_key: str | None = None,
    reasoning_sink: Callable[[str], None] | None = None,
) -> AssembledRuntime:
    """Build the complete runtime.

    ``model_client`` and ``api_key`` are injection points for deterministic tests;
    production callers leave both unset and let the config resolve the key from the
    process environment.
    """
    if not isinstance(config, RuntimeConfig):
        raise TypeError("config must be RuntimeConfig")
    if not config.repo.is_dir():
        raise RuntimeAssemblyError("repo_not_found")
    try:
        store = SqliteEventStore(config.db)
        runtime = ThreadRuntime(store, actor="p0-runtime")
        ledger = ToolLedgerStore(store)
        approvals = ApprovalService(
            store,
            ledger,
            budget_action_limits=dict(config.budget_action_limits),
        )
        trace = TraceStore(store)
        correlation_id = uuid4()

        runner = _build_command_runner(config, store)
        builtin_registry = build_verified_coding_tool_registry(
            config.repo,
            command_runner=runner,
        )
        mcp_sessions, mcp_bindings = _connect_mcp_servers(
            config, trace, correlation_id
        )
        registry = (
            builtin_registry
            if not mcp_sessions
            else CompositeToolRegistry(
                (
                    builtin_registry,
                    *(item[1] for item in mcp_bindings.values()),
                )
            )
        )
        executor = _bind_ledger_policy(
            config,
            registry,
            ledger,
            approvals,
            trace,
            correlation_id,
            mcp_bindings,
        )
        if model_client is None:
            key = api_key if api_key is not None else resolve_api_key(config.provider)
            client = _build_openai_client(
                config.provider,
                key,
                reasoning_sink=reasoning_sink,
            )
        else:
            if not callable(getattr(model_client, "stream", None)):
                raise RuntimeAssemblyError("invalid_model_client")
            client = model_client

        instructions = (
            InstructionMessage(
                InstructionRole.SYSTEM,
                config.system_prompt,
            ),
        )
        loop = AgentLoop(
            client,
            tool_executor=executor,
            completion_gate=registry,
            limits=AgentLoopLimits(
                max_model_rounds=config.model_rounds,
                max_tool_calls=config.max_tool_calls,
            ),
            stream_limits=StreamLimits(),
            trace_store=trace,
            correlation_id=correlation_id,
        )
        checkpoint_store = CheckpointStore(store)
        worker = TurnWorker(
            runtime,
            loop,
            provider=config.provider.provider,
            model=config.provider.model,
            instructions=instructions,
            max_output_tokens=config.provider.max_output_tokens,
            checkpoint_store=checkpoint_store,
            owner_id=config.owner_id,
            lease_seconds=config.lease_seconds,
        )
        return AssembledRuntime(
            config=config,
            store=store,
            runtime=runtime,
            ledger=ledger,
            approvals=approvals,
            trace=trace,
            registry=registry,
            executor=executor,
            client=client,
            worker=worker,
            checkpoint_store=checkpoint_store,
            correlation_id=correlation_id,
            loop=loop,
            mcp_sessions=mcp_sessions,
        )
    except (RuntimeConfigError, RuntimeAssemblyError):
        raise
    except CommandRunnerError as exc:
        raise RuntimeAssemblyError(getattr(exc, "code", "command_runner_failed")) from None
    except SandboxError as exc:
        raise RuntimeAssemblyError(getattr(exc, "code", "sandbox_failed")) from None
    except GitFacadeError as exc:
        raise RuntimeAssemblyError(getattr(exc, "code", "git_facade_failed")) from None
    except Exception as exc:
        raise RuntimeAssemblyError("runtime_assembly_failed") from None


def _build_command_runner(config: RuntimeConfig, store: SqliteEventStore):
    profiles = config.test_profiles
    if config.sandbox.runner is SandboxRunner.DOCKER:
        if config.sandbox.image_id is None:
            raise RuntimeAssemblyError("docker_image_id_required")
        return DockerCommandRunner(
            config.repo,
            [_docker_profile(item) for item in profiles],
            store,
            config.sandbox.image_id,
            docker_executable=config.sandbox.docker_executable,
        )
    trust = {
        RepositoryTrustMode.UNTRUSTED: RepositoryTrust.UNTRUSTED,
        RepositoryTrustMode.BUILTIN_FIXTURE: RepositoryTrust.BUILTIN_FIXTURE,
        RepositoryTrustMode.USER_CONFIRMED: RepositoryTrust.USER_CONFIRMED,
    }[config.sandbox.host_trust]
    return TrustedCommandRunner(
        config.repo,
        [_host_profile(item) for item in profiles],
        trust=trust,
    )


def _docker_profile(item: TestProfileConfig) -> SandboxCommandProfile:
    return SandboxCommandProfile(
        item.profile_id,
        item.argv,
        timeout_seconds=item.timeout_seconds,
        max_stdout_bytes=item.max_stdout_bytes,
        max_stderr_bytes=item.max_stderr_bytes,
        environment=item.environment,
    )


def _host_profile(item: TestProfileConfig) -> CommandProfile:
    argv = list(item.argv)
    if not Path(argv[0]).is_absolute():
        resolved = shutil.which(argv[0])
        if resolved is None:
            raise RuntimeAssemblyError("command_executable_not_found")
        argv[0] = resolved
    return CommandProfile(
        item.profile_id,
        tuple(argv),
        timeout_seconds=item.timeout_seconds,
        max_stdout_bytes=item.max_stdout_bytes,
        max_stderr_bytes=item.max_stderr_bytes,
        environment=item.environment,
    )


def _connect_mcp_servers(
    config: RuntimeConfig,
    trace: TraceStore,
    correlation_id: object,
):
    sessions: list[tuple[McpServerConfig, object, McpCatalog]] = []
    bindings: dict[str, tuple[McpServerConfig, object, McpCatalog]] = {}
    opened: list[object] = []
    try:
        for server_config in config.mcp_servers:
            transport = StdioTransport(
                server_config.command,
                env=dict(server_config.environment),
                cwd=(
                    None
                    if server_config.cwd is None
                    else str(server_config.cwd)
                ),
            )
            session = McpSession(
                server_config.server_id,
                transport,
                request_timeout=server_config.request_timeout_seconds,
                trace_store=trace,
                correlation_id=correlation_id,
            )
            catalog = session.connect()
            adapter = build_mcp_registry(session, catalog)
            opened.append(session)
            # Retain the McpSession (not the adapter) so the ownership chain
            # can close every real transport/session in reverse order.
            sessions.append((server_config, session, catalog))
            bindings[server_config.server_id] = (
                server_config,
                adapter,
                catalog,
            )
        return tuple(sessions), bindings
    except BaseException:
        # Mid-assembly failure: close every already-started session through
        # the same idempotent teardown helper that AssembledRuntime.close()
        # uses (reverse order, never raises).
        _close_mcp_sessions(opened)
        raise


def _mcp_policy_side_effect(config: McpServerConfig) -> SideEffectClass:
    return {
        "read_only": SideEffectClass.READ_ONLY,
        "idempotent_write": SideEffectClass.IDEMPOTENT_WRITE,
        "non_idempotent_write": SideEffectClass.NON_IDEMPOTENT_WRITE,
    }[config.side_effect_class]


def _mcp_recovery_profile(config: McpServerConfig):
    from ..ledger.protocol import (
        RecoveryMode,
        SideEffectClass as LedgerSideEffectClass,
        ToolRecoveryProfile,
    )

    side_effect = {
        "read_only": LedgerSideEffectClass.READ_ONLY,
        "idempotent_write": LedgerSideEffectClass.IDEMPOTENT_WRITE,
        "non_idempotent_write": LedgerSideEffectClass.NON_IDEMPOTENT_WRITE,
    }[config.side_effect_class]
    recovery_mode = {
        "retry": RecoveryMode.RETRY,
        "authoritative_query": RecoveryMode.AUTHORITATIVE_QUERY,
        "manual": RecoveryMode.MANUAL,
    }[config.recovery_mode]
    return ToolRecoveryProfile(side_effect, recovery_mode)


def _bind_ledger_policy(
    config: RuntimeConfig,
    registry,
    ledger: ToolLedgerStore,
    approvals: ApprovalService,
    trace: TraceStore,
    correlation_id: object,
    mcp_bindings: Mapping[str, tuple[McpServerConfig, object, McpCatalog]] | None = None,
) -> LedgerExecutor:
    definitions = registry.definitions()
    names = tuple(item.name for item in definitions)
    principal = Principal("root", config.policy.principal_scopes)
    mcp_by_tool: dict[str, tuple[McpServerConfig, object, McpBinding]] = {}
    mcp_rules: list[PolicyRule] = []
    for server_id, (server_config, _adapter, catalog) in (mcp_bindings or {}).items():
        tool_names = tuple(
            sorted(binding.registry_name for binding in catalog.bindings.values())
        )
        for binding in catalog.bindings.values():
            mcp_by_tool[binding.registry_name] = (
                server_config,
                _adapter,
                binding,
            )
        mcp_rules.append(
            PolicyRule(
                f"mcp-{server_id}-allow-or-ask",
                server_config.decision,
                action_kinds=(ActionKind.MCP_TOOL,),
                tool_names=tool_names,
                principal_ids=("root",),
                required_scopes=("mcp.use",),
            )
        )
    engine = PolicyEngine(
        config.policy.policy_version,
        (
            PolicyRule(
                "builtin-read-allow-or-ask",
                config.policy.read_decision,
                action_kinds=(ActionKind.BUILTIN_TOOL,),
                tool_names=READ_TOOL_NAMES,
                principal_ids=("root",),
                required_scopes=("workspace.read",),
                side_effect_classes=(SideEffectClass.READ_ONLY,),
            ),
            PolicyRule(
                "builtin-patch-allow-or-ask",
                config.policy.patch_decision,
                action_kinds=(ActionKind.BUILTIN_TOOL,),
                tool_names=WRITE_TOOL_NAMES,
                principal_ids=("root",),
                required_scopes=("workspace.write",),
                side_effect_classes=(SideEffectClass.IDEMPOTENT_WRITE,),
            ),
            PolicyRule(
                "builtin-test-allow-or-ask",
                config.policy.test_decision,
                action_kinds=(ActionKind.BUILTIN_TOOL,),
                tool_names=TEST_TOOL_NAMES,
                principal_ids=("root",),
                required_scopes=("sandbox.test",),
                side_effect_classes=(SideEffectClass.READ_ONLY,),
            ),
            *mcp_rules,
        ),
        network_enabled=False,
        proxy_required=True,
    )

    def resolve_tool(
        call,
        context: ToolExecutionContext,
        profile,
        previous: ResolvedAction | None,
    ) -> ResolvedAction:
        del context, profile, previous
        mcp_entry = mcp_by_tool.get(call.name)
        if mcp_entry is not None:
            server_config, _adapter, binding = mcp_entry
            return ResolvedAction(
                kind=ActionKind.MCP_TOOL,
                tool_name=call.name,
                canonical_arguments_json=canonical_arguments(call.arguments_json),
                principal=principal,
                side_effect_class=_mcp_policy_side_effect(server_config),
                sandbox_profile_id=f"mcp-{server_config.server_id}",
                policy_version=config.policy.policy_version,
                mcp_server_id=server_config.server_id,
                mcp_session_generation=binding.session_generation,
                mcp_schema_hash=binding.schema_hash,
            )
        if call.name == "apply_patch":
            side_effect = SideEffectClass.IDEMPOTENT_WRITE
            sandbox_profile_id = "builtin-patch"
        elif call.name == "run_test_profile":
            side_effect = SideEffectClass.READ_ONLY
            sandbox_profile_id = "sandbox-test"
        else:
            side_effect = SideEffectClass.READ_ONLY
            sandbox_profile_id = "builtin-read"
        return ResolvedAction(
            kind=ActionKind.BUILTIN_TOOL,
            tool_name=call.name,
            canonical_arguments_json=canonical_arguments(call.arguments_json),
            principal=principal,
            side_effect_class=side_effect,
            sandbox_profile_id=sandbox_profile_id,
            policy_version=config.policy.policy_version,
        )

    profiles: dict[str, object] = {}
    for name in names:
        if name == "apply_patch":
            profiles[name] = IDEMPOTENT_WRITE_PROFILE
        elif name in mcp_by_tool:
            profiles[name] = _mcp_recovery_profile(mcp_by_tool[name][0])
        else:
            profiles[name] = READ_ONLY_PROFILE
    return LedgerExecutor(
        registry,
        ledger,
        profiles,
        policy_engine=engine,
        approval_service=approvals,
        action_resolvers={name: resolve_tool for name in names},
        trace_store=trace,
        correlation_id=correlation_id,
    )


def _build_openai_client(
    provider: ProviderConfig,
    api_key: str | None,
    *,
    reasoning_sink: Callable[[str], None] | None = None,
):
    return OpenAICompatibleChatClient(
        provider.base_url,
        api_key,
        provider=provider.provider,
        timeout_seconds=provider.timeout_seconds,
        max_stream_seconds=provider.max_stream_seconds,
        max_request_bytes=provider.max_request_bytes,
        max_response_bytes=provider.max_response_bytes,
        max_sse_event_bytes=provider.max_sse_event_bytes,
        provider_options=dict(provider.provider_options),
        reasoning_effort=provider.reasoning_effort,
        reasoning_sink=reasoning_sink,
    )
