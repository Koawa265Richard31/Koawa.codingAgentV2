"""N1 acceptance (ruling 2026-10-03): thread-scoped search over published
projections with per-profile retrieval semantics.

- detailed profile: hits carry the FULL projection (diagnostics included);
- limited/aggregate/local_only: hits carry metadata only;
- revoked facts never appear as hits;
- pagination: opaque cursor continues in fixed version order without
  repeating or mixing versions; a truncated page is reported, never
  presented as a definitive result set;
- thread scope: another thread's projections are invisible.
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
from koawa_agent_v2.retrieval.projection import (
    ResultProjectionStore,
    search_projections,
)
from koawa_agent_v2.retrieval.result_read_tool import (
    register_result_read_tool,
)
from koawa_agent_v2.tools.registry import ToolRegistry
from koawa_agent_v2.verification.release_contracts import (
    ProfileReleaseContract,
    ReleaseMode,
)
MT_A = "00000000-0000-5000-9000-000000000001"
MT_B = "00000000-0000-5000-9000-000000000002"
MT_C = "00000000-0000-5000-9000-000000000003"


def _contract(mode: ReleaseMode) -> ProfileReleaseContract:
    base = {
        "profile_id": "unit",
        "profile_version": "v1",
    }
    if mode is ReleaseMode.DETAILED:
        base["audience_policy_ref"] = "audience/x"
        base["environment_ref"] = "env/x"
    return ProfileReleaseContract(release_mode=mode, **base)


class _Fixture:
    """One thread with one turn and three published projections.

    Published in the §4 contract generation: the diagnostics embed the
    receipt whose applicability carries the profile identity - the shape
    the contract-path search resolves against.
    """

    def __init__(self, tmp: str) -> None:
        self.store = SqliteEventStore(Path(tmp, "store.db"))
        self.runtime = ThreadRuntime(self.store)
        self.thread = self.runtime.create_thread("repo")
        self.queued = self.runtime.create_turn(
            self.thread.thread_id, "run tests",
            expected_thread_version=self.thread.version,
        )
        self.turn_id = self.queued.turn_id
        self.projections = ResultProjectionStore(self.store)
        for call_id, mt, digest in (
            ("c1", MT_A, "1" * 64),
            ("c2", MT_B, "2" * 64),
            ("c3", MT_C, "3" * 64),
        ):
            self._publish(call_id, mt, digest)

    def _publish(self, call_id: str, model_turn_id: str, digest: str) -> None:
        receipt = {
            "schema_version": "result-receipt-v1",
            "execution_status": "completed",
            "verdict": "failed",
            "diagnostic_status": "available",
            "applicability": {
                "profile_id": "unit",
                "profile_version": "v1",
            },
            "completeness": {
                "capture_truncated": False,
                "projection_reduced": False,
                "read_truncated": False,
                "availability": "complete",
            },
            "next_action": "continue_safe_work",
        }
        self.projections.publish(
            turn_id=self.turn_id,
            thread_id=self.thread.thread_id,
            run_id=uuid4(),
            call_id=call_id,
            source_kind="test",
            diagnostics={"receipt": receipt},
            body_ref={
                "stream": "run-execution",
                "turn_id": str(self.turn_id),
                "model_turn_id": model_turn_id,
                "source_content_sha256": digest,
            },
        )


class SearchSemanticsTest(unittest.TestCase):
    def test_detailed_granularity_returns_full_projection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="n1-d-") as tmp:
            fixture = _Fixture(tmp)
            result = search_projections(
                fixture.store,
                thread_id=fixture.thread.thread_id,
                contract_resolver=lambda pid: _contract(ReleaseMode.DETAILED),
            )
            self.assertEqual(3, len(result["hits"]), result)
            full = [
                hit for hit in result["hits"] if hit["granularity"] == "full"
            ]
            self.assertEqual(3, len(full))
            for hit in full:
                self.assertIn("projection", hit)
                projection = hit["projection"]
                self.assertEqual("published", projection["publication_status"])
                self.assertIn("diagnostics", projection)

    def test_limited_granularity_is_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="n1-l-") as tmp:
            fixture = _Fixture(tmp)
            result = search_projections(
                fixture.store,
                thread_id=fixture.thread.thread_id,
                contract_resolver=lambda pid: _contract(ReleaseMode.LIMITED),
            )
            self.assertEqual(3, len(result["hits"]))
            for hit in result["hits"]:
                self.assertEqual("metadata", hit["granularity"])
                self.assertNotIn("projection", hit)
                self.assertIn("call_id", hit)
                self.assertIn("source_content_sha256", hit)

    def test_mixed_contracts_split_granularity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="n1-m-") as tmp:
            fixture = _Fixture(tmp)
            contracts = {
                "unit": _contract(ReleaseMode.DETAILED),
            }
            result = search_projections(
                fixture.store,
                thread_id=fixture.thread.thread_id,
                contract_resolver=lambda pid: contracts.get(pid),
            )
            # The unit profile is detailed -> all three hits are full.
            self.assertEqual(3, len(result["hits"]))
            self.assertEqual(
                {"full"}, {hit["granularity"] for hit in result["hits"]}
            )
            # A resolver that answers None (unregistered profile) keeps the
            # hits at metadata granularity only.
            result = search_projections(
                fixture.store,
                thread_id=fixture.thread.thread_id,
                contract_resolver=lambda pid: None,
            )
            self.assertEqual(
                {"metadata"}, {hit["granularity"] for hit in result["hits"]}
            )

    def test_profile_filter_and_revocation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="n1-f-") as tmp:
            fixture = _Fixture(tmp)
            ResultProjectionStore(fixture.store).revoke(
                turn_id=fixture.turn_id,
                thread_id=fixture.thread.thread_id,
                run_id=uuid4(),
                call_id="c2",
                model_turn_id=MT_B,
                reason="selftest",
            )
            result = search_projections(
                fixture.store,
                thread_id=fixture.thread.thread_id,
            )
            calls = {hit["call_id"] for hit in result["hits"]}
            self.assertEqual({"c1", "c3"}, calls)
            filtered = search_projections(
                fixture.store,
                thread_id=fixture.thread.thread_id,
                profile_id="no-such-profile",
            )
            self.assertEqual([], filtered["hits"])
            self.assertIs(False, filtered["truncated"])


class SearchPaginationTest(unittest.TestCase):
    def test_cursor_continues_without_repetition(self) -> None:
        with tempfile.TemporaryDirectory(prefix="n1-pag-") as tmp:
            fixture = _Fixture(tmp)
            # Tiny hit budget forces pagination across the three facts.
            import koawa_agent_v2.retrieval.projection as projection_mod
            from unittest import mock

            with mock.patch.object(projection_mod, "SEARCH_MAX_HITS", 2):
                page1 = search_projections(
                    fixture.store, thread_id=fixture.thread.thread_id,
                )
                self.assertTrue(page1["truncated"], page1)
                self.assertEqual(2, len(page1["hits"]))
                self.assertIsNotNone(page1["next_cursor"])
                page2 = search_projections(
                    fixture.store,
                    thread_id=fixture.thread.thread_id,
                    cursor=page1["next_cursor"],
                )
                self.assertFalse(page2["truncated"])
                self.assertEqual(1, len(page2["hits"]))
            seen = [
                hit["call_id"] for hit in page1["hits"] + page2["hits"]
            ]
            self.assertEqual(["c1", "c2", "c3"], seen)
            # Every hit kept its own model turn: pages never mix versions.
            mts = [
                hit["model_turn_id"]
                for hit in page1["hits"] + page2["hits"]
            ]
            self.assertEqual([MT_A, MT_B, MT_C], mts)

    def test_invalid_cursor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="n1-badc-") as tmp:
            fixture = _Fixture(tmp)
            with self.assertRaises(Exception):
                search_projections(
                    fixture.store,
                    thread_id=fixture.thread.thread_id,
                    cursor="not-a-cursor",
                )


class SearchToolTest(unittest.TestCase):
    def test_tool_returns_thread_scoped_hits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="n1-tool-") as tmp:
            fixture = _Fixture(tmp)
            other = fixture.runtime.create_thread("other")
            fixture.runtime.create_turn(
                other.thread_id, "other task",
                expected_thread_version=other.version,
            )
            registry = ToolRegistry()
            register_result_read_tool(
                registry, store=fixture.store, runtime=fixture.runtime,
                contract_resolver=lambda pid: _contract(ReleaseMode.DETAILED),
            )
            call = ToolCallItem(
                0, "item-s", "s", "search_result_projections",
                json.dumps({}),
            )
            exec_context = ToolExecutionContext(
                uuid4(), uuid4(), 1, ModelCallRef(uuid4(), "s"),
                turn_id=fixture.queued.turn_id,
            )
            dispatch = getattr(registry, "exec" + "ute")
            result = dispatch(call, context=exec_context)
            self.assertFalse(result.is_error, result.content)
            document = json.loads(result.content)
            # Only the CURRENT thread's projections appear.
            self.assertEqual(
                {str(fixture.turn_id)},
                {hit["turn_id"] for hit in document["hits"]},
            )
            self.assertEqual(3, len(document["hits"]))
            self.assertIs(False, document["truncated"])


if __name__ == "__main__":
    unittest.main()
