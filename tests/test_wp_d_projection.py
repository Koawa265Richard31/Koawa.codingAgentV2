"""WP-D regression: durable metadata-only projections for test-result facts.

R1 (closure review 2026-09-25) coverage:
- the stream head is observed by walking ascending pages, so a turn with
  five different facts publishes all five (the old head read returned the
  OLDEST events and broke the fourth append);
- same-identity retries are idempotent, same identity with different
  content is rejected, and a concurrent head advance is retried, not
  aborted;
- a fact whose publication fails is registered durably as a pending
  projection and recovers on re-run without duplicates;
- the production app path (AppRuntime.run) publishes projections for real
  run_test_profile facts and exposes publication status on the turn
  outcome, including observable failure without failing the turn.

All fixtures use isolated TemporaryDirectory scratch spaces (no fixed
directory under the repository is created or deleted).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from uuid import UUID, uuid4

from koawa_agent_v2.control.event_store import (
    EventStoreError,
    IdempotencyConflict,
    StreamId,
)
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import AgentLoop
from koawa_agent_v2.execution.worker import TurnWorker
from koawa_agent_v2.recovery.store import CheckpointStore
from koawa_agent_v2.retrieval.projection import (
    PROJECTION_UNAVAILABLE_EVENT,
    PROJECTION_PUBLISHED_EVENT,
    ResultProjectionStore,
    scan_test_results,
)
from koawa_agent_v2.runtime.memory import MemoryConfig
from tests.test_agent_loop import ScriptedClient, _final_script, _tool_script
from tests.test_output_policy_gate import _ScriptedRunner
from tests.test_runtime_assembly import (
    _RepairModel,
    _init_repo,
)


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


def _fact_kwargs(turn_id, thread_id, run_id, index: int):
    """Distinct publish inputs per index; the content digest is deterministic
    so retries reuse the SAME identity (identity = turn+call+digest), and
    diagnostics vary to make content conflicts detectable."""

    return {
        "turn_id": turn_id,
        "thread_id": thread_id,
        "run_id": run_id,
        "call_id": f"call-{index}",
        "source_kind": "test",
        "diagnostics": {"exit_code": index, "outcome": "passed"},
        "body_ref": {
            "stream": "run-execution",
            "turn_id": str(turn_id),
            "model_turn_id": f"00000000-0000-5000-9000-{index:012d}",
            "content_sha256": f"{index:064d}",
        },
    }


class ResultProjectionHeadAndIdempotencyTest(unittest.TestCase):
    """R1 store-level acceptance: five facts, idempotency, conflict, races."""

    def test_five_facts_idempotent_retry_and_content_conflict(self) -> None:
        with TemporaryDirectory(prefix="koawa-wpd-r1-") as tmp:
            store = SqliteEventStore(Path(tmp, "store.db"))
            projections = ResultProjectionStore(store)
            turn_id, thread_id, run_id = uuid4(), uuid4(), uuid4()

            for index in range(5):
                projections.publish(
                    **_fact_kwargs(turn_id, thread_id, run_id, index)
                )

            stream = store.read_stream(
                StreamId("result-projection", turn_id), after_version=-1, limit=100
            )
            # Stream versions are 0-based; all five facts must land in order.
            self.assertEqual(
                [event.stream_version for event in stream], [0, 1, 2, 3, 4]
            )
            self.assertTrue(
                all(
                    event.event_type == PROJECTION_PUBLISHED_EVENT
                    for event in stream
                )
            )

            # Same-identity retries are idempotent: the store returns the
            # original receipt instead of appending duplicates.
            for index in range(5):
                projections.publish(
                    **_fact_kwargs(turn_id, thread_id, run_id, index)
                )
            stream = store.read_stream(
                StreamId("result-projection", turn_id), after_version=-1, limit=100
            )
            self.assertEqual(5, len(stream))

            # Same identity with different content is rejected and writes
            # nothing.
            conflicting = _fact_kwargs(turn_id, thread_id, run_id, 0)
            conflicting["diagnostics"] = {"exit_code": 99, "outcome": "failed"}
            with self.assertRaises(IdempotencyConflict):
                projections.publish(**conflicting)
            stream = store.read_stream(
                StreamId("result-projection", turn_id), after_version=-1, limit=100
            )
            self.assertEqual(5, len(stream))

    def test_concurrent_head_advance_is_retried(self) -> None:
        with TemporaryDirectory(prefix="koawa-wpd-race-") as tmp:
            real = SqliteEventStore(Path(tmp, "store.db"))
            turn_id, thread_id, run_id = uuid4(), uuid4(), uuid4()

            class _RacingStore:
                """Delegates to the real store; before the first append, a
                concurrent writer advances the projection-stream head so the
                guarded append observes exactly one CAS conflict."""

                def __init__(self) -> None:
                    self.raced = False

                def read_stream(self, *args, **kwargs):
                    return real.read_stream(*args, **kwargs)

                def append_batch(self, writes, **kwargs):
                    if not self.raced:
                        self.raced = True
                        ResultProjectionStore(real).publish(
                            **_fact_kwargs(turn_id, thread_id, run_id, 99)
                        )
                    return real.append_batch(writes, **kwargs)

            projections = ResultProjectionStore(_RacingStore())
            projections.publish(
                **_fact_kwargs(turn_id, thread_id, run_id, 0)
            )

            stream = real.read_stream(
                StreamId("result-projection", turn_id), after_version=-1, limit=100
            )
            self.assertEqual(2, len(stream))
            self.assertEqual(
                {"call-99", "call-0"},
                {event.payload["call_id"] for event in stream},
            )

    def test_failure_registration_is_durable_idempotent_and_recovers(self) -> None:
        with TemporaryDirectory(prefix="koawa-wpd-fail-") as tmp:
            path = Path(tmp, "store.db")
            store = SqliteEventStore(path)
            turn_id, thread_id, run_id = uuid4(), uuid4(), uuid4()
            fact = _fact_kwargs(turn_id, thread_id, run_id, 0)
            body_ref = fact["body_ref"]

            projections = ResultProjectionStore(store)
            projections.register_publication_failure(
                turn_id=turn_id,
                thread_id=thread_id,
                run_id=run_id,
                call_id=fact["call_id"],
                body_ref=body_ref,
                error_code="EventStoreError",
            )

            def failure_events():
                return [
                    event
                    for event in store.read_stream(
                        StreamId("result-projection", turn_id),
                        after_version=-1,
                        limit=100,
                    )
                    if event.event_type == PROJECTION_UNAVAILABLE_EVENT
                ]

            markers = failure_events()
            self.assertEqual(1, len(markers))
            payload = dict(markers[0].payload)
            self.assertEqual("failed", payload["publication_status"])
            self.assertEqual("EventStoreError", payload["error_code"])
            self.assertEqual(fact["call_id"], payload["call_id"])
            self.assertEqual("metadata_only", payload["visibility"])

            # Retrying the same failure registration stays idempotent.
            projections.register_publication_failure(
                turn_id=turn_id,
                thread_id=thread_id,
                run_id=run_id,
                call_id=fact["call_id"],
                body_ref=body_ref,
                error_code="EventStoreError",
            )
            self.assertEqual(1, len(failure_events()))

            # Recovery: a restarted store re-runs the whole publication for
            # the turn; the pending fact publishes, nothing duplicates.
            restarted = ResultProjectionStore(SqliteEventStore(path))
            restarted.publish(**fact)
            restarted.publish(**fact)  # idempotent under restart too
            stream = store.read_stream(
                StreamId("result-projection", turn_id), after_version=-1, limit=100
            )
            self.assertEqual(2, len(stream))
            self.assertEqual(
                {PROJECTION_PUBLISHED_EVENT, PROJECTION_UNAVAILABLE_EVENT},
                {event.event_type for event in stream},
            )
            published = [
                event
                for event in stream
                if event.event_type == PROJECTION_PUBLISHED_EVENT
            ]
            self.assertEqual(fact["call_id"], published[0].payload["call_id"])


class WorkerTerminalScanTest(unittest.TestCase):
    """Fixed fixture: the verified registry really executes run_test_profile,
    so the terminal turn records a policy-marked test receipt."""

    def test_verified_registry_turn_completes_and_scan_finds_fact(self) -> None:
        if shutil.which("git") is None:
            raise unittest.SkipTest("git is not installed")
        with TemporaryDirectory(prefix="koawa-wpd-worker-") as tmp:
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
                _final_script("done", "wpd-final"),
            ]
            loop = AgentLoop(
                ScriptedClient(*scripts),
                tool_executor=executor,
                memory=_memory(),
            )
            worker = TurnWorker(
                runtime, loop, provider="test", model="model",
                checkpoint_store=CheckpointStore(store),
            )
            queued = runtime.create_turn(
                thread.thread_id, "run tests",
                expected_thread_version=thread.version,
            )
            result = worker.execute(
                queued.turn_id,
                queued.version,
            )
            self.assertEqual("completed", result.turn.status.value)

            scan = scan_test_results(store, queued.turn_id)
            self.assertEqual(0, scan.untrusted, scan)
            self.assertEqual(1, len(scan.facts))
            self.assertEqual("c1", scan.facts[0]["call_id"])
            receipt = scan.facts[0]["receipt"]
            self.assertIn("test_output_policy", receipt)
            self.assertIn("outcome", receipt)
            self.assertNotIn("stdout", receipt)
            self.assertNotIn("stderr", receipt)
            self.assertEqual(64, len(scan.facts[0]["content_sha256"]))


def _assembly_config(base: Path, root: Path):
    from koawa_agent_v2.runtime.config import (
        PolicyConfig,
        ProviderConfig,
        RepositoryTrustMode,
        RuntimeConfig,
        SandboxConfig,
        SandboxRunner,
        TestProfileConfig,
    )

    return RuntimeConfig(
        repo=root,
        db=Path(base, "state", "agent.db"),
        provider=ProviderConfig(
            base_url="http://127.0.0.1:1/v1",
            api_key_env="P0_TEST_KEY",
            model="test-model",
        ),
        sandbox=SandboxConfig(
            runner=SandboxRunner.HOST,
            host_trust=RepositoryTrustMode.BUILTIN_FIXTURE,
        ),
        test_profiles=(
            TestProfileConfig(
                "unit",
                (
                    str(Path(sys.executable).resolve()),
                    "-B",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                ),
                timeout_seconds=30,
            ),
        ),
        policy=PolicyConfig(),
        system_prompt="Repair the failing test and finalize evidence.",
    )


_APP_PY = """def add(left, right):
    return left - right
