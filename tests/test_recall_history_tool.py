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
        self.assertEqual(1, len(document["hits"]))
        hit = document["hits"][0]
        self.assertEqual(str(hit_record.turn_id), hit["turn_id"])
        self.assertLessEqual(len(hit["user_input"]), 120)
        self.assertLessEqual(len(hit["final_text"]), 120)
        self.assertNotIn("U" * 300, result.content)
        # thread scope resolved from the execution context's turn
        self.assertEqual(
            (str(thread.thread_id), "audit findings", 5),
            stub.seen_threads[0],
        )

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
