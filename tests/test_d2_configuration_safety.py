from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.execution.loop import AgentLoop
from koawa_agent_v2.model.protocol import ToolDefinition
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.worker import TurnWorker


class NeverCalledClient:
    """静态配置失败时，Provider 不应得到调用机会。"""

    def stream(self, request):  # pragma: no cover - a call is itself a failure
        raise AssertionError(f"unexpected request: {request!r}")


class NeverCalledExecutor:
    """只暴露配置快照；重复定义必须阻止执行器进入调用阶段。"""

    def __init__(self, definitions: tuple[ToolDefinition, ...]) -> None:
        self._definitions = tuple(definitions)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    def execute(self, call, *, context):  # pragma: no cover - a call is itself a failure
        raise AssertionError(f"unexpected tool call: {call!r}, {context!r}")


class D2ConfigurationSafetyTest(unittest.TestCase):
    def test_duplicate_tool_names_are_rejected_before_turn_start(self) -> None:
        """可预检的工具配置错误不能把 durable Turn 留在 RUNNING。"""
        with tempfile.TemporaryDirectory() as directory:
            runtime = ThreadRuntime(
                SqliteEventStore(Path(directory) / "d2-config.sqlite3"),
                actor="d2-config-test",
            )
            thread = runtime.create_thread("D:/work/repository")
            turn = runtime.create_turn(
                thread.thread_id,
                "检查配置失败是否污染状态",
                expected_thread_version=thread.version,
            )
            duplicate_a = ToolDefinition("read_file", "读取文件", "{}")
            duplicate_b = ToolDefinition("read_file", "另一个定义", "{}")

            with self.assertRaisesRegex(ValueError, "duplicate names"):
                TurnWorker(
                    runtime,
                    AgentLoop(
                        NeverCalledClient(),
                        tool_executor=NeverCalledExecutor((duplicate_a, duplicate_b)),
                    ),
                    provider="test-provider",
                    model="test-model",
                )

            unchanged = runtime.get_turn(turn.turn_id)
            self.assertEqual(TurnStatus.QUEUED, unchanged.status)
            self.assertEqual(turn.version, unchanged.version)


if __name__ == "__main__":
    unittest.main()
