"""Audit evidence snapshot: sha256 (first 16 hex) of files this round's
conclusions bind to, plus HEAD. Read-only over the working tree."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

FILES = [
    "src/koawa_agent_v2/runtime/assembly.py",
    "src/koawa_agent_v2/runtime/config.py",
    "src/koawa_agent_v2/verification/finalization.py",
    "src/koawa_agent_v2/verification/tools.py",
    "src/koawa_agent_v2/editing/transaction.py",
    "src/koawa_agent_v2/verification/git.py",
    "tests/test_d5_vertical_slice.py",
    "tests/test_runtime_config.py",
    "tests/test_d5_required_profiles.py",
    "README.md",
]


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    out = {"head": subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()}
    for name in FILES:
        out[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()[:16]
    target = root / "audits" / "2026-09-11-platform-audit" / "file-hashes-20260911.json"
    target.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
