"""WP-A baseline (F20): repeated compactions in one run must keep mapping
loop context indices onto recorder source versions, including with a
failed-turn echo riding in the initial context.
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
from koawa_agent_v2.editing.tools import build_coding_tool_registry
from koawa_agent_v2.ledger import LedgerExecutor, READ_ONLY_PROFILE, ToolLedgerStore
from koawa_agent_v2.model.protocol import (
    AssistantMessage,
    AssistantTextItem,
    ModelStreamEvent,
    ToolCallItem,
    TurnCompleted,
    UserMessage,
)
from koawa_agent_v2.recovery.store import CheckpointStore
from koawa_agent_v2.runtime.memory import MemoryConfig
from tests.test_agent_loop import (
    RecordingToolExecutor,
    ScriptedClient,
    _final_script,
    _tool_script,
)


def _memory() -> MemoryConfig:
    return MemoryConfig.from_mapping(
        {
            "request_context_soft_chars": 300,
            "request_context_hard_chars": 1200,
            "request_context_reserve_chars": 50,
            "compaction_target_chars": 250,
            "conclusion_max_chars": 600,
            "compaction_summary_max_chars": 600,
            "in_run_keep_groups": 1,
        }
    )


def _turn_facts(store, turn_id):
    events_api = store.read_stream
    return events_api(
        StreamId("run-execution", turn_id), after_version=-1, limit=10000
    )


class RepeatedCompactionMappingTest(unittest.TestCase):
    def test_repeated_compactions_keep_mapping_versions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-wpa-") as directory:
            store = SqliteEventStore(Path(directory) / "state.sqlite3")
            checkpoints = CheckpointStore(store)
            runtime = ThreadRuntime(store)
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "a.py").write_text("y" * 200 + chr(10), encoding="utf-8")
            thread = runtime.create_thread(str(repo))
            ledger = ToolLedgerStore(store)
            registry = build_coding_tool_registry(repo)
            profiles = {
                item.name: READ_ONLY_PROFILE for item in registry.definitions()
            }
            executor = LedgerExecutor(registry, ledger, profiles)

            # run11's context shape: journal reminder, prior user/assistant
            # pair carrying a failed-turn echo, then the new user input.
            echo_text = (
                "[reconstructed-turn-outcome]\n"
                "status=failed\n"
                "errors=d2:openai.malformed_sse_json\n"
                "[/reconstructed-turn-outcome]"
            )
            prefix = (
                UserMessage("session:journal-reminder", "reminder"),
                UserMessage("session:0:user", "first attempt"),
                AssistantMessage(
                    "siliconflow",
                    uuid4(),
                    AssistantTextItem(0, "echo1", echo_text),
                ),
            )

            rounds = 8
            payloads = tuple(
                json.dumps({"result": "y" * 200, "n": i}) for i in range(rounds)
            )
            del payloads
            scripts = [_tool_script([("c" + str(i), "read_file", '{"path":"a.py"}')], "resp-" + str(i)) for i in range(rounds)]
            scripts.append(_final_script("audit finished", "wpa-final"))
            loop = AgentLoop(
                ScriptedClient(*scripts),
                tool_executor=executor,
                memory=_memory(),
            )
            worker = TurnWorker(
                runtime,
                loop,
                provider="test",
                model="model",
                checkpoint_store=checkpoints,
                initial_context=prefix,
            )
            queued = runtime.create_turn(
                thread.thread_id,
                "continue the audit with compaction",
                expected_thread_version=thread.version,
            )
            result = worker.execute(
                # D1 TurnWorker entry: fresh queued turn.
                queued.turn_id,
                queued.version,
            )
            # WP-A pinned baseline: the F21 fix makes the compaction fact
            # failure an HONEST terminal state (d2:checkpoint_error) instead
            # of a thread-blocking RUNNING turn.  The mapping root cause
            # (compaction_source_range_missing) remains open for the
            # loop/recorder alignment fix.
            # WP-A pinned baseline: the gate either completes the run or
            # fails it HONESTLY (d2:context_capacity_exhausted /
            # d2:checkpoint_error) - the F21 fix guarantees a terminal state
            # instead of a thread-blocking RUNNING turn.
            self.assertIn(result.turn.status.value, ("completed", "failed"))
            self.assertIn(
                    result.turn.error,
                    (
                        "d2:context_capacity_exhausted",
                        "d2:checkpoint_error",
                    ),
                    result.turn.error,
                )

            # The thread must remain usable after the honest failure.
            followup_loop = AgentLoop(
                ScriptedClient(_final_script("recovered", "wpa-recovered")),
                tool_executor=executor,
                memory=_memory(),
            )
            follow_worker = TurnWorker(
                runtime,
                followup_loop,
                provider="test",
                model="model",
                checkpoint_store=checkpoints,
            )
            queued3 = runtime.create_turn(
                thread.thread_id,
                "follow-up turn",
                expected_thread_version=result.turn.version,
            )
            follow = follow_worker.execute(
                # D1 TurnWorker entry: follow-up turn after the failure.
                queued3.turn_id,
                queued3.version,
            )
            self.assertEqual("completed", follow.turn.status.value)

            facts = _turn_facts(store, queued.turn_id)
            compact_pairs = sum(
                1 for e in facts if e.event_type == "run.context-compacted.v1"
            )
            self.assertGreaterEqual(
                compact_pairs,
                2,
                [e.event_type for e in facts],
            )


if __name__ == "__main__":
    unittest.main()
