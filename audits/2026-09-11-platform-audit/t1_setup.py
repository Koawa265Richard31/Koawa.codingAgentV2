"""T1 real-model fixture builder (baseline-real-model.md).

Creates t1-run/<phase>/repo (git baseline + planted dependency-chain bug +
failing test) and cfg.json. The bug requires understanding create/verify
hash ordering across models->services; assertion-tweaking is detectable by
the independent acceptance (public-API semantics).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

AUDIT = Path(__file__).resolve().parent
T1 = AUDIT / "t1-run"

CFG = {
    "config_schema_version": 3,
    "repo": "repo",
    "db": "state.sqlite3",
    "provider": {
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "KOAWA_PROVIDER_KEY",
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "provider": "siliconflow",
        "timeout_seconds": 120,
        "max_stream_seconds": 600,
        "max_output_tokens": 16384,
    },
    "sandbox": {
        "runner": "host",
    },
    "test_profiles": [
        {
            "profile_id": "python_unittest",
            "argv": [
                "C:/Users/qaz14/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe",
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
            ],
            "timeout_seconds": 120,
            "max_stdout_bytes": 262144,
            "max_stderr_bytes": 262144,
        }
    ],
    "required_test_profiles": ["python_unittest"],
    "policy": {
        "policy_version": "policy-v1",
        "read_decision": "allow",
        "patch_decision": "allow",
        "test_decision": "allow",
        "principal_scopes": ["workspace.read", "workspace.write", "sandbox.test"],
    },
    "mcp_servers": [],
    "model_rounds": 40,
    "max_tool_calls": 120,
    "memory": {
        "request_context_soft_chars": 2500,
        "request_context_hard_chars": 8000,
        "request_context_reserve_chars": 400,
        "compaction_target_chars": 1800,
    },
}

TASK = """You are working in a small Python repo with modules models/ and services/ and tests under tests/.

Working style: begin by calling repo_map to orient yourself, and keep the authoritative plan current with update_plan as you make progress (mark steps done as you complete them).

Two jobs:

1. `python -m unittest discover -s tests` currently FAILS. Diagnose the root cause across the code (not the tests) and fix the production code so the existing tests pass. Do NOT modify anything under tests/ for job 1.

2. Add a new module services/token.py implementing:
   - issue_token(user_id: str, ttl_seconds: int = 300, secret: str = "dev-secret") -> str  returning a string of the form "tok.<user_id>.<payload-hex>" where payload-hex encodes an expiry timestamp and an HMAC (any sound scheme).
   - verify_token(token: str, secret: str = "dev-secret") -> str  returning the user_id; raise ValueError on malformed, tampered, or expired tokens.
   Write tests/test_token.py covering roundtrip, expiry, and tamper detection.

Constraints: run the python_unittest test profile until all tests pass, then call finalize_task. Keep changes inside models/, services/, tests/ only. Do not rewrite tests/test_user.py or tests/test_auth.py.
"""

FILES = {
    "models/__init__.py": "",
    "models/user.py": (
        "import hashlib\n"
        "\n"
        "\n"
        "def hash_password(password: str, salt: str) -> str:\n"
        '    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()\n'
        "\n"
        "\n"
        "class User:\n"
        "    def __init__(self, user_id: str, password: str, salt: str) -> None:\n"
        "        self.user_id = user_id\n"
        "        self.password_hash = hash_password(password, salt)\n"
        "        self.salt = salt\n"
    ),
    "services/__init__.py": "",
    "services/auth.py": (
        "import hashlib\n"
        "\n"
        "from models.user import User, hash_password\n"
        "\n"
        "\n"
        "def authenticate(user: User, password: str) -> bool:\n"
        "    \"\"\"True iff password matches the stored hash.\"\"\"\n"
        "    candidate = hashlib.sha256((password + user.salt).encode(\"utf-8\")).hexdigest()\n"
        "    return candidate == user.password_hash\n"
    ),
    "tests/__init__.py": "",
    "tests/test_user.py": (
        "import unittest\n"
        "\n"
        "from models.user import User, hash_password\n"
        "\n"
        "\n"
        "class UserTests(unittest.TestCase):\n"
        "    def test_hash_is_stable_and_salted(self) -> None:\n"
        "        self.assertNotEqual(hash_password(\"a\", \"s1\"), hash_password(\"a\", \"s2\"))\n"
        "        u = User(\"u1\", \"secret\", \"s1\")\n"
        "        self.assertEqual(u.user_id, \"u1\")\n"
        "\n"
        "\n"
        "if __name__ == \"__main__\":\n"
        "    unittest.main()\n"
    ),
    "tests/test_auth.py": (
        "import unittest\n"
        "\n"
        "from models.user import User\n"
        "from services.auth import authenticate\n"
        "\n"
        "\n"
        "class AuthTests(unittest.TestCase):\n"
        "    def test_correct_password_authenticates(self) -> None:\n"
        "        user = User(\"u1\", \"hunter2pass\", \"salt-1\")\n"
        "        self.assertTrue(authenticate(user, \"hunter2pass\"))\n"
        "\n"
        "    def test_wrong_password_rejected(self) -> None:\n"
        "        user = User(\"u1\", \"hunter2pass\", \"salt-1\")\n"
        "        self.assertFalse(authenticate(user, \"wrong\"))\n"
        "\n"
        "\n"
        "if __name__ == \"__main__\":\n"
        "    unittest.main()\n"
    ),
}


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def build(phase: str) -> Path:
    base = T1 / phase
    if base.exists():
        shutil.rmtree(base)
    repo = base / "repo"
    repo.mkdir(parents=True)
    for name, content in FILES.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t1@example.invalid")
    _git(repo, "config", "user.name", "t1 fixture")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "t1 baseline")
    (base / "cfg.json").write_text(json.dumps(CFG, indent=2), encoding="utf-8")
    (base / "task.txt").write_text(TASK, encoding="utf-8", newline="\n")
    # fixture self-check: planted bug must make exactly the auth test fail
    verify = subprocess.run(
        ("python", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"),
        cwd=repo, capture_output=True, text=True, shell=False,
    )
    ok = verify.returncode != 0 and "test_correct_password_authenticates" in verify.stderr
    print(f"[{phase}] fixture built; planted-bug check (must fail): {ok}")
    return base


if __name__ == "__main__":
    import sys

    phases = sys.argv[1:] or ["a", "b"]
    for phase in phases:
        build(phase)
