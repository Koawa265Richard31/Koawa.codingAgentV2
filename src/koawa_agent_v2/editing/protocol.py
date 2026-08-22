"""D4 结构化 Patch 协议与纯内存精确应用。

Provider 只提交版本化 JSON 文档；本模块先把不可信文档完整解析成不可变类型，
再对 UPDATE hunk 做精确上下文匹配。这里不接触文件系统，因此协议错误永远发生在
第一个写副作用之前。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeAlias

from ..tools.errors import ToolConfigurationError


_SHA256 = re.compile(r"[0-9a-f]{64}")
_PATCH_LIMIT_CEILINGS = {
    "max_patch_json_chars": 1_000_000,
    "max_path_chars": 4_096,
    "max_files": 128,
    "max_hunks_per_file": 1_000,
    "max_lines_per_hunk": 10_000,
    "max_line_chars": 100_000,
    "max_file_bytes": 16 * 1024 * 1024,
    "max_file_lines": 1_000_000,
    "max_total_input_bytes": 64 * 1024 * 1024,
    "max_total_output_bytes": 64 * 1024 * 1024,
    "max_result_chars": 262_144,
}


class PatchError(Exception):
    """可安全跨工具边界传播的稳定 Patch 失败。"""

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
            raise ValueError("invalid patch error code")
        if detail is not None and not re.fullmatch(r"[a-z0-9_:-]{1,127}", detail):
            raise ValueError("invalid patch error detail")
        self.code = code
        self.detail = detail
        super().__init__(code)


class PatchOperation(StrEnum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"


class NewlineStyle(StrEnum):
    LF = "lf"
    CRLF = "crlf"


@dataclass(frozen=True, slots=True)
class PatchLimits:
    """Patch 文档、内存规划和模型可见结果共用的硬上限。"""

    max_patch_json_chars: int = 262_144
    max_path_chars: int = 1_024
    max_files: int = 32
    max_hunks_per_file: int = 128
    max_lines_per_hunk: int = 500
    max_line_chars: int = 10_000
    max_file_bytes: int = 1_000_000
    max_file_lines: int = 100_000
    max_total_input_bytes: int = 16 * 1024 * 1024
    max_total_output_bytes: int = 16 * 1024 * 1024
    max_result_chars: int = 100_000

    def __post_init__(self) -> None:
        for name, ceiling in _PATCH_LIMIT_CEILINGS.items():
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > ceiling
            ):
                raise ToolConfigurationError("invalid_patch_limits")
        if self.max_total_output_bytes < self.max_file_bytes:
            raise ToolConfigurationError("invalid_patch_limits")
        if self.max_total_input_bytes < self.max_file_bytes:
            raise ToolConfigurationError("invalid_patch_limits")
        if self.max_result_chars < 512:
            raise ToolConfigurationError("invalid_patch_limits")
        minimum_result = 512 + self.max_files * (2 * self.max_path_chars + 384)
        if self.max_result_chars < minimum_result:
            raise ToolConfigurationError("invalid_patch_limits")


@dataclass(frozen=True, slots=True, repr=False)
class PatchHunk:
    old_start: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            f"PatchHunk(old_start={self.old_start}, old_lines={len(self.old_lines)}, "
            f"new_lines={len(self.new_lines)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AddFileChange:
    operation: PatchOperation
    path: str
    content: str
    newline: NewlineStyle
    utf8_bom: bool

    def __repr__(self) -> str:
        return (
            f"AddFileChange(path={self.path!r}, content_length={len(self.content)}, "
            f"newline={self.newline.value!r}, utf8_bom={self.utf8_bom})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class UpdateFileChange:
    operation: PatchOperation
    path: str
    base_sha256: str
    hunks: tuple[PatchHunk, ...]

    def __repr__(self) -> str:
        return (
            f"UpdateFileChange(path={self.path!r}, base_sha256={self.base_sha256!r}, "
            f"hunks={len(self.hunks)})"
        )


@dataclass(frozen=True, slots=True)
class DeleteFileChange:
    operation: PatchOperation
    path: str
    base_sha256: str


FileChange: TypeAlias = AddFileChange | UpdateFileChange | DeleteFileChange


@dataclass(frozen=True, slots=True, repr=False)
class PatchSet:
    schema_version: int
    changes: tuple[FileChange, ...]
    document_sha256: str

    def __repr__(self) -> str:
        return (
            f"PatchSet(schema_version={self.schema_version}, changes={len(self.changes)}, "
            f"document_sha256={self.document_sha256!r})"
        )


@dataclass(frozen=True, slots=True)
class TextDocument:
    """已解码文本的格式事实；UPDATE 重建时必须全部保留。"""

    lines: tuple[str, ...]
    newline: str
    final_newline: bool
    utf8_bom: bool


class _DuplicateKey(ValueError):
    pass


def parse_patch_document(value: str, limits: PatchLimits | None = None) -> PatchSet:
    """严格解析一个完整 Patch JSON object，不接受重复键或宽松类型。"""
    limits = limits or PatchLimits()
    if not isinstance(limits, PatchLimits):
        raise TypeError("limits must be PatchLimits")
    if not isinstance(value, str) or not value:
        raise PatchError("invalid_patch_document")
    if len(value) > limits.max_patch_json_chars:
        raise PatchError("patch_document_too_large")
    _utf8(value, "invalid_patch_document")
    try:
        document = json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
        raise PatchError("invalid_patch_document") from None
    if not isinstance(document, dict) or set(document) != {"schema_version", "changes"}:
        raise PatchError("invalid_patch_document")
    version = document["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise PatchError("invalid_patch_document")
    if version != 1:
        raise PatchError("unsupported_patch_schema")
    raw_changes = document["changes"]
    if (
        not isinstance(raw_changes, list)
        or not raw_changes
        or len(raw_changes) > limits.max_files
    ):
        raise PatchError("patch_file_limit_exceeded")

    changes = tuple(_parse_change(item, limits) for item in raw_changes)
    path_keys: set[str] = set()
    for change in changes:
        key = change.path.casefold()
        if key in path_keys:
            raise PatchError("duplicate_patch_path")
        path_keys.add(key)
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return PatchSet(version, changes, hashlib.sha256(canonical).hexdigest())


def decode_text_document(
    data: bytes, *, max_lines: int = 1_000_000
) -> TextDocument:
    """只接受 UTF-8 文本，并识别需要由 UPDATE 保留的格式。"""
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if b"\x00" in data:
        raise PatchError("binary_file")
    bom = data.startswith(b"\xef\xbb\xbf")
    body = data[3:] if bom else data
    try:
        text = body.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise PatchError("unsupported_text_encoding") from None
    if "\x00" in text:
        raise PatchError("binary_file")
    without_crlf = text.replace("\r\n", "")
    if "\r" in without_crlf:
        raise PatchError("unsupported_line_endings")
    if "\r\n" in text and "\n" in without_crlf:
        raise PatchError("mixed_line_endings")
    newline = "\r\n" if "\r\n" in text else "\n"
    normalized = text.replace("\r\n", "\n")
    final_newline = normalized.endswith("\n")
    line_count = normalized.count("\n") + (0 if final_newline or not normalized else 1)
    if line_count > max_lines:
        raise PatchError("patch_file_line_limit_exceeded")
    if not normalized:
        lines: tuple[str, ...] = ()
    else:
        parts = normalized.split("\n")
        if final_newline:
            parts.pop()
        lines = tuple(parts)
    return TextDocument(lines, newline, final_newline, bom)


def encode_text_document(document: TextDocument) -> bytes:
    text = document.newline.join(document.lines)
    if document.final_newline:
        text += document.newline
    data = text.encode("utf-8", "strict")
    return (b"\xef\xbb\xbf" + data) if document.utf8_bom else data


def apply_update(
    before: bytes,
    change: UpdateFileChange,
    limits: PatchLimits,
) -> tuple[bytes, int, int]:
    """按原始行号精确应用 hunks；返回新字节及增加/删除行数。"""
    document = decode_text_document(before, max_lines=limits.max_file_lines)
    source = document.lines
    output: list[str] = []
    cursor = 0
    previous_start = 0
    additions = 0
    deletions = 0
    for hunk in change.hunks:
        if hunk.old_start <= previous_start:
            raise PatchError("overlapping_patch_hunks")
        previous_start = hunk.old_start
        start = hunk.old_start - 1
        end = start + len(hunk.old_lines)
        if start < cursor:
            raise PatchError("overlapping_patch_hunks")
        if start > len(source) or end > len(source):
            raise PatchError("patch_context_mismatch")
        if source[start:end] != hunk.old_lines:
            first_mismatch = start + next(
                (
                    index
                    for index, (current, expected_line) in enumerate(
                        zip(source[start:end], hunk.old_lines)
                    )
                    if current != expected_line
                ),
                -1,
            )
            detail = None
            if first_mismatch >= 0:
                detail = f"first_mismatch_line:{first_mismatch + 1}"
            raise PatchError("patch_context_mismatch", detail=detail)
        output.extend(source[cursor:start])
        output.extend(hunk.new_lines)
        cursor = end
        additions += len(hunk.new_lines)
        deletions += len(hunk.old_lines)
    output.extend(source[cursor:])
    after = encode_text_document(
        TextDocument(tuple(output), document.newline, document.final_newline, document.utf8_bom)
    )
    if after == before:
        raise PatchError("patch_no_changes")
    if len(after) > limits.max_file_bytes:
        raise PatchError("patched_file_too_large")
    return after, additions, deletions


def encode_add(change: AddFileChange, limits: PatchLimits) -> bytes:
    """ADD 的 content 使用 LF 作为协议换行，再按显式声明编码。"""
    if "\r" in change.content:
        raise PatchError("invalid_patch_newline")
    if change.content.startswith("\ufeff"):
        raise PatchError("invalid_patch_bom")
    line_count = change.content.count("\n") + (
        0 if change.content.endswith("\n") or not change.content else 1
    )
    if line_count > limits.max_file_lines:
        raise PatchError("patch_file_line_limit_exceeded")
    newline = "\r\n" if change.newline is NewlineStyle.CRLF else "\n"
    text = change.content.replace("\n", newline)
    data = text.encode("utf-8", "strict")
    if change.utf8_bom:
        data = b"\xef\xbb\xbf" + data
    if len(data) > limits.max_file_bytes:
        raise PatchError("patched_file_too_large")
    return data


def _parse_change(raw: Any, limits: PatchLimits) -> FileChange:
    if not isinstance(raw, dict) or not isinstance(raw.get("operation"), str):
        raise PatchError("invalid_patch_change")
    try:
        operation = PatchOperation(raw["operation"])
    except ValueError:
        raise PatchError("invalid_patch_operation") from None
    path = raw.get("path")
    if not isinstance(path, str) or not path or len(path) > limits.max_path_chars:
        raise PatchError("invalid_patch_change")
    _utf8(path, "invalid_patch_change")

    if operation is PatchOperation.ADD:
        expected_fields = {"operation", "path", "content", "newline", "utf8_bom"}
        actual_fields = set(raw)
        if actual_fields != expected_fields:
            missing = sorted(expected_fields - actual_fields)
            extra = sorted(actual_fields - expected_fields)
            detail = None
            if missing:
                detail = "missing_field:" + missing[0]
            elif extra:
                detail = "unexpected_field:" + extra[0]
            raise PatchError("invalid_patch_change", detail=detail)
        content = raw["content"]
        if not isinstance(content, str) or "\x00" in content:
            raise PatchError("invalid_patch_change")
        _utf8(content, "invalid_patch_change")
        try:
            newline = NewlineStyle(raw["newline"])
        except (TypeError, ValueError):
            raise PatchError("invalid_patch_newline") from None
        if not isinstance(raw["utf8_bom"], bool):
            raise PatchError("invalid_patch_change")
        return AddFileChange(operation, path, content, newline, raw["utf8_bom"])

    if operation is PatchOperation.DELETE:
        if set(raw) != {"operation", "path", "base_sha256"}:
            raise PatchError("invalid_patch_change")
        return DeleteFileChange(operation, path, _sha(raw["base_sha256"]))

    if set(raw) != {"operation", "path", "base_sha256", "hunks"}:
        raise PatchError("invalid_patch_change")
    hunks = raw["hunks"]
    if (
        not isinstance(hunks, list)
        or not hunks
        or len(hunks) > limits.max_hunks_per_file
    ):
        raise PatchError("patch_hunk_limit_exceeded")
    return UpdateFileChange(
        operation,
        path,
        _sha(raw["base_sha256"]),
        tuple(_parse_hunk(item, limits) for item in hunks),
    )


def _parse_hunk(raw: Any, limits: PatchLimits) -> PatchHunk:
    if not isinstance(raw, dict) or set(raw) != {"old_start", "old_lines", "new_lines"}:
        raise PatchError("invalid_patch_hunk")
    start = raw["old_start"]
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        raise PatchError("invalid_patch_hunk")
    old_lines = _lines(raw["old_lines"], limits)
    new_lines = _lines(raw["new_lines"], limits)
    if not old_lines and not new_lines:
        raise PatchError("invalid_patch_hunk")
    return PatchHunk(start, old_lines, new_lines)


def _lines(raw: Any, limits: PatchLimits) -> tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) > limits.max_lines_per_hunk:
        raise PatchError("patch_hunk_limit_exceeded")
    result: list[str] = []
    for line in raw:
        if (
            not isinstance(line, str)
            or len(line) > limits.max_line_chars
            or "\n" in line
            or "\r" in line
            or "\x00" in line
        ):
            raise PatchError("invalid_patch_hunk")
        _utf8(line, "invalid_patch_hunk")
        result.append(line)
    return tuple(result)


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PatchError("invalid_base_sha256")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey()
        result[key] = value
    return result


def _utf8(value: str, code: str) -> None:
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise PatchError(code) from None
