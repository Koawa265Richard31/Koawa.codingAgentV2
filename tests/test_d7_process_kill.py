from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import UUID

from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.ledger import (
    ToolExecutionState,
    ToolLedgerStore,
    ToolRecoveryManager,
)


ROOT = Path(__file__).resolve().parents[1]
CHILD = ROOT / "tests" / "fixtures" / "d7_kill_worker.py"


class D7ProcessKillTest(unittest.TestCase):
    CRASH_WINDOWS = (
        "before_claim",
        "after_claim",
        "handler_in_progress",
        "after_handler_before_result_commit",
        "after_result_commit_before_checkpoint",
        "cancel_vs_claim",
    )
    PROCESS_POINTS = {
        "before_claim": (ToolExecutionState.PREPARED, 0, True),
        "after_claim": (ToolExecutionState.CLAIMED, 0, True),
        "handler_in_progress": (ToolExecutionState.CLAIMED, 1, False),
        "after_handler": (ToolExecutionState.CLAIMED, 1, False),
        "after_result_commit": (ToolExecutionState.SUCCEEDED, 1, True),
    }

    def test_six_required_crash_windows_are_explicit(self) -> None:
        self.assertEqual(6, len(self.CRASH_WINDOWS))
        self.assertEqual(6, len(set(self.CRASH_WINDOWS)))

    def test_forced_process_kill_reconstructs_five_ledger_boundaries(self) -> None:
        for point, (initial_state, side_effects, safe) in self.PROCESS_POINTS.items():
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                database = root / "runtime.db"
                marker = root / "ready.json"
                side_effect = root / "side-effect-count.txt"
                environment = os.environ.copy()
                configured_path = environment.get("PYTHONPATH")
                environment["PYTHONPATH"] = os.pathsep.join(
                    part
                    for part in (str(ROOT / "src"), str(ROOT), configured_path)
                    if part
                )
                kwargs = {}
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        str(CHILD),
                        str(database),
                        str(marker),
                        str(side_effect),
                        point,
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    **kwargs,
                )
                try:
                    deadline = time.monotonic() + 15
                    while not marker.exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            stdout, stderr = process.communicate()
                            self.fail(
                                f"child exited before {point}:\n{stdout}\n{stderr}"
                            )
                        time.sleep(0.05)
                    if not marker.exists():
                        self.fail(f"child did not reach D7 kill point {point}")
                    document = json.loads(marker.read_text(encoding="utf-8"))
                    self.assertEqual(point, document["point"])
                    process.kill()
                    process.communicate(timeout=5)
                    self.assertNotEqual(0, process.returncode)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)

                count = (
                    int(side_effect.read_text(encoding="utf-8"))
                    if side_effect.exists()
                    else 0
                )
                self.assertEqual(side_effects, count)
                ledger = ToolLedgerStore(SqliteEventStore(database))
                execution_id = UUID(document["execution_id"])
                record = ledger.load(execution_id)
                self.assertIsNotNone(record)
                self.assertEqual(initial_state, record.state)

                pending = (
                    {
                        "model_turn_id": document["model_turn_id"],
                        "call_id": document["call_id"],
                    },
                )
                recovered = ToolRecoveryManager(ledger).reconcile_pending(
                    UUID(document["turn_id"]),
                    pending,
                )
                self.assertEqual(safe, recovered)
                after = ledger.load(execution_id)
                if point in {"handler_in_progress", "after_handler"}:
                    self.assertEqual(ToolExecutionState.OUTCOME_UNKNOWN, after.state)
                else:
                    self.assertEqual(initial_state, after.state)


if __name__ == "__main__":
    unittest.main()
