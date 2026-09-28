"""Hardening 2026-09-19 (WP-E slice): model-facing history recall tool.

Exposes the existing D19-4 metadata-only lexical retrieval
(``SessionMemory.recall``) as a model tool.  Hits carry STRUCTURED METADATA
ONLY - turn id, score, tool names, file names and content LENGTHS; never
free-text previews and never tool-result bodies (R3, closure review
2026-09-25: a truncated user-input/reply preview is still unrestricted
content release, so historical text is not auto-opened as "metadata").
Scope is the CURRENT thread, resolved from the tool execution context;
cross-thread recall is not offered.  Text content stays behind an explicit
per-audience release rule; without one the tool reports the independent
``content_release`` state instead of falling back to raw bodies.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from ..execution.loop import ToolExecutionContext, ToolExecutionResult
from ..tools.errors import tool_error_result
from ..tools.registry import ToolRegistry
from ..tools.schema import ToolSpec

MAX_HITS = 5
CONTENT_RELEASE_STATE = "unavailable_without_release_rule"


@dataclass(frozen=True, slots=True)
class RecallQueryArguments:
    query: str


def recall_tool_spec() -> ToolSpec[RecallQueryArguments]:
    return ToolSpec(
        "recall_history",
        "Metadata-only search over this thread's past turns (lexical IDF "
        "ranking).  Hits carry turn id, score, tool/file names and content "
        "lengths - never free text or result bodies.  Use it to recover what "
        "earlier turns did before re-running anything.",
        RecallQueryArguments,
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to rank past turns by.",
                    "minLength": 1,
                    "maxLength": 512,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )


def register_recall_tool(
    registry: ToolRegistry,
    *,
    store,
    runtime,
    memory_factory: Callable[[], Any] | None = None,
) -> None:
    """Register ``recall_history`` on a not-yet-sealed registry.

    Thread scope resolves from the execution context's turn; failures
    surface as ``recall_unavailable`` - an explicit unavailable state,
    never a fallback to raw bodies.
    """
    memory_obj = (
        memory_factory if memory_factory is not None else _DefaultMemory(store, runtime)
    )

    def handler(
        arguments: RecallQueryArguments, *, context: ToolExecutionContext
    ) -> ToolExecutionResult:
        try:
            memory = (
                memory_obj(context)
                if memory_factory is not None
                else _DefaultMemory(store, runtime)
            )
            turn = runtime.get_turn(context.turn_id)
            hits = memory.recall(turn.thread_id, arguments.query, MAX_HITS)
        except Exception as error:
            return tool_error_result(
                getattr(error, "code", "recall_unavailable")
            )
        hits_payload = [
            {
                "turn_id": str(hit.turn_id),
                "score": round(hit.score, 2),
                "user_input_chars": len(hit.user_input or ""),
                "final_text_chars": (
                    len(hit.final_text) if hit.final_text else None
                ),
                "tools": list(hit.tools),
                "files": list(hit.files),
            }
            for hit in hits
        ]
        return ToolExecutionResult(
            json.dumps(
                {
                    "query": arguments.query,
                    "visibility": "metadata_only",
                    # R3: hit text is not auto-released metadata; a future
                    # per-audience release rule upgrades this state.
                    "content_release": CONTENT_RELEASE_STATE,
                    "hits": hits_payload,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    registry.register(recall_tool_spec(), handler)


class _DefaultMemory:
    """Production SessionMemory resolution (lazy import avoids a cycle)."""

    def __init__(self, store, runtime) -> None:
        self._store = store
        self._runtime = runtime

    def recall(self, thread_id, query, limit):
        from ..runtime.session import SessionMemory

        return SessionMemory(self._store, self._runtime).recall(
            thread_id, query, limit
        )
