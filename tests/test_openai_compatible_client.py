"""OpenAI-compatible SSE Adapter 的固定协议夹具。"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from uuid import uuid4

from koawa_agent_v2.model.protocol import (
    FinishReason,
    InstructionMessage,
    InstructionRole,
    ItemCompleted,
    ModelRequest,
    ModelStreamFailure,
    StreamFailed,
    ToolCallItem,
    ToolDefinition,
    TurnCompleted,
    UserMessage,
)
from koawa_agent_v2.model.stream import assemble_model_stream
from koawa_agent_v2.model.openai_client import (
    OpenAICompatibleChatClient,
    OpenAICompatibleClientError,
)


class _FakeResponse:
    """只实现 urllib response 在本适配器使用的最小读取面。"""

    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "text/event-stream; charset=utf-8",
    ) -> None:
        self._body = io.BytesIO(body)
        self.status = status
        self.headers = {"Content-Type": content_type}
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
    """记录唯一网络尝试，并返回预置 response。"""

    def __init__(self, response=None, *, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0
        self.request = None
        self.timeout = None

    def __call__(self, request, *, timeout):
        self.calls += 1
        self.request = request
        self.timeout = timeout
        if self.error is not None:
            raise self.error
        return self.response


def _request(*, tools: bool = False) -> ModelRequest:
    definitions = (
        ToolDefinition(
            "read_file",
            "读取文件",
            '{"type":"object","properties":{"path":{"type":"string"}}}',
        ),
        ToolDefinition(
            "search",
            None,
            '{"type":"object","properties":{"query":{"type":"string"}}}',
        ),
    ) if tools else ()
    return ModelRequest(
        model_turn_id=uuid4(),
        provider="openai_compatible",
        model="gpt-test",
        input_items=(
            InstructionMessage(InstructionRole.SYSTEM, "你是 coding agent"),
            UserMessage("input-1", "检查仓库"),
        ),
        tool_definitions=definitions,
        max_output_tokens=321,
    )


def _chunk(
    *,
    choices,
    usage=None,
    response_id: str = "chatcmpl-fixture",
    model: str = "gpt-test",
) -> dict:
    value = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": choices,
    }
    if usage is not None:
        value["usage"] = usage
    return value


def _sse(*events, done: bool = True) -> bytes:
    parts = []
    for event in events:
        raw = event if isinstance(event, str) else json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        parts.append(f"data: {raw}\n\n")
    if done:
        parts.append("data: [DONE]\n\n")
    return "".join(parts).encode("utf-8")


class OpenAICompatibleChatClientTest(unittest.TestCase):
    def test_streams_text_usage_and_safe_request_projection(self) -> None:
        """文本 delta、usage 和完整 Turn snapshot 保持同一身份与严格顺序。"""
        body = _sse(
            _chunk(
                choices=[
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "测试"},
                        "finish_reason": None,
                    }
                ]
            ),
            _chunk(
                choices=[
                    {
                        "index": 0,
                        "delta": {"content": "通过"},
                        "finish_reason": None,
                    }
                ]
            ),
            _chunk(
                choices=[
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ]
            ),
            _chunk(
                choices=[],
                usage={
                    "prompt_tokens": 11,
                    "completion_tokens": 2,
                    "total_tokens": 13,
                },
            ),
        )
        opener = _RecordingUrlOpen(_FakeResponse(body))
        client = OpenAICompatibleChatClient(
            "https://example.test/v1/",
            "super-secret-key",
            timeout_seconds=7,
            urlopen=opener,
        )

        events = tuple(client.stream(_request(tools=True)))
        turn = assemble_model_stream(events)

        self.assertEqual("测试通过", turn.final_text)
        self.assertEqual(FinishReason.STOP, turn.finish_reason)
        self.assertEqual(13, turn.usage.total_tokens)
        self.assertIsInstance(events[-1], TurnCompleted)
        self.assertEqual(list(range(len(events))), [e.header.sequence for e in events])
        self.assertEqual(1, opener.calls)
        self.assertEqual(7.0, opener.timeout)

        wire = json.loads(opener.request.data.decode("utf-8"))
        self.assertTrue(wire["stream"])
        self.assertEqual({"include_usage": True}, wire["stream_options"])
        self.assertEqual(321, wire["max_tokens"])
        self.assertEqual(["system", "user"], [m["role"] for m in wire["messages"]])
        self.assertEqual(["read_file", "search"], [t["function"]["name"] for t in wire["tools"]])
        self.assertNotIn("super-secret-key", opener.request.data.decode("utf-8"))
        self.assertNotIn("super-secret-key", repr(client))

    def test_interleaved_multiple_tool_calls_complete_in_canonical_order(self) -> None:
        """两个调用的 argument delta 可交错，但 call identity 和最终顺序不能串。"""
        body = _sse(
            _chunk(
                choices=[
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-A",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"pa',
                                    },
                                },
                                {
                                    "index": 1,
                                    "id": "call-B",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"qu',
                                    },
                                },
                            ],
                        },
                        "finish_reason": None,
                    }
                ]
            ),
            _chunk(
                choices=[
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "function": {"arguments": 'ery":"agent"}'},
                                },
                                {
                                    "index": 0,
                                    "function": {"arguments": 'th":"README.md"}'},
                                },
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            ),
            _chunk(
                choices=[
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                ]
            ),
        )
        client = OpenAICompatibleChatClient(
            "http://localhost:9999/v1",
            provider="openai_compatible",
            urlopen=_RecordingUrlOpen(_FakeResponse(body)),
        )

        events = tuple(client.stream(_request(tools=True)))
        turn = assemble_model_stream(events)
        calls = tuple(item for item in turn.output_items if isinstance(item, ToolCallItem))

        self.assertEqual(FinishReason.TOOL_CALLS, turn.finish_reason)
        self.assertEqual(["call-A", "call-B"], [call.call_id for call in calls])
        self.assertEqual(
            [{"path": "README.md"}, {"query": "agent"}],
            [call.arguments for call in calls],
        )
        completed = [event.item for event in events if isinstance(event, ItemCompleted)]
        self.assertEqual(tuple(completed), turn.output_items)

    def test_stream_failure_after_identity_never_publishes_completed_snapshot(self) -> None:
        """未知必需语义和缺失 DONE 都只能产生 typed failure terminal。"""
        cases = {
            "unknown_delta": _sse(
                _chunk(
                    choices=[
                        {
                            "index": 0,
                            "delta": {"content": "partial"},
                            "finish_reason": None,
                        }
                    ]
                ),
                _chunk(
                    choices=[
                        {
                            "index": 0,
                            "delta": {"audio": {"id": "opaque"}},
                            "finish_reason": None,
                        }
                    ]
                ),
            ),
            "missing_done": _sse(
                _chunk(
                    choices=[
                        {
                            "index": 0,
                            "delta": {"content": "partial"},
                            "finish_reason": None,
                        }
                    ]
                ),
                _chunk(
                    choices=[
                        {"index": 0, "delta": {}, "finish_reason": "stop"}
                    ]
                ),
                done=False,
            ),
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                client = OpenAICompatibleChatClient(
                    "https://example.test/v1",
                    urlopen=_RecordingUrlOpen(_FakeResponse(body)),
                )
                events = tuple(client.stream(_request()))
                self.assertIsInstance(events[-1], StreamFailed)
                self.assertFalse(any(isinstance(event, ItemCompleted) for event in events))
                self.assertFalse(any(isinstance(event, TurnCompleted) for event in events))
                with self.assertRaises(ModelStreamFailure):
                    assemble_model_stream(events)

    def test_malformed_first_event_and_multiple_choices_are_rejected_without_retry(self) -> None:
        """identity 前的畸形数据抛安全错误，且适配器不会隐式重试。"""
        cases = {
            "malformed_json": _sse('{"id":'),
            "multiple_choices": _sse(
                _chunk(
                    choices=[
                        {"index": 0, "delta": {}, "finish_reason": None},
                        {"index": 1, "delta": {}, "finish_reason": None},
                    ]
                )
            ),
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                opener = _RecordingUrlOpen(_FakeResponse(body))
                client = OpenAICompatibleChatClient(
                    "https://example.test/v1",
                    urlopen=opener,
                )
                with self.assertRaises(OpenAICompatibleClientError):
                    tuple(client.stream(_request()))
                self.assertEqual(1, opener.calls)

    def test_http_and_sse_limits_do_not_leak_api_key_or_raw_body(self) -> None:
        """transport/限额错误只暴露稳定 code，不回显凭据或 Provider 正文。"""
        secret = "api-key-do-not-log"
        raw_secret = "raw-provider-secret"
        too_large = _sse(
            _chunk(
                choices=[
                    {
                        "index": 0,
                        "delta": {"content": raw_secret * 20},
                        "finish_reason": None,
                    }
                ]
            )
        )
        opener = _RecordingUrlOpen(_FakeResponse(too_large))
        client = OpenAICompatibleChatClient(
            "https://example.test/v1",
            secret,
            max_sse_event_bytes=128,
            urlopen=opener,
        )
        with self.assertRaises(OpenAICompatibleClientError) as caught:
            tuple(client.stream(_request()))
        rendered = f"{caught.exception!r} {caught.exception} {client!r}"
        self.assertNotIn(secret, rendered)
        self.assertNotIn(raw_secret, rendered)
        self.assertEqual(1, opener.calls)

        http_error = urllib.error.HTTPError(
            "https://example.test/v1/chat/completions",
            401,
            raw_secret,
            {},
            io.BytesIO(raw_secret.encode()),
        )
        failing = _RecordingUrlOpen(error=http_error)
        client = OpenAICompatibleChatClient(
            "https://example.test/v1",
            secret,
            urlopen=failing,
        )
        with self.assertRaises(OpenAICompatibleClientError) as caught:
            tuple(client.stream(_request()))
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(raw_secret, str(caught.exception))
        self.assertEqual(1, failing.calls)

    def test_invalid_content_type_and_event_after_done_fail_closed(self) -> None:
        """非 SSE 响应和 DONE 后追加 data 都不能被当作合法完成。"""
        invalid_type = _RecordingUrlOpen(
            _FakeResponse(b"{}", content_type="application/json")
        )
        client = OpenAICompatibleChatClient(
            "https://example.test/v1",
            urlopen=invalid_type,
        )
        with self.assertRaises(OpenAICompatibleClientError):
            tuple(client.stream(_request()))

        body = _sse(
            _chunk(
                choices=[
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ]
            )
        ) + _sse(
            _chunk(choices=[]),
            done=False,
        )
        client = OpenAICompatibleChatClient(
            "https://example.test/v1",
            urlopen=_RecordingUrlOpen(_FakeResponse(body)),
        )
        events = tuple(client.stream(_request()))
        self.assertIsInstance(events[-1], StreamFailed)
        self.assertFalse(any(isinstance(event, TurnCompleted) for event in events))


if __name__ == "__main__":
    unittest.main()
