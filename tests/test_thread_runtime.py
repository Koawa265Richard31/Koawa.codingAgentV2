from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.event_store import (
    IdempotencyConflict,
    StreamId,
    WrongExpectedVersion,
)
from koawa_agent_v2.control.models import (
    CorruptEventStream,
    InvalidTransition,
    ThreadStatus,
    TurnStatus,
    rebuild_turn,
)
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime


class AdvancingStore:
    """在提交后、Runtime 重建返回值前注入竞争命令，用于复现精细竞态。"""

    def __init__(self, delegate, after_commit):
        self._delegate = delegate
        self._after_commit = after_commit
        self._advanced = False

    def append_batch(self, writes, **kwargs):
        receipt = self._delegate.append_batch(writes, **kwargs)
        if not self._advanced:
            self._advanced = True
            self._after_commit()
        return receipt

    def read_idempotency(self, *args, **kwargs):
        return self._delegate.read_idempotency(*args, **kwargs)

    def read_stream(self, *args, **kwargs):
        return self._delegate.read_stream(*args, **kwargs)

    def read_all(self, *args, **kwargs):
        return self._delegate.read_all(*args, **kwargs)


class ThreadRuntimeTest(unittest.TestCase):
    """验证 Thread/Turn 状态机、恢复语义、命令幂等与旧 Worker 隔离。"""
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "runtime.sqlite3"
        self.store = SqliteEventStore(self.database_path)
        self.runtime = ThreadRuntime(self.store)

    def create_thread_and_turn(self):
        """创建处于 QUEUED 状态的标准测试任务。"""
        thread = self.runtime.create_thread("D:/work/repository")
        turn = self.runtime.create_turn(
            thread.thread_id,
            "repair the failing test",
            expected_thread_version=thread.version,
        )
        return thread, turn

    def start(self, turn):
        """使用调用者看到的精确版本启动一次新 Run。"""
        return self.runtime.start_turn(turn.turn_id, turn.version)

    def test_create_turn_is_atomic_and_active_turn_is_exclusive(self) -> None:
        """创建 Turn 同时挂接 Thread，且同一 Thread 只允许一个根 Turn。"""
        thread, turn = self.create_thread_and_turn()

        rebuilt_thread = self.runtime.get_thread(thread.thread_id)
        self.assertEqual(ThreadStatus.OPEN, rebuilt_thread.status)
        self.assertEqual(turn.turn_id, rebuilt_thread.active_turn_id)
        self.assertEqual(1, rebuilt_thread.version)
        self.assertEqual(TurnStatus.QUEUED, turn.status)
        self.assertEqual(0, turn.version)
        with self.assertRaises(InvalidTransition):
            self.runtime.create_turn(
                thread.thread_id,
                "a competing root task",
                expected_thread_version=rebuilt_thread.version,
            )

        all_events = self.store.read_all()
        self.assertEqual(3, len(all_events))
        self.assertEqual(
            {"thread.created.v1", "thread.turn-attached.v1", "turn.created.v1"},
            {event.event_type for event in all_events},
        )

    def test_wait_resume_and_new_run_attempt(self) -> None:
        """等待内容可恢复，且恢复后的执行必须产生新的 attempt/run_id。"""
        _, turn = self.create_thread_and_turn()
        first_run = self.start(turn)
        first_run_id = first_run.current_run_id
        interrupt_id = uuid4()
        waiting = self.runtime.wait_for_input(
            turn.turn_id,
            "Which database should I target?",
            expected_version=first_run.version,
            run_id=first_run_id,
            interrupt_id=interrupt_id,
        )
        self.assertEqual(TurnStatus.WAITING_FOR_INPUT, waiting.status)
        self.assertEqual(interrupt_id, waiting.pending_interrupt.interrupt_id)

        with self.assertRaises(WrongExpectedVersion):
            self.runtime.request_resume(
                turn.turn_id,
                waiting.version - 1,
                interrupt_id=interrupt_id,
                response="PostgreSQL",
            )
        with self.assertRaises(InvalidTransition):
            self.runtime.request_resume(
                turn.turn_id,
                waiting.version,
                interrupt_id=uuid4(),
                response="PostgreSQL",
            )
        self.assertEqual(waiting, self.runtime.get_turn(turn.turn_id))

        queued = self.runtime.request_resume(
            turn.turn_id,
            waiting.version,
            interrupt_id=interrupt_id,
            response="PostgreSQL",
        )
        self.assertEqual(TurnStatus.QUEUED, queued.status)
        self.assertIsNone(queued.current_run_id)
        self.assertEqual("PostgreSQL", queued.last_resume_response)
        second_run = self.runtime.start_turn(turn.turn_id, queued.version)
        self.assertEqual(2, second_run.attempt)
        self.assertIsNotNone(second_run.current_run_id)
        self.assertNotEqual(first_run_id, second_run.current_run_id)

    def test_old_worker_is_fenced_after_a_new_run_starts(self) -> None:
        """旧 Worker 无论使用旧版本还是猜到新版本，都不能完成新 Run。"""
        _, turn = self.create_thread_and_turn()
        run_a = self.start(turn)
        paused = self.runtime.pause_turn(
            turn.turn_id,
            run_a.version,
            "worker A disconnected",
            run_id=run_a.current_run_id,
        )
        queued = self.runtime.request_resume(turn.turn_id, paused.version)
        run_b = self.runtime.start_turn(turn.turn_id, queued.version)

        with self.assertRaises(WrongExpectedVersion):
            self.runtime.complete_turn(
                turn.turn_id,
                "stale A result",
                expected_version=run_a.version,
                run_id=run_a.current_run_id,
            )
        with self.assertRaises(InvalidTransition):
            self.runtime.complete_turn(
                turn.turn_id,
                "A guessed the latest version",
                expected_version=run_b.version,
                run_id=run_a.current_run_id,
            )

        unchanged = self.runtime.get_turn(turn.turn_id)
        self.assertEqual(TurnStatus.RUNNING, unchanged.status)
        self.assertEqual(run_b.current_run_id, unchanged.current_run_id)
        completed = self.runtime.complete_turn(
            turn.turn_id,
            "worker B result",
            expected_version=run_b.version,
            run_id=run_b.current_run_id,
        )
        self.assertEqual("worker B result", completed.outcome)

    def test_every_attempt_has_a_fresh_run_id(self) -> None:
        """重试旧 start 命令不等于新 attempt；真正的新 attempt 必须换 run_id。"""
        _, turn = self.create_thread_and_turn()
        shared_command = uuid4()
        run_a = self.runtime.start_turn(
            turn.turn_id,
            turn.version,
            command_id=shared_command,
        )
        paused = self.runtime.pause_turn(
            turn.turn_id,
            run_a.version,
            run_id=run_a.current_run_id,
        )
        queued = self.runtime.request_resume(turn.turn_id, paused.version)

        # Reusing the command identifier is a retry of attempt 1, not a way to
        # start attempt 2 with the same run fence.
        retried_a = self.runtime.start_turn(
            turn.turn_id,
            turn.version,
            command_id=shared_command,
        )
        self.assertEqual(run_a, retried_a)
        run_b = self.runtime.start_turn(turn.turn_id, queued.version)
        self.assertNotEqual(run_a.current_run_id, run_b.current_run_id)

    def test_runtime_commands_retry_idempotently_after_state_changes(self) -> None:
        """公开 Runtime 命令在状态继续推进后仍返回自己首次执行的结果。"""
        create_thread_command = uuid4()
        first_thread = self.runtime.create_thread(
            "D:/work/repository",
            command_id=create_thread_command,
        )
        retried_thread = self.runtime.create_thread(
            "D:/work/repository",
            command_id=create_thread_command,
        )
        self.assertEqual(first_thread, retried_thread)
        with self.assertRaises(IdempotencyConflict):
            self.runtime.create_thread(
                "D:/different-repository",
                command_id=create_thread_command,
            )

        create_turn_command = uuid4()
        first_turn = self.runtime.create_turn(
            first_thread.thread_id,
            "repair tests",
            expected_thread_version=first_thread.version,
            command_id=create_turn_command,
        )
        retried_turn = self.runtime.create_turn(
            first_thread.thread_id,
            "repair tests",
            expected_thread_version=first_thread.version,
            command_id=create_turn_command,
        )
        self.assertEqual(first_turn, retried_turn)
        # A retry returns the original command result, not today's newer state.
        self.assertEqual(
            first_thread,
            self.runtime.create_thread(
                "D:/work/repository",
                command_id=create_thread_command,
            ),
        )
        replacement_process = ThreadRuntime(
            SqliteEventStore(self.database_path),
            actor="replacement-worker",
        )
        self.assertEqual(
            first_thread,
            replacement_process.create_thread(
                "D:/work/repository",
                command_id=create_thread_command,
            ),
        )

        start_command = uuid4()
        run = self.runtime.start_turn(
            first_turn.turn_id,
            first_turn.version,
            command_id=start_command,
        )
        self.assertEqual(
            run,
            self.runtime.start_turn(
                first_turn.turn_id,
                first_turn.version,
                command_id=start_command,
            ),
        )

        complete_command = uuid4()
        completed = self.runtime.complete_turn(
            run.turn_id,
            "all tests pass",
            expected_version=run.version,
            run_id=run.current_run_id,
            command_id=complete_command,
        )
        self.assertEqual(
            completed,
            self.runtime.complete_turn(
                run.turn_id,
                "all tests pass",
                expected_version=run.version,
                run_id=run.current_run_id,
                command_id=complete_command,
            ),
        )
        self.assertEqual(
            run,
            self.runtime.start_turn(
                first_turn.turn_id,
                first_turn.version,
                command_id=start_command,
            ),
        )
        self.assertEqual(6, len(self.store.read_all()))

    def test_first_call_returns_its_receipt_version_not_a_competing_future(self) -> None:
        """提交后发生并发推进时，首次调用也只能返回本命令对应版本。"""
        command_id = uuid4()
        # 使用隔离数据库，使竞争命令能引用确定性 thread_id，又不污染其他用例。
        other_path = self.database_path.with_name("receipt-race.sqlite3")
        base_store = SqliteEventStore(other_path)
        competing_runtime = ThreadRuntime(base_store, actor="competitor")

        # 先在探针库验证 command_id 派生的确定性 ID，再用 Hook 强制制造竞态。
        probe_path = self.database_path.with_name("probe.sqlite3")
        probe = ThreadRuntime(SqliteEventStore(probe_path))
        target_id = probe.create_thread(
            "D:/race",
            command_id=command_id,
        ).thread_id

        def advance() -> None:
            created = competing_runtime.get_thread(target_id)
            competing_runtime.create_turn(
                target_id,
                "competing command",
                expected_thread_version=created.version,
            )

        hooked = ThreadRuntime(AdvancingStore(base_store, advance))
        returned = hooked.create_thread("D:/race", command_id=command_id)
        latest = competing_runtime.get_thread(target_id)
        self.assertEqual(0, returned.version)
        self.assertIsNone(returned.active_turn_id)
        self.assertEqual(1, latest.version)
        self.assertIsNotNone(latest.active_turn_id)

    def test_process_restart_reconstructs_completion_and_detachment(self) -> None:
        """新进程仅依赖事件即可恢复完成状态和 Thread/Turn 关系。"""
        thread, turn = self.create_thread_and_turn()
        running = self.start(turn)
        completed = self.runtime.complete_turn(
            turn.turn_id,
            "all tests pass",
            expected_version=running.version,
            run_id=running.current_run_id,
        )

        restarted = ThreadRuntime(SqliteEventStore(self.database_path))
        rebuilt_turn = restarted.get_turn(turn.turn_id)
        rebuilt_thread = restarted.get_thread(thread.thread_id)
        self.assertEqual(completed, rebuilt_turn)
        self.assertEqual(TurnStatus.COMPLETED, rebuilt_turn.status)
        self.assertEqual("all tests pass", rebuilt_turn.outcome)
        self.assertIsNone(rebuilt_thread.active_turn_id)
        self.assertEqual(2, rebuilt_thread.version)

        with self.assertRaises(InvalidTransition):
            restarted.request_resume(turn.turn_id, rebuilt_turn.version)
        archived = restarted.archive_thread(
            thread.thread_id,
            expected_version=rebuilt_thread.version,
        )
        self.assertEqual(ThreadStatus.ARCHIVED, archived.status)
        with self.assertRaises(InvalidTransition):
            restarted.create_turn(
                thread.thread_id,
                "cannot run after archive",
                expected_thread_version=archived.version,
            )

    def test_archive_is_blocked_until_active_turn_terminates(self) -> None:
        """存在活动 Turn 时禁止归档；Turn 终结并解绑后才允许归档。"""
        thread, turn = self.create_thread_and_turn()
        attached_thread = self.runtime.get_thread(thread.thread_id)
        with self.assertRaises(InvalidTransition):
            self.runtime.archive_thread(
                thread.thread_id,
                expected_version=attached_thread.version,
            )

        running = self.start(turn)
        failed = self.runtime.fail_turn(
            turn.turn_id,
            "model provider unavailable",
            expected_version=running.version,
            run_id=running.current_run_id,
        )
        self.assertEqual(TurnStatus.FAILED, failed.status)
        detached = self.runtime.get_thread(thread.thread_id)
        self.assertIsNone(detached.active_turn_id)
        archived = self.runtime.archive_thread(
            thread.thread_id,
            expected_version=detached.version,
        )
        self.assertEqual(ThreadStatus.ARCHIVED, archived.status)

    def test_pause_queues_for_a_new_worker_without_interrupt(self) -> None:
        """运维暂停不需要 interrupt 响应，恢复后由新 Worker 创建新 Run。"""
        _, turn = self.create_thread_and_turn()
        running = self.start(turn)
        paused = self.runtime.pause_turn(
            turn.turn_id,
            running.version,
            "worker shutdown",
            run_id=running.current_run_id,
        )
        self.assertEqual(TurnStatus.PAUSED, paused.status)
        self.assertIsNone(paused.pending_interrupt)

        queued = self.runtime.request_resume(turn.turn_id, paused.version)
        self.assertEqual(TurnStatus.QUEUED, queued.status)
        restarted = self.runtime.start_turn(turn.turn_id, queued.version)
        self.assertEqual(running.attempt + 1, restarted.attempt)
        self.assertNotEqual(running.current_run_id, restarted.current_run_id)

    def test_approval_decision_is_durable_and_wrong_type_is_rejected(self) -> None:
        """审批恢复只接受布尔值，且选择可在进程重启后重放。"""
        _, turn = self.create_thread_and_turn()
        running = self.start(turn)
        interrupt_id = uuid4()
        waiting = self.runtime.wait_for_approval(
            turn.turn_id,
            "Allow the test command?",
            expected_version=running.version,
            run_id=running.current_run_id,
            interrupt_id=interrupt_id,
        )

        with self.assertRaises(InvalidTransition):
            self.runtime.request_resume(
                turn.turn_id,
                waiting.version,
                interrupt_id=interrupt_id,
                response="yes",
            )
        queued = self.runtime.request_resume(
            turn.turn_id,
            waiting.version,
            interrupt_id=interrupt_id,
            response=False,
        )
        self.assertIs(False, queued.last_resume_response)
        restarted = ThreadRuntime(SqliteEventStore(self.database_path))
        self.assertIs(False, restarted.get_turn(turn.turn_id).last_resume_response)

    def test_timeout_is_terminal_and_detaches_the_thread(self) -> None:
        """超时是终态，并与 Thread 解绑原子提交。"""
        thread, turn = self.create_thread_and_turn()
        timed_out = self.runtime.timeout_turn(
            turn.turn_id,
            "queue deadline exceeded",
            expected_version=turn.version,
        )
        self.assertEqual(TurnStatus.TIMED_OUT, timed_out.status)
        self.assertIsNone(self.runtime.get_thread(thread.thread_id).active_turn_id)
        with self.assertRaises(InvalidTransition):
            self.runtime.timeout_turn(
                turn.turn_id,
                "again",
                expected_version=timed_out.version,
            )

    def test_completion_commit_updates_turn_and_thread_as_one_group(self) -> None:
        """Turn 完成和 Thread 解绑属于同一个可识别的原子 commit。"""
        _, turn = self.create_thread_and_turn()
        running = self.start(turn)
        before = self.store.read_all()
        completed = self.runtime.complete_turn(
            turn.turn_id,
            "done",
            expected_version=running.version,
            run_id=running.current_run_id,
        )
        after = self.store.read_all(after_position=before[-1].global_position)

        self.assertEqual(TurnStatus.COMPLETED, completed.status)
        self.assertEqual(2, len(after))
        self.assertEqual(
            {"turn.completed.v1", "thread.turn-detached.v1"},
            {event.event_type for event in after},
        )
        self.assertEqual({after[0].commit_id}, {event.commit_id for event in after})
        self.assertEqual([0, 1], [event.commit_index for event in after])
        self.assertEqual([2, 2], [event.commit_size for event in after])

    def test_replay_fails_closed_on_unknown_schema_version(self) -> None:
        """未知事件版本必须 fail closed，不能按旧结构侥幸解析。"""
        _, turn = self.create_thread_and_turn()
        event = self.store.read_stream(StreamId("turn", turn.turn_id))[0]
        with self.assertRaises(CorruptEventStream):
            rebuild_turn(turn.turn_id, (replace(event, schema_version=2),))


