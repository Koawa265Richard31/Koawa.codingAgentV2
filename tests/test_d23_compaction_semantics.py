"""D13-D23-001 regression: compaction replacement must preserve bounded
result semantics - a compacted failure stays distinguishable from a success.
"""
from __future__ import annotations

import unittest
from uuid import uuid4

from koawa_agent_v2.execution.loop import AgentLoop, AgentLoopError
from koawa_agent_v2.model.protocol import (
    ModelCallRef,
    ToolCallEcho,
    ToolResultMessage,
    UserMessage,
)
from koawa_agent_v2.runtime.memory import MemoryConfig
from tests.test_d23_loop_compaction import (
    RecordingCompactionSink,
    _tool_turn,
)


def _memory() -> MemoryConfig:
    return MemoryConfig.from_mapping(
        {
            "request_context_soft_chars": 500,
            "request_context_hard_chars": 2000,
            "request_context_reserve_chars": 100,
            "compaction_target_chars": 400,
            "conclusion_max_chars": 1000,
            "compaction_summary_max_chars": 1000,
            "in_run_keep_groups": 1,
        }
    )


def _loop_with_sink(sink: RecordingCompactionSink) -> AgentLoop:
    from tests.test_agent_loop import ScriptedClient

    return AgentLoop(ScriptedClient(), memory=_memory(), compaction_sink=sink)


def _append_group(context: list, call_id: str, content: str, is_error: bool) -> None:
    turn = _tool_turn(call_id)
    echo = ToolCallEcho(
        "test", ModelCallRef(turn.model_turn_id, call_id), turn.output_items[1]
    )
    context.append(echo)
    context.append(ToolResultMessage(echo.call_ref, content, is_error))
    context.append(UserMessage(f"n{call_id}", "y" * 200))


def _context(result_content: str, *, is_error: bool) -> list:
    context = [UserMessage(f"u{uuid4()}", "x" * 800)]
    for call_id in ("c1", "c2"):
        _append_group(context, call_id, result_content, is_error)
    return context


class CompactionSemanticsTest(unittest.TestCase):
    def _replacement_content(self, result_content: str, is_error: bool) -> str:
        sink = RecordingCompactionSink()
        sink._source_versions = list(range(64))
        loop = _loop_with_sink(sink)
        context = _context(result_content, is_error=is_error)
        loop._maybe_compact(context)
        assert sink.calls, "compaction did not fire"
        return sink.calls[0]["replacement"].content

    def test_failed_result_is_distinguishable_from_success(self) -> None:
        failed = self._replacement_content(
            "exit_code=1; LOGIN_EXPECTED_401_GOT_200", is_error=True
        )
        success = self._replacement_content(
            '{"exit_code": 0, "passed": true}', is_error=False
        )
        self.assertIn("[result error]", failed)
        self.assertIn("LOGIN_EXPECTED_401_GOT_200", failed)
        self.assertIn("[result ok]", success)
        self.assertNotEqual(failed, success)

    def test_semantic_lines_are_bounded(self) -> None:
        sink = RecordingCompactionSink()
        sink._source_versions = list(range(64))
        loop = _loop_with_sink(sink)
        context = [UserMessage(f"u{uuid4()}", "x" * 800)]
        for index in range(5):
            _append_group(context, f"c{index}", "boom", True)
        loop._maybe_compact(context)
        assert sink.calls
        self.assertLessEqual(
            sink.calls[0]["replacement"].content.count("[result "), 8
        )


if __name__ == "__main__":
    unittest.main()
