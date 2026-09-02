"""update_plan 工具：全量替换权威计划（D24 W1）。

参数采用平行数组（texts/statuses）：sealed ToolSpec 编译器只支持标量与
标量数组，嵌套对象会破坏 D3 冻结合同，不在本切片扩展。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..execution.loop import ToolExecutionContext, ToolExecutionResult
from ..tools.errors import tool_error_result
from ..tools.registry import ToolRegistry
from ..tools.schema import ToolSpec
from .board import PlanBoard, PlanError, PlanLimits


@dataclass(frozen=True, slots=True)
class UpdatePlanArguments:
    texts: tuple[str, ...]
    statuses: tuple[str, ...]


def plan_tool_spec(limits: PlanLimits | None = None) -> ToolSpec[UpdatePlanArguments]:
    """从同一份硬限额生成 update_plan 的 definition 与 decoder。"""
    limits = limits or PlanLimits()
    if not isinstance(limits, PlanLimits):
        raise PlanError("plan_limits_invalid")
    text_item = {
        "type": "string",
        "description": "One plan step; plain text, never executed.",
        "minLength": 1,
        "maxLength": limits.max_text_chars,
    }
    status_item = {
        "type": "string",
        "description": "Step status: 'pending' or 'done'.",
        "minLength": 1,
        "maxLength": 8,
    }
    return ToolSpec(
        "update_plan",
        "Replace the authoritative task plan. Send the FULL plan every call "
        "(pending and done steps); ids are positions 1..N.",
        UpdatePlanArguments,
        {
            "type": "object",
            "properties": {
                "texts": {
                    "type": "array",
                    "description": "Plan step texts in order.",
                    "items": text_item,
                    "minItems": 1,
                    "maxItems": limits.max_items,
                },
                "statuses": {
                    "type": "array",
                    "description": "Status per step, parallel to texts.",
                    "items": status_item,
                    "minItems": 1,
                    "maxItems": limits.max_items,
                },
            },
            "required": ["texts", "statuses"],
            "additionalProperties": False,
        },
    )


class PlanToolRegistry(ToolRegistry):
    """持有 PlanBoard 的最小 registry；注册后即可被 sealed 装配复用。"""

    def __init__(self, board: PlanBoard, *, limits: PlanLimits | None = None) -> None:
        if not isinstance(board, PlanBoard):
            raise PlanError("plan_board_invalid")
        super().__init__()
        self._board = board
        self.register(plan_tool_spec(limits or board.limits), self._update_plan)

    def _update_plan(
        self,
        arguments: UpdatePlanArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del context
        return update_plan_handler(self._board, arguments)


def update_plan_handler(
    board: PlanBoard,
    arguments: UpdatePlanArguments,
) -> ToolExecutionResult:
    try:
        items = board.replace(arguments.texts, arguments.statuses)
    except PlanError as error:
        return tool_error_result(error.code)
    pending = [item.item_id for item in items if item.status == "pending"]
    payload: dict[str, Any] = {
        "ok": True,
        "items": len(items),
        "pending_ids": pending,
        "done_count": len(items) - len(pending),
    }
    return ToolExecutionResult(
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def register_plan_tool(registry: ToolRegistry, board: PlanBoard) -> None:
    """把 update_plan 并入既有 sealed 装配（如 verified coding registry）。"""
    if not isinstance(board, PlanBoard):
        raise PlanError("plan_board_invalid")
    registry.register(
        plan_tool_spec(board.limits),
        _BoundPlanHandler(board),
    )


class _BoundPlanHandler:
    """绑定具体 board 的 handler；保持 registry.register 的可调用合同。"""

    def __init__(self, board: PlanBoard) -> None:
        self._board = board

    def __call__(
        self,
        arguments: UpdatePlanArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del context
        return update_plan_handler(self._board, arguments)
