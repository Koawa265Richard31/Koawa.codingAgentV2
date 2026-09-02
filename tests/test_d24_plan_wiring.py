"""D24 W1 wiring: durable journal, session projection, sealed registry join."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.control.event_store import (
    EventMetadata,
    NewEvent,
    StreamWrite,
)
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import ModelCallRef, ToolExecutionContext
from koawa_agent_v2.model.protocol import ToolCallItem, UserMessage
from koawa_agent_v2.plan import PlanBoard, PlanDurableJournal, PlanError, register_plan_tool
from koawa_agent_v2.plan.durable import PLAN_EVENT_TYPE
from koawa_agent_v2.runtime.session import SessionHistory, SessionTurn
from koawa_agent_v2.tools.registry import ToolRegistry


def _context() -> ToolExecutionContext:
    turn = uuid4()
    return ToolExecutionContext(
        run_id=uuid4(),
        model_turn_id=turn,
        model_round=1,
        call_ref=ModelCallRef(model_turn_id=turn, call_id="call-1"),
    )


def _call(texts: list[str], statuses: list[str]) -> ToolCallItem:
    return ToolCallItem(
        0,
        "item-1",
        "call-1",
        "update_plan",
        json.dumps({"texts": texts, "statuses": statuses}, ensure_ascii=False),
    )


class PlanDurableJournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteEventStore(Path(self._tmp.name) / "plan.sqlite3")
        self.thread_id = uuid4()

    def tearDown(self) -> None:
        close = getattr(self.store, "close", None)
        if close is not None:
            close()
        self._tmp.cleanup()

    def test_roundtrip_two_snapshots_on_thread_stream(self) -> None:
        board = PlanBoard()
        journal = PlanDurableJournal(self.store, self.thread_id)
        board.bind_journal(journal.append)
        board.replace(["first", "second"], ["pending", "pending"])
        board.set_status(1, "done")
        events = list(journal._events())
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e.event_type == PLAN_EVENT_TYPE for e in events))
        self.assertEqual(journal.current_version(), 1)
        reloaded = PlanDurableJournal(self.store, self.thread_id).load()
        self.assertEqual(
            tuple((i.item_id, i.text, i.status) for i in reloaded),
            ((1, "first", "done"), (2, "second", "pending")),
        )

    def test_empty_thread_loads_empty(self) -> None:
        self.assertEqual(PlanDurableJournal(self.store, uuid4()).load(), ())

    def test_corrupt_latest_snapshot_fails_closed(self) -> None:
        from datetime import datetime, timezone

        journal = PlanDurableJournal(self.store, self.thread_id)
        command = uuid4()
        bad = NewEvent(
            uuid4(),
            PLAN_EVENT_TYPE,
            1,
            datetime.now(timezone.utc),
            {"items": "not-a-list"},
            EventMetadata(command, command, thread_id=self.thread_id, actor="runtime"),
        )
        self.store.append_batch(
            (StreamWrite(
                stream_id=journal.stream(),
                expected_version=-1,
                events=(bad,),
            ),),
            idempotency_key=command,
        )
        with self.assertRaises(PlanError) as raised:
            journal.load()
        self.assertEqual("plan_stream_corrupt", raised.exception.code)

    def test_other_threads_are_isolated(self) -> None:
        board = PlanBoard()
        journal = PlanDurableJournal(self.store, self.thread_id)
        board.bind_journal(journal.append)
        board.replace(["mine"], ["pending"])
        self.assertEqual(PlanDurableJournal(self.store, uuid4()).load(), ())


class SessionPlanProjectionTest(unittest.TestCase):
    def _history(self, **kwargs) -> SessionHistory:
        history = SessionHistory(provider="provider", **kwargs)
        history.append(SessionTurn(user_input="do it", final_text="done", status="completed"))
        return history

    def test_plan_is_first_context_item_as_user_message(self) -> None:
        history = self._history(plan_projection=lambda: "PLAN-STATE")
        items = history.context_items()
        first = items[0]
        self.assertIsInstance(first, UserMessage)
        assert isinstance(first, UserMessage)
        self.assertEqual("session:plan", first.input_id)
        self.assertEqual("PLAN-STATE", first.content)
        self.assertNotIn("session:plan", [getattr(i, "input_id", None) for i in items[1:]])

    def test_absent_without_projection(self) -> None:
        items = self._history().context_items()
        self.assertNotIn(
            "session:plan", [getattr(i, "input_id", None) for i in items]
        )

    def test_projection_setter_validates(self) -> None:
        history = SessionHistory(provider="provider")
        with self.assertRaises(TypeError):
            history.plan_projection = "not-callable"  # type: ignore[assignment]
        history.plan_projection = lambda: "x"
        assert history.plan_projection is not None
        self.assertEqual("x", history.plan_projection())


class SealedRegistryJoinTest(unittest.TestCase):
    def test_update_plan_joins_verified_coding_registry(self) -> None:
        import subprocess

        from koawa_agent_v2.verification.runner import CommandProfile
        from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry

        with tempfile.TemporaryDirectory() as raw:
            subprocess.run(
                ["git", "init", "-q"], cwd=raw, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            registry = build_verified_coding_tool_registry(
                Path(raw),
                command_profiles=(
                    CommandProfile(
                        profile_id="python_unittest",
                        argv=(sys.executable, "-m", "unittest", "discover", "-s", "tests"),
                    ),
                ),
            )
            board = PlanBoard()
            register_plan_tool(registry, board)
            names = {d.name for d in registry.definitions()}
            names_before = names - {"update_plan"}
        self.assertIn("read_file", names_before)
        self.assertIn("update_plan", names)
        result = registry.execute(
            _call(["step a", "step b"], ["pending", "done"]), context=_context()
        )
        self.assertFalse(result.is_error)
        self.assertEqual(board.snapshot()[0].text, "step a")
        self.assertEqual(board.snapshot()[1].status, "done")

    def test_plan_walkthrough_three_subtasks(self) -> None:
        """W1 完成门：计划驱动 ≥3 个子任务的全链路（registry→board→durable→投影）。"""
        with tempfile.TemporaryDirectory() as raw:
            store = SqliteEventStore(Path(raw) / "walkthrough.sqlite3")
            try:
                board = PlanBoard()
                journal = PlanDurableJournal(store, UUID(int=42))
                board.restore(())
                board.bind_journal(journal.append)
                registry = ToolRegistry()
                register_plan_tool(registry, board)
                history = SessionHistory(
                    provider="provider",
                    plan_projection=board.authoritative_projection,
                )
                steps = ["parse config", "apply fix", "run tests"]
                for index in range(3):
                    statuses = ["done"] * (index + 1) + ["pending"] * (2 - index)
                    result = registry.execute(
                        _call(steps, statuses), context=_context()
                    )
                    self.assertFalse(result.is_error)
                    history.append(
                        SessionTurn(
                            user_input=f"advance step {index + 1}",
                            final_text=f"step {index + 1} complete",
                            status="completed",
                        )
                    )
                    items = history.context_items()
                    first = items[0]
                    self.assertIsInstance(first, UserMessage)
                    assert isinstance(first, UserMessage)
                    self.assertIn(f"[{index + 1}][x]", first.content)
                # 持久层与内存层一致；重放恢复得到全完成计划。
                reloaded = PlanDurableJournal(store, UUID(int=42)).load()
                self.assertTrue(all(i.status == "done" for i in reloaded))
                self.assertEqual(len(reloaded), 3)
                self.assertIn("pending=0 done=3", board.authoritative_projection())
            finally:
                close = getattr(store, "close", None)
                if close is not None:
                    close()


if __name__ == "__main__":
    unittest.main()
