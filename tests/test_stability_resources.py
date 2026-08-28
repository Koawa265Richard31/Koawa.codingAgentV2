from __future__ import annotations

import unittest

from scripts.stability_resources import assess_soak, least_squares_slope, snapshot


def stable_day():
    return [{"elapsed_seconds": second, "rss_bytes": 100 * 1024**2, "threads": 4,
             "handles_or_fds": 100, "db_bytes": second * 100, "wal_bytes": 0}
            for second in range(0, 86401, 60)]


class StabilityResourceTest(unittest.TestCase):
    def test_os_snapshot_uses_actual_resident_memory_threads_and_handles(self):
        value = snapshot(0)
        self.assertGreater(value["rss_bytes"], 1024)
        self.assertGreaterEqual(value["threads"], 1)
        self.assertGreaterEqual(value["handles_or_fds"], 1)

    def test_centered_regression_reports_per_hour_growth(self):
        self.assertEqual(2, least_squares_slope([0, 1, 2], [5, 7, 9]))
        self.assertEqual(-2, least_squares_slope([0, 1, 2], [9, 7, 5]))
        with self.assertRaises(ValueError):
            least_squares_slope([0, 0], [1, 2])

    def test_short_collection_cannot_pass_even_with_reference_flag(self):
        result = assess_soak(stable_day()[:10], reference_qualified=True)
        self.assertFalse(result["protocol_complete"])
        self.assertFalse(result["threshold_enforced"])
        self.assertIsNone(result["passed"])

    def test_day_requires_reference_before_enforcing_thresholds(self):
        result = assess_soak(stable_day())
        self.assertTrue(result["protocol_complete"])
        self.assertTrue(result["observed_within_limits"])
        self.assertFalse(result["threshold_enforced"])
        self.assertIsNone(result["passed"])
        result = assess_soak(stable_day(), reference_qualified=True)
        self.assertTrue(result["passed"])
        self.assertEqual(0, result["metrics"]["rss_bytes"]["slope_per_hour"])

    def test_rss_slope_detected_even_below_absolute_limit(self):
        samples = stable_day()
        for sample in samples:
            sample["rss_bytes"] += int(sample["elapsed_seconds"] / 3600 * 2 * 1024**2)
        result = assess_soak(samples, reference_qualified=True)
        self.assertFalse(result["passed"])
        self.assertGreater(result["metrics"]["rss_bytes"]["slope_per_hour"], 1)
        self.assertLess(result["metrics"]["rss_bytes"]["absolute_delta"], 64)

    def test_absolute_growth_and_handle_slope_have_independent_checks(self):
        for field, extra in (("rss_bytes", 65 * 1024**2), ("threads", 3), ("handles_or_fds", 9)):
            samples = stable_day()
            samples[-1][field] += extra
            with self.subTest(field=field):
                self.assertFalse(assess_soak(samples, reference_qualified=True)["passed"])
        samples = stable_day()
        for sample in samples:
            sample["handles_or_fds"] += int(sample["elapsed_seconds"] / 3600 / 4)
        result = assess_soak(samples, reference_qualified=True)
        self.assertFalse(result["passed"])
        self.assertLessEqual(result["metrics"]["handles_or_fds"]["absolute_delta"], 8)
        self.assertGreater(result["metrics"]["handles_or_fds"]["slope_per_hour"], .1)

    def test_sampling_gap_cannot_be_hidden_by_good_resource_values(self):
        samples = stable_day()
        del samples[100]
        result = assess_soak(samples, reference_qualified=True)
        self.assertFalse(result["threshold_enforced"])
        self.assertIsNone(result["passed"])
        self.assertIn("resource_sampling_gap", result["qualification_reasons"])

    def test_nonfinite_duplicate_and_untrusted_fields_are_rejected(self):
        for value in (float("nan"), float("inf"), -1, True):
            sample = stable_day()[0]
            sample["rss_bytes"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                assess_soak([sample])
        sample = stable_day()[0]
        with self.assertRaises(ValueError):
            assess_soak([sample, sample])
        with self.assertRaises(ValueError):
            assess_soak([{**sample, "environment": {}}])

    def test_dense_adaptive_and_drifting_sampling_cannot_pass(self):
        template = stable_day()[0]
        dense = [{**template, "elapsed_seconds": second} for second in range(0, 86401, 30)]
        adaptive = stable_day()
        adaptive.insert(101, {**adaptive[100], "elapsed_seconds": adaptive[100]["elapsed_seconds"] + 1})
        drifting = [{**sample, "elapsed_seconds": sample["elapsed_seconds"] + index * .01}
                    for index, sample in enumerate(stable_day())]
        for samples in (dense, adaptive, drifting):
            with self.subTest(count=len(samples)):
                result = assess_soak(samples, reference_qualified=True)
                self.assertFalse(result["protocol_complete"])
                self.assertIsNone(result["passed"])
                self.assertIn("resource_sampling_cadence", result["qualification_reasons"])

    def test_scheduled_jitter_and_single_final_endpoint_are_allowed(self):
        samples = [{**sample, "elapsed_seconds": sample["elapsed_seconds"] + .2}
                   for sample in stable_day()]
        samples.append({**samples[-1], "elapsed_seconds": 86400.5})
        self.assertTrue(assess_soak(samples, reference_qualified=True)["passed"])
        samples.append({**samples[-1], "elapsed_seconds": 86400.8})
        self.assertFalse(assess_soak(samples, reference_qualified=True)["protocol_complete"])


if __name__ == "__main__":
    unittest.main()
