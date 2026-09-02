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

import math
import re
import hashlib
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence
from uuid import UUID, uuid4

from ..control.durable_json import (
    USER_INPUT_MAX_UTF8_BYTES,
    CanonicalTextError,
    canonicalize_text,
)
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
from ..workspace.effects import (
    WorkspaceEffectKind,
    WorkspaceEffectResolvedState,
    WorkspaceEffectResultKind,
    WorkspaceEffectState,
    WorkspaceEffectStore,
    workspace_effect_id,
)
from .memory import MemoryConfig

_SESSION_ERROR = re.compile(r"[a-z][a-z0-9_]{0,127}")
_UNTRUSTED_MARKER = "[untrusted-session-summary]"
_SUMMARY_INSTRUCTION = (
    "You are a session summarizer for a coding agent conversation. Condense the "
    "following past turns (user requests and agent answers) into a short factual "
    "summary in the same language as the turns. Keep concrete facts: what was "
    "requested, what was changed, what failed, what remains. Never invent steps."
)
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
    conclusions is an optional TurnConclusionStore used for D23 failed-turn
    echo and window-out conclusion blocks; when absent both features degrade
    to the previous projection behavior (failed turns leave no echo).
    """

    def __init__(
        self,
        *,
        provider: str,
        limits: SessionHistoryLimits | None = None,
        summarize: Callable[[str], str] | None = None,
        conclusions: object | None = None,
        memory: MemoryConfig | None = None,
        plan_projection: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be non-empty")
        if summarize is not None and not callable(summarize):
            raise TypeError("summarize must be callable or None")
        if conclusions is not None and not (
            hasattr(conclusions, "build") and hasattr(conclusions, "load")
        ):
            raise TypeError("conclusions must implement TurnConclusionStore or None")
        if plan_projection is not None and not callable(plan_projection):
            raise TypeError("plan_projection must be callable or None")
        self._provider = provider
        self._limits = limits or SessionHistoryLimits()
        self._summarize = summarize
        self._conclusions = conclusions
        self._memory = memory or MemoryConfig()
        self._plan_projection = plan_projection
        self._turns: list[SessionTurn] = []
        self._compacted: list[CompactionResult] = []
        self._compacted_up_to = 0
        # D23 §7 journal reminder state (turns since last successful journal).
        self._last_journal_turn_count = 0
        self._journal_changed_files: set[str] = set()

    @property
    def plan_projection(self) -> Callable[[], str] | None:
        return self._plan_projection

    @plan_projection.setter
    def plan_projection(self, value: Callable[[], str] | None) -> None:
        if value is not None and not callable(value):
            raise TypeError("plan_projection must be callable or None")
        self._plan_projection = value

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
        """Append one turn whose user text is canonicalized at the entry point.

        The in-process history therefore stores the same canonical
        (redacted/NFC/LF) user_input that the durable turn event carries, so a
        same-process second turn and a restart via SessionHistory.from_thread
        produce identical model context (contract §6.4).
        """
        if not isinstance(turn, SessionTurn):
            raise TypeError("turn must be SessionTurn")
        try:
            canon = canonicalize_text(
                turn.user_input,
                USER_INPUT_MAX_UTF8_BYTES,
                name="user_input",
            )
        except CanonicalTextError as exc:
            raise SessionHistoryError("invalid_session_user_input") from exc
        if not canon.value.strip():
            raise ValueError("user_input must be non-empty text")
        if canon.value != turn.user_input:
            turn = replace(turn, user_input=canon.value)
        self._turns.append(turn)
        # D23 §7.1: track new changed files for the journal reminder.
        self._journal_changed_files.update(turn.changed_files)

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
        """Bounded projection: compaction blocks, conclusion blocks, recent turns.

        D23 §4.5 dedup: in-window successful turns keep only their original
        projection; in-window failed/interrupted/UNKNOWN turns appear only as
        failed-turn echoes; out-of-window turns with a conclusion appear in the
        conclusion block. A turn_id + authoritative_digest appears at most once.
        """
        self.maybe_compact()
        items: list[ModelContextItem] = []
        if self._plan_projection is not None:
            plan_text = self._plan_projection()
            if plan_text.strip():
                items.append(
                    UserMessage(input_id="session:plan", content=plan_text)
                )
        reminder = self._journal_reminder()
        if reminder is not None:
            items.append(reminder)
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
        window_out = self._turns[: len(self._turns) - len(recent)]
        conclusion_items = self._conclusion_items(window_out, offset)
        items.extend(conclusion_items)
        offset += len(conclusion_items)
        failed_echoes: list[str] = []
        for local_index, turn in enumerate(recent):
            items.append(
                UserMessage(
                    input_id=f"session:{offset + local_index}:user",
                    content=turn.user_input,
                )
            )
            if turn.final_text is not None:
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
            else:
                echo = _failed_turn_text(turn)
                if echo is not None:
                    failed_echoes.append(echo)
        # D23 §4.4: at most the NEWEST failed_echo_max_turns echoes are shown.
        limit = self._memory.failed_echo_max_turns
        for local_index, echo in enumerate(failed_echoes[-limit:]):
            items.append(
                AssistantMessage(
                    source_provider=self._provider,
                    model_turn_id=uuid4(),
                    item=AssistantTextItem(
                        0,
                        f"session:echo:{local_index}",
                        echo,
                    ),
                )
            )
        return tuple(items)

    def _conclusion_items(
        self,
        window_out: Sequence[SessionTurn],
        offset: int,
    ) -> list[ModelContextItem]:
        """Out-of-window conclusions as a bounded, deduped block (D23 §4.5).

        Only turns with a recorded conclusion participate; identical
        (turn_id, authoritative_digest) never repeats; the newest turns are
        kept first when the block exceeds conclusion_recent_limit or the
        character budget, and a conclusion is never cut mid-way.
        """
        if self._conclusions is None or not window_out:
            return []
        items: list[ModelContextItem] = []
        seen: set[tuple[str, str]] = set()
        chars = 0
        for index, turn in enumerate(reversed(window_out)):
            conclusion = self._load_conclusion(turn)
            if conclusion is None:
                continue
            key = (str(conclusion.turn_id), conclusion.authoritative_digest)
            if key in seen:
                continue
            seen.add(key)
            content = _conclusion_text(conclusion)
            if chars + len(content) > self._memory.conclusion_max_chars and items:
                break  # keep newest first; oldest are dropped whole
            items.append(
                UserMessage(
                    input_id=f"session:conclusion:{offset + index}",
                    content=content,
                )
            )
            chars += len(content)
            if len(items) >= self._memory.conclusion_recent_limit:
                break
        return list(reversed(items))

    def _load_conclusion(self, turn: SessionTurn):
        """Best-effort recorded conclusion for one turn; None on any failure."""
        if turn.turn_id is None or self._conclusions is None:
            return None
        try:
            recorded = self._conclusions.load(turn.turn_id)
        except Exception:
            return None
        if not recorded:
            return None
        return recorded[-1]

    def _journal_reminder(self) -> UserMessage | None:
        """D23 §7.1 deterministic reminder for the next fresh interactive turn.

        Any of: turns since the last successful journal >= journal_remind_turns;
        the most recent turn is FAILED/TIMED_OUT/OUTCOME_UNKNOWN; or at least
        journal_remind_changed_files new changed files since the last journal.
        The reminder is a low-priority [session-memory-reminder] UserMessage,
        never a system/developer instruction.
        """
        if not self._turns:
            return None
        turns_since = len(self._turns) - self._last_journal_turn_count
        reasons: list[str] = []
        if turns_since >= self._memory.journal_remind_turns:
            reasons.append("turns_since_journal")
        latest = self._turns[-1]
        if latest.status in {"failed", "timed_out", "outcome_unknown"}:
            reasons.append("recent_turn_failed")
        new_files = len(self._journal_changed_files)
        if new_files >= self._memory.journal_remind_changed_files:
            reasons.append(f"changed_files={new_files}")
        if not reasons:
            return None
        return UserMessage(
            input_id="session:journal-reminder",
            content=(
                "[session-memory-reminder]"
                "建议写入 /journal 检查点（低优先级）: "
                + ",".join(reasons)
                + "[/session-memory-reminder]"
            ),
        )

    def mark_journal_written(self) -> None:
        """Record a successful journal export so reminders reset (§7.1)."""
        self._last_journal_turn_count = len(self._turns)
        self._journal_changed_files = set()

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
        conclusions: object | None = None,
        memory: MemoryConfig | None = None,
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
        history = cls(
            provider=provider, limits=limits, summarize=summarize,
            conclusions=conclusions, memory=memory,
        )
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


def _conclusion_text(conclusion) -> str:
    """D23 §4.5 conclusion-block projection of one recorded conclusion.

    Only authoritative bounded fields and an explicitly marked untrusted
    summary are rendered; raw arguments, results, reasoning and credentials
    never enter the projection.
    """
    lines = [
        "[reconstructed-turn-conclusion]",
        f"turn_id={conclusion.turn_id}",
        f"status={conclusion.turn_status}",
    ]
    if conclusion.run_status is not None:
        lines.append(f"run_status={conclusion.run_status}")
    if conclusion.error_codes:
        lines.append("errors=" + ",".join(conclusion.error_codes))
    if conclusion.successful_tools:
        lines.append("tools=" + ",".join(conclusion.successful_tools))
    if conclusion.changed_files:
        lines.append("files=" + ",".join(conclusion.changed_files))
    if conclusion.uncertainty_codes:
        lines.append("uncertainty=" + ",".join(conclusion.uncertainty_codes))
    if conclusion.open_obligations:
        lines.append("obligations=" + ",".join(conclusion.open_obligations))
    lines.append("[/reconstructed-turn-conclusion]")
    if conclusion.untrusted_summary:
        lines.append(
            f"{_UNTRUSTED_MARKER}\n{conclusion.untrusted_summary}"
        )
    return "\n".join(lines)


def _failed_turn_text(turn: SessionTurn) -> str | None:
    """D23 §4.4 deterministic failed-turn echo (never claims model's words)."""
    status = turn.status or "unknown"
    lines = ["[reconstructed-turn-outcome]"]
    lines.append(f"status={status}")
    if turn.error is not None:
        lines.append(f"errors={turn.error}")
    if turn.changed_files:
        lines.append("files=" + ",".join(turn.changed_files))
    lines.append("[/reconstructed-turn-outcome]")
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


class SessionJournal:
    """Ledgered, crash-aware human-readable session artifact."""

    DEFAULT_PATH = "SESSION.md"

    def __init__(self, effect_store: WorkspaceEffectStore) -> None:
        if not isinstance(effect_store, WorkspaceEffectStore):
            raise TypeError("effect_store must be WorkspaceEffectStore")
        self._effects = effect_store

    def write(
        self,
        repo: Path,
        turns: Sequence[SessionTurn],
        *,
        path: str = DEFAULT_PATH,
        run_id: UUID,
        semantic_command_id: UUID,
        repository_identity_digest: str,
    ) -> Path:
        """Write deterministic markdown through a JOURNAL_EXPORT effect."""
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
        relative = Path(path)
        if relative.is_absolute() or not relative.parts or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise SessionHistoryError("invalid_journal_path")
        root = repo.resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise SessionHistoryError("invalid_journal_path") from None
        encoded = "\n".join(lines).encode("utf-8", "strict")
        expected_post = hashlib.sha256(encoded).hexdigest()
        before = target.read_bytes() if target.is_file() else b""
        precondition = hashlib.sha256(before).hexdigest()
        existing = self._effects.load(
            workspace_effect_id(WorkspaceEffectKind.JOURNAL_EXPORT, semantic_command_id)
        )
        if existing is not None:
            precondition = existing.precondition_digest
        intended = self._effects.intend(
            semantic_command_id=semantic_command_id,
            kind=WorkspaceEffectKind.JOURNAL_EXPORT,
            repository_identity_digest=repository_identity_digest,
            agent_id=None,
            run_id=run_id,
            resource_ref=relative.as_posix(),
            base_digest=None,
            input_digest=expected_post,
            precondition_digest=precondition,
            expected_postcondition_digest=expected_post,
        ).record
        if intended.state is WorkspaceEffectState.APPLIED:
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_post:
                raise SessionHistoryError("journal_postcondition_drift")
            return target
        if intended.state is WorkspaceEffectState.OUTCOME_UNKNOWN:
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_post:
                raise SessionHistoryError("journal_outcome_unknown")
            self._effects.resolve_unknown(
                intended.effect_id,
                expected_version=intended.version,
                claim_epoch=intended.claim_epoch,
                claim_token=intended.claim_token,
                unknown_event_id=intended.unknown_event_id,
                resolved_state=WorkspaceEffectResolvedState.APPLIED,
                result_kind=WorkspaceEffectResultKind.SUCCESS,
                reconciler_principal="session-journal",
                evidence_kind="exact_content_digest",
                evidence_digest=expected_post,
            )
            return target
        claimed = (
            intended
            if intended.state is WorkspaceEffectState.CLAIMED
            else self._effects.claim(
                intended.effect_id,
                expected_version=intended.version,
                owner_id="session-journal",
            ).record
        )
        if claimed.state is not WorkspaceEffectState.CLAIMED:
            raise SessionHistoryError("journal_effect_invalid_state")
        # A lost response after replace is reconciled from exact target bytes.
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected_post:
            self._effects.record_applied(
                claimed.effect_id,
                expected_version=claimed.version,
                claim_epoch=claimed.claim_epoch,
                claim_token=claimed.claim_token,
                result_kind=WorkspaceEffectResultKind.SUCCESS,
                result_code="journal_exported",
                exit_code=0,
                postcondition_digest=expected_post,
                evidence_digest=expected_post,
            )
            return target
        temporary_name: str | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
            temporary_name = None
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected_post:
                raise OSError("journal postcondition mismatch")
        except Exception:
            self._effects.record_outcome_unknown(
                claimed.effect_id,
                expected_version=claimed.version,
                claim_epoch=claimed.claim_epoch,
                claim_token=claimed.claim_token,
                uncertainty_code="journal_export_uncertain",
                evidence_digest=None,
            )
            raise
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
        self._effects.record_applied(
            claimed.effect_id,
            expected_version=claimed.version,
            claim_epoch=claimed.claim_epoch,
            claim_token=claimed.claim_token,
            result_kind=WorkspaceEffectResultKind.SUCCESS,
            result_code="journal_exported",
            exit_code=0,
            postcondition_digest=expected_post,
            evidence_digest=expected_post,
        )
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
