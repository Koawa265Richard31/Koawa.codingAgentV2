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
from ..editing.protocol import PatchLimits
from ..editing.tools import register_patch_tool
from ..editing.transaction import FaultInjector
from ..tools.repository import (
    RepositoryToolLimits,
    RepositoryToolRegistry,
    register_repository_tools,
)
from ..tools.errors import ToolConfigurationError, tool_error_result
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
    ) -> None:
        self._runner = runner
        self._git = git
        self._verification = verification
        self._limits = limits

    def run_test_profile(
        self,
        arguments: RunTestArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        try:
            self._runner.validate_profile(arguments.profile_id)
            generation = self._verification.reserve_test(context.run_id)
            result = self._runner.run(
                arguments.profile_id,
                progress_guard=context.check_progress,
                execution_id=context.execution_id,
            )
            self._verification.record_test(context.run_id, generation, result)
            return ToolExecutionResult(self._command_json(result))
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
            return tool_error_result(error.code)

    def _command_json(self, result: CommandResult) -> str:
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
            "stderr": result.stderr,
            "stderr_bytes": result.stderr_bytes,
            "stderr_truncated": result.stderr_truncated,
            "stdout": result.stdout,
            "stdout_bytes": result.stdout_bytes,
            "stdout_truncated": result.stdout_truncated,
            "timeout_seconds": result.timeout_seconds,
        }
        return _bounded_json(payload, self._limits.max_tool_result_chars, crop=("stdout", "stderr"))

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
    repository_trust: RepositoryTrust = RepositoryTrust.UNTRUSTED,
    repository_limits: RepositoryToolLimits | None = None,
    patch_limits: PatchLimits | None = None,
    git_limits: GitLimits | None = None,
    verification_limits: VerificationLimits | None = None,
    tool_limits: D5ToolLimits | None = None,
    fault_injector: FaultInjector | None = None,
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
        git = GitFacade(workspace_root, resolver, limits=git_limits)
        verification = VerificationLedger(git, limits=verification_limits)
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
        registry = CodingToolRegistry(resolver, verification, git)
        register_repository_tools(registry, resolver, limits=repository_limits)
        register_patch_tool(
            registry,
            workspace_root,
            resolver,
            limits=patch_limits,
            fault_injector=fault_injector,
            protected_paths=git.protected_paths,
            observer=verification.record_patch,
        )
        _register_verification_tools(registry, runner, git, verification, tool_limits)
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
) -> None:
    tools = _VerificationTools(runner, git, verification, limits)
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
