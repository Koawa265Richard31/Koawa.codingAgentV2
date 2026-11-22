"""会话历史召回（D19-4）：单线程持久回合记录上的词典检索与打分。

SessionMemory 按 D23 §6 的确定性公式对线程的已完成回合排序：tokenize
（casefold + 停用词 + 去重保序）、idf = log((1+N)/(1+df))+1、字段权重
（request/final 2.0、error 1.5、tool/file 1.0）与 recency
（0.5 + 0.5*(position+1)/N，越新越高）。扫描范围为 position 相邻两个
turn.created 之间的事件（D1 单活跃回合保证归属）。

共享数据类型 SessionTurn 来自 session_types；本模块不导入 runtime.session。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

from .session_types import SessionTurn

# D23 §6 recall tokenizer: ASCII word chunks plus contiguous CJK runs.
_TOKEN_PATTERN = re.compile(
    r"[A-Za-z0-9_]+|[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]+",
    re.UNICODE,
)
_STOP_WORDS = frozenset(
    {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "to",
        "of", "in", "on", "for", "and", "or", "but", "with", "at", "by",
        "from", "as", "it", "this", "that", "i", "you", "we", "they", "he",
        "she", "not", "no", "do", "does", "did", "have", "has", "had",
        "的", "了", "是", "在", "我", "你", "他", "她", "它", "们", "与",
        "和", "或", "不", "也", "都", "就", "而", "及", "对", "从", "为",
        "这", "那", "个", "等", "把", "被", "让", "用", "以", "其",
        "这个", "那个", "这些", "那些", "一个", "什么", "怎么", "我们",
        "你们", "他们", "没有", "可以", "需要", "应该", "进行", "已经",
        "还是", "或者", "然后", "因为", "所以", "如果", "但是", "并且",
    }
)


@dataclass(frozen=True, slots=True)
class RecallHit:
    """One retrieved turn summary from the event store."""

    turn_id: UUID
    user_input: str
    final_text: str | None = None
    tools: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    score: float = 0.0


class SessionMemory:
    """Lexical retrieval over the durable turn records of one thread (D19-4)."""

    def __init__(self, store, runtime) -> None:
        self._store = store
        self._runtime = runtime

    def recall(
        self,
        thread_id: UUID | str,
        query: str,
        limit: int = 5,
    ) -> tuple[RecallHit, ...]:
        """Rank completed turns of a thread by D23 §6 IDF/recency scoring."""
        if not isinstance(query, str) or not query.strip():
            return ()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        terms = _tokenize(query)
        if not terms:
            return ()
        resolved = UUID(str(thread_id))
        records = _thread_records(self._store, self._runtime, resolved)
        if not records:
            return ()
        idf = _recall_idf(terms, records)
        total = len(records)
        hits: list[RecallHit] = []
        for position, (turn, tools) in enumerate(records):
            score = _recall_score(
                terms, turn, tools,
                total_turns=total, turn_position=position,
            )
            if score <= 0:
                continue
            weighted = 0.0
            text_pool = f"{turn.user_input} {turn.final_text or ''}".casefold()
            error_pool = (turn.error or "").casefold()
            tool_pool = " ".join(tools).casefold()
            file_pool = " ".join(turn.changed_files).casefold()
            for term in terms:
                weight = idf.get(term, 1.0)
                if term in text_pool:
                    weighted += 2.0 * weight
                if term in error_pool:
                    weighted += 1.5 * weight
                if term in tool_pool or term in file_pool:
                    weighted += 1.0 * weight
            recency = 0.5 + 0.5 * ((position + 1) / total)
            hits.append(
                RecallHit(
                    turn_id=turn.turn_id,
                    user_input=turn.user_input,
                    final_text=turn.final_text,
                    tools=tools,
                    files=turn.changed_files,
                    score=weighted * recency,
                )
            )
        hits.sort(key=lambda hit: (-hit.score, str(hit.turn_id)))
        return tuple(hits[:limit])


def _thread_records(
    store,
    runtime,
    thread_id: UUID,
) -> list[tuple[SessionTurn, tuple[str, ...]]]:
    """Completed turns of a thread with their tool names, position-scoped.

    D1 enforces one active turn per thread, so events between two consecutive
    turn.created positions belong to the earlier turn.
    """
    created: list[tuple[int, UUID]] = []
    cursor = 0
    while True:
        page = store.read_all(after_position=cursor, limit=500)
        if not page:
            break
        for event in page:
            if (
                event.event_type == "turn.created.v1"
                and UUID(event.payload["thread_id"]) == thread_id
            ):
                created.append((event.global_position, UUID(event.payload["turn_id"])))
        if len(page) < 500:
            break
        cursor = page[-1].global_position
    created.sort(key=lambda pair: pair[0])
    records: list[tuple[SessionTurn, tuple[str, ...]]] = []
    for index, (position, turn_id) in enumerate(created):
        end = created[index + 1][0] if index + 1 < len(created) else None
        state = runtime.get_turn(turn_id)
        if not state.is_terminal:
            continue
        tools = _scan_tools(store, position, end)
        records.append(
            (
                SessionTurn(
                    user_input=state.user_input,
                    final_text=state.outcome,
                    turn_id=turn_id,
                    status=state.status.value,
                    error=state.error,
                ),
                tools,
            )
        )
    return records


def _scan_tools(store, start: int, end: int | None) -> tuple[str, ...]:
    """Tool names from trace.tool.v1 events within a position range."""
    names: set[str] = set()
    cursor = start
    while True:
        page = store.read_all(after_position=cursor, limit=500)
        if not page:
            break
        for event in page:
            if end is not None and event.global_position >= end:
                return tuple(sorted(names))
            if event.event_type == "trace.tool.v1":
                fields = event.payload.get("fields") or {}
                name = fields.get("tool_name")
                if isinstance(name, str) and name:
                    names.add(name)
        if len(page) < 500:
            break
        cursor = page[-1].global_position
    return tuple(sorted(names))


def _recall_score(
    terms: tuple[str, ...],
    turn: SessionTurn,
    tools: tuple[str, ...],
    *,
    total_turns: int = 1,
    turn_position: int = 0,
) -> float:
    """D23 §6 deterministic IDF/field-weight/recency scoring.

    score = Σ(idf(token) * matched_field_weight) * recency, where idf uses the
    corpus document frequency over the scanned turns and recency is
    ``0.5 + 0.5 * (turn_position + 1) / total_turns`` (newest highest).
    """
    if not terms:
        return 0.0
    text_pool = f"{turn.user_input} {turn.final_text or ''}".casefold()
    error_pool = (turn.error or "").casefold()
    score = 0.0
    for term in terms:
        if term in text_pool:
            score += 2.0
        if term in error_pool:
            score += 1.5
        for tool in tools:
            if term in tool.casefold():
                score += 1.0
        for path in turn.changed_files:
            if term in path.casefold():
                score += 1.0
    total = max(1, total_turns)
    recency = 0.5 + 0.5 * ((turn_position + 1) / total)
    return score * recency


def _tokenize(text: str) -> tuple[str, ...]:
    """D23 §6: Unicode casefold -> alnum/underscore tokens -> de-stopword,
    de-duplicated preserving order."""
    if not isinstance(text, str):
        return ()
    folded = text.casefold()
    seen: set[str] = set()
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.findall(folded):
        token = match.lower()
        if token in _STOP_WORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tuple(tokens)


def _recall_idf(
    terms: tuple[str, ...],
    records: Sequence[tuple[SessionTurn, tuple[str, ...]]],
) -> dict[str, float]:
    """D23 §6 idf(t) = log((1+N)/(1+df(t)))+1 over the scanned corpus."""
    document_frequency: dict[str, int] = {}
    for turn, tools in records:
        pool = {term for term in _tokenize(
            f"{turn.user_input} {turn.final_text or ''} {turn.error or ''}"
        )}
        pool.update(tool.casefold() for tool in tools)
        for term in terms:
            if term in pool:
                document_frequency[term] = document_frequency.get(term, 0) + 1
    total = max(1, len(records))
    return {
        term: math.log((1 + total) / (1 + document_frequency.get(term, 0))) + 1.0
        for term in terms
    }
