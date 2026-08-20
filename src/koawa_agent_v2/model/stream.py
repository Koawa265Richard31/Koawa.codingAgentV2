"""把 D2 typed model stream 严格聚合成完整 ``ModelTurn``。

聚合器会读完整条流后才返回，因此任何已完成的早期 ToolCall 都必须等待整个
response 的 terminal 和所有尾部校验通过，之后 Agent Loop 才能产生工具副作用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .protocol import (
    AssistantTextItem,
    BlockedItem,
    ContentDelta,
    ContentKind,
    ItemCompleted,
    ItemStarted,
    ModelProtocolError,
    ModelStreamEvent,
    ModelStreamFailure,
    ModelTurn,
    ModelUsage,
    OutputItem,
    OutputKind,
    PublicReasoningSummaryItem,
    StreamFailed,
    StreamHeader,
    ToolArgumentsDelta,
    ToolCallItem,
    TurnCompleted,
    TurnStarted,
    UnknownEvent,
    UsageReported,
)


@dataclass(frozen=True, slots=True)
class StreamLimits:
    """限制不可信 Provider 流在内存中的占用。"""

    max_events: int = 100_000
    max_items: int = 128
    max_text_chars: int = 2_000_000
    max_argument_chars: int = 1_000_000
    max_total_chars: int = 4_000_000

    def __post_init__(self) -> None:
        for name in (
            "max_events",
            "max_items",
            "max_text_chars",
            "max_argument_chars",
            "max_total_chars",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(slots=True)
class _ItemSlot:
    """一个 output target 的进程内组装状态。"""

    started: ItemStarted
    fragments: list[str] = field(default_factory=list)
    fragment_chars: int = 0
    completed: OutputItem | None = None


class ModelStreamAssembler:
    """验证单条 canonical stream，并构造唯一完成态 ``ModelTurn``。"""

    def __init__(self, limits: StreamLimits | None = None) -> None:
        self._limits = limits or StreamLimits()
        self._next_sequence = 0
        self._event_count = 0
        self._identity: tuple[object, str, str] | None = None
        self._model: str | None = None
        self._items: dict[int, _ItemSlot] = {}
        self._item_ids: set[str] = set()
        self._call_ids: set[str] = set()
        self._usage: ModelUsage | None = None
        self._turn: ModelTurn | None = None
        self._failure: StreamFailed | None = None
        self._terminal = False
        self._total_chars = 0

    def accept(self, event: ModelStreamEvent) -> None:
        """应用一个 typed event；任何非法顺序立即 fail closed。"""
        if self._terminal:
            raise ModelProtocolError("event_after_terminal")
        header = getattr(event, "header", None)
        if not isinstance(header, StreamHeader):
            raise ModelProtocolError("missing_stream_header")
        self._require_sequence(header)
        self._event_count += 1
        if self._event_count > self._limits.max_events:
            raise ModelProtocolError("stream_event_limit_exceeded")

        if self._identity is None:
            if not isinstance(event, TurnStarted):
                raise ModelProtocolError("stream_must_start_with_turn_started")
            self._identity = (
                header.model_turn_id,
                header.provider,
                header.provider_response_id,
            )
            self._model = event.model
            return

        self._require_identity(header)
        if isinstance(event, TurnStarted):
            raise ModelProtocolError("duplicate_turn_started")
        if isinstance(event, ItemStarted):
            self._start_item(event)
        elif isinstance(event, ContentDelta):
            self._append_content(event)
        elif isinstance(event, ToolArgumentsDelta):
            self._append_arguments(event)
        elif isinstance(event, ItemCompleted):
            self._complete_item(event)
        elif isinstance(event, UsageReported):
            if self._usage is not None:
                raise ModelProtocolError("duplicate_usage")
            self._usage = event.usage
        elif isinstance(event, TurnCompleted):
            self._complete_turn(event)
        elif isinstance(event, StreamFailed):
            self._failure = event
            self._terminal = True
        elif isinstance(event, UnknownEvent):
            self._terminal = True
            raise ModelProtocolError("unknown_stream_event")
        else:
            raise ModelProtocolError("unsupported_stream_event")

    def finish(self) -> ModelTurn:
        """在 EOF 处取得完成回合；没有 typed terminal 时拒绝部分结果。"""
        if self._event_count == 0:
            raise ModelProtocolError("empty_model_stream")
        if not self._terminal:
            raise ModelProtocolError("unexpected_stream_eof")
        if self._failure is not None:
            raise ModelStreamFailure(
                self._failure.stable_code,
                kind=self._failure.failure_kind,
                retryable=self._failure.retryable,
            )
        if self._turn is None:
            raise ModelProtocolError("stream_has_no_completed_turn")
        return self._turn

    def _require_sequence(self, header: StreamHeader) -> None:
        if header.sequence != self._next_sequence:
            raise ModelProtocolError("stream_sequence_mismatch")
        self._next_sequence += 1

    def _require_identity(self, header: StreamHeader) -> None:
        identity = (
            header.model_turn_id,
            header.provider,
            header.provider_response_id,
        )
        if identity != self._identity:
            raise ModelProtocolError("stream_identity_changed")

    def _start_item(self, event: ItemStarted) -> None:
        if event.canonical_index in self._items:
            raise ModelProtocolError("duplicate_item_index")
        if event.item_id in self._item_ids:
            raise ModelProtocolError("duplicate_item_id")
        if len(self._items) >= self._limits.max_items:
            raise ModelProtocolError("stream_item_limit_exceeded")
        if event.call_id is not None:
            if event.call_id in self._call_ids:
                raise ModelProtocolError("duplicate_call_id")
            self._call_ids.add(event.call_id)
        self._item_ids.add(event.item_id)
        self._items[event.canonical_index] = _ItemSlot(event)

    def _append_content(self, event: ContentDelta) -> None:
        slot = self._open_slot(event.canonical_index, event.item_id)
        expected_kind = {
            OutputKind.ASSISTANT_TEXT: ContentKind.ASSISTANT_TEXT,
            OutputKind.REASONING_SUMMARY: ContentKind.REASONING_SUMMARY,
        }.get(slot.started.output_kind)
        if expected_kind is None or event.content_kind is not expected_kind:
            raise ModelProtocolError("content_delta_target_mismatch")
        self._append_fragment(slot, event.delta, argument=False)

    def _append_arguments(self, event: ToolArgumentsDelta) -> None:
        slot = self._open_slot(event.canonical_index, event.item_id)
        if slot.started.output_kind is not OutputKind.TOOL_CALL:
            raise ModelProtocolError("tool_delta_target_mismatch")
        if event.call_id != slot.started.call_id:
            raise ModelProtocolError("tool_delta_call_id_mismatch")
        self._append_fragment(slot, event.delta, argument=True)

    def _append_fragment(
        self,
        slot: _ItemSlot,
        fragment: str,
        *,
        argument: bool,
    ) -> None:
        next_item_size = slot.fragment_chars + len(fragment)
        limit = (
            self._limits.max_argument_chars
            if argument
            else self._limits.max_text_chars
        )
        if next_item_size > limit:
            code = (
                "tool_arguments_limit_exceeded"
                if argument
                else "text_output_limit_exceeded"
            )
            raise ModelProtocolError(code)
        if self._total_chars + len(fragment) > self._limits.max_total_chars:
            raise ModelProtocolError("stream_content_limit_exceeded")
        slot.fragments.append(fragment)
        slot.fragment_chars = next_item_size
        self._total_chars += len(fragment)

    def _open_slot(self, index: int, item_id: str) -> _ItemSlot:
        slot = self._items.get(index)
        if slot is None:
            raise ModelProtocolError("delta_before_item_started")
        if slot.started.item_id != item_id:
            raise ModelProtocolError("delta_item_id_mismatch")
        if slot.completed is not None:
            raise ModelProtocolError("delta_after_item_completed")
        return slot

    def _complete_item(self, event: ItemCompleted) -> None:
        item = event.item
        slot = self._open_slot(item.canonical_index, item.item_id)
        self._require_completed_identity(slot.started, item)
        full_content = _item_content(item)
        if slot.fragments:
            if "".join(slot.fragments) != full_content:
                raise ModelProtocolError("completed_item_does_not_match_deltas")
        else:
            limit = (
                self._limits.max_argument_chars
                if isinstance(item, ToolCallItem)
                else self._limits.max_text_chars
            )
            if len(full_content) > limit:
                code = (
                    "tool_arguments_limit_exceeded"
                    if isinstance(item, ToolCallItem)
                    else "text_output_limit_exceeded"
                )
                raise ModelProtocolError(code)
            if self._total_chars + len(full_content) > self._limits.max_total_chars:
                raise ModelProtocolError("stream_content_limit_exceeded")
            self._total_chars += len(full_content)
        slot.completed = item

    @staticmethod
    def _require_completed_identity(started: ItemStarted, item: OutputItem) -> None:
        if item.kind is not started.output_kind:
            raise ModelProtocolError("completed_item_kind_mismatch")
        if isinstance(item, ToolCallItem):
            if item.call_id != started.call_id or item.name != started.tool_name:
                raise ModelProtocolError("completed_tool_identity_mismatch")

    def _complete_turn(self, event: TurnCompleted) -> None:
        if any(slot.completed is None for slot in self._items.values()):
            raise ModelProtocolError("turn_completed_with_open_item")
        ordered_indices = sorted(self._items)
        if ordered_indices != list(range(len(self._items))):
            raise ModelProtocolError("output_item_index_gap")
        ordered_items = tuple(
            self._items[index].completed for index in ordered_indices
        )
        if any(item is None for item in ordered_items):
            raise ModelProtocolError("turn_completed_with_open_item")
        turn = event.turn
        expected_turn_id, expected_provider, expected_response_id = self._identity
        if (
            turn.model_turn_id != expected_turn_id
            or turn.provider != expected_provider
            or turn.provider_response_id != expected_response_id
            or turn.model != self._model
            or turn.output_items != ordered_items
        ):
            raise ModelProtocolError("completed_turn_snapshot_mismatch")
        if turn.usage != self._usage:
            raise ModelProtocolError("completed_turn_usage_mismatch")
        self._turn = turn
        self._terminal = True


def assemble_model_stream(
    events: Iterable[ModelStreamEvent],
    *,
    limits: StreamLimits | None = None,
) -> ModelTurn:
    """消费完整 iterable 并返回唯一合法 ``ModelTurn``。"""
    assembler = ModelStreamAssembler(limits)
    for event in events:
        assembler.accept(event)
    return assembler.finish()


def _item_content(item: OutputItem) -> str:
    if isinstance(item, AssistantTextItem):
        return item.text
    if isinstance(item, PublicReasoningSummaryItem):
        return item.summary
    if isinstance(item, ToolCallItem):
        return item.arguments_json
    if isinstance(item, BlockedItem):
        return ""
    raise ModelProtocolError("unsupported_completed_item")
