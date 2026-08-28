"""Real process contention, ownership fencing, and bounded MCP pending load."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.stability_load import concurrent_spawn, heartbeat_takeover, mcp_pending_storm
from scripts.stability_scenarios import store_at


class StabilityLoadTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_os_process_barrier_obeys_parent_capacity_and_root_budget(self):
        for capacity, budget in ((2, 3), (3, 1)):
            with self.subTest(capacity=capacity, budget=budget):
                path = self.root / f"spawn-{capacity}-{budget}.db"
                report = concurrent_spawn(path, workers=4, capacity=capacity, budget=budget)
                self.assertEqual(4, report["ready_before_release"])
                self.assertEqual(4, len({item["pid"] for item in report["worker_reports"]}))
                self.assertLessEqual(report["created"], min(capacity, budget))
                self.assertEqual(report["created"], report["peak_resources"]["budget"])
                self.assertEqual(4 - report["created"], sum(report["rejections"].values()))
                self.assertEqual(0, report["final_resources"]["capacity"])
                self.assertEqual(0, report["final_resources"]["budget"])
                events = store_at(path).read_all()
                child_spawns = [event for event in events if event.event_type == "agent.spawned.v2"
                                and event.payload["parent_agent_id"] is not None]
                self.assertEqual(report["created"], len(child_spawns))
                for spawn in child_spawns:
                    batch = [event for event in events if event.commit_id == spawn.commit_id]
                    self.assertEqual(4, len(batch))
                    self.assertEqual(4, len({event.stream_id for event in batch}))

    def test_takeover_cycles_fence_every_old_owner_without_resource_drift(self):
        first = heartbeat_takeover(self.root / "a.db", cycles=4)
        second = heartbeat_takeover(self.root / "b.db", cycles=4)
        self.assertEqual(4, first["stale_owners_fenced"])
        self.assertEqual(5, first["attempts"])
        self.assertEqual(4, len(first["cycle_raw_ns"]))
        self.assertEqual(first["seed_digest"], second["seed_digest"])
        # Production run UUIDs are random: compare the measured state/fence
        # trajectory, not opaque identity-dependent terminal digest bytes.
        self.assertEqual(first["transitions"], second["transitions"])
        self.assertEqual(0, first["final_resources"]["active_children"])

    def test_hundred_mcp_requests_are_simultaneously_pending_during_storm(self):
        report = mcp_pending_storm(pending=100, notifications=2048)
        self.assertEqual(100, report["peak_pending"])
        self.assertEqual(100, report["sent"])
        self.assertEqual(100, report["completed"])
        self.assertEqual(1, report["overflow_rejected"])
        self.assertEqual(0, report["pending_after_close"])
        self.assertEqual(0, report["unknown_responses"])
        self.assertTrue(report["session_closed"])
        self.assertGreater(report["notifications_throttled"], 0)

    def test_invalid_load_shapes_fail_before_creating_state(self):
        for workers in (True, 0, 101):
            with self.assertRaises(ValueError):
                concurrent_spawn(self.root / "not-created.db", workers=workers, capacity=1, budget=1)
        self.assertFalse((self.root / "not-created.db").exists())
        with self.assertRaises(ValueError):
            heartbeat_takeover(self.root / "not-created.db", cycles=0)
        with self.assertRaises(ValueError):
            mcp_pending_storm(pending=101, notifications=128)


if __name__ == "__main__":
    unittest.main()
