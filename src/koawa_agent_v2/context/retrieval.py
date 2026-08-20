"""D13 retrieval: literal scoring, dedupe, untrusted snippets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..agents.graph import AgentError
from .budget import ContextBudget, fit_within_budget
from .index import IndexedFile, RepositoryIndex


@dataclass(frozen=True, slots=True)
class ContextItem:
    path: str
    sha256: str
    line_range: tuple[int, int]
    source: str
    score: int
    snippet: str

    def to_document(self) -> dict:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "line_range": list(self.line_range),
            "source": self.source,
            "score": self.score,
            "snippet": self.snippet,
        }


class ContextRetriever:
    """Score and budget repository context; snippets are untrusted text."""

    def __init__(
        self,
        index: RepositoryIndex,
        *,
        budget: ContextBudget | None = None,
    ) -> None:
        self.index = index
        self.budget = budget or ContextBudget()

    def retrieve(
        self,
        *,
        query: str,
        files: list[IndexedFile],
        max_snippet_lines: int = 40,
    ) -> list[ContextItem]:
        items: list[ContextItem] = []
        seen: set[str] = set()
        for file in sorted(files, key=lambda item: item.path):
            if self.index.is_stale(file):
                continue
            snippet, score, start = self._score_snippet(file, query, max_snippet_lines)
            if score <= 0:
                continue
            item = ContextItem(
                path=file.path,
                sha256=file.sha256,
                line_range=(start, start + max_snippet_lines - 1),
                source="repository",
                score=score,
                snippet=snippet,
            )
            key = (file.path, item.sha256)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
        items.sort(key=lambda item: (-item.score, item.path))
        selected = fit_within_budget(
            [(item.snippet, item.score) for item in items],
            budget=self.budget,
        )
        result: list[ContextItem] = []
        for snippet, _ in selected:
            for item in items:
                if (
                    item.snippet[: self.budget.max_snippet_chars] == snippet
                    and item not in result
                ):
                    result.append(item)
                    break
        return result

    def _score_snippet(
        self,
        file: IndexedFile,
        query: str,
        max_lines: int,
    ) -> tuple[str, int, int]:
        path = self.index.repo_root / file.path
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            return "", 0, 1
        if "\x00" in text[:4096]:
            return "", 0, 1
        needle = query.strip()
        if not needle:
            return text[: self.budget.max_snippet_chars], 1, 1
        matches = [m.start() for m in re.finditer(re.escape(needle), text)]
        if not matches:
            return "", 0, 1
        start = matches[0]
        lines = text.splitlines()
        line_start = text[:start].count("\n") + 1
        snippet = "\n".join(lines[line_start - 1 : line_start - 1 + max_lines])
        return snippet, len(matches) + len(file.path), line_start
