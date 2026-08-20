"""D3 工作区内、有界、只读的文件系统访问边界。

Resolver 不把宿主机 ``Path`` 交给调用者稍后重新打开；校验与使用属于同一个操作：
文件只能从已经验证的 descriptor/handle 读取，目录枚举前后还要复核对象身份。

构造参数 root 来自可信本地配置。若 root 本身是 symlink/junction，会先解析并绑定它的
最终目录；模型传入路径中的任何后代 symlink/junction/reparse point 则一律拒绝。

POSIX 逐组件使用 ``dir_fd`` 与 ``O_NOFOLLOW``，关闭 rename/symlink 竞态。Python 在
Windows 没有等价的目录相对句柄 API；Windows 实现会拒绝所有静态可见 reparse 组件，
在读取前验证已打开句柄的最终路径，并在 ``scandir`` 前后复核目录身份及每个 entry。
这足以阻止静态恶意仓库通过 junction/reparse 逃逸，但不宣称能抵抗另一个有宿主机
改名权限、能在目录枚举窗口反复 swap/restore 的并发进程。hard link 本身没有
symlink/reparse 标志，本层只证明所打开目录项位于绑定 namespace 内，不证明同一 inode
在 root 外没有另一个名字；bind mount/完整文件系统隔离属于 D8 容器边界。
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable


_ERROR_CODE = re.compile(r"[a-z0-9_]{1,96}")
_DOS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
    *(f"COM{index}" for index in ("¹", "²", "³")),
    *(f"LPT{index}" for index in ("¹", "²", "³")),
}
_MAX_PATH_CHARS = 4096
_MAX_PATH_COMPONENTS = 256
_MAX_COMPONENT_CHARS = 255
_MAX_HARD_READ_BYTES = 64 * 1024 * 1024
_MAX_HARD_DIRECTORY_SCAN_ENTRIES = 1_000_000


class WorkspacePathError(Exception):
    """稳定、可交给模型的错误；正文永不嵌入宿主绝对路径。"""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
            raise ValueError("invalid workspace error code")
        self.code = code
        super().__init__(code)


class WorkspaceEntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True, slots=True, repr=False)
class WorkspaceFileRead:
    path: str
    data: bytes
    byte_length: int
    sha256: str
    identity: tuple[int, ...]

    def __repr__(self) -> str:
        return (
            f"WorkspaceFileRead(path={self.path!r}, byte_length={self.byte_length}, "
            f"sha256={self.sha256!r})"
        )


@dataclass(frozen=True, slots=True)
class WorkspaceDirectoryEntry:
    path: str
    name: str
    kind: WorkspaceEntryKind
    size: int | None


@dataclass(frozen=True, slots=True)
class WorkspaceDirectoryListing:
    path: str
    entries: tuple[WorkspaceDirectoryEntry, ...]
    total_entries: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class WorkspaceDirectoryIdentity:
    """D4 写事务使用的已打开目录身份；不暴露宿主绝对路径。"""

    path: str
    identity: tuple[int, ...]


ProgressGuard = Callable[[], None]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    identity: tuple[int, ...]
    kind: int
    size: int
    mtime_ns: int


class WorkspacePathResolver:
    """绑定一个 canonical workspace root，只暴露有界只读 API。"""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        hard_max_read_bytes: int = 64 * 1024 * 1024,
        hard_max_directory_scan_entries: int = 100_000,
    ) -> None:
        self._hard_max_read_bytes = _positive_limit(
            hard_max_read_bytes, "invalid_workspace_limit"
        )
        self._hard_max_directory_scan_entries = _positive_limit(
            hard_max_directory_scan_entries, "invalid_workspace_limit"
        )
        if (
            self._hard_max_read_bytes > _MAX_HARD_READ_BYTES
            or self._hard_max_directory_scan_entries
            > _MAX_HARD_DIRECTORY_SCAN_ENTRIES
        ):
            raise WorkspacePathError("invalid_workspace_limit")
        try:
            canonical = Path(root).resolve(strict=True)
            root_stat = canonical.stat()
        except (OSError, TypeError, ValueError):
            raise WorkspacePathError("invalid_workspace_root") from None
        if not stat.S_ISDIR(root_stat.st_mode):
            raise WorkspacePathError("invalid_workspace_root")

        self._root = canonical
        self._lock = threading.RLock()
        self._closed = False
        self._root_fd: int | None = None
        self._root_handle: int | None = None

        if os.name == "nt":
            handle = _win_open_directory(canonical)
            try:
                if _win_is_reparse(handle):
                    raise WorkspacePathError("invalid_workspace_root")
                self._root_final = _win_final_path(handle)
                self._root_snapshot = _win_snapshot(handle)
            except BaseException:
                _win_close(handle)
                raise
            self._root_handle = handle
        else:
            required = (os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"))
            if not required:
                raise WorkspacePathError("secure_path_backend_unavailable")
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                fd = os.open(canonical, flags)
            except OSError:
                raise WorkspacePathError("invalid_workspace_root") from None
            self._root_fd = fd
            self._root_snapshot = _snapshot_from_stat(os.fstat(fd))
            self._root_final = None

    def __enter__(self) -> "WorkspacePathResolver":
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._root_fd is not None:
                os.close(self._root_fd)
                self._root_fd = None
            if self._root_handle is not None:
                _win_close(self._root_handle)
                self._root_handle = None

    def read_bytes(
        self,
        path: str,
        *,
        max_bytes: int,
        progress_guard: ProgressGuard | None = None,
    ) -> WorkspaceFileRead:
        """从已验证的打开对象读取一个普通文件，绝不返回路径让调用者重开。"""
        limit = _positive_limit(max_bytes, "invalid_workspace_limit")
        if limit > self._hard_max_read_bytes:
            raise WorkspacePathError("invalid_workspace_limit")
        guard = _guard(progress_guard)
        parts = _relative_parts(path)
        display = _display(parts)

        with self._lock:
            self._ensure_open()
            guard()
            self._preflight(parts)
            if os.name == "nt":
                return self._read_windows(parts, display, limit, guard)
            return self._read_posix(parts, display, limit, guard)

    def list_directory(
        self,
        path: str = ".",
        *,
        max_entries: int,
        max_scan_entries: int,
        progress_guard: ProgressGuard | None = None,
    ) -> WorkspaceDirectoryListing:
        """不跟随链接，返回确定性排序的直接子项。

        ``max_scan_entries`` 与 ``max_entries`` 分离：OS 枚举顺序不稳定，如果到 N
        就停止，无法得到确定性的 top-N。因此扫描上限命中时整体失败，不返回随机子集。
        """
        output_limit = _positive_limit(max_entries, "invalid_workspace_limit")
        scan_limit = _positive_limit(max_scan_entries, "invalid_workspace_limit")
        if output_limit > scan_limit or scan_limit > self._hard_max_directory_scan_entries:
            raise WorkspacePathError("invalid_workspace_limit")
        guard = _guard(progress_guard)
        parts = _relative_parts(path)
        display = _display(parts)

        with self._lock:
            self._ensure_open()
            guard()
            self._preflight(parts)
            if os.name == "nt":
                entries = self._list_windows(parts, scan_limit, guard)
            else:
                entries = self._list_posix(parts, scan_limit, guard)
            entries.sort(key=lambda item: (item.path.casefold(), item.path))
            total = len(entries)
            return WorkspaceDirectoryListing(
                path=display,
                entries=tuple(entries[:output_limit]),
                total_entries=total,
                truncated=total > output_limit,
            )

    def inspect_directory(
        self,
        path: str = ".",
        *,
        progress_guard: ProgressGuard | None = None,
    ) -> WorkspaceDirectoryIdentity:
        """打开并验证一个目录，供写事务绑定 parent identity。

        与 ``list_directory`` 不同，本方法不枚举目录内容，因此巨大目录不会为了
        一次文件修改触发扫描预算。返回值只包含 workspace-relative display path
        和可在同一进程内复核的 OS object identity。
        """
        guard = _guard(progress_guard)
        parts = _relative_parts(path)
        display = _display(parts)
        with self._lock:
            self._ensure_open()
            guard()
            self._preflight(parts)
            if os.name == "nt":
                handle = _win_open_directory(self._root.joinpath(*parts))
                try:
                    snapshot = _win_snapshot(handle)
                    self._assert_windows_contained(handle)
                    if _win_is_reparse(handle) or snapshot.kind != stat.S_IFDIR:
                        raise WorkspacePathError("workspace_not_directory")
                    self._preflight(parts)
                    self._assert_root_unchanged()
                    return WorkspaceDirectoryIdentity(display, snapshot.identity)
                finally:
                    _win_close(handle)

            fd = self._open_posix(parts, require_directory=True)
            try:
                snapshot = _snapshot_from_stat(os.fstat(fd))
                if snapshot.kind != stat.S_IFDIR:
                    raise WorkspacePathError("workspace_not_directory")
                self._assert_root_unchanged()
                return WorkspaceDirectoryIdentity(display, snapshot.identity)
            except WorkspacePathError:
                raise
            except OSError:
                raise WorkspacePathError("workspace_io_error") from None
            finally:
                os.close(fd)

    def _ensure_open(self) -> None:
        if self._closed:
            raise WorkspacePathError("workspace_resolver_closed")

    def _preflight(self, parts: tuple[str, ...]) -> None:
        current = self._root
        try:
            for component in parts:
                current = current / component
                item_stat = current.lstat()
                if stat.S_ISLNK(item_stat.st_mode) or _stat_is_reparse(item_stat):
                    raise WorkspacePathError("workspace_path_link_forbidden")
            canonical = current.resolve(strict=True)
            canonical.relative_to(self._root)
        except WorkspacePathError:
            raise
        except FileNotFoundError:
            raise WorkspacePathError("workspace_path_not_found") from None
        except PermissionError:
            raise WorkspacePathError("workspace_path_access_denied") from None
        except ValueError:
            raise WorkspacePathError("workspace_path_outside") from None
        except OSError:
            raise WorkspacePathError("workspace_io_error") from None

    def _read_posix(
        self, parts: tuple[str, ...], display: str, limit: int, guard: ProgressGuard
    ) -> WorkspaceFileRead:
        fd = self._open_posix(parts, require_directory=False)
        try:
            before = _snapshot_from_stat(os.fstat(fd))
            if before.kind != stat.S_IFREG:
                raise WorkspacePathError("workspace_not_regular_file")
            if before.size > limit:
                raise WorkspacePathError("workspace_file_too_large")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                guard()
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = _snapshot_from_stat(os.fstat(fd))
            self._assert_root_unchanged()
            if before != after:
                raise WorkspacePathError("workspace_object_changed")
            if len(data) > limit:
                raise WorkspacePathError("workspace_file_too_large")
            return _file_read(display, data, before.identity)
        except WorkspacePathError:
            raise
        except OSError:
            raise WorkspacePathError("workspace_io_error") from None
        finally:
            os.close(fd)

    def _read_windows(
        self, parts: tuple[str, ...], display: str, limit: int, guard: ProgressGuard
    ) -> WorkspaceFileRead:
        import msvcrt

        candidate = self._root.joinpath(*parts)
        try:
            if not stat.S_ISREG(candidate.lstat().st_mode):
                raise WorkspacePathError("workspace_not_regular_file")
        except WorkspacePathError:
            raise
        except FileNotFoundError:
            raise WorkspacePathError("workspace_path_not_found") from None
        except PermissionError:
            raise WorkspacePathError("workspace_path_access_denied") from None
        except OSError:
            raise WorkspacePathError("workspace_io_error") from None
        try:
            stream = open(candidate, "rb", buffering=0)
        except FileNotFoundError:
            raise WorkspacePathError("workspace_path_not_found") from None
        except PermissionError:
            raise WorkspacePathError("workspace_path_access_denied") from None
        except OSError:
            raise WorkspacePathError("workspace_io_error") from None
        try:
            handle = msvcrt.get_osfhandle(stream.fileno())
            before = _win_snapshot(handle)
            self._assert_windows_contained(handle)
            if _win_is_reparse(handle) or before.kind != stat.S_IFREG:
                raise WorkspacePathError("workspace_not_regular_file")
            if before.size > limit:
                raise WorkspacePathError("workspace_file_too_large")
            guard()
            data = stream.read(limit + 1)
            guard()
            after = _win_snapshot(handle)
            self._assert_windows_contained(handle)
            self._preflight(parts)
            self._assert_root_unchanged()
            if before != after:
                raise WorkspacePathError("workspace_object_changed")
            if len(data) > limit:
                raise WorkspacePathError("workspace_file_too_large")
            return _file_read(display, data, before.identity)
        except WorkspacePathError:
            raise
        except OSError:
            raise WorkspacePathError("workspace_io_error") from None
        finally:
            stream.close()

    def _list_posix(
        self, parts: tuple[str, ...], scan_limit: int, guard: ProgressGuard
    ) -> list[WorkspaceDirectoryEntry]:
        fd = self._open_posix(parts, require_directory=True)
        try:
            before = _snapshot_from_stat(os.fstat(fd))
            if before.kind != stat.S_IFDIR:
                raise WorkspacePathError("workspace_not_directory")
            result: list[WorkspaceDirectoryEntry] = []
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    guard()
                    if len(result) >= scan_limit:
                        raise WorkspacePathError("workspace_directory_scan_limit_exceeded")
                    item_parts = (*parts, entry.name)
                    _validate_component(entry.name)
                    item_stat = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    result.append(_directory_entry(item_parts, item_stat))
            after = _snapshot_from_stat(os.fstat(fd))
            self._assert_root_unchanged()
            if before != after:
                raise WorkspacePathError("workspace_object_changed")
            return result
        except WorkspacePathError:
            raise
        except FileNotFoundError:
            raise WorkspacePathError("workspace_object_changed") from None
        except PermissionError:
            raise WorkspacePathError("workspace_path_access_denied") from None
        except OSError:
            raise WorkspacePathError("workspace_io_error") from None
        finally:
            os.close(fd)

    def _list_windows(
        self, parts: tuple[str, ...], scan_limit: int, guard: ProgressGuard
    ) -> list[WorkspaceDirectoryEntry]:
        candidate = self._root.joinpath(*parts)
        handle = _win_open_directory(candidate)
        try:
            before = _win_snapshot(handle)
            self._assert_windows_contained(handle)
            if _win_is_reparse(handle) or before.kind != stat.S_IFDIR:
                raise WorkspacePathError("workspace_not_directory")
            result: list[WorkspaceDirectoryEntry] = []
            with os.scandir(candidate) as iterator:
                for entry in iterator:
                    guard()
                    if len(result) >= scan_limit:
                        raise WorkspacePathError("workspace_directory_scan_limit_exceeded")
                    _validate_component(entry.name)
                    item_parts = (*parts, entry.name)
                    self._preflight(item_parts)
                    item_stat = entry.stat(follow_symlinks=False)
                    result.append(_directory_entry(item_parts, item_stat))
            after = _win_snapshot(handle)
            self._assert_windows_contained(handle)
            self._preflight(parts)
            reopened = _win_open_directory(candidate)
            try:
                if _win_snapshot(reopened).identity != before.identity:
                    raise WorkspacePathError("workspace_object_changed")
            finally:
                _win_close(reopened)
            self._assert_root_unchanged()
            if before != after:
                raise WorkspacePathError("workspace_object_changed")
            return result
        except WorkspacePathError:
            raise
        except FileNotFoundError:
            raise WorkspacePathError("workspace_object_changed") from None
        except PermissionError:
            raise WorkspacePathError("workspace_path_access_denied") from None
        except OSError:
            raise WorkspacePathError("workspace_io_error") from None
        finally:
            _win_close(handle)

    def _open_posix(self, parts: tuple[str, ...], *, require_directory: bool) -> int:
        assert self._root_fd is not None
        current = os.dup(self._root_fd)
        if not parts:
            return current
        try:
            for index, component in enumerate(parts):
                last = index == len(parts) - 1
                flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                if not last or require_directory:
                    flags |= os.O_DIRECTORY
                else:
                    flags |= getattr(os, "O_NONBLOCK", 0)
                try:
                    child = os.open(component, flags, dir_fd=current)
                except OSError as exc:
                    _raise_open_error(exc, final=last, require_directory=require_directory)
                os.close(current)
                current = child
            return current
        except BaseException:
            os.close(current)
            raise

    def _assert_windows_contained(self, handle: int) -> None:
        assert self._root_handle is not None
        root_final = _win_final_path(self._root_handle)
        target_final = _win_final_path(handle)
        root_key = root_final.rstrip("\\/").casefold()
        target_key = target_final.rstrip("\\/").casefold()
        if target_key != root_key and not target_key.startswith(root_key + "\\"):
            raise WorkspacePathError("workspace_path_outside")

    def _assert_root_unchanged(self) -> None:
        if os.name == "nt":
            assert self._root_handle is not None
            if _win_snapshot(self._root_handle).identity != self._root_snapshot.identity:
                raise WorkspacePathError("workspace_object_changed")
        else:
            assert self._root_fd is not None
            if _snapshot_from_stat(os.fstat(self._root_fd)).identity != self._root_snapshot.identity:
                raise WorkspacePathError("workspace_object_changed")


def _relative_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH_CHARS:
        raise WorkspacePathError("invalid_workspace_path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise WorkspacePathError("invalid_workspace_path")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise WorkspacePathError("invalid_workspace_path") from None
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if windows.drive or windows.root or posix.root or value.startswith(("/", "\\")):
        raise WorkspacePathError("invalid_workspace_path")
    raw = re.split(r"[\\/]+", value)
    parts: list[str] = []
    for component in raw:
        if component in ("", "."):
            continue
        if component == "..":
            raise WorkspacePathError("invalid_workspace_path")
        _validate_component(component)
        parts.append(component)
    if len(parts) > _MAX_PATH_COMPONENTS:
        raise WorkspacePathError("invalid_workspace_path")
    return tuple(parts)


def _validate_component(component: str) -> None:
    if (
        not component
        or len(component) > _MAX_COMPONENT_CHARS
        or component in (".", "..")
        or "/" in component
        or "\\" in component
        or ":" in component
        or component.endswith((".", " "))
        or any(ord(char) < 32 or ord(char) == 127 for char in component)
    ):
        raise WorkspacePathError("invalid_workspace_path")
    try:
        component.encode("utf-8", "strict")
    except UnicodeError:
        # POSIX scandir 会用 surrogateescape 表示非法字节文件名；它不能进入
        # JSON ToolResult，也不能等到下一轮 Provider UTF-8 序列化时才失败。
        raise WorkspacePathError("invalid_workspace_path") from None
    stem = component.split(".", 1)[0].upper()
    if stem in _DOS_RESERVED:
        raise WorkspacePathError("invalid_workspace_path")


def _display(parts: tuple[str, ...]) -> str:
    return "/".join(parts) if parts else "."


def _positive_limit(value: int, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WorkspacePathError(code)
    return value


def _guard(value: ProgressGuard | None) -> ProgressGuard:
    if value is None:
        return lambda: None
    if not callable(value):
        raise TypeError("progress_guard must be callable or None")
    return value


def _snapshot_from_stat(value: os.stat_result) -> _Snapshot:
    return _Snapshot(
        identity=(int(value.st_dev), int(value.st_ino)),
        kind=stat.S_IFMT(value.st_mode),
        size=int(value.st_size),
        mtime_ns=int(value.st_mtime_ns),
    )


def _stat_is_reparse(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _directory_entry(
    parts: tuple[str, ...], value: os.stat_result
) -> WorkspaceDirectoryEntry:
    if stat.S_ISLNK(value.st_mode) or _stat_is_reparse(value):
        raise WorkspacePathError("workspace_path_link_forbidden")
    kind_bits = stat.S_IFMT(value.st_mode)
    if kind_bits == stat.S_IFREG:
        kind = WorkspaceEntryKind.FILE
        size: int | None = int(value.st_size)
    elif kind_bits == stat.S_IFDIR:
        kind = WorkspaceEntryKind.DIRECTORY
        size = None
    else:
        raise WorkspacePathError("workspace_object_type_forbidden")
    return WorkspaceDirectoryEntry(
        path=_display(parts), name=parts[-1], kind=kind, size=size
    )


def _file_read(
    display: str, data: bytes, identity: tuple[int, ...]
) -> WorkspaceFileRead:
    return WorkspaceFileRead(
        path=display,
        data=data,
        byte_length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        identity=identity,
    )


def _raise_open_error(exc: OSError, *, final: bool, require_directory: bool) -> None:
    if exc.errno in (errno.ELOOP,):
        raise WorkspacePathError("workspace_path_link_forbidden") from None
    if exc.errno in (errno.ENOENT,):
        raise WorkspacePathError("workspace_path_not_found") from None
    if exc.errno in (errno.EACCES, errno.EPERM):
        raise WorkspacePathError("workspace_path_access_denied") from None
    if exc.errno == errno.ENOTDIR:
        code = "workspace_not_directory" if final and require_directory else "workspace_path_not_found"
        raise WorkspacePathError(code) from None
    raise WorkspacePathError("workspace_io_error") from None


# 最小 Win32 句柄适配；定义在 POSIX 仍可导入，但不会被调用。
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _KERNEL32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _KERNEL32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL
    _GetFinalPathNameByHandleW = _KERNEL32.GetFinalPathNameByHandleW
    _GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    ]
    _GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _GetFileInformationByHandle = _KERNEL32.GetFileInformationByHandle
    _GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    _GetFileInformationByHandle.restype = wintypes.BOOL


def _win_open_directory(path: Path) -> int:
    if os.name != "nt":
        raise WorkspacePathError("secure_path_backend_unavailable")
    handle = _CreateFileW(
        str(path), 0x80000000, 0x00000001 | 0x00000002 | 0x00000004,
        None, 3, 0x02000000 | 0x00200000, None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in (2, 3):
            raise WorkspacePathError("workspace_path_not_found")
        if error == 5:
            raise WorkspacePathError("workspace_path_access_denied")
        raise WorkspacePathError("workspace_io_error")
    return int(handle)


def _win_close(handle: int) -> None:
    if os.name == "nt":
        _CloseHandle(handle)


def _win_final_path(handle: int) -> str:
    if os.name != "nt":
        raise WorkspacePathError("secure_path_backend_unavailable")
    size = 512
    while size <= 32768:
        buffer = ctypes.create_unicode_buffer(size)
        length = _GetFinalPathNameByHandleW(handle, buffer, size, 0x2)
        if length == 0:
            raise WorkspacePathError("workspace_io_error")
        if length < size:
            return buffer.value
        size = length + 1
    raise WorkspacePathError("workspace_io_error")


def _win_snapshot(handle: int) -> _Snapshot:
    if os.name != "nt":
        raise WorkspacePathError("secure_path_backend_unavailable")
    info = _BY_HANDLE_FILE_INFORMATION()
    if not _GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise WorkspacePathError("workspace_io_error")
    attributes = int(info.dwFileAttributes)
    kind = stat.S_IFDIR if attributes & 0x10 else stat.S_IFREG
    size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
    mtime = (int(info.ftLastWriteTime.dwHighDateTime) << 32) | int(
        info.ftLastWriteTime.dwLowDateTime
    )
    return _Snapshot(
        identity=(
            int(info.dwVolumeSerialNumber), int(info.nFileIndexHigh), int(info.nFileIndexLow)
        ),
        kind=kind,
        size=size,
        mtime_ns=mtime,
    )


def _win_is_reparse(handle: int) -> bool:
    if os.name != "nt":
        return False
    info = _BY_HANDLE_FILE_INFORMATION()
    if not _GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise WorkspacePathError("workspace_io_error")
    return bool(int(info.dwFileAttributes) & 0x400)
