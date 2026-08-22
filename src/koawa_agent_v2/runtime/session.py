"""D16 interactive session: bounded conversation history projection + compaction.

Projection is a whitelist: only (user_input, final_text) pairs become model
context items. Tool arguments, raw tool results, credentials and reasoning
never cross into history. Truncation drops the OLDEST turns first; compaction
keeps an authoritative projection of the dropped turns and, when a summarizer
is wired, an explicit [untrusted-session-summary] marker (D13 convention).

History is reconstructible from the event store (TurnState.outcome persists
the worker's final_text), so an interactive CLI can continue a session after a
restart by re-reading the thread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from uuid import UUID, uuid4

from ..model.protocol import (
    AssistantMessage,
    AssistantTextItem,
    InstructionMessage,
    InstructionRole,
    ModelContextItem,
    ModelRequest,
    UserMessage,
)
from ..model.stream import assemble_model_stream

_SESSION_ERROR = re.compile(r"[a-z][a-z0-9_]{0,127}")
_UNTRUSTED_MARKER = "[untrusted-session-summary]"
_SUMMARY_INSTRUCTION = (
    "You are a session summarizer for a coding agent conversation. Condense the "
    "following past turns (user requests and agent answers) into a short factual "
    "summary in the same language as the turns. Keep concrete facts: what was "
    "requested, what was changed, what failed, what remains. Never invent steps."
)


class SessionHistoryError(RuntimeError):
    """Stable, content-free session-history failure safe to print to an operator."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _SESSION_ERROR.fullmatch(code):
            raise ValueError("invalid session history error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SessionTurn:
    """One completed conversational turn, projected under the whitelist."""

    user_input: str
    final_text: str | None = None
    turn_id: UUID | None = None
    status: str | None = None
    error: str | None = None
    changed_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.user_input, str) or not self.user_input.strip():
            raise ValueError("user_input must be non-empty text")
        if self.final_text is not None and (
            not isinstance(self.final_text, str) or not self.final_text.strip()
        ):
            raise ValueError("final_text must be non-empty text or None")
        if self.turn_id is not None and not isinstance(self.turn_id, UUID):
            raise TypeError("turn_id must be UUID or None")
        if self.status is not None and (
            not isinstance(self.status, str) or not self.status.strip()
        ):
            raise ValueError("status must be non-empty text or None")
        if self.error is not None and (
            not isinstance(self.error, str) or not self.error.strip()
        ):
            raise ValueError("error must be non-empty text or None")
        if not isinstance(self.changed_files, tuple) or any(
            not isinstance(path, str) or not path.strip()
            for path in self.changed_files
        ):
            raise ValueError("changed_files must be a tuple of non-empty paths")


