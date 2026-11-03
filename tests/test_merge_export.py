"""Positive cases for ``koawa_agent_v2.workspace.merge`` (merge-export slice).

Contract under test (fixed by the survey of the existing slices, not invented
here): exporting a worktree relative to a base commit produces an apply_patch
document plus a 64-char digest.

- The exported ``patch_json`` is a single JSON object with exactly the keys
  ``{"schema_version", "changes"}`` and ``schema_version == 1``
  (editing/protocol.py:194, 199-200).
- Each change uses one of the exact per-operation field sets from
  ``editing/protocol.py`` (ADD: ``operation/path/content/newline/utf8_bom``,
  UPDATE: ``operation/path/base_sha256/hunks``) — extra or missing fields are
  rejected by the protocol (protocol.py:370, 386, 392), so the exporter must
  not emit them.
- Untracked files have no base blob, so they can only be exported as ``add``
  (survey notes, pitfall 3); modified tracked files are exported as ``update``.
- ``base_sha256`` is the sha256 of the **base blob** (the pre-image the
  candidate still holds), not of the child's new bytes: transaction.py:243
  compares it against the file it reads from the target tree, so anchoring it
  to post-change bytes would make every update fail as ``stale_patch_base``.
- Hunk ``old_lines`` must equal the decoded existing file lines character by
  character (protocol.py:294-308), and hunk line arrays carry no ``\\r``/``\\n``
  (protocol.py:442-443), so content travels LF-normalised inside ``content``
  (survey notes, pitfall 1).
- Exporting the same unchanged changes twice yields the identical digest.
- A ``base_commit`` that does not exist in the repository raises ``MergeError``.

The tests build their own throwaway git repositories (``git init``/``git add``/
``git commit``) under ``tempfile`` and never touch the enclosing repository.

Result access is via :func:`_field` because the module is contract-first: the
export result may expose ``patch_json``/``digest`` either as attributes or as
mapping keys; both satisfy this contract.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from koawa_agent_v2.workspace.merge import MergeError, export_patch


_FAKE_BASE_COMMIT = "1234567890abcdef1234567890abcdef12345678"
_HEX_DIGITS = set("0123456789abcdef")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr}"
        )
    return result.stdout


def _write(repo: Path, relative: str, text: str) -> None:
    """Write LF-terminated UTF-8 bytes, immune to platform newline translation."""
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))


def _field(result: object, name: str):
    if isinstance(result, Mapping):
        return result[name]
    return getattr(result, name)


class MergeExportTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo = Path(temporary.name) / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "test")
        _git(repo, "config", "core.autocrlf", "false")
        _git(repo, "config", "commit.gpgsign", "false")

        # Commit 1 and commit 2 differ, so the base commit is unambiguous.
        _write(repo, "tracked.txt", "one\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "first")
        _write(repo, "tracked.txt", "two\n")
        _write(repo, "stable.txt", "stable\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "second")

        self.repo = repo
        self.base = _git(repo, "rev-parse", "HEAD").strip()

    def _apply_changes(self) -> None:
        """Uncommitted changes on top of the second commit: modify + add."""
        _write(self.repo, "tracked.txt", "two changed\n")
        _write(self.repo, "added.txt", "added\n")

    def test_export_covers_modified_and_added_paths_with_valid_document(
        self,
    ) -> None:
        self._apply_changes()
        result = export_patch(worktree_path=self.repo, base_commit=self.base)

        patch_json = _field(result, "patch_json")
        self.assertIsInstance(patch_json, str)
        document = json.loads(patch_json)

        # Top level is exactly the protocol document shape.
        self.assertEqual({"schema_version", "changes"}, set(document.keys()))
        self.assertEqual(1, document["schema_version"])
        changes = document["changes"]
        self.assertIsInstance(changes, list)
        self.assertEqual(2, len(changes))

        by_path = {change["path"]: change for change in changes}
        # Both touched paths are covered; the untouched file is not.
        self.assertEqual({"tracked.txt", "added.txt"}, set(by_path.keys()))
        self.assertNotIn("stable.txt", by_path)

        # The tracked modification is exported as an update with the exact
        # UPDATE field set and a base_sha256 anchored to the BASE blob.
        update = by_path["tracked.txt"]
        self.assertEqual("update", update["operation"])
        self.assertEqual(
            {"operation", "path", "base_sha256", "hunks"}, set(update.keys())
        )
        self.assertRegex(update["base_sha256"], r"^[0-9a-f]{64}$")
        # 应用端（transaction.py:243）拿候选区里的旧文件比对 base_sha256，
        # 不是拿子代理改完的新文件；写成新字节会让每次 update 都 stale。
        self.assertEqual(
            hashlib.sha256(b"two\n").hexdigest(), update["base_sha256"]
        )
        # The file on disk is one LF line; old_lines must match it exactly and
        # the hunk must actually change it (patch_no_changes otherwise).
        hunks = update["hunks"]
        self.assertEqual(1, len(hunks))
        hunk = hunks[0]
        self.assertEqual({"old_start", "old_lines", "new_lines"}, set(hunk.keys()))
        self.assertGreaterEqual(hunk["old_start"], 1)
        self.assertEqual(["two"], hunk["old_lines"])
        self.assertEqual(["two changed"], hunk["new_lines"])

        # The untracked file has no base blob, so it can only be an add with
        # the exact ADD field set; content travels LF with encoding declared
        # out of band.
        added = by_path["added.txt"]
        self.assertEqual("add", added["operation"])
        self.assertEqual(
            {"operation", "path", "content", "newline", "utf8_bom"},
            set(added.keys()),
        )
        self.assertEqual("added\n", added["content"])
        self.assertNotIn("\r", added["content"])
        self.assertIn(added["newline"], ("lf", "crlf"))
        self.assertIsInstance(added["utf8_bom"], bool)
        self.assertFalse(added["utf8_bom"])

        digest = _field(result, "digest")
        self.assertIsInstance(digest, str)
        self.assertEqual(64, len(digest))
        self.assertTrue(set(digest) <= _HEX_DIGITS)

    def test_export_same_changes_twice_yields_identical_digest(self) -> None:
        self._apply_changes()
        first = export_patch(worktree_path=self.repo, base_commit=self.base)
        second = export_patch(worktree_path=self.repo, base_commit=self.base)

        first_digest = _field(first, "digest")
        second_digest = _field(second, "digest")
        self.assertIsInstance(first_digest, str)
        self.assertEqual(64, len(first_digest))
        self.assertEqual(first_digest, second_digest)

    def test_export_unknown_base_commit_raises_merge_error(self) -> None:
        with self.assertRaises(MergeError):
            export_patch(
                worktree_path=self.repo,
                base_commit=_FAKE_BASE_COMMIT,
            )


if __name__ == "__main__":
    unittest.main()
