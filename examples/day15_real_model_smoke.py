"""Real-provider smoke for the P0 assembled runtime.

Creates an isolated temporary Git repository with one intentionally failing test,
then asks a configured OpenAI-compatible model to repair it through the real
D3+D4+D5 registry behind D7/D9.  This is a manual/opt-in smoke test; deterministic
CI never depends on a paid provider.

Usage:

    $env:SF_CodingAgentTestKey = [Environment]::GetEnvironmentVariable(
        'SF_CodingAgentTestKey', 'User')
    $env:PYTHONPATH = 'src'
    py -3.14 -B examples/day15_real_model_smoke.py
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from koawa_agent_v2.runtime.app import AppRuntime
from koawa_agent_v2.runtime.config import (
    PolicyConfig,
    ProviderConfig,
    RuntimeConfig,
    SandboxConfig,
    SandboxRunner,
    TestProfileConfig,
)

DEFAULT_IMAGE_ID = (
    "sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"
)

TASK = (
    "The test suite currently fails. Inspect the repository with read_file, "
    "list_files, or search_text, find the bug, fix it with apply_patch, then run "
    "the python_unittest test profile. After the tests pass, call git_status and "
    "git_diff, then finalize_task."
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo():
    temporary = tempfile.TemporaryDirectory(prefix="koawa-p0-smoke-")
    repo = Path(temporary.name) / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "smoke@example.com")
    _git(repo, "config", "user.name", "Koawa Smoke")
    # Same fixture hygiene as tests: without these, GitFacade's baseline on a
    # freshly-committed fixture can misreport tracked files as dirty when the
    # facade flips fsmonitor/stat-cache mode mid-lifecycle.
    _git(repo, "config", "core.fsmonitor", "false")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "core.filemode", "false")
    (repo / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a - b\n"
        "\n"
        "def multiply(a, b):\n"
        "    return a * b\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "import unittest\n"
        "\n"
        "from calc import add, multiply\n"
        "\n"
        "\n"
        "class CalcTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(5, add(2, 3))\n"
        "\n"
        "    def test_multiply(self):\n"
        "        self.assertEqual(6, multiply(2, 3))\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "failing baseline")
    return repo, temporary


# Verified default on SiliconFlow: Qwen3.5-35B-A3B (cheap MoE) follows the
# tool schemas and is fast; thinking must be disabled or the model answers
# entirely inside reasoning_content, which this provider cannot echo back.
DEFAULT_MODEL = "Qwen/Qwen3.5-35B-A3B"

SYSTEM_PROMPT = """You are KoawaAgent V2, a durable coding agent working inside one Git repository.
Available tools and their EXACT required arguments (integers must be JSON numbers, never strings):

1. list_files(path, max_depth, max_entries)  e.g. {"path": ".", "max_depth": 3, "max_entries": 50}
2. read_file(path, start_line, max_lines)    e.g. {"path": "calc.py", "start_line": 1, "max_lines": 200}
   The response contains sha256: you MUST reuse that exact sha256 in apply_patch.
3. search_text(query, path, max_depth, max_files, max_matches)
4. run_test_profile(profile_id)              e.g. {"profile_id": "python_unittest"}
5. apply_patch(patch_json)                   stringified JSON: {"schema_version": 1, "changes": [{"operation": "update", "path": "calc.py", "base_sha256": "<sha256 from read_file>", "hunks": [{"old_start": 1, "old_lines": ["<exact old lines>"], "new_lines": ["<replacement lines>"]}]}]}
6. git_status() / 7. git_diff() / 8. finalize_task()   — no arguments

Tool results arrive as JSON: {"is_error": false, "content": "<the tool output>"} — parse the inner content.
Protocol: inspect first (list_files/read_file), then apply_patch, then run_test_profile until it passes,
then git_status and git_diff, then finalize_task. Never call apply_patch without a fresh base_sha256.
Always end with a written summary of what you changed and the test evidence."""


def main() -> int:
    model = os.environ.get("KOAWA_SF_MODEL", DEFAULT_MODEL)
    repo, temporary = _make_repo()
    try:
        config = RuntimeConfig(
            repo=repo,
            db=Path(temporary.name) / "smoke.sqlite3",
            provider=ProviderConfig(
                base_url="https://api.siliconflow.cn/v1",
                api_key_env="SF_CodingAgentTestKey",
                model=model,
                reasoning_effort="off",
            ),
            sandbox=SandboxConfig(
                runner=SandboxRunner.DOCKER,
                image_id=os.environ.get(
                    "KOAWA_DOCKER_IMAGE_ID", DEFAULT_IMAGE_ID
                ),
            ),
            test_profiles=(
                TestProfileConfig(
                    "python_unittest",
                    (
                        "/usr/local/bin/python",
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "tests",
                    ),
                    timeout_seconds=120,
                ),
            ),
            policy=PolicyConfig(),
            system_prompt=SYSTEM_PROMPT,
        )
        app = AppRuntime(config)
        outcome = app.run(TASK)
        print(outcome.to_json())
        print(f"repo={config.repo}")
        print(f"db={config.db}")
        return 0 if outcome.ok else 1
    finally:
        temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
