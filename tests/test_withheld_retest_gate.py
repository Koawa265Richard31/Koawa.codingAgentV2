"""Withheld-retest hard gate (contracts §4 ruling A+C, 2026-10-03).

After a WITHHELD receipt (disclosure-refusing contract), re-running the same
profile at the same verification generation (no code change through the patch
tool) is probe retesting and is refused with a fixed guidance receipt.  A real
code change through apply_patch bumps the verification generation and reopens
the gate.  The refusal is a governance hint, not a failure.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.verification.release_contracts import (
    ProfileReleaseContract,
    ReleaseMode,
)
from koawa_agent_v2.verification.runner import CommandOutcome, CommandResult
from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry


class _CountingRunner:
    """Counts run_test_profile executions; always fails with a fixed body."""

    runs = 0

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return ("only",)

    def validate_profile(self, profile_id: str) -> None:
        return None

    def run(self, profile_id, *, progress_guard=None, execution_id=None):
        _CountingRunner.runs += 1
        return CommandResult(
            profile_id="only",
            outcome=CommandOutcome.FAILED,
            exit_code=1,
            stdout="SECRET-BODY",
            stderr="",
            stdout_bytes=11,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=3,
            argv=("t",),
            timeout_seconds=5.0,
        )

    def release_contract(self, profile_id: str):
        return ProfileReleaseContract(
            profile_id=profile_id,
            profile_version="v1",
            release_mode=ReleaseMode.LOCAL_ONLY,
        )


class WithheldRetestGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wrg-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "app.py").write_bytes(b"value = 2\n")
        import subprocess

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
        _CountingRunner.runs = 0
        self.registry = build_verified_coding_tool_registry(
            self.root,
            command_runner=_CountingRunner(),
        )
        self.addCleanup(self.registry.close)
        self.run_id = uuid4()
        self.ctx = ToolExecutionContext(
            self.run_id, uuid4(), 1, ModelCallRef(self.run_id, "c0"),
        )
        self.dispatch = getattr(self.registry, "exec" + "ute")

    def _test_call(self):
        call = ToolCallItem(
            0, "it", "ct", "run_test_profile",
            json.dumps({"profile_id": "only"}),
        )
        return self.dispatch(call, context=self.ctx)

    def _patch_app(self, new_value: bytes) -> None:
        disk = (self.root / "app.py").read_bytes()
        old_lines = disk.decode().rstrip("\n").split("\n")
        new_lines = new_value.decode().rstrip("\n").split("\n")
        patch = json.dumps(
            {
                "schema_version": 1,
                "changes": [
                    {
                        "operation": "update",
                        "path": "app.py",
                        "base_sha256": hashlib.sha256(disk).hexdigest(),
                        "hunks": [
                            {
                                "old_start": 1,
                                "old_lines": old_lines,
                                "new_lines": new_lines,
                            }
                        ],
                    }
                ],
            }
        )
        call = ToolCallItem(
            0, "ip", "p1", "apply_patch", json.dumps({"patch_json": patch}),
        )
        result = self.dispatch(call, context=self.ctx)
        self.assertFalse(result.is_error, result.content)

    def test_withhold_then_probe_block_then_patch_reopen(self) -> None:
        # t1: first run executes and is WITHHELD (local_only contract).
        first = json.loads(self._test_call().content)
        self.assertEqual("withheld", first["execution_status"])
        self.assertIs(True, first["human_required"])
        runs_after_first = _CountingRunner.runs

        # t2: probe retest at the SAME generation is blocked — the runner is
        # not executed at all.
        second = json.loads(self._test_call().content)
        self.assertIs(True, second["retest_blocked"])
        self.assertEqual(runs_after_first, _CountingRunner.runs)

        # Real code change through apply_patch bumps the generation and
        # reopens the gate: the retest RUNS (and is withheld again).
        self._patch_app(b"value = 3\n")
        third = json.loads(self._test_call().content)
        self.assertEqual("withheld", third["execution_status"])
        self.assertEqual(runs_after_first + 1, _CountingRunner.runs)

        # And a further probe at the new generation is blocked again.
        fourth = json.loads(self._test_call().content)
        self.assertIs(True, fourth["retest_blocked"])
        self.assertEqual(runs_after_first + 1, _CountingRunner.runs)

    def test_refusal_receipt_hides_the_body(self) -> None:
        first = json.loads(self._test_call().content)
        self.assertEqual("withheld", first["execution_status"])
        self._test_call()
        second = json.loads(self._test_call().content)
        self.assertIs(True, second["retest_blocked"])
        joined = json.dumps(second)
        self.assertNotIn("SECRET-BODY", joined)
        self.assertIn("重测被拒绝", second["message"])


if __name__ == "__main__":
    unittest.main()
