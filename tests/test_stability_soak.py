from __future__ import annotations

import threading
import unittest

from scripts.stability_soak import run_soak


class StabilitySoakTest(unittest.TestCase):
    def test_short_real_workload_releases_resources_and_cannot_claim_24h(self):
        before = {thread.ident for thread in threading.enumerate()}
        result = run_soak(duration_seconds=.5, sample_interval=.1, quick=True)
        self.assertGreater(result["operation_counts"]["agent_tasks"], 0)
        self.assertTrue(result["workload_complete"])
        self.assertTrue(all(value > 0 for value in result["operation_counts"].values()))
        self.assertEqual(0, result["final_resources"]["capacity"])
        self.assertEqual(0, result["final_resources"]["budget"])
        self.assertEqual(result["children"], result["queued_parent_results"])
        self.assertGreaterEqual(len(result["samples"]), 2)
        self.assertFalse(result["threshold_enforced"])
        self.assertIsNone(result["passed"])
        leaked = [thread.name for thread in threading.enumerate()
                  if thread.ident not in before and thread.name.startswith("stability-")]
        self.assertEqual([], leaked)

    def test_shape_validation_precedes_workload(self):
        for duration, interval in ((0, 1), (86401, 1), (1, 0), (1, 61), (True, 1), (float("nan"), 1), (1, float("inf"))):
            with self.subTest(duration=duration, interval=interval), self.assertRaises(ValueError):
                run_soak(duration_seconds=duration, sample_interval=interval)


if __name__ == "__main__":
    unittest.main()
