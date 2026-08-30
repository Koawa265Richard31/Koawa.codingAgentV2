"""D23-B tests: MemoryConfig defaults, mapping round-trip, and hard limits.

Covers docs/day-23-memory-layer-upgrade.md §8: strict bool/int typing,
positive bounds (chars-class ceiling 2_000_000, other ints 1_000_000),
relationship invariants, fail-closed unknown keys, and stable content-free
error codes.
"""

from __future__ import annotations

import json
import unittest

from koawa_agent_v2.runtime.memory import MemoryConfig, MemoryConfigError

BOOL_FIELDS = (
    "conclusions_enabled",
    "conclusion_model_summary",
    "in_run_compaction_enabled",
    "journal_inject_latest",
)

# A fully explicit, valid document whose every value differs from the default.
FULL_DOCUMENT = {
    "conclusions_enabled": False,
    "conclusion_max_chars": 1_024,
    "conclusion_recent_limit": 12,
    "conclusion_model_summary": True,
    "failed_echo_max_turns": 5,
    "in_run_compaction_enabled": False,
    "in_run_keep_groups": 6,
    "request_context_soft_chars": 50_000,
    "request_context_hard_chars": 80_000,
    "request_context_reserve_chars": 12_000,
    "compaction_target_chars": 40_000,
    "max_compaction_epochs_per_run": 20,
    "max_compaction_source_groups": 40,
    "compaction_summary_max_chars": 4_096,
    "recall_scan_max_turns": 512,
    "journal_remind_turns": 15,
    "journal_remind_changed_files": 30,
    "journal_inject_latest": True,
}

STABLE_CODES = {
    "invalid_memory_value",
    "invalid_memory_relation",
    "memory_unknown_field",
}


