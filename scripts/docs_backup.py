"""Snapshot docs/ onto the local-only backup branch (never pushed).

Usage:
    python scripts/docs_backup.py "snapshot message"

Mechanics:
- Uses a TEMPORARY index (GIT_INDEX_FILE) so the current branch and the main
  index are untouched - no branch switch, no interference with in-flight work.
- Commits ONLY docs/ onto refs/heads/local/full-backup.
- The branch is never pushed (a push guard is configured on it); it exists for
  local backup and per-snapshot rollback of documentation.
"""

from __future__ import annotations

import os
import subprocess
import sys

BACKUP = "local/full-backup"


def _run(args: list[str], env: dict | None = None) -> None:
    subprocess.run(args, check=True, env=env)


def _out(args: list[str], env: dict | None = None) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


def main() -> int:
    message = sys.argv[1] if len(sys.argv) > 1 else "docs backup snapshot"
    repo = _out(["git", "rev-parse", "--show-toplevel"])
    index = os.path.join(repo, ".git", "backup-index")
    env = dict(os.environ, GIT_INDEX_FILE=index)

    # 建分支(若不存在):基点 = 当前 HEAD
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", BACKUP],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        head = _out(["git", "rev-parse", "HEAD"])
        _run(["git", "branch", BACKUP, head])
        print(f"created {BACKUP} at {head[:12]}")

    # 若临时索引过期(与分支头不一致),重新加载
    _run(["git", "read-tree", BACKUP], env=env)

    # docs/ 已在 .gitignore,备份快照需要 -f
    _run(["git", "add", "-f", "docs/"], env=env)

    tree = _out(["git", "write-tree"], env=env)
    parent = _out(["git", "rev-parse", BACKUP])
    prev_tree = _out(["git", "rev-parse", f"{BACKUP}^{{tree}}"])
    if tree == prev_tree:
        print("no docs changes since the last snapshot")
        return 0

    commit = _out(["git", "commit-tree", tree, "-p", parent, "-m", message], env=env)
    _run(["git", "update-ref", f"refs/heads/{BACKUP}", commit])
    try:
        os.remove(index)
    except OSError:
        pass
    print(f"backup commit: {commit[:12]} on {BACKUP}")
    print("rollback: git checkout <backup-commit> -- docs/  或  git diff <commit>..HEAD -- docs/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
