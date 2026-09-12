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
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from ..approval_service import ApprovalService
from ..control.durable_json import CanonicalTextPolicy
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
from ..mcp.activation import (
    ActivationService,
    ActivationView,
    AuthorizedLaunchTicket,
    McpActivationError,
    StagedLaunchPlan,
    process_start_scope,
    stage_code_artifacts,
)
from ..mcp.launcher import (
    HostTrustedLauncher,
    McpProcessLauncher,
    SandboxedLauncher,
)
from ..mcp.tool_binding import McpBinding, McpCatalog, build_mcp_registry
from ..mcp.transport import TransportError
from ..model.openai_client import OpenAICompatibleChatClient
from ..security import SecurityGate
from .config import resolve_canary_key
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
from ..telemetry.trace import BestEffortTraceSink, TraceSink, TraceStore
from ..tools.registry import ToolRegistry
from ..verification.runner import (
    CommandProfile,
    CommandRunnerError,
    RepositoryTrust,
    TrustedCommandRunner,
)
from ..verification.git import GitFacade, GitFacadeError
from ..verification.finalization import VerificationLimits
from ..verification.tools import build_verified_coding_tool_registry
from ..plan import PlanBoard
from .composite_registry import CompositeToolRegistry
from .config import (
    McpExecutionProfile,
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
    trace_sink: TraceSink
    registry: object
    executor: LedgerExecutor
    client: object
    worker: TurnWorker
    checkpoint_store: CheckpointStore
    correlation_id: object
    loop: AgentLoop
    mcp_sessions: tuple[tuple[McpServerConfig, McpSession, McpCatalog], ...] = ()
    # D24 W1: thread-lifetime plan board shared by the tool registry and the
    # session projection; durable journal binding happens per session.
    plan_board: object | None = None
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
                trace_sink=self.trace_sink,
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


@dataclass(frozen=True, slots=True, repr=False)
class ControlPlaneRuntime:
    """I6 §8.9: control-plane resources only; never spawns MCP processes.

    status / doctor / approvals / approve / deny / cancel run entirely from
    this object; the execution plane (model client + MCP processes) is
    assembled lazily on the first real run/resume/chat.
    """

    config: RuntimeConfig
    config_base_dir: Path
    store: SqliteEventStore
    runtime: ThreadRuntime
    ledger: ToolLedgerStore
    approvals: ApprovalService
    trace: TraceStore
    trace_sink: TraceSink
    checkpoint_store: CheckpointStore
    correlation_id: object
    activation: ActivationService
    staging_root: Path
    git: GitFacade
    _execution: object | None = field(
        default=None, init=False, repr=False, compare=False,
    )
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def attach_execution(self, execution: object) -> None:
        object.__setattr__(self, "_execution", execution)

    @property
    def client(self):
        execution = self._execution
        return None if execution is None else getattr(execution, "client", None)

    @property
    def loop(self):
        execution = self._execution
        return None if execution is None else getattr(execution, "loop", None)

    @property
    def worker(self):
        execution = self._execution
        return None if execution is None else getattr(execution, "worker", None)

    @property
    def executor(self):
        execution = self._execution
        return None if execution is None else getattr(execution, "executor", None)

    @property
    def mcp_sessions(self) -> tuple:
        execution = self._execution
        return () if execution is None else getattr(execution, "mcp_sessions", ())

    def build_worker(
        self,
        initial_context: Sequence[ModelContextItem] = (),
        *,
        task_mode: bool = True,
        claim_gate: bool = False,
    ) -> TurnWorker:
        execution = self._execution
        if execution is None:
            raise RuntimeAssemblyError("execution_plane_not_assembled")
        return execution.build_worker(
            initial_context, task_mode=task_mode, claim_gate=claim_gate,
        )

    def close(self) -> None:
        """Close the execution plane (if any) and the control staging dir."""
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        execution = self._execution
        if execution is not None:
            try:
                execution.close()
            except Exception as exc:
                _logger.warning("execution plane close failed: %r", exc)
        staging = self.staging_root
        if staging is not None:
            try:
                import shutil as _shutil

                _shutil.rmtree(staging, ignore_errors=True)
            except Exception:
                pass

    def __enter__(self) -> "ControlPlaneRuntime":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class GrantedExecutionPlan:
    """Preflight outcome: every activated server has an effective grant."""

    activated_plans: Mapping[str, StagedLaunchPlan]
    activated_views: Mapping[str, ActivationView]
    legacy_servers: tuple[McpServerConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class ActivationPending:
    """Preflight outcome: durable mcp_process_start ASKs exist; no Turn yet."""

    requests: Mapping[str, ActivationView]

    @property
    def request_ids(self) -> tuple[str, ...]:
        return tuple(
            view.request_id_str for view in self.requests.values()
        )


def _close_mcp_sessions(sessions: Sequence[object]) -> None:
    """Close sessions in reverse assembly order; never raises."""
    for session in reversed(tuple(sessions)):
        try:
            session.close()
        except Exception as exc:
            _logger.warning("mcp session close failed: %r", exc)


def assemble_control_plane(
    config: RuntimeConfig,
    *,
    config_base_dir: Path | None = None,
) -> ControlPlaneRuntime:
    """Control plane only: no model client, no MCP process spawn (I6 §8.9)."""
    if not isinstance(config, RuntimeConfig):
        raise TypeError("config must be RuntimeConfig")
    if not config.repo.is_dir():
        raise RuntimeAssemblyError("repo_not_found")
    base_dir = (
        Path(config_base_dir).resolve()
        if config_base_dir is not None
        else config.repo
    )
    try:
        store = SqliteEventStore(config.db, durable_limits=config.durable_limits)
        runtime = ThreadRuntime(
            store,
            actor="p0-runtime",
            text_policy=CanonicalTextPolicy.from_ingress(config.durable_limits),
        )
        ledger = ToolLedgerStore(store)
        approvals = ApprovalService(
            store,
            ledger,
            budget_action_limits=dict(config.budget_action_limits),
        )
        trace = TraceStore(store)
        trace_sink = BestEffortTraceSink(trace)
        correlation_id = uuid4()
        activation = ActivationService(
            store, approval_ttl_seconds=300,
        )
        staging_root = Path(tempfile.mkdtemp(prefix="koawa-mcp-staging-"))
        checkpoint_store = CheckpointStore(store)
        # Same repo boundary the execution registry uses: construct the git
        # facade so not_a_git_repository surfaces at control construction.
        from ..tools.workspace import WorkspacePathResolver

        git = GitFacade(
            config.repo,
            WorkspacePathResolver(config.repo),
        )
        return ControlPlaneRuntime(
            config=config,
            config_base_dir=base_dir,
            store=store,
            runtime=runtime,
            ledger=ledger,
            approvals=approvals,
            trace=trace,
            trace_sink=trace_sink,
            checkpoint_store=checkpoint_store,
            correlation_id=correlation_id,
            activation=activation,
            staging_root=staging_root,
            git=git,
        )
    except (RuntimeConfigError, RuntimeAssemblyError):
        raise
    except GitFacadeError as exc:
        raise RuntimeAssemblyError(getattr(exc, "code", "git_facade_failed")) from None
    except CommandRunnerError as exc:
        raise RuntimeAssemblyError(getattr(exc, "code", "command_runner_failed")) from None
    except SandboxError as exc:
        raise RuntimeAssemblyError(getattr(exc, "code", "sandbox_failed")) from None
    except Exception as exc:
        raise RuntimeAssemblyError("runtime_assembly_failed") from None


def _process_start_decision(
    server_config: McpServerConfig,
    scopes: tuple[str, ...],
) -> tuple[str, str]:
    """Policy decision for one process start (I6 §8.5)."""
    profile = server_config.execution_profile
    if profile is McpExecutionProfile.HOST_TRUSTED:
        scope = process_start_scope(profile)
        if scope not in scopes:
            return ("deny", scope)
        return ("ask", scope)
    if profile is McpExecutionProfile.SANDBOXED:
        # D25 W4: a valid sandboxed profile is a controlled allow inside the
        # mcp.use scope - the container boundary is the safety story, and the
        # per-tool policy/ledger chain still applies afterwards.
        scope = process_start_scope(profile)
        if scope not in scopes:
            return ("deny", scope)
        return ("allow", scope)
    # D25 W1: legacy (None-profile) configs lost the implicit allow in normal
    # assembly.  Only an explicitly marked test fixture keeps the old
    # decision; file-borne configs can never carry the marker.
    if not server_config.legacy_fixture:
        raise RuntimeAssemblyError("mcp_legacy_profile_requires_migration")
    return ("allow", process_start_scope(None))


def preflight_execution_activation(
    control: ControlPlaneRuntime,
    *,
    command_context: str,
) -> GrantedExecutionPlan | ActivationPending:
    """Resolve activated MCP launch identity + policy (I6 §8.5)."""
    if not isinstance(control, ControlPlaneRuntime):
        raise TypeError("control must be ControlPlaneRuntime")
    activated_plans: dict[str, StagedLaunchPlan] = {}
    activated_views: dict[str, ActivationView] = {}
    legacy_servers: list[McpServerConfig] = []
    scopes = control.config.policy.principal_scopes
    try:
        for server_config in control.config.mcp_servers:
            if server_config.execution_profile is None:
                legacy_servers.append(server_config)
                continue
            plan = stage_code_artifacts(
                server_config,
                base_dir=control.config_base_dir,
                staging_root=control.staging_root,
            )
            decision, scope = _process_start_decision(
                server_config, scopes,
            )
            view = control.activation.plan_start(
                plan.identity,
                principal_id="root",
                scope=scope,
                decision=decision,
            )
            activated_plans[server_config.server_id] = plan
            activated_views[server_config.server_id] = view
    except McpActivationError as exc:
        raise RuntimeAssemblyError(getattr(exc, "code", "mcp_activation_failed")) from None
    pending = {
        server_id: view
        for server_id, view in activated_views.items()
        if view.status == ActivationService.STATUS_REQUESTED
    }
    if pending:
        return ActivationPending(requests=pending)
    return GrantedExecutionPlan(
        activated_plans=activated_plans,
        activated_views=activated_views,
        legacy_servers=tuple(legacy_servers),
    )


def assemble_execution_plane(
    control: ControlPlaneRuntime,
    granted_plan: GrantedExecutionPlan,
    *,
    model_client: object | None = None,
    api_key: str | None = None,
    reasoning_sink: Callable[[str], None] | None = None,
    launcher_builder=None,
    post_build_registrars: Callable | tuple = (),
) -> AssembledRuntime:
    """Build the lazy execution plane: verified registry + MCP + client."""
    if not isinstance(control, ControlPlaneRuntime):
        raise TypeError("control must be ControlPlaneRuntime")
    if not isinstance(granted_plan, GrantedExecutionPlan):
        raise TypeError("granted_plan must be GrantedExecutionPlan")
    config = control.config
    store = control.store
    runtime = control.runtime
    ledger = control.ledger
    approvals = control.approvals
    trace = control.trace
    trace_sink = control.trace_sink
    correlation_id = control.correlation_id
    try:
        runner = _build_command_runner(config, store)
        plan_board = PlanBoard()
        # Audit F7: the JSON config admits up to 64 required profiles while
        # VerificationLimits defaults to max_test_runs=4, so 5+ profiles were
        # structurally unsatisfiable (the 5th reservation always failed).
        # Derive the budget from the config: every required profile must fit
        # at least one full pass plus repair cycles, inside the 1..32 bound.
        required_count = len(config.required_test_profiles or ())
        verification_limits = VerificationLimits(
            max_test_runs=min(32, max(4, 4 * required_count))
        )
        builtin_registry = build_verified_coding_tool_registry(
            config.repo,
            command_runner=runner,
            required_test_profiles=config.required_test_profiles,
            verification_limits=verification_limits,
            git_facade=control.git,
            plan_board=plan_board,
            post_build_registrars=tuple(post_build_registrars),
        )
        mcp_sessions, mcp_bindings = _connect_execution_mcp_servers(
            control, granted_plan, launcher_builder=launcher_builder,
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
            trace_sink,
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
            trace_sink=trace_sink,
            correlation_id=correlation_id,
        )
        checkpoint_store = control.checkpoint_store
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
        assembled = AssembledRuntime(
            config=config,
            store=store,
            runtime=runtime,
            ledger=ledger,
            approvals=approvals,
            trace=trace,
            trace_sink=trace_sink,
            registry=registry,
            executor=executor,
            client=client,
            worker=worker,
            checkpoint_store=checkpoint_store,
            correlation_id=correlation_id,
            loop=loop,
            mcp_sessions=mcp_sessions,
            plan_board=plan_board,
        )
        control.attach_execution(assembled)
        return assembled
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


def assemble_runtime(
    config: RuntimeConfig,
    *,
    model_client: object | None = None,
    api_key: str | None = None,
    reasoning_sink: Callable[[str], None] | None = None,
    config_base_dir: Path | None = None,
) -> AssembledRuntime:
    """Eager composition for deterministic tests and legacy callers."""
    control = assemble_control_plane(
        config, config_base_dir=config_base_dir,
    )
    plan = preflight_execution_activation(
        control, command_context="eager",
    )
    if isinstance(plan, ActivationPending):
        raise RuntimeAssemblyError("mcp_process_activation_pending")
    return assemble_execution_plane(
        control,
        plan,
        model_client=model_client,
        api_key=api_key,
        reasoning_sink=reasoning_sink,
    )


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
    trace_sink: TraceSink,
    correlation_id: object,
):
    # Legacy eager connect path (I6 §8.9); only profile-None servers reach it.
    sessions: list[tuple[McpServerConfig, object, McpCatalog]] = []
    bindings: dict[str, tuple[McpServerConfig, object, McpCatalog]] = {}
    opened: list[object] = []
    try:
        for server_config in config.mcp_servers:
            transport, session, catalog, adapter = _connect_legacy_server(
                server_config, trace_sink, correlation_id,
            )
            opened.append(session)
            sessions.append((server_config, session, catalog))
            bindings[server_config.server_id] = (
                server_config,
                adapter,
                catalog,
            )
        return tuple(sessions), bindings
    except BaseException:
        _close_mcp_sessions(opened)
        raise


def _connect_legacy_server(
    server_config: McpServerConfig,
    trace_sink: TraceSink,
    correlation_id: object,
):
    # Connect one pre-I6 (fixture stdio) server exactly as before.
    transport = StdioTransport(
        server_config.command,
        env=dict(server_config.environment),
        cwd=(
            None
            if server_config.cwd is None
            else str(server_config.cwd)
        ),
        process_start_timeout_seconds=(
            server_config.process_start_timeout_seconds
        ),
        shutdown_timeout_seconds=server_config.shutdown_timeout_seconds,
        max_inbound_messages=server_config.max_inbound_messages,
        max_stderr_bytes=server_config.max_stderr_bytes,
    )
    session = McpSession(
        server_config.server_id,
        transport,
        initialize_timeout_seconds=server_config.initialize_timeout_seconds,
        tools_list_timeout_seconds=server_config.tools_list_timeout_seconds,
        tool_call_timeout_seconds=server_config.tool_call_timeout_seconds,
        io_poll_timeout_seconds=server_config.io_poll_timeout_seconds,
        shutdown_timeout_seconds=server_config.shutdown_timeout_seconds,
        max_pending_requests=server_config.max_pending_requests,
        max_tools=server_config.max_tools,
        max_list_pages=server_config.max_list_pages,
        max_cursor_bytes=server_config.max_cursor_bytes,
        max_notifications_per_window=server_config.max_notifications_per_window,
        max_result_chars=server_config.max_result_bytes,
        trace_sink=trace_sink,
        correlation_id=correlation_id,
        tool_allowlist=(
            frozenset(server_config.tool_allowlist)
            if server_config.tool_allowlist is not None
            else None
        ),
    )
    catalog = session.connect()
    adapter = build_mcp_registry(session, catalog)
    return transport, session, catalog, adapter


def _connect_execution_mcp_servers(
    control: ControlPlaneRuntime,
    granted_plan: GrantedExecutionPlan,
    *,
    launcher_builder=None,
):
    # Execution-plane MCP connect in config order (legacy + activated).
    config = control.config
    sessions: list[tuple[McpServerConfig, object, McpCatalog]] = []
    bindings: dict[str, tuple[McpServerConfig, object, McpCatalog]] = {}
    opened: list[object] = []
    try:
        for server_config in config.mcp_servers:
            if server_config.execution_profile is None:
                transport, session, catalog, adapter = _connect_legacy_server(
                    server_config, control.trace_sink, control.correlation_id,
                )
            else:
                transport, session, catalog, adapter = _connect_activated_server(
                    control,
                    granted_plan,
                    server_config,
                    launcher_builder=launcher_builder,
                )
            opened.append(session)
            sessions.append((server_config, session, catalog))
            bindings[server_config.server_id] = (
                server_config,
                adapter,
                catalog,
            )
        return tuple(sessions), bindings
    except BaseException:
        _close_mcp_sessions(opened)
        raise


def _connect_activated_server(
    control: ControlPlaneRuntime,
    granted_plan: GrantedExecutionPlan,
    server_config: McpServerConfig,
    *,
    launcher_builder=None,
):
    # grant -> intend -> claim -> ticket -> launcher -> transport (I6 §8.5).
    plan = granted_plan.activated_plans.get(server_config.server_id)
    view = granted_plan.activated_views.get(server_config.server_id)
    if plan is None or view is None:
        raise RuntimeAssemblyError("mcp_grant_missing")
    activation = control.activation
    if view.status != ActivationService.STATUS_GRANTED:
        raise RuntimeAssemblyError("mcp_no_effective_grant")
    intent = activation.intend(view)
    ticket = activation.claim(
        intent, view, principal_id="root",
    )
    # Keep one allocation-store facade for the launcher and the transport
    # lifecycle reporter.  A normal endpoint close must release the matching
    # sandbox allocation; otherwise the MCP ledger says ``stopped`` while
    # the sandbox ledger remains an orphaned ``STARTED`` allocation.
    from ..sandbox.runtime import SandboxAllocationStore

    sandbox_store = SandboxAllocationStore(control.store)
    launcher = (
        launcher_builder(activation, server_config, plan)
        if launcher_builder is not None
        else _default_launcher(
            control, server_config, plan, sandbox_store=sandbox_store,
        )
    )

    def factory():
        try:
            return launcher.launch(ticket)
        except McpActivationError as exc:
            raise TransportError(exc.code) from None

    def reporter(state: str) -> None:
        _allocation_reporter(
            activation, ticket, state, sandbox_store=sandbox_store,
        )

    transport = StdioTransport(
        (),
        env={},
        cwd=None,
        process_factory=factory,
        allocation_state_reporter=reporter,
        process_start_timeout_seconds=(
            server_config.process_start_timeout_seconds
        ),
        shutdown_timeout_seconds=server_config.shutdown_timeout_seconds,
        max_inbound_messages=server_config.max_inbound_messages,
        max_stderr_bytes=server_config.max_stderr_bytes,
    )
    session = McpSession(
        server_config.server_id,
        transport,
        initialize_timeout_seconds=server_config.initialize_timeout_seconds,
        tools_list_timeout_seconds=server_config.tools_list_timeout_seconds,
        tool_call_timeout_seconds=server_config.tool_call_timeout_seconds,
        io_poll_timeout_seconds=server_config.io_poll_timeout_seconds,
        shutdown_timeout_seconds=server_config.shutdown_timeout_seconds,
        max_pending_requests=server_config.max_pending_requests,
        max_tools=server_config.max_tools,
        max_list_pages=server_config.max_list_pages,
        max_cursor_bytes=server_config.max_cursor_bytes,
        max_notifications_per_window=server_config.max_notifications_per_window,
        max_result_chars=server_config.max_result_bytes,
        trace_sink=control.trace_sink,
        correlation_id=control.correlation_id,
        launch_identity=plan.identity,
    )
    try:
        catalog = session.connect()
        activation.record_ready(ticket)
    except BaseException:
        try:
            activation.record_outcome_unknown(
                ticket, reason="connect_failed",
            )
        except Exception:
            pass
        raise
    adapter = build_mcp_registry(session, catalog)
    return transport, session, catalog, adapter


def _allocation_reporter(
    activation: ActivationService,
    ticket: AuthorizedLaunchTicket,
    state: str,
    *,
    sandbox_store=None,
) -> None:
    # Map transport lifecycle states onto allocation events (§8.5).
    try:
        if state == "started":
            activation.record_started(ticket)
        elif state == "failed_before_start":
            activation.record_failed_before_start(
                ticket, reason="launcher_failed",
            )
        elif state == "stopped":
            # Stopped is the only close outcome that proves cleanup.  Mirror
            # that proof into the sandbox ledger with exact-version appends;
            # an uncertain close deliberately leaves the allocation open for
            # DualLedgerReconciler instead of being cosmetically released.
            release_failed = False
            if sandbox_store is not None:
                allocation = sandbox_store.load(ticket.allocation_id)
                if allocation is not None:
                    state_value = getattr(allocation.state, "value", None)
                    if state_value in {"intended", "bound", "started"}:
                        try:
                            sandbox_store.finish(
                                allocation.allocation_id,
                                outcome="stopped",
                                exit_code=None,
                                oom_killed=None,
                            )
                            sandbox_store.release(
                                allocation.allocation_id,
                                reason="transport_stopped",
                            )
                        except Exception:
                            release_failed = True
            if release_failed:
                activation.record_outcome_unknown(
                    ticket, reason="sandbox_release_failed",
                )
            else:
                activation.record_stopped(ticket)
        elif state == "outcome_unknown":
            activation.record_outcome_unknown(
                ticket, reason="cleanup_uncertain",
            )
    except Exception:
        pass


def _default_launcher(
    control: ControlPlaneRuntime,
    server_config: McpServerConfig,
    plan: StagedLaunchPlan,
    *,
    sandbox_store=None,
) -> McpProcessLauncher:
    if server_config.execution_profile is McpExecutionProfile.SANDBOXED:
        from ..sandbox.runtime import SandboxAllocationStore
        from .mcp_sandbox_labels import MCP_SANDBOX_LABELS

        if sandbox_store is None:
            sandbox_store = SandboxAllocationStore(control.store)

        return SandboxedLauncher(
            control.activation,
            plan,
            sandbox_store=sandbox_store,
            docker_executable=control.config.sandbox.docker_executable,
            container_labels=MCP_SANDBOX_LABELS,
            process_start_timeout_seconds=(
                server_config.process_start_timeout_seconds
            ),
        )
    return HostTrustedLauncher(
        control.activation,
        plan,
        staging_root=control.staging_root,
    )


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
    trace_sink: TraceSink,
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
    # RT/J J2 / PSEC semantics-C: activate the canary gate only when the
    # config declares a canary key env var (resolve fails closed on a
    # configured-but-missing variable).  None keeps the executor's J2 checks
    # skipped — the pre-activation behavior, unchanged for existing configs.
    canary_key = resolve_canary_key(config)
    security_gate = (
        SecurityGate(ledger.event_store, canary_key)
        if canary_key is not None
        else None
    )
    return LedgerExecutor(
        registry,
        ledger,
        profiles,
        policy_engine=engine,
        approval_service=approvals,
        action_resolvers={name: resolve_tool for name in names},
        trace_sink=trace_sink,
        correlation_id=correlation_id,
        security_gate=security_gate,
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
