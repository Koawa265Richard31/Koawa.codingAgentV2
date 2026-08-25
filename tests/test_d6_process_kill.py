from __future__ import annotations

import json
import os
import subprocess
import sys
import sqlite3
import tempfile
from contextlib import closing
import time
import unittest
from pathlib import Path

from koawa_agent_v2.recovery import (
    AutomaticRecoveryBlocked,
    CheckpointStore,
    RecoveryCoordinator,
    RunPhase,
)
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime


ROOT = Path(__file__).resolve().parents[1]
CHILD = ROOT / "tests" / "fixtures" / "d6_kill_worker.py"


class D6ProcessKillTest(unittest.TestCase):
    POINTS = {
        "atomic_started": RunPhase.READY_FOR_MODEL,
        "model_event_no_checkpoint": RunPhase.READY_TO_FINALIZE,
        "checkpoint_saved": RunPhase.READY_TO_FINALIZE,
        "ready_for_tool": RunPhase.READY_FOR_TOOL,
        "tool_in_progress": RunPhase.TOOL_IN_PROGRESS,
        "tool_result_saved": RunPhase.READY_FOR_MODEL,
    }

    def test_forced_process_kill_recovers_six_durable_boundaries(self):
        for point, expected_phase in self.POINTS.items():
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                database = root / "runtime.db"
                marker = root / "ready.json"
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
                        self.fail(f"child did not reach kill point {point}")
                    marker_document = json.loads(marker.read_text(encoding="utf-8"))
                    self.assertEqual(marker_document["point"], point)
                    process.kill()
                    process.communicate(timeout=5)
                    self.assertNotEqual(process.returncode, 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)

                store = SqliteEventStore(database)
                checkpoints = CheckpointStore(store)
                runtime = ThreadRuntime(store)
                coordinator = RecoveryCoordinator(
                    runtime,
                    checkpoints,
                    owner_id=f"parent-{point}",
                )
                # The durable child lease would still be live; make the
                # recoverable projection deterministic by expiring it.
                from contextlib import closing
                with closing(sqlite3.connect(str(database))) as connection:
                    connection.execute(
                        "UPDATE recoverable_turns SET lease_expires_at=? ",
                        ("2000-01-01T00:00:00.000000Z",),
                    )
                    connection.commit()
                candidate = coordinator.list_recoverable_turns()[0]
                rebuilt = coordinator.reconstruct(candidate)
                self.assertEqual(rebuilt.phase, expected_phase)
                self.assertIn(point, rebuilt.context[0]["content"])
                if point == "tool_in_progress":
                    with self.assertRaises(AutomaticRecoveryBlocked):
                        coordinator.claim_stale(candidate, force=True)
                else:
                    claim = coordinator.claim_stale(candidate, force=True)
                    self.assertEqual(claim.turn.status.value, "queued")


if __name__ == "__main__":
    unittest.main()