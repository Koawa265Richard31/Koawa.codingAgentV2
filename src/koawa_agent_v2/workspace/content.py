"""I7 stable repository prestate and content identity snapshots.

The scanner deliberately keeps file bodies out of events.  It returns bounded
digests and raw binary patch bytes; callers that need crash recovery must place
those bytes in the content-addressed artifact package store.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..agents.graph import AgentError
from .subprocesses import run_bounded


MAX_ENTRIES = 10_000
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PATH_BYTES = 4_096
GIT_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    path_bytes: bytes
    kind: str
    mode: int
    size: int
    content_or_target_sha256: str

    def document(self) -> dict[str, object]:
        return {
            "path_sha256": _digest(self.path_bytes),
            "kind": self.kind,
            "mode": self.mode,
            "size": self.size,
            "content_or_target_sha256": self.content_or_target_sha256,
        }


@dataclass(frozen=True, slots=True)
class RepoPrestate:
    head_commit: str
    index_identity_digest: str
    status_raw_digest: str
    working_tree_manifest_digest: str
    prestate_digest: str


@dataclass(frozen=True, slots=True)
class ContentSnapshot:
    prestate: RepoPrestate
    diff_bytes: bytes
    diff_digest: str
    entries: tuple[ManifestEntry, ...]
    working_tree_content_digest: str


def capture_repository(
    repo_root: Path,
    *,
    base_commit: str | None = None,
    git_binary: str | None = None,
) -> ContentSnapshot:
    """Capture one coherent repository state or fail with a stable race code."""

    root = Path(repo_root).resolve()
    git = _resolve_git(git_binary)
    # Prime Git's index stat cache before establishing the first fence.  Git
    # may refresh this cache on the first diff after checkout; that is an
    # internal observation side effect, not a concurrent workspace mutation.
    initial_head = _git_bytes(root, git, "rev-parse", "HEAD").strip().decode("ascii", "strict")
    base = initial_head if base_commit is None else _commit(base_commit)
    _git_bytes(
        root, git, "diff", "--binary", "--full-index", "--no-ext-diff",
        "--no-textconv", base, "--",
    )
    before = _git_fence(root, git)
    diff_bytes = _git_bytes(
        root,
        git,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        base,
        "--",
    )
    entries = tuple(_scan(root))
    after = _git_fence(root, git)
    if before != after:
        raise AgentError("workspace_changed_during_digest")
    manifest_digest = _manifest_digest(entries)
    prestate_doc = {
        "head_commit": before[0],
        "index_identity_digest": before[1],
        "status_raw_digest": before[2],
        "working_tree_manifest_digest": manifest_digest,
    }
    prestate_digest = _canonical_digest(prestate_doc)
    return ContentSnapshot(
        RepoPrestate(before[0], before[1], before[2], manifest_digest, prestate_digest),
        diff_bytes,
        _digest(diff_bytes),
        entries,
        manifest_digest,
    )


def repository_identity(repo_root: Path, *, git_binary: str | None = None) -> str:
    root = Path(repo_root).resolve()
    git = _resolve_git(git_binary)
    common = _git_bytes(root, git, "rev-parse", "--git-common-dir").strip()
    top = _git_bytes(root, git, "rev-parse", "--show-toplevel").strip()
    return _canonical_digest(
        {"git_common_dir": os.fsdecode(common), "top_level": os.fsdecode(top)}
    )


def _git_fence(root: Path, git: str) -> tuple[str, str, str]:
    head = _git_bytes(root, git, "rev-parse", "HEAD").strip().decode("ascii", "strict")
    _commit(head)
    # status may refresh the index stat cache, so capture the index identity
    # after it has completed on both sides of the content scan.
    status = _git_bytes(
        root, git, "status", "--porcelain=v2", "-z", "--untracked-files=all"
    )
    index_path = _git_bytes(root, git, "rev-parse", "--git-path", "index").strip()
    index = Path(os.fsdecode(index_path))
    if not index.is_absolute():
        index = root / index
    try:
        info = index.stat()
        index_doc = {
            "device": info.st_dev,
            "file_id": info.st_ino,
            "size": info.st_size,
            "content": _hash_file(index, max_bytes=MAX_FILE_BYTES),
        }
    except OSError:
        index_doc = {"missing": True}
    return head, _canonical_digest(index_doc), _digest(status)


def _scan(root: Path) -> Iterable[ManifestEntry]:
    candidates: list[tuple[bytes, Path]] = []
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        names[:] = [name for name in names if not (current == root and name == ".git")]
        for name in (*names, *files):
            if current == root and name == ".git":
                continue
            path = current / name
            try:
                relative = os.fsencode(path.relative_to(root))
            except (OSError, UnicodeError, ValueError):
                raise AgentError("workspace_path_unrepresentable") from None
            if len(relative) > MAX_PATH_BYTES:
                raise AgentError("workspace_path_limit_exceeded")
            if path.is_dir() and not path.is_symlink():
                continue
            candidates.append((relative.replace(b"\\", b"/"), path))
    candidates.sort(key=lambda item: item[0])
    if len(candidates) > MAX_ENTRIES:
        raise AgentError("workspace_entry_limit_exceeded")
    total = 0
    for relative, path in candidates:
        try:
            before = path.lstat()
        except OSError:
            raise AgentError("workspace_changed_during_digest") from None
        mode = stat.S_IMODE(before.st_mode)
        if stat.S_ISLNK(before.st_mode):
            target = os.fsencode(os.readlink(path))
            data = target
            kind = "symlink"
        elif stat.S_ISREG(before.st_mode):
            if before.st_size > MAX_FILE_BYTES:
                raise AgentError("workspace_file_limit_exceeded")
            data = _read_nofollow(path, before)
            kind = "file"
        else:
            raise AgentError("workspace_entry_type_unsupported")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise AgentError("workspace_total_limit_exceeded")
        try:
            after = path.lstat()
        except OSError:
            raise AgentError("workspace_changed_during_digest") from None
        if _stat_identity(before) != _stat_identity(after):
            raise AgentError("workspace_changed_during_digest")
        yield ManifestEntry(relative, kind, mode, len(data), _digest(data))


def _read_nofollow(path: Path, expected: os.stat_result) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            actual = os.fstat(descriptor)
            if _stat_identity(expected) != _stat_identity(actual):
                raise AgentError("workspace_changed_during_digest")
            chunks: list[bytes] = []
            remaining = MAX_FILE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > MAX_FILE_BYTES:
                raise AgentError("workspace_file_limit_exceeded")
            return data
        finally:
            os.close(descriptor)
    except AgentError:
        raise
    except OSError:
        raise AgentError("workspace_changed_during_digest") from None


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev), int(value.st_ino), int(value.st_mode),
        int(value.st_size), int(value.st_mtime_ns),
    )


def _manifest_digest(entries: Iterable[ManifestEntry]) -> str:
    return _canonical_digest([entry.document() for entry in entries])


def _resolve_git(value: str | None) -> str:
    candidate = value or shutil.which("git")
    if not candidate:
        raise AgentError("git_executable_unavailable")
    resolved = Path(candidate).resolve()
    if not resolved.is_absolute() or not resolved.is_file():
        raise AgentError("git_executable_invalid")
    return str(resolved)


def _git_bytes(root: Path, git: str, *arguments: str) -> bytes:
    environment = {
        "PATH": str(Path(git).parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GIT_PAGER": "cat",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    }
    result = run_bounded(
        [git, "-c", "core.hooksPath=", *arguments], cwd=root,
        environment=environment, timeout=GIT_TIMEOUT_SECONDS,
        output_limit=MAX_TOTAL_BYTES + 1024 * 1024,
        failure_code="git_content_identity_failed",
        output_limit_code="git_content_output_limit",
    )
    if result.returncode != 0:
        raise AgentError("git_content_identity_failed")
    return bytes(result.stdout)


def _commit(value: str) -> str:
    if len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        raise AgentError("invalid_base_commit")
    return value


def _hash_file(path: Path, *, max_bytes: int) -> str:
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise AgentError("workspace_file_limit_exceeded")
        return _digest(path.read_bytes())
    except AgentError:
        raise
    except OSError:
        raise AgentError("workspace_changed_during_digest") from None


def _canonical_digest(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _digest(data)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "ContentSnapshot", "ManifestEntry", "RepoPrestate", "capture_repository",
    "repository_identity",
]
