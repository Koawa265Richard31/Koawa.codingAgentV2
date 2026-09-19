"""D2 的 Provider-neutral 模型协议。

model/protocol.py 把 Provider 流事件、完成态模型事实和下一轮上下文拆成不同类型，
通过不可变 dataclass、严格 JSON、稳定调用身份和上下文状态机，防止半截 ToolCall、错配 ToolResult、
未知 Provider 输出或敏感正文进入 Agent 执行面

"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeAlias
from uuid import UUID


MODEL_PROTOCOL_VERSION = 1
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_STABLE_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ModelError(Exception):
    """所有可安全跨 Agent Loop 边界传播的模型错误。"""

    def __init__(self, code: str) -> None:
        self.code = _stable_code(code, "code")
        super().__init__(self.code)


class ModelProtocolError(ModelError):
    """Canonical 模型事件违反本地协议。"""


class ModelStreamFailure(ModelError):
    """Provider 用 typed terminal 明确报告模型流失败。"""

    def __init__(
        self,
        code: str,
        *,
        kind: "StreamFailureKind",
        retryable: bool,
    ) -> None:
        if not isinstance(kind, StreamFailureKind):
            raise TypeError("kind must be StreamFailureKind")
        if not isinstance(retryable, bool):
            raise TypeError("retryable must be bool")
        self.kind = kind
        self.retryable = retryable
        super().__init__(code)


class InstructionRole(StrEnum):
    """模型请求中可信指令的角色。"""

    SYSTEM = "system"
    DEVELOPER = "developer"


class FinishReason(StrEnum):
    """一次完整 Provider 回合的本地终止语义。"""

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    MAX_OUTPUT_TOKENS = "max_output_tokens"
    REFUSED = "refused"
    CONTENT_FILTER = "content_filter"
    INCOMPLETE = "incomplete"


class OutputKind(StrEnum):
    """完成态 OutputItem 的封闭类型集合。"""

    ASSISTANT_TEXT = "assistant_text"
    REASONING_SUMMARY = "reasoning_summary"
    TOOL_CALL = "tool_call"
    BLOCKED = "blocked"


class ContentKind(StrEnum):
    """允许通过 delta 预览的文本目标。"""

    ASSISTANT_TEXT = "assistant_text"
    REASONING_SUMMARY = "reasoning_summary"


class BlockedKind(StrEnum):
    """不能进入模型上下文或工具执行面的输出类型。"""

    UNSUPPORTED = "unsupported"
    MALFORMED = "malformed"


class StreamFailureKind(StrEnum):
    """Canonical 流失败分类；正文错误不进入对象。"""

    PROVIDER_ERROR = "provider_error"
    MALFORMED_EVENT = "malformed_event"
    UNKNOWN_REQUIRED_SEMANTIC = "unknown_required_semantic"
    STREAM_INTERRUPTED = "stream_interrupted"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ModelCallRef:
    """把 call_id 约束在生成它的本地 model_turn_id 内。"""

    model_turn_id: UUID
    call_id: str

    def __post_init__(self) -> None:
        _uuid(self.model_turn_id, "model_turn_id")
        object.__setattr__(self, "call_id", _identifier(self.call_id, "call_id"))


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Provider 明确报告的 token 计数；缺失 total 时不自行合成。

    ``cached_input_tokens`` 是 Provider 报告的提示词缓存命中量（OpenAI 形态
    ``usage.prompt_tokens_details.cached_tokens`` 或 DeepSeek 原生
    ``prompt_cache_hit_tokens``）；未报告时为 None，绝不合成。长任务的输入
    成本几乎由缓存命中率决定，缺失该字段则成本/延迟不可分析。
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int | None = None
    cached_input_tokens: int | None = None

    def __post_init__(self) -> None:
        _non_negative_int(self.input_tokens, "input_tokens")
        _non_negative_int(self.output_tokens, "output_tokens")
        if self.total_tokens is not None:
            _non_negative_int(self.total_tokens, "total_tokens")
        if self.cached_input_tokens is not None:
            _non_negative_int(self.cached_input_tokens, "cached_input_tokens")


@dataclass(frozen=True, slots=True, repr=False)
class ToolDefinition:
    """发送给 Provider 的工具说明；D3 的 Registry 将产生这些值。"""

    name: str
    description: str | None
    input_schema_json: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _tool_name(self.name))
        if self.description is not None and not isinstance(self.description, str):
            raise TypeError("description must be str or None")
        _strict_json_object(self.input_schema_json, "input_schema_json")

    @property
    def input_schema(self) -> dict[str, Any]:
        """每次返回独立 JSON 对象，避免调用方修改协议值。"""
        return _strict_json_object(self.input_schema_json, "input_schema_json")

    def __repr__(self) -> str:
        return (
            f"ToolDefinition(name={self.name!r}, "
            f"description_present={self.description is not None}, "
            f"schema_length={len(self.input_schema_json)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AssistantTextItem:
    """一次模型回合中已经完成的 assistant 文本项。"""

    canonical_index: int
    item_id: str
    text: str

    def __post_init__(self) -> None:
        _non_negative_int(self.canonical_index, "canonical_index")
        object.__setattr__(self, "item_id", _identifier(self.item_id, "item_id"))
        _content(self.text, "text")

    @property
    def kind(self) -> OutputKind:
        return OutputKind.ASSISTANT_TEXT

    def __repr__(self) -> str:
        return (
            f"AssistantTextItem(index={self.canonical_index}, "
            f"item_id={self.item_id!r}, text_length={len(self.text)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PublicReasoningSummaryItem:
    """Provider 明确公开的推理摘要；不表示隐藏 chain-of-thought。"""

    canonical_index: int
    item_id: str
    summary: str

    def __post_init__(self) -> None:
        _non_negative_int(self.canonical_index, "canonical_index")
        object.__setattr__(self, "item_id", _identifier(self.item_id, "item_id"))
        _content(self.summary, "summary")

    @property
    def kind(self) -> OutputKind:
        return OutputKind.REASONING_SUMMARY

    def __repr__(self) -> str:
        return (
            f"PublicReasoningSummaryItem(index={self.canonical_index}, "
            f"item_id={self.item_id!r}, summary_length={len(self.summary)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ToolCallItem:
    """只有 Provider item done 后才可构造的完整工具调用。"""

    canonical_index: int
    item_id: str
    call_id: str
    name: str
    arguments_json: str

    def __post_init__(self) -> None:
        _non_negative_int(self.canonical_index, "canonical_index")
        object.__setattr__(self, "item_id", _identifier(self.item_id, "item_id"))
        object.__setattr__(self, "call_id", _identifier(self.call_id, "call_id"))
        object.__setattr__(self, "name", _tool_name(self.name))
        _strict_json_object(self.arguments_json, "arguments_json")

    @property
    def kind(self) -> OutputKind:
        return OutputKind.TOOL_CALL

    @property
    def arguments(self) -> dict[str, Any]:
        """返回防御性解析后的参数，不暴露内部可变映射。"""
        return _strict_json_object(self.arguments_json, "arguments_json")

    def __repr__(self) -> str:
        return (
            f"ToolCallItem(index={self.canonical_index}, item_id={self.item_id!r}, "
            f"call_id={self.call_id!r}, name={self.name!r}, "
            f"arguments_length={len(self.arguments_json)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BlockedItem:
    """未知或畸形输出的安全墓碑，不保留原始正文。"""

    canonical_index: int
    item_id: str
    blocked_kind: BlockedKind
    payload_length: int
    sha256: str

    def __post_init__(self) -> None:
        _non_negative_int(self.canonical_index, "canonical_index")
        object.__setattr__(self, "item_id", _identifier(self.item_id, "item_id"))
        if not isinstance(self.blocked_kind, BlockedKind):
            raise TypeError("blocked_kind must be BlockedKind")
        _non_negative_int(self.payload_length, "payload_length")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 must be lowercase SHA-256")

    @property
    def kind(self) -> OutputKind:
        return OutputKind.BLOCKED

    def __repr__(self) -> str:
        return (
            f"BlockedItem(index={self.canonical_index}, item_id={self.item_id!r}, "
            f"blocked_kind={self.blocked_kind.value!r}, "
            f"payload_length={self.payload_length}, sha256={self.sha256!r})"
        )


OutputItem: TypeAlias = (
    AssistantTextItem | PublicReasoningSummaryItem | ToolCallItem | BlockedItem
)


@dataclass(frozen=True, slots=True, repr=False)
class InstructionMessage:
    """可信 system/developer 指令。"""

    role: InstructionRole
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, InstructionRole):
            raise TypeError("role must be InstructionRole")
        _content(self.content, "content", require_non_empty=True)

    def __repr__(self) -> str:
        return f"InstructionMessage(role={self.role.value!r}, content_length={len(self.content)})"


@dataclass(frozen=True, slots=True, repr=False)
class UserMessage:
    """带稳定输入身份的用户消息。"""

    input_id: str
    content: str
    source_interrupt_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_id", _identifier(self.input_id, "input_id"))
        _content(self.content, "content", require_non_empty=True)
        if self.source_interrupt_id is not None:
            object.__setattr__(
                self,
                "source_interrupt_id",
                _identifier(self.source_interrupt_id, "source_interrupt_id"),
            )

    def __repr__(self) -> str:
        return (
            f"UserMessage(input_id={self.input_id!r}, content_length={len(self.content)}, "
            f"interrupt_present={self.source_interrupt_id is not None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AssistantMessage:
    """完成 assistant 文本进入下一轮模型上下文的白名单包装。"""

    source_provider: str
    model_turn_id: UUID
    item: AssistantTextItem

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_provider", _provider(self.source_provider))
        _uuid(self.model_turn_id, "model_turn_id")
        if not isinstance(self.item, AssistantTextItem):
            raise TypeError("item must be AssistantTextItem")

    def __repr__(self) -> str:
        return (
            f"AssistantMessage(provider={self.source_provider!r}, "
            f"model_turn_id={self.model_turn_id}, index={self.item.canonical_index}, "
            f"text_length={len(self.item.text)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ReasoningSummaryEcho:
    """公开推理摘要进入下一轮上下文的显式包装。"""

    source_provider: str
    model_turn_id: UUID
    item: PublicReasoningSummaryItem

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_provider", _provider(self.source_provider))
        _uuid(self.model_turn_id, "model_turn_id")
        if not isinstance(self.item, PublicReasoningSummaryItem):
            raise TypeError("item must be PublicReasoningSummaryItem")

    def __repr__(self) -> str:
        return (
            f"ReasoningSummaryEcho(provider={self.source_provider!r}, "
            f"model_turn_id={self.model_turn_id}, index={self.item.canonical_index}, "
            f"summary_length={len(self.item.summary)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ToolCallEcho:
    """完整 ToolCall 进入下一轮上下文的显式包装。"""

    source_provider: str
    call_ref: ModelCallRef
    item: ToolCallItem

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_provider", _provider(self.source_provider))
        if not isinstance(self.call_ref, ModelCallRef):
            raise TypeError("call_ref must be ModelCallRef")
        if not isinstance(self.item, ToolCallItem):
            raise TypeError("item must be ToolCallItem")
        if self.call_ref.call_id != self.item.call_id:
            raise ValueError("tool call identity mismatch")

    def __repr__(self) -> str:
        return (
            f"ToolCallEcho(provider={self.source_provider!r}, "
            f"model_turn_id={self.call_ref.model_turn_id}, call_id={self.call_ref.call_id!r}, "
            f"name={self.item.name!r}, arguments_length={len(self.item.arguments_json)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ToolResultMessage:
    """按 ModelCallRef 与先前调用关联的模型可见工具结果。"""

    call_ref: ModelCallRef
    content: str
    is_error: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.call_ref, ModelCallRef):
            raise TypeError("call_ref must be ModelCallRef")
        _content(self.content, "content")
        if not isinstance(self.is_error, bool):
            raise TypeError("is_error must be bool")

    def __repr__(self) -> str:
        return (
            f"ToolResultMessage(model_turn_id={self.call_ref.model_turn_id}, "
            f"call_id={self.call_ref.call_id!r}, content_length={len(self.content)}, "
            f"is_error={self.is_error})"
        )


ModelContextItem: TypeAlias = (
    InstructionMessage
    | UserMessage
    | AssistantMessage
    | ReasoningSummaryEcho
    | ToolCallEcho
    | ToolResultMessage
)


@dataclass(frozen=True, slots=True, repr=False)
class ModelRequest:
    """Provider-neutral 请求；只接受经过白名单定义的上下文类型。"""

    model_turn_id: UUID
    provider: str
    model: str
    input_items: tuple[ModelContextItem, ...]
    tool_definitions: tuple[ToolDefinition, ...] = ()
    max_output_tokens: int = 4096
    protocol_version: int = MODEL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != MODEL_PROTOCOL_VERSION:
            raise ValueError("unsupported model request protocol version")
        _uuid(self.model_turn_id, "model_turn_id")
        object.__setattr__(self, "provider", _provider(self.provider))
        object.__setattr__(self, "model", _identifier(self.model, "model"))
        items = tuple(self.input_items)
        tools = tuple(self.tool_definitions)
        _validate_context(items)
        names: set[str] = set()
        for tool in tools:
            if not isinstance(tool, ToolDefinition):
                raise TypeError("tool_definitions contains an invalid item")
            if tool.name in names:
                raise ValueError("duplicate tool definition")
            names.add(tool.name)
        _positive_int(self.max_output_tokens, "max_output_tokens")
        object.__setattr__(self, "input_items", items)
        object.__setattr__(self, "tool_definitions", tools)

    def __repr__(self) -> str:
        return (
            f"ModelRequest(model_turn_id={self.model_turn_id}, provider={self.provider!r}, "
            f"model={self.model!r}, input_count={len(self.input_items)}, "
            f"tool_count={len(self.tool_definitions)}, "
            f"max_output_tokens={self.max_output_tokens})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ModelTurn:
    """完整 Provider response 的 canonical 完成态。"""

    model_turn_id: UUID
    provider: str
    model: str
    provider_response_id: str
    output_items: tuple[OutputItem, ...]
    finish_reason: FinishReason
    usage: ModelUsage | None = None
    protocol_version: int = MODEL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != MODEL_PROTOCOL_VERSION:
            raise ValueError("unsupported model turn protocol version")
        _uuid(self.model_turn_id, "model_turn_id")
        object.__setattr__(self, "provider", _provider(self.provider))
        object.__setattr__(self, "model", _identifier(self.model, "model"))
        object.__setattr__(
            self,
            "provider_response_id",
            _identifier(self.provider_response_id, "provider_response_id"),
        )
        items = tuple(self.output_items)
        item_ids: set[str] = set()
        call_ids: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(
                item,
                (AssistantTextItem, PublicReasoningSummaryItem, ToolCallItem, BlockedItem),
            ):
                raise TypeError("output_items contains an invalid item")
            if item.canonical_index != index:
                raise ValueError("output item canonical index mismatch")
            if item.item_id in item_ids:
                raise ValueError("duplicate output item ID")
            item_ids.add(item.item_id)
            if isinstance(item, ToolCallItem):
                if item.call_id in call_ids:
                    raise ValueError("duplicate call ID inside model turn")
                call_ids.add(item.call_id)
        if not isinstance(self.finish_reason, FinishReason):
            raise TypeError("finish_reason must be FinishReason")
        if self.finish_reason is FinishReason.TOOL_CALLS and not call_ids:
            raise ValueError("TOOL_CALLS finish requires at least one tool call")
        if self.finish_reason is FinishReason.STOP and call_ids:
            raise ValueError("STOP finish cannot contain tool calls")
        if self.usage is not None and not isinstance(self.usage, ModelUsage):
            raise TypeError("usage must be ModelUsage or None")
        object.__setattr__(self, "output_items", items)

    @property
    def final_text(self) -> str:
        """按 canonical 顺序连接该回合的 assistant 文本。"""
        return "".join(
            item.text for item in self.output_items if isinstance(item, AssistantTextItem)
        )

    def __repr__(self) -> str:
        return (
            f"ModelTurn(model_turn_id={self.model_turn_id}, provider={self.provider!r}, "
            f"model={self.model!r}, response_id={self.provider_response_id!r}, "
            f"output_count={len(self.output_items)}, finish_reason={self.finish_reason.value!r}, "
            f"usage_present={self.usage is not None})"
        )


@dataclass(frozen=True, slots=True)
class StreamHeader:
    """每个 canonical stream event 共享的顺序与响应身份。"""

    model_turn_id: UUID
    provider: str
    provider_response_id: str
    sequence: int
    provider_sequence: int | None = None

    def __post_init__(self) -> None:
        _uuid(self.model_turn_id, "model_turn_id")
        object.__setattr__(self, "provider", _provider(self.provider))
        object.__setattr__(
            self,
            "provider_response_id",
            _identifier(self.provider_response_id, "provider_response_id"),
        )
        _non_negative_int(self.sequence, "sequence")
        if self.provider_sequence is not None:
            _non_negative_int(self.provider_sequence, "provider_sequence")


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """Canonical 流的唯一首事件。"""

    header: StreamHeader
    model: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", _identifier(self.model, "model"))


@dataclass(frozen=True, slots=True)
class ItemStarted:
    """建立一个可接收 delta 的 output target。"""

    header: StreamHeader
    canonical_index: int
    item_id: str
    output_kind: OutputKind
    call_id: str | None = None
    tool_name: str | None = None

    def __post_init__(self) -> None:
        _non_negative_int(self.canonical_index, "canonical_index")
        object.__setattr__(self, "item_id", _identifier(self.item_id, "item_id"))
        if not isinstance(self.output_kind, OutputKind):
            raise TypeError("output_kind must be OutputKind")
        if self.output_kind is OutputKind.TOOL_CALL:
            object.__setattr__(self, "call_id", _identifier(self.call_id, "call_id"))
            object.__setattr__(self, "tool_name", _tool_name(self.tool_name))
        elif self.call_id is not None or self.tool_name is not None:
            raise ValueError("only a tool item may carry call identity")


@dataclass(frozen=True, slots=True, repr=False)
class ContentDelta:
    """文本或公开推理摘要的非权威预览分片。"""

    header: StreamHeader
    canonical_index: int
    item_id: str
    content_kind: ContentKind
    delta: str

    def __post_init__(self) -> None:
        _non_negative_int(self.canonical_index, "canonical_index")
        object.__setattr__(self, "item_id", _identifier(self.item_id, "item_id"))
        if not isinstance(self.content_kind, ContentKind):
            raise TypeError("content_kind must be ContentKind")
        _content(self.delta, "delta")

    def __repr__(self) -> str:
        return (
            f"ContentDelta(sequence={self.header.sequence}, index={self.canonical_index}, "
            f"item_id={self.item_id!r}, kind={self.content_kind.value!r}, "
            f"delta_length={len(self.delta)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ToolArgumentsDelta:
    """工具参数 JSON 的非权威预览分片。"""

    header: StreamHeader
    canonical_index: int
    item_id: str
    call_id: str
    delta: str

    def __post_init__(self) -> None:
        _non_negative_int(self.canonical_index, "canonical_index")
        object.__setattr__(self, "item_id", _identifier(self.item_id, "item_id"))
        object.__setattr__(self, "call_id", _identifier(self.call_id, "call_id"))
        _content(self.delta, "delta")

    def __repr__(self) -> str:
        return (
            f"ToolArgumentsDelta(sequence={self.header.sequence}, "
            f"index={self.canonical_index}, item_id={self.item_id!r}, "
            f"call_id={self.call_id!r}, delta_length={len(self.delta)})"
        )


@dataclass(frozen=True, slots=True)
class ItemCompleted:
    """关闭 target 的权威完整 OutputItem。"""

    header: StreamHeader
    item: OutputItem


@dataclass(frozen=True, slots=True)
class UsageReported:
    """Provider 明确报告的一次 response usage。"""

    header: StreamHeader
    usage: ModelUsage

    def __post_init__(self) -> None:
        if not isinstance(self.usage, ModelUsage):
            raise TypeError("usage must be ModelUsage")


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """完整 response 的唯一成功 terminal。"""

    header: StreamHeader
    turn: ModelTurn

    def __post_init__(self) -> None:
        if not isinstance(self.turn, ModelTurn):
            raise TypeError("turn must be ModelTurn")


@dataclass(frozen=True, slots=True)
class StreamFailed:
    """无有效 ModelTurn 时的 typed failure terminal。"""

    header: StreamHeader
    failure_kind: StreamFailureKind
    stable_code: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.failure_kind, StreamFailureKind):
            raise TypeError("failure_kind must be StreamFailureKind")
        object.__setattr__(self, "stable_code", _stable_code(self.stable_code, "stable_code"))
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be bool")


@dataclass(frozen=True, slots=True)
class UnknownEvent:
    """未知 Provider 事件的无正文墓碑；聚合器收到后立即 fail closed。"""

    header: StreamHeader
    type_digest: str
    payload_length: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.type_digest, str) or not re.fullmatch(
            r"unknown-[0-9a-f]{16}", self.type_digest
        ):
            raise ValueError("type_digest must be an opaque event digest")
        _non_negative_int(self.payload_length, "payload_length")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 must be lowercase SHA-256")


ModelStreamEvent: TypeAlias = (
    TurnStarted
    | ItemStarted
    | ContentDelta
    | ToolArgumentsDelta
    | ItemCompleted
    | UsageReported
    | TurnCompleted
    | StreamFailed
    | UnknownEvent
)


def _validate_context(items: tuple[ModelContextItem, ...]) -> None:
    """验证 ToolCall/ToolResult 作用域和模型输出的 canonical 顺序。"""
    active_turn: UUID | None = None
    "一次 Provider 返回对应的 model_turn_id"
    next_output_index = 0
    ordered_calls: list[ModelCallRef] = []
    next_result = 0
    results_started = False
    closed_turns: set[UUID] = set()
    input_ids: set[str] = set()
    all_calls: set[ModelCallRef] = set()
    all_results: set[ModelCallRef] = set()

    def close_active() -> None:
        nonlocal active_turn, next_output_index, next_result, results_started
        if active_turn is None:
            return
        if next_result != len(ordered_calls):
            raise ValueError("model context contains an unresolved tool call")
        closed_turns.add(active_turn)
        active_turn = None
        next_output_index = 0
        ordered_calls.clear()
        next_result = 0
        results_started = False

    def output(model_turn_id: UUID, index: int) -> None:
        nonlocal active_turn, next_output_index
        if active_turn is None:
            if model_turn_id in closed_turns:
                raise ValueError("model context returns to a closed model turn")
            active_turn = model_turn_id
        elif active_turn != model_turn_id:
            close_active()
            if model_turn_id in closed_turns:
                raise ValueError("model context returns to a closed model turn")
            active_turn = model_turn_id
        elif results_started:
            raise ValueError("model output cannot follow its tool result")
        if index != next_output_index:
            raise ValueError("model context output index mismatch")
        next_output_index += 1

    for item in items:
        if isinstance(item, (InstructionMessage, UserMessage)):
            close_active()
            if isinstance(item, UserMessage):
                if item.input_id in input_ids:
                    raise ValueError("duplicate user input ID")
                input_ids.add(item.input_id)
        elif isinstance(item, AssistantMessage):
            output(item.model_turn_id, item.item.canonical_index)
        elif isinstance(item, ReasoningSummaryEcho):
            output(item.model_turn_id, item.item.canonical_index)
        elif isinstance(item, ToolCallEcho):
            output(item.call_ref.model_turn_id, item.item.canonical_index)
            if item.call_ref in all_calls:
                raise ValueError("duplicate tool-call echo")
            all_calls.add(item.call_ref)
            ordered_calls.append(item.call_ref)
        elif isinstance(item, ToolResultMessage):
            if active_turn != item.call_ref.model_turn_id:
                raise ValueError("tool result crosses model-turn order")
            results_started = True
            if next_result >= len(ordered_calls) or ordered_calls[next_result] != item.call_ref:
                raise ValueError("tool results must follow tool-call order")
            if item.call_ref in all_results:
                raise ValueError("duplicate tool result")
            all_results.add(item.call_ref)
            next_result += 1
        else:
            raise TypeError("input_items contains an invalid context item")
    close_active()
    if all_calls != all_results:
        raise ValueError("model request contains unresolved tool calls")


def _strict_json_object(raw: str, name: str) -> dict[str, Any]:
    """严格解析 JSON object，拒绝 duplicate key、NaN 和其他顶层类型。"""
    if not isinstance(raw, str):
        raise TypeError(f"{name} must be str")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"{name} contains a duplicate key")
            result[key] = value
        return result

    def invalid_constant(_: str) -> Any:
        raise ValueError(f"{name} contains a non-JSON number")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=invalid_constant,
        )
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"{name} must be a complete JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _provider(value: str) -> str:
    value = _identifier(value, "provider")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("provider is invalid")
    return value


def _tool_name(value: str | None) -> str:
    if not isinstance(value, str) or not _TOOL_NAME.fullmatch(value):
        raise ValueError("tool name is invalid")
    return value


def _stable_code(value: str, name: str) -> str:
    if not isinstance(value, str) or not _STABLE_CODE.fullmatch(value):
        raise ValueError(f"{name} is invalid")
    return value


def _identifier(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _content(value: str, name: str, *, require_non_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if "\x00" in value:
        raise ValueError(f"{name} contains NUL")
    if require_non_empty and not value.strip():
        raise ValueError(f"{name} must be non-empty")


def _uuid(value: UUID, name: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{name} must be UUID")


def _non_negative_int(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be an integer >= 0")


def _positive_int(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
