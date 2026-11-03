"""F8 合并边界:导出端的拒绝面,以及"导出的补丁真的能被应用"。

前一版有两份重复的导出实现,其中一份把 ``base_sha256`` 写成了子代理改完之后
的字节。所有测试都只校验 JSON 形状与摘要,所以没人发现——那份补丁在应用端
会 100% 抛 ``stale_patch_base``。这里补两类用例:

1. 导出端的每个拒绝码各一条;
2. 一条真的把导出的补丁应用到 base 候选目录上的集成用例。
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.editing.protocol import PatchLimits, parse_patch_document
from koawa_agent_v2.editing.transaction import AtomicPatchWorkspace
from koawa_agent_v2.tools.workspace import WorkspacePathResolver
from koawa_agent_v2.workspace.merge import (
    MergeError,
    export_patch,
    export_worktree_patch,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr}"
        )
    return result.stdout


def _write(repo: Path, relative: str, data: bytes) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


class _RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo = Path(temporary.name) / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "test")
        _git(repo, "config", "core.autocrlf", "false")
        _git(repo, "config", "commit.gpgsign", "false")
        _write(repo, "tracked.txt", b"one\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "first")
        _write(repo, "tracked.txt", b"two\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "second")
        self.repo = repo
        self.base = _git(repo, "rev-parse", "HEAD").strip()

    def _code(self, **kwargs) -> str:
        with self.assertRaises(MergeError) as raised:
            export_patch(
                worktree_path=kwargs.pop("worktree_path", self.repo),
                base_commit=kwargs.pop("base_commit", self.base),
            )
        return raised.exception.code


class ExportRejectionTest(_RepoCase):
    def test_clean_worktree_is_refused(self) -> None:
        self.assertEqual("merge_no_changes", self._code())

    def test_malformed_base_commit_is_refused(self) -> None:
        self.assertEqual("invalid_merge_base_commit", self._code(base_commit="HEAD"))

    def test_missing_worktree_is_refused(self) -> None:
        ghost = self.repo.parent / "does-not-exist"
        self.assertEqual(
            "merge_worktree_unavailable", self._code(worktree_path=ghost)
        )

    def test_binary_file_is_refused(self) -> None:
        _write(self.repo, "blob.bin", b"abc\x00def")
        self.assertEqual("merge_binary_file_unsupported", self._code())

    def test_rename_is_refused(self) -> None:
        _git(self.repo, "mv", "tracked.txt", "moved.txt")
        self.assertEqual("merge_rename_unsupported", self._code())

    def test_head_ahead_of_base_is_refused(self) -> None:
        _write(self.repo, "tracked.txt", b"three\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "third")
        self.assertEqual("merge_base_commit_mismatch", self._code())


class ExportAppliesTest(_RepoCase):
    """导出的补丁必须真的能应用到"仍在 base 版本"的候选目录上。"""

    def test_exported_patch_applies_to_base_candidate(self) -> None:
        # 子代理侧:未提交的修改 + 新增文件。
        _write(self.repo, "tracked.txt", b"two changed\n")
        _write(self.repo, "added.txt", b"added\n")

        request = export_worktree_patch(
            worktree=self.repo,
            base_commit=self.base,
            source_agent_id=uuid4(),
            run_id=uuid4(),
        )

        # 父级侧:一个只含 base 版本的候选目录(不是 git 仓库)。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        candidate = Path(temporary.name) / "candidate"
        candidate.mkdir()
        _write(candidate, "tracked.txt", b"two\n")

        resolver = WorkspacePathResolver(candidate)
        self.addCleanup(resolver.close)
        workspace = AtomicPatchWorkspace(candidate, resolver)
        patch = parse_patch_document(request.patch_json, PatchLimits())
        workspace.apply(patch)

        self.assertEqual(b"two changed\n", (candidate / "tracked.txt").read_bytes())
        self.assertEqual(b"added\n", (candidate / "added.txt").read_bytes())


if __name__ == "__main__":
    unittest.main()
