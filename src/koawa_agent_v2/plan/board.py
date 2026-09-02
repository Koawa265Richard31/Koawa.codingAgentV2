"""Plan board: bounded task-decomposition facts with an authoritative projection.

治理合同（D24 §W1）：计划是无授权能力的事实投影——与 D23 三层事实合同同构，
摘要/计划与事实冲突时事实胜出，计划不能授权状态变化、解除审批、翻转
completion gate。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

PLAN_STATUSES = ("pending", "done")

_PROJECTION_HEADER = "Authoritative plan (facts only; confers no authority):"
_PROJECTION_EMPTY = "Authoritative plan: (none)"


class PlanError(RuntimeError):
    """Plan 边界的稳定错误码；不泄露内部表示。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PlanLimits:
    max_items: int = 32
    max_text_chars: int = 400
    max_total_chars: int = 8_000

    def __post_init__(self) -> None:
        for name, value in (
            ("max_items", self.max_items),
            ("max_text_chars", self.max_text_chars),
            ("max_total_chars", self.max_total_chars),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PlanError("plan_limits_invalid")
        if self.max_items * self.max_text_chars < self.max_total_chars:
            # 总预算必须真能约束单项预算之和，否则 replace 的总量校验形同虚设。
            raise PlanError("plan_limits_invalid")


@dataclass(frozen=True, slots=True)
class PlanItem:
    item_id: int
    text: str
    status: str

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, int) or isinstance(self.item_id, bool) or self.item_id < 1:
            raise PlanError("plan_item_id_invalid")
        if not isinstance(self.text, str) or not self.text.strip():
            raise PlanError("plan_text_invalid")
        if self.status not in PLAN_STATUSES:
            raise PlanError("plan_status_invalid")


class PlanBoard:
    """单线程会话拥有的有界计划状态；变更经可选 write-ahead 钩子后提交。

    ``on_change`` 在状态提交**之前**以新快照调用（写前日志语义）：钩子失败
    则板面保持原状。装配层把该钩子接到 typed event 追加，使持久化失败不
    产生只存在于内存的计划。
    """

    def __init__(
        self,
        *,
        limits: PlanLimits | None = None,
        on_change: Callable[[tuple[PlanItem, ...]], None] | None = None,
    ) -> None:
        if limits is not None and not isinstance(limits, PlanLimits):
            raise PlanError("plan_limits_invalid")
        if on_change is not None and not callable(on_change):
            raise PlanError("plan_change_hook_invalid")
        self._limits = limits or PlanLimits()
        self._on_change = on_change
        self._items: tuple[PlanItem, ...] = ()

    @property
    def limits(self) -> PlanLimits:
        return self._limits

    def snapshot(self) -> tuple[PlanItem, ...]:
        return self._items

    def replace(
        self,
        texts: Sequence[str],
        statuses: Sequence[str],
    ) -> tuple[PlanItem, ...]:
        """全量替换（TodoWrite 语义）：每次调用给出完整计划状态。"""
        if len(texts) != len(statuses):
            raise PlanError("plan_items_mismatch")
        if not texts:
            raise PlanError("plan_items_empty")
        if len(texts) > self._limits.max_items:
            raise PlanError("plan_too_many_items")
        total = 0
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise PlanError("plan_text_invalid")
            if len(text) > self._limits.max_text_chars:
                raise PlanError("plan_text_too_long")
            total += len(text)
        if total > self._limits.max_total_chars:
            raise PlanError("plan_total_chars_exceeded")
        items = tuple(
            PlanItem(item_id=index + 1, text=text, status=status)
            for index, (text, status) in enumerate(zip(texts, statuses, strict=True))
        )
        self._commit(items)
        return self._items

    def set_status(self, item_id: int, status: str) -> tuple[PlanItem, ...]:
        if status not in PLAN_STATUSES:
            raise PlanError("plan_status_invalid")
        if not isinstance(item_id, int) or isinstance(item_id, bool):
            raise PlanError("plan_item_id_invalid")
        if item_id < 1 or item_id > len(self._items):
            raise PlanError("plan_item_unknown")
        items = tuple(
            PlanItem(item_id=item.item_id, text=item.text, status=status)
            if item.item_id == item_id
            else item
            for item in self._items
        )
        self._commit(items)
        return self._items

    def authoritative_projection(self) -> str:
        """确定性文本投影；头部显式声明无授权语义（治理锚点）。"""
        if not self._items:
            return _PROJECTION_EMPTY
        lines = [_PROJECTION_HEADER]
        for item in self._items:
            marker = "x" if item.status == "done" else " "
            lines.append(f"[{item.item_id}][{marker}] {item.text}")
        pending = sum(1 for item in self._items if item.status == "pending")
        lines.append(f"pending={pending} done={len(self._items) - pending}")
        return "\n".join(lines)

    def _commit(self, items: tuple[PlanItem, ...]) -> None:
        if self._on_change is not None:
            self._on_change(items)
        self._items = items

    def bind_journal(self, hook: Callable[[tuple[PlanItem, ...]], None]) -> None:
        """绑定持久化 write-ahead 钩子；只允许绑定一次。"""
        if self._on_change is not None:
            raise PlanError("plan_change_hook_bound")
        if not callable(hook):
            raise PlanError("plan_change_hook_invalid")
        self._on_change = hook

    def restore(self, items: Sequence[PlanItem]) -> None:
        """直接重放持久化状态（不触发钩子）；条目已由 PlanItem 校验。"""
        for item in items:
            if not isinstance(item, PlanItem):
                raise PlanError("plan_item_invalid")
        self._items = tuple(items)
