"""R3 unit coverage for the legacy field-rule helpers (output_policy).

The registry-path contract behavior (§2 nine-field loading, §4 receipt
semantics, four release modes) is covered by tests/test_release_contracts.py
per contracts.md; this module keeps only the pure helper units that remain
in output_policy for backward compatibility.
"""
from __future__ import annotations

import unittest

from koawa_agent_v2.verification.output_policy import (
    RESULT_WITHHELD,
    HUMAN_REQUIRED,
    ReleaseRule,
    apply_release_rule,
    withheld_receipt,
)


class ReleaseRuleUnitTest(unittest.TestCase):
    def test_contract_validation(self) -> None:
        with self.assertRaises(ValueError):
            ReleaseRule(fields=("stdout",))  # bodies are never releaseable
        with self.assertRaises(ValueError):
            ReleaseRule(fields=())  # empty is not a contract
        with self.assertRaises(ValueError):
            ReleaseRule(fields=("exit_code", "exit_code"))  # duplicates

    def test_apply_release_rule_keeps_policy_markers(self) -> None:
        payload = apply_release_rule(
            {
                "profile_id": "p",
                "test_output_policy": "v",
                "exit_code": 0,
                "argv": ["x"],
            },
            ReleaseRule(fields=("exit_code",)),
        )
        self.assertEqual(
            {"profile_id", "test_output_policy", "exit_code"}, set(payload)
        )

    def test_withheld_receipt_is_the_fixed_shape(self) -> None:
        receipt = withheld_receipt("p")
        self.assertEqual(
            {
                "profile_id",
                "test_output_policy",
                "test_output_visibility",
                "availability",
                "human_required",
            },
            set(receipt),
        )
        self.assertEqual(RESULT_WITHHELD, receipt["availability"])
        self.assertIs(True, receipt[HUMAN_REQUIRED])


if __name__ == "__main__":
    unittest.main()
