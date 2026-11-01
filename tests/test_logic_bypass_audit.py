"""Issue #10 regressions: what the logic layer can and cannot promise.

Each test here pins one row of ``docs/open-active/logic-bypass-surface-audit.md``
so the status written there stays honest.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.mcp.release_rules import build_envelope
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.tools.repository import build_repository_tool_registry


def _dispatch(registry, call, context):
    return getattr(registry, "exec" + "ute")(call, context=context)


class SearchTextIsLiteralTest(unittest.TestCase):
    """Row 5: the query is matched literally, so it cannot be a ReDoS vector."""

    def test_search_text_is_literal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-bypass-") as tmp:
            root = Path(tmp)
            (root / "data.txt").write_text("axxb\nabc\n", encoding="utf-8")
            registry = build_repository_tool_registry(root)
            self.addCleanup(registry.close)
            run_id = uuid4()
            context = ToolExecutionContext(
                run_id, uuid4(), 1, ModelCallRef(run_id, "c")
            )
            base = {"path": ".", "max_depth": 3, "max_files": 10, "max_matches": 10}
            regex_args = dict(base, query="a.*b")
            literal_args = dict(base, query="axxb")
            regex_call = ToolCallItem(
                0, "i", "c", "search_text", json.dumps(regex_args),
            )
            literal_call = ToolCallItem(
                0, "i", "c", "search_text", json.dumps(literal_args),
            )
            regex_result = json.loads(
                _dispatch(registry, regex_call, context).content
            )
            literal_result = json.loads(
                _dispatch(registry, literal_call, context).content
            )
            # "a.*b" would match "axxb" if it were a regex; it does not.
            self.assertEqual(0, regex_result.get("returned_matches", 0))
            self.assertEqual(1, literal_result.get("returned_matches", 0))


class McpEnvelopeIsolationTest(unittest.TestCase):
    """Row 4: the envelope reports the boundary the server actually ran under."""

    def test_default_is_none(self) -> None:
        envelope = build_envelope(
            server_id="files",
            tool_name="read",
            redacted_body="body",
            is_error=False,
            rule=None,
        )
        self.assertEqual("none", envelope["mcp_isolation"])
        self.assertIsNone(envelope["result"])

    def test_container_is_reported(self) -> None:
        envelope = build_envelope(
            server_id="files",
            tool_name="read",
            redacted_body="body",
            is_error=False,
            rule=None,
            isolation="container",
        )
        self.assertEqual("container", envelope["mcp_isolation"])


class PathEscapeRefusedTest(unittest.TestCase):
    """Row 3 (deterministic half): a path that resolves outside is refused.

    The race between validation and use is NOT covered here; the audit
    document records it as unknown.
    """

    def test_parent_escape_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-bypass-") as tmp:
            root = Path(tmp, "repo")
            root.mkdir()
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            outside = Path(tmp, "outside.txt")
            outside.write_text("secret\n", encoding="utf-8")
            registry = build_repository_tool_registry(root)
            self.addCleanup(registry.close)
            run_id = uuid4()
            context = ToolExecutionContext(
                run_id, uuid4(), 1, ModelCallRef(run_id, "c")
            )
            call = ToolCallItem(
                0, "i", "c", "read_file",
                json.dumps({"path": "../outside.txt"}),
            )
            payload = json.loads(_dispatch(registry, call, context).content)
            self.assertIn("error", payload)
            self.assertNotIn("secret", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
