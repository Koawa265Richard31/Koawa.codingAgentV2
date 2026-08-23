"""D22 F1：交互完成门（防无工具幻觉完成）——单元 + AgentLoop 集成。
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from koawa_agent_v2.execution.loop import (
    AgentLoop,
    AgentLoopError,
    ToolExecutionResult,
)
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    OutputKind,
    StreamHeader,
    ToolCallItem,
    ToolDefinition,
    TurnCompleted,
    TurnStarted,
    UserMessage,
)
from koawa_agent_v2.runtime.claim_gate import (
    claim_gate_allows,
    claims_workspace_change,
)


class ClaimGateUnitTest(unittest.TestCase):
    def test_claims_pattern(self) -> None:
        self.assertTrue(claims_workspace_change("已创建 index.html"))
        self.assertTrue(claims_workspace_change("我修改了 README"))
        self.assertTrue(claims_workspace_change("成功删除无用文件"))
        self.assertFalse(claims_workspace_change("好的，我来处理"))
        self.assertFalse(claims_workspace_change(""))

    def test_gate_matrix(self) -> None:
        self.assertFalse(claim_gate_allows("已创建 index.html", frozenset()))
        self.assertTrue(claim_gate_allows("已创建 index.html", frozenset({"apply_patch"})))
        self.assertTrue(claim_gate_allows("好的", frozenset()))
        # 声称了但没有写工具 → 拒绝（读类工具不算）。
        self.assertFalse(claim_gate_allows("已修改 README", frozenset({"read_file", "git_status"})))


class _FakeExecutor:
    def __init__(self, *, ok: bool) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def definitions(self):
        return (ToolDefinition("apply_patch", "patch", '{"type":"object","properties":{}}'),)

    def execute(self, call, *, context):
        self.calls.append(call.name)
        if self.ok:
            return ToolExecutionResult("{}")
        return ToolExecutionResult('{"error":{"code":"x"}}', True)


def _stream(request: ModelRequest, items, finish: FinishReason, response_id: str) -> list[ModelStreamEvent]:
    def header(sequence: int) -> StreamHeader:
        return StreamHeader(request.model_turn_id, request.provider, response_id, sequence, sequence)

    events: list[ModelStreamEvent] = [TurnStarted(header(0), request.model)]
    sequence = 1
    for item in items:
        kind = OutputKind.TOOL_CALL if isinstance(item, ToolCallItem) else OutputKind.ASSISTANT_TEXT
        events.append(ItemStarted(header(sequence), item.canonical_index, item.item_id, kind
                                  if not isinstance(item, ToolCallItem) else kind,
                                  None if not isinstance(item, ToolCallItem) else item.call_id,
                                  None if not isinstance(item, ToolCallItem) else item.name))
        sequence += 1
        events.append(ItemCompleted(header(sequence), item))
        sequence += 1
    turn = ModelTurn(request.model_turn_id, request.provider, request.model, response_id,
                     tuple(items), finish)
    events.append(TurnCompleted(header(sequence), turn))
    return events


class _ScriptedClient:
    def __init__(self, final_text: str, *, with_tool_round: bool, tool_ok: bool):
        self.final_text = final_text
        self.with_tool_round = with_tool_round
        self.tool_ok = tool_ok
        self.requests: list[ModelRequest] = []

    def stream(self, request: ModelRequest) -> list[ModelStreamEvent]:
        self.requests.append(request)
        if self.with_tool_round and not self.requests[-1].input_items:
            pass
        if len(self.requests) == 1 and self.with_tool_round:
            call = ToolCallItem(0, "item-tool", "call-tool", "apply_patch", '{"patch_json":"{}"}')
            return _stream(request, (call,), FinishReason.TOOL_CALLS, "r-tool")
        assert self.with_tool_round or len(self.requests) == 1
        item = AssistantTextItem(0, "item-final", self.final_text)
        return _stream(request, (item,), FinishReason.STOP, "r-final")


class ClaimGateLoopTest(unittest.TestCase):
    def _run(self, client, executor) -> str:
        loop = AgentLoop(client, tool_executor=executor, claim_gate=True)
        return loop.run(
            run_id=uuid4(),
            input_items=(UserMessage("u1", "task"),),
            provider="test",
            model="m",
            max_output_tokens=128,
        ).final_text

    def test_claim_without_tool_is_rejected(self) -> None:
        client = _ScriptedClient("已创建文件 index.html", with_tool_round=False, tool_ok=True)
        executor = _FakeExecutor(ok=True)
        with self.assertRaises(AgentLoopError) as raised:
            self._run(client, executor)
        self.assertEqual("claimed_change_without_tool", raised.exception.code)
        self.assertEqual([], executor.calls)

    def test_claim_with_failed_tool_is_rejected(self) -> None:
        client = _ScriptedClient("已创建文件 index.html", with_tool_round=True, tool_ok=False)
        executor = _FakeExecutor(ok=False)
        with self.assertRaises(AgentLoopError) as raised:
            self._run(client, executor)
        self.assertEqual("claimed_change_without_tool", raised.exception.code)
        self.assertEqual(1, len(executor.calls))

    def test_claim_with_successful_tool_passes(self) -> None:
        client = _ScriptedClient("已创建文件 index.html", with_tool_round=True, tool_ok=True)
        executor = _FakeExecutor(ok=True)
        final = self._run(client, executor)
        self.assertEqual("已创建文件 index.html", final)

    def test_plain_answer_without_claim_passes(self) -> None:
        client = _ScriptedClient("好的，我来处理", with_tool_round=False, tool_ok=True)
        executor = _FakeExecutor(ok=True)
        final = self._run(client, executor)
        self.assertEqual("好的，我来处理", final)

    def test_gate_disabled_allows_claim_without_tool(self) -> None:
        client = _ScriptedClient("已创建文件 index.html", with_tool_round=False, tool_ok=True)
        executor = _FakeExecutor(ok=True)
        loop = AgentLoop(client, tool_executor=executor, claim_gate=False)
        final = loop.run(
            run_id=uuid4(),
            input_items=(UserMessage("u1", "task"),),
            provider="test",
            model="m",
            max_output_tokens=128,
        ).final_text
        self.assertEqual("已创建文件 index.html", final)


if __name__ == "__main__":
    unittest.main()
