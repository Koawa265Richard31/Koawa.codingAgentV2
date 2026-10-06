"""Hardening 2026-09-19 (WP-D/E slice, R2 closure): model-facing read of
published result projections by reference.

The model can read back the EXACT published safe projection of one test
call - (turn_id, call_id[, model_turn_id]) - instead of raw tool bodies.
Thread scope resolves from the execution context's turn: a reference into
another thread is refused.  Availability is explicit and three-valued
(``published`` / ``projection_unavailable`` / ``not_found``); an
unavailable or missing projection never falls back to raw run-execution
bodies, and an error to fetch the state is ``read_unavailable`` - never a
fabricated result.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from ..execution.loop import ToolExecutionContext, ToolExecutionResult
from ..tools.errors import tool_error_result
from ..tools.registry import ToolRegistry
from ..tools.schema import ToolSpec


@dataclass(frozen=True, slots=True)
class ResultReadArguments:
    turn_id: str
    call_id: str
    # Schema binder only accepts plain str annotations; empty means absent
    # (the schema's minLength already rejects empty from the model side).
    model_turn_id: str = ""


def result_read_tool_spec() -> ToolSpec[ResultReadArguments]:
    return ToolSpec(
        "read_result_projection",
        "Read the published metadata-only projection of one test result by "
        "reference.  Pass the FULL reference (turn id + call id + model "
        "turn id); a call-id-only shorthand is answered only when it matches "
        "exactly one call, otherwise availability is ambiguous_reference "
        "with the candidate list - re-query with the full reference.  "
        "Availability is published / projection_unavailable / not_found; "
        "structured diagnostics and a body reference, never raw command "
        "output.  scan_truncated true means the projection scan hit its "
        "event quota - a not_found under it is NOT a full answer, re-query "
        "with the full reference.  Same thread only.",
        ResultReadArguments,
        {
            "type": "object",
            "properties": {
                "turn_id": {
                    "type": "string",
                    "description": "Turn that produced the result.",
                    "minLength": 1,
                    "maxLength": 64,
                },
                "call_id": {
                    "type": "string",
                    "description": "Tool call id within that turn.",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "model_turn_id": {
                    "type": "string",
                    "description": "Model turn id of the call.  Required to "
                    "disambiguate reused call ids.",
                    "maxLength": 64,
                },
            },
            "required": ["turn_id", "call_id"],
            "additionalProperties": False,
        },
    )


def register_result_read_tool(
    registry: ToolRegistry,
    *,
    store,
    runtime,
) -> None:
    """Register ``read_result_projection`` on a not-yet-sealed registry."""

    def handler(
        arguments: ResultReadArguments, *, context: ToolExecutionContext
    ) -> ToolExecutionResult:
        try:
            context_turn = runtime.get_turn(context.turn_id)
            target_turn = runtime.get_turn(UUID(arguments.turn_id))
            if target_turn.thread_id != context_turn.thread_id:
                # Thread scope is a hard boundary: a reference into another
                # thread's results is denied, never downgraded to metadata.
                return tool_error_result("cross_thread_read_denied")
            from .projection import lookup_projection

            result = lookup_projection(
                store,
                target_turn.turn_id,
                arguments.call_id,
                model_turn_id=arguments.model_turn_id or None,
            )
        except Exception as error:
            return tool_error_result(
                getattr(error, "code", "read_unavailable")
            )
        return ToolExecutionResult(
            json.dumps(
                {
                    "turn_id": arguments.turn_id,
                    "call_id": arguments.call_id,
                    "availability": result["availability"],
                    "error_code": result["error_code"],
                    "model_turn_id": result.get("model_turn_id"),
                    "projection": result["projection"],
                    "matches": result.get("matches"),
                    "scan_truncated": bool(result.get("scan_truncated")),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    registry.register(result_read_tool_spec(), handler)
