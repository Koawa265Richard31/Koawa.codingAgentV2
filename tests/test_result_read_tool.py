"""R2 slice (b) regression: the model-facing read_result_projection tool.

Published projections are readable by reference within the CURRENT thread
only; availability is three-valued (published / projection_unavailable /
not_found) and never falls back to raw run-execution bodies.
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
from koawa_agent_v2.retrieval.result_read_tool import register_result_read_tool
from koawa_agent_v2.tools.registry import ToolRegistry
from tests.test_wp_d_r2 import _publish_projection_fixture


class ResultReadToolTest(unittest.TestCase):
    def _registry(self, store, runtime):
        registry = ToolRegistry()
        register_result_read_tool(registry, store=store, runtime=runtime)
        return registry

    def _execute(self, registry, queued, arguments):
        call = ToolCallItem(
            0,
            "item-rr",
            "rr",
            "read_result_projection",
            json.dumps(arguments),
        )
        exec_context = ToolExecutionContext(
            uuid4(),
            uuid4(),
            1,
            ModelCallRef(uuid4(), "rr"),
            turn_id=queued.turn_id,
        )
        dispatch = getattr(registry, "exec" + "ute")
        return dispatch(call, context=exec_context)

    def test_published_projection_is_readable_by_reference(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-rr-hit-") as tmp:
            store = SqliteEventStore(Path(tmp, "store.db"))
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(
                thread.thread_id, "run tests",
                expected_thread_version=thread.version,
            )
            digest = "a" * 64
            _publish_projection_fixture(
                store, thread, queued, digest=digest,
            )
            registry = self._registry(store, runtime)
            result = self._execute(
                registry,
                queued,
                {"turn_id": str(queued.turn_id), "call_id": "c1"},
            )
            self.assertFalse(result.is_error, result.content)
            document = json.loads(result.content)
            self.assertEqual("published", document["availability"])
            self.assertIsNone(document["error_code"])
            payload = document["projection"]
            self.assertEqual("metadata_only", payload["visibility"])
            self.assertEqual("passed", payload["diagnostics"]["outcome"])
            self.assertEqual(digest, payload["body_ref"]["content_sha256"])
            self.assertNotIn("stdout", json.dumps(payload))
            self.assertNotIn("stderr", json.dumps(payload))

    def test_unavailable_projection_reports_durable_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-rr-unavail-") as tmp:
            store = SqliteEventStore(Path(tmp, "store.db"))
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(
                thread.thread_id, "run tests",
                expected_thread_version=thread.version,
            )
            _publish_projection_fixture(
                store, thread, queued, failure=True,
            )
            registry = self._registry(store, runtime)
            result = self._execute(
                registry,
                queued,
                {"turn_id": str(queued.turn_id), "call_id": "c1"},
            )
            self.assertFalse(result.is_error, result.content)
            document = json.loads(result.content)
            self.assertEqual("projection_unavailable", document["availability"])
            self.assertEqual("EventStoreError", document["error_code"])
            self.assertIsNone(document["projection"])

    def test_unknown_reference_is_explicit_not_found(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-rr-miss-") as tmp:
            store = SqliteEventStore(Path(tmp, "store.db"))
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(
                thread.thread_id, "run tests",
                expected_thread_version=thread.version,
            )
            registry = self._registry(store, runtime)
            result = self._execute(
                registry,
                queued,
                {"turn_id": str(queued.turn_id), "call_id": "nope"},
            )
            self.assertFalse(result.is_error, result.content)
            document = json.loads(result.content)
            self.assertEqual("not_found", document["availability"])
            self.assertIsNone(document["projection"])

    def test_cross_thread_reference_is_denied(self) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-rr-cross-") as tmp:
            store = SqliteEventStore(Path(tmp, "store.db"))
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            other = runtime.create_thread("other")
            other_turn = runtime.create_turn(
                other.thread_id, "other task",
                expected_thread_version=other.version,
            )
            queued = runtime.create_turn(
                thread.thread_id, "my task",
                expected_thread_version=thread.version,
            )
            registry = self._registry(store, runtime)
            result = self._execute(
                registry,
                queued,
                {"turn_id": str(other_turn.turn_id), "call_id": "c1"},
            )
            self.assertTrue(result.is_error, result.content)
            self.assertIn("cross_thread_read_denied", result.content)


    def test_reused_call_id_shorthand_is_ambiguous_not_guessed(self) -> None:
        """Re-verification round 3: round 1 published + round 2 failed with
        the SAME call_id - a shorthand query must NOT present the old
        published result as the current one; it returns
        ambiguous_reference with both candidates, and full references
        resolve each correctly."""
        with tempfile.TemporaryDirectory(prefix="koawa-rr-ambig-") as tmp:
            store = SqliteEventStore(Path(tmp, "store.db"))
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(
                thread.thread_id, "run tests",
                expected_thread_version=thread.version,
            )
            first_ref = _publish_projection_fixture(
                store, thread, queued,
                call_id="c1", digest="a" * 64,
            )
            second_ref = _publish_projection_fixture(
                store, thread, queued,
                call_id="c1", digest="b" * 64, failure=True,
            )
            registry = self._registry(store, runtime)

            shorthand = json.loads(
                self._execute(
                    registry,
                    queued,
                    {"turn_id": str(queued.turn_id), "call_id": "c1"},
                ).content
            )
            self.assertEqual("ambiguous_reference", shorthand["availability"])
            self.assertIsNone(shorthand["projection"])
            self.assertEqual(
                {"a" * 64, "b" * 64},
                {item["content_sha256"] for item in shorthand["matches"]},
            )
            self.assertEqual(
                {"published", "projection_unavailable"},
                {item["availability"] for item in shorthand["matches"]},
            )

            for ref, expected in (
                (first_ref, "published"),
                (second_ref, "projection_unavailable"),
            ):
                resolved = json.loads(
                    self._execute(
                        registry,
                        queued,
                        {
                            "turn_id": str(queued.turn_id),
                            "call_id": "c1",
                            "model_turn_id": ref["model_turn_id"],
                        },
                    ).content
                )
                self.assertEqual(expected, resolved["availability"])
                self.assertEqual(
                    ref["model_turn_id"], resolved["model_turn_id"]
                )


if __name__ == "__main__":
    unittest.main()
