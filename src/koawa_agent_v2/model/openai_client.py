"""OpenAI-compatible Chat Completions 的标准库流式适配器。

适配器只把已经识别的 SSE 语义转换成 D2 canonical typed events。原始响应、
请求正文、API key 和半截工具参数不会出现在异常或对象表示中。网络或流中断
不会自动重试，因为 Provider 没有承诺重试后复用 response/call identity。
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from time import monotonic
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, BinaryIO

from .protocol import (
    AssistantMessage,
    AssistantTextItem,
    ContentDelta,
    ContentKind,
    FinishReason,
    InstructionMessage,
    ItemCompleted,
    ItemStarted,
    ModelContextItem,
    ModelError,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    ModelUsage,
    OutputItem,
    OutputKind,
    ReasoningSummaryEcho,
    StreamFailed,
    StreamFailureKind,
    StreamHeader,
    ToolArgumentsDelta,
    ToolCallEcho,
    ToolCallItem,
    ToolResultMessage,
    TurnCompleted,
    TurnStarted,
    UsageReported,
    UserMessage,
)


__all__ = ["OpenAICompatibleChatClient", "OpenAICompatibleClientError"]


class OpenAICompatibleClientError(ModelError):
    """尚未建立 response identity 时产生的安全适配器错误。"""


@dataclass(frozen=True, slots=True)
class _AdapterFault(Exception):
    """只携带稳定错误分类，绝不携带 Provider 原始正文。"""

    code: str
    kind: StreamFailureKind
    retryable: bool = False


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """POST 模型端点对任何 3xx fail closed，避免跨源转发 Authorization。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "redirect rejected",
            headers,
            fp,
        )


@dataclass(slots=True)
class _TextBuffer:
    """一个 Chat Completions assistant content 项的临时缓冲。"""

    canonical_index: int
    item_id: str
    fragments: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _ToolBuffer:
    """一个 Provider tool_calls[index] 的临时缓冲。"""

    provider_index: int
    canonical_index: int
    item_id: str
    call_id: str
    name: str
    fragments: list[str] = field(default_factory=list)


class ReasoningEffort(StrEnum):
    """User-facing reasoning intensity knob, translated per provider/model family.

    Providers expose very different knobs (binary thinking on/off vs.
    low/medium/high budgets), so the runtime maps this abstract scale to the
    concrete request body fields in _reasoning_effort_body.
    """

    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def reasoning_family(provider: str, model: str) -> str | None:
    """Return the known reasoning-control family for a provider+model, or None."""
    if provider == "siliconflow":
        lowered = model.lower()
        if "qwen3.5" in lowered:
            return "qwen3.5-thinking"
        if "qwen3" in lowered:
            return "qwen3-thinking"
        if "deepseek" in lowered:
            return "deepseek-thinking"
    if provider in ("openai", "openai_compatible"):
        lowered = model.lower()
        if any(token in lowered for token in ("o1", "o3", "o4")):
            return "openai-reasoning"
    return None


def _reasoning_effort_body(
    provider: str,
    model: str,
    effort: ReasoningEffort,
) -> dict[str, Any]:
    """Translate a reasoning effort into provider-specific request body fields.

    Fail-closed: unknown provider/model families raise and direct the operator
    to provider_options, because guessing a knob we cannot verify is worse
    than refusing.
    """
    family = reasoning_family(provider, model)
    enabled = effort is not ReasoningEffort.OFF
    if family == "qwen3.5-thinking":
        return {"thinking": {"type": "enabled" if enabled else "disabled"}}
    if family == "qwen3-thinking":
        return {"chat_template_kwargs": {"enable_thinking": enabled}}
    if family == "deepseek-thinking":
        return {"thinking": {"type": "enabled" if enabled else "disabled"}}
    if family == "openai-reasoning":
        if not enabled:
            raise OpenAICompatibleClientError("reasoning_effort_off_unsupported")
        return {"reasoning_effort": effort.value}
    raise OpenAICompatibleClientError("reasoning_effort_unsupported")


