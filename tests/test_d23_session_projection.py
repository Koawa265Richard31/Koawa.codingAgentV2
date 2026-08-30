"""D23-B tests: session failed-turn echo, conclusion block, dedup rules.

Covers D23 §4.4 (failed echo format/bounds), §4.5 (dedup: in-window success
stays as original projection; in-window failure only as echo; out-of-window
conclusions only in the conclusion block; same turn_id+digest at most once)
and §8 (MemoryConfig defaults flow into the projection).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.model.protocol import AssistantMessage, UserMessage
from koawa_agent_v2.recovery.store import CheckpointStore
from koawa_agent_v2.runtime.memory import MemoryConfig
from koawa_agent_v2.runtime.session import (
    SessionHistory,
    SessionHistoryLimits,
    SessionTurn,
)
from koawa_agent_v2.runtime.turn_conclusion import TurnConclusionStore


def _conclusion_turn(turn_id, status="failed", error="max_model_rounds_exceeded"):
    return SessionTurn(
        user_input="task",
        final_text=None,
        turn_id=turn_id,
        status=status,
        error=error,
    )


class FailedEchoTest(unittest.TestCase):
    def setUp(self):
        self.history = SessionHistory(
            provider="test",
            limits=SessionHistoryLimits(max_turns=16, max_chars=32_000),
        )

    def test_failed_turn_produces_echo_without_conclusion_store(self):
        self.history.append(_conclusion_turn(uuid4()))
        items = self.history.context_items()
        echoes = [
            item
            for item in items
            if isinstance(item, AssistantMessage)
            and "[reconstructed-turn-outcome]" in item.item.text
        ]
        self.assertEqual(len(echoes), 1)
        text = echoes[0].item.text
        self.assertIn("status=failed", text)
        self.assertIn("errors=max_model_rounds_exceeded", text)
        self.assertIn("[/reconstructed-turn-outcome]", text)

    def test_successful_turn_keeps_original_projection_only(self):
        turn = SessionTurn(user_input="hi", final_text="hello")
        self.history.append(turn)
        items = self.history.context_items()
        assistants = [
            item for item in items if isinstance(item, AssistantMessage)
        ]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0].item.text, "hello")
        self.assertNotIn("reconstructed", assistants[0].item.text)

    def test_echo_limit_keeps_newest_only(self):
        memory = MemoryConfig(failed_echo_max_turns=2)
        self.history = SessionHistory(
            provider="test",
            limits=SessionHistoryLimits(max_turns=16, max_chars=32_000),
            memory=memory,
        )
        for index in range(4):
            self.history.append(_conclusion_turn(uuid4()))
        items = self.history.context_items()
        echoes = [
            item
            for item in items
            if isinstance(item, AssistantMessage)
            and "[reconstructed-turn-outcome]" in item.item.text
        ]
        self.assertEqual(len(echoes), 2)  # newest two only


class ConclusionBlockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.db"
        self.store = SqliteEventStore(self.path)
        self.runtime = ThreadRuntime(self.store)
        self.conclusions = TurnConclusionStore(self.store, self.runtime)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_terminal_turn(self, label: str, fail: bool = False):
        thread = self.runtime.create_thread("repo")
        queued = self.runtime.create_turn(
            thread.thread_id, label, expected_thread_version=thread.version,
        )
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        if fail:
            self.runtime.fail_turn(
                running.turn_id,
                "d2:max_model_rounds_exceeded",
                expected_version=running.version,
                run_id=running.current_run_id,
            )
        else:
            self.runtime.complete_turn(
                running.turn_id,
                "ok",
                expected_version=running.version,
                run_id=running.current_run_id,
            )
        return running.turn_id

    def test_out_of_window_conclusion_enters_conclusion_block(self):
        turn_id = self._make_terminal_turn("task", fail=True)
        conclusion = self.conclusions.build(turn_id)
        self.conclusions.persist(conclusion)
        history = SessionHistory(
            provider="test",
            limits=SessionHistoryLimits(max_turns=1, max_chars=32_000),
            conclusions=self.conclusions,
        )
        # Two turns with max_turns=1: the first is pushed out of the window
        # and becomes a conclusion block candidate.
        history.append(_conclusion_turn(turn_id, status="failed"))
        history.append(SessionTurn(user_input="current", final_text="ok"))
        items = history.context_items()
        blocks = [
            item
            for item in items
            if isinstance(item, UserMessage)
            and "[reconstructed-turn-conclusion]" in item.content
        ]
        self.assertEqual(len(blocks), 1)
        self.assertIn("turn_id=" + str(turn_id), blocks[0].content)
        self.assertIn("status=failed", blocks[0].content)

    def test_conclusion_block_is_bounded_by_recent_limit(self):
        memory = MemoryConfig(conclusion_recent_limit=2)
        history = SessionHistory(
            provider="test",
            limits=SessionHistoryLimits(max_turns=1, max_chars=32_000),
            conclusions=self.conclusions,
            memory=memory,
        )
        for index in range(4):
            turn_id = self._make_terminal_turn(f"t{index}", fail=True)
            conclusion = self.conclusions.build(turn_id)
            self.conclusions.persist(conclusion)
            history.append(_conclusion_turn(turn_id, status="failed"))
        items = history.context_items()
        blocks = [
            item
            for item in items
            if isinstance(item, UserMessage)
            and "[reconstructed-turn-conclusion]" in item.content
        ]
        self.assertLessEqual(len(blocks), 2)

    def test_same_turn_id_and_digest_appears_at_most_once(self):
        turn_id = self._make_terminal_turn("task", fail=True)
        conclusion = self.conclusions.build(turn_id)
        self.conclusions.persist(conclusion)
        history = SessionHistory(
            provider="test",
            limits=SessionHistoryLimits(max_turns=1, max_chars=32_000),
            conclusions=self.conclusions,
        )
        # Same out-of-window turn appended twice (e.g. replay) must not
        # duplicate its conclusion block.
        history.append(_conclusion_turn(turn_id, status="failed"))
        history.append(_conclusion_turn(turn_id, status="failed"))
        history.append(SessionTurn(user_input="current", final_text="ok"))
        items = history.context_items()
        blocks = [
            item
            for item in items
            if isinstance(item, UserMessage)
            and "[reconstructed-turn-conclusion]" in item.content
        ]
        self.assertEqual(len(blocks), 1)


if __name__ == "__main__":
    unittest.main()