class CanonicalTextRuntimeTest(unittest.TestCase):
    """I4 6.3/6.4: ThreadRuntime canonicalizes free text before fingerprint/event."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "canonical-runtime.sqlite3"
        self.store = SqliteEventStore(self.database_path)
        self.runtime = ThreadRuntime(self.store)

    def create_thread_and_turn(self, text: str):
        thread = self.runtime.create_thread("D:/work/repository")
        turn = self.runtime.create_turn(
            thread.thread_id,
            text,
            expected_thread_version=thread.version,
        )
        return thread, turn

    def test_user_input_is_canonical_before_persist_and_idempotent(self) -> None:
        """Credential/user text is canonical in the event, never the raw form."""
        from koawa_agent_v2.control.durable_json import canonicalize_text

        raw = "please commit now\r\nsk-abc1234567890xyz"
        expected = canonicalize_text(
            raw, 65_536, name="user_input"
        ).value
        _, turn = self.create_thread_and_turn(raw)
        self.assertEqual(expected, turn.user_input)
        event = self.store.read_stream(StreamId("turn", turn.turn_id))[0]
        self.assertEqual(expected, event.payload["user_input"])
        self.assertNotIn("sk-abc1234567890xyz", turn.user_input)

    def test_canonicalization_is_deterministic_across_retry(self) -> None:
        """Same command + same raw input returns the canonical receipt result."""
        from koawa_agent_v2.control.durable_json import canonicalize_text

        raw = "run tests now sk-abc1234567890xyz"
        thread = self.runtime.create_thread("repo")
        command_id = uuid4()
        turn_id = uuid4()
        first = self.runtime.create_turn(
            thread.thread_id,
            raw,
            expected_thread_version=thread.version,
            turn_id=turn_id,
            command_id=command_id,
        )
        second = self.runtime.create_turn(
            thread.thread_id,
            raw,
            expected_thread_version=thread.version,
            turn_id=turn_id,
            command_id=command_id,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            canonicalize_text(raw, 65_536, name="user_input").value,
            first.user_input,
        )

    def test_crlf_nfc_multibyte_and_no_text_trimming(self) -> None:
        """CRLF->LF, NFC, multibyte counting; surrounding whitespace preserved."""
        from koawa_agent_v2.control.durable_json import canonicalize_text

        raw = "  cafe\u0301 fix\r\n\u4e2d\u6587 ok  "
        canon = canonicalize_text(raw, 65_536, name="user_input")
        self.assertIn("caf\u00e9", canon.value)
        self.assertIn("fix\n", canon.value)
        self.assertTrue(canon.value.startswith("  "))
        self.assertTrue(canon.value.endswith("  "))
        _, turn = self.create_thread_and_turn(raw)
        self.assertEqual(canon.value, turn.user_input)

    def test_wait_resume_pause_complete_fail_reasons_are_canonical(self) -> None:
        """Every free-text entry point canonicalizes before persist (idempotent)."""
        from koawa_agent_v2.control.durable_json import canonicalize_text

        _, turn = self.create_thread_and_turn("do the task")
        running = self.runtime.start_turn(turn.turn_id, turn.version)
        interrupt_id = uuid4()
        waiting = self.runtime.wait_for_input(
            turn.turn_id,
            "please paste sk-abc1234567890xyz here",
            expected_version=running.version,
            run_id=running.current_run_id,
            interrupt_id=interrupt_id,
        )
        self.assertNotIn("sk-abc1234567890xyz", waiting.pending_interrupt.prompt)
        self.assertIn("[REDACTED]", waiting.pending_interrupt.prompt)
        queued = self.runtime.request_resume(
            turn.turn_id,
            expected_version=waiting.version,
            interrupt_id=interrupt_id,
            response="resume sk-abc1234567890xyz now",
        )
        self.assertNotIn("sk-abc1234567890xyz", queued.last_resume_response)
        running = self.runtime.start_turn(turn.turn_id, queued.version)
        completed = self.runtime.complete_turn(
            turn.turn_id,
            "done sk-abc1234567890xyz summary",
            expected_version=running.version,
            run_id=running.current_run_id,
        )
        self.assertNotIn("sk-abc1234567890xyz", completed.outcome)
        self.assertIn("[REDACTED]", completed.outcome)
        # terminal error path and operator reasons use the same policy.
        _, failed_turn = self.create_thread_and_turn("task two")
        running = self.runtime.start_turn(failed_turn.turn_id, failed_turn.version)
        failed = self.runtime.fail_turn(
            failed_turn.turn_id,
            "failed with sk-abc1234567890xyz",
            expected_version=running.version,
            run_id=running.current_run_id,
        )
        self.assertIn("[REDACTED]", failed.error)
        _, paused_turn = self.create_thread_and_turn("task three")
        running = self.runtime.start_turn(paused_turn.turn_id, paused_turn.version)
        paused = self.runtime.pause_turn(
            paused_turn.turn_id,
            expected_version=running.version,
            reason="pause sk-abc1234567890xyz",
            run_id=running.current_run_id,
        )
        pause_event = self.store.read_stream(StreamId("turn", paused_turn.turn_id))[-1]

    def test_user_input_over_canonical_limit_is_rejected_content_free(self) -> None:
        """A too-large user input never becomes an event."""
        from koawa_agent_v2.control.durable_json import (
            CanonicalTextError,
            CanonicalTextPolicy,
        )

        runtime = ThreadRuntime(
            self.store,
            text_policy=CanonicalTextPolicy(user_input_max_utf8_bytes=1_024),
        )
        thread = runtime.create_thread("repo")
        with self.assertRaises(CanonicalTextError):
            runtime.create_turn(
                thread.thread_id,
                "x" * 2_048,
                expected_thread_version=thread.version,
            )
        self.assertNotIn("turn.created.v1", {e.event_type for e in self.store.read_all()})

    def test_idempotency_sqlite_canary_contains_no_original_user_text(self) -> None:
        """The SQLite file holds canonical/redacted text, never the raw canary."""
        canary = "sk-abc1234567890xyz"
        thread = self.runtime.create_thread("repo")
        queued = self.runtime.create_turn(
            thread.thread_id,
            "handle " + canary + " please",
            expected_thread_version=thread.version,
        )
        running = self.runtime.start_turn(queued.turn_id, queued.version)
        self.runtime.wait_for_input(
            queued.turn_id,
            "wait: " + canary,
            expected_version=running.version,
            run_id=running.current_run_id,
        )
        payload = self.database_path.read_bytes()
        self.assertNotIn(canary.encode("utf-8"), payload)
        self.assertIn(b"[REDACTED]", payload)

if __name__ == "__main__":
    unittest.main()
