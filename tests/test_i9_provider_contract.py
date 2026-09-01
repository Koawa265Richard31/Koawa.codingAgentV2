"""Offline provider evidence shape contract (never a provider lane target)."""
from __future__ import annotations

import hashlib
import json
import unittest


class ProviderEvidenceContractTest(unittest.TestCase):
    def test_contract_requires_scope_digest_and_explicit_cleanup(self):
        scope = "provider-scope-reference"
        evidence = {
            "provider": "contract-fixture",
            "model": "offline-contract",
            "credential_scope_digest": hashlib.sha256(scope.encode("utf-8")).hexdigest(),
            "execution_mode": "contract-only",
            "real_provider_executed": False,
            "resource_cleanup": {
                "before": {"non_daemon_threads": 1, "active_children": 0, "handles_or_fds": 4},
                "after": {"non_daemon_threads": 1, "active_children": 0, "handles_or_fds": 4},
                "delta": {"non_daemon_threads": 0, "active_children": 0, "handles_or_fds": 0},
                "zero_delta": True,
            },
        }
        encoded = json.dumps(evidence, sort_keys=True)
        self.assertNotIn(scope, encoded)
        self.assertNotIn("OPENAI_API_KEY", encoded)
        self.assertRegex(evidence["credential_scope_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual({"before", "after", "delta", "zero_delta"}, set(evidence["resource_cleanup"]))
        self.assertTrue(evidence["resource_cleanup"]["zero_delta"])
        self.assertFalse(evidence["real_provider_executed"])


if __name__ == "__main__":
    unittest.main()
