from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from koawa_agent_v2.execution.loop import AgentLoop, ToolExecutionResult
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.tools.errors import ToolConfigurationError
from koawa_agent_v2.tools.registry import ToolRegistry
from koawa_agent_v2.tools.schema import ToolSpec
from koawa_agent_v2.execution.worker import TurnWorker


@dataclass(frozen=True, slots=True)
class EchoArguments:
    text: str


def _schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    }


def _spec(name: str = "echo") -> ToolSpec[EchoArguments]:
    return ToolSpec(name, "回显一段文本", EchoArguments, _schema())


def _handler(arguments: EchoArguments, *, context) -> ToolExecutionResult:
    del context
    return ToolExecutionResult(arguments.text)


class NeverCalledClient:
    """配置预检失败时，Provider 必须保持零调用。"""

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, request):  # pragma: no cover - a call is itself a failure
        self.calls += 1
        raise AssertionError(f"unexpected model request: {request!r}")


class D3ConfigurationSafetyTest(unittest.TestCase):
    """配置错误必须在 durable Turn 启动前失败。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = ThreadRuntime(
            SqliteEventStore(Path(temporary.name) / "d3-configuration.sqlite3"),
            actor="d3-configuration-test",
        )
        thread = self.runtime.create_thread("D:/work/repository")
        self.turn = self.runtime.create_turn(
            thread.thread_id,
            "验证 D3 工具配置",
            expected_thread_version=thread.version,
        )
        self.client = NeverCalledClient()

    def assert_turn_was_not_started(self) -> None:
        unchanged = self.runtime.get_turn(self.turn.turn_id)
        self.assertEqual(TurnStatus.QUEUED, unchanged.status)
        self.assertEqual(self.turn.version, unchanged.version)
        self.assertEqual(0, self.client.calls)

    def test_invalid_schema_fails_before_worker_execution(self) -> None:
        """不支持的 schema keyword 在构造 ToolSpec 时即失败。"""
        schema = _schema()
        schema["properties"]["text"]["pattern"] = ".*"  # type: ignore[index]

        with self.assertRaises(ToolConfigurationError) as raised:
            ToolSpec("echo", "回显一段文本", EchoArguments, schema)

        self.assertEqual("unsupported_schema_keyword", raised.exception.code)
        self.assert_turn_was_not_started()

    def test_duplicate_registration_fails_before_worker_execution(self) -> None:
        """重名注册在 Registry 配置阶段失败，不能启动 durable Run。"""
        registry = ToolRegistry()
        registry.register(_spec(), _handler)

        with self.assertRaises(ToolConfigurationError) as raised:
            registry.register(_spec(), _handler)

        self.assertEqual("duplicate_tool_name", raised.exception.code)
        self.assert_turn_was_not_started()

    def test_agent_loop_seals_registry_before_worker_execution(self) -> None:
        """Loop 取得定义快照后，Worker 执行前也不能再加入新工具。"""
        registry = ToolRegistry()
        registry.register(_spec(), _handler)
        loop = AgentLoop(self.client, tool_executor=registry)
        worker = TurnWorker(
            self.runtime,
            loop,
            provider="test-provider",
            model="test-model",
        )
        definitions_before = registry.definitions()

        with self.assertRaises(ToolConfigurationError) as raised:
            registry.register(_spec("late_tool"), _handler)

        self.assertIsInstance(worker, TurnWorker)
        self.assertEqual("tool_registry_sealed", raised.exception.code)
        self.assertIs(definitions_before, registry.definitions())
        self.assertEqual(["echo"], [item.name for item in definitions_before])
        self.assert_turn_was_not_started()


if __name__ == "__main__":
    unittest.main()
