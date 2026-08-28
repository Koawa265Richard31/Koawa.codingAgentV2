from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.workspace.artifacts import (
    ArtifactPackageEntry,
    ArtifactPackageRef,
    ArtifactPackageStore,
    package_from_snapshot,
)
from koawa_agent_v2.workspace.content import capture_repository, repository_identity


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class I7ContentIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.name", "i7")
        _git(self.repo, "config", "user.email", "i7@example.invalid")
        _git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "tracked.bin").write_bytes(b"base\x00bytes")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")

    def test_snapshot_captures_binary_diff_and_untracked_content_identity(self) -> None:
        (self.repo / "tracked.bin").write_bytes(b"changed\x00bytes")
        (self.repo / "untracked.txt").write_text("new\n", encoding="utf-8")
        snapshot = capture_repository(self.repo, base_commit=self.base)
        self.assertEqual(self.base, snapshot.prestate.head_commit)
        self.assertIn(b"GIT binary patch", snapshot.diff_bytes)
        self.assertEqual(64, len(snapshot.prestate.prestate_digest))
        self.assertTrue(any(item.path_bytes == b"untracked.txt" for item in snapshot.entries))
        self.assertEqual(64, len(repository_identity(self.repo)))

    def test_symlink_is_hashed_without_following_when_supported(self) -> None:
        link = self.repo / "outside-link"
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        snapshot = capture_repository(self.repo, base_commit=self.base)
        entry = next(item for item in snapshot.entries if item.path_bytes == b"outside-link")
        self.assertEqual("symlink", entry.kind)
        self.assertNotEqual(6, entry.size)


class I7ArtifactPackageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.events = SqliteEventStore(self.root / "events.sqlite3")
        self.store = ArtifactPackageStore(self.root / "state", event_store=self.events)
        sha = "a" * 64
        self.package = package_from_snapshot(
            repository_identity_digest=sha,
            base_commit="b" * 40,
            tracked_binary_patch=b"patch\x00bytes",
            entries=(ArtifactPackageEntry.create(
                path_bytes=b"new.bin", kind="untracked_file", mode=0o644,
                content_or_target=b"content\x00",
            ),),
            prestate_digest="c" * 64,
            poststate_manifest_digest="d" * 64,
        )

    def test_atomic_put_load_and_deterministic_reference(self) -> None:
        first = self.store.put(self.package)
        second = self.store.put(self.package)
        self.assertEqual(first, second)
        self.assertEqual(self.package, self.store.load(first))
        encoded = (self.store.root / f"{first.package_digest}.json").read_bytes()
        self.assertEqual(encoded, json.dumps(json.loads(encoded), ensure_ascii=False,
                                             sort_keys=True, separators=(",", ":")).encode())

    def test_tamper_and_forged_reference_fail_closed(self) -> None:
        reference = self.store.put(self.package)
        target = self.store.root / f"{reference.package_digest}.json"
        target.write_bytes(target.read_bytes() + b" ")
        with self.assertRaises(AgentError) as raised:
            self.store.load(reference)
        self.assertEqual("artifact_package_tampered", raised.exception.code)
        forged = ArtifactPackageRef("sha256:" + "0" * 64, "0" * 64, 1)
        with self.assertRaises(AgentError):
            self.store.load(forged)

    def test_pin_and_release_are_exact_version_events(self) -> None:
        reference = self.store.put(self.package)
        artifact_id = uuid4()
        self.store.pin(artifact_id=artifact_id, package=reference, command_id=uuid4())
        events = self.events.read_stream(
            __import__("koawa_agent_v2.control.event_store", fromlist=["StreamId"]).StreamId(
                "workspace-artifact", artifact_id
            ), after_version=-1, limit=10,
        )
        self.assertEqual("workspace.artifact-package-pinned.v1", events[0].event_type)
        self.assertEqual((), self.store.collect_unpinned())
        self.assertEqual(self.package, self.store.load(reference))
        self.store.release(artifact_id=artifact_id, expected_version=0, command_id=uuid4())
        self.assertEqual((reference.package_ref,), self.store.collect_unpinned())
        with self.assertRaises(AgentError) as raised:
            self.store.load(reference)
        self.assertEqual("artifact_package_missing", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
