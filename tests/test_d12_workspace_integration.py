from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.workspace.container import (
    DockerContainerRunner,
    InjectedContainerRunner,
)
from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.workspace.store import AgentWorkspaceStore
from koawa_agent_v2.workspace.integration import Artifact, ArtifactIntegrator
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.sandbox.runtime import DockerSandboxDoctor
from koawa_agent_v2.workspace.worktree import WorktreeManager


IMAGE_ID = "sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    (repo / "b.txt").write_text("beta\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


class D12WorkspaceIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.root = root
        self.repo = _make_repo(root)
        self.managed = root / "managed"
        self.managed.mkdir()
        self.store = AgentWorkspaceStore(
            SqliteEventStore(root / "d12.sqlite3"),
            managed_root=self.managed,
        )
        self.manager = WorktreeManager(self.store, repo_root=self.repo)
        self.base = _git(self.repo, "rev-parse", "HEAD").strip()

    def _agent(self, name: str) -> tuple[object, Path]:
        agent_id = uuid4()
        run_id = uuid4()
        worktree = self.manager.create(
            agent_id,
            run_id=run_id,
            base_commit=self.base,
            branch=f"agent/{name}",
            write_agent=True,
        )
        return (agent_id, run_id, Path(worktree))

    def test_worktree_allocate_and_safe_reap(self) -> None:
        agent_id = uuid4()
        run_id = uuid4()
        path = Path(
            self.manager.create(
                agent_id,
                run_id=run_id,
                base_commit=self.base,
                branch="agent/one",
                write_agent=False,
            )
        )
        self.assertTrue(path.exists())
        self.assertTrue(str(path.resolve()).startswith(str(self.managed.resolve())))
        record = self.manager.reap(agent_id, run_id=run_id, reason="test")
        self.assertEqual("reaped", record.state)
        self.assertFalse(path.exists())

    def test_dirty_user_baseline_rejects_write_agents(self) -> None:
        (self.repo / "a.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(AgentError) as raised:
            self.manager.create(
                uuid4(),
                run_id=uuid4(),
                base_commit=self.base,
                branch="agent/write",
                write_agent=True,
            )
        self.assertEqual("dirty_user_worktree_requires_snapshot", raised.exception.code)
        path = self.manager.create(
            uuid4(),
            run_id=uuid4(),
            base_commit=self.base,
            branch="agent/read",
            write_agent=False,
        )
        self.assertTrue(Path(path).exists())

    def test_two_agents_different_files_integrate_and_deliver(self) -> None:
        agent_a, run_a, tree_a = self._agent("a")
        agent_b, run_b, tree_b = self._agent("b")
        (tree_a / "a.txt").write_text("alpha-a\n", encoding="utf-8")
        (tree_b / "b.txt").write_text("beta-b\n", encoding="utf-8")
        diff_a = self.manager.diff(agent_a, base_commit=self.base)
        diff_b = self.manager.diff(agent_b, base_commit=self.base)
        head_a = _git(tree_a, "rev-parse", "HEAD").strip()
        head_b = _git(tree_b, "rev-parse", "HEAD").strip()
        artifact_a = Artifact(agent_a, run_a, self.base, head_a, diff_a, "ok", "img")
        artifact_b = Artifact(agent_b, run_b, self.base, head_b, diff_b, "ok", "img")
        integrator = ArtifactIntegrator(
            repo_root=self.repo,
            integration_root=self.managed / "integration",
            runner=InjectedContainerRunner(),
        )
        accepted = [
            integrator.accept(
                artifact_a,
                expected_run_id=run_a,
                expected_base_commit=self.base,
            ),
            integrator.accept(
                artifact_b,
                expected_run_id=run_b,
                expected_base_commit=self.base,
            ),
        ]
        self.assertEqual(2, len(set(accepted)))
        _, head = integrator.integrate(
            [artifact_a, artifact_b],
            test_argv=[sys.executable, "-c", "pass"],
        )
        integrator.deliver([artifact_a, artifact_b], user_base_commit=self.base)
        self.assertEqual("alpha-a\n", (self.repo / "a.txt").read_text(encoding="utf-8"))
        self.assertEqual("beta-b\n", (self.repo / "b.txt").read_text(encoding="utf-8"))

    def test_same_line_conflict_is_explicit(self) -> None:
        agent_a, run_a, tree_a = self._agent("a")
        agent_b, run_b, tree_b = self._agent("b")
        (tree_a / "a.txt").write_text("conflict-a\n", encoding="utf-8")
        (tree_b / "a.txt").write_text("conflict-b\n", encoding="utf-8")
        diff_a = self.manager.diff(agent_a, base_commit=self.base)
        diff_b = self.manager.diff(agent_b, base_commit=self.base)
        artifact_a = Artifact(agent_a, run_a, self.base, self.base, diff_a, "ok", "img")
        artifact_b = Artifact(agent_b, run_b, self.base, self.base, diff_b, "ok", "img")
        integrator = ArtifactIntegrator(
            repo_root=self.repo,
            integration_root=self.managed / "integration",
            runner=InjectedContainerRunner(),
        )
        with self.assertRaises(AgentError) as raised:
            integrator.integrate(
                [artifact_a, artifact_b],
                test_argv=[sys.executable, "-c", "pass"],
            )
        self.assertTrue(raised.exception.code.startswith("artifact_conflict"))

    def test_stale_run_artifact_is_fenced(self) -> None:
        agent_a, run_a, tree_a = self._agent("a")
        (tree_a / "a.txt").write_text("new\n", encoding="utf-8")
        diff = self.manager.diff(agent_a, base_commit=self.base)
        artifact = Artifact(agent_a, run_a, self.base, self.base, diff, "ok", "img")
        integrator = ArtifactIntegrator(
            repo_root=self.repo,
            integration_root=self.managed / "integration",
            runner=InjectedContainerRunner(),
        )
        with self.assertRaises(AgentError) as raised:
            integrator.accept(
                artifact,
                expected_run_id=uuid4(),
                expected_base_commit=self.base,
            )
        self.assertEqual("artifact_run_fenced", raised.exception.code)

    def test_delivery_requires_exact_head(self) -> None:
        agent_a, run_a, tree_a = self._agent("a")
        (tree_a / "a.txt").write_text("drift\n", encoding="utf-8")
        diff = self.manager.diff(agent_a, base_commit=self.base)
        artifact = Artifact(agent_a, run_a, self.base, self.base, diff, "ok", "img")
        integrator = ArtifactIntegrator(
            repo_root=self.repo,
            integration_root=self.managed / "integration",
            runner=InjectedContainerRunner(),
        )
        (self.repo / "c.txt").write_text("user-change\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "user change")
        with self.assertRaises(AgentError) as raised:
            integrator.deliver([artifact], user_base_commit=self.base)
        self.assertEqual("user_workspace_drift", raised.exception.code)

    def test_docker_runner_executes_in_agent_worktree(self) -> None:
        doctor = DockerSandboxDoctor().check(IMAGE_ID)
        if not doctor.ready:
            self.skipTest(doctor.error_code or "docker_doctor_not_ready")
        _, _, tree = self._agent("docker")
        (tree / "a.txt").write_text("patched\n", encoding="utf-8")
        store = SqliteEventStore(self.root / "container.sqlite3")
        runner = DockerContainerRunner(store, image_id=IMAGE_ID)
        result = runner.run(
            Path(tree),
            ["/bin/sh", "-c", "grep -q patched a.txt"],
        )
        self.assertEqual(0, result.exit_code)
        self.assertEqual(IMAGE_ID, result.image_digest)


if __name__ == "__main__":
    unittest.main()
