"""Audit F11 regression: resume after a killed durable run.

A durable CLI worker heartbeats the dedicated recovery-lease stream, so when
its process is killed the stream head still names the dead run.  Stale
takeover plus a new durable worker must re-establish the head for its own run
(in the same commit as the durable start) instead of failing its first
heartbeat with ``recovery lease token mismatch``.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from koawa_agent_v2.recovery import CheckpointStore, RecoveryCoordinator
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import AgentLoop
from koawa_agent_v2.execution.worker import TurnWorker
from tests.test_agent_loop import ScriptedClient, _final_script


class ResumeAfterKilledDurableRunTest(unittest.TestCase):
    def test_new_durable_run_reestablishes_lease_head(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-f11-") as directory:
            store = SqliteEventStore(Path(directory, "state.sqlite3"))
            checkpoints = CheckpointStore(store)
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(
                thread.thread_id,
                "crash mid-run",
                expected_thread_version=thread.version,
            )
            running = runtime.start_turn(queued.turn_id, queued.version)
            # The token-less heartbeat a durable worker leaves at the lease
            # stream head before its process dies.
            runtime.heartbeat_recovery_run(
                running.turn_id,
                expected_version=running.version,
                run_id=running.current_run_id,
                claim_token=None,
                lease_seconds=1,
                owner_id="cli",
            )
            time.sleep(1.5)  # dead run's lease must expire for stale discovery
            coordinator = RecoveryCoordinator(
                runtime, checkpoints, owner_id="recovery"
            )
            claim = coordinator.claim_stale(
                coordinator.list_recoverable_turns()[0], force=True
            )
            resumed_turn_id = claim.turn.turn_id
            resumed_version = claim.turn.version

            def slow_final(request):
                # Real model latency spans the resumed worker's first
                # heartbeat (lease_seconds=1 → keeper fires at 0.5s), which
                # is exactly when the stale-head fence used to kill the run.
                time.sleep(1.0)
                return _final_script("done", "killed-resume-final")(request)

            client = ScriptedClient(slow_final)
            worker = TurnWorker(
                runtime,
                AgentLoop(client),
                provider="test",
                model="model",
                checkpoint_store=checkpoints,
                owner_id="cli-resumed",
                lease_seconds=1,
            )
            result = worker.execute(
                # D1 TurnWorker entry: the claim's queued turn identity.
                resumed_turn_id,
                resumed_version,
            )
            self.assertEqual(result.turn.status, TurnStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
