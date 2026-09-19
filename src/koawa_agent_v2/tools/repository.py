"""D3 只读仓库工具：同源 schema、有界执行与稳定 JSON 结果。

``read_file``、``list_files`` 和 ``search_text`` 不直接打开模型提供的路径；
所有文件系统访问都通过 :class:`WorkspacePathResolver` 完成。这一层
负责工具语义和预算，路径边界、链接/重解析点拒绝则由 resolver 负责。
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..execution.loop import ToolExecutionContext, ToolExecutionResult
from .errors import ToolConfigurationError, tool_error_result
from .registry import ToolRegistry
from .schema import ToolSpec
from .workspace import (
    WorkspaceDirectoryEntry,
    WorkspaceEntryKind,
    WorkspacePathError,
    WorkspacePathResolver,
)


_DEFAULT_SKIPPED_DIRECTORIES = frozenset({".git", ".hg", ".svn", "__pycache__"})
_MIN_RESULT_BUDGET = 512
_REPOSITORY_LIMIT_CEILINGS = {
    "max_path_chars": 4_096,
    "max_query_chars": 4_096,
    "max_glob_patterns": 64,
    "max_glob_pattern_chars": 1_024,
    "max_file_bytes": 16 * 1024 * 1024,
    "max_start_line": 2_147_483_647,
    "max_read_lines": 10_000,
    "max_directory_entries": 10_000,
    "max_directory_scan_entries": 100_000,
    "max_search_files": 10_000,
    "max_search_total_bytes": 64 * 1024 * 1024,
    "max_depth": 64,
    "max_matches": 10_000,
    "max_match_chars": 4_096,
    "max_output_chars": 262_144,
}


@dataclass(frozen=True, slots=True)
class RepositoryToolLimits:
    """Provider schema 与 handler 共用的硬限额。"""

    max_path_chars: int = 1_024
    max_query_chars: int = 256
    max_glob_patterns: int = 16
    max_glob_pattern_chars: int = 256
    max_file_bytes: int = 1_000_000
    max_start_line: int = 2_147_483_647
    max_read_lines: int = 500
    max_directory_entries: int = 1_000
    max_directory_scan_entries: int = 10_000
    max_search_files: int = 1_000
    max_search_total_bytes: int = 10_000_000
    max_depth: int = 12
    max_matches: int = 500
    max_match_chars: int = 400
    max_output_chars: int = 100_000

    def __post_init__(self) -> None:
        for name in (
            "max_path_chars",
            "max_query_chars",
            "max_glob_patterns",
            "max_glob_pattern_chars",
            "max_file_bytes",
            "max_start_line",
            "max_read_lines",
            "max_directory_entries",
            "max_directory_scan_entries",
            "max_search_files",
            "max_search_total_bytes",
            "max_depth",
            "max_matches",
            "max_match_chars",
            "max_output_chars",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ToolConfigurationError("invalid_repository_tool_limits")
            if value > _REPOSITORY_LIMIT_CEILINGS[name]:
                raise ToolConfigurationError("invalid_repository_tool_limits")
        if self.max_output_chars < _MIN_RESULT_BUDGET:
            raise ToolConfigurationError("invalid_repository_tool_limits")
        if self.max_directory_entries > self.max_directory_scan_entries:
            raise ToolConfigurationError("invalid_repository_tool_limits")


@dataclass(frozen=True, slots=True)
class ReadFileArguments:
    path: str
    start_line: int
    max_lines: int


@dataclass(frozen=True, slots=True)
class ListFilesArguments:
    path: str
    max_depth: int
    max_entries: int


@dataclass(frozen=True, slots=True)
class SearchTextArguments:
    query: str
    path: str
    max_depth: int
    max_files: int
    max_matches: int
    case_sensitive: bool = True
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


def repository_tool_specs(
    limits: RepositoryToolLimits | None = None,
) -> tuple[
    ToolSpec[ReadFileArguments],
    ToolSpec[ListFilesArguments],
    ToolSpec[SearchTextArguments],
]:
    """从同一份硬限额生成模型 definition 和运行时 decoder。"""
    limits = limits or RepositoryToolLimits()
    if not isinstance(limits, RepositoryToolLimits):
        raise TypeError("limits must be RepositoryToolLimits")

    path_schema = {
        "type": "string",
        "description": (
            "Path relative to the REPOSITORY ROOT using forward slashes, "
            "e.g. 'src/main.py' - never prefix the repository folder name."
        ),
        "minLength": 1,
        "maxLength": limits.max_path_chars,
    }
    glob_item_schema = {
        "type": "string",
        "description": "A bounded glob matched only against validated relative paths.",
        "minLength": 1,
        "maxLength": limits.max_glob_pattern_chars,
    }
    read_spec = ToolSpec(
        "read_file",
        "Read a bounded UTF-8 text slice from one workspace file.",
        ReadFileArguments,
        {
            "type": "object",
            "properties": {
                "path": path_schema,
                "start_line": {
                    "type": "integer",
                    "description": "One-based first line to return.",
                    "minimum": 1,
                    "maximum": limits.max_start_line,
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of lines to return.",
                    "minimum": 1,
                    "maximum": limits.max_read_lines,
                },
            },
            "required": ["path", "start_line", "max_lines"],
            "additionalProperties": False,
        },
    )
    list_spec = ToolSpec(
        "list_files",
        "List bounded workspace entries recursively in deterministic path order.",
        ListFilesArguments,
        {
            "type": "object",
            "properties": {
                "path": path_schema,
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum recursive depth; zero lists direct children only.",
                    "minimum": 0,
                    "maximum": limits.max_depth,
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum number of entries returned.",
                    "minimum": 1,
                    "maximum": limits.max_directory_entries,
                },
            },
            "required": ["path", "max_depth", "max_entries"],
            "additionalProperties": False,
        },
    )
    search_spec = ToolSpec(
        "search_text",
        "Search for a literal string in bounded UTF-8 workspace files.",
        SearchTextArguments,
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Non-empty literal text; never interpreted as a regex.",
                    "minLength": 1,
                    "maxLength": limits.max_query_chars,
                },
                "path": path_schema,
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum recursive depth; zero searches direct files only.",
                    "minimum": 0,
                    "maximum": limits.max_depth,
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum number of candidate files read.",
                    "minimum": 1,
                    "maximum": limits.max_search_files,
                },
                "max_matches": {
                    "type": "integer",
                    "description": "Maximum number of literal matches returned.",
                    "minimum": 1,
                    "maximum": limits.max_matches,
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Whether matching preserves case.",
                },
                "include": {
                    "type": "array",
                    "description": "Optional include globs; empty means every safe file.",
                    "items": glob_item_schema,
                    "minItems": 0,
                    "maxItems": limits.max_glob_patterns,
                },
                "exclude": {
                    "type": "array",
                    "description": "Optional exclude globs applied after include globs.",
                    "items": glob_item_schema,
                    "minItems": 0,
                    "maxItems": limits.max_glob_patterns,
                },
            },
            "required": ["query", "path", "max_depth", "max_files", "max_matches"],
            "additionalProperties": False,
        },
    )
    return read_spec, list_spec, search_spec


def build_repository_tool_registry(
    workspace_root: str | Path,
    *,
    limits: RepositoryToolLimits | None = None,
) -> RepositoryToolRegistry:
    """为一个已存在的 workspace root 构建只读、未 seal 的 Registry。"""
    limits = limits or RepositoryToolLimits()
    if not isinstance(limits, RepositoryToolLimits):
        raise TypeError("limits must be RepositoryToolLimits")
    resolver = WorkspacePathResolver(
        workspace_root,
        hard_max_read_bytes=limits.max_file_bytes,
        hard_max_directory_scan_entries=limits.max_directory_scan_entries,
    )
    try:
        registry = RepositoryToolRegistry(resolver)
        register_repository_tools(registry, resolver, limits=limits)
        return registry
    except BaseException:
        resolver.close()
        raise


def register_repository_tools(
    registry: ToolRegistry,
    resolver: WorkspacePathResolver,
    *,
    limits: RepositoryToolLimits | None = None,
) -> None:
    """把 D3 三个工具接到调用方 Registry，供 D4 组合完整 Coding catalog。"""
    if not isinstance(registry, ToolRegistry):
        raise TypeError("registry must be ToolRegistry")
    if not isinstance(resolver, WorkspacePathResolver):
        raise TypeError("resolver must be WorkspacePathResolver")
    limits = limits or RepositoryToolLimits()
    if not isinstance(limits, RepositoryToolLimits):
        raise TypeError("limits must be RepositoryToolLimits")
    tools = _RepositoryTools(resolver, limits)
    read_spec, list_spec, search_spec = repository_tool_specs(limits)
    registry.register(read_spec, tools.read_file)
    registry.register(list_spec, tools.list_files)
    registry.register(search_spec, tools.search_text)


class RepositoryToolRegistry(ToolRegistry):
    """ToolRegistry 加 resolver 生命周期；长运行进程可显式释放 root handle。"""

    def __init__(self, resolver: WorkspacePathResolver) -> None:
        if not isinstance(resolver, WorkspacePathResolver):
            raise TypeError("resolver must be WorkspacePathResolver")
        super().__init__()
        self._workspace_resolver = resolver

    def close(self) -> None:
        self._workspace_resolver.close()

    def __enter__(self) -> RepositoryToolRegistry:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


@dataclass(slots=True)
class _WalkState:
    scanned_entries: int = 0
    truncated: bool = False
    reason: str | None = None
    root_path: str | None = None


class _RepositoryTools:
    def __init__(
        self,
        resolver: WorkspacePathResolver,
        limits: RepositoryToolLimits,
    ) -> None:
        self._resolver = resolver
        self._limits = limits

    def read_file(
        self,
        arguments: ReadFileArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del context
        if _is_control_path(arguments.path):
            return tool_error_result("repository_control_path_forbidden")
        try:
            file_read = self._resolver.read_bytes(
                arguments.path,
                max_bytes=self._limits.max_file_bytes,
            )
        except WorkspacePathError as error:
            return _workspace_error_result(error)

        decoded = _decode_text(file_read.data)
        if isinstance(decoded, ToolExecutionResult):
            return decoded
        start_index = arguments.start_line - 1
        stop_index = start_index + arguments.max_lines
        selected: list[str] = []
        total_lines = 0
        for line_index, line in enumerate(_iter_text_lines(decoded)):
            total_lines = line_index + 1
            if start_index <= line_index < stop_index:
                selected.append(line)
        content = "\n".join(selected)
        end_index = start_index + len(selected)
        end_line = arguments.start_line + len(selected) - 1 if selected else None
        line_truncated = start_index > 0 or end_index < total_lines
        payload: dict[str, Any] = {
            "byte_length": file_read.byte_length,
            "content": content,
            "end_line": end_line,
            "ok": True,
            "output_truncated": False,
            "path": file_read.path,
            "sha256": file_read.sha256,
            "start_line": arguments.start_line,
            "total_lines": total_lines,
            "truncated": line_truncated,
        }
        _fit_metadata_strings(
            payload,
            fields=("path",),
            max_chars=self._limits.max_output_chars,
            excluded_field="content",
        )
        return ToolExecutionResult(
            _bounded_string_payload(
                payload,
                field="content",
                max_chars=self._limits.max_output_chars,
            )
        )

    def list_files(
        self,
        arguments: ListFilesArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del context
        if _is_control_path(arguments.path):
            return tool_error_result("repository_control_path_forbidden")
        state = _WalkState()
        try:
            entries = self._walk(
                arguments.path,
                max_depth=arguments.max_depth,
                state=state,
            )
        except WorkspacePathError as error:
            return _workspace_error_result(error)

        documents: list[dict[str, Any]] = []
        result_limit_hit = False
        output_limit_hit = False
        base: dict[str, Any] = {
            "entries": documents,
            "ok": True,
            "path": state.root_path or arguments.path,
            "returned_entries": 0,
            "scanned_entries": state.scanned_entries,
            "truncated": state.truncated,
            "truncation_reason": state.reason,
        }
        _fit_metadata_strings(
            base,
            fields=("path",),
            max_chars=self._limits.max_output_chars,
        )
        for entry in entries:
            if len(documents) >= arguments.max_entries:
                result_limit_hit = True
                break
            item = _entry_document(entry)
            documents.append(item)
            base["returned_entries"] = len(documents)
            if len(_json(base)) > self._limits.max_output_chars:
                documents.pop()
                base["returned_entries"] = len(documents)
                output_limit_hit = True
                break
        if result_limit_hit:
            base["truncated"] = True
            base["truncation_reason"] = "entry_limit"
        elif output_limit_hit:
            base["truncated"] = True
            base["truncation_reason"] = "output_limit"
        while len(_json(base)) > self._limits.max_output_chars and documents:
            documents.pop()
            base["returned_entries"] = len(documents)
            base["truncated"] = True
            base["truncation_reason"] = "output_limit"
        _fit_metadata_strings(
            base,
            fields=("path",),
            max_chars=self._limits.max_output_chars,
        )
        content = _json(base)
        if len(content) > self._limits.max_output_chars:
            # limits 的最小值保证空 entries 结构不会到这里。
            raise RuntimeError("bounded list result invariant failed")
        return ToolExecutionResult(content)

    def search_text(
        self,
        arguments: SearchTextArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del context
        if _is_control_path(arguments.path):
            return tool_error_result("repository_control_path_forbidden")
        state = _WalkState()
        try:
            entries = self._walk(
                arguments.path,
                max_depth=arguments.max_depth,
                state=state,
            )
        except WorkspacePathError as error:
            return _workspace_error_result(error)

        matches: list[dict[str, Any]] = []
        scanned_files = 0
        scanned_bytes = 0
        skipped_binary = 0
        skipped_invalid_utf8 = 0
        skipped_too_large = 0
        truncated = state.truncated
        truncation_reason = state.reason

        for entry in entries:
            if (
                entry.kind is not WorkspaceEntryKind.FILE
                or _is_control_path(entry.path)
                or not _matches_patterns(
                entry.path,
                arguments.include,
                arguments.exclude,
                )
            ):
                continue
            if scanned_files >= arguments.max_files:
                truncated = True
                truncation_reason = "file_limit"
                break
            remaining_bytes = self._limits.max_search_total_bytes - scanned_bytes
            if remaining_bytes <= 0:
                truncated = True
                truncation_reason = "byte_limit"
                break
            scanned_files += 1
            per_file_limit = min(self._limits.max_file_bytes, remaining_bytes)
            try:
                file_read = self._resolver.read_bytes(
                    entry.path,
                    max_bytes=per_file_limit,
                )
            except WorkspacePathError as error:
                if error.code == "workspace_file_too_large":
                    if per_file_limit < self._limits.max_file_bytes:
                        truncated = True
                        truncation_reason = "byte_limit"
                        break
                    skipped_too_large += 1
                    continue
                return _workspace_error_result(error)

            scanned_bytes += file_read.byte_length
            decoded = _try_decode_text(file_read.data)
            if decoded is None:
                if b"\x00" in file_read.data:
                    skipped_binary += 1
                else:
                    skipped_invalid_utf8 += 1
                continue
            for line_number, line in enumerate(_iter_text_lines(decoded), 1):
                for column in _literal_columns(
                    line,
                    arguments.query,
                    case_sensitive=arguments.case_sensitive,
                    max_columns=arguments.max_matches - len(matches),
                ):
                    matches.append(
                        {
                            "column": column + 1,
                            "line": line_number,
                            "path": file_read.path,
                            "text": _bounded_match_text(
                                line,
                                column,
                                len(arguments.query),
                                self._limits.max_match_chars,
                            ),
                        }
                    )
                    if len(matches) >= arguments.max_matches:
                        truncated = True
                        truncation_reason = "match_limit"
                        break
                if len(matches) >= arguments.max_matches:
                    break
            if len(matches) >= arguments.max_matches:
                break

        matches.sort(key=lambda item: (item["path"], item["line"], item["column"]))
        payload: dict[str, Any] = {
            "matches": [],
            "ok": True,
            "path": state.root_path or arguments.path,
            "query": arguments.query,
            "returned_matches": 0,
            "scanned_bytes": scanned_bytes,
            "scanned_entries": state.scanned_entries,
            "scanned_files": scanned_files,
            "skipped_binary": skipped_binary,
            "skipped_invalid_utf8": skipped_invalid_utf8,
            "skipped_too_large": skipped_too_large,
            "truncated": truncated,
            "truncation_reason": truncation_reason,
        }
        _fit_metadata_strings(
            payload,
            fields=("path", "query"),
            max_chars=self._limits.max_output_chars,
        )
        output_matches: list[dict[str, Any]] = payload["matches"]
        for item in matches:
            output_matches.append(item)
            payload["returned_matches"] = len(output_matches)
            if len(_json(payload)) > self._limits.max_output_chars:
                output_matches.pop()
                payload["returned_matches"] = len(output_matches)
                payload["truncated"] = True
                payload["truncation_reason"] = "output_limit"
                break
        while len(_json(payload)) > self._limits.max_output_chars and output_matches:
            output_matches.pop()
            payload["returned_matches"] = len(output_matches)
            payload["truncated"] = True
            payload["truncation_reason"] = "output_limit"
        _fit_metadata_strings(
            payload,
            fields=("path", "query"),
            max_chars=self._limits.max_output_chars,
        )
        content = _json(payload)
        if len(content) > self._limits.max_output_chars:
            raise RuntimeError("bounded search result invariant failed")
        return ToolExecutionResult(content)

    def _walk(
        self,
        path: str,
        *,
        max_depth: int,
        state: _WalkState,
    ) -> list[WorkspaceDirectoryEntry]:
        """通过 resolver 做确定性 DFS；全局 scan budget 不按目录重置。"""
        pending: list[tuple[str, int]] = [(path, 0)]
        collected: list[WorkspaceDirectoryEntry] = []
        while pending:
            directory, depth = pending.pop()
            remaining = self._limits.max_directory_scan_entries - state.scanned_entries
            if remaining <= 0:
                state.truncated = True
                state.reason = "scan_limit"
                break
            listing = self._resolver.list_directory(
                directory,
                max_entries=remaining,
                max_scan_entries=remaining,
            )
            if state.root_path is None:
                state.root_path = listing.path
            state.scanned_entries += listing.total_entries
            if listing.truncated:
                state.truncated = True
                state.reason = "scan_limit"
            entries = sorted(listing.entries, key=lambda item: item.path)
            visible_entries = [
                entry for entry in entries if not _is_internal_patch_path(entry.path)
            ]
            collected.extend(visible_entries)
            directories: list[str] = []
            for entry in visible_entries:
                if (
                    entry.kind is WorkspaceEntryKind.DIRECTORY
                    and depth < max_depth
                    and not _is_control_path(entry.path)
                ):
                    directories.append(entry.path)
            if listing.truncated:
                break
            # stack 逆序入栈，保证字典序目录先访问。
            pending.extend((item, depth + 1) for item in reversed(directories))
        collected.sort(key=lambda item: item.path)
        return collected


def _entry_document(entry: WorkspaceDirectoryEntry) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": entry.kind.value, "path": entry.path}
    if entry.size is not None:
        result["byte_length"] = entry.size
    return result


def _workspace_error_result(error: WorkspacePathError) -> ToolExecutionResult:
    return tool_error_result(error.code)


def _decode_text(data: bytes) -> str | ToolExecutionResult:
    decoded = _try_decode_text(data)
    if decoded is not None:
        return decoded
    if b"\x00" in data:
        return tool_error_result("binary_file")
    return tool_error_result("invalid_utf8")


def _try_decode_text(data: bytes) -> str | None:
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return None


def _iter_text_lines(text: str) -> Iterator[str]:
    """按 ``str.splitlines()`` 的边界语义逐行产出，不物化百万行列表。"""
    boundaries = {"\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"}
    start = 0
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character not in boundaries:
            index += 1
            continue
        yield text[start:index]
        if character == "\r" and index + 1 < length and text[index + 1] == "\n":
            index += 1
        index += 1
        start = index
    if start < length:
        yield text[start:]


def _matches_patterns(
    path: str,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
) -> bool:
    normalized = path.replace("\\", "/")
    name = PurePosixPath(normalized).name

    def matches(pattern: str) -> bool:
        normalized_pattern = pattern.replace("\\", "/")
        return fnmatch.fnmatchcase(normalized, normalized_pattern) or (
            "/" not in normalized_pattern
            and fnmatch.fnmatchcase(name, normalized_pattern)
        )

    included = not include or any(matches(pattern) for pattern in include)
    return included and not any(matches(pattern) for pattern in exclude)


def _is_control_path(path: str) -> bool:
    """控制树正文与 D4 内部事务文件一律不可进入模型。"""
    normalized = path.replace("\\", "/")
    return _is_internal_patch_path(normalized) or any(
        component.casefold() in _DEFAULT_SKIPPED_DIRECTORIES
        for component in PurePosixPath(normalized).parts
    )


def _is_internal_patch_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        component.casefold().startswith(".koawa-patch-")
        for component in PurePosixPath(normalized).parts
    )


def _literal_columns(
    line: str,
    query: str,
    *,
    case_sensitive: bool,
    max_columns: int,
) -> list[int]:
    """最多返回 ``max_columns`` 个原字符串列号，不先收集整行全部命中。"""
    if max_columns <= 0:
        return []
    if case_sensitive:
        haystack = line
        needle = query
        columns: list[int] = []
        start = 0
        while len(columns) < max_columns:
            index = haystack.find(needle, start)
            if index < 0:
                break
            columns.append(index)
            start = index + 1
        return columns

    haystack = line.casefold()
    needle = query.casefold()
    if not needle:
        return []

    # 命中在 folded 字符串中单调前进；source cursor 也只向前扫一次，
    # 因此无需为整行建立 Python-int/array 索引表。
    columns: list[int] = []
    start = 0
    source_index = 0
    folded_source_start = 0
    folded_source_end = len(line[0].casefold()) if line else 0
    while len(columns) < max_columns:
        index = haystack.find(needle, start)
        if index < 0:
            break
        while source_index < len(line) and folded_source_end <= index:
            folded_source_start = folded_source_end
            source_index += 1
            if source_index < len(line):
                folded_source_end += len(line[source_index].casefold())
        if source_index >= len(line):
            break
        if folded_source_start <= index < folded_source_end:
            if not columns or columns[-1] != source_index:
                columns.append(source_index)
        start = index + 1
    return columns


def _bounded_match_text(line: str, column: int, query_length: int, limit: int) -> str:
    if len(line) <= limit:
        return line
    width = max(query_length, 1)
    left_room = max((limit - width) // 2, 0)
    start = max(column - left_room, 0)
    end = min(start + limit, len(line))
    start = max(end - limit, 0)
    return line[start:end]


def _bounded_string_payload(
    payload: dict[str, Any],
    *,
    field: str,
    max_chars: int,
) -> str:
    content = _json(payload)
    if len(content) <= max_chars:
        return content
    original = payload[field]
    payload["output_truncated"] = True
    payload["truncated"] = True
    low = 0
    high = len(original)
    while low < high:
        middle = (low + high + 1) // 2
        payload[field] = original[:middle]
        if len(_json(payload)) <= max_chars:
            low = middle
        else:
            high = middle - 1
    payload[field] = original[:low]
    result = _json(payload)
    if len(result) > max_chars:
        raise RuntimeError("bounded string result invariant failed")
    return result


def _fit_metadata_strings(
    payload: dict[str, Any],
    *,
    fields: tuple[str, ...],
    max_chars: int,
    excluded_field: str | None = None,
) -> None:
    """必要时显式截断回显元数据，不让长路径突破结果上限。"""
    excluded: Any = None
    if excluded_field is not None:
        excluded = payload[excluded_field]
        payload[excluded_field] = ""
    try:
        for field in fields:
            if len(_json(payload)) <= max_chars:
                return
            original = payload[field]
            marker = f"{field}_truncated"
            payload[marker] = True
            low = 0
            high = len(original)
            while low < high:
                middle = (low + high + 1) // 2
                payload[field] = original[:middle]
                if len(_json(payload)) <= max_chars:
                    low = middle
                else:
                    high = middle - 1
            payload[field] = original[:low]
        if len(_json(payload)) > max_chars:
            raise RuntimeError("bounded metadata result invariant failed")
    finally:
        if excluded_field is not None:
            payload[excluded_field] = excluded


def _json(document: Any) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
