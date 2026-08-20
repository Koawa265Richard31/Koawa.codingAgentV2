"""D13 repository index: ignore rules, binary detection, limits, staleness."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..agents.graph import AgentError


@dataclass(frozen=True, slots=True)
class IndexLimits:
    max_files: int = 10_000
    max_file_bytes: int = 1_000_000
    max_total_bytes: int = 64_000_000


@dataclass(frozen=True, slots=True)
class IndexedFile:
    path: str
    size: int
    sha256: str


class RepositoryIndex:
    """Index a Git repo honoring .gitignore via `git ls-files`."""

    def __init__(
        self,
        repo_root: Path,
        *,
        limits: IndexLimits | None = None,
        include: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
        git_binary: str = "git",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.limits = limits or IndexLimits()
        self.include = tuple(include)
        self.exclude = tuple(exclude)
        self.git_binary = git_binary

    def list_files(self) -> list[IndexedFile]:
        result = self._git("ls-files", "-z")
        names = [item for item in result.split("\x00") if item]
        selected = [
            name
            for name in names
            if self._allowed(name)
        ]
        if len(selected) > self.limits.max_files:
            raise AgentError("index_file_limit_exceeded")
        total = 0
        indexed: list[IndexedFile] = []
        for name in selected:
            path = self.repo_root / name
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size > self.limits.max_file_bytes:
                continue
            if total + size > self.limits.max_total_bytes:
                raise AgentError("index_total_bytes_exceeded")
            total += size
            indexed.append(IndexedFile(name, size, self.hash_file(path)))
        indexed.sort(key=lambda item: item.path)
        return indexed

    def is_stale(self, file: IndexedFile) -> bool:
        path = self.repo_root / file.path
        if not path.is_file() or path.stat().st_size != file.size:
            return True
        return self.hash_file(path) != file.sha256

    def hash_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _allowed(self, name: str) -> bool:
        if self.exclude and any(re.search(pattern, name) for pattern in self.exclude):
            return False
        if self.include and not any(
            re.search(pattern, name) for pattern in self.include
        ):
            return False
        return True

    def _git(self, *arguments: str) -> str:
        try:
            result = subprocess.run(
                [self.git_binary, *arguments],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            raise AgentError("git_index_failed") from None
        if result.returncode != 0:
            raise AgentError("git_index_failed")
        return result.stdout
