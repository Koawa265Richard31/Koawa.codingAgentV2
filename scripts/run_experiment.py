"""Experiment runner: resets the S-scenario fixture and runs one turn.

Usage: PYTHONPATH=src py -3.13 -B scripts/run_experiment.py --fixture <dir>
    --config <cfg.json> --task <text> --label <name>

Resets the repo working tree to the buggy baseline commit and removes the
previous state DB so each experiment starts from the same fixture.  The API
key comes from the environment (never printed or persisted).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, help="experiment fixture dir")
    parser.add_argument("--config", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--label", required=True)
    arguments = parser.parse_args()

    fixture = Path(arguments.fixture)
    repo = fixture / "repo"
    subprocess.run(
        ("git", "-C", str(repo), "checkout", "-q", "--", "."),
        check=True,
    )
    state = fixture / "state"
    state.mkdir(exist_ok=True)
    for stale in state.glob("agent.sqlite3*"):
        stale.unlink()

    from koawa_agent_v2.runtime.cli import main

    out = fixture / f"{arguments.label}-run.json"
    print(f"[{arguments.label}] repo reset, state cleared -> {out}")
    sys.argv = [
        "koawa-agent-v2",
        "run",
        "--config",
        arguments.config,
        "--task",
        arguments.task,
    ]
    code = main()
    Path(out).write_text(json.dumps({"exit_code": code}), encoding="utf-8")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
