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
    CommandProfile,
    CommandResult,
    CommandRunner,
    CommandRunnerError,
    RepositoryTrust,
    TrustedCommandRunner,
)
from ..tools.workspace import WorkspacePathResolver


@dataclass(frozen=True, slots=True)
class RunTestArguments:
    profile_id: str


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
            self._verification.record_test(context.run_id, generation, result)
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

        if result.outcome is CommandOutcome.PASSED:
            execution_status, verdict = "completed", "passed"
        elif result.outcome is CommandOutcome.FAILED:
            execution_status, verdict = "completed", "failed"
        elif result.outcome is CommandOutcome.START_FAILED:
            # K05: tests never ran - never write failed.
            execution_status, verdict = "not_completed", "not_evaluated"
        else:  # TIMED_OUT / cancelled: result unknown.
            execution_status, verdict = "uncertain", "unknown"

        mode_allows_body = contract.release_mode.value in {
            "limited", "detailed",
        } and contract.effective_mode_allows_body()
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
                _effective(contract.release_mode, contract)
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
            contract,
            execution_status=execution_status,
            verdict=verdict,
            diagnostic=diagnostic,
            capture_truncated=bool(
                result.stdout_truncated or result.stderr_truncated
            ),
        )
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
) -> None:
    tools = _VerificationTools(
        runner, git, verification, limits, release_contracts=release_contracts,
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
