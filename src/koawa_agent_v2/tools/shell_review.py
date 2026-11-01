"""Issue #9: a provider-model review gate for shell commands.

This is a HEURISTIC second gate, never a security barrier on its own:

* the reviewer is not reproducible;
* a sufficiently benign-looking command can pass it;
* the reviewer itself receives untrusted text and could be injected.

Therefore it runs only AFTER the deterministic allowlist/path gate (a command
that fails the deterministic gate never reaches the model), and every
uncertain outcome is a DENY: timeout, empty answer, malformed JSON, low
confidence, provider error.

Only the closed ``reason_code`` is ever persisted; the reviewer's free text
may carry injected content and must not enter events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

REVIEW_ALLOW = "allow"
REVIEW_DENY = "deny"

_REASON_CODES = frozenset(
    {
        "allowed",
        "dangerous_side_effect",
        "destructive_command",
        "credential_access",
        "network_access",
        "outside_workspace",
        "injected_instruction",
        "policy_violation",
        "unavailable",
        "invalid_response",
        "low_confidence",
    }
)


@dataclass(frozen=True, slots=True)
class ShellReviewVerdict:
    verdict: str
    reason_code: str
    confidence: float

    def __post_init__(self) -> None:
        if self.verdict not in (REVIEW_ALLOW, REVIEW_DENY):
            raise ShellReviewUnavailable("shell_review_invalid_response")
        if self.reason_code not in _REASON_CODES:
            raise ShellReviewUnavailable("shell_review_invalid_response")
        if not isinstance(self.confidence, (int, float)) or isinstance(
            self.confidence, bool
        ):
            raise ShellReviewUnavailable("shell_review_invalid_response")


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    """Tunable only from trusted config; the model cannot lower it."""

    min_confidence: float = 0.6
    timeout_seconds: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.min_confidence, (int, float)) or isinstance(
            self.min_confidence, bool
        ):
            raise ShellReviewUnavailable("shell_review_invalid_policy")
        if not 0.0 <= float(self.min_confidence) <= 1.0:
            raise ShellReviewUnavailable("shell_review_invalid_policy")
        if (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= 120
        ):
            raise ShellReviewUnavailable("shell_review_invalid_policy")


class ShellReviewUnavailable(RuntimeError):
    """Content-free reviewer failure carrying a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ShellReviewer(Protocol):
    def review(self, argv: tuple[str, ...], *, mode: str) -> ShellReviewVerdict: ...


class UnavailableShellReviewer:
    """Configured-but-not-wired reviewer: every call is denied.

    Shipping a gate that silently allows would be worse than shipping one
    that visibly refuses; the receipt names the reason so an operator can
    either wire a judge or turn the gate off.
    """

    def review(self, argv: tuple[str, ...], *, mode: str) -> ShellReviewVerdict:
        raise ShellReviewUnavailable("shell_review_unavailable")


def parse_review_response(text: str, policy: ReviewPolicy) -> ShellReviewVerdict:
    """Strict parse of the judge's answer; anything unexpected is a deny."""

    if not isinstance(text, str) or not text.strip():
        raise ShellReviewUnavailable("shell_review_invalid_response")
    try:
        document = json.loads(text)
    except (TypeError, ValueError):
        raise ShellReviewUnavailable("shell_review_invalid_response") from None
    if not isinstance(document, dict):
        raise ShellReviewUnavailable("shell_review_invalid_response")
    verdict = document.get("verdict")
    reason_code = document.get("reason_code")
    confidence = document.get("confidence")
    if (
        verdict not in (REVIEW_ALLOW, REVIEW_DENY)
        or reason_code not in _REASON_CODES
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise ShellReviewUnavailable("shell_review_invalid_response")
    parsed = ShellReviewVerdict(
        verdict=verdict, reason_code=reason_code, confidence=float(confidence),
    )
    if parsed.verdict == REVIEW_ALLOW and parsed.confidence < policy.min_confidence:
        # A hesitant allow is a deny: the gate never turns uncertainty into
        # permission.
        return ShellReviewVerdict(
            verdict=REVIEW_DENY,
            reason_code="low_confidence",
            confidence=parsed.confidence,
        )
    return parsed


class CallableShellReviewer:
    """Adapter around an injected judge ``callable(argv, mode) -> str``.

    The judge is supplied by the deployment (the provider client lives
    outside this module), which keeps the gate testable without network.
    """

    def __init__(self, judge, policy: ReviewPolicy) -> None:
        if not callable(judge):
            raise TypeError("judge must be callable")
        if not isinstance(policy, ReviewPolicy):
            raise TypeError("policy must be ReviewPolicy")
        self._judge = judge
        self._policy = policy

    def review(self, argv: tuple[str, ...], *, mode: str) -> ShellReviewVerdict:
        try:
            raw = self._judge(tuple(argv), mode)
        except Exception:
            # Timeout / provider error / transport failure: never "allow".
            raise ShellReviewUnavailable("shell_review_unavailable") from None
        return parse_review_response(raw, self._policy)


def review_or_deny(
    reviewer: ShellReviewer | None,
    argv: tuple[str, ...],
    *,
    mode: str,
    policy: ReviewPolicy,
) -> tuple[bool, dict]:
    """Run the gate and convert every outcome into (allowed, receipt_fields)."""

    if reviewer is None:
        return True, {"review": "disabled"}
    try:
        verdict = reviewer.review(argv, mode=mode)
    except ShellReviewUnavailable as error:
        return False, {
            "review": "deny",
            "review_reason": error.code,
            "review_confidence": 0.0,
        }
    allowed = (
        verdict.verdict == REVIEW_ALLOW
        and verdict.confidence >= policy.min_confidence
    )
    return allowed, {
        "review": "allow" if allowed else "deny",
        "review_reason": verdict.reason_code,
        "review_confidence": verdict.confidence,
    }
