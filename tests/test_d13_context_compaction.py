from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.context.compaction import (
    AuthoritativeProjection,
    Compactor,
    ToolCallPair,
    rebuild_after_restart,
)
from koawa_agent_v2.context.budget import ContextBudget
from koawa_agent_v2.context.retrieval import ContextRetriever
from koawa_agent_v2.context.index import IndexLimits, RepositoryIndex


def _git(cwd: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)


class D13ContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (self.repo / "ignored.txt").write_text("secret\n", encoding="utf-8")
        (self.repo / "src.py").write_text("def target():\n    pass\n" * 20, encoding="utf-8")
        (self.repo / "notes.md").write_text("plan: target\n", encoding="utf-8")
        (self.repo / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "base")

    def test_index_respects_ignore_and_limits(self) -> None:
        index = RepositoryIndex(self.repo)
        files = index.list_files()
        names = {item.path for item in files}
        self.assertNotIn("ignored.txt", names)
        self.assertIn("src.py", names)
        self.assertIn("blob.bin", names)
        small = RepositoryIndex(
            self.repo,
            limits=IndexLimits(max_files=1),
        )
        with self.assertRaises(AgentError) as raised:
            small.list_files()
        self.assertEqual("index_file_limit_exceeded", raised.exception.code)
        filtered = RepositoryIndex(self.repo, include=(r"\.py$",))
        self.assertEqual(1, len(filtered.list_files()))

    def test_stale_invalidation(self) -> None:
        index = RepositoryIndex(self.repo)
        files = index.list_files()
        target = next(item for item in files if item.path == "src.py")
        self.assertFalse(index.is_stale(target))
        (self.repo / "src.py").write_text("changed\n", encoding="utf-8")
        self.assertTrue(index.is_stale(target))

    def test_retrieval_orders_and_dedupes(self) -> None:
        index = RepositoryIndex(self.repo)
        retriever = ContextRetriever(
            index,
            budget=ContextBudget(max_chars=10_000, max_items=10),
        )
        items = retriever.retrieve(query="target", files=index.list_files())
        self.assertTrue(items)
        self.assertTrue(all(item.sha256 for item in items))
        self.assertEqual(items[0].path, "src.py")
        keys = {(item.path, item.sha256) for item in items}
        self.assertEqual(len(keys), len(items))
        self.assertTrue(all(item.source == "repository" for item in items))

    def test_compaction_preserves_authority_and_rejects_open_calls(self) -> None:
        compactor = Compactor(
            system_instructions="system",
            developer_instructions="developer",
        )
        projection = AuthoritativeProjection(
            user_goal="fix bug",
            constraints=("no network",),
            changed_files=("src.py",),
            test_evidence="1 passed",
            pending_approval="approval-id-1",
            unknown_outcome="exec-9",
            active_children=("agent-1",),
            budget="50%",
        )
        compacted = compactor.compact(
            summary="fixed bug",
            projection=projection,
            pairs=(ToolCallPair("call-1", "read_file", True),),
        )
        self.assertIn("[system]\nsystem", compacted)
        self.assertIn("[untrusted-model-summary]", compacted)
        self.assertIn("pending_approval=approval-id-1", compacted)
        self.assertIn("unknown_outcome=exec-9", compacted)
        self.assertIn("active_children=agent-1", compacted)
        with self.assertRaises(AgentError) as raised:
            compactor.compact(
                summary="half",
                projection=projection,
                pairs=(ToolCallPair("call-1", "patch", False),),
            )
        self.assertEqual("unresolved_tool_call", raised.exception.code)

    def test_restart_rebuild_is_deterministic(self) -> None:
        projection = AuthoritativeProjection(
            user_goal="g",
            constraints=(),
            changed_files=(),
            test_evidence="",
            pending_approval=None,
            unknown_outcome="exec-7",
            active_children=(),
            budget="10%",
        )
        first = rebuild_after_restart(
            summary="s", projection=projection, tail_events=("turn.started.v1",)
        )
        second = rebuild_after_restart(
            summary="s", projection=projection, tail_events=("turn.started.v1",)
        )
        self.assertEqual(first, second)
        self.assertIn("unknown_outcome=exec-7", first)


if __name__ == "__main__":
    unittest.main()
