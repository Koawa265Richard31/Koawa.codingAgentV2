"""D13 walkthrough: indexed context budget, staleness, compaction."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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
from koawa_agent_v2.context.index import RepositoryIndex


def main() -> dict:
    temporary = tempfile.TemporaryDirectory()
    repo = Path(temporary.name) / "repo"
    repo.mkdir()
    for command in (
        ["init", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *command], cwd=str(repo), check=True, capture_output=True)
    (repo / ".gitignore").write_text("cache/\n", encoding="utf-8")
    (repo / "cache").mkdir()
    (repo / "cache" / "junk.bin").write_bytes(b"\x00" * 100)
    (repo / "src.py").write_text(
        "def handler():\n    return 'ok'\n" * 30, encoding="utf-8"
    )
    (repo / "README.md").write_text("handler docs\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=str(repo), check=True, capture_output=True)

    index = RepositoryIndex(repo)
    files = index.list_files()
    retriever = ContextRetriever(index, budget=ContextBudget(max_chars=20_000))
    items = retriever.retrieve(query="handler", files=files)
    assert all(item.path != "cache/junk.bin" for item in items)
    assert any(item.path == "src.py" for item in items)
    stale_before = {item.path: item.sha256 for item in files}
    (repo / "src.py").write_text("changed\n", encoding="utf-8")
    stale = [
        path
        for path, digest in stale_before.items()
        if index.is_stale(next(item for item in files if item.path == path))
    ]
    assert "src.py" in stale

    projection = AuthoritativeProjection(
        user_goal="implement handler",
        constraints=("no network",),
        changed_files=("src.py",),
        test_evidence="2 passed",
        pending_approval=None,
        unknown_outcome="exec-3",
        active_children=(),
        budget="40%",
    )
    compactor = Compactor(system_instructions="system", developer_instructions="developer")
    compacted = compactor.compact(
        summary="implemented handler",
        projection=projection,
        pairs=(ToolCallPair("call-1", "read_file", True),),
    )
    assert "unknown_outcome=exec-3" in compacted
    rebuilt = rebuild_after_restart(
        summary="implemented handler",
        projection=projection,
        tail_events=("tool.execution-succeeded.v1",),
    )
    assert rebuilt == rebuild_after_restart(
        summary="implemented handler",
        projection=projection,
        tail_events=("tool.execution-succeeded.v1",),
    )
    temporary.cleanup()
    return {
        "storage": {"temporary": True, "external_services": ["git"]},
        "indexed_files": len(files),
        "retrieved_paths": [item.path for item in items],
        "stale_detected": stale,
        "compaction_preserves_authority": True,
        "all_assertions_passed": True,
    }


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2, sort_keys=True))
    sys.exit(0)