class MemoryConfigTest(unittest.TestCase):
    def test_defaults(self):
        config = MemoryConfig()
        self.assertTrue(config.conclusions_enabled)
        self.assertEqual(config.conclusion_max_chars, 512)
        self.assertEqual(config.conclusion_recent_limit, 8)
        self.assertFalse(config.conclusion_model_summary)
        self.assertEqual(config.failed_echo_max_turns, 3)
        self.assertTrue(config.in_run_compaction_enabled)
        self.assertEqual(config.in_run_keep_groups, 4)
        self.assertEqual(config.request_context_soft_chars, 48_000)
        self.assertEqual(config.request_context_hard_chars, 64_000)
        self.assertEqual(config.request_context_reserve_chars, 8_000)
        self.assertEqual(config.compaction_target_chars, 36_000)
        self.assertEqual(config.max_compaction_epochs_per_run, 16)
        self.assertEqual(config.max_compaction_source_groups, 32)
        self.assertEqual(config.compaction_summary_max_chars, 2_048)
        self.assertEqual(config.recall_scan_max_turns, 256)
        self.assertEqual(config.journal_remind_turns, 10)
        self.assertEqual(config.journal_remind_changed_files, 20)
        self.assertFalse(config.journal_inject_latest)

    def test_from_mapping_full_explicit_round_trip(self):
        config = MemoryConfig.from_mapping(FULL_DOCUMENT)
        self.assertFalse(config.conclusions_enabled)
        self.assertEqual(config.conclusion_max_chars, 1_024)
        self.assertTrue(config.conclusion_model_summary)
        document = config.to_document()
        # All keys present with JSON-serializable values.
        self.assertEqual(set(document), set(FULL_DOCUMENT))
        self.assertEqual(document, FULL_DOCUMENT)
        json.dumps(document)  # must not raise
        # Round-trip: to_document -> from_mapping yields an equal config.
        self.assertEqual(MemoryConfig.from_mapping(document), config)

    def test_empty_and_none_return_defaults(self):
        default = MemoryConfig()
        self.assertEqual(MemoryConfig.from_mapping({}), default)
        self.assertEqual(MemoryConfig.from_mapping(None), default)

    def test_unknown_key_fails_closed(self):
        with self.assertRaises(MemoryConfigError) as caught:
            MemoryConfig.from_mapping({"conclusions_enabled": True, "bogus": 1})
        self.assertEqual(caught.exception.code, "memory_unknown_field")

    def test_every_bool_field_rejects_int(self):
        for field in BOOL_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(MemoryConfigError) as caught:
                    MemoryConfig.from_mapping({field: 1})
                self.assertEqual(caught.exception.code, "invalid_memory_value")

    def test_int_fields_reject_bool(self):
        for field in ("conclusion_max_chars", "in_run_keep_groups"):
            with self.subTest(field=field):
                with self.assertRaises(MemoryConfigError) as caught:
                    MemoryConfig.from_mapping({field: True})
                self.assertEqual(caught.exception.code, "invalid_memory_value")

    def test_relation_target_gte_soft_rejected(self):
        with self.assertRaises(MemoryConfigError) as caught:
            MemoryConfig.from_mapping({"compaction_target_chars": 60_000})
        self.assertEqual(caught.exception.code, "invalid_memory_relation")

    def test_relation_soft_gte_hard_rejected(self):
        with self.assertRaises(MemoryConfigError) as caught:
            MemoryConfig.from_mapping({"request_context_soft_chars": 64_000})
        self.assertEqual(caught.exception.code, "invalid_memory_relation")

    def test_relation_reserve_over_limit_rejected(self):
        # hard - target = 28_000 by default; reserve must stay below it.
        with self.assertRaises(MemoryConfigError) as caught:
            MemoryConfig.from_mapping({"request_context_reserve_chars": 30_000})
        self.assertEqual(caught.exception.code, "invalid_memory_relation")

    def test_relation_conclusion_over_hard_rejected(self):
        with self.assertRaises(MemoryConfigError) as caught:
            MemoryConfig.from_mapping({"conclusion_max_chars": 100_000})
        self.assertEqual(caught.exception.code, "invalid_memory_relation")

    def test_relation_summary_over_hard_rejected(self):
        with self.assertRaises(MemoryConfigError) as caught:
            MemoryConfig.from_mapping({"compaction_summary_max_chars": 100_000})
        self.assertEqual(caught.exception.code, "invalid_memory_relation")

    def test_lower_bound_keep_groups_zero_rejected(self):
        with self.assertRaises(MemoryConfigError) as caught:
            MemoryConfig.from_mapping({"in_run_keep_groups": 0})
        self.assertEqual(caught.exception.code, "invalid_memory_value")

    def test_lower_bound_failed_echo_zero_rejected(self):
        with self.assertRaises(MemoryConfigError) as caught:
            MemoryConfig.from_mapping({"failed_echo_max_turns": 0})
        self.assertEqual(caught.exception.code, "invalid_memory_value")

    def test_chars_ceiling_enforced(self):
        with self.assertRaises(MemoryConfigError) as caught:
            MemoryConfig.from_mapping({"request_context_hard_chars": 2_000_001})
        self.assertEqual(caught.exception.code, "invalid_memory_value")

    def test_int_ceiling_enforced(self):
        with self.assertRaises(MemoryConfigError) as caught:
            MemoryConfig.from_mapping({"recall_scan_max_turns": 1_000_001})
        self.assertEqual(caught.exception.code, "invalid_memory_value")

    def test_error_codes_are_stable_and_content_free(self):
        cases = (
            ({"conclusion_max_chars": 2_000_001}, "invalid_memory_value"),
            ({"in_run_keep_groups": -5}, "invalid_memory_value"),
            ({"compaction_target_chars": 99_000}, "invalid_memory_relation"),
            ({"totally_unknown": 123}, "memory_unknown_field"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(MemoryConfigError) as caught:
                    MemoryConfig.from_mapping(raw)
                code = caught.exception.code
                self.assertEqual(code, expected)
                self.assertIn(code, STABLE_CODES)
                # No input value content may leak into the code or message.
                for value in raw.values():
                    self.assertNotIn(str(value), code)
                    self.assertNotIn(str(value), str(caught.exception))

    def test_error_code_must_match_stable_pattern(self):
        with self.assertRaises(ValueError):
            MemoryConfigError("Bad Code!")
        with self.assertRaises(ValueError):
            MemoryConfigError("1starts_with_digit")
        self.assertEqual(MemoryConfigError("invalid_memory_value").code, "invalid_memory_value")

    def test_repr_is_concise_and_shows_changes(self):
        self.assertEqual(repr(MemoryConfig()), "MemoryConfig(<default>)")
        changed = MemoryConfig.from_mapping({"conclusion_max_chars": 1_024})
        self.assertIn("conclusion_max_chars=1024", repr(changed))
        self.assertNotIn("conclusion_recent_limit", repr(changed))


if __name__ == "__main__":
    unittest.main()
