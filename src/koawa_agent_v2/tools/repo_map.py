"""D24 W3: repo_map —— 有界目录树 + Python 符号大纲（低信任元数据）。

治理合同：本工具只产出路径与符号名，不产出文件内容；模型取内容仍必须走
read_file（resolver 强制的路径边界与哈希校验不被绕过）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..execution.loop import ToolExecutionContext, ToolExecutionResult
from ..tools.errors import tool_error_result
from ..tools.registry import ToolRegistry
from ..tools.schema import ToolSpec
from .repository import _is_control_path
from .workspace import WorkspaceEntryKind, WorkspacePathError, WorkspacePathResolver

_DEF_PATTERN = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+([A-Za-z_]\w*)", re.MULTILINE)
_CLASS_PATTERN = re.compile(r"^[ \t]*class[ \t]+([A-Za-z_]\w*)", re.MULTILINE)


class RepoMapError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RepoMapLimits:
    max_entries: int = 256
    max_depth: int = 6
    max_scan_entries: int = 2_000
    max_files: int = 200
    max_symbol_probe_bytes: int = 32_768
    max_symbols_per_file: int = 12
    max_result_chars: int = 16_000

    def __post_init__(self) -> None:
        for name, value in (
            ("max_entries", self.max_entries),
            ("max_depth", self.max_depth),
            ("max_scan_entries", self.max_scan_entries),
            ("max_files", self.max_files),
            ("max_symbol_probe_bytes", self.max_symbol_probe_bytes),
            ("max_symbols_per_file", self.max_symbols_per_file),
            ("max_result_chars", self.max_result_chars),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise RepoMapError("repo_map_limits_invalid")


@dataclass(frozen=True, slots=True)
class RepoMapArguments:
    path: str
    max_depth: int


def repo_map_tool_spec(limits: RepoMapLimits | None = None) -> ToolSpec[RepoMapArguments]:
    limits = limits or RepoMapLimits()
    return ToolSpec(
        "repo_map",
        "Bounded workspace map: directory tree plus top Python symbols per "
        "file. Metadata only; use read_file for content.",
        RepoMapArguments,
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Root relative to the REPOSITORY ROOT using forward "
                        "slashes, e.g. 'src/koawa_agent_v2/runtime' - never "
                        "prefix the repository folder name; use '.' for the root."
                    ),
                    "minLength": 1,
                    "maxLength": 512,
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum recursive depth; zero lists direct children only.",
                    "minimum": 0,
                    "maximum": limits.max_depth,
                },
            },
            "required": ["path", "max_depth"],
            "additionalProperties": False,
        },
    )


def _workspace_error_result(error: WorkspacePathError) -> ToolExecutionResult:
    return tool_error_result(error.code)


class _RepoMapBuilder:
    def __init__(
        self,
        resolver: WorkspacePathResolver,
        limits: RepoMapLimits,
        max_depth: int,
    ) -> None:
        self._resolver = resolver
        self._limits = limits
        self._max_depth = max_depth
        self._entries: list[dict[str, Any]] = []
        self._scanned = 0
        self._py_files = 0
        self.truncated = False
        self.reason: str | None = None

    def _budget_left(self) -> bool:
        if self._scanned >= self._limits.max_scan_entries:
            self.truncated = True
            self.reason = "scan_limit"
            return False
        if len(self._entries) >= self._limits.max_entries:
            self.truncated = True
            self.reason = "entry_limit"
            return False
        return True

    def walk(self, path: str) -> None:
        pending: list[tuple[str, int]] = [(path, 0)]
        while pending and not self.truncated:
            directory, depth = pending.pop()
            remaining_scan = self._limits.max_scan_entries - self._scanned
            if remaining_scan <= 0:
                self.truncated = True
                self.reason = "scan_limit"
                break
            listing = self._resolver.list_directory(
                directory,
                max_entries=min(remaining_scan, self._limits.max_entries),
                max_scan_entries=remaining_scan,
            )
            self._scanned += listing.total_entries
            if listing.truncated:
                self.truncated = True
                self.reason = "scan_limit"
            for entry in sorted(listing.entries, key=lambda item: item.path):
                if _is_control_path(entry.path):
                    continue
                if not self._budget_left():
                    return
                if entry.kind is WorkspaceEntryKind.DIRECTORY:
                    self._entries.append({"path": entry.path, "kind": "dir"})
                    if depth < self._max_depth:
                        pending.append((entry.path, depth + 1))
                else:
                    if entry.path.endswith(".py"):
                        if self._py_files >= self._limits.max_files:
                            self.truncated = True
                            self.reason = "file_limit"
                            continue
                        self._py_files += 1
                        self._entries.append(
                            {
                                "path": entry.path,
                                "kind": "py",
                                "symbols": self._symbols(entry.path),
                            }
                        )
                    else:
                        self._entries.append(
                            {"path": entry.path, "kind": "file", "size": entry.size}
                        )

    def _symbols(self, path: str) -> list[str]:
        try:
            read = self._resolver.read_bytes(
                path, max_bytes=self._limits.max_symbol_probe_bytes
            )
        except WorkspacePathError:
            return []
        prefix = read.data.decode("utf-8", "ignore")
        found: list[str] = []
        for pattern in (_CLASS_PATTERN, _DEF_PATTERN):
            for name in pattern.findall(prefix):
                if name not in found:
                    found.append(name)
                if len(found) >= self._limits.max_symbols_per_file:
                    return found
        return found

    def payload(self, root: str) -> dict[str, Any]:
        return {
            "root": root,
            "entries": self._entries,
            "truncated": self.truncated,
            "reason": self.reason,
            "scanned_entries": self._scanned,
        }


def register_repo_map_tool(
    registry: ToolRegistry,
    resolver: WorkspacePathResolver,
    *,
    limits: RepoMapLimits | None = None,
) -> None:
    limits = limits or RepoMapLimits()

    def handler(
        arguments: RepoMapArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del context
        if _is_control_path(arguments.path):
            return tool_error_result("repository_control_path_forbidden")
        try:
            builder = _RepoMapBuilder(resolver, limits, arguments.max_depth)
            builder.walk(arguments.path)
            payload = builder.payload(arguments.path)
        except WorkspacePathError as error:
            return _workspace_error_result(error)
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(content) > limits.max_result_chars:
            payload["truncated"] = True
            payload["reason"] = "result_limit"
            # 截断时丢弃符号细节之外的体量：按路径序保留前 N 项。
            kept: list[dict[str, Any]] = []
            size = len(json.dumps({k: v for k, v in payload.items() if k != "entries"}))
            for item in payload["entries"]:
                candidate = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if size + len(candidate) + 32 > limits.max_result_chars:
                    payload["truncated"] = True
                    payload["reason"] = "result_limit"
                    break
                size += len(candidate) + 1
                kept.append(item)
            payload["entries"] = kept
            content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return ToolExecutionResult(content=content)

    registry.register(repo_map_tool_spec(limits), handler)
