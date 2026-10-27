"""A3 (WP-C): candidate admission identity and invalidation.

A candidate binds code, tests, dependencies, frozen inputs and the execution
environment.  Any bound content change produces a NEW candidate identity, so
an earlier approval never carries over (design: "批准后任何绑定项变化使原
批准不适用"), and the cumulative disclosure budget is per candidate+turn, not
resettable by a fresh turn or a resume.

This module is the trusted computation: callers supply the already-measured
components (repo digests, test digests, dependency identity, input snapshot
digest, environment digest) and receive a stable, opaque candidate id plus a
verdict against the admission policy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

ADMISSION_APPROVED = "approved"
ADMISSION_REVIEW_REQUIRED = "local_review_required"
ADMISSION_REFUSED = "admission_refused"


class AdmissionError(RuntimeError):
    """Content-free candidate-admission failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    """The bound facts of one candidate; every field participates."""

    code_digest: str
    test_digest: str
    dependency_digest: str
    input_digest: str
    environment_digest: str
    profile_id: str
    profile_version: str

    def __post_init__(self) -> None:
        for name in (
            "code_digest",
            "test_digest",
            "dependency_digest",
            "input_digest",
            "environment_digest",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise AdmissionError("invalid_candidate_digest")
        for name in ("profile_id", "profile_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise AdmissionError("invalid_candidate_profile")

    @property
    def document(self) -> dict:
        return {
            "code_digest": self.code_digest,
            "test_digest": self.test_digest,
            "dependency_digest": self.dependency_digest,
            "input_digest": self.input_digest,
            "environment_digest": self.environment_digest,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
        }

    @property
    def candidate_id(self) -> str:
        """Opaque, stable identity; any bound change yields a new id."""

        return hashlib.sha256(
            json.dumps(
                self.document, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    """Which candidates may run without local review.

    ``approved_candidate_ids`` is the maintainer's explicit set; a candidate
    not listed needs local review when ``require_review`` is true (the
    default for sensitive validation).  ``max_candidate_changes`` bounds how
    many distinct candidates one turn may try before the task stops for
    review - fixed scripts alone cannot prove safety, so the allowance is
    explicit and small.
    """

    approved_candidate_ids: tuple[str, ...] = ()
    require_review: bool = True
    max_candidate_changes: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.approved_candidate_ids, tuple) or any(
            not isinstance(item, str) or len(item) != 64
            for item in self.approved_candidate_ids
        ):
            raise AdmissionError("invalid_approved_candidates")
        if not isinstance(self.require_review, bool):
            raise AdmissionError("invalid_admission_review_flag")
        if (
            not isinstance(self.max_candidate_changes, int)
            or isinstance(self.max_candidate_changes, bool)
            or self.max_candidate_changes < 1
        ):
            raise AdmissionError("invalid_max_candidate_changes")


@dataclass(slots=True)
class AdmissionTracker:
    """Per-turn admission state: distinct candidates tried and their verdict.

    Disclosure budget is intentionally NOT stored here - it belongs to the
    task-level counter so a new turn or a resume cannot reset it.
    """

    policy: AdmissionPolicy
    seen: list[str] | None = None

    def __post_init__(self) -> None:
        if self.seen is None:
            self.seen = []

    def evaluate(self, identity: CandidateIdentity) -> str:
        """APPROVED / REVIEW_REQUIRED for this candidate.

        Exhausting the per-turn candidate-change allowance is itself a
        "stop for local review" state (the design's fixed requires-new-
        authorization status), never a silent continue and never a crash.
        """

        candidate = identity.candidate_id
        if candidate not in self.seen:
            if len(self.seen) >= self.policy.max_candidate_changes:
                return ADMISSION_REVIEW_REQUIRED
            self.seen.append(candidate)
        if candidate in self.policy.approved_candidate_ids:
            return ADMISSION_APPROVED
        if self.policy.require_review:
            return ADMISSION_REVIEW_REQUIRED
        return ADMISSION_APPROVED


def candidate_identity_from_parts(
    *,
    code_digests: tuple[str, ...],
    test_digests: tuple[str, ...],
    dependency_digest: str,
    input_digest: str,
    environment_digest: str,
    profile_id: str,
    profile_version: str,
) -> CandidateIdentity:
    """Fold ordered component digests into one candidate identity."""

    def fold(items: tuple[str, ...]) -> str:
        if not items:
            raise AdmissionError("candidate_component_missing")
        return hashlib.sha256(
            "\n".join(items).encode("utf-8")
        ).hexdigest()

    return CandidateIdentity(
        code_digest=fold(code_digests),
        test_digest=fold(test_digests),
        dependency_digest=dependency_digest,
        input_digest=input_digest,
        environment_digest=environment_digest,
        profile_id=profile_id,
        profile_version=profile_version,
    )
