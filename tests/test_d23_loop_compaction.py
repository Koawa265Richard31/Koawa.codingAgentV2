"""D23-E tests: AgentLoop safe-point compaction trigger, budget, fail-closed.

Covers D23 §5.3 (safe trigger: no pending calls, durable sink, not cancelled),
§5.4 (soft trigger -> compact to target; hard overrun -> context_capacity_
exhausted, never an oversized request) and §5.6 (deterministic fallback keeps
the loop alive when compaction succeeded).
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from koawa_agent_v2.execution.loop import AgentLoop, AgentLoopError, AgentLoopLimits
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ModelCallRef,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    ToolCallEcho,
    ToolCallItem,
    ToolDefinition,
    ToolResultMessage,
    TurnCompleted,
    UserMessage,
)
from koawa_agent_v2.runtime.memory import MemoryConfig
from tests.test_agent_loop import RecordingToolExecutor, ScriptedClient


def _tool_turn(call_id: str = "c1"):
    items = (
        AssistantTextItem(0, f"i{uuid4()}", "work"),
        ToolCallItem(1, f"c{uuid4()}", call_id, "read_file", "{}"),
    )
    return ModelTurn(
        uuid4(), "test", "model", f"r{uuid4()}", items,
        FinishReason.TOOL_CALLS, None,
    )


def _final_turn():
    return ModelTurn(
        uuid4(), "test", "model", f"r{uuid4()}",
        (AssistantTextItem(0, f"i{uuid4()}", "done"),),
        FinishReason.STOP, None,
    )


class RecordingCompactionSink:
    """Records compact() calls like the durable recorder would."""

    def __init__(self):
        self.calls = []
        self.tool_count = 0
        self.pending_calls = ()
        self.context = []
        self._source_versions = [0]

    def source_versions_for(self, first: int, last: int):
        if last >= len(self._source_versions):
            from koawa_agent_v2.execution.loop import AgentLoopError
            raise AgentLoopError("compaction_source_versions_unavailable")
        return self._source_versions[first], self._source_versions[last]

    def synced_context(self):
        if not self.calls:
            return list(self.context)
        last = self.calls[-1]
        return [last["replacement"]]

    def compact(self, **kwargs):
        self.calls.append(kwargs)
        return (0, 0)


class CompactionTriggerTest(unittest.TestCase):
    def setUp(self):
        self.sink = RecordingCompactionSink()
        self.loop = AgentLoop(
            ScriptedClient(),
            memory=MemoryConfig.from_mapping({
                "request_context_soft_chars": 500,
                "request_context_hard_chars": 2000,
                "request_context_reserve_chars": 100,
                "compaction_target_chars": 400,
                "conclusion_max_chars": 1000,
                "compaction_summary_max_chars": 1000,
                "in_run_keep_groups": 1,
            }),
            compaction_sink=self.sink,
            limits=AgentLoopLimits(max_model_rounds=10, max_tool_calls=20),
        )

    def _big_user(self):
        return UserMessage(f"u{uuid4()}", "x" * 800)

    def test_soft_budget_without_closed_groups_fails_closed(self):
        # Above soft budget, nothing compressible, but under hard: compaction
        # simply does nothing and the request proceeds (deterministic fallback).
        context = [self._big_user()]
        self.loop._maybe_compact(context)
        self.assertEqual(len(self.sink.calls), 0)

    def test_above_hard_without_closed_groups_fails_closed(self):
        # Over hard with nothing compressible: never send the oversized
        # request; fail closed instead.
        loop = AgentLoop(
            ScriptedClient(),
            memory=MemoryConfig.from_mapping({
                "request_context_soft_chars": 100,
                "request_context_hard_chars": 500,
                "request_context_reserve_chars": 50,
                "compaction_target_chars": 60,
                "conclusion_max_chars": 400,
                "compaction_summary_max_chars": 400,
                "in_run_keep_groups": 1,
            }),
            compaction_sink=self.sink,
        )
        context = [UserMessage("u1", "x" * 800)]
        with self.assertRaises(AgentLoopError) as raised:
            loop._maybe_compact(context)
        self.assertEqual(raised.exception.code, "context_capacity_exhausted")
        self.assertEqual(len(self.sink.calls), 0)

    def test_no_pending_calls_and_closed_groups_compact(self):
        context = [self._big_user()]
        self.sink._source_versions = [0]
        # Two complete closed groups so keep_recent=1 leaves one compressible.
        for index, call_id in enumerate(("c1", "c2"), start=1):
            turn = _tool_turn(call_id)
            echo = ToolCallEcho(
                "test", ModelCallRef(turn.model_turn_id, call_id),
                turn.output_items[1],
            )
            context.append(echo)
            context.append(ToolResultMessage(echo.call_ref, "ok", False))
            # echo at versions 1/3, result at 2/4 (phase-advance occupies gaps)
            self.sink._source_versions.extend([index * 2 - 1, index * 2])
        self.loop._maybe_compact(context)
        self.assertEqual(len(self.sink.calls), 1)
        # The oldest group (indices 1..2) maps to stream versions 1..2.
        self.assertEqual(self.sink.calls[0]["source_first_version"], 1)
        self.assertEqual(self.sink.calls[0]["source_last_version"], 2)
        # The group was replaced by a single user projection.
        kinds = [type(item).__name__ for item in context]
        self.assertIn("UserMessage", kinds)
        self.assertLessEqual(len(context), 3 + 1)

    def test_open_pending_call_never_compresses(self):
        self.sink.pending_calls = ({"kind": "tool_call"},)
        context = [UserMessage("u1", "x" * 800)]
        turn = _tool_turn("c1")
        echo = ToolCallEcho(
            "test", ModelCallRef(turn.model_turn_id, "c1"), turn.output_items[1],
        )
        context.append(echo)  # no result -> open group
        # Over hard, open group present, nothing compressible -> fail closed.
        loop = AgentLoop(
            ScriptedClient(),
            memory=MemoryConfig.from_mapping({
                "request_context_soft_chars": 100,
                "request_context_hard_chars": 500,
                "request_context_reserve_chars": 50,
                "compaction_target_chars": 60,
                "conclusion_max_chars": 400,
                "compaction_summary_max_chars": 400,
                "in_run_keep_groups": 1,
            }),
            compaction_sink=self.sink,
        )
        with self.assertRaises(AgentLoopError) as raised:
            loop._maybe_compact(context)
        self.assertEqual(raised.exception.code, "context_capacity_exhausted")
        self.assertEqual(len(self.sink.calls), 0)

    def test_no_compaction_when_below_soft_budget(self):
        context = [UserMessage("u1", "small")]
        self.loop._maybe_compact(context)
        self.assertEqual(len(self.sink.calls), 0)

    def test_compaction_disabled_keeps_old_behavior(self):
        loop = AgentLoop(
            ScriptedClient(),
            memory=MemoryConfig(in_run_compaction_enabled=False),
            compaction_sink=self.sink,
        )
        context = [self._big_user()]
        loop._maybe_compact(context)
        self.assertEqual(len(self.sink.calls), 0)


if __name__ == "__main__":
    unittest.main()
