"""Hardening 2026-09-19 (E1/E2) regressions: full-request capacity gate.

E1: trusted instructions (InstructionMessage) are metered with the request.
E2: the gate runs even when no compaction sink is bound - a sink-less path
    can no longer silently bypass the budget; over-hard fails closed.
Definitions: pinned tool-definition schemas count against the gate.
"""
from __future__ import annotations

import json
import unittest
from uuid import uuid4

from koawa_agent_v2.execution.loop import AgentLoop, AgentLoopError
from koawa_agent_v2.model.protocol import (
    InstructionMessage,
    InstructionRole,
    ToolDefinition,
    UserMessage,
)
from koawa_agent_v2.runtime.memory import MemoryConfig
from tests.test_agent_loop import RecordingToolExecutor, ScriptedClient


def _memory(soft: int, hard: int, reserve: int = 40, target: int = None) -> MemoryConfig:
    target = soft - 50 if target is None else target
    bounded = min(800, hard - 50)
    return MemoryConfig.from_mapping(
        {
            "request_context_soft_chars": soft,
            "request_context_hard_chars": hard,
            "request_context_reserve_chars": reserve,
            "compaction_target_chars": target,
            "conclusion_max_chars": bounded,
            "compaction_summary_max_chars": bounded,
            "in_run_keep_groups": 1,
        }
    )


def _definitions_executor() -> RecordingToolExecutor:
    schema = json.dumps({"type": "object", "pad": "y" * 900})
    definition = ToolDefinition("read_file", "Read a file.", schema)
    return RecordingToolExecutor(definitions=(definition,))


class FullRequestMeteringTest(unittest.TestCase):
    def test_instructions_are_metered_without_sink(self) -> None:
        loop = AgentLoop(ScriptedClient(), memory=_memory(soft=200, hard=400))
        context = [InstructionMessage(InstructionRole.SYSTEM, "S" * 500)]
        with self.assertRaises(AgentLoopError) as raised:
            loop._maybe_compact(context)
        self.assertEqual("context_capacity_exhausted", raised.exception.code)

    def test_definitions_are_metered_without_sink(self) -> None:
        loop = AgentLoop(
            ScriptedClient(),
            tool_executor=_definitions_executor(),
            memory=_memory(soft=200, hard=400),
        )
        context = [UserMessage(f"u{uuid4()}", "x" * 300)]
        with self.assertRaises(AgentLoopError) as raised:
            loop._maybe_compact(context)
        self.assertEqual("context_capacity_exhausted", raised.exception.code)

    def test_small_full_request_passes_without_sink(self) -> None:
        loop = AgentLoop(ScriptedClient(), memory=_memory(soft=200, hard=400))
        context = [InstructionMessage(InstructionRole.SYSTEM, "S" * 30)]
        loop._maybe_compact(context)  # under soft: no raise, no compaction

    def test_definitions_and_context_sum_over_hard_fails_closed(self) -> None:
        loop = AgentLoop(
            ScriptedClient(),
            tool_executor=_definitions_executor(),
            memory=_memory(soft=200, hard=700),
        )
        # context alone (~350) is under hard; the definitions (~950) push the
        # FULL request over - the gate must see the sum.
        context = [UserMessage(f"u{uuid4()}", "x" * 350)]
        with self.assertRaises(AgentLoopError) as raised:
            loop._maybe_compact(context)
        self.assertEqual("context_capacity_exhausted", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
