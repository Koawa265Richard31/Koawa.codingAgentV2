"""D1 的重启/恢复演示：暂时不接模型和工具，只验证持久化控制面。"""

from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path

from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime


def main() -> None:
    """模拟旧 Worker 等待输入后退出，再由全新的 Runtime 恢复并完成任务。"""

    with TemporaryDirectory() as directory:
        # SQLite 文件代表跨进程保留的唯一事实来源；Runtime 本身可以被销毁。
        database = Path(directory) / "koawa-v2.sqlite3"
        runtime = ThreadRuntime(SqliteEventStore(database), actor="worker-1")
        thread = runtime.create_thread("D:/example-repository")
        turn = runtime.create_turn(
            thread.thread_id,
            "repair the failing tests",
            expected_thread_version=thread.version,
        )
        running = runtime.start_turn(turn.turn_id, turn.version)
        waiting = runtime.wait_for_input(
            turn.turn_id,
            "Which database should the patch target?",
            expected_version=running.version,
            run_id=running.current_run_id,
        )
        print(
            f"worker-1 stopped: status={waiting.status.value}, "
            f"attempt={running.attempt}, version={waiting.version}"
        )

        # 模拟新进程：不复用旧 Runtime，只用同一个数据库和 turn_id 重建状态。
        restarted = ThreadRuntime(SqliteEventStore(database), actor="worker-2")
        recovered = restarted.get_turn(turn.turn_id)
        queued = restarted.request_resume(
            recovered.turn_id,
            recovered.version,
            interrupt_id=recovered.pending_interrupt.interrupt_id,
            response="PostgreSQL",
        )
        second_run = restarted.start_turn(queued.turn_id, queued.version)
        completed = restarted.complete_turn(
            second_run.turn_id,
            "tests pass",
            expected_version=second_run.version,
            run_id=second_run.current_run_id,
        )
        print(
            f"worker-2 finished: status={completed.status.value}, "
            f"attempt={completed.attempt}, response={completed.last_resume_response!r}"
        )


if __name__ == "__main__":
    main()
