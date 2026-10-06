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
            self.assertEqual(digest, payload["body_ref"]["source_content_sha256"])
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
                {item["source_content_sha256"] for item in shorthand["matches"]},
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


class ReadChainSemanticsTest(unittest.TestCase):
    """WP-E audit (2026-10-03, baseline 4cabb7c): quota truncation must be
    visible to the model (a not_found under truncation is NOT a full
    answer), and revocation verdicts ride the tool response."""

    def test_scan_truncated_is_propagated_not_silently_full(self) -> None:
        import tempfile as _tempfile
        from unittest import mock

        from koawa_agent_v2.retrieval import projection as projection_mod
        from tests.test_wp_d_r2 import _publish_projection_fixture

        with _tempfile.TemporaryDirectory(prefix="wp-e-quota-") as tmp:
            store = SqliteEventStore(Path(tmp, "store.db"))
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(
                thread.thread_id, "run tests",
                expected_thread_version=thread.version,
            )
            # Two published projections for the SAME call_id but different
            # model turns (so a shorthand stays ambiguous and candidates are
            # resolved one by one); then shrink the scan quota so the walk
            # stops before the second event.
            _publish_projection_fixture(
                store, thread, queued, call_id="c1", digest="1" * 64,
                model_turn_id="00000000-0000-5000-9000-000000000001",
            )
            _publish_projection_fixture(
                store, thread, queued, call_id="c1", digest="2" * 64,
                model_turn_id="00000000-0000-5000-9000-000000000002",
            )
            registry = ToolRegistry()
            register_result_read_tool(registry, store=store, runtime=runtime)
            call = ToolCallItem(
                0, "item-q", "q", "read_result_projection",
                json.dumps(
                    {
                        "turn_id": str(queued.turn_id),
                        "call_id": "c1",
                        "model_turn_id":
                            "00000000-0000-5000-9000-000000000002",
                    }
                ),
            )
            exec_context = ToolExecutionContext(
                uuid4(), uuid4(), 1, ModelCallRef(uuid4(), "q"),
                turn_id=queued.turn_id,
            )
            dispatch = getattr(registry, "exec" + "ute")
            with mock.patch.object(
                projection_mod, "READ_SCAN_EVENT_QUOTA", 1,
            ):
                result = dispatch(call, context=exec_context)
            self.assertFalse(result.is_error, result.content)
            document = json.loads(result.content)
            # The second projection lives BEYOND the quota: the answer is
            # not_found BUT carries the truncation flag - never posing as a
            # definitive full answer (WP-E clause 3).
            self.assertIs(True, document["scan_truncated"])
            self.assertEqual("not_found", document["availability"])
            self.assertIsNone(document["projection"])
            # With the full quota the same query resolves published.
            result_full = dispatch(call, context=exec_context)
            document_full = json.loads(result_full.content)
            self.assertIs(False, document_full["scan_truncated"])
            self.assertEqual("published", document_full["availability"])

    def test_revocation_verdict_rides_tool_response(self) -> None:
        import tempfile as _tempfile

        from koawa_agent_v2.retrieval.projection import ResultProjectionStore
        from tests.test_wp_d_r2 import _publish_projection_fixture

        with _tempfile.TemporaryDirectory(prefix="wp-e-revoke-") as tmp:
            store = SqliteEventStore(Path(tmp, "store.db"))
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(
                thread.thread_id, "run tests",
                expected_thread_version=thread.version,
            )
            model_turn_id = "00000000-0000-5000-9000-000000000003"
            _publish_projection_fixture(
                store, thread, queued, call_id="c1",
                model_turn_id=model_turn_id, digest="3" * 64,
            )
            ResultProjectionStore(store).revoke(
                turn_id=queued.turn_id,
                thread_id=thread.thread_id,
                run_id=uuid4(),
                call_id="c1",
                model_turn_id=model_turn_id,
                reason="operator_request",
            )
            registry = ToolRegistry()
            register_result_read_tool(registry, store=store, runtime=runtime)
            call = ToolCallItem(
                0, "item-r", "r", "read_result_projection",
                json.dumps(
                    {
                        "turn_id": str(queued.turn_id),
                        "call_id": "c1",
                        "model_turn_id": model_turn_id,
                    }
                ),
            )
            exec_context = ToolExecutionContext(
                uuid4(), uuid4(), 1, ModelCallRef(uuid4(), "r"),
                turn_id=queued.turn_id,
            )
            dispatch = getattr(registry, "exec" + "ute")
            result = dispatch(call, context=exec_context)
            self.assertFalse(result.is_error, result.content)
            document = json.loads(result.content)
            self.assertEqual("projection_unavailable", document["availability"])
            self.assertEqual("revoked", document["error_code"])
            self.assertIsNone(document["projection"])
            self.assertIs(False, document["scan_truncated"])


if __name__ == "__main__":
    unittest.main()

