"""WP-D R2 regressions (closure review 2026-09-25).

- in-loop publication ordering: the projection for a test receipt is
  durably committed BEFORE the receipt reaches any later model round;
- long-stream scan: the run-execution scan page-walks past the first
  500-event page (no silent truncation).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from koawa_agent_v2.control.event_store import (
    EventMetadata,
    EventStoreError,
    NewEvent,
    StreamId,
    StreamWrite,
)
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import AgentLoop
from koawa_agent_v2.model.protocol import InstructionMessage, InstructionRole
from koawa_agent_v2.retrieval.projection import (
    PROJECTION_PUBLISHED_EVENT,
    scan_test_results,
)
from koawa_agent_v2.runtime.memory import MemoryConfig
from koawa_agent_v2.verification.output_policy import POLICY_VERSION
from tests.test_agent_loop import ScriptedClient, _final_script, _tool_script
from tests.test_output_policy_gate import _ScriptedRunner


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


def _publish_projection_fixture(
    store,
    thread,
    queued,
    *,
    call_id="c1",
    model_turn_id=None,
    digest="a" * 64,
    failure=False,
    error_code="EventStoreError",
):
    """Publish (or register unavailable) one projection for a read-tool
    fixture; shared with tests/test_result_read_tool.py."""

    from koawa_agent_v2.retrieval.projection import ResultProjectionStore

    projections = ResultProjectionStore(store)
    body_ref = {
        "stream": "run-execution",
        "turn_id": str(queued.turn_id),
        "model_turn_id": model_turn_id or str(uuid4()),
        "content_sha256": digest,
    }
    if failure:
        projections.register_publication_failure(
            turn_id=queued.turn_id,
            thread_id=thread.thread_id,
            run_id=uuid4(),
            call_id=call_id,
            body_ref=body_ref,
            error_code=error_code,
        )
        return body_ref
    projections.publish(
        turn_id=queued.turn_id,
        thread_id=thread.thread_id,
        run_id=uuid4(),
        call_id=call_id,
        source_kind="test",
        diagnostics={"exit_code": 0, "outcome": "passed"},
        body_ref=body_ref,
    )
    return body_ref


class InLoopPublicationOrderingTest(unittest.TestCase):
    """R2 ordering: the projection for a test receipt is committed BEFORE
    the receipt is sent to any later model round (the second-round script
    itself reads the projection stream and records what it sees)."""

    def test_projection_committed_before_next_model_round(self) -> None:
        if shutil.which("git") is None:
            raise unittest.SkipTest("git is not installed")
        with TemporaryDirectory(prefix="koawa-wpd-order-") as tmp:
            repo = Path(tmp, "repo")
            repo.mkdir()
            Path(repo, "a.py").write_text("y" * 200 + chr(10), encoding="utf-8")
            subprocess.run(
                ("git", "-C", str(repo), "init", "-q"),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            from koawa_agent_v2.ledger import (
                LedgerExecutor,
                READ_ONLY_PROFILE,
                ToolLedgerStore,
            )
            from koawa_agent_v2.retrieval.projection import (
                make_test_receipt_publisher,
            )
            from koawa_agent_v2.verification.tools import (
                build_verified_coding_tool_registry,
            )

            store = SqliteEventStore(Path(tmp, "store.db"))
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread(str(repo))
            registry = build_verified_coding_tool_registry(
                repo,
                command_runner=_ScriptedRunner(),
                required_test_profiles=("only",),
            )
            self.addCleanup(registry.close)
            ledger = ToolLedgerStore(store)
            profiles = {item.name: READ_ONLY_PROFILE for item in registry.definitions()}
            executor = LedgerExecutor(registry, ledger, profiles)
            probe_seen = []

            base_final = _final_script("done", "wpd-final")

            def second_round(request):
                """Produced as the SECOND model round: the first round's
                receipt must already be durably published by then."""
                events = store.read_stream(
                    StreamId("result-projection", queued.turn_id),
                    after_version=-1,
                    limit=10,
                )
                for item in events:
                    if item.event_type == PROJECTION_PUBLISHED_EVENT:
                        probe_seen.append(item.payload["call_id"])
                return base_final(request)

            scripts = [
                _tool_script(
                    [("c1", "run_test_profile", json.dumps({"profile_id": "only"}))],
                    "r1",
                ),
                second_round,
            ]
            loop = AgentLoop(
                ScriptedClient(*scripts),
                tool_executor=executor,
                memory=_memory(),
                result_projection_publisher=make_test_receipt_publisher(
                    store, ledger, runtime,
                ),
            )
            queued = runtime.create_turn(
                thread.thread_id, "run tests",
                expected_thread_version=thread.version,
            )
            # Claim the run like the worker does, so the ledger's turn fence
            # (head event turn.started.v1) holds before the first tool call.
            start_command = uuid4()
            running = runtime.start_turn(
                queued.turn_id,
                queued.version,
                command_id=start_command,
            )
            loop.run(
                run_id=running.current_run_id,
                turn_id=queued.turn_id,
                turn_version=running.version,
                input_items=(
                    InstructionMessage(InstructionRole.SYSTEM, "run tests"),
                ),
                provider="test",
                model="model",
            )
            # The projection existed before the final round was produced.
            self.assertEqual(["c1"], probe_seen)
            # Exactly one published projection for the single fact.
            events = store.read_stream(
                StreamId("result-projection", queued.turn_id),
                after_version=-1,
                limit=10,
            )
            self.assertEqual(1, len(events))


class LongStreamScanTest(unittest.TestCase):
    """R2: scan page-walks the full run-execution stream (no 1000 cap)."""

    def test_scan_walks_past_first_page(self) -> None:
        with TemporaryDirectory(prefix="koawa-wpd-long-") as tmp:
            store = SqliteEventStore(Path(tmp, "store.db"))
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(
                thread.thread_id, "run tests",
                expected_thread_version=thread.version,
            )
            receipt = json.dumps(
                {"test_output_policy": POLICY_VERSION, "outcome": "passed"}
            )
            # 501 policy-marked receipts without ledger binding: all
            # untrusted, but counting them proves the scan read past the
            # 500-event first page.
            for index in range(501):
                item = {
                    "kind": "tool_result",
                    "model_turn_id": str(uuid4()),
                    "call_id": f"c{index}",
                    "content": receipt,
                    "is_error": False,
                }
                payload = {
                    "thread_id": str(thread.thread_id),
                    "turn_id": str(queued.turn_id),
                    "run_id": str(uuid4()),
                    "context_item": item,
                }
                command = uuid4()
                event = NewEvent(
                    uuid4(),
                    "tool.result-recorded.v1",
                    1,
                    datetime.now(timezone.utc),
                    payload,
                    EventMetadata(command, queued.turn_id),
                )
                prior_head = -1
                if index > 0:
                    prior_head = index - 1
                write = StreamWrite(
                    StreamId("run-execution", queued.turn_id),
                    prior_head,
                    (event,),
                )
                store.append_batch(
                    (write,),
                    idempotency_key=command,
                )
            scan = scan_test_results(store, queued.turn_id)
            self.assertEqual(0, len(scan.facts))
            self.assertEqual(501, scan.untrusted)


class CallIdentityAcrossModelTurnsTest(unittest.TestCase):
    """Re-verification 2026-09-25 must-fix: call_id is only unique within
    its model turn.  Two rounds reusing call_id=c1 with IDENTICAL receipts
    are two execution facts and must produce two projections (the digest
    alone never replaces the call identity)."""

    def test_same_call_id_in_two_rounds_publishes_twice(self) -> None:
        if shutil.which("git") is None:
            raise unittest.SkipTest("git is not installed")
        with TemporaryDirectory(prefix="koawa-wpd-ident-") as tmp:
            repo = Path(tmp, "repo")
            repo.mkdir()
            Path(repo, "a.py").write_text("y" * 200 + chr(10), encoding="utf-8")
            subprocess.run(
                ("git", "-C", str(repo), "init", "-q"),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            from koawa_agent_v2.ledger import (
                LedgerExecutor,
                READ_ONLY_PROFILE,
                ToolLedgerStore,
            )
            from koawa_agent_v2.retrieval.projection import (
                make_test_receipt_publisher,
            )
            from koawa_agent_v2.verification.tools import (
                build_verified_coding_tool_registry,
            )

            store = SqliteEventStore(Path(tmp, "store.db"))
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread(str(repo))
            registry = build_verified_coding_tool_registry(
                repo,
                command_runner=_ScriptedRunner(),
                required_test_profiles=("only",),
            )
            self.addCleanup(registry.close)
            ledger = ToolLedgerStore(store)
            profiles = {item.name: READ_ONLY_PROFILE for item in registry.definitions()}
            executor = LedgerExecutor(registry, ledger, profiles)

            scripts = [
                _tool_script(
                    [("c1", "run_test_profile", json.dumps({"profile_id": "only"}))],
                    "r1",
                ),
                # Round 2 reuses the SAME call_id with the SAME receipt
                # content coming back - a distinct execution fact.
                _tool_script(
                    [("c1", "run_test_profile", json.dumps({"profile_id": "only"}))],
                    "r2",
                ),
                _final_script("done", "ident-final"),
            ]
            loop = AgentLoop(
                ScriptedClient(*scripts),
                tool_executor=executor,
                memory=_memory(),
                result_projection_publisher=make_test_receipt_publisher(
                    store, ledger, runtime,
                ),
            )
            queued = runtime.create_turn(
                thread.thread_id, "run tests twice",
                expected_thread_version=thread.version,
            )
            start_command = uuid4()
            running = runtime.start_turn(
                queued.turn_id,
                queued.version,
                command_id=start_command,
            )
            loop.run(
                run_id=running.current_run_id,
                turn_id=queued.turn_id,
                turn_version=running.version,
                input_items=(
                    InstructionMessage(InstructionRole.SYSTEM, "run tests"),
                ),
                provider="test",
                model="model",
            )
            events = store.read_stream(
                StreamId("result-projection", queued.turn_id),
                after_version=-1,
                limit=10,
            )
            published = [
                event
                for event in events
                if event.event_type == PROJECTION_PUBLISHED_EVENT
            ]
            # The old digest-only identity collapsed these to ONE event.
            self.assertEqual(2, len(published), events)
            self.assertEqual(
                2,
                len({
                    event.payload["body_ref"]["model_turn_id"]
                    for event in published
                }),
            )
            # Same content digest on both - content verifies, identity
            # distinguishes.
            self.assertEqual(
                1,
                len({
                    event.payload["body_ref"]["content_sha256"]
                    for event in published
                }),
            )


class UnverifiedSourceClassificationTest(unittest.TestCase):
    """Re-verification 2026-09-25: a ledger READ FAILURE is not a source
    mismatch - the fact must fail closed (never published) but be counted
    as unverified, distinct from untrusted."""

    def test_ledger_read_failure_counts_unverified(self) -> None:
        with TemporaryDirectory(prefix="koawa-wpd-unver-") as tmp:
            real = SqliteEventStore(Path(tmp, "store.db"))
            runtime = ThreadRuntime(real)
            thread = runtime.create_thread("repo")
            queued = runtime.create_turn(
                thread.thread_id, "run tests",
                expected_thread_version=thread.version,
            )
            receipt = json.dumps(
                {"test_output_policy": POLICY_VERSION, "outcome": "passed"}
            )
            item = {
                "kind": "tool_result",
                "model_turn_id": str(uuid4()),
                "call_id": "c1",
                "content": receipt,
                "is_error": False,
            }
            payload = {
                "thread_id": str(thread.thread_id),
                "turn_id": str(queued.turn_id),
                "run_id": str(uuid4()),
                "context_item": item,
            }
            command = uuid4()
            event = NewEvent(
                uuid4(),
                "tool.result-recorded.v1",
                1,
                datetime.now(timezone.utc),
                payload,
                EventMetadata(command, queued.turn_id),
            )
            write = StreamWrite(
                StreamId("run-execution", queued.turn_id), -1, (event,),
            )
            real.append_batch(
                (write,),
                idempotency_key=command,
            )

            class _LedgerReadBrokenStore:
                """Passes run-execution reads through; every ledger
                tool-execution read fails (transient outage)."""

                def __init__(self, inner):
                    self._inner = inner

                def append_batch(self, *args, **kwargs):
                    return self._inner.append_batch(*args, **kwargs)

                def read_stream(self, stream_id, **kwargs):
                    if stream_id.category == "tool-execution":
                        raise EventStoreError("ledger read outage")
                    return self._inner.read_stream(stream_id, **kwargs)

            broken = _LedgerReadBrokenStore(real)
            scan = scan_test_results(broken, queued.turn_id)
            self.assertEqual(0, len(scan.facts))
            self.assertEqual(0, scan.untrusted)
            self.assertEqual(1, scan.unverified)

            from koawa_agent_v2.retrieval.projection import (
                read_publication_status,
            )

            status = read_publication_status(broken, queued.turn_id)
            self.assertEqual(0, status["published"], status)
            self.assertEqual(1, status["unverified"], status)
            self.assertEqual(0, status["untrusted"], status)


if __name__ == "__main__":
    unittest.main()
