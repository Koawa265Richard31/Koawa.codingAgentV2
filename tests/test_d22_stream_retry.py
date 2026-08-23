"""D22 F6：空完成归一 + 客户端零输出单次有界重试。

端点以合规快照结束但零输出 → openai.empty_completion（独立稳定故障），
客户端在无任何可见输出前提下重试一次；再失败 → openai.empty_completion_retried。
"""

from __future__ import annotations

import io
import json
import unittest
from uuid import uuid4

from koawa_agent_v2.model.openai_client import (
    OpenAICompatibleChatClient,
    OpenAICompatibleClientError,
)
from koawa_agent_v2.model.protocol import (
    InstructionMessage,
    InstructionRole,
    ModelRequest,
    StreamFailed,
    TurnStarted,
    UserMessage,
)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)
        self.status = 200
        self.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        self.closed = False

    def readline(self, size: int = -1) -> bytes:
        return self._body.readline(size)

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class _RecordingUrlOpen:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, request, *, timeout):
        self.calls += 1
        current = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(current, Exception):
            raise current
        return current


def _chunk(*, choices, usage=None, response_id="chatcmpl-f", model="model-x") -> dict:
    value = {"id": response_id, "object": "chat.completion.chunk", "model": model, "choices": choices}
    if usage is not None:
        value["usage"] = usage
    return value


def _sse(*events, done: bool = True) -> bytes:
    parts = []
    for event in events:
        raw = event if isinstance(event, str) else json.dumps(
            event, ensure_ascii=False, separators=(",", ":")
        )
        parts.append(f"data: {raw}\n\n")
    if done:
        parts.append("data: [DONE]\n\n")
    return "".join(parts).encode("utf-8")


def _request() -> ModelRequest:
    return ModelRequest(
        model_turn_id=uuid4(),
        provider="test_provider",
        model="model-x",
        input_items=(
            InstructionMessage(InstructionRole.SYSTEM, "sys"),
            UserMessage("u1", "hello"),
        ),
        tool_definitions=(),
        max_output_tokens=32,
    )


def _empty_sse() -> bytes:
    return _sse(
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
    )


def _normal_sse() -> bytes:
    return _sse(
        _chunk(choices=[{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
               usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}),
    )


def _client(recorder) -> OpenAICompatibleChatClient:
    return OpenAICompatibleChatClient(
        "https://api.example.test/v1",
        provider="test_provider",
        urlopen=recorder,
    )


class EmptyCompletionRetryTest(unittest.TestCase):
    def test_empty_then_normal_succeeds_with_single_retry(self) -> None:
        recorder = _RecordingUrlOpen(_FakeResponse(_empty_sse()), _FakeResponse(_normal_sse()))
        client = _client(recorder)
        events = list(client.stream(_request()))
        self.assertEqual(2, recorder.calls)
        starts = [e for e in events if isinstance(e, TurnStarted)]
        self.assertEqual(1, len(starts))
        self.assertFalse(any(isinstance(e, StreamFailed) for e in events))
        texts = [
            getattr(e, "item", None).text
            for e in events
            if getattr(e, "item", None) is not None
            and hasattr(getattr(e, "item", None), "text")
        ]
        self.assertIn("ok", texts)

    def test_two_empties_fail_with_retried_code(self) -> None:
        recorder = _RecordingUrlOpen(
            _FakeResponse(_empty_sse()),
            _FakeResponse(_empty_sse()),
        )
        client = _client(recorder)
        with self.assertRaises(OpenAICompatibleClientError) as raised:
            list(client.stream(_request()))
        self.assertEqual("openai.empty_completion_retried", raised.exception.code)
        self.assertEqual(2, recorder.calls)

    def test_normal_stream_makes_exactly_one_request(self) -> None:
        recorder = _RecordingUrlOpen(_FakeResponse(_normal_sse()))
        client = _client(recorder)
        events = list(client.stream(_request()))
        self.assertEqual(1, recorder.calls)
        self.assertEqual(1, len([e for e in events if isinstance(e, TurnStarted)]))


if __name__ == "__main__":
    unittest.main()
