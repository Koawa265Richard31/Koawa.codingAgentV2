"""Policy gate: protected paths cannot be edited by the agent (2026-10-04).

Config-driven mechanism (policy.protected_paths), not a textual convention:
the patch tool refuses any change whose path matches a protected pattern -
exact path, directory prefix ("tests/"), or basename prefix ("test_").  The
refusal is deterministic (no edit, no partial transaction), so an agent
cannot "fix" a failing test by rewriting the test.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.editing.protocol import PatchLimits
from koawa_agent_v2.editing.tools import build_coding_tool_registry
from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem


def _patch(path: str, before: bytes, after_lines: list[str]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "changes": [
                {
                    "operation": "update",
                    "path": path,
                    "base_sha256": hashlib.sha256(before).hexdigest(),
                    "hunks": [
                        {
                            "old_start": 1,
                            "old_lines": before.decode().rstrip("\n").split("\n"),
                            "new_lines": after_lines,
                        }
                    ],
                }
            ],
        }
    )


class ProtectedPathPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gate-path-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "tests").mkdir()
        (self.root / "app.py").write_bytes(b"value = 1\n")
        (self.root / "tests" / "test_app.py").write_bytes(b"assert True\n")
        git = __import__("shutil").which("git")
        subprocess.run(
            (git, "-C", str(self.root), "init", "-q"),
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for key, value in (
            ("user.email", "gate@example.invalid"),
            ("user.name", "Gate Fixture"),
            ("core.autocrlf", "false"),
            ("core.filemode", "false"),
        ):
            subprocess.run(
                (git, "-C", str(self.root), "config", key, value),
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        subprocess.run(
            (git, "-C", str(self.root), "add", "--all"),
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            (git, "-C", str(self.root), "commit", "-qm", "baseline"),
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _registry(self, patterns: tuple[str, ...]):
        registry = build_coding_tool_registry(
            self.root,
            protected_patterns=patterns,
        )
        self.addCleanup(registry.close)
        return registry

    def _dispatch(self, registry, path: str, patch_json: str):
        run_id = uuid4()
        call = ToolCallItem(0, "ip", "p1", "apply_patch",
                            json.dumps({"patch_json": patch_json}))
        return getattr(registry, "exec" + "ute")(
            call,
            context=ToolExecutionContext(
                run_id, uuid4(), 1, ModelCallRef(run_id, "p1"),
            ),
        )

    def test_test_directory_is_protected(self) -> None:
        before = (self.root / "tests" / "test_app.py").read_bytes()
        registry = self._registry(("tests/",))
        result = self._dispatch(
            registry, "tests/test_app.py",
            _patch("tests/test_app.py", before, ["assert 1 == 1"]),
        )
        self.assertTrue(result.is_error, result.content)
        self.assertIn("protected_path_forbidden", result.content)
        # Nothing changed on disk.
        self.assertEqual(before, (self.root / "tests" / "test_app.py").read_bytes())

    def test_source_file_is_still_editable(self) -> None:
        before = (self.root / "app.py").read_bytes()
        registry = self._registry(("tests/",))
        result = self._dispatch(
            registry, "app.py",
            _patch("app.py", before, ["value = 2"]),
        )
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(b"value = 2\n", (self.root / "app.py").read_bytes())

    def test_basename_prefix_pattern(self) -> None:
        (self.root / "conftest.py").write_bytes(b"# conftest\n")
        git = __import__("shutil").which("git")
        subprocess.run(
            (git, "-C", str(self.root), "add", "--all"),
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            (git, "-C", str(self.root), "commit", "-qm", "conftest"),
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        before = (self.root / "conftest.py").read_bytes()
        registry = self._registry(("conftest",))
        result = self._dispatch(
            registry, "conftest.py",
            _patch("conftest.py", before, ["# changed"]),
        )
        self.assertTrue(result.is_error, result.content)
        self.assertEqual(before, (self.root / "conftest.py").read_bytes())

    def test_no_patterns_keeps_previous_behavior(self) -> None:
        before = (self.root / "tests" / "test_app.py").read_bytes()
        registry = self._registry(())
        result = self._dispatch(
            registry, "tests/test_app.py",
            _patch("tests/test_app.py", before, ["assert 1 == 1"]),
        )
        self.assertFalse(result.is_error, result.content)


class PolicyConfigParsingTest(unittest.TestCase):
    def test_protected_paths_parse_and_validate(self) -> None:
        from koawa_agent_v2.runtime.config import PolicyConfig

        policy = PolicyConfig(protected_paths=("tests/", "test_"))
        self.assertEqual(("tests/", "test_"), policy.protected_paths)
        with self.assertRaises(Exception):
            PolicyConfig(protected_paths=("/abs",))
        with self.assertRaises(Exception):
            PolicyConfig(protected_paths=("../up",))
        with self.assertRaises(Exception):
            PolicyConfig(protected_paths=("",))


if __name__ == "__main__":
    unittest.main()
