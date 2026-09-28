"""WP-E regression: the recall_history model tool serves metadata-only hits
scoped to the execution context's thread, with explicit unavailable state.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.retrieval.recall_tool import register_recall_tool
from koawa_agent_v2.tools.registry import ToolRegistry


class _Hit:
    def __init__(self) -> None:
        self.turn_id = uuid4()
        self.user_input = "U" * 300
        self.final_text = "F" * 300
        self.tools = ("read_file",)
        self.files = ("a.py",)
        self.score = 3.5


class _StubMemory:
    def __init__(self, hits) -> None:
        self._hits = hits
        self.seen_threads = []

    def recall(self, thread_id, query, limit):
        self.seen_threads.append((str(thread_id), query, limit))
        return self._hits


class _MemoryFactory:
    def __init__(self, memory) -> None:
        self._memory = memory

    def __call__(self, context):
        return self._memory


class RecallHistoryToolTest(unittest.TestCase):
    def _registry_with_stub(self, stub):
        tmp = tempfile.TemporaryDirectory(prefix="koawa-recall-")
        self.addCleanup(tmp.cleanup)
        event_store = SqliteEventStore(Path(tmp.name) / "s.sqlite3")
        turn_runtime = ThreadRuntime(event_store)
        thread = turn_runtime.create_thread("repo")
        queued = turn_runtime.create_turn(
            thread.thread_id, "query target",
            expected_thread_version=thread.version,
        )
        registry = ToolRegistry()
        memory_factory = _MemoryFactory(stub)
        register_recall_tool(
            registry,
            store=event_store,
            runtime=turn_runtime,
            memory_factory=memory_factory,
        )
        return registry, queued, thread, event_store

    def test_hits_are_metadata_only_and_thread_scoped(self) -> None:
        hit_record = _Hit()
        stub = _StubMemory([hit_record])
        registry, queued, thread, event_store = self._registry_with_stub(stub)
        call = ToolCallItem(
            0, "item-r", "r", "recall_history", json.dumps({"query": "audit findings"})
        )
        exec_context = ToolExecutionContext(
            uuid4(), uuid4(), 1, ModelCallRef(uuid4(), "r"), turn_id=queued.turn_id
        )
        result = registry.execute(
            call,
            context=exec_context,
        )
        self.assertFalse(result.is_error, result.content)
        document = json.loads(result.content)
        self.assertEqual("metadata_only", document["visibility"])
        # R3: free-text previews are not metadata - only lengths are served,
        # and the content state is an explicit not-released marker.
        self.assertEqual(
            "unavailable_without_release_rule", document["content_release"]
        )
        self.assertEqual(1, len(document["hits"]))
        hit = document["hits"][0]
        self.assertEqual(str(hit_record.turn_id), hit["turn_id"])
        self.assertNotIn("user_input", hit)
        self.assertNotIn("final_text", hit)
        self.assertNotIn("U", result.content)
        self.assertNotIn("F", result.content)
        self.assertEqual(300, hit["user_input_chars"])
        self.assertEqual(300, hit["final_text_chars"])
        # thread scope resolved from the execution context's turn
        self.assertEqual(
            (str(thread.thread_id), "audit findings", 5),
            stub.seen_threads[0],
        )

    def test_hit_text_never_reaches_provider_request(self) -> None:
        """R3 acceptance: the retrieval must actually RUN and HIT, and the
        final provider request must still carry no seeded text.  The turn
        is bound so recall resolves; the query is neutral (the secret must
        not ride the request as a search argument); success is asserted
        before the leak check (re-verification 2026-09-25: the previous
        version ran without a turn, got recall_unavailable, and passed
        vacuously).

        SCOPE (re-verification round 3): this observes the request CONTEXT
        items (``.content``), not the full provider wire serialization -
        the proven conclusion is that historical input/reply previews no
        longer leak via this retrieval receipt, not a wire-level
        guarantee."""
        from uuid import uuid4 as new_id

        from koawa_agent_v2.execution.loop import AgentLoop
        from koawa_agent_v2.model.protocol import (
            InstructionMessage,
            InstructionRole,
            ToolResultMessage,
        )
        from koawa_agent_v2.runtime.memory import MemoryConfig
        from tests.test_agent_loop import (
            ScriptedClient,
            _final_script,
            _tool_script,
        )

        marker = "TOPSECRET-SEED-"
        hit_record = _Hit()
        hit_record.user_input = marker + "U" * 300
        hit_record.final_text = marker + "F" * 300
        stub = _StubMemory([hit_record])
        registry, queued, thread, event_store = self._registry_with_stub(stub)
        scripts = [
            _tool_script(
                [("rc", "recall_history", json.dumps({"query": "audit findings"}))],
                "r1",
            ),
            _final_script("done", "r2"),
        ]
        client = ScriptedClient(*scripts)
        memory = MemoryConfig.from_mapping(
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
        loop = AgentLoop(client, tool_executor=registry, memory=memory)
        loop.run(
            run_id=new_id(),
            turn_id=queued.turn_id,
            turn_version=queued.version,
            input_items=(
                InstructionMessage(InstructionRole.SYSTEM, "search history"),
            ),
            provider="test",
            model="model",
        )
        self.assertEqual(2, len(client.requests))
        # The recall actually executed against the bound thread and HIT
        # the seeded turn (not a vacuous recall_unavailable path).
        self.assertEqual(
            [(str(thread.thread_id), "audit findings", 5)],
            stub.seen_threads,
        )
        recall_results = [
            item
            for item in client.requests[1].input_items
            if isinstance(item, ToolResultMessage)
            and item.call_ref.call_id == "rc"
        ]
        self.assertEqual(1, len(recall_results))
        self.assertFalse(recall_results[0].is_error, recall_results[0].content)
        document = json.loads(recall_results[0].content)
        self.assertEqual(
            [str(hit_record.turn_id)],
            [item["turn_id"] for item in document["hits"]],
        )
        self.assertEqual(
            "unavailable_without_release_rule", document["content_release"]
        )
        # The full request stream never carries the seeded text.
        for request in client.requests:
            serialized = "".join(
                getattr(item, "content", "") or ""
                for item in request.input_items
            )
            self.assertNotIn(marker, serialized)

    def test_memory_failure_is_explicit_unavailable(self) -> None:
        class _Broken:
            def recall(self, *args):
                raise RuntimeError("backend down")

        stub = _Broken()
        with tempfile.TemporaryDirectory(prefix="koawa-recall2-") as directory:
            event_store = SqliteEventStore(Path(directory) / "s.sqlite3")
            turn_runtime = ThreadRuntime(event_store)
            thread = turn_runtime.create_thread("repo")
            queued = turn_runtime.create_turn(
                thread.thread_id, "q", expected_thread_version=thread.version
            )
            registry = ToolRegistry()
            memory_factory = _MemoryFactory(stub)
            register_recall_tool(
                registry,
                store=event_store,
                runtime=turn_runtime,
                memory_factory=memory_factory,
            )
            call = ToolCallItem(
                0, "item-r", "r", "recall_history", json.dumps({"query": "anything"})
            )
            exec_context = ToolExecutionContext(
                uuid4(), uuid4(), 1, ModelCallRef(uuid4(), "r"),
                turn_id=queued.turn_id,
            )
            result = registry.execute(
                call,
                context=exec_context,
            )
            self.assertTrue(result.is_error, result.content)
            self.assertIn("recall_unavailable", result.content)


if __name__ == "__main__":
    unittest.main()
