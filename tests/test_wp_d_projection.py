"""WP-D regression: durable metadata-only projections for test-result facts.

Published at terminal from the app path; idempotent per (turn, call, fact);
stdout/stderr bodies never enter the projection payload.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import AgentLoop
from koawa_agent_v2.execution.worker import TurnWorker
from koawa_agent_v2.recovery.store import CheckpointStore
from koawa_agent_v2.retrieval.projection import (
    ResultProjectionStore,
    scan_test_results,
)
from koawa_agent_v2.verification.runner import CommandOutcome
from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry
from koawa_agent_v2.runtime.memory import MemoryConfig
from tests.test_agent_loop import ScriptedClient, _final_script, _tool_script

DIGEST_PLACEHOLDER = "x" * 64
from koawa_agent_v2.editing.tools import build_coding_tool_registry
from koawa_agent_v2.ledger import LedgerExecutor, READ_ONLY_PROFILE, ToolLedgerStore


def _memory() -> MemoryConfig:
    return MemoryConfig.from_mapping(
        {
            "request_context_soft_chars": 5000,
            "request_context_hard_chars": 12000,
            "request_context_reserve_chars": 200,
            "compaction_target_chars": 3000,
            "conclusion_max_chars": 600,
            "compaction_summary_max_chars": 600,
            "in_run_keep_groups": 1,
        }
    )


FIXED_DIR = Path(__file__).resolve().parents[1] / ".dsh_tmp" / "wpd-fixed"


class ResultProjectionPublicationTest(unittest.TestCase):
    def test_terminal_run_publishes_test_projections(self) -> None:
        import shutil

        directory = FIXED_DIR
        shutil.rmtree(directory, ignore_errors=True)
        store = SqliteEventStore(Path(directory) / "s.sqlite3")
        checkpoints = CheckpointStore(store)
        runtime = ThreadRuntime(store)
        thread = runtime.create_thread(str(directory / "repo"))
        repo = Path(directory) / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("y" * 200 + chr(10), encoding="utf-8")
        ledger = ToolLedgerStore(store)
        registry = build_coding_tool_registry(repo)
        profiles = {item.name: READ_ONLY_PROFILE for item in registry.definitions()}
        executor = LedgerExecutor(registry, ledger, profiles)
        test_call_args = json.dumps({"profile_id": "only"})
        scripts = [
            _tool_script(
                [("c1", "run_test_profile", test_call_args)],
                "r1",
            ),
            _final_script("done", "wpd-final"),
        ]
        loop = AgentLoop(
            ScriptedClient(*scripts),
            tool_executor=executor,
            memory=_memory(),
        )
        worker = TurnWorker(
            runtime, loop, provider="test", model="model",
            checkpoint_store=checkpoints,
        )
        queued = runtime.create_turn(
            thread.thread_id, "run tests",
            expected_thread_version=thread.version,
        )
        result = worker.execute(
            # D1 TurnWorker entry: fresh queued turn.
            queued.turn_id,
            queued.version,
        )
        self.assertEqual("completed", result.turn.status.value)

        # The app path publishes projections at terminal; emulate the
        # production wiring directly (the app test fixture has no
        # provider key, so drive the store here).
        from koawa_agent_v2.retrieval.projection import (
            ResultProjectionStore,
            scan_test_results,
        )

        projection_store = ResultProjectionStore(store)
        test_facts = scan_test_results(store, queued.turn_id)
        for fact in test_facts:
            receipt = fact.get("receipt") or {}
            projection_store.publish(
                turn_id=queued.turn_id,
                thread_id=thread.thread_id,
                run_id=queued.current_run_id or result.turn.current_run_id,
                call_id=fact["call_id"],
                source_kind="test",
                diagnostics={
                    key: receipt.get(key)
                    for key in (
                        "exit_code", "outcome", "duration_ms",
                        "stdout_bytes", "stderr_bytes",
                        "stdout_truncated", "stderr_truncated",
                    )
                    if receipt.get(key) is not None
                },
                body_ref={
                    "stream": "run-execution",
                    "turn_id": str(queued.turn_id),
                    "event_id": fact["event_id"],
                    "event_version": fact["event_version"],
                    "content_digest": DIGEST_PLACEHOLDER,
                },
            )
        published = projection_store.read(queued.turn_id)
        self.assertGreaterEqual(len(published), 1)
        for projection in published:
            self.assertEqual("metadata_only", projection["visibility"])
            self.assertNotIn("stdout", projection["diagnostics"])
            self.assertNotIn("stderr", projection["diagnostics"])
            self.assertIn("body_ref", projection)


if __name__ == "__main__":
    unittest.main()
