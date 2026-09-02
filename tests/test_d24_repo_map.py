"""D24 W3: repo_map contracts — bounded metadata, no content, no bypass."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.execution.loop import ModelCallRef, ToolExecutionContext
from koawa_agent_v2.model.protocol import ToolCallItem
from koawa_agent_v2.tools.registry import ToolRegistry
from koawa_agent_v2.tools.repo_map import register_repo_map_tool
from koawa_agent_v2.tools.workspace import WorkspacePathResolver


def _context() -> ToolExecutionContext:
    turn = uuid4()
    return ToolExecutionContext(
        run_id=uuid4(),
        model_turn_id=turn,
        model_round=1,
        call_ref=ModelCallRef(model_turn_id=turn, call_id="call-1"),
    )


class RepoMapToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "app.py").write_text(
            "class Loader:\n"
            "    def load(self):\n"
            "        SECRET_BODY_SENTINEL = 'tool output must not leak bodies'\n"
            "        return 1\n"
            "\n"
            "async def fetch():\n"
            "    return 2\n",
            encoding="utf-8",
        )
        (root / "sub").mkdir()
        (root / "sub" / "helper.py").write_text(
            "def util():\n    return 0\n", encoding="utf-8"
        )
        (root / "data.txt").write_text("plain\n", encoding="utf-8")
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        self.resolver = WorkspacePathResolver(root)
        self.registry = ToolRegistry()
        register_repo_map_tool(self.registry, self.resolver)

    def tearDown(self) -> None:
        self.resolver.close()
        self._tmp.cleanup()

    def _execute(self, arguments: dict):
        call = ToolCallItem(
            0, "item-1", "call-1", "repo_map", json.dumps(arguments)
        )
        return self.registry.execute(call, context=_context())

    def test_map_lists_tree_symbols_and_skips_control_paths(self) -> None:
        result = self._execute({"path": ".", "max_depth": 3})
        self.assertFalse(result.is_error, result.content)
        payload = json.loads(result.content)
        paths = [entry["path"] for entry in payload["entries"]]
        self.assertIn("app.py", paths)
        self.assertIn("sub", paths)
        self.assertIn("sub/helper.py", paths)
        self.assertIn("data.txt", paths)
        self.assertFalse(any(p.startswith(".git") for p in paths), paths)
        app = next(e for e in payload["entries"] if e["path"] == "app.py")
        self.assertEqual(app["kind"], "py")
        for symbol in ("Loader", "load", "fetch"):
            self.assertIn(symbol, app["symbols"])
        helper = next(e for e in payload["entries"] if e["path"] == "sub/helper.py")
        self.assertIn("util", helper["symbols"])

    def test_map_is_metadata_only_never_file_bodies(self) -> None:
        result = self._execute({"path": ".", "max_depth": 2})
        self.assertNotIn("SECRET_BODY_SENTINEL", result.content)
        self.assertNotIn("must not leak bodies", result.content)

    def test_depth_zero_and_escape_paths(self) -> None:
        shallow = self._execute({"path": ".", "max_depth": 0})
        payload = json.loads(shallow.content)
        self.assertNotIn(
            "sub/helper.py", [e["path"] for e in payload["entries"]]
        )
        escaped = self._execute({"path": "..", "max_depth": 1})
        self.assertTrue(escaped.is_error)
        control = self._execute({"path": ".git", "max_depth": 1})
        self.assertTrue(control.is_error)

    def test_entry_limit_truncates_deterministically(self) -> None:
        from koawa_agent_v2.tools.repo_map import RepoMapLimits, register_repo_map_tool as reg

        registry = ToolRegistry()
        reg(
            registry,
            self.resolver,
            limits=RepoMapLimits(max_entries=2, max_scan_entries=100, max_files=10),
        )
        call = ToolCallItem(
            0, "item-1", "call-1", "repo_map",
            json.dumps({"path": ".", "max_depth": 3}),
        )
        result = registry.execute(call, context=_context())
        payload = json.loads(result.content)
        self.assertTrue(payload["truncated"])
        self.assertLessEqual(len(payload["entries"]), 2)
        self.assertIn(payload["reason"], ("entry_limit", "result_limit", "scan_limit"))

    def test_joins_verified_coding_registry(self) -> None:
        import subprocess

        from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry

        with tempfile.TemporaryDirectory() as raw:
            subprocess.run(
                ["git", "init", "-q"], cwd=raw, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            from koawa_agent_v2.verification.runner import CommandProfile
            import sys as _sys

            registry = build_verified_coding_tool_registry(
                Path(raw),
                command_profiles=(
                    CommandProfile(
                        profile_id="python_unittest",
                        argv=(_sys.executable, "-c", "pass"),
                    ),
                ),
            )
            names = {d.name for d in registry.definitions()}
        self.assertIn("repo_map", names)


if __name__ == "__main__":
    unittest.main()
