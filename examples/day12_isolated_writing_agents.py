"""D12 walkthrough: per-agent worktrees, integration, conflict, gated delivery.

Run from ``v2/`` with ``PYTHONPATH=src`` (needs a local ``git`` binary):

    python -B examples/day12_isolated_writing_agents.py

The container step uses the injected host-bootstrap runner; real Docker wiring
remains env-gated (D8 runtime).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.workspace.container import InjectedContainerRunner
from koawa_agent_v2.workspace.store import AgentWorkspaceStore
from koawa_agent_v2.workspace.integration import Artifact, ArtifactIntegrator
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.workspace.worktree import WorktreeManager


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout


def main() -> dict:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "demo@example.com")
    _git(repo, "config", "user.name", "demo")
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    (repo / "b.txt").write_text("beta\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()

    managed = root / "managed"
    managed.mkdir()
    store = AgentWorkspaceStore(
        SqliteEventStore(root / "day12.sqlite3"), managed_root=managed
    )
    manager = WorktreeManager(store, repo_root=repo)
    artifacts: list[Artifact] = []
    for name, content in (("a", "alpha-agent-a\n"), ("b", "beta-agent-b\n")):
        agent_id = uuid4()
        run_id = uuid4()
        tree = Path(
            manager.create(
                agent_id,
                run_id=run_id,
                base_commit=base,
                branch=f"agent/{name}",
                write_agent=True,
            )
        )
        (tree / f"{name}.txt").write_text(content, encoding="utf-8")
        diff = manager.diff(agent_id, base_commit=base)
        artifacts.append(
            Artifact(agent_id, run_id, base, base, diff, "ok", "injected-test-image")
        )

    integrator = ArtifactIntegrator(
        repo_root=repo,
        integration_root=managed / "integration",
        runner=InjectedContainerRunner(),
    )
    for artifact in artifacts:
        integrator.accept(
            artifact,
            expected_run_id=artifact.run_id,
            expected_base_commit=base,
        )
    integrator.integrate(
        artifacts,
        test_argv=[sys.executable, "-c", "pass"],
    )
    integrator.deliver(artifacts, user_base_commit=base)
    delivered = {
        "a.txt": (repo / "a.txt").read_text(encoding="utf-8").strip(),
        "b.txt": (repo / "b.txt").read_text(encoding="utf-8").strip(),
    }
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "delivered")
    base2 = _git(repo, "rev-parse", "HEAD").strip()

    # Conflict branch: two agents editing the same line.
    conflicting: list[Artifact] = []
    for value in ("conflict-one\n", "conflict-two\n"):
        agent_id = uuid4()
        run_id = uuid4()
        tree = Path(
            manager.create(
                agent_id,
                run_id=run_id,
                base_commit=base2,
                branch=f"agent/conflict-{len(conflicting)}",
                write_agent=True,
            )
        )
        (tree / "a.txt").write_text(value, encoding="utf-8")
        diff = manager.diff(agent_id, base_commit=base)
        conflicting.append(
            Artifact(agent_id, run_id, base2, base2, diff, "ok", "injected-test-image")
        )
    conflict_raised = False
    try:
        integrator.integrate(
            conflicting,
            test_argv=[sys.executable, "-c", "pass"],
        )
    except Exception as error:
        conflict_raised = getattr(error, "code", None) == "artifact_conflict"
    assert conflict_raised

    temporary.cleanup()
    return {
        "storage": {"temporary": True, "external_services": ["git"]},
        "delivered": delivered,
        "same_line_conflict": "artifact_conflict",
        "all_assertions_passed": True,
    }


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2, sort_keys=True))
    sys.exit(0)
