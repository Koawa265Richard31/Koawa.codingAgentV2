"""D23-C: turn-internal closed execution-group parsing and selection (pure functions).

docs/day-23-memory-layer-upgrade.md §5 models one tool execution as an
indivisible whole::

    ModelTurn(assistant text + tool_call A/B/...)
      + ToolResult(A)
      + ToolResult(B)
      + ...every corresponding result

Only when every ``call_ref`` is uniquely paired and its result is present is the
group *closed*.  Compaction, deletion and movement operate on whole closed
groups, and any pending / in-progress / approval-waiting / unknown tool call is
barred from the compaction source range (§5.1, §5.2).

This module is the pure-function half of the slice: it never touches the event
store, never persists, and never imports ``AgentLoop``.  ``parse_closed_groups``
turns a flat model-context sequence into ``ClosedExecutionGroup`` records;
``select_compressible`` picks the compressible prefix; ``anchors_are_preserved``
verifies that a selection leaves the anchors untouched.

Semantics decisions (documented here and exercised by the tests):

* a call without a matching result — i.e. a pending / in-progress / unknown
  call — yields ``closed=False``.  That is a faithful *in-progress* state, not
  corruption, so parsing continues and the group simply stays uncompressible;
* a result whose ``call_ref`` matches no call in its own turn is corrupt data
  and raises ``CompactionError("unpaired_tool_result")``;
* two results for the same ``call_ref`` are contradictory (one call cannot have
  two durable results) and raise ``CompactionError("duplicate_tool_result")``;
* a result appearing outside its turn (a "cross-turn" result) is contradictory
  and raises ``CompactionError("cross_turn_tool_result")`` — it mirrors the
  protocol validator's "tool result crosses model-turn order" rejection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

from ..model.protocol import (
    AssistantMessage,
    InstructionMessage,
    ModelContextItem,
    ReasoningSummaryEcho,
    ToolCallEcho,
    ToolResultMessage,
    UserMessage,
)

_COMPACTION_ERROR = re.compile(r"[a-z][a-z0-9_]{0,127}")


class CompactionError(RuntimeError):
    """Stable, content-free compaction failure safe to print to an operator.

    ``code`` is a lowercase snake_case identifier (``[a-z][a-z0-9_]{0,127}``)
    that never carries the offending value, so it may be persisted in a trace
    or shown to an operator without leaking context content.
    """

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _COMPACTION_ERROR.fullmatch(code):
            raise ValueError("invalid compaction error code")
        self.code = code
        super().__init__(code)


def _check_index(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be an integer >= 0")


@dataclass(frozen=True, slots=True, repr=False)
class ClosedExecutionGroup:
    """One ``ModelTurn`` plus its tool results — the atomic compaction unit."""

    model_turn_id: UUID
    assistant_text: str
    calls: tuple[ToolCallEcho, ...]
    results: tuple[ToolResultMessage, ...]
    first_context_index: int
    last_context_index: int
    closed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.model_turn_id, UUID):
            raise TypeError("model_turn_id must be UUID")
        if not isinstance(self.assistant_text, str):
            raise TypeError("assistant_text must be str")
        if not isinstance(self.calls, tuple):
            raise TypeError("calls must be tuple")
        if not isinstance(self.results, tuple):
            raise TypeError("results must be tuple")
        _check_index(self.first_context_index, "first_context_index")
        _check_index(self.last_context_index, "last_context_index")
        if self.first_context_index > self.last_context_index:
            raise ValueError("first_context_index must not exceed last_context_index")
        if not isinstance(self.closed, bool):
            raise TypeError("closed must be bool")

    def __repr__(self) -> str:
        return (
            f"ClosedExecutionGroup(model_turn_id={self.model_turn_id}, "
            f"calls={len(self.calls)}, results={len(self.results)}, "
            f"span=[{self.first_context_index}, {self.last_context_index}], "
            f"closed={self.closed})"
        )


def _item_turn_id(item: ModelContextItem) -> UUID:
    if isinstance(item, AssistantMessage):
        return item.model_turn_id
    if isinstance(item, ReasoningSummaryEcho):
        return item.model_turn_id
    if isinstance(item, ToolCallEcho):
        return item.call_ref.model_turn_id
    if isinstance(item, ToolResultMessage):
        return item.call_ref.model_turn_id
    raise TypeError("context item does not carry a model turn id")


def _analyze_turn(items: list[tuple[int, ModelContextItem]]) -> ClosedExecutionGroup:
    """Validate pairing within one turn block and build its group record."""
    turn_id = _item_turn_id(items[0][1])
    first_index = items[0][0]
    last_index = items[-1][0]
    text_parts: list[str] = []
    calls: list[ToolCallEcho] = []
    results: list[ToolResultMessage] = []
    declared: set = set()
    matched: set = set()
    for _, item in items:
        if isinstance(item, AssistantMessage):
            text_parts.append(item.item.text)
        elif isinstance(item, ToolCallEcho):
            ref = item.call_ref
            if ref in declared:
                raise CompactionError("duplicate_tool_call")
            declared.add(ref)
            calls.append(item)
        elif isinstance(item, ToolResultMessage):
            ref = item.call_ref
            if ref not in declared:
                raise CompactionError("unpaired_tool_result")
            if ref in matched:
                raise CompactionError("duplicate_tool_result")
            matched.add(ref)
            results.append(item)
        # ReasoningSummaryEcho carries no call/result; it only extends the span.
    return ClosedExecutionGroup(
        model_turn_id=turn_id,
        assistant_text="".join(text_parts),
        calls=tuple(calls),
        results=tuple(results),
        first_context_index=first_index,
        last_context_index=last_index,
        closed=len(matched) == len(calls),
    )


def parse_closed_groups(
    context: Sequence[ModelContextItem],
) -> tuple[ClosedExecutionGroup, ...]:
    """Split a model-context sequence into ``ClosedExecutionGroup`` records.

    A turn is opened by an ``AssistantMessage`` (or, tolerantly, a
    ``ReasoningSummaryEcho`` / ``ToolCallEcho``) and runs until the next turn
    item carries a different ``model_turn_id`` or an
    ``InstructionMessage`` / ``UserMessage`` boundary appears.  A group is
    ``closed`` only when every call has a matching result (unique ``call_ref``,
    both ``call_id`` and ``model_turn_id`` agree).  See the module docstring for
    the exact open-vs-corrupt error split.
    """
    groups: list[ClosedExecutionGroup] = []
    current: list[tuple[int, ModelContextItem]] = []
    current_turn: UUID | None = None

    def flush() -> None:
        nonlocal current, current_turn
        if current:
            groups.append(_analyze_turn(current))
        current = []
        current_turn = None

    for index, item in enumerate(context):
        if not isinstance(item, ModelContextItem):
            raise TypeError("context contains a non-ModelContextItem value")
        if isinstance(item, (InstructionMessage, UserMessage)):
            flush()
            continue
        if isinstance(item, ToolResultMessage):
            # A result must belong to the turn currently open; anything else is
            # a cross-turn result (corrupt), mirroring the protocol validator's
            # "tool result crosses model-turn order" rejection.
            if current_turn is None or item.call_ref.model_turn_id != current_turn:
                raise CompactionError("cross_turn_tool_result")
            current.append((index, item))
            continue
        item_turn = _item_turn_id(item)
        if current_turn is not None and item_turn != current_turn:
            flush()
        if current_turn is None:
            current_turn = item_turn
        current.append((index, item))

    flush()
    return tuple(groups)


def select_compressible(
    groups: Sequence[ClosedExecutionGroup], *, keep_recent: int
) -> tuple[ClosedExecutionGroup, ...]:
    """Return the older closed groups, keeping the most recent ``keep_recent``.

    Only ``closed=True`` groups are ever candidates; the ``keep_recent`` most
    recent closed groups are anchors (§5.2 "最近 in_run_keep_groups 个 closed
    groups 永不压缩") and are excluded.  ``keep_recent < 1`` is invalid.
    """
    if isinstance(keep_recent, bool) or not isinstance(keep_recent, int):
        raise TypeError("keep_recent must be an int")
    if keep_recent < 1:
        raise ValueError("keep_recent must be >= 1")
    closed = tuple(group for group in groups if group.closed)
    if keep_recent >= len(closed):
        return ()
    return closed[: len(closed) - keep_recent]


def anchors_are_preserved(
    groups: Sequence[ClosedExecutionGroup],
    selected: Sequence[ClosedExecutionGroup],
) -> bool:
    """Verify a selection leaves every anchor untouched (pure-function level).

    An anchor at this layer is either a non-closed (pending / unknown) group or
    a recent group.  Since ``in_run_keep_groups >= 1``, the most recent group is
    always an anchor.  ``selected`` is valid iff it is empty, or it is a strict
    contiguous prefix of ``groups`` (the oldest groups) with every selected
    group ``closed=True``.
    """
    groups = tuple(groups)
    selected = tuple(selected)
    if any(not group.closed for group in selected):
        return False
    if not selected:
        return True
    if len(selected) >= len(groups):
        return False
    return selected == groups[: len(selected)]
