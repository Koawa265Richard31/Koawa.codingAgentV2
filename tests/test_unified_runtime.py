from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.runtime.unified import UnifiedAgentRuntime


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


class UnifiedRuntimeTest(unittest.TestCase):
    def test_unified_flow_wires_context_subagent_compaction_trace(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo = root / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        (repo / "src.py").write_text(
            "def locate():\n    return 'target'\n" * 5, encoding="utf-8"
        )
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "base")

        runtime = UnifiedAgentRuntime(db=root / "u.sqlite3", repo=repo)
        result = runtime.execute("locate")

        self.assertEqual("completed", result.turn_status)
        self.assertIn("src.py", result.context_items)
        self.assertEqual(("completed",), result.child_states)
        self.assertIn("[untrusted-model-summary]", result.compacted)
        self.assertIn("user_goal=locate", result.compacted)
        self.assertIn("subagent", result.trace_streams)
        self.assertIn("model", result.trace_streams)


if __name__ == "__main__":
    unittest.main()
