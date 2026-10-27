"""A3 acceptance (WP-C): candidate admission identity + invalidation rules.

- every bound component (code/tests/deps/inputs/environment/profile version)
  participates in the candidate id, so any change yields a new identity;
- an approval covers exactly one candidate; a changed candidate needs review;
- one turn may try at most ``max_candidate_changes`` distinct candidates.
"""
from __future__ import annotations

import unittest

from koawa_agent_v2.verification.admission import (
    ADMISSION_APPROVED,
    ADMISSION_REVIEW_REQUIRED,
    AdmissionError,
    AdmissionPolicy,
    AdmissionTracker,
    CandidateIdentity,
    candidate_identity_from_parts,
)

BASE = dict(
    code_digests=("a" * 64,),
    test_digests=("b" * 64,),
    dependency_digest="c" * 64,
    input_digest="d" * 64,
    environment_digest="e" * 64,
    profile_id="unit",
    profile_version="v1",
)


class CandidateIdentityTest(unittest.TestCase):
    def test_every_component_changes_the_identity(self) -> None:
        baseline = candidate_identity_from_parts(**BASE)
        others = []
        for field, value in (
            ("code_digests", ("f" * 64,)),
            ("test_digests", ("0" * 64,)),
            ("dependency_digest", "1" * 64),
            ("input_digest", "2" * 64),
            ("environment_digest", "3" * 64),
            ("profile_version", "v2"),
        ):
            parts = dict(BASE)
            parts[field] = value
            others.append(candidate_identity_from_parts(**parts))
        ids = {baseline.candidate_id} | {item.candidate_id for item in others}
        self.assertEqual(7, len(ids))

    def test_identity_is_stable_for_same_inputs(self) -> None:
        first = candidate_identity_from_parts(**BASE)
        second = candidate_identity_from_parts(**BASE)
        self.assertEqual(first.candidate_id, second.candidate_id)

    def test_digest_validation(self) -> None:
        with self.assertRaises(AdmissionError):
            CandidateIdentity(
                code_digest="short",
                test_digest="b" * 64,
                dependency_digest="c" * 64,
                input_digest="d" * 64,
                environment_digest="e" * 64,
                profile_id="unit",
                profile_version="v1",
            )
        with self.assertRaises(AdmissionError):
            candidate_identity_from_parts(**{**BASE, "code_digests": ()})


class AdmissionPolicyTest(unittest.TestCase):
    def test_unapproved_candidate_requires_review(self) -> None:
        identity = candidate_identity_from_parts(**BASE)
        tracker = AdmissionTracker(AdmissionPolicy(require_review=True))
        self.assertEqual(
            ADMISSION_REVIEW_REQUIRED, tracker.evaluate(identity)
        )

    def test_approved_candidate_runs(self) -> None:
        identity = candidate_identity_from_parts(**BASE)
        tracker = AdmissionTracker(
            AdmissionPolicy(
                approved_candidate_ids=(identity.candidate_id,),
                require_review=True,
            )
        )
        self.assertEqual(ADMISSION_APPROVED, tracker.evaluate(identity))

    def test_changed_candidate_invalidates_approval(self) -> None:
        original = candidate_identity_from_parts(**BASE)
        tracker = AdmissionTracker(
            AdmissionPolicy(approved_candidate_ids=(original.candidate_id,))
        )
        self.assertEqual(ADMISSION_APPROVED, tracker.evaluate(original))
        changed = candidate_identity_from_parts(
            **{**BASE, "code_digests": ("f" * 64,)}
        )
        # A new candidate: the old approval does not apply.
        self.assertEqual(
            ADMISSION_REVIEW_REQUIRED, tracker.evaluate(changed)
        )

    def test_candidate_change_budget(self) -> None:
        tracker = AdmissionTracker(
            AdmissionPolicy(require_review=False, max_candidate_changes=2)
        )
        for index in range(2):
            identity = candidate_identity_from_parts(
                **{**BASE, "code_digests": (f"{index}" * 64,)}
            )
            self.assertEqual(ADMISSION_APPROVED, tracker.evaluate(identity))
        third = candidate_identity_from_parts(
            **{**BASE, "code_digests": ("9" * 64,)}
        )
        # Exhausting the allowance is an explicit stop-for-review state.
        self.assertEqual(
            ADMISSION_REVIEW_REQUIRED, tracker.evaluate(third)
        )

    def test_repeated_same_candidate_does_not_consume_budget(self) -> None:
        tracker = AdmissionTracker(
            AdmissionPolicy(require_review=False, max_candidate_changes=1)
        )
        identity = candidate_identity_from_parts(**BASE)
        for _ in range(5):
            self.assertEqual(ADMISSION_APPROVED, tracker.evaluate(identity))


if __name__ == "__main__":
    unittest.main()