@dataclass(frozen=True, slots=True)
class SessionHistoryLimits:
    max_turns: int = 16
    max_chars: int = 32_000
    compact_min_turns: int = 4

    def __post_init__(self) -> None:
        for name, value in (
            ("max_turns", self.max_turns),
            ("compact_min_turns", self.compact_min_turns),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SessionHistoryError(f"invalid_session_{name}")
        if (
            not isinstance(self.max_chars, int)
            or isinstance(self.max_chars, bool)
            or self.max_chars <= 0
            or self.max_chars > 2_000_000
        ):
            raise SessionHistoryError("invalid_session_max_chars")


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Authoritative projection plus an optional untrusted model summary."""

    authoritative: str
    summary: str | None = None
    dropped: tuple[SessionTurn, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.authoritative, str) or not self.authoritative.strip():
            raise ValueError("authoritative must be non-empty text")
        if self.summary is not None and (
            not isinstance(self.summary, str) or not self.summary.strip()
        ):
            raise ValueError("summary must be non-empty text or None")


class SessionHistory:
    """In-memory session memory with bounded projection and compaction.

    provider names the model source used to build AssistantMessage items;
    summarize is an optional callable(text) -> summary for compaction.
    """

    def __init__(
        self,
        *,
        provider: str,
        limits: SessionHistoryLimits | None = None,
        summarize: Callable[[str], str] | None = None,
    ) -> None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be non-empty")
        if summarize is not None and not callable(summarize):
            raise TypeError("summarize must be callable or None")
        self._provider = provider
        self._limits = limits or SessionHistoryLimits()
        self._summarize = summarize
        self._turns: list[SessionTurn] = []
        self._compacted: list[CompactionResult] = []
        self._compacted_up_to = 0

    @property
    def limits(self) -> SessionHistoryLimits:
        return self._limits

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    @property
    def turns(self) -> tuple[SessionTurn, ...]:
        return tuple(self._turns)

    def append(self, turn: SessionTurn) -> None:
        if not isinstance(turn, SessionTurn):
            raise TypeError("turn must be SessionTurn")
        self._turns.append(turn)

    def _bounded_recent(self) -> list[SessionTurn]:
        recent = list(self._turns)
        if len(recent) > self._limits.max_turns:
            recent = recent[-self._limits.max_turns :]

        def size_of(turn: SessionTurn) -> int:
            return len(turn.user_input) + len(turn.final_text or "")

        total = sum(size_of(turn) for turn in recent)
        while recent and total > self._limits.max_chars:
            removed = recent.pop(0)
            total -= size_of(removed)
        return recent

    def maybe_compact(self) -> tuple[CompactionResult, ...]:
        """Compact newly dropped turns when their count hits the threshold."""
        recent = self._bounded_recent()
        dropped = self._turns[: len(self._turns) - len(recent)]
        new_dropped = dropped[self._compacted_up_to :]
        if len(new_dropped) < self._limits.compact_min_turns:
            return tuple(self._compacted)
        summary: str | None = None
        if self._summarize is not None:
            transcript = "\n".join(
                f"user: {turn.user_input}\nagent: {turn.final_text or ''}"
                for turn in new_dropped
            )
            try:
                summary = self._summarize(transcript)
            except Exception:
                summary = None  # failure path: authoritative projection still usable
        authoritative = _authoritative_projection(new_dropped)
        result = CompactionResult(
            authoritative=authoritative,
            summary=summary,
            dropped=tuple(new_dropped),
        )
        self._compacted.append(result)
        self._compacted_up_to += len(new_dropped)
        return tuple(self._compacted)

    def context_items(self) -> tuple[ModelContextItem, ...]:
        """Bounded projection: compaction blocks first, then recent turns."""
        self.maybe_compact()
        items: list[ModelContextItem] = []
        for index, block in enumerate(self._compacted):
            content = block.authoritative
            if block.summary is not None:
                content += f"\n{_UNTRUSTED_MARKER}\n{block.summary}"
            items.append(
                UserMessage(
                    input_id=f"session:compact:{index}",
                    content=content,
                )
            )
        recent = self._bounded_recent()
        offset = len(self._compacted)
        for local_index, turn in enumerate(recent):
            items.append(
                UserMessage(
                    input_id=f"session:{offset + local_index}:user",
                    content=turn.user_input,
                )
            )
            if turn.final_text is None:
                continue  # failed/interrupted turns leave no assistant echo
            items.append(
                AssistantMessage(
                    source_provider=self._provider,
                    model_turn_id=uuid4(),
                    item=AssistantTextItem(
                        0,
                        f"session:{offset + local_index}:assistant",
                        turn.final_text,
                    ),
                )
            )
        return tuple(items)

    @classmethod
    def from_thread(
        cls,
        store,
        runtime,
        thread_id: UUID | str,
        *,
        provider: str,
        limits: SessionHistoryLimits | None = None,
        summarize: Callable[[str], str] | None = None,
    ) -> "SessionHistory":
        """Rebuild session history from the durable thread records."""
        resolved = UUID(str(thread_id))
        try:
            thread = runtime.get_thread(resolved)
        except (ValueError, AttributeError):
            raise SessionHistoryError("thread_not_found") from None
        except Exception as exc:  # AggregateNotFound from thread replay
            if exc.__class__.__name__ == "AggregateNotFound":
                raise SessionHistoryError("thread_not_found") from None
            raise
        del thread
        turn_ids: list[UUID] = []
        cursor = 0
        while True:
            page = store.read_all(after_position=cursor, limit=500)
            for event in page:
                if event.event_type == "turn.created.v1" and UUID(
                    event.payload["thread_id"]
                ) == resolved:
                    turn_ids.append(UUID(event.payload["turn_id"]))
            if len(page) < 500:
                break
            cursor = page[-1].global_position
        history = cls(provider=provider, limits=limits, summarize=summarize)
        for turn_id in turn_ids:
            state = runtime.get_turn(turn_id)
            if not state.is_terminal:
                continue
            history.append(
                SessionTurn(
                    user_input=state.user_input,
                    final_text=state.outcome,
                    turn_id=state.turn_id,
                    status=state.status.value,
                    error=state.error,
                )
            )
        return history


def _authoritative_projection(turns: Sequence[SessionTurn]) -> str:
    """Deterministic, typed projection of dropped turns (never model-written)."""
    lines = [
        "# compacted conversation history (authoritative)",
        "# dropped turns are no longer in the window; only these facts remain",
    ]
    for index, turn in enumerate(turns):
        prefix = f"- turn[{index}]"
        if turn.turn_id is not None:
            prefix += f" id={turn.turn_id}"
        prefix += f" status={turn.status or 'unknown'}"
        if turn.error is not None:
            prefix += f" error={turn.error}"
        if turn.changed_files:
            prefix += " files=" + ",".join(turn.changed_files)
        prefix += f" request={turn.user_input[:160]!r}"
        lines.append(prefix)
    return "\n".join(lines)


def summarize_via_client(
    client,
    *,
    provider: str,
    model: str,
    text: str,
    max_output_tokens: int = 512,
) -> str:
    """Summarize dropped history through the configured model client."""
    if not isinstance(text, str) or not text.strip():
        raise SessionHistoryError("empty_history_to_summarize")
    request = ModelRequest(
        model_turn_id=uuid4(),
        provider=provider,
        model=model,
        input_items=(
            InstructionMessage(InstructionRole.SYSTEM, _SUMMARY_INSTRUCTION),
            UserMessage(input_id=f"summarize:{uuid4()}", content=text),
        ),
        max_output_tokens=max_output_tokens,
    )
    turn = assemble_model_stream(client.stream(request))
    summary = turn.final_text.strip()
    if not summary:
        raise SessionHistoryError("empty_session_summary")
    return summary

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
        """Rank completed turns of a thread by literal query-term hits."""
        if not isinstance(query, str) or not query.strip():
            return ()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        terms = tuple(term for term in query.casefold().split() if term)
        resolved = UUID(str(thread_id))
        records = _thread_records(self._store, self._runtime, resolved)
        hits: list[RecallHit] = []
        for turn, tools in records:
            score = _recall_score(terms, turn, tools)
            if score > 0:
                hits.append(
                    RecallHit(
                        turn_id=turn.turn_id,
                        user_input=turn.user_input,
                        final_text=turn.final_text,
                        tools=tools,
                        files=turn.changed_files,
                        score=score,
                    )
                )
        hits.sort(key=lambda hit: (-hit.score, str(hit.turn_id)))
        return tuple(hits[:limit])


class SessionJournal:
    """Durable, human-readable session artifact written into the repo (D19-5)."""

    DEFAULT_PATH = "SESSION.md"

    def write(
        self,
        repo: Path,
        turns: Sequence[SessionTurn],
        *,
        path: str = DEFAULT_PATH,
    ) -> Path:
        """Write a deterministic markdown summary of the session turns."""
        if not isinstance(repo, Path) or not repo.is_dir():
            raise SessionHistoryError("invalid_journal_repo")
        lines = ["# Session journal", ""]
        lines.append(f"- turns: {len(turns)}")
        lines.append("")
        for index, turn in enumerate(turns):
            lines.append(f"## turn {index}")
            lines.append(f"**user:** {turn.user_input}")
            if turn.final_text is not None:
                lines.append(f"**agent:** {turn.final_text}")
            if turn.changed_files:
                lines.append("**files:** " + ", ".join(turn.changed_files))
            if turn.status is not None:
                lines.append(f"**status:** {turn.status}")
            if turn.error is not None:
                lines.append(f"**error:** {turn.error}")
            lines.append("")
        target = repo / path
        target.write_text("\n".join(lines), encoding="utf-8")
        return target


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
) -> float:
    """Deterministic literal scoring: request/answer x2, tools/files x1."""
    score = 0.0
    text_pool = f"{turn.user_input} {turn.final_text or ''}".casefold()
    for term in terms:
        if term in text_pool:
            score += 2.0
        for tool in tools:
            if term in tool.casefold():
                score += 1.0
        for path in turn.changed_files:
            if term in path.casefold():
                score += 1.0
    return score
