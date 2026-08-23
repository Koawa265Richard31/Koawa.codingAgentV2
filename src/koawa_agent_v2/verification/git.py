"""D5 安全只读 Git 门面。

模型不能提交 git 参数。本模块只提供固定的 status/diff，并显式关闭 hooks、
fsmonitor、pager、external diff 与 textconv 等仓库可注入执行面。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from .runner import run_bounded_process
from ..tools.workspace import WorkspacePathError, WorkspacePathResolver


class GitFacadeError(Exception):
    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
            raise ValueError("invalid git facade error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GitLimits:
    timeout_seconds: float = 15.0
    max_status_bytes: int = 1_000_000
    max_diff_bytes: int = 1_000_000
    max_paths: int = 10_000
    max_untracked_file_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 300
        ):
            raise GitFacadeError("invalid_git_limits")
        for name, ceiling in (
            ("max_status_bytes", 16 * 1024 * 1024),
            ("max_diff_bytes", 16 * 1024 * 1024),
            ("max_paths", 100_000),
            ("max_untracked_file_bytes", 16 * 1024 * 1024),
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > ceiling:
                raise GitFacadeError("invalid_git_limits")


@dataclass(frozen=True, slots=True)
class GitStatusEntry:
    status: str
    path: str


@dataclass(frozen=True, slots=True)
class GitStatusSnapshot:
    entries: tuple[GitStatusEntry, ...]
    digest: str

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.entries)


@dataclass(frozen=True, slots=True, repr=False)
class GitDiffSnapshot:
    changed_paths: tuple[str, ...]
    diff: str
    diff_sha256: str
    truncated: bool
    status_digest: str

    def __repr__(self) -> str:
        return (
            f"GitDiffSnapshot(changed_paths={len(self.changed_paths)}, "
            f"diff_length={len(self.diff)}, truncated={self.truncated})"
        )


class GitFacade:
    """绑定一个仓库和创建时 dirty baseline 的只读视图。"""

    def __init__(
        self,
        workspace_root: str | Path,
        resolver: WorkspacePathResolver,
        *,
        limits: GitLimits | None = None,
        git_executable: str | Path | None = None,
    ) -> None:
        self._limits = limits or GitLimits()
        if not isinstance(self._limits, GitLimits):
            raise TypeError("limits must be GitLimits")
        if not isinstance(resolver, WorkspacePathResolver):
            raise TypeError("resolver must be WorkspacePathResolver")
        try:
            self._root = Path(workspace_root).resolve(strict=True)
        except (OSError, TypeError, ValueError):
            raise GitFacadeError("invalid_workspace_root") from None
        executable = str(git_executable) if git_executable is not None else shutil.which("git")
        if not executable:
            raise GitFacadeError("git_not_available")
        try:
            self._git = Path(executable).resolve(strict=True)
        except OSError:
            raise GitFacadeError("git_not_available") from None
        self._resolver = resolver
        try:
            probe = self._git_command(
                ("rev-parse", "--is-inside-work-tree"), max_bytes=128
            )
        except GitFacadeError as exc:
            # A non-repository directory makes rev-parse exit non-zero before
            # any real probe output; report the actionable code.
            if exc.code == "git_command_failed":
                raise GitFacadeError("not_a_git_repository") from None
            raise
        if probe.strip() != b"true":
            raise GitFacadeError("not_a_git_repository")
        # 可信 facade 会关闭用户/仓库配置中的 fsmonitor。第一次 status 允许 Git
        # 清理既有 index 扩展并刷新 stat cache；第二次才作为稳定 dirty baseline。
        # 这也避免极短 fixture 在切换 fsmonitor 模式时被误判成用户 dirty 文件。
        self.status()
        self._baseline = self.status()
        self._baseline_paths = frozenset(item.path.casefold() for item in self._baseline.entries)
        self._baseline_fingerprints = {
            item.path: self._fingerprint(item.path) for item in self._baseline.entries
        }
        # D22 F2: 保护路径集合必须内容锚定。status 元数据可能出现 stat 缓存幻影
        # （报告 " M" 但内容与索引 blob 一致——D20 实测 6 轮中的 5 轮），授权判定
        # 不得信任它：逐个 baseline 条目用 git hash-object 与 ls-files -s 的索引
        # blob 比对，内容一致即视为干净、不保护。未跟踪路径无 blob，一律保护。
        self._protected_paths = self._content_verified_protection(self._baseline)

    @property
    def baseline(self) -> GitStatusSnapshot:
        return self._baseline

    @property
    def protected_paths(self) -> frozenset[str]:
        return self._protected_paths

    def _content_verified_protection(
        self,
        baseline: GitStatusSnapshot,
    ) -> frozenset[str]:
        """Startup protection set with content-truth dirty classification.

        A path is protected only when it has no index blob (untracked) or when
        its current content hash differs from the index blob sha.  Phantom
        stat-cache modifications (metadata says " M", content identical) are
        therefore NOT protected; the D4 transaction still re-verifies
        base_sha256 against the live content before applying.
        """
        protected: set[str] = set()
        for entry in baseline.entries:
            key = entry.path.casefold()
            if entry.status == "??":
                protected.add(key)
                continue
            if self._diff_has_changes(entry.path):
                protected.add(key)
        return frozenset(protected)

    def status(
        self,
        *,
        progress_guard: Callable[[], None] | None = None,
    ) -> GitStatusSnapshot:
        raw = self._git_command(
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            max_bytes=self._limits.max_status_bytes,
            progress_guard=progress_guard,
        )
        entries = _parse_status(raw, self._limits.max_paths)
        canonical = json.dumps(
            [(item.status, item.path) for item in entries],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return GitStatusSnapshot(entries, hashlib.sha256(canonical).hexdigest())

    def agent_changed_paths(self, status: GitStatusSnapshot | None = None) -> tuple[str, ...]:
        snapshot = status or self.status()
        paths = {
            item.path
            for item in snapshot.entries
            if item.path.casefold() not in self._baseline_paths
        }
        return tuple(sorted(paths, key=lambda path: (path.casefold(), path)))

    def baseline_unchanged(self) -> bool:
        return all(
            self._fingerprint(path) == fingerprint
            for path, fingerprint in self._baseline_fingerprints.items()
        )

    def diff(
        self,
        *,
        progress_guard: Callable[[], None] | None = None,
    ) -> GitDiffSnapshot:
        status = self.status(progress_guard=progress_guard)
        changed = self.agent_changed_paths(status)
        status_by_path = {item.path: item.status for item in status.entries}
        tracked = tuple(
            path for path in changed if status_by_path.get(path) != "??"
        )
        raw = b""
        if tracked:
            raw = self._git_command(
                (
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                    "--",
                    *tracked,
                ),
                max_bytes=self._limits.max_diff_bytes,
                progress_guard=progress_guard,
                allow_truncation=True,
            )
        text = raw.decode("utf-8", "replace")
        for entry in status.entries:
            if entry.status != "??" or entry.path not in changed:
                continue
            if progress_guard is not None:
                progress_guard()
            try:
                read = self._resolver.read_bytes(
                    entry.path, max_bytes=self._limits.max_untracked_file_bytes
                )
                content = read.data.decode("utf-8", "strict")
            except (WorkspacePathError, UnicodeError):
                text += f"Binary or unreadable untracked file: {entry.path}\n"
                continue
            text += "".join(
                difflib.unified_diff(
                    (),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{entry.path}",
                    tofile=f"b/{entry.path}",
                )
            )
        encoded = text.encode("utf-8")
        truncated = len(encoded) > self._limits.max_diff_bytes
        if truncated:
            encoded = encoded[: self._limits.max_diff_bytes]
            text = encoded.decode("utf-8", "ignore")
        return GitDiffSnapshot(
            changed,
            text,
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            truncated,
            status.digest,
        )

    def _git_prefix(self) -> tuple[str, ...]:
        return (
            str(self._git),
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
            "-C", str(self._root),
        )

    @staticmethod
    def _git_environment() -> dict[str, str]:
        return {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_EXTERNAL_DIFF": "",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }

    def _git_command(
        self,
        arguments: tuple[str, ...],
        *,
        max_bytes: int,
        progress_guard: Callable[[], None] | None = None,
        allow_truncation: bool = False,
    ) -> bytes:
        result = run_bounded_process(
            self._root,
            self._git_prefix() + arguments,
            timeout_seconds=self._limits.timeout_seconds,
            max_stdout_bytes=max_bytes,
            max_stderr_bytes=32_768,
            environment=self._git_environment(),
            progress_guard=progress_guard,
        )
        if result.start_failed:
            raise GitFacadeError("git_start_failed")
        if result.timed_out:
            raise GitFacadeError("git_timed_out")
        if result.exit_code != 0:
            raise GitFacadeError("git_command_failed")
        if result.stdout_bytes > len(result.stdout) and not allow_truncation:
            raise GitFacadeError("git_output_too_large")
        return result.stdout

    def _diff_has_changes(self, relative: str) -> bool:
        """Content-anchored dirty test for a tracked path.

        Uses git diff (worktree vs index) with --ignore-space-at-eol: stat-cache
        phantom " M" entries and pure CRLF/LF byte differences are clean, real
        text changes or deletions are dirty -> protected.

        Why --ignore-space-at-eol: the facade neutralizes ALL git config
        (GIT_CONFIG_* = devnull, D5 hardening) which disables core.autocrlf.
        On Windows, Python/editor writes are CRLF while the index holds LF
        blobs -> status reports " M" although the text is identical. That
        deterministic misclassification blocked every UPDATE for 6 rounds in
        D20. CR is trailing whitespace at EOL, so this flag makes the decision
        config-neutral and reproducible; real text changes still exit 1.
        """
        result = run_bounded_process(
            self._root,
            self._git_prefix()
            + (
                "diff",
                "--quiet",
                "--ignore-space-at-eol",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--",
                relative,
            ),
            timeout_seconds=self._limits.timeout_seconds,
            max_stdout_bytes=512,
            max_stderr_bytes=32_768,
            environment=self._git_environment(),
            progress_guard=None,
        )
        if result.start_failed or result.timed_out:
            return True  # fail-closed: cannot verify -> protect
        return result.exit_code != 0

    def _fingerprint(self, relative: str) -> str:
        try:
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts:
                return "invalid"
            target = self._root.joinpath(*path.parts)
            stat_result = target.lstat()
            if target.is_file() and not target.is_symlink():
                digest = hashlib.sha256()
                with target.open("rb") as stream:
                    remaining = self._limits.max_untracked_file_bytes + 1
                    while remaining > 0:
                        chunk = stream.read(min(65_536, remaining))
                        if not chunk:
                            break
                        digest.update(chunk)
                        remaining -= len(chunk)
                if remaining == 0:
                    return f"large:{stat_result.st_size}:{stat_result.st_mtime_ns}"
                return "file:" + digest.hexdigest()
            return f"other:{stat_result.st_mode}:{stat_result.st_size}:{stat_result.st_mtime_ns}"
        except OSError:
            return "missing"


def _parse_status(raw: bytes, max_paths: int) -> tuple[GitStatusEntry, ...]:
    fields = raw.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    entries: list[GitStatusEntry] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        if len(field) < 4 or field[2:3] != b" ":
            raise GitFacadeError("invalid_git_status_output")
        try:
            status = field[:2].decode("ascii", "strict")
            path = field[3:].decode("utf-8", "strict").replace("\\", "/")
        except UnicodeError:
            raise GitFacadeError("invalid_git_status_output") from None
        if not path or "\x00" in path:
            raise GitFacadeError("invalid_git_status_output")
        entries.append(GitStatusEntry(status, path))
        index += 1
        if "R" in status or "C" in status:
            if index >= len(fields):
                raise GitFacadeError("invalid_git_status_output")
            try:
                old_path = fields[index].decode("utf-8", "strict").replace("\\", "/")
            except UnicodeError:
                raise GitFacadeError("invalid_git_status_output") from None
            entries.append(GitStatusEntry(status, old_path))
            index += 1
        if len(entries) > max_paths:
            raise GitFacadeError("git_path_limit_exceeded")
    return tuple(sorted(entries, key=lambda item: (item.path.casefold(), item.path, item.status)))
