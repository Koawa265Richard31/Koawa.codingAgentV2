"""把 agent worktree 相对 base commit 的变更导出成 apply_patch 文档。

本模块是 merge 的只读前半程：``export_worktree_patch`` 在 hermetic git 与受限
子进程下枚举源 worktree 相对 ``base_commit`` 的 A/M/D 变更，编码成
``editing/protocol`` 形状的 patch JSON 并绑定规范摘要；
``validate_merge_request`` 在候选根目录上复核同一份 :class:`MergeRequest`。
这里不落库、不写文件、不执行 apply。

有意失败闭合的决策（与 patch 协议的表达能力对齐）：

- 重命名/拷贝/未合并等 porcelain 状态无法用 add/update/delete 表达
  → ``merge_rename_unsupported``。
- 二进制、非 UTF-8、孤立 CR 或混合换行的内容无法进入 patch 文档
  → ``merge_binary_file_unsupported`` / ``merge_text_format_unsupported``。
- ``export_worktree_patch`` 的 update/delete ``base_sha256`` 取自 base_commit
  中 blob 的原始字节：候选目录在这些路径上必须是 pristine 状态；应用前由
  editing/transaction 按"目标文件当前字节"重新校验，漂移即
  ``stale_patch_base``。update 的 ``old_lines`` 同样来自 base blob 的解码行，
  与候选目录的解码行逐字符一致。``export_patch`` 是同一导出的 F8 契约入口
  （无 UUID/限额参数），update 的 ``base_sha256`` 按 worktree 文件当前磁盘
  字节锚定（transaction.py:244 应用时按目标文件当前字节重校验）。
- 仅行尾风格/BOM/末行换行差异的文件在解码后行相等，导出为零变化并剔除，避免
  应用时抛 ``patch_no_changes``；行尾与 BOM 事实由 apply 端从目标文件保留。
- ``git rev-parse HEAD`` 与 base_commit 不一致（源 worktree 有过提交）时，status
  列出的"相对 HEAD"变更不再是相对 base 的变更 → ``merge_base_commit_mismatch``。
- 源 worktree 完全干净时没有可合并内容 → ``merge_no_changes``，而不是返回一份
  连自身 validate 都拒绝的空文档。

导出端不拆分 hunk：每个 hunk 是 difflib 的一个连续差异区，行数/行宽/hunk 数
上限由应用方的 ``PatchLimits`` 承担；超出 :class:`MergeLimits` 的大变更必须由
调用方切成多份文档。``max_total_bytes`` 按"实际读取的每个字节"计（含 update
与 delete 的基线 blob），读超即 ``merge_too_large``。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable
from uuid import UUID

from ..agents.graph import AgentError
from ..editing.protocol import PatchError, TextDocument, decode_text_document
from .subprocesses import run_bounded


_STABLE_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_BASE_COMMIT = re.compile(r"[0-9a-f]{7,64}")
# 与 editing/protocol.py:20 相同的 base_sha256 形状；校验端按协议复核。
_SHA256 = re.compile(r"[0-9a-f]{64}")
# porcelain XY 里允许出现的字母；R/C/U/T/S/! 等一律 merge_rename_unsupported。
_ALLOWED_STATUS_LETTERS = frozenset(" AMD")
_STATUS_OUTPUT_LIMIT = 1_000_000
_PROBE_OUTPUT_LIMIT = 4_096
_GIT_TIMEOUT_SECONDS = 60.0


class MergeError(RuntimeError):
    """可安全跨边界传播的稳定 merge 失败码。"""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _STABLE_CODE.fullmatch(code):
            raise ValueError("invalid merge error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MergeLimits:
    """单次导出/校验的字节与文件数硬上限。"""

    max_files: int = 64
    max_total_bytes: int = 16 * 1024 * 1024
    max_patch_json_chars: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in ("max_files", "max_total_bytes", "max_patch_json_chars"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise MergeError("invalid_merge_limits")


@dataclass(frozen=True, slots=True)
class MergeRequest:
    """一次待合并导出的不可变载体；patch_json 即完整 apply_patch 文档。"""

    source_agent_id: UUID
    run_id: UUID
    base_commit: str
    patch_digest: str
    patch_json: str


def canonical_patch_digest(document: dict) -> str:
    """对 patch 文档做排序键紧凑序列化后取 sha256 十六进制摘要。"""
    canonical = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def export_worktree_patch(
    *,
    worktree: Path,
    base_commit: str,
    source_agent_id: UUID,
    run_id: UUID,
    limits: MergeLimits | None = None,
) -> MergeRequest:
    """导出 worktree 相对 base_commit 的补丁，并绑定来源身份。

    F8 合并边界：子代理只改自己的工作区、不提交；父级凭这份请求决定是否合入
    候选。导出与校验共用 ``editing/protocol.py`` 的文档形状。
    """
    if not isinstance(source_agent_id, UUID) or not isinstance(run_id, UUID):
        raise TypeError("source_agent_id and run_id must be UUID")
    document, patch_json = _export_document(
        worktree=worktree,
        base_commit=base_commit,
        limits=limits or MergeLimits(),
    )
    return MergeRequest(
        source_agent_id=source_agent_id,
        run_id=run_id,
        base_commit=base_commit,
        patch_digest=canonical_patch_digest(document),
        patch_json=patch_json,
    )


@dataclass(frozen=True, slots=True)
class MergeExport:
    """导出结果：完整 patch 文档 JSON 与其规范摘要。"""

    patch_json: str
    digest: str


def export_patch(*, worktree_path: Path, base_commit: str) -> MergeExport:
    """只导出文档与摘要，不绑定来源身份（工具与测试使用）。

    与 :func:`export_worktree_patch` 共用 ``_export_document``；两者唯一的区别
    是是否携带 source_agent_id / run_id。
    """
    document, patch_json = _export_document(
        worktree=worktree_path,
        base_commit=base_commit,
        limits=MergeLimits(),
    )
    return MergeExport(
        patch_json=patch_json, digest=canonical_patch_digest(document)
    )


def _export_document(
    *,
    worktree: Path,
    base_commit: str,
    limits: MergeLimits,
) -> tuple[dict[str, Any], str]:
    """把 worktree 相对 base 的 A/M/D 变更导出成一份 patch 文档。

    ``base_sha256`` 取 **base blob** 的摘要：应用端（transaction.py:243）是拿
    候选区里现存的旧文件比对，不是拿子代理改完的新文件。这条曾经在两份重复
    实现里分叉过一次（其中一份写成工作区字节，会让每次 update 都
    ``stale_patch_base``），不要改回去。
    """
    if not isinstance(limits, MergeLimits):
        raise TypeError("limits must be MergeLimits")
    if not isinstance(base_commit, str) or not _BASE_COMMIT.fullmatch(base_commit):
        raise MergeError("invalid_merge_base_commit")
    try:
        root = Path(worktree).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        raise MergeError("merge_worktree_unavailable") from None
    if not root.is_dir():
        raise MergeError("merge_worktree_unavailable")
    git_binary = _locate_git()
    _verify_head(git_binary, root, base_commit)
    entries = _status_entries(git_binary, root)

    changes: list[dict[str, Any]] = []
    spent_bytes = 0
    seen_paths: set[str] = set()
    for status, path in entries:
        kind = _classify_change(status)
        if kind is None:
            continue
        path_key = path.casefold()
        if path_key in seen_paths:
            raise MergeError("merge_status_invalid")
        seen_paths.add(path_key)
        if len(changes) >= limits.max_files:
            raise MergeError("merge_too_large")
        target = _entry_target(root, path)
        if kind == "delete":
            blob, spent_bytes = _read_base_blob(
                git_binary, root, base_commit, path, limits, spent_bytes
            )
            _decode_content(blob)
            changes.append({
                "operation": "delete", "path": path,
                "base_sha256": hashlib.sha256(blob).hexdigest(),
            })
            continue
        data, spent_bytes = _read_worktree_file(target, limits, spent_bytes)
        text_doc = _decode_content(data)
        if kind == "add":
            content = "\n".join(text_doc.lines)
            if text_doc.final_newline:
                content += "\n"
            changes.append({
                "operation": "add", "path": path, "content": content,
                "newline": "lf" if text_doc.newline == "\n" else "crlf",
                "utf8_bom": text_doc.utf8_bom,
            })
            continue
        blob, spent_bytes = _read_base_blob(
            git_binary, root, base_commit, path, limits, spent_bytes
        )
        base_document = _decode_content(blob)
        hunks = _build_hunks(base_document.lines, text_doc.lines)
        if hunks is None:
            # 解码后行相等：只有行尾风格/BOM/末行换行差异，应用会抛
            # patch_no_changes，按约定在导出前剔除。
            continue
        changes.append({
            "operation": "update", "path": path,
            "base_sha256": hashlib.sha256(blob).hexdigest(), "hunks": hunks,
        })

    if not changes:
        raise MergeError("merge_no_changes")
    changes.sort(key=lambda change: change["path"])
    export = {"schema_version": 1, "changes": changes}
    patch_json = json.dumps(
        export, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(patch_json) > limits.max_patch_json_chars:
        raise MergeError("merge_too_large")
    return export, patch_json


def validate_merge_request(
    request: MergeRequest,
    *,
    candidate_root: Path,
    protected_patterns: tuple[str, ...] = (),
    limits: MergeLimits | None = None,
) -> None:
    """在候选根目录上复核一份 MergeRequest；任一条件不满足即抛 MergeError。

    依次校验：patch_json 长度不超过 ``max_patch_json_chars``（请求不可信，导出
    端的上限在这里重放）、patch_json 可解析、规范摘要一致、schema_version==1
    且 changes 非空、每个路径折叠成仓库规范形后不重复（casefold，与协议去重
    一致；``./a``、``a\\b`` 之类别名写法折叠后与已有路径相同视作重复）、每个
    路径都落在 candidate_root 内（拒绝盘符、根与 ".."）、不命中
    protected_patterns（"/" 结尾按目录前缀、含 "/" 按精确匹配、裸词按文件名
    前缀；统一 casefold 比较，作用在折叠后的路径上）、文件数与内容字节数不超
    限。每个 change 先按 editing/protocol 的精确字段集与类型复核（未知
    operation、缺失/多余字段、计费字段类型不对都是 ``invalid_merge_patch``），
    字节数按其新内容估计：add 计 content 的 UTF-8 字节，update 计每个 hunk
    old_lines 与 new_lines 的 Σ(行字节+1)，delete 计 0——所有字符串字段要么
    被计费、要么被拒，巨型载荷无处藏身。
    """
    limits = limits or MergeLimits()
    if not isinstance(request, MergeRequest):
        raise TypeError("request must be MergeRequest")
    if not isinstance(limits, MergeLimits):
        raise TypeError("limits must be MergeLimits")
    if len(request.patch_json) > limits.max_patch_json_chars:
        raise MergeError("merge_too_large")
    try:
        document = json.loads(request.patch_json)
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError, TypeError):
        raise MergeError("invalid_merge_patch") from None
    if not isinstance(document, dict) or set(document) != {"schema_version", "changes"}:
        raise MergeError("invalid_merge_patch")
    try:
        computed_digest = canonical_patch_digest(document)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        # 孤立 surrogate 等无法 UTF-8 编码的载荷算不出规范摘要：失败闭合为
        # 文档无效，而不是泄漏 UnicodeEncodeError。
        raise MergeError("invalid_merge_patch") from None
    if computed_digest != request.patch_digest:
        raise MergeError("merge_digest_mismatch")
    version = document["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise MergeError("invalid_merge_patch")
    changes = document["changes"]
    if not isinstance(changes, list) or not changes:
        raise MergeError("invalid_merge_patch")

    seen_paths: set[str] = set()
    paths: list[str] = []
    change_bytes: list[int] = []
    for change in changes:
        if not isinstance(change, dict):
            raise MergeError("invalid_merge_patch")
        raw_path = change.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise MergeError("invalid_merge_patch")
        path = _canonical_merge_path(raw_path)
        path_key = path.casefold()
        if path_key in seen_paths:
            # 别名写法折叠后与已有路径相同：同一文件的重复提交。
            raise MergeError("invalid_merge_patch")
        seen_paths.add(path_key)
        paths.append(path)
        # 结构校验与字节估计在同一处完成：未知 operation、被忽略字段里的巨型
        # 载荷、update 的巨大 old_lines 都无法再绕过 max_total_bytes 的重放。
        change_bytes.append(_validated_change_bytes(change))

    try:
        root = Path(candidate_root).resolve()
    except (OSError, TypeError, ValueError):
        raise MergeError("merge_path_outside_candidate") from None
    for path in paths:
        try:
            target = root.joinpath(*path.split("/")).resolve()
            target.relative_to(root)
        except (OSError, ValueError):
            raise MergeError("merge_path_outside_candidate") from None

    for path in paths:
        if _path_protected(path, protected_patterns):
            raise MergeError("merge_path_protected")

    if len(changes) > limits.max_files:
        raise MergeError("merge_too_large")
    total_bytes = 0
    for size in change_bytes:
        total_bytes += size
        if total_bytes > limits.max_total_bytes:
            raise MergeError("merge_too_large")


def _locate_git() -> str:
    """解析可用的 git 可执行文件；与 WorktreeManager 同样的可用性判据。"""
    candidate = shutil.which("git")
    if not candidate:
        raise MergeError("merge_git_unavailable")
    try:
        resolved = Path(candidate).resolve()
    except OSError:
        raise MergeError("merge_git_unavailable") from None
    if not resolved.is_file():
        raise MergeError("merge_git_unavailable")
    return str(resolved)


def _run_git(
    git_binary: str,
    root: Path,
    arguments: tuple[str, ...],
    *,
    output_limit: int,
    output_limit_code: str,
) -> bytes:
    """在 hermetic 配置下执行一条只读 git 命令；exit 0 才返回 stdout。

    参数前缀合并 verification/git.py（safe.directory、hooks/fsmonitor/pager/
    external diff/submodule/autocrlf 全关）与 workspace/worktree.py（GIT_* 环境
    隔离、LC_ALL=C、GIT_OPTIONAL_LOCKS=0）两个照抄模板，经 run_bounded 的
    Popen(shell=False) 执行。
    """
    argv = (
        git_binary,
        # hermetic 环境丢弃用户 gitconfig；Windows 上他人账户拥有的仓库会全部
        # 报 dubious ownership，只对显式配置的 root 自授权（git.py:287-293）。
        "-c", f"safe.directory={root}",
        "-c", "core.hooksPath=" + os.devnull,
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", "core.pager=cat",
        "-c", "pager.status=false",
        "-c", "pager.diff=false",
        "-c", "color.ui=false",
        "-c", "diff.external=",
        "-c", "interactive.diffFilter=",
        "-c", "submodule.recurse=false",
        # 无全局配置时 autocrlf 默认 false，Windows CRLF 检出会把整文件报成
        # 幻影改动（git.py:304-311）。
        "-c", "core.autocrlf=true",
        "-C", str(root),
    ) + tuple(arguments)
    environment = {
        "PATH": str(Path(git_binary).parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_EXTERNAL_DIFF": "",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    }
    try:
        result = run_bounded(
            argv, cwd=root, environment=environment, timeout=_GIT_TIMEOUT_SECONDS,
            output_limit=output_limit, failure_code="merge_git_failed",
            output_limit_code=output_limit_code,
        )
    except AgentError as error:
        if getattr(error, "code", "") == "merge_too_large":
            raise MergeError("merge_too_large") from None
        raise MergeError("merge_git_failed") from None
    if result.returncode != 0:
        raise MergeError("merge_git_failed")
    return result.stdout


def _verify_head(git_binary: str, root: Path, base_commit: str) -> None:
    """确认 HEAD 与 base_commit 同一提交，status 的"相对 HEAD"才等于相对 base。"""
    head = _run_git(
        git_binary, root, ("rev-parse", "HEAD"),
        output_limit=_PROBE_OUTPUT_LIMIT, output_limit_code="merge_git_failed",
    ).strip()
    resolved = _run_git(
        git_binary, root, ("rev-parse", base_commit),
        output_limit=_PROBE_OUTPUT_LIMIT, output_limit_code="merge_git_failed",
    ).strip()
    if head != resolved:
        raise MergeError("merge_base_commit_mismatch")


def _status_entries(git_binary: str, root: Path) -> list[tuple[str, str]]:
    """解析 porcelain v1 -z 输出为 (XY 状态, 归一化 posix 路径) 列表。"""
    raw = _run_git(
        git_binary, root, ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        output_limit=_STATUS_OUTPUT_LIMIT, output_limit_code="merge_too_large",
    )
    fields = raw.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        if len(field) < 4 or field[2:3] != b" ":
            raise MergeError("merge_status_invalid")
        try:
            status = field[:2].decode("ascii", "strict")
            path = field[3:].decode("utf-8", "strict").replace("\\", "/")
        except UnicodeError:
            raise MergeError("merge_status_invalid") from None
        if not path or "\x00" in path:
            raise MergeError("merge_status_invalid")
        index += 1
        if "R" in status or "C" in status:
            # -z 下 R/C 各占两个字段（新路径在前，旧路径在后，git.py:435-443）。
            # 协议没有 rename 操作，补齐第二个字段后按约定失败闭合。
            if index >= len(fields):
                raise MergeError("merge_status_invalid")
            try:
                fields[index].decode("utf-8", "strict")
            except UnicodeError:
                raise MergeError("merge_status_invalid") from None
            index += 1
            raise MergeError("merge_rename_unsupported")
        entries.append((status, path))
    return entries


def _classify_change(status: str) -> str | None:
    """porcelain 状态映射为 add/update/delete；None 表示相对 base 无净变化。

    索引新增后又从工作区删除（"AD"）净效果与 base 相同，返回 None。出现协议
    无法表达的 R/C/U/T/S/! 等状态一律 ``merge_rename_unsupported``。
    """
    if status == "??":
        return "add"
    if any(letter not in _ALLOWED_STATUS_LETTERS for letter in status[:2]):
        raise MergeError("merge_rename_unsupported")
    index_status, worktree_status = status[0], status[1]
    if index_status == "D" or worktree_status == "D":
        return "delete" if index_status != "A" else None
    if index_status == "A":
        return "add"
    return "update"


def _entry_target(root: Path, path: str) -> Path:
    """校验 status 路径是安全的 worktree 内相对路径并返回其绝对路径。"""
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts:
        raise MergeError("merge_status_invalid")
    try:
        target = root.joinpath(*posix.parts).resolve()
        target.relative_to(root)
    except (OSError, ValueError):
        raise MergeError("merge_status_invalid") from None
    return target


def _read_worktree_file(
    target: Path, limits: MergeLimits, spent_bytes: int
) -> tuple[bytes, int]:
    """读取工作区文件，预算不足或超预算即 ``merge_too_large``。"""
    remaining = limits.max_total_bytes - spent_bytes
    if remaining <= 0:
        raise MergeError("merge_too_large")
    try:
        if target.stat().st_size > remaining:
            raise MergeError("merge_too_large")
        data = target.read_bytes()
    except (OSError, ValueError):
        raise MergeError("merge_status_invalid") from None
    if len(data) > remaining:
        raise MergeError("merge_too_large")
    return data, spent_bytes + len(data)


def _read_base_blob(
    git_binary: str, root: Path, base_commit: str, path: str,
    limits: MergeLimits, spent_bytes: int,
) -> tuple[bytes, int]:
    """读取 base_commit 中该路径的 blob 原始字节，计入字节预算。"""
    remaining = limits.max_total_bytes - spent_bytes
    if remaining <= 0:
        raise MergeError("merge_too_large")
    data = _run_git(
        git_binary, root, ("cat-file", "blob", f"{base_commit}:{path}"),
        output_limit=remaining + 1, output_limit_code="merge_too_large",
    )
    return data, spent_bytes + len(data)


def _decode_content(data: bytes) -> TextDocument:
    """按 patch 协议的文本判据解码；不可表达的内容失败闭合。"""
    try:
        return decode_text_document(data)
    except PatchError as error:
        if error.code == "binary_file":
            raise MergeError("merge_binary_file_unsupported") from None
        raise MergeError("merge_text_format_unsupported") from None


def _build_hunks(
    old_lines: tuple[str, ...], new_lines: tuple[str, ...]
) -> list[dict[str, Any]] | None:
    """把两份解码行的差异编码为严格递增、不重叠的 hunk 列表。

    SequenceMatcher 的相邻 opcode 之间必有 equal 分隔，故每个非 equal opcode
    即一个合法 hunk；不拆分大区域。零差异返回 None。导出后做一次自检：把
    hunks 重放到 old_lines 上必须逐行得到 new_lines。
    """
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    hunks: list[dict[str, Any]] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        hunks.append({
            "old_start": old_start + 1,
            "old_lines": list(old_lines[old_start:old_end]),
            "new_lines": list(new_lines[new_start:new_end]),
        })
    if not hunks:
        return None
    output: list[str] = []
    cursor = 0
    for hunk in hunks:
        start = hunk["old_start"] - 1
        if start < cursor or start > len(old_lines):
            raise MergeError("merge_export_selfcheck_failed")
        output.extend(old_lines[cursor:start])
        output.extend(hunk["new_lines"])
        cursor = start + len(hunk["old_lines"])
    output.extend(old_lines[cursor:])
    if output != list(new_lines):
        raise MergeError("merge_export_selfcheck_failed")
    return hunks


def _canonical_merge_path(value: str) -> str:
    """把补丁路径折叠成仓库规范形并拒绝不成文件的写法。

    规则与仓库路径边界一致（tools/workspace.py 的 ``_relative_parts`` /
    ``_validate_component``）：``\\`` 与 ``/`` 同为分隔符，空分量与 ``.`` 分量
    折叠；盘符、UNC、根、``..`` 分量、控制字符、分量尾点/尾空格、``:`` 一律
    拒绝。去重、候选根包含检查与保护模式匹配都作用在折叠结果上，否则
    ``./tests/x``、``tests\\x``、``src//x`` 这类别名写法会绕过它们。

    ``..`` 与根/盘符指向候选根之外，报 ``merge_path_outside_candidate``；
    ``""``、``"."``、``"./"`` 这类折叠后指向根本身、以及控制字符/尾点等不成
    文件的写法报 ``invalid_merge_patch``。
    """
    windows = PureWindowsPath(value)
    if windows.drive or windows.root:
        raise MergeError("merge_path_outside_candidate")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MergeError("invalid_merge_patch")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise MergeError("invalid_merge_patch") from None
    parts: list[str] = []
    for component in re.split(r"[\\/]+", value):
        if component in ("", "."):
            continue
        if component == "..":
            raise MergeError("merge_path_outside_candidate")
        if component.endswith((".", " ")) or ":" in component:
            raise MergeError("invalid_merge_patch")
        parts.append(component)
    if not parts:
        raise MergeError("invalid_merge_patch")
    return "/".join(parts)


def _path_protected(path: str, patterns: Iterable[str]) -> bool:
    """保护模式匹配："/" 结尾按目录前缀、含 "/" 按精确匹配、裸词按文件名前缀。

    全部比较统一 casefold（与协议的路径去重和 Windows 大小写不敏感一致）。
    空模式按裸词空前缀处理，会保护一切路径——保持字面语义，由调用方负责传入
    有意义的模式。
    """
    key = path.casefold()
    name = PurePosixPath(path).name.casefold()
    for pattern in patterns:
        if not isinstance(pattern, str):
            raise TypeError("protected_patterns must be strings")
        folded = pattern.casefold()
        if folded.endswith("/"):
            if key.startswith(folded):
                return True
        elif "/" in folded:
            if key == folded:
                return True
        elif name.startswith(folded):
            return True
    return False


_ADD_FIELDS = frozenset({"operation", "path", "content", "newline", "utf8_bom"})
_DELETE_FIELDS = frozenset({"operation", "path", "base_sha256"})
_UPDATE_FIELDS = frozenset({"operation", "path", "base_sha256", "hunks"})
_HUNK_FIELDS = frozenset({"old_start", "old_lines", "new_lines"})


def _validated_change_bytes(change: dict[str, Any]) -> int:
    """按 editing/protocol 的精确字段集与类型复核一个 change，返回其新内容
    字节估计；任何结构偏差都是 ``invalid_merge_patch``，没有"计 0 放行"分支。

    与协议的对应：字段集见 protocol.py:370/386/392，``base_sha256`` 形状见
    protocol.py:454，hunk 见 protocol.py:416-429（old_start 为非 bool 的正
    int，行是不含 ``\\n``/``\\r``/``\\x00`` 的 str，且 hunk 必须真正改行）。
    行宽/hunk 数等 PatchLimits 上限仍由应用端承担，这里只钉死字节估计依赖的
    结构：add 计 content，update 把 old_lines 与 new_lines 都计入——只计
    new_lines 时巨型载荷可藏进 old_lines 绕过 max_total_bytes。孤立 surrogate
    已在规范摘要一步被拒绝，这里无需重复 UTF-8 检查。
    """
    operation = change.get("operation")
    if operation == "add":
        if set(change) != _ADD_FIELDS:
            raise MergeError("invalid_merge_patch")
        content = change["content"]
        if not isinstance(content, str) or "\x00" in content:
            raise MergeError("invalid_merge_patch")
        if change["newline"] not in ("lf", "crlf") or not isinstance(
            change["utf8_bom"], bool
        ):
            raise MergeError("invalid_merge_patch")
        return len(content.encode("utf-8"))
    if not isinstance(operation, str) or operation not in ("update", "delete"):
        raise MergeError("invalid_merge_patch")
    if set(change) != (_DELETE_FIELDS if operation == "delete" else _UPDATE_FIELDS):
        raise MergeError("invalid_merge_patch")
    base_sha256 = change["base_sha256"]
    if not isinstance(base_sha256, str) or not _SHA256.fullmatch(base_sha256):
        raise MergeError("invalid_merge_patch")
    if operation == "delete":
        return 0
    hunks = change["hunks"]
    if not isinstance(hunks, list) or not hunks:
        raise MergeError("invalid_merge_patch")
    total = 0
    for hunk in hunks:
        if not isinstance(hunk, dict) or set(hunk) != _HUNK_FIELDS:
            raise MergeError("invalid_merge_patch")
        old_start = hunk["old_start"]
        if not isinstance(old_start, int) or isinstance(old_start, bool) or old_start < 1:
            raise MergeError("invalid_merge_patch")
        line_counts: list[int] = []
        for field in ("old_lines", "new_lines"):
            lines = hunk[field]
            if not isinstance(lines, list):
                raise MergeError("invalid_merge_patch")
            count = 0
            for line in lines:
                if (
                    not isinstance(line, str)
                    or "\n" in line
                    or "\r" in line
                    or "\x00" in line
                ):
                    raise MergeError("invalid_merge_patch")
                count += len(line.encode("utf-8")) + 1
            line_counts.append(count)
        if not hunk["old_lines"] and not hunk["new_lines"]:
            raise MergeError("invalid_merge_patch")
        total += sum(line_counts)
    return total


__all__ = [
    "MergeError",
    "MergeExport",
    "MergeLimits",
    "MergeRequest",
    "canonical_patch_digest",
    "export_patch",
    "export_worktree_patch",
    "validate_merge_request",
]
