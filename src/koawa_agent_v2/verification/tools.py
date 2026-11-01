"""D5 测试、Git 证据、Finalizer 工具及完整 Coding Registry。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID

from ..execution.loop import ToolExecutionContext, ToolExecutionResult
from .finalization import VerificationError, VerificationLedger, VerificationLimits
from .git import GitDiffSnapshot, GitFacade, GitFacadeError, GitLimits
from .environment_signals import (
    classify_environment_failure,
    evidence_isolation,
    parse_test_count,
)
from . import output_policy
from ..editing.protocol import PatchLimits
from ..editing.tools import register_patch_tool
from ..editing.transaction import FaultInjector
from ..plan import PlanBoard
from ..plan.tools import register_plan_tool
from ..tools.repository import (
    RepositoryToolLimits,
    RepositoryToolRegistry,
    register_repository_tools,
)
from ..tools.errors import ToolConfigurationError, tool_error_result
from ..tools.repo_map import register_repo_map_tool
from ..tools.schema import ToolSpec
from .runner import (
    CommandOutcome,
    CommandProfile,
    CommandResult,
    CommandRunner,
    CommandRunnerError,
    RepositoryTrust,
    TrustedCommandRunner,
)
from ..tools.workspace import WorkspacePathResolver


# Issue #7 A4: a run may ask at most twice.  The same (profile, package
# set) is idempotent, so the effective bound is two DISTINCT plans per
# profile per run; beyond that the state is already a human handoff.
_MAX_PREPARATION_REQUESTS_PER_RUN = 2


@dataclass(frozen=True, slots=True)
class RunTestArguments:
    profile_id: str


@dataclass(frozen=True, slots=True)
class PreparationArguments:
    """Issue #7 A4: request controlled dependency preparation.

    ``packages`` may only name dependencies the runtime itself observed as
    missing; free-form names are refused so the model cannot turn this into
    an arbitrary install channel.  An empty tuple means "use the observed
    set".
    """

    profile_id: str
    packages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EmptyArguments:
    pass


@dataclass(frozen=True, slots=True)
class D5ToolLimits:
    max_tool_result_chars: int = 262_144
    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_tool_result_chars, int)
            or isinstance(self.max_tool_result_chars, bool)
            or self.max_tool_result_chars < 2_048
            or self.max_tool_result_chars > 1_000_000
        ):
            raise ToolConfigurationError("invalid_d5_tool_limits")


class CodingToolRegistry(RepositoryToolRegistry):
    """D3+D4+D5 工具目录，同时实现 AgentLoop CompletionGate。"""

    def __init__(
        self,
        resolver: WorkspacePathResolver,
        verification: VerificationLedger,
        git: GitFacade,
    ) -> None:
        super().__init__(resolver)
        self._verification = verification
        self.git = git

    def assert_complete(self, run_id: UUID) -> None:
        self._verification.assert_complete(run_id)


class _VerificationTools:
    def __init__(
        self,
        runner: CommandRunner,
        git: GitFacade,
        verification: VerificationLedger,
        limits: D5ToolLimits,
        release_contracts=None,
        dependency_scope=None,
        dependency_executor=None,
    ) -> None:
        self._runner = runner
        self._git = git
        self._verification = verification
        self._limits = limits
        # contracts.md §2/§4: resolves the per-profile release contract;
        # runners without the accessor (non-contract test doubles) keep the
        # legacy safe-diagnostic receipt shape.
        self._release_contracts = release_contracts
        # Withheld-retest gate state: (run_id, profile_id) -> generation at
        # which the last receipt was WITHHELD.  A re-test at the same
        # generation (no code change since) is probe retesting and is
        # refused; a patch bumps the generation and reopens the gate.
        self._withheld_marks: dict = {}
        # Issue #7 A3/A4: the runtime - never the model - decides what is
        # missing.  The observation is the only source a preparation request
        # may name, so the tool cannot become an arbitrary install channel.
        self._dependency_scope = dependency_scope
        self._dependency_executor = dependency_executor
        self._environment_observed: dict[str, tuple[str, tuple[str, ...]]] = {}
        self._prep_replies: dict[tuple[str, tuple[str, ...]], dict] = {}
        self._prep_counts: dict[tuple[UUID, str], int] = {}
        # Issue #7 C: (profile_id, profile_version) -> highest test count ever
        # observed.  A later green run that collected fewer tests is
        # degenerate evidence, not a pass; a new profile_version starts a new
        # baseline.
        self._collection_baseline: dict[tuple[str, str], int] = {}

    def run_test_profile(
        self,
        arguments: RunTestArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        try:
            self._runner.validate_profile(arguments.profile_id)
            generation = self._verification.reserve_test(context.run_id)
            if self._release_contracts is not None:
                mark = self._withheld_marks.get(
                    (context.run_id, arguments.profile_id),
                )
                if mark is not None and mark == generation:
                    # contracts.md §4: withheld results do not count as
                    # evidence; re-running without a code change is probe
                    # retesting and is refused with the fixed guidance.
                    return ToolExecutionResult(
                        json.dumps(
                            {
                                "profile_id": arguments.profile_id,
                                "test_output_policy": output_policy.POLICY_VERSION,
                                "availability": "result_withheld",
                                "human_required": True,
                                "retest_blocked": True,
                                "message": (
                                    "该 profile 的结果已扣且代码未变更，重测被拒绝。"
                                    "请整合已知信息给出最终报告，或修改代码后再测。"
                                ),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
            result = self._runner.run(
                arguments.profile_id,
                progress_guard=context.check_progress,
                execution_id=context.execution_id,
            )
            # Issue #7 C: a green run that collected FEWER tests than the
            # established baseline proves nothing - it is the "make the
            # failures disappear" reward hack.  The evidence is refused
            # before it is recorded, so finalize_task cannot use it.
            collected = parse_test_count(
                (result.stdout or "") + "\n" + (result.stderr or "")
            )
            if result.outcome is CommandOutcome.PASSED and collected is not None:
                collection_key = self._collection_key(arguments.profile_id)
                baseline = self._collection_baseline.get(collection_key)
                if baseline is not None and collected < baseline:
                    return ToolExecutionResult(
                        self._collection_regression_receipt(
                            arguments.profile_id, baseline, collected,
                        )
                    )
                self._collection_baseline[collection_key] = max(
                    baseline or 0, collected
                )
            self._verification.record_test(context.run_id, generation, result)
            # Issue #7 A1: record the LOCAL classification.  This is what a
            # subsequent preparation request may name; it is never derived
            # from model text.
            if result.outcome is CommandOutcome.PASSED:
                self._environment_observed.pop(arguments.profile_id, None)
            elif result.outcome is CommandOutcome.FAILED:
                signal, names = classify_environment_failure(
                    result.stdout, result.stderr
                )
                if signal is None:
                    self._environment_observed.pop(arguments.profile_id, None)
                else:
                    self._environment_observed[arguments.profile_id] = (
                        signal,
                        names,
                    )
            receipt_text = self._command_json(result)
            # Withheld-retest gate bookkeeping: mark the generation when the
            # receipt was withheld; clear it once a receipt is disclosable
            # (a code change reopens the normal repair loop).
            if self._release_contracts is not None:
                try:
                    receipt = json.loads(receipt_text)
                except (TypeError, ValueError):
                    receipt = None
                if (
                    isinstance(receipt, dict)
                    and receipt.get("execution_status") == "withheld"
                ):
                    self._withheld_marks[
                        (context.run_id, arguments.profile_id)
                    ] = generation
                else:
                    self._withheld_marks.pop(
                        (context.run_id, arguments.profile_id), None
                    )
            return ToolExecutionResult(receipt_text)
        except (CommandRunnerError, VerificationError) as error:
            return tool_error_result(error.code)

    def _collection_key(self, profile_id: str) -> tuple[str, str]:
        """Baseline identity: a new profile_version starts a new baseline."""

        version = ""
        if self._release_contracts is not None:
            try:
                contract = self._release_contracts(profile_id)
            except Exception:
                contract = None
            if contract is not None:
                version = contract.profile_version
        return (profile_id, version)

    def _collection_regression_receipt(
        self, profile_id: str, expected: int, observed: int,
    ) -> str:
        from .release_contracts import build_receipt

        if self._release_contracts is not None:
            contract = self._release_contracts(profile_id)
            if contract is not None:
                receipt = build_receipt(
                    contract,
                    execution_status="not_completed",
                    verdict="not_evaluated",
                    diagnostic=None,
                    collection_regression=(expected, observed),
                )
                return _bounded_json(receipt, self._limits.max_tool_result_chars)
        return _bounded_json(
            {
                "profile_id": profile_id,
                "execution_status": "not_completed",
                "verdict": "not_evaluated",
                "next_action": "repair_safe_environment",
                "test_output_policy": output_policy.POLICY_VERSION,
                "test_collection_regression": {
                    "expected": expected,
                    "observed": observed,
                },
            },
            self._limits.max_tool_result_chars,
        )

    def request_dependency_preparation(
        self,
        arguments: PreparationArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """Issue #7 A4/A5: the only channel from "missing" to "prepared".

        Every terminal branch maps to a fixed state - there is no branch that
        silently retries, so a missing dependency always converges (to a
        prepared environment or to a human handoff).
        """

        from .dependency_preparation import (
            EXEC_MATERIALIZED,
            PREP_READY,
            PREP_REFUSED,
            PREP_REQUIRES_AUTHORIZATION,
            PreparationExecutorError,
            plan_preparation,
        )

        profile_id = arguments.profile_id
        try:
            self._runner.validate_profile(profile_id)
        except CommandRunnerError as error:
            return tool_error_result(error.code)
        if self._dependency_scope is None:
            return tool_error_result("dependency_preparation_not_configured")
        observed = self._environment_observed.get(profile_id)
        if observed is None:
            return tool_error_result("no_environment_failure_observed")
        signal, known = observed
        requested = tuple(arguments.packages) if arguments.packages else known
        if not requested:
            # The run did not name an installable package (e.g. a broken
            # import inside an installed distribution).  No free-form text is
            # accepted as a substitute.
            return ToolExecutionResult(
                _bounded_json(
                    {
                        "profile_id": profile_id,
                        "status": "names_unavailable",
                        "environment_signal": signal,
                        "environment_ref": None,
                        "human_required": True,
                        "next_action": "human_required",
                    },
                    self._limits.max_tool_result_chars,
                )
            )
        if any(name not in known for name in requested):
            return tool_error_result("unexpected_dependency_request")

        cache_key = (profile_id, tuple(sorted(requested)))
        cached = self._prep_replies.get(cache_key)
        if cached is not None:
            replay = dict(cached)
            replay["idempotent_replay"] = True
            return ToolExecutionResult(
                _bounded_json(replay, self._limits.max_tool_result_chars)
            )
        usage_key = (context.run_id, profile_id)
        used = self._prep_counts.get(usage_key, 0)
        if used >= _MAX_PREPARATION_REQUESTS_PER_RUN:
            return tool_error_result("dependency_preparation_budget_exceeded")
        self._prep_counts[usage_key] = used + 1

        decision = plan_preparation(
            requested, self._dependency_scope, missing=known,
        )
        payload: dict[str, Any] = {
            "profile_id": profile_id,
            "status": decision.status,
            "install": list(decision.install),
            "requires_authorization": list(decision.requires_authorization),
            "refused": list(decision.refused),
            "reason": decision.reason,
            "environment_signal": signal,
            "environment_ref": None,
            "human_required": decision.status is not PREP_READY,
            "next_action": "human_required",
        }
        cacheable = True
        if decision.status == PREP_READY:
            if self._dependency_executor is None:
                payload["status"] = "executor_unavailable"
                payload["reason"] = "preparation_executor_unavailable"
                payload["human_required"] = True
            else:
                try:
                    outcome = self._dependency_executor.materialize(
                        decision.install, profile_id=profile_id,
                    )
                except PreparationExecutorError as error:
                    payload["status"] = "executor_failed"
                    payload["reason"] = error.code
                    payload["human_required"] = True
                    cacheable = False
                else:
                    payload["executor"] = outcome.status
                    payload["environment_ref"] = outcome.environment_ref
                    if outcome.status == EXEC_MATERIALIZED:
                        payload["human_required"] = False
                    else:
                        payload["reason"] = outcome.detail
                        payload["human_required"] = True
                        cacheable = False
        elif decision.status in (
            PREP_REQUIRES_AUTHORIZATION, PREP_REFUSED,
        ):
            payload["human_required"] = True
        payload["next_action"] = (
            "continue_safe_work"
            if payload["human_required"] is False
            else "human_required"
        )
        if cacheable:
            # Idempotency: the same (profile, package set) never re-runs the
            # executor and never consumes another budget slot.
            self._prep_replies[cache_key] = dict(payload)
        return ToolExecutionResult(
            _bounded_json(payload, self._limits.max_tool_result_chars)
        )

    def git_status(
        self,
        arguments: EmptyArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del arguments
        try:
            status = self._git.status(progress_guard=context.check_progress)
            self._verification.record_status(context.run_id, status)
            agent = self._git.agent_changed_paths(status)
            return ToolExecutionResult(
                _bounded_json(
                    {
                        "agent_changed_paths": list(agent),
                        "baseline_dirty_paths": list(self._git.baseline.paths),
                        "clean": not status.entries,
                        "status": [
                            {"path": item.path, "status": item.status}
                            for item in status.entries
                        ],
                        "status_sha256": status.digest,
                    },
                    self._limits.max_tool_result_chars,
                )
            )
        except GitFacadeError as error:
            return tool_error_result(error.code)

    def git_diff(
        self,
        arguments: EmptyArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del arguments
        try:
            diff = self._git.diff(progress_guard=context.check_progress)
            self._verification.record_diff(context.run_id, diff)
            return ToolExecutionResult(self._diff_json(diff))
        except GitFacadeError as error:
            return tool_error_result(error.code)

    def finalize_task(
        self,
        arguments: EmptyArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del arguments
        try:
            return ToolExecutionResult(self._verification.finalize(context.run_id))
        except VerificationError as error:
            return tool_error_result(
                error.code,
                field="profile_id" if error.profile_id is not None else None,
                detail=error.profile_id,
            )

    def _command_json(self, result: CommandResult) -> str:
        # Hardening 2026-09-19: the model-visible receipt carries fixed
        # structured diagnostics only - stdout/stderr bodies stay in the
        # durable record (operator side) and never enter the model service.
        # R3: the per-profile release contract then filters these fields; a
        # sensitive profile without a contract yields the fixed withheld
        # state (distinct from failure, human handoff flagged).
        if self._release_contracts is not None:
            return self._contract_receipt(result)
        payload: dict[str, Any] = {
            "allocation_id": (
                str(result.allocation_id) if result.allocation_id is not None else None
            ),
            "argv": list(result.argv),
            "backend": result.backend,
            "container_id": result.container_id,
            "duration_ms": result.duration_ms,
            "exit_code": result.exit_code,
            "immutable_image_id": result.immutable_image_id,
            "outcome": result.outcome.value,
            "profile_id": result.profile_id,
            "profile_digest": result.profile_digest,
            "stderr_bytes": result.stderr_bytes,
            "stderr_truncated": result.stderr_truncated,
            "stdout_bytes": result.stdout_bytes,
            "stdout_truncated": result.stdout_truncated,
            "timeout_seconds": result.timeout_seconds,
            "test_output_policy": output_policy.POLICY_VERSION,
            "test_output_visibility": output_policy.BODY_VISIBILITY,
            "isolated_workspace": bool(result.isolated_workspace),
        }
        if result.isolated_workspace:
            # WP-C v1: bounded structured excerpt for isolated/synthetic
            # runs - rich diagnostics by design, never a raw body dump.
            from ..sandbox.test_workspace import extract_diagnostics

            payload["diagnostics_excerpt"] = extract_diagnostics(result.stdout)
        return _bounded_json(payload, self._limits.max_tool_result_chars)

    def _contract_receipt(self, result: CommandResult) -> str:
        """contracts.md §4: receipt generated FROM the release contract.

        execution_status / verdict map from trusted process facts; the
        diagnostic body only appears in the modes and fields the contract
        allows.  The legacy legacy meta fields (test_output_visibility /
        isolated_workspace / diagnostics_excerpt) ride the diagnostic body
        under limited/detailed so isolation provenance survives contracts.
        """

        from .release_contracts import build_receipt
        from .runner import CommandOutcome

        contract = self._release_contracts(result.profile_id)
        if contract is None:
            # Unregistered profile: runner validation already refuses; the
            # receipt path fails closed as well.
            return _bounded_json(
                output_policy.withheld_receipt(result.profile_id),
                self._limits.max_tool_result_chars,
            )

        # Issue #7 A1: classify environment failures LOCALLY before mapping
        # the outcome.  A missing dependency still exits non-zero, so the
        # plain mapping would report "your code failed" and send the model
        # into a patch-and-retry loop it cannot win.
        environment_signal: str | None = None
        missing_dependencies: tuple[str, ...] = ()
        if result.outcome is CommandOutcome.FAILED:
            environment_signal, missing_dependencies = (
                classify_environment_failure(result.stdout, result.stderr)
            )

        if result.outcome is CommandOutcome.PASSED:
            execution_status, verdict = "completed", "passed"
        elif result.outcome is CommandOutcome.FAILED:
            if environment_signal is not None:
                # contracts.md §4: not_completed + not_evaluated - the tests
                # never produced a verdict about the candidate at all.
                execution_status, verdict = "not_completed", "not_evaluated"
            else:
                execution_status, verdict = "completed", "failed"
        elif result.outcome is CommandOutcome.START_FAILED:
            # K05: tests never ran - never write failed.
            execution_status, verdict = "not_completed", "not_evaluated"
        else:  # TIMED_OUT / cancelled: result unknown.
            execution_status, verdict = "uncertain", "unknown"

        isolation = evidence_isolation(result.backend)
        receipt_contract = contract
        environment_unproven = False
        if contract.environment_ref and isolation != "container":
            # Issue #7 E1/E3: ``environment_ref`` is a declaration; only an
            # isolated run can prove it.  A host run that *declares* an
            # environment does not get the detailed body or the proof flag.
            from dataclasses import replace as _replace

            receipt_contract = _replace(contract, environment_ref=None)
            environment_unproven = True

        mode_allows_body = receipt_contract.release_mode.value in {
            "limited", "detailed",
        } and receipt_contract.effective_mode_allows_body()
        diagnostic: dict = {}
        if mode_allows_body:
            diagnostic = output_policy.safe_diagnostics(result)
            # Execution identity fields are releasable structured provenance
            # (D8 governance reads them); never secrets.
            for key in (
                "backend",
                "immutable_image_id",
                "profile_digest",
                "allocation_id",
                "container_id",
            ):
                value = getattr(result, key, None)
                if value is not None:
                    diagnostic[key] = (
                        str(value) if not isinstance(value, str) else value
                    )
            diagnostic["isolated_workspace"] = bool(
                result.isolated_workspace
            )
            # detailed (effective, i.e. with audience+environment proofs)
            # discloses a BOUNDED failure excerpt - assertion lines and the
            # head of the first traceback only.  Without it a real failing
            # test is undebuggable (exit code alone cannot locate the fault);
            # the full body still never leaves the operator side.
            from .release_contracts import (
                ReleaseMode as _ReleaseMode,
                effective_release_mode as _effective,
            )

            if (
                _effective(
                    receipt_contract.release_mode, receipt_contract,
                )
                is _ReleaseMode.DETAILED
                and verdict in ("failed", "unknown")
            ):
                from ..sandbox.test_workspace import extract_diagnostics

                combined = (result.stdout or "") + "\n" + (result.stderr or "")
                diagnostic["diagnostics_excerpt"] = extract_diagnostics(combined)
            # §2 release_fields: unlisted fields are not shown.
            if contract.release_fields is not None:
                diagnostic = {
                    key: value
                    for key, value in diagnostic.items()
                    if key in contract.release_fields
                }
            # §2 producer_rules execution point: when rules are configured,
            # a field is shown only if a rule names it AND names the trusted
            # producer that generated it; unlisted fields are never shown.
            from .release_contracts import apply_producer_rules

            diagnostic = apply_producer_rules(
                diagnostic, contract.producer_rules,
            )
        receipt = build_receipt(
            receipt_contract,
            execution_status=execution_status,
            verdict=verdict,
            diagnostic=diagnostic,
            capture_truncated=bool(
                result.stdout_truncated or result.stderr_truncated
            ),
            environment_signal=environment_signal,
            missing_dependencies=missing_dependencies,
            isolation=isolation,
        )
        if environment_unproven:
            # Content-free marker: the declared environment was not proven by
            # the evidence.  Never silently upgrade it to "proven".
            receipt["environment_unproven"] = True
        return _bounded_json(receipt, self._limits.max_tool_result_chars)

    def _diff_json(self, diff: GitDiffSnapshot) -> str:
        return _bounded_json(
            {
                "changed_paths": list(diff.changed_paths),
                "diff": diff.diff,
                "diff_sha256": diff.diff_sha256,
                "diff_truncated": diff.truncated,
                "status_sha256": diff.status_digest,
            },
            self._limits.max_tool_result_chars,
            crop=("diff",),
        )


def build_verified_coding_tool_registry(
    workspace_root: str | Path,
    *,
    command_profiles: Sequence[CommandProfile] | None = None,
    command_runner: CommandRunner | None = None,
    required_test_profiles: Sequence[str] | None = None,
    repository_trust: RepositoryTrust = RepositoryTrust.UNTRUSTED,
    repository_limits: RepositoryToolLimits | None = None,
    patch_limits: PatchLimits | None = None,
    git_limits: GitLimits | None = None,
    verification_limits: VerificationLimits | None = None,
    tool_limits: D5ToolLimits | None = None,
    fault_injector: FaultInjector | None = None,
    git_facade: GitFacade | None = None,
    plan_board: PlanBoard | None = None,
    post_build_registrars: Sequence = (),
    protected_patterns: tuple[str, ...] = (),
    dependency_scope=None,
    dependency_executor=None,
) -> CodingToolRegistry:
    """构建 D3 read/search + D4 patch + D5 test/git/finalize 的完整目录。"""
    repository_limits = repository_limits or RepositoryToolLimits()
    patch_limits = patch_limits or PatchLimits()
    tool_limits = tool_limits or D5ToolLimits()
    if patch_limits.max_file_bytes > repository_limits.max_file_bytes:
        raise ToolConfigurationError("incompatible_coding_tool_limits")
    resolver = WorkspacePathResolver(
        workspace_root,
        hard_max_read_bytes=max(repository_limits.max_file_bytes, patch_limits.max_file_bytes),
        hard_max_directory_scan_entries=repository_limits.max_directory_scan_entries,
    )
    try:
        if git_facade is not None and not isinstance(git_facade, GitFacade):
            raise TypeError("git_facade must be GitFacade or None")
        git = git_facade or GitFacade(workspace_root, resolver, limits=git_limits)
        if command_runner is not None:
            if command_profiles is not None:
                raise ToolConfigurationError("ambiguous_command_runner_configuration")
            if not isinstance(command_runner, CommandRunner):
                raise TypeError("command_runner must implement CommandRunner")
            runner = command_runner
        else:
            if command_profiles is None:
                raise ToolConfigurationError("command_runner_required")
            runner = TrustedCommandRunner(
                workspace_root,
                command_profiles,
                trust=repository_trust,
            )
        verification = VerificationLedger(
            git,
            profile_ids=runner.profile_ids,
            required_test_profiles=required_test_profiles,
            limits=verification_limits,
        )
        registry = CodingToolRegistry(resolver, verification, git)
        register_repository_tools(registry, resolver, limits=repository_limits)
        register_patch_tool(
            registry,
            workspace_root,
            resolver,
            limits=patch_limits,
            fault_injector=fault_injector,
            protected_paths=git.protected_paths,
            protected_patterns=protected_patterns,
            observer=verification.record_patch,
        )
        _register_verification_tools(
            registry,
            runner,
            git,
            verification,
            tool_limits,
            release_contracts=getattr(runner, "release_contract", None),
            dependency_scope=dependency_scope,
            dependency_executor=dependency_executor,
        )
        if plan_board is not None:
            register_plan_tool(registry, plan_board)
        register_repo_map_tool(registry, resolver)
        # RT-1 control-exercise additions register here, before the first
        # definitions() call seals the catalog (e.g. the frozen loopback
        # egress probe).  Registrars are trusted code, never config-driven.
        for registrar in post_build_registrars:
            registrar(registry)
        return registry
    except BaseException:
        resolver.close()
        raise


def _register_verification_tools(
    registry: CodingToolRegistry,
    runner: CommandRunner,
    git: GitFacade,
    verification: VerificationLedger,
    limits: D5ToolLimits,
    release_contracts=None,
    dependency_scope=None,
    dependency_executor=None,
) -> None:
    tools = _VerificationTools(
        runner,
        git,
        verification,
        limits,
        release_contracts=release_contracts,
        dependency_scope=dependency_scope,
        dependency_executor=dependency_executor,
    )
    empty_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    registry.register(
        ToolSpec(
            "run_test_profile",
            "Run one administrator-configured trusted test profile; never accepts argv or shell.",
            RunTestArguments,
            {
                "type": "object",
                "properties": {
                    "profile_id": {"type": "string", "minLength": 1, "maxLength": 64}
                },
                "required": ["profile_id"],
                "additionalProperties": False,
            },
        ),
        tools.run_test_profile,
    )
    if dependency_scope is not None:
        # Issue #7 A4: only registered when a preparation scope exists.  The
        # tool can name nothing the runtime did not itself observe as missing.
        registry.register(
            ToolSpec(
                "request_dependency_preparation",
                (
                    "Request controlled preparation of dependencies the runtime "
                    "observed as missing; names outside that set are refused."
                ),
                PreparationArguments,
                {
                    "type": "object",
                    "properties": {
                        "profile_id": {
                            "type": "string", "minLength": 1, "maxLength": 64,
                        },
                        "packages": {
                            "type": "array",
                            "items": {
                                "type": "string", "minLength": 1, "maxLength": 128,
                            },
                            "maxItems": 8,
                        },
                    },
                    "required": ["profile_id"],
                    "additionalProperties": False,
                },
            ),
            tools.request_dependency_preparation,
        )
    for name, description, handler in (
        ("git_status", "Read bounded Git status without repository-defined executors.", tools.git_status),
        ("git_diff", "Read bounded agent-owned Git diff without external diff or textconv.", tools.git_diff),
        ("finalize_task", "Verify current-generation passing tests, status, and diff before final answer.", tools.finalize_task),
    ):
        registry.register(ToolSpec(name, description, EmptyArguments, empty_schema), handler)


def _bounded_json(
    payload: dict[str, Any],
    max_chars: int,
    *,
    crop: tuple[str, ...] = (),
) -> str:
    def encode() -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    encoded = encode()
    if len(encoded) <= max_chars:
        return encoded
    payload["output_truncated"] = True
    for key in crop:
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            payload[key] = value[:middle]
            if len(encode()) <= max_chars:
                low = middle
            else:
                high = middle - 1
        payload[key] = value[:low]
        encoded = encode()
        if len(encoded) <= max_chars:
            return encoded
    raise ToolConfigurationError("d5_tool_result_too_large")
