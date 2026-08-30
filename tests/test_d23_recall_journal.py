"""D23-F tests: recall IDF/recency formula, journal reminder triggers/reset.

Covers D23 §6 (tokenize -> stop words -> de-dup; idf = log((1+N)/(1+df))+1;
field weights request/final 2.0, error 1.5, tool/file 1.0; recency
0.5 + 0.5*(position+1)/N) and §7.1 (turns-since-journal, failed latest turn,
changed-files thresholds; reminder is a low-priority UserMessage and resets
after mark_journal_written).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.model.protocol import UserMessage
from koawa_agent_v2.runtime.memory import MemoryConfig
from koawa_agent_v2.runtime.session import (
    SessionHistory,
    SessionHistoryLimits,
    SessionTurn,
    _recall_idf,
    _recall_score,
    _tokenize,
)


class TokenizeTest(unittest.TestCase):
    def test_casefold_and_dedup_preserve_order(self):
        tokens = _tokenize("Fix the Fix FIX file")
        self.assertEqual(tokens, ("fix", "file"))

    def test_stop_words_removed(self):
        tokens = _tokenize("the quick and the slow")
        self.assertEqual(tokens, ("quick", "slow"))

    def test_cjk_tokens_kept(self):
        tokens = _tokenize("修复 这个 测试 文件")
        self.assertEqual(tokens, ("修复", "测试", "文件"))


class RecallFormulaTest(unittest.TestCase):
    def test_recall_score_uses_field_weights_and_recency(self):
        turn = SessionTurn(
            user_input="fix test",
            final_text="done",
            error="d2:max_model_rounds_exceeded",
            changed_files=("src/a.py",),
        )
        score_recent = _recall_score(
            ("test",), turn, ("run_test",),
            total_turns=10, turn_position=9,
        )
        score_old = _recall_score(
            ("test",), turn, ("run_test",),
            total_turns=10, turn_position=0,
        )
        self.assertGreater(score_recent, score_old)

    def test_idf_penalizes_common_terms(self):
        records = [
            (SessionTurn(user_input=f"error in task {index}", final_text="x"), ())
            for index in range(5)
        ]
        rare = _recall_idf(("unique_rare_term",), records)
        common = _recall_idf(("error",), records)
        self.assertGreater(rare["unique_rare_term"], common["error"])

    def test_recall_score_matches_field_weights(self):
        turn = SessionTurn(user_input="fix test", final_text="ok")
        base = _recall_score(("test",), turn, (), total_turns=1, turn_position=0)
        self.assertAlmostEqual(base, 2.0)  # request match x2, recency 1.0
        tooled = _recall_score(("run_test",), turn, ("run_test",),
                               total_turns=1, turn_position=0)
        self.assertAlmostEqual(tooled, 1.0)  # tool match x1


class JournalReminderTest(unittest.TestCase):
    def setUp(self):
        self.history = SessionHistory(
            provider="test",
            limits=SessionHistoryLimits(max_turns=16, max_chars=32_000),
            memory=MemoryConfig.from_mapping({
                "journal_remind_turns": 3,
                "journal_remind_changed_files": 2,
            }),
        )

    def test_no_reminder_with_few_turns(self):
        for index in range(2):
            self.history.append(SessionTurn(user_input=f"t{index}", final_text="ok"))
        items = self.history.context_items()
        self.assertFalse(
            any(
                isinstance(item, UserMessage)
                and "session-memory-reminder" in item.content
                for item in items
            )
        )

    def test_turns_since_journal_triggers_reminder(self):
        for index in range(3):
            self.history.append(SessionTurn(user_input=f"t{index}", final_text="ok"))
        items = self.history.context_items()
        reminders = [
            item for item in items
            if isinstance(item, UserMessage)
            and "session-memory-reminder" in item.content
        ]
        self.assertEqual(len(reminders), 1)
        self.assertIn("turns_since_journal", reminders[0].content)

    def test_failed_latest_turn_triggers_reminder(self):
        self.history.append(SessionTurn(user_input="t0", final_text="ok"))
        self.history.append(
            SessionTurn(user_input="t1", final_text=None, status="failed",
                        error="d2:model_client_failed")
        )
        items = self.history.context_items()
        reminders = [
            item for item in items
            if isinstance(item, UserMessage)
            and "session-memory-reminder" in item.content
        ]
        self.assertEqual(len(reminders), 1)
        self.assertIn("recent_turn_failed", reminders[0].content)

    def test_changed_files_threshold_triggers_reminder(self):
        self.history.append(
            SessionTurn(user_input="t0", final_text="ok",
                        changed_files=("a.py", "b.py"))
        )
        items = self.history.context_items()
        reminders = [
            item for item in items
            if isinstance(item, UserMessage)
            and "session-memory-reminder" in item.content
        ]
        self.assertEqual(len(reminders), 1)
        self.assertIn("changed_files=2", reminders[0].content)

    def test_mark_journal_written_resets_reminder(self):
        for index in range(3):
            self.history.append(SessionTurn(user_input=f"t{index}", final_text="ok"))
        self.assertIn(
            "session-memory-reminder",
            "".join(
                item.content for item in self.history.context_items()
                if isinstance(item, UserMessage)
            ),
        )
        self.history.mark_journal_written()
        reminders = [
            item for item in self.history.context_items()
            if isinstance(item, UserMessage)
            and "session-memory-reminder" in item.content
        ]
        self.assertEqual(len(reminders), 0)


class RecallIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.db"
        self.store = SqliteEventStore(self.path)
        self.runtime = ThreadRuntime(self.store)
        self.thread = self.runtime.create_thread("repo")

    def tearDown(self):
        self.tmp.cleanup()

    def _turn(self, text: str, fail: bool = False):
        thread = self.runtime.get_thread(self.thread.thread_id)
        queued = self.runtime.create_turn(
            self.thread.thread_id, text,
            expected_thread_version=thread.version,
        )
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        if fail:
            self.runtime.fail_turn(
                running.turn_id, "d2:max_model_rounds_exceeded",
                expected_version=running.version, run_id=running.current_run_id,
            )
        else:
            self.runtime.complete_turn(
                running.turn_id, "ok",
                expected_version=running.version, run_id=running.current_run_id,
            )

    def test_recall_ranks_recent_and_rare(self):
        self._turn("fix the parser crash")
        self._turn("fix the parser crash")
        self._turn("refactor the UI layout")
        from koawa_agent_v2.runtime.session import SessionMemory
        memory = SessionMemory(self.store, self.runtime)
        hits = memory.recall(self.thread.thread_id, "parser")
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].user_input, "fix the parser crash")
        # newest crash turn ranks first via recency
        hits = memory.recall(self.thread.thread_id, "layout")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].user_input, "refactor the UI layout")


if __name__ == "__main__":
    unittest.main()