class OpenAICompatibleChatClient:
    """OpenAI-compatible Chat Completions SSE adapter (D2 canonical stream).

    ``urlopen`` 参数只用于确定性测试或嵌入方注入受控 transport。生产默认使用
    :func:`urllib.request.urlopen`。该类每次 ``stream`` 只发起一次 HTTP 请求。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        provider: str = "openai_compatible",
        timeout_seconds: float = 60.0,
        max_stream_seconds: float = 300.0,
        max_request_bytes: int = 2 * 1024 * 1024,
        max_response_bytes: int = 16 * 1024 * 1024,
        max_sse_event_bytes: int = 2 * 1024 * 1024,
        provider_options: Mapping[str, Any] | None = None,
        reasoning_effort: str | None = None,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        self._endpoint = _chat_completions_endpoint(base_url)
        self._provider_options = dict(provider_options or {})
        self._reasoning_effort: ReasoningEffort | None = None
        if reasoning_effort is not None:
            try:
                self._reasoning_effort = ReasoningEffort(reasoning_effort)
            except ValueError:
                raise ValueError("invalid reasoning_effort") from None
        self._api_key = _api_key(api_key)
        if (
            self._api_key is not None
            and urllib.parse.urlsplit(self._endpoint).scheme.lower() != "https"
        ):
            raise ValueError("api_key requires an HTTPS base_url")
        self._provider = _provider_name(provider)
        self._timeout_seconds = _positive_number(timeout_seconds, "timeout_seconds")
        self._max_stream_seconds = _positive_number(
            max_stream_seconds,
            "max_stream_seconds",
        )
        self._max_request_bytes = _positive_int(max_request_bytes, "max_request_bytes")
        self._max_response_bytes = _positive_int(
            max_response_bytes,
            "max_response_bytes",
        )
        self._max_sse_event_bytes = _positive_int(
            max_sse_event_bytes,
            "max_sse_event_bytes",
        )
        if urlopen is not None and not callable(urlopen):
            raise TypeError("urlopen must be callable or None")
        self._urlopen = urlopen or urllib.request.build_opener(
            _RejectRedirectHandler()
        ).open

    def __repr__(self) -> str:
        """只展示非敏感配置，URL user-info 在构造时已经被拒绝。"""
        return (
            f"OpenAICompatibleChatClient(endpoint={self._endpoint!r}, "
            f"provider={self._provider!r}, api_key_present={self._api_key is not None}, "
            f"timeout_seconds={self._timeout_seconds}, "
            f"max_stream_seconds={self._max_stream_seconds})"
        )

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        """无外部控制器的直接调用；仍受 transport 与整流 deadline 约束。"""
        return self._stream(request, progress_guard=None)

    def stream_controlled(
        self,
        request: ModelRequest,
        *,
        progress_guard: Callable[[], None],
    ) -> Iterator[ModelStreamEvent]:
        """让 Agent Loop 在每条 wire line 处检查取消和 D1 ownership。"""
        if not callable(progress_guard):
            raise TypeError("progress_guard must be callable")
        return self._stream(request, progress_guard=progress_guard)

    def _stream(
        self,
        request: ModelRequest,
        *,
        progress_guard: Callable[[], None] | None,
    ) -> Iterator[ModelStreamEvent]:
        """发送一次请求并把完整 SSE 映射为严格 canonical 事件流。

        在收到 ``[DONE]``、确认 finish reason、关闭全部 Item 并校验 usage 之前，
        不会产生 ``ItemCompleted`` 或 ``TurnCompleted``。因此调用方即使已经看见
        delta，也不能把半截工具参数当作可执行事实。
        """
        deadline = monotonic() + self._max_stream_seconds
        if not isinstance(request, ModelRequest):
            raise TypeError("request must be ModelRequest")
        if request.provider != self._provider:
            raise OpenAICompatibleClientError("openai.provider_mismatch")

        body = _request_body(
            request,
            self._provider_options,
            self._reasoning_effort,
        )
        if len(body) > self._max_request_bytes:
            raise OpenAICompatibleClientError("openai.request_limit_exceeded")
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "koawa-agent-v2/0.1",
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        http_request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        decoder = _ChatStreamDecoder(request)

        try:
            _check_stream_progress(progress_guard, deadline)
            response = self._urlopen(
                http_request,
                timeout=min(self._timeout_seconds, self._max_stream_seconds),
            )
        except _AdapterFault as exc:
            raise OpenAICompatibleClientError(exc.code) from None
        except urllib.error.HTTPError as exc:
            exc.close()
            raise OpenAICompatibleClientError("openai.http_error") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise OpenAICompatibleClientError("openai.transport_error") from None

        try:
            with response:
                _check_stream_progress(progress_guard, deadline)
                _validate_http_response(response)
                for provider_sequence, data in _iter_sse_data(
                    response,
                    max_response_bytes=self._max_response_bytes,
                    max_event_bytes=self._max_sse_event_bytes,
                    progress_guard=progress_guard,
                    deadline=deadline,
                ):
                    events = decoder.feed(data, provider_sequence)
                    for event in events:
                        _check_stream_progress(progress_guard, deadline)
                        yield event
                        decoder.note_emitted(event)
                _check_stream_progress(progress_guard, deadline)
                for event in decoder.finish():
                    _check_stream_progress(progress_guard, deadline)
                    yield event
                    decoder.note_emitted(event)
        except GeneratorExit:
            raise
        except _AdapterFault as exc:
            if decoder.turn_started_emitted:
                failure = decoder.failure_event(exc)
                yield failure
                decoder.note_emitted(failure)
                return
            raise OpenAICompatibleClientError(exc.code) from None
        except (TimeoutError, OSError, urllib.error.URLError):
            fault = _AdapterFault(
                "openai.stream_interrupted",
                StreamFailureKind.STREAM_INTERRUPTED,
                True,
            )
            if decoder.turn_started_emitted:
                failure = decoder.failure_event(fault)
                yield failure
                decoder.note_emitted(failure)
                return
            raise OpenAICompatibleClientError(fault.code) from None


class _ChatStreamDecoder:
    """把 Chat Completions JSON chunks 转成 canonical stream lifecycle。"""

    def __init__(self, request: ModelRequest) -> None:
        self._request = request
        self._provider = request.provider
        self._response_id: str | None = None
        self._model: str | None = None
        self._next_sequence = 0
        self._last_provider_sequence = 0
        self._turn_started_emitted = False
        self._text: _TextBuffer | None = None
        self._tools: dict[int, _ToolBuffer] = {}
        self._outputs: list[_TextBuffer | _ToolBuffer] = []
        self._finish_reason: FinishReason | None = None
        self._usage: ModelUsage | None = None
        self._saw_done = False
        self._terminal_emitted = False

    @property
    def turn_started_emitted(self) -> bool:
        return self._turn_started_emitted

    def note_emitted(self, event: ModelStreamEvent) -> None:
        """记录消费者实际见到的首/终事件，区分单 chunk 内的解析失败。"""
        if isinstance(event, TurnStarted):
            self._turn_started_emitted = True
        if isinstance(event, (TurnCompleted, StreamFailed)):
            self._terminal_emitted = True

    def feed(self, data: str, provider_sequence: int) -> tuple[ModelStreamEvent, ...]:
        """事务式解析一个 SSE data event；失败时不消耗 canonical sequence。"""
        start_sequence = self._next_sequence
        self._last_provider_sequence = provider_sequence
        try:
            return self._feed(data, provider_sequence)
        except _AdapterFault:
            self._next_sequence = start_sequence
            raise
        except (TypeError, ValueError):
            self._next_sequence = start_sequence
            raise _AdapterFault(
                "openai.canonical_mapping_failed",
                StreamFailureKind.MALFORMED_EVENT,
            ) from None

    def _feed(
        self,
        data: str,
        provider_sequence: int,
    ) -> tuple[ModelStreamEvent, ...]:
        if self._saw_done:
            raise _fault("openai.event_after_done")
        if data.strip() == "[DONE]":
            self._saw_done = True
            return ()
        try:
            payload = json.loads(
                data,
                parse_constant=lambda _: _raise_invalid_number(),
            )
        except (json.JSONDecodeError, UnicodeError, ValueError):
            raise _fault("openai.malformed_sse_json") from None
        if not isinstance(payload, dict):
            raise _fault("openai.chunk_not_object")
        if payload.get("error") is not None:
            raise _AdapterFault(
                "openai.provider_error",
                StreamFailureKind.PROVIDER_ERROR,
                False,
            )
        object_type = payload.get("object")
        if object_type is not None and object_type != "chat.completion.chunk":
            raise _unknown("openai.unknown_chunk_object")

        response_id = _required_identifier(payload.get("id"), "response_id")
        model = _required_identifier(payload.get("model"), "model")
        choices = payload.get("choices")
        if not isinstance(choices, list):
            raise _fault("openai.choices_not_array")
        if len(choices) > 1:
            raise _unknown("openai.multiple_choices_unsupported")

        events: list[ModelStreamEvent] = []
        events.extend(
            self._ensure_started(
                response_id,
                model,
                provider_sequence,
            )
        )
        if self._finish_reason is not None and choices:
            # SiliconFlow intermittently appends a trailing chunk that still
            # carries an (empty) choices array after the terminal finish.
            # Tolerate an empty delta; any new content, tool call, or
            # reasoning after finish remains a protocol fault.
            first_after_finish = choices[0]
            delta_after_finish = (
                first_after_finish.get("delta")
                if isinstance(first_after_finish, dict)
                else None
            )
            if isinstance(delta_after_finish, dict) and not (
                delta_after_finish.get("content")
                or delta_after_finish.get("tool_calls")
                or delta_after_finish.get("reasoning_content")
            ):
                return tuple(events)
            raise _fault("openai.choice_after_finish")

        usage_raw = payload.get("usage")
        if usage_raw is not None:
            # Some providers (e.g. SiliconFlow) attach cumulative usage to every
            # chunk; the final chunk is authoritative. Store last-wins here and
            # emit UsageReported exactly once from finish().
            self._usage = _parse_usage(usage_raw)

        if not choices:
            if usage_raw is None:
                raise _unknown("openai.empty_chunk_without_usage")
            return tuple(events)

        choice = choices[0]
        if not isinstance(choice, dict):
            raise _fault("openai.choice_not_object")
        choice_index = choice.get("index")
        if choice_index != 0 or isinstance(choice_index, bool):
            raise _unknown("openai.choice_index_unsupported")
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise _fault("openai.delta_not_object")
        events.extend(self._consume_delta(delta, provider_sequence))

        raw_finish = choice.get("finish_reason")
        if raw_finish is not None:
            if self._finish_reason is not None:
                raise _fault("openai.duplicate_finish_reason")
            self._finish_reason = _finish_reason(raw_finish)
        return tuple(events)

    def _ensure_started(
        self,
        response_id: str,
        model: str,
        provider_sequence: int,
    ) -> tuple[TurnStarted, ...]:
        if self._response_id is None:
            self._response_id = response_id
            self._model = model
            return (
                TurnStarted(
                    self._header(provider_sequence),
                    model,
                ),
            )
        if response_id != self._response_id:
            raise _fault("openai.response_identity_changed")
        if model != self._model:
            raise _fault("openai.response_model_changed")
        return ()

    def _consume_delta(
        self,
        delta: Mapping[str, Any],
        provider_sequence: int,
    ) -> tuple[ModelStreamEvent, ...]:
        # reasoning_content is emitted by thinking-capable models (Qwen3 on
        # SiliconFlow). We accept it but do not forward it: this provider
        # cannot echo reasoning back (openai.reasoning_context_unsupported).
        known = {"role", "content", "tool_calls", "refusal", "reasoning_content"}
        if any(key not in known and value is not None for key, value in delta.items()):
            raise _unknown("openai.unknown_delta_semantic")
        role = delta.get("role")
        if role is not None and role != "assistant":
            raise _unknown("openai.unknown_delta_role")
        refusal = delta.get("refusal")
        if refusal not in (None, ""):
            raise _unknown("openai.refusal_delta_unsupported")

        events: list[ModelStreamEvent] = []
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise _fault("openai.content_delta_not_text")
            if content:
                if self._text is None:
                    item = _TextBuffer(
                        canonical_index=len(self._outputs),
                        item_id=f"chat-text:{self._response_id}",
                    )
                    self._text = item
                    self._outputs.append(item)
                    events.append(
                        ItemStarted(
                            self._header(provider_sequence),
                            item.canonical_index,
                            item.item_id,
                            OutputKind.ASSISTANT_TEXT,
                        )
                    )
                self._text.fragments.append(content)
                events.append(
                    ContentDelta(
                        self._header(provider_sequence),
                        self._text.canonical_index,
                        self._text.item_id,
                        ContentKind.ASSISTANT_TEXT,
                        content,
                    )
                )

        tool_calls = delta.get("tool_calls")
        if tool_calls is not None:
            if not isinstance(tool_calls, list):
                raise _fault("openai.tool_calls_delta_not_array")
            seen_in_chunk: set[int] = set()
            for raw_call in tool_calls:
                if not isinstance(raw_call, dict):
                    raise _fault("openai.tool_call_delta_not_object")
                unknown = {
                    key
                    for key, value in raw_call.items()
                    if key not in {"index", "id", "type", "function"}
                    and value is not None
                }
                if unknown:
                    raise _unknown("openai.unknown_tool_call_semantic")
                provider_index = raw_call.get("index")
                if (
                    not isinstance(provider_index, int)
                    or isinstance(provider_index, bool)
                    or provider_index < 0
                    or provider_index in seen_in_chunk
                ):
                    raise _fault("openai.invalid_tool_call_index")
                seen_in_chunk.add(provider_index)
                events.extend(
                    self._consume_tool_delta(
                        provider_index,
                        raw_call,
                        provider_sequence,
                    )
                )
        return tuple(events)

    def _consume_tool_delta(
        self,
        provider_index: int,
        raw_call: Mapping[str, Any],
        provider_sequence: int,
    ) -> tuple[ModelStreamEvent, ...]:
        raw_type = raw_call.get("type")
        if raw_type is not None and raw_type != "function":
            raise _unknown("openai.unknown_tool_call_type")
        function = raw_call.get("function")
        if function is None:
            function = {}
        if not isinstance(function, dict):
            raise _fault("openai.tool_function_not_object")
        if any(
            key not in {"name", "arguments"} and value is not None
            for key, value in function.items()
        ):
            raise _unknown("openai.unknown_tool_function_semantic")

        call = self._tools.get(provider_index)
        events: list[ModelStreamEvent] = []
        if call is None:
            if provider_index != len(self._tools):
                raise _fault("openai.tool_call_start_order")
            call_id = _required_identifier(raw_call.get("id"), "call_id")
            name = _required_identifier(function.get("name"), "tool_name")
            call = _ToolBuffer(
                provider_index=provider_index,
                canonical_index=len(self._outputs),
                item_id=f"chat-tool:{self._response_id}:{provider_index}",
                call_id=call_id,
                name=name,
            )
            self._tools[provider_index] = call
            self._outputs.append(call)
            events.append(
                ItemStarted(
                    self._header(provider_sequence),
                    call.canonical_index,
                    call.item_id,
                    OutputKind.TOOL_CALL,
                    call.call_id,
                    call.name,
                )
            )
        else:
            raw_id = raw_call.get("id")
            if raw_id is not None and raw_id != call.call_id:
                raise _fault("openai.tool_call_id_changed")
            raw_name = function.get("name")
            # SiliconFlow repeats an empty name ("") on continuation chunks;
            # an empty name carries no identity change, so ignore it.
            if raw_name is not None and raw_name.strip() and raw_name != call.name:
                raise _fault("openai.tool_name_changed")

        arguments = function.get("arguments")
        if arguments is not None:
            if not isinstance(arguments, str):
                raise _fault("openai.tool_arguments_delta_not_text")
            if arguments:
                call.fragments.append(arguments)
                events.append(
                    ToolArgumentsDelta(
                        self._header(provider_sequence),
                        call.canonical_index,
                        call.item_id,
                        call.call_id,
                        arguments,
                    )
                )
        return tuple(events)

    def finish(self) -> tuple[ModelStreamEvent, ...]:
        """在 HTTP EOF 处生成完整 Item/Turn snapshot。"""
        if not self._saw_done:
            raise _AdapterFault(
                "openai.missing_done",
                StreamFailureKind.STREAM_INTERRUPTED,
                True,
            )
        if self._response_id is None or self._model is None:
            raise _fault("openai.empty_stream")
        if self._finish_reason is None:
            raise _fault("openai.missing_finish_reason")
        if self._terminal_emitted:
            raise _fault("openai.duplicate_terminal")

        items: list[OutputItem] = []
        try:
            for output in self._outputs:
                if isinstance(output, _TextBuffer):
                    items.append(
                        AssistantTextItem(
                            output.canonical_index,
                            output.item_id,
                            "".join(output.fragments),
                        )
                    )
                else:
                    items.append(
                        ToolCallItem(
                            output.canonical_index,
                            output.item_id,
                            output.call_id,
                            output.name,
                            "".join(output.fragments),
                        )
                    )
            turn = ModelTurn(
                model_turn_id=self._request.model_turn_id,
                provider=self._provider,
                model=self._model,
                provider_response_id=self._response_id,
                output_items=tuple(items),
                finish_reason=self._finish_reason,
                usage=self._usage,
            )
        except (TypeError, ValueError):
            raise _fault("openai.invalid_completed_snapshot") from None

        provider_sequence = self._last_provider_sequence
        events: list[ModelStreamEvent] = []
        for item in items:
            events.append(
                ItemCompleted(
                    self._header(provider_sequence),
                    item,
                )
            )
        if self._usage is not None:
            events.append(
                UsageReported(
                    self._header(provider_sequence),
                    self._usage,
                )
            )
        events.append(
            TurnCompleted(
                self._header(provider_sequence),
                turn,
            )
        )
        return tuple(events)

    def failure_event(self, fault: _AdapterFault) -> StreamFailed:
        """在已建立且已发布 identity 后关闭 canonical stream。"""
        if self._response_id is None:
            raise OpenAICompatibleClientError(fault.code)
        return StreamFailed(
            self._header(self._last_provider_sequence),
            fault.kind,
            fault.code,
            fault.retryable,
        )

    def _header(self, provider_sequence: int) -> StreamHeader:
        if self._response_id is None:
            raise _fault("openai.response_identity_missing")
        header = StreamHeader(
            model_turn_id=self._request.model_turn_id,
            provider=self._provider,
            provider_response_id=self._response_id,
            sequence=self._next_sequence,
            provider_sequence=provider_sequence,
        )
        self._next_sequence += 1
        return header


def _request_body(
    request: ModelRequest,
    provider_options: Mapping[str, Any] | None = None,
    reasoning_effort: ReasoningEffort | None = None,
) -> bytes:
    """把 canonical context 投影为 Chat Completions JSON，不记录正文。"""
    try:
        document: dict[str, Any] = {
            "model": request.model,
            "messages": _messages(request.input_items, request.provider),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": request.max_output_tokens,
        }
        if reasoning_effort is not None:
            # Abstract reasoning knob -> provider/model specific fields.
            document.update(
                _reasoning_effort_body(request.provider, request.model, reasoning_effort)
            )
        if provider_options:
            # Raw provider-specific overrides win over generated fields.
            document.update(provider_options)
        if request.tool_definitions:
            document["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        **(
                            {"description": tool.description}
                            if tool.description is not None
                            else {}
                        ),
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tool_definitions
            ]
        return json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except OpenAICompatibleClientError:
        raise
    except (TypeError, ValueError, UnicodeError):
        raise OpenAICompatibleClientError("openai.request_serialization_failed") from None


def _messages(
    items: Sequence[ModelContextItem],
    provider: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    index = 0
    while index < len(items):
        item = items[index]
        if isinstance(item, InstructionMessage):
            result.append({"role": item.role.value, "content": item.content})
            index += 1
            continue
        if isinstance(item, UserMessage):
            result.append({"role": "user", "content": item.content})
            index += 1
            continue
        if isinstance(item, ToolResultMessage):
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": item.call_ref.call_id,
                    "content": json.dumps(
                        {"is_error": item.is_error, "content": item.content},
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                }
            )
            index += 1
            continue
        if isinstance(item, ReasoningSummaryEcho):
            raise OpenAICompatibleClientError(
                "openai.reasoning_context_unsupported"
            )
        if isinstance(item, (AssistantMessage, ToolCallEcho)):
            model_turn_id = (
                item.model_turn_id
                if isinstance(item, AssistantMessage)
                else item.call_ref.model_turn_id
            )
            texts: list[str] = []
            calls: list[dict[str, Any]] = []
            while index < len(items):
                current = items[index]
                current_turn = _context_turn_id(current)
                if current_turn != model_turn_id:
                    break
                if isinstance(current, ReasoningSummaryEcho):
                    raise OpenAICompatibleClientError(
                        "openai.reasoning_context_unsupported"
                    )
                if isinstance(current, AssistantMessage):
                    if current.source_provider != provider:
                        raise OpenAICompatibleClientError(
                            "openai.cross_provider_context_unsupported"
                        )
                    texts.append(current.item.text)
                elif isinstance(current, ToolCallEcho):
                    if current.source_provider != provider:
                        raise OpenAICompatibleClientError(
                            "openai.cross_provider_context_unsupported"
                        )
                    calls.append(
                        {
                            "id": current.item.call_id,
                            "type": "function",
                            "function": {
                                "name": current.item.name,
                                "arguments": current.item.arguments_json,
                            },
                        }
                    )
                else:
                    break
                index += 1
            message: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(texts) if texts else None,
            }
            if calls:
                message["tool_calls"] = calls
            result.append(message)
            continue
        raise OpenAICompatibleClientError("openai.context_item_unsupported")
    return result


def _context_turn_id(item: ModelContextItem) -> object | None:
    if isinstance(item, AssistantMessage):
        return item.model_turn_id
    if isinstance(item, ReasoningSummaryEcho):
        return item.model_turn_id
    if isinstance(item, ToolCallEcho):
        return item.call_ref.model_turn_id
    return None


def _check_stream_progress(
    progress_guard: Callable[[], None] | None,
    deadline: float,
) -> None:
    if progress_guard is not None:
        progress_guard()
    if monotonic() >= deadline:
        raise _AdapterFault(
            "openai.stream_deadline_exceeded",
            StreamFailureKind.STREAM_INTERRUPTED,
            True,
        )


def _iter_sse_data(
    response: BinaryIO,
    *,
    max_response_bytes: int,
    max_event_bytes: int,
    progress_guard: Callable[[], None] | None,
    deadline: float,
) -> Iterator[tuple[int, str]]:
    """按 SSE 规范组合多行 data，并同时执行 body/event 字节上限。"""
    total_bytes = 0
    event_bytes = 0
    data_parts: list[str] = []
    event_type = "message"
    provider_sequence = 0

    while True:
        _check_stream_progress(progress_guard, deadline)
        raw = response.readline(max_event_bytes + 2)
        _check_stream_progress(progress_guard, deadline)
        if raw == b"":
            break
        if not isinstance(raw, bytes):
            raise _fault("openai.response_line_not_bytes")
        total_bytes += len(raw)
        event_bytes += len(raw)
        if total_bytes > max_response_bytes:
            raise _fault("openai.response_limit_exceeded")
        if event_bytes > max_event_bytes:
            raise _fault("openai.sse_event_limit_exceeded")
        if len(raw) >= max_event_bytes + 2 and not raw.endswith((b"\n", b"\r")):
            raise _fault("openai.sse_event_limit_exceeded")
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _fault("openai.sse_not_utf8") from None
        line = line.rstrip("\r\n")
        if not line:
            if data_parts:
                if event_type not in ("", "message"):
                    raise _unknown("openai.unknown_sse_event_type")
                yield provider_sequence, "\n".join(data_parts)
                provider_sequence += 1
            data_parts = []
            event_type = "message"
            event_bytes = 0
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_parts.append(value)
        elif field == "event":
            event_type = value
        elif field in {"id", "retry"}:
            continue
        else:
            # SSE 规范允许忽略未知 transport field；payload 语义仍由 JSON gate 处理。
            continue

    if data_parts:
        if event_type not in ("", "message"):
            raise _unknown("openai.unknown_sse_event_type")
        yield provider_sequence, "\n".join(data_parts)


def _validate_http_response(response: Any) -> None:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    if status != 200:
        raise _AdapterFault(
            "openai.http_error",
            StreamFailureKind.PROVIDER_ERROR,
            False,
        )
    headers = getattr(response, "headers", None)
    content_type = headers.get("Content-Type") if headers is not None else None
    if not isinstance(content_type, str) or not content_type.lower().startswith(
        "text/event-stream"
    ):
        raise _fault("openai.invalid_content_type")


def _parse_usage(value: Any) -> ModelUsage:
    if not isinstance(value, dict):
        raise _fault("openai.usage_not_object")
    input_tokens = value.get("prompt_tokens")
    output_tokens = value.get("completion_tokens")
    total_tokens = value.get("total_tokens")
    for count in (input_tokens, output_tokens):
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise _fault("openai.invalid_usage")
    if total_tokens is not None and (
        not isinstance(total_tokens, int)
        or isinstance(total_tokens, bool)
        or total_tokens < 0
    ):
        raise _fault("openai.invalid_usage")
    return ModelUsage(input_tokens, output_tokens, total_tokens)


def _finish_reason(value: Any) -> FinishReason:
    if not isinstance(value, str):
        raise _fault("openai.invalid_finish_reason")
    mapped = {
        "stop": FinishReason.STOP,
        "tool_calls": FinishReason.TOOL_CALLS,
        "length": FinishReason.MAX_OUTPUT_TOKENS,
        "content_filter": FinishReason.CONTENT_FILTER,
    }.get(value)
    if mapped is None:
        raise _unknown("openai.unknown_finish_reason")
    return mapped


def _chat_completions_endpoint(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url must be non-empty")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain user info")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain query or fragment")
    path = parsed.path.rstrip("/") + "/chat/completions"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _api_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("api_key is invalid")
    if "\r" in value or "\n" in value:
        raise ValueError("api_key is invalid")
    return value


def _provider_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or not all(char.isalnum() or char in "_-" for char in value)
    ):
        raise ValueError("provider is invalid")
    return value


def _required_identifier(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise _fault(f"openai.invalid_{name}")
    return value


def _positive_int(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_number(value: float, name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _fault(code: str) -> _AdapterFault:
    return _AdapterFault(code, StreamFailureKind.MALFORMED_EVENT, False)


def _unknown(code: str) -> _AdapterFault:
    return _AdapterFault(code, StreamFailureKind.UNKNOWN_REQUIRED_SEMANTIC, False)


def _raise_invalid_number() -> Any:
    raise ValueError("invalid JSON number")
