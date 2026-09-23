"""Hardening 2026-09-19 regression: the run_test_profile model-visible
receipt carries structured diagnostics only - stdout/stderr bodies never
enter the model service (they stay in the durable record for operators).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from collections import deque
from uuid import uuid4

from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.verification.output_policy import (
    BODY_VISIBILITY,
    POLICY_VERSION,
    safe_diagnostics,
)
from koawa_agent_v2.verification.runner import CommandOutcome, CommandResult
from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry


class _ScriptedRunner:
    def __init__(self) -> None:
        self._queued: list[CommandOutcome] = [CommandOutcome.PASSED]

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return ("only",)

    def validate_profile(self, profile_id: str) -> None:
        pass

    def run(self, profile_id, *, progress_guard=None, execution_id=None):
        outcome = self._queued.pop(0) if self._queued else CommandOutcome.PASSED
        return CommandResult(
            profile_id=profile_id,
            outcome=outcome,
            exit_code=0 if outcome is CommandOutcome.PASSED else 1,
            stdout="SECRET-STDOUT " + "x" * 5000,
            stderr="SECRET-STDERR " + "e" * 5000,
            stdout_bytes=5014,
            stderr_bytes=5014,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=7,
            argv=("trusted-test", profile_id),
            timeout_seconds=10.0,
            backend="docker",
            immutable_image_id="sha256:" + "1" * 64,
            profile_digest="2" * 64,
        )


class OutputPolicyGateTest(unittest.TestCase):
    def test_receipt_carries_diagnostics_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-outpol-") as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            import subprocess

            subprocess.run(
                ("git", "-C", str(root), "init", "-q"), check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            registry = build_verified_coding_tool_registry(
                root,
                command_runner=_ScriptedRunner(),
                required_test_profiles=("only",),
            )
            self.addCleanup(registry.close)
            run_id = uuid4()
            result = registry.execute(
                ToolCallItem(
                    0, "item-t", "t", "run_test_profile",
                    json.dumps({"profile_id": "only"}),
                ),
                context=ToolExecutionContext(
                    run_id, uuid4(), 1, ModelCallRef(run_id, "t")
                ),
            )
            self.assertFalse(result.is_error, result.content)
            document = json.loads(result.content)
            self.assertNotIn("stdout", document)
            self.assertNotIn("stderr", document)
            self.assertNotIn("SECRET-STDOUT", result.content)
            self.assertNotIn("SECRET-STDERR", result.content)
            self.assertEqual("metadata_only", document["test_output_visibility"])
            self.assertEqual(POLICY_VERSION, document["test_output_policy"])
            self.assertEqual(5014, document["stdout_bytes"])
            self.assertEqual(5014, document["stderr_bytes"])
            self.assertTrue(document["stdout_truncated"] is False)

    def test_safe_diagnostics_excludes_bodies(self) -> None:
        from koawa_agent_v2.verification.runner import CommandOutcome

        result = CommandResult(
            profile_id="p",
            outcome=CommandOutcome.FAILED,
            exit_code=3,
            stdout="SECRET",
            stderr="SECRET",
            stdout_bytes=6,
            stderr_bytes=6,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=1,
            argv=("t",),
            timeout_seconds=1.0,
            backend="host",
            immutable_image_id=None,
            profile_digest=None,
        )
        diagnostics = safe_diagnostics(result)
        self.assertEqual(
            {
                "exit_code": 3,
                "outcome": "failed",
                "duration_ms": 1,
                "stderr_bytes": 6,
                "stderr_truncated": False,
                "stdout_bytes": 6,
                "stdout_truncated": False,
                "timeout_seconds": 1.0,
            },
            diagnostics,
        )
        self.assertNotIn("SECRET", json.dumps(diagnostics))


if __name__ == "__main__":
    import tempfile

    unittest.main()
