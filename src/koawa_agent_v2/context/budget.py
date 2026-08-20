"""D13 context budget: deterministic truncation and accounting."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextBudget:
    max_chars: int = 120_000
    max_items: int = 200
    max_snippet_chars: int = 8_000

    def __post_init__(self) -> None:
        for value in (self.max_chars, self.max_items, self.max_snippet_chars):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("context budget must be positive integers")


def fit_within_budget(
    items: list[tuple[str, int]],
    *,
    budget: ContextBudget,
) -> list[tuple[str, int]]:
    """Deterministically select items until the char budget is exhausted."""

    result: list[tuple[str, int]] = []
    total = 0
    for text, weight in items:
        if len(result) >= budget.max_items:
            break
        if len(text) > budget.max_snippet_chars:
            text = text[: budget.max_snippet_chars]
        if total + len(text) > budget.max_chars:
            break
        result.append((text, weight))
        total += len(text)
    return result
