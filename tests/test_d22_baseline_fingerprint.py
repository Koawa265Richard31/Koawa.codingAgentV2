"""D22 F2/F3：保护路径内容锚定 + baseline_dirty detail。

幻影 stat 缓存（status 报 " M" 但内容与索引 blob 一致）不得进入保护集合；
真实脏/未跟踪/删除仍受保护。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.editing.protocol import PatchLimits
from koawa_agent_v2.editing.tools import ApplyPatchArguments
from koawa_agent_v2.editing.tools import _ApplyPatchTool
from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef
from koawa_agent_v2.tools.workspace import WorkspacePathResolver
from koawa_agent_v2.verification.git import (
    GitFacade,
    GitStatusEntry,
    GitStatusSnapshot,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                   text=True, check=True)


class BaselineFingerprintTest(unittest.TestCase):
    def make_repo(self) -> tuple[Path, GitFacade]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "repo"
        root.mkdir()
        _git(root, "init", "-b", "main")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "t")
        (root / "README.md").write_text("# D22\n", encoding="utf-8")
        _git(root, "add", "README.md")
        _git(root, "commit", "-m", "base")
        resolver = WorkspacePathResolver(root)
        self.addCleanup(resolver.close)
        facade = GitFacade(root, resolver)
        return root, facade

    def test_clean_repo_has_no_protected_paths(self) -> None:
        _, facade = self.make_repo()
        self.assertEqual(frozenset(), facade.protected_paths)

    def test_touched_clean_file_is_not_protected(self) -> None:
        # 只改 mtime（stat 层假阳性条件），内容与索引 blob 一致 → 不保护。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "repo"
        root.mkdir()
        _git(root, "init", "-b", "main")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "t")
        (root / "README.md").write_text("# x\n", encoding="utf-8")
        _git(root, "add", "README.md")
        _git(root, "commit", "-m", "base")
        os.utime(root / "README.md", (1_700_000_000, 1_700_000_000))
        resolver = WorkspacePathResolver(root)
        self.addCleanup(resolver.close)
        facade = GitFacade(root, resolver)
        self.assertNotIn("readme.md", facade.protected_paths)

    def test_phantom_baseline_entry_is_rejected_even_if_status_lies(self) -> None:
        # 直接喂一个虚假 baseline 条目（" M"），内容与索引 blob 一致 → 不保护。
        _, facade = self.make_repo()
        fake_baseline = GitStatusSnapshot(
            (GitStatusEntry(" M", "README.md"),),
            "digest",
        )
        protected = facade._content_verified_protection(fake_baseline)
        self.assertNotIn("readme.md", protected)

    def test_really_dirty_file_is_protected(self) -> None:
        root, _ = self.make_repo()
        (root / "README.md").write_text("# D22\ndirty\n", encoding="utf-8")
        resolver = WorkspacePathResolver(root)
        self.addCleanup(resolver.close)
        facade = GitFacade(root, resolver)
        self.assertIn("readme.md", facade.protected_paths)

    def test_untracked_file_is_protected(self) -> None:
        root, _ = self.make_repo()
        (root / "user_work.py").write_text("x = 1\n", encoding="utf-8")
        resolver = WorkspacePathResolver(root)
        self.addCleanup(resolver.close)
        facade = GitFacade(root, resolver)
        self.assertIn("user_work.py", facade.protected_paths)

    def test_deleted_tracked_file_is_protected(self) -> None:
        root, _ = self.make_repo()
        (root / "README.md").unlink()
        resolver = WorkspacePathResolver(root)
        self.addCleanup(resolver.close)
        facade = GitFacade(root, resolver)
        self.assertIn("readme.md", facade.protected_paths)

    def test_baseline_dirty_error_carries_detail(self) -> None:
        """F3：保护门命中的错误带稳定 detail（真实保护场景才出现）。"""
        resolver_stub = object()  # gate 命中时不会触达 workspace
        tool = _ApplyPatchTool(
            resolver_stub,  # type: ignore[arg-type]
            PatchLimits(),
            protected_paths=frozenset({"readme.md"}),
        )
        model_turn_id = uuid4()
        context = ToolExecutionContext(
            uuid4(),
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, "call-1"),
            progress_guard=lambda: None,
        )
        patch = json.dumps({
            "schema_version": 1,
            "changes": [
                {"operation": "add", "path": "README.md",
                 "content": "x\n", "newline": "lf", "utf8_bom": False},
            ],
        }, ensure_ascii=False, separators=(",", ":"))
        result = tool(ApplyPatchArguments(patch_json=patch), context=context)
        self.assertTrue(result.is_error)
        parsed = json.loads(result.content)
        self.assertEqual("baseline_dirty_path_forbidden", parsed["error"]["code"])
        self.assertEqual("protected_user_changes:start", parsed["error"].get("detail"))


if __name__ == "__main__":
    unittest.main()
