"""D13 compaction: untrusted summary + authoritative projections + pairs."""

from __future__ import annotations

from dataclasses import dataclass

from ..agents.graph import AgentError


@dataclass(frozen=True, slots=True)
class ToolCallPair:
    call_ref: str
    tool_name: str
    has_result: bool


@dataclass(frozen=True, slots=True)
class AuthoritativeProjection:
    """Typed safety/execution facts that summary text must never replace."""

    user_goal: str
    constraints: tuple[str, ...]
    changed_files: tuple[str, ...]
    test_evidence: str
    pending_approval: str | None
    unknown_outcome: str | None
    active_children: tuple[str, ...]
    budget: str

    def render(self) -> str:
        return (
            "[authoritative-execution-state]\n"
            f"user_goal={self.user_goal}\n"
            f"constraints={','.join(self.constraints)}\n"
            f"changed_files={','.join(self.changed_files)}\n"
            f"test_evidence={self.test_evidence}\n"
            f"pending_approval={self.pending_approval or 'none'}\n"
            f"unknown_outcome={self.unknown_outcome or 'none'}\n"
            f"active_children={','.join(self.active_children)}\n"
            f"budget={self.budget}\n"
            "[/authoritative-execution-state]"
        )


class Compactor:
    """Build a compacted prompt without dropping obligations or open calls."""

    def __init__(
        self,
        *,
        system_instructions: str,
        developer_instructions: str,
    ) -> None:
        self.system_instructions = system_instructions
        self.developer_instructions = developer_instructions

    def compact(
        self,
        *,
        summary: str,
        projection: AuthoritativeProjection,
        pairs: tuple[ToolCallPair, ...],
    ) -> str:
        for pair in pairs:
            if not pair.has_result:
                raise AgentError("unresolved_tool_call")
        summary = summary.strip()
        if not summary:
            raise AgentError("empty_compaction_summary")
        return "\n".join(
            (
                "[system]",
                self.system_instructions,
                "[/system]",
                "[developer]",
                self.developer_instructions,
                "[/developer]",
                "[untrusted-model-summary]",
                summary,
                "[/untrusted-model-summary]",
                projection.render(),
                f"[resolved-tool-pairs]{len(pairs)}[/resolved-tool-pairs]",
            )
        )


def rebuild_after_restart(
    *,
    summary: str,
    projection: AuthoritativeProjection,
    tail_events: tuple[str, ...],
) -> str:
    """Deterministic restart reconstruction: projection never derives from text."""

    return "\n".join(
        (
            "[restart-rebuild]",
            "[untrusted-model-summary]",
            summary,
            "[/untrusted-model-summary]",
            projection.render(),
            f"[tail-events]{','.join(tail_events)}[/tail-events]",
            "[/restart-rebuild]",
        )
    )
