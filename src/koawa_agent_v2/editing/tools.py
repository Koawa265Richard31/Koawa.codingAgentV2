"""D4 ``apply_patch`` ToolSpec、handler 与 D3+D4 组合 Registry。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..execution.loop import ToolExecutionContext, ToolExecutionResult
from .protocol import PatchError, PatchLimits, parse_patch_document
from .transaction import (
    AtomicPatchWorkspace,
    FaultInjector,
    PatchTransactionResult,
)
from ..tools.repository import (
    RepositoryToolLimits,
    RepositoryToolRegistry,
    register_repository_tools,
)
from ..tools.errors import ToolConfigurationError, tool_error_result
from ..tools.schema import ToolSpec
from ..tools.workspace import WorkspacePathResolver


PatchObserver = Callable[
    [ToolExecutionContext, PatchTransactionResult],
    None,
]


@dataclass(frozen=True, slots=True)
class ApplyPatchArguments:
    patch_json: str


def apply_patch_tool_spec(limits: PatchLimits | None = None) -> ToolSpec[ApplyPatchArguments]:
    """外层 schema 保持 D3 子集；嵌套 Patch 文档由版本化 parser 严格校验。"""
    limits = limits or PatchLimits()
    if not isinstance(limits, PatchLimits):
        raise TypeError("limits must be PatchLimits")
    return ToolSpec(
        "apply_patch",
        (
            "Atomically add, update, or delete UTF-8 workspace files using a "
            "schema_version=1 structured patch JSON document."
        ),
        ApplyPatchArguments,
        {
            "type": "object",
            "properties": {
                "patch_json": {
                    "type": "string",
                    "description": (
                        "Strict JSON object with schema_version and changes. UPDATE/DELETE "
                        "must use the SHA-256 returned by read_file."
                    ),
                    "minLength": 1,
                    "maxLength": limits.max_patch_json_chars,
                }
            },
            "required": ["patch_json"],
            "additionalProperties": False,
        },
    )


class _ApplyPatchTool:
    def __init__(
        self,
        workspace: AtomicPatchWorkspace,
        limits: PatchLimits,
        *,
        protected_paths: frozenset[str] = frozenset(),
        observer: PatchObserver | None = None,
    ) -> None:
        self._workspace = workspace
        self._limits = limits
        self._protected_paths = protected_paths
        self._observer = observer

    def __call__(
        self,
        arguments: ApplyPatchArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        # D7 会在这里用 context.call_ref/run_id 做 durable claim；D4 只在当前
        # 活跃 Worker 的工具边界内完成原子文件事务，不声称 crash exactly-once。
        try:
            patch = parse_patch_document(arguments.patch_json, self._limits)
            if any(
                change.path.casefold() in self._protected_paths
                for change in patch.changes
            ):
                return tool_error_result("baseline_dirty_path_forbidden")
            result = self._workspace.apply(
                patch,
                progress_guard=context.check_progress,
            )
            if self._observer is not None:
                self._observer(context, result)
            return ToolExecutionResult(
                result.to_tool_content(max_chars=self._limits.max_result_chars)
            )
        except PatchError as error:
            return tool_error_result(error.code)


def register_patch_tool(
    registry: RepositoryToolRegistry,
    workspace_root: str | Path,
    resolver: WorkspacePathResolver,
    *,
    limits: PatchLimits | None = None,
    fault_injector: FaultInjector | None = None,
    protected_paths: frozenset[str] = frozenset(),
    observer: PatchObserver | None = None,
) -> None:
    """注册 D4 工具；D5 可附加脏文件保护与成功修改证据。"""
    limits = limits or PatchLimits()
    workspace = AtomicPatchWorkspace(
        workspace_root,
        resolver,
        limits=limits,
        fault_injector=fault_injector,
    )
    registry.register(
        apply_patch_tool_spec(limits),
        _ApplyPatchTool(
            workspace,
            limits,
            protected_paths=protected_paths,
            observer=observer,
        ),
    )


def build_coding_tool_registry(
    workspace_root: str | Path,
    *,
    repository_limits: RepositoryToolLimits | None = None,
    patch_limits: PatchLimits | None = None,
    fault_injector: FaultInjector | None = None,
) -> RepositoryToolRegistry:
    """构建 D3 read/list/search + D4 apply_patch 的单一、可 seal Registry。"""
    repository_limits = repository_limits or RepositoryToolLimits()
    patch_limits = patch_limits or PatchLimits()
    if not isinstance(repository_limits, RepositoryToolLimits):
        raise TypeError("repository_limits must be RepositoryToolLimits")
    if not isinstance(patch_limits, PatchLimits):
        raise TypeError("patch_limits must be PatchLimits")
    if patch_limits.max_file_bytes > repository_limits.max_file_bytes:
        raise ToolConfigurationError("incompatible_coding_tool_limits")
    resolver = WorkspacePathResolver(
        workspace_root,
        hard_max_read_bytes=max(
            repository_limits.max_file_bytes, patch_limits.max_file_bytes
        ),
        hard_max_directory_scan_entries=repository_limits.max_directory_scan_entries,
    )
    try:
        registry = RepositoryToolRegistry(resolver)
        register_repository_tools(
            registry, resolver, limits=repository_limits
        )
        register_patch_tool(
            registry,
            workspace_root,
            resolver,
            limits=patch_limits,
            fault_injector=fault_injector,
        )
        return registry
    except BaseException:
        resolver.close()
        raise