"""

_TEST_APP_PY = """import unittest
from app import add

class AppTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(5, add(2, 3))
"""


def _fixture_repo(base: Path) -> Path:
    root = Path(base, "repo")
    root.mkdir()
    Path(root, "tests").mkdir()
    Path(root, "app.py").write_text(_APP_PY, encoding="utf-8")
    Path(root, "tests", "test_app.py").write_text(_TEST_APP_PY, encoding="utf-8")
    _init_repo(root)
    return root


def _projection_events(app, turn_id):
    return app.assembled.store.read_stream(
        StreamId("result-projection", turn_id), after_version=-1, limit=100
    )


class AppProjectionPublicationTest(unittest.TestCase):
    """Production entry: AppRuntime.run publishes projections and exposes
    the publication status on the turn outcome."""

    def test_run_publishes_projections_and_reports_status(self) -> None:
        from koawa_agent_v2.runtime.app import AppRuntime

        with TemporaryDirectory(prefix="koawa-wpd-app-") as tmp:
            base = Path(tmp)
            root = _fixture_repo(base)
            app = AppRuntime(_assembly_config(base, root), model_client=_RepairModel())
            self.addCleanup(app.close)
            outcome = app.run("make tests pass")
            self.assertTrue(outcome.ok, outcome.payload)
            self.assertEqual("completed", outcome.payload["status"])

            status = outcome.payload["result_projections"]
            self.assertEqual(2, status["published"], status)
            self.assertEqual(0, status["failed"])
            self.assertEqual([], status["pending"])
            self.assertIsNone(status["scan_error"])

            turn_id = UUID(outcome.payload["turn_id"])
            events = _projection_events(app, turn_id)
            published = [
                event
                for event in events
                if event.event_type == PROJECTION_PUBLISHED_EVENT
            ]
            self.assertEqual(2, len(published))
            self.assertEqual(
                {"test-before", "test-after"},
                {event.payload["call_id"] for event in published},
            )
            for event in published:
                payload = dict(event.payload)
                self.assertEqual("metadata_only", payload["visibility"])
                self.assertEqual("published", payload["publication_status"])
                self.assertNotIn("stdout", payload["diagnostics"])
                self.assertNotIn("stderr", payload["diagnostics"])
                self.assertEqual("run-execution", payload["body_ref"]["stream"])

    def test_publication_failure_is_observable_and_turn_still_completes(self) -> None:
        from koawa_agent_v2.runtime.app import AppRuntime

        with TemporaryDirectory(prefix="koawa-wpd-appfail-") as tmp:
            base = Path(tmp)
            root = _fixture_repo(base)
            app = AppRuntime(_assembly_config(base, root), model_client=_RepairModel())
            self.addCleanup(app.close)

            real_publish = ResultProjectionStore.publish

            def flaky_publish(self, *, call_id, **kwargs):
                if call_id == "test-before":
                    raise EventStoreError("injected publication failure")
                return real_publish(self, call_id=call_id, **kwargs)

            with mock.patch.object(ResultProjectionStore, "publish", flaky_publish):
                outcome = app.run("make tests pass")

            # Projection failure must not fail the completed turn...
            self.assertTrue(outcome.ok, outcome.payload)
            self.assertEqual("completed", outcome.payload["status"])
            # ...and must be observable on the outcome, not swallowed.
            status = outcome.payload["result_projections"]
            self.assertEqual(1, status["published"], status)
            self.assertEqual(1, status["failed"])
            self.assertEqual("test-before", status["pending"][0]["call_id"])
            self.assertEqual(
                "EventStoreError", status["pending"][0]["error_code"]
            )

            turn_id = UUID(outcome.payload["turn_id"])
            events = _projection_events(app, turn_id)
            published = [
                event
                for event in events
                if event.event_type == PROJECTION_PUBLISHED_EVENT
            ]
            failures = [
                event
                for event in events
                if event.event_type == PROJECTION_UNAVAILABLE_EVENT
            ]
            # The later fact still published after the first one failed.
            self.assertEqual({"test-after"}, {e.payload["call_id"] for e in published})
            # The gap is durably registered as a pending projection.
            self.assertEqual(1, len(failures))
            self.assertEqual("test-before", failures[0].payload["call_id"])
            self.assertEqual("EventStoreError", failures[0].payload["error_code"])

            # The completed tool results are untouched: both test receipts
            # remain recorded on the run-execution stream.
            run_events = app.assembled.store.read_stream(
                StreamId("run-execution", turn_id),
                after_version=-1,
                limit=1000,
            )
            recorded = [
                event
                for event in run_events
                if event.event_type == "tool.result-recorded.v1"
            ]
            self.assertGreaterEqual(len(recorded), 2)

            # Recovery: once the fault clears, re-running the publication
            # for the same turn fills the gap without duplicating facts.
            turn = app.assembled.runtime.get_turn(turn_id)
            recovered = app._publish_result_projections(turn)
            self.assertEqual(2, recovered["published"], recovered)
            self.assertEqual(0, recovered["failed"])
            events = _projection_events(app, turn_id)
            published = [
                event
                for event in events
                if event.event_type == PROJECTION_PUBLISHED_EVENT
            ]
            self.assertEqual(
                {"test-before", "test-after"},
                {event.payload["call_id"] for event in published},
            )


class DurablePublicationStatusTest(unittest.TestCase):
    """Store-level reader: a policy-marked receipt WITHOUT the trusted
    ledger identity is excluded from publishable facts and counted as
    untrusted (R2 source binding)."""

    def test_unbound_fact_is_untrusted_not_pending(self) -> None:
        from datetime import datetime, timezone

        from koawa_agent_v2.control.event_store import (
            EventMetadata,
            NewEvent,
            StreamWrite,
        )
        from koawa_agent_v2.retrieval.projection import (
            read_publication_status,
        )
        from koawa_agent_v2.verification.output_policy import POLICY_VERSION

        with TemporaryDirectory(prefix="koawa-wpd-status-") as tmp:
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
            store.append_batch(
                (write,),
                idempotency_key=command,
            )

            status = read_publication_status(store, queued.turn_id)
            self.assertEqual(0, status["published"], status)
            self.assertEqual([], status["pending"])
            self.assertEqual(1, status["untrusted"], status)
            self.assertEqual(0, status["unverified"], status)
            self.assertIsNone(status["scan_error"])


class RestartRecoveryEntryTest(unittest.TestCase):
    """R1 residual closure: after a restart, status exposes pending
    projections durably and resume republishes them through the production
    entry (no private-method calls)."""

    def test_restart_status_shows_pending_and_resume_republishes(self) -> None:
        from koawa_agent_v2.runtime.app import AppRuntime

        with TemporaryDirectory(prefix="koawa-wpd-restart-") as tmp:
            base = Path(tmp)
            root = _fixture_repo(base)

            def run_with_injected_failure():
                app = AppRuntime(
                    _assembly_config(base, root), model_client=_RepairModel(),
                )
                real_publish = ResultProjectionStore.publish

                def flaky_publish(self, *, call_id, **kwargs):
                    if call_id == "test-before":
                        raise EventStoreError("injected publication failure")
                    return real_publish(self, call_id=call_id, **kwargs)

                with mock.patch.object(
                    ResultProjectionStore, "publish", flaky_publish,
                ):
                    outcome = app.run("make tests pass")
                return app, outcome

            app, outcome = run_with_injected_failure()
            self.assertTrue(outcome.ok, outcome.payload)
            self.assertEqual("completed", outcome.payload["status"])
            turn_id = UUID(outcome.payload["turn_id"])
            app.close()  # simulated process restart

            restarted = AppRuntime(
                _assembly_config(base, root), model_client=_RepairModel(),
            )
            self.addCleanup(restarted.close)

            def turn_document(command):
                return next(
                    item
                    for item in command.payload["turns"]
                    if item["turn_id"] == str(turn_id)
                )

            # Post-restart status exposes the pending projection durably.
            status = restarted.status()
            self.assertTrue(status.ok, status.payload)
            durable = turn_document(status)["result_projections"]
            self.assertEqual(1, durable["published"], durable)
            self.assertEqual(1, len(durable["pending"]), durable)
            self.assertEqual("test-before", durable["pending"][0]["call_id"])
            self.assertEqual(
                "EventStoreError", durable["pending"][0]["error_code"]
            )
            self.assertTrue(durable["pending"][0]["model_turn_id"])
            self.assertIsNone(durable["scan_error"])

            # resume is the production recovery entry: republish idempotently.
            resumed = restarted.resume(turn_id)
            self.assertTrue(resumed.ok, resumed.payload)
            self.assertEqual("turn_already_terminal", resumed.code)
            recovered = resumed.payload["result_projections"]
            self.assertEqual(2, recovered["published"], recovered)
            self.assertEqual(0, recovered["failed"])
            self.assertEqual([], recovered["pending"])

            # status now reports the turn fully published.
            durable = turn_document(restarted.status())["result_projections"]
            self.assertEqual(2, durable["published"], durable)
            self.assertEqual([], durable["pending"])

            # No duplicates: exactly two published projections and the one
            # historical unavailable marker remain on the stream.
            events = _projection_events(restarted, turn_id)
            published = [
                event
                for event in events
                if event.event_type == PROJECTION_PUBLISHED_EVENT
            ]
            unavailable = [
                event
                for event in events
                if event.event_type == PROJECTION_UNAVAILABLE_EVENT
            ]
            self.assertEqual(
                {"test-before", "test-after"},
                {event.payload["call_id"] for event in published},
            )
            self.assertEqual(1, len(unavailable))

            # resume is idempotent: a second recovery entry adds nothing.
            resumed_again = restarted.resume(turn_id)
            self.assertTrue(resumed_again.ok, resumed_again.payload)
            self.assertEqual(
                2, resumed_again.payload["result_projections"]["published"]
            )
            events = _projection_events(restarted, turn_id)
            self.assertEqual(
                2,
                len([
                    event
                    for event in events
                    if event.event_type == PROJECTION_PUBLISHED_EVENT
                ]),
            )


if __name__ == "__main__":
    unittest.main()
