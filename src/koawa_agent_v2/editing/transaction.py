"""D4 工作区 Patch 两阶段事务。

本模块把纯内存 ``PatchSet`` 应用到真实 workspace：先在持有 mutation lock 时
读取并规划全部目标，再将新内容写入同目录临时文件，最后逐项复核 base/identity 后
替换。普通进程内失败会反向回滚；无法证明回滚成功时必须返回
``workspace_outcome_unknown``，绝不伪装成普通 FAILED。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import (
    AddFileChange,
    DeleteFileChange,
    FileChange,
    PatchError,
    PatchLimits,
    PatchOperation,
    PatchSet,
    UpdateFileChange,
    apply_update,
    decode_text_document,
    encode_add,
)
from ..tools.workspace import (
    WorkspaceFileRead,
    WorkspacePathError,
    WorkspacePathResolver,
    _relative_parts,
)


ProgressGuard = Callable[[], None]
FaultInjector = Callable[[str, str], None]
_CONTROL_NAMES = frozenset({".git", ".hg", ".svn", "__pycache__"})
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class PatchFileResult:
    operation: PatchOperation
    path: str
    before_sha256: str | None
    after_sha256: str | None
    additions: int
    deletions: int
    diff: str


@dataclass(frozen=True, slots=True)
class PatchTransactionResult:
    patch_sha256: str
    files: tuple[PatchFileResult, ...]

    def to_tool_content(self, *, max_chars: int) -> str:
        """生成确定性、有界 JSON；超大 diff 被显式截断而非静默丢失。"""
        changes = [
            {
                "additions": item.additions,
                "after_sha256": item.after_sha256,
                "before_sha256": item.before_sha256,
                "deletions": item.deletions,
                "operation": item.operation.value,
                "path": item.path,
            }
            for item in self.files
        ]
        full_diff = "".join(item.diff for item in self.files)
        base: dict[str, Any] = {
            "changed_files": len(changes),
            "changes": changes,
            "diff": full_diff,
            "diff_truncated": False,
            "ok": True,
            "patch_sha256": self.patch_sha256,
        }
        encoded = _json(base)
        if len(encoded) <= max_chars:
            return encoded
        base["diff_truncated"] = True
        low, high = 0, len(full_diff)
        while low < high:
            middle = (low + high + 1) // 2
            base["diff"] = full_diff[:middle]
            if len(_json(base)) <= max_chars:
                low = middle
            else:
                high = middle - 1
        base["diff"] = full_diff[:low]
        encoded = _json(base)
        if len(encoded) > max_chars:
            raise PatchError("patch_result_too_large")
        return encoded


@dataclass(frozen=True, slots=True)
class _Plan:
    change: FileChange
    parts: tuple[str, ...]
    target: Path
    parent_path: str
    parent_identity: tuple[int, ...]
    before: bytes | None
    before_sha256: str | None
    before_identity: tuple[int, ...] | None
    after: bytes | None
    after_sha256: str | None
    mode: int
    additions: int
    deletions: int
    diff: str


@dataclass(slots=True)
class _Staged:
    plan: _Plan
    temporary: Path | None = None
    temporary_identity: tuple[int, ...] | None = None
    backup: Path | None = None
    original_moved: bool = False
    new_installed: bool = False


class AtomicPatchWorkspace:
    """绑定一个 workspace 的结构化 Patch 执行器。"""

    def __init__(
        self,
        workspace_root: str | Path,
        resolver: WorkspacePathResolver,
        *,
        limits: PatchLimits | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        if not isinstance(resolver, WorkspacePathResolver):
            raise TypeError("resolver must be WorkspacePathResolver")
        self._limits = limits or PatchLimits()
        if not isinstance(self._limits, PatchLimits):
            raise TypeError("limits must be PatchLimits")
        if fault_injector is not None and not callable(fault_injector):
            raise TypeError("fault_injector must be callable or None")
        try:
            root = Path(workspace_root).resolve(strict=True)
            if not root.is_dir():
                raise OSError()
        except (OSError, TypeError, ValueError):
            raise PatchError("invalid_workspace_root") from None
        if getattr(resolver, "_root", None) != root:
            raise PatchError("workspace_resolver_root_mismatch")
        self._root = root
        self._resolver = resolver
        self._fault_injector = fault_injector

    def apply(
        self,
        patch: PatchSet,
        *,
        progress_guard: ProgressGuard | None = None,
    ) -> PatchTransactionResult:
        if not isinstance(patch, PatchSet):
            raise TypeError("patch must be PatchSet")
        guard = progress_guard or (lambda: None)
        if not callable(guard):
            raise TypeError("progress_guard must be callable or None")

        with _workspace_mutation_lock(self._root):
            guard()
            plans = self._plan(patch, guard)
            planned_result = PatchTransactionResult(
                patch.document_sha256,
                tuple(
                    PatchFileResult(
                        plan.change.operation,
                        plan.change.path,
                        plan.before_sha256,
                        plan.after_sha256,
                        plan.additions,
                        plan.deletions,
                        plan.diff,
                    )
                    for plan in plans
                ),
            )
            # 模型可见结果必须在第一个 stage/replace 前证明可编码；绝不能文件已改
            # 完才发现结果预算装不下，随后向模型返回一个可重试的“失败”。
            planned_result.to_tool_content(max_chars=self._limits.max_result_chars)
            self._fault("after_preflight", ".")
            staged = self._stage(plans, guard)
            try:
                self._commit(staged, guard)
            except PatchError:
                raise
            return planned_result

    def _plan(self, patch: PatchSet, guard: ProgressGuard) -> tuple[_Plan, ...]:
        plans: list[_Plan] = []
        total_before = 0
        total_after = 0
        for change in sorted(patch.changes, key=lambda item: (item.path.casefold(), item.path)):
            guard()
            try:
                parts = _relative_parts(change.path)
            except WorkspacePathError as error:
                raise PatchError(error.code) from None
            if not parts:
                raise PatchError("invalid_workspace_path")
            if any(
                part.casefold() in _CONTROL_NAMES
                or part.casefold().startswith(".koawa-patch-")
                for part in parts
            ):
                raise PatchError("repository_control_path_forbidden")
            target = self._root.joinpath(*parts)
            parent_path = "/".join(parts[:-1]) or "."
            parent_identity = self._directory_identity(parent_path, guard)

            if isinstance(change, AddFileChange):
                self._assert_missing(target, initial=True)
                after = encode_add(change, self._limits)
                after_document = decode_text_document(
                    after, max_lines=self._limits.max_file_lines
                )
                before = None
                before_sha256 = None
                before_identity = None
                mode = 0o644
                additions = len(after_document.lines)
                deletions = 0
            else:
                read = self._read_existing(change.path, guard)
                if read.sha256 != change.base_sha256:
                    raise PatchError("stale_patch_base")
                before = read.data
                total_before += len(before)
                if total_before > self._limits.max_total_input_bytes:
                    raise PatchError("patch_total_input_limit_exceeded")
                before_sha256 = read.sha256
                before_identity = read.identity
                try:
                    mode = stat.S_IMODE(target.lstat().st_mode)
                except OSError:
                    raise PatchError("stale_patch_base") from None
                # DELETE 也只允许文本，避免模型把任意二进制当成普通源码删除。
                before_document = decode_text_document(
                    before, max_lines=self._limits.max_file_lines
                )
                if isinstance(change, UpdateFileChange):
                    after, additions, deletions = apply_update(
                        before, change, self._limits
                    )
                else:
                    after = None
                    additions = 0
                    deletions = len(before_document.lines)

            after_sha256 = None if after is None else hashlib.sha256(after).hexdigest()
            total_after += 0 if after is None else len(after)
            if total_after > self._limits.max_total_output_bytes:
                raise PatchError("patch_total_output_limit_exceeded")
            diff = _unified_diff(
                change.path, before, after, max_lines=self._limits.max_file_lines
            )
            plans.append(
                _Plan(
                    change,
                    parts,
                    target,
                    parent_path,
                    parent_identity,
                    before,
                    before_sha256,
                    before_identity,
                    after,
                    after_sha256,
                    mode,
                    additions,
                    deletions,
                    diff,
                )
            )
        return tuple(plans)

    def _stage(self, plans: tuple[_Plan, ...], guard: ProgressGuard) -> list[_Staged]:
        staged: list[_Staged] = []
        try:
            for plan in plans:
                guard()
                item = _Staged(plan)
                staged.append(item)
                if plan.after is None:
                    continue
                self._assert_parent(plan, guard)
                fd, name = tempfile.mkstemp(
                    prefix=".koawa-patch-stage-",
                    suffix=".tmp",
                    dir=plan.target.parent,
                )
                item.temporary = Path(name)
                try:
                    with os.fdopen(fd, "wb", closefd=True) as stream:
                        stream.write(plan.after)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(item.temporary, plan.mode)
                except BaseException:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
                self._assert_parent(plan, guard)
                temporary_relative = "/".join(
                    (*plan.parts[:-1], item.temporary.name)
                )
                temporary_read = self._resolver.read_bytes(
                    temporary_relative,
                    max_bytes=self._limits.max_file_bytes,
                    progress_guard=guard,
                )
                if temporary_read.sha256 != plan.after_sha256:
                    raise PatchError("workspace_stage_failed")
                item.temporary_identity = temporary_read.identity
                self._fault("after_stage", plan.change.path)
            return staged
        except PatchError:
            if not self._discard_uncommitted(staged):
                raise PatchError("workspace_cleanup_failed") from None
            raise
        except (OSError, WorkspacePathError):
            if not self._discard_uncommitted(staged):
                raise PatchError("workspace_cleanup_failed") from None
            raise PatchError("workspace_stage_failed") from None
        except Exception:
            if not self._discard_uncommitted(staged):
                raise PatchError("workspace_cleanup_failed") from None
            raise

    def _commit(self, staged: list[_Staged], guard: ProgressGuard) -> None:
        committed: list[_Staged] = []
        try:
            for item in staged:
                guard()
                self._fault("before_commit", item.plan.change.path)
                # fault hook 也作为确定性并发修改注入点：hook 返回后必须再次读取
                # base/hash/identity，再进行第一个 replace。
                self._revalidate(item.plan, guard)
                operation = item.plan.change.operation
                if operation is PatchOperation.ADD:
                    assert item.temporary is not None
                    os.replace(item.temporary, item.plan.target)
                    item.temporary = None
                    item.new_installed = True
                    committed.append(item)
                else:
                    item.backup = self._backup_name(item.plan.target.parent)
                    os.replace(item.plan.target, item.backup)
                    item.original_moved = True
                    committed.append(item)
                    self._fault("after_original_moved", item.plan.change.path)
                    if operation is PatchOperation.UPDATE:
                        assert item.temporary is not None
                        os.replace(item.temporary, item.plan.target)
                        item.temporary = None
                        item.new_installed = True
                        self._fault("after_new_installed", item.plan.change.path)
                _fsync_directory(item.plan.target.parent)
                self._fault("after_commit", item.plan.change.path)

            for item in committed:
                self._verify_committed(item, guard)
            for item in committed:
                if item.backup is not None:
                    item.backup.unlink()
                    item.backup = None
                    _fsync_directory(item.plan.target.parent)
            self._discard_uncommitted(staged)
        except BaseException as failure:
            rollback_ok = self._rollback(committed, staged, guard)
            if not rollback_ok:
                raise PatchError("workspace_outcome_unknown") from None
            if isinstance(failure, PatchError):
                raise failure
            if isinstance(failure, OSError):
                raise PatchError("workspace_commit_failed") from None
            raise

    def _rollback(
        self,
        committed: list[_Staged],
        staged: list[_Staged],
        guard: ProgressGuard,
    ) -> bool:
        ok = True
        for item in reversed(committed):
            try:
                guard()
                self._fault("before_rollback", item.plan.change.path)
                operation = item.plan.change.operation
                if operation is PatchOperation.ADD:
                    self._assert_installed(item, guard)
                    item.plan.target.unlink()
                    item.new_installed = False
                else:
                    assert item.backup is not None and item.original_moved
                    if item.new_installed:
                        self._assert_installed(item, guard)
                        item.plan.target.unlink()
                        item.new_installed = False
                    else:
                        self._assert_missing(item.plan.target, initial=False)
                    os.replace(item.backup, item.plan.target)
                    item.backup = None
                    item.original_moved = False
                    restored = self._read_existing(item.plan.change.path, guard)
                    if (
                        restored.sha256 != item.plan.before_sha256
                        or restored.identity != item.plan.before_identity
                    ):
                        raise PatchError("workspace_object_changed")
                _fsync_directory(item.plan.target.parent)
                self._fault("after_rollback", item.plan.change.path)
            except BaseException:
                ok = False
        if not self._discard_uncommitted(staged):
            ok = False
        return ok

    def _revalidate(self, plan: _Plan, guard: ProgressGuard) -> None:
        self._assert_parent(plan, guard)
        if plan.change.operation is PatchOperation.ADD:
            self._assert_missing(plan.target, initial=False)
            return
        current = self._read_existing(plan.change.path, guard)
        if current.sha256 != plan.before_sha256 or current.identity != plan.before_identity:
            raise PatchError("stale_patch_base")

    def _verify_committed(self, item: _Staged, guard: ProgressGuard) -> None:
        self._assert_parent(item.plan, guard)
        if item.plan.after is None:
            self._assert_missing(item.plan.target, initial=False)
            return
        self._assert_installed(item, guard)

    def _assert_installed(self, item: _Staged, guard: ProgressGuard) -> None:
        current = self._read_existing(item.plan.change.path, guard)
        if (
            current.sha256 != item.plan.after_sha256
            or current.identity != item.temporary_identity
        ):
            raise PatchError("workspace_object_changed")

    def _assert_parent(self, plan: _Plan, guard: ProgressGuard) -> None:
        if self._directory_identity(plan.parent_path, guard) != plan.parent_identity:
            raise PatchError("workspace_object_changed")

    def _directory_identity(self, path: str, guard: ProgressGuard) -> tuple[int, ...]:
        try:
            return self._resolver.inspect_directory(
                path, progress_guard=guard
            ).identity
        except WorkspacePathError as error:
            raise PatchError(error.code) from None

    def _read_existing(self, path: str, guard: ProgressGuard) -> WorkspaceFileRead:
        try:
            return self._resolver.read_bytes(
                path,
                max_bytes=self._limits.max_file_bytes,
                progress_guard=guard,
            )
        except WorkspacePathError as error:
            if error.code == "workspace_path_not_found":
                raise PatchError("patch_target_missing") from None
            raise PatchError(error.code) from None

    @staticmethod
    def _assert_missing(target: Path, *, initial: bool) -> None:
        try:
            target.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise PatchError("workspace_io_error") from None
        code = "patch_target_exists" if initial else "stale_patch_base"
        raise PatchError(code)

    @staticmethod
    def _backup_name(parent: Path) -> Path:
        fd, name = tempfile.mkstemp(
            prefix=".koawa-patch-backup-", suffix=".tmp", dir=parent
        )
        os.close(fd)
        path = Path(name)
        path.unlink()
        return path

    def _fault(self, point: str, path: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point, path)

    @staticmethod
    def _discard_uncommitted(staged: list[_Staged]) -> bool:
        ok = True
        for item in staged:
            if item.temporary is not None:
                try:
                    item.temporary.unlink(missing_ok=True)
                except OSError:
                    ok = False
                item.temporary = None
            if item.backup is not None and not item.original_moved:
                # 原件从未移入或已成功还原：backup 只是陈旧残留。
                try:
                    item.backup.unlink(missing_ok=True)
                except OSError:
                    ok = False
                item.backup = None
            # original_moved=True（还原失败的已提交项）时，backup 是原始内容
            # 唯一幸存副本，必须留在盘上供手工恢复；outcome 已是 unknown，
            # 静默删除会把"未知"变成不可逆的数据销毁（审计 F6）。
        return ok


def _unified_diff(
    path: str, before: bytes | None, after: bytes | None, *, max_lines: int
) -> str:
    before_lines = _diff_lines(before, max_lines=max_lines)
    after_lines = _diff_lines(after, max_lines=max_lines)
    fromfile = "/dev/null" if before is None else f"a/{path}"
    tofile = "/dev/null" if after is None else f"b/{path}"
    return "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=fromfile,
            tofile=tofile,
            n=3,
            lineterm="\n",
        )
    )


def _diff_lines(data: bytes | None, *, max_lines: int) -> list[str]:
    if data is None:
        return []
    document = decode_text_document(data, max_lines=max_lines)
    lines = [line + "\n" for line in document.lines]
    if lines and not document.final_newline:
        lines[-1] = lines[-1][:-1]
    return lines


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock_roots() -> tuple[Path, ...]:
    """Candidate OS-lock directories, newest-compatible first.

    ``%TEMP%`` is the legacy location and remains the first choice, but a stale
    directory created by another Windows account can make it inaccessible even
    though the workspace itself is writable.  The user-local fallback prevents
    every patch transaction from degrading to ``workspace_lock_failed``.
    """
    candidates: list[Path] = []
    candidates.append(Path(tempfile.gettempdir()) / "koawa-agent-v2-locks")
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "KoawaAgentV2" / "locks")
    else:
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
        if xdg_runtime:
            candidates.append(Path(xdg_runtime) / "koawa-agent-v2-locks")
    return tuple(dict.fromkeys(candidates))


@contextmanager
def _workspace_mutation_lock(root: Path) -> Iterator[None]:
    """同进程 RLock + 跨进程 OS lock；锁文件只包含 root digest。"""
    key = str(root).casefold() if os.name == "nt" else str(root)
    with _PROCESS_LOCKS_GUARD:
        local_lock = _PROCESS_LOCKS.setdefault(key, threading.RLock())
    local_lock.acquire()
    stream = None
    acquired = False
    try:
        name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"
        for lock_root in _lock_roots():
            candidate = None
            try:
                lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                candidate = open(lock_root / name, "a+b", buffering=0)
                if candidate.seek(0, os.SEEK_END) == 0:
                    candidate.write(b"\x00")
                candidate.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(candidate.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(candidate.fileno(), fcntl.LOCK_EX)
                stream = candidate
                acquired = True
                break
            except OSError:
                if candidate is not None:
                    try:
                        candidate.close()
                    except OSError:
                        pass
                continue
        if not acquired:
            raise PatchError("workspace_lock_failed")
        yield
    finally:
        try:
            if stream is not None:
                try:
                    if acquired:
                        stream.seek(0)
                        if os.name == "nt":
                            import msvcrt

                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    # 关闭 fd/handle 仍会释放 OS lock。事务结果已经确定后，显式
                    # unlock 的清理错误不能把成功副作用伪装成可重试失败。
                    pass
                try:
                    stream.close()
                except OSError:
                    pass
        finally:
            local_lock.release()
