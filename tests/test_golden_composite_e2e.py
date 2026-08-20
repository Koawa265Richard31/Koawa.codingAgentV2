from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from koawa_agent_v2.sandbox.runtime import DockerSandboxDoctor


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "tests" / "fixtures" / "golden_worker.py"
IMAGE_ID = "sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout


class GoldenCompositeE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "golden@example.com")
        _git(self.repo, "config", "user.name", "golden")
        (self.repo / "README.md").write_text("golden\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "base")
        self.db = root / "golden.sqlite3"
        self.ready = root / "ready.txt"
        self.state = root / "state.json"
        self.evidence = root / "evidence.json"

    def _env(self, resume: bool) -> dict:
        environment = os.environ.copy()
        configured = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(ROOT / "src"), str(ROOT), configured) if part
        )
        environment["GOLDEN_DB"] = str(self.db)
        environment["GOLDEN_REPO"] = str(self.repo)
        environment["GOLDEN_RESUME"] = "1" if resume else "0"
        environment["GOLDEN_READY_FILE"] = str(self.ready)
        environment["GOLDEN_STATE_FILE"] = str(self.state)
        environment["GOLDEN_EVIDENCE_FILE"] = str(self.evidence)
        if not resume:
            environment["GOLDEN_KILL_POINT"] = "after_claim"
        return environment

    def _spawn(self, resume: bool) -> subprocess.Popen:
        kwargs = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.Popen(
            [sys.executable, "-B", str(WORKER)],
            cwd=ROOT,
            env=self._env(resume),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **kwargs,
        )

    def test_full_composite_kill_and_resume(self) -> None:
        if not DockerSandboxDoctor().check(IMAGE_ID).ready:
            self.skipTest("docker_doctor_not_ready")

        run = self._spawn(resume=False)
        try:
            deadline = time.monotonic() + 45
            while not self.ready.exists() and time.monotonic() < deadline:
                if run.poll() is not None:
                    stdout, stderr = run.communicate()
                    self.fail(f"run phase exited early:\n{stdout}\n{stderr}")
                time.sleep(0.05)
            self.assertTrue(self.ready.exists(), "run phase never reached the kill point")
            run.kill()
            run.communicate(timeout=10)
            self.assertNotEqual(0, run.returncode)
        finally:
            if run.poll() is None:
                run.kill()
                run.communicate(timeout=10)

        resume = self._spawn(resume=True)
        try:
            deadline = time.monotonic() + 45
            while not self.evidence.exists() and time.monotonic() < deadline:
                if resume.poll() is not None:
                    stdout, stderr = resume.communicate()
                    self.fail(f"resume phase exited early:\n{stdout}\n{stderr}")
                time.sleep(0.05)
            self.assertTrue(self.evidence.exists(), "resume phase never produced evidence")
            resume.communicate(timeout=15)
            self.assertEqual(0, resume.returncode)
        finally:
            if resume.poll() is None:
                resume.kill()
                resume.communicate(timeout=10)

        evidence = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertEqual("completed", evidence["status"])
        self.assertEqual("v2", evidence["solution"])
        self.assertEqual("completed", evidence["subagent_state"])
        for stream in ("model", "tool", "ledger", "mcp"):
            self.assertIn(stream, evidence["trace_streams"], stream)
        self.assertIn("tool.execution-succeeded.v1", evidence["ledger_states"])


if __name__ == "__main__":
    unittest.main()
