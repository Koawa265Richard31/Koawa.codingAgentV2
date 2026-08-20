"""D14 deterministic failure injection across named failure points."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ..agents.graph import AgentError


FAILURE_POINTS = frozenset(
    {
        "bad_sse",
        "terminal_after_event",
        "checkpoint_write",
        "ledger_claim",
        "db_version_conflict",
        "mcp_eof",
        "mcp_timeout",
        "container_kill",
        "approval_loss",
        "approval_tamper",
        "subagent_orphan",
        "worktree_conflict",
    }
)


@dataclass(frozen=True, slots=True)
class FaultInjector:
    seed: str
    script: tuple[str, ...] = ()
    triggered: list[str] = field(default_factory=list, compare=False)

    def should_fail(self, point: str) -> bool:
        if point not in FAILURE_POINTS:
            raise AgentError("unknown_failure_point")
        if self.script:
            enabled = point in self.script
        else:
            digest = hashlib.sha256(
                f"{self.seed}:{point}".encode("utf-8")
            ).hexdigest()
            enabled = int(digest[:2], 16) % 5 == 0
        if enabled:
            self.triggered.append(point)
        return enabled

    def to_document(self) -> dict:
        return {
            "seed": self.seed,
            "script": list(self.script),
            "triggered": list(self.triggered),
        }


def classify_failure(code: str) -> str:
    """Map stable error codes to a small failure taxonomy."""

    if "timeout" in code or "slow" in code:
        return "timeout"
    if "conflict" in code or "version" in code or "drift" in code:
        return "concurrency"
    if "denied" in code or "forbidden" in code or "required" in code:
        return "policy"
    if "unknown" in code or "orphan" in code or "crash" in code:
        return "uncertainty"
    if "malformed" in code or "invalid" in code:
        return "contract"
    return "other"
