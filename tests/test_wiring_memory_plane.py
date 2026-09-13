"""Audit F12/F13/F15/F16 regressions: production wiring for the memory plane.

F15: update_plan / repo_map must pass the production PolicyEngine (they are
     registered and READ_ONLY-classified but were missing from the read
     rule's tool list -> denied_by_default, "registered but dead").
F16: chat/resume turns built by AppRuntime._execute must keep the claim gate.
F13: every terminal turn driven through AppRuntime._execute persists a
     TurnConclusion (gated on memory.conclusions_enabled).
F12: the production loop carries the D23 memory budgets and TurnWorker binds
     the per-run recorder as the durable compaction sink.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import (
    AgentLoop,
    ToolExecutionContext,
    ToolExecutionResult,
)
from koawa_agent_v2.execution.worker import TurnWorker
from koawa_agent_v2.model.protocol import (
    ModelCallRef,
    ModelStreamEvent,
    ToolCallItem,
    TurnCompleted,
)
from koawa_agent_v2.recovery.store import CheckpointStore
from koawa_agent_v2.runtime.app import AppRuntime
from koawa_agent_v2.runtime.memory import MemoryConfig
from tests.test_agent_loop import (
    READ_FILE,
    RecordingToolExecutor,
    ScriptedClient,
    ToolOutcome,
    _final_script,
    _tool_script,
)
from tests.test_assembly_verification_budget import _git_init, _write_config

import koawa_agent_v2.runtime.assembly as assembly_module


class _AppFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="koawa-wiring-")
        root = Path(self.tmp.name)
        (root / "repo").mkdir()
        _git_init(root / "repo")
        (root / "repo" / "README.md").write_text("fixture\n", encoding="utf-8")
        self.config_path = _write_config(root, required=["p1"], canary=None)

    def app(self) -> AppRuntime:
        # The execution plane is lazy: the provider key must stay in the
        # environment until the plane is (first) assembled in the test body.
        self._previous_key = os.environ.get("KOAWA_PROVIDER_KEY")
        os.environ["KOAWA_PROVIDER_KEY"] = "k" * 40
        return AppRuntime.from_config_file(self.config_path)

    def cleanup(self) -> None:
        if getattr(self, "_previous_key", "unset") != "unset":
            if self._previous_key is None:
                os.environ.pop("KOAWA_PROVIDER_KEY", None)
            else:
                os.environ["KOAWA_PROVIDER_KEY"] = self._previous_key
        self.tmp.cleanup()


def _tool_round(call_id: str):
    def script(request):
        call = ToolCallItem(
            0, f"i{call_id}", call_id, "read_file", '{"path":"a.py"}'
        )
        return (
            ModelStreamEvent(
                "t", "model", request.model_turn_id, None,
                TurnCompleted(request.model_turn_id),
            ),
            ModelStreamEvent("t", "model", request.model_turn_id, None, call),
        )

    return script


class ProductionPolicyAdmitsPlanAndRepoMapTest(unittest.TestCase):
    def test_update_plan_and_repo_map_execute_through_policy(self) -> None:
        fixture = _AppFixture()
        self.addCleanup(fixture.cleanup)
        app = fixture.app()
        self.addCleanup(app.close)
        execution = app._ensure_execution_plane()
        runtime = app.assembled.runtime
        thread = runtime.create_thread("repo")
        queued = runtime.create_turn(
            thread.thread_id, "map the repo", expected_thread_version=thread.version
        )
        running = runtime.start_turn(queued.turn_id, queued.version)
        run_id = running.current_run_id
        mid = uuid4()

        def context(call_id: str) -> ToolExecutionContext:
            return ToolExecutionContext(
                run_id,
                mid,
                1,
                ModelCallRef(mid, call_id),
                turn_id=running.turn_id,
                turn_version=running.version,
            )

        def call(name: str, arguments: dict, call_id: str) -> ToolCallItem:
            return ToolCallItem(
                0, f"item-{call_id}", call_id, name,
                json.dumps(arguments, separators=(",", ":")),
            )

        ledger_executor = execution.executor
        repo_map = ledger_executor.execute(
            # Tool-ledger entry: read-only repo_map over the fixture repo.
            call("repo_map", {"path": ".", "max_depth": 1}, "repo-map"),
            context=context("repo-map"),
        )
        self.assertFalse(repo_map.is_error, repo_map.content)

        update = ledger_executor.execute(
            # Tool-ledger entry: in-memory plan board replacement.
            call(
                "update_plan",
                {"texts": ["inspect", "edit"], "statuses": ["done", "pending"]},
                "plan-1",
            ),
            context=context("plan-1"),
        )
        self.assertFalse(update.is_error, update.content)


class ChatResumeClaimGateAndConclusionTest(unittest.TestCase):
    def test_chat_resume_worker_keeps_claim_gate_and_conclusion_persisted(
        self,
    ) -> None:
        fixture = _AppFixture()
        self.addCleanup(fixture.cleanup)
        app = fixture.app()
        self.addCleanup(app.close)
        captured: dict[str, object] = {}
        original = assembly_module.AssembledRuntime.build_worker

        def capture(self, *args, **kwargs):  # noqa: ANN001
            captured.update(kwargs)
            return original(self, *args, **kwargs)

        assembly_module.AssembledRuntime.build_worker = capture
        runtime = app.assembled.runtime
        thread = runtime.create_thread("repo")
        queued = runtime.create_turn(
            thread.thread_id,
            "just chat, no tools",
            expected_thread_version=thread.version,
        )
        app._chat_turn_ids.add(queued.turn_id)
        try:
            # The unreachable example provider fails the model call fast; the
            # worker is still built first, which is what this test captures.
            app._execute(queued.turn_id, queued.version, None)
        finally:
            assembly_module.AssembledRuntime.build_worker = original
        self.assertTrue(captured.get("claim_gate") is True)

        # F13: the (failed) terminal turn must have persisted a conclusion.
        store = SqliteEventStore(Path(fixture.tmp.name) / "state.sqlite3")
        events = store.read_stream(StreamId("turn-memory", queued.turn_id))
        self.assertTrue(
            any(
                event.event_type == "memory.turn-conclusion-recorded.v1"
                for event in events
            ),
            [event.event_type for event in events],
        )


class ProductionLoopMemoryWiringTest(unittest.TestCase):
    def test_task_and_chat_loops_carry_memory_config(self) -> None:
        fixture = _AppFixture()
        self.addCleanup(fixture.cleanup)
        app = fixture.app()
        self.addCleanup(app.close)
        execution = app._ensure_execution_plane()
        self.assertIs(app.config.memory, execution.loop._memory)
        chat_worker = execution.build_worker((), task_mode=False)
        self.assertIs(app.config.memory, chat_worker._loop._memory)


class WorkerBindsRunCompactionSinkTest(unittest.TestCase):
    def test_worker_binds_recorder_and_compacts_within_run(self) -> None:
        from koawa_agent_v2.editing.tools import build_coding_tool_registry
        from koawa_agent_v2.ledger import (
            LedgerExecutor,
            READ_ONLY_PROFILE,
            ToolLedgerStore,
        )

        with tempfile.TemporaryDirectory(prefix="koawa-f12-run-") as directory:
            base = Path(directory)
            db_path = base / "state.sqlite3"
            repo = base / "repo"
            repo.mkdir()
            (repo / "a.py").write_text("y" * 2400 + "\n", encoding="utf-8")
            store = SqliteEventStore(db_path)
            checkpoints = CheckpointStore(store)
            ledger = ToolLedgerStore(store)
            registry = build_coding_tool_registry(repo)
            self.addCleanup(registry.close)
            profiles = {
                definition.name: READ_ONLY_PROFILE
                for definition in registry.definitions()
            }
            executor = LedgerExecutor(registry, ledger, profiles)
            # Four read_file rounds (results are bounded by the tool) grow the
            # context past the tiny soft budget; closed groups then compact
            # through the per-run recorder the worker binds as the loop's sink.
            client = ScriptedClient(
                *(
                    _tool_script(
                        [(f"c{i}", "read_file", '{"path":"a.py"}')],
                        f"resp-{i}",
                    )
                    for i in range(1, 5)
                ),
                _final_script("done after compaction", "f12-final"),
            )
            loop = AgentLoop(
                client,
                tool_executor=executor,
                memory=MemoryConfig.from_mapping(
                    {
                        "request_context_soft_chars": 250,
                        "request_context_hard_chars": 8000,
                        "request_context_reserve_chars": 50,
                        "compaction_target_chars": 200,
                        "in_run_keep_groups": 1,
                    }
                ),
            )
            runtime = ThreadRuntime(store)
            thread = runtime.create_thread("repo")
            worker = TurnWorker(
                runtime,
                loop,
                provider="test",
                model="model",
                checkpoint_store=checkpoints,
            )
            queued = runtime.create_turn(
                thread.thread_id,
                "grow the context with tool results",
                expected_thread_version=thread.version,
            )
            result = worker.execute(
                # D1 TurnWorker entry: fresh queued turn.
                queued.turn_id,
                queued.version,
            )
            self.assertEqual("completed", result.turn.status.value)
            self.assertIsNone(loop._compaction_sink)
            facts = store.read_stream(StreamId("run-execution", queued.turn_id))
            compacted = sum(
                1 for e in facts if e.event_type == "run.context-compacted.v1"
            )
            self.assertGreaterEqual(compacted, 1, [e.event_type for e in facts])


if __name__ == "__main__":
    unittest.main()
