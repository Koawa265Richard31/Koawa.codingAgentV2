"""I8 measurement contracts: production semantics, not synthetic SQL timings."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from scripts import stability_benchmark as benchmark
from scripts import stability_scenarios as scenarios
from koawa_agent_v2.control.event_store import StreamId


ROOT = Path(__file__).resolve().parents[1]


class CapacityStatisticsTest(unittest.TestCase):
    def test_nearest_rank_not_interpolated(self):
        self.assertEqual(19, benchmark.nearest_rank(list(range(1, 21)), .95))
        self.assertEqual(29, benchmark.nearest_rank(list(range(1, 31)), .95))
        self.assertEqual(1, benchmark.nearest_rank([1], .95))
        self.assertEqual(2, benchmark.nearest_rank([4, 1, 3, 2], .5))

    def test_invalid_statistics_rejected(self):
        for values in ([], [float("nan")], [float("inf")], [-1]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                benchmark.nearest_rank(values, .95)
        for percentile in (0, -1, 1.1, float("nan")):
            with self.subTest(percentile=percentile), self.assertRaises(ValueError):
                benchmark.nearest_rank([1], percentile)

    def test_raw_samples_can_reproduce_summary(self):
        values = [index * 1_000_000 for index in range(30)]
        summary = benchmark.summary(values)
        self.assertEqual(values, summary["raw_ns"])
        self.assertEqual(28.0, summary["p95_ms"])
        self.assertEqual(14.0, summary["p50_ms"])

    def test_hash_seed_must_be_explicit_before_collection(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            benchmark.run(ROOT, quick=True, reference_digest=None)

    def test_environment_digest_excludes_live_load_observations(self):
        with patch.object(benchmark, "_cpu_times", side_effect=[(0, 100), (10, 200), (0, 100), (80, 200)]):
            first = benchmark._environment(0)
            second = benchmark._environment(0)
        self.assertEqual(.1, first["background_system_cpu_ratio"])
        self.assertEqual(.8, second["background_system_cpu_ratio"])
        self.assertEqual(first["environment_digest"], second["environment_digest"])


class CapacityProductionDatasetsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_execution_dataset_replays_real_model_and_tool_events(self):
        path = self.root / "execution.db"
        manifest = scenarios.seed_execution(path, 10)
        store = scenarios.store_at(path)
        events = scenarios.read_events(store, StreamId("run-execution", UUID(manifest["turn_id"])))
        self.assertEqual(10, len(events))
        self.assertEqual({"run.context-seeded.v2", "model.turn-completed.v1", "run.phase-advanced.v1", "tool.result-recorded.v1"}, {event.event_type for event in events})
        scenarios.operation("verified_event_rebuild_10k", path, manifest)()
        # Cache deletion is a projection loss, not truth loss.
        with closing(store._connect()) as connection:
            connection.execute("DELETE FROM checkpoint_cache")
        scenarios.operation("verified_event_rebuild_10k", path, manifest)()

    def test_same_seed_has_same_logical_dataset_and_projection_digest(self):
        first = scenarios.seed_execution(self.root / "first.db", 10)
        second = scenarios.seed_execution(self.root / "second.db", 10)
        self.assertEqual(first, second)
        self.assertEqual(scenarios.seed_agents(self.root / "a.db", 2, 2),
                         scenarios.seed_agents(self.root / "b.db", 2, 2))

    def test_mailbox_and_wait_use_real_agents(self):
        path = self.root / "agents.db"
        manifest = scenarios.seed_agents(path, 3, 2)
        for name in ("mailbox_next", "mailbox_list", "agent_list", "wait_agents_100"):
            scenarios.operation(name, path, manifest)()
        store = scenarios.store_at(path)
        self.assertEqual(6, sum(event.event_type == "message.enqueued.v1" for event in store.read_all()))

    def test_each_spawn_sample_uses_verified_seed_and_four_stream_transaction(self):
        seed = self.root / "seed.db"
        manifest = scenarios.seed_spawn(seed)
        for index in range(2):
            path = self.root / f"copy-{index}.db"
            scenarios.clone_database(seed, path)
            self.assertEqual(manifest["dataset_digest"], scenarios.event_digest(scenarios.store_at(path)))
            operation = scenarios.operation("uncontended_spawn_transaction", path, manifest)
            operation()
            store = scenarios.store_at(path)
            events = store.read_all()
            operation()
            self.assertEqual(events, store.read_all(), "semantic retry duplicated spawn")
            last = events[-1]
            batch = [event for event in events if event.commit_id == last.commit_id]
            self.assertEqual(4, len(batch))
            self.assertEqual(4, len({event.stream_id for event in batch}))
        self.assertEqual(manifest["dataset_digest"], scenarios.event_digest(scenarios.store_at(seed)))

    def test_pragmas_are_from_actual_production_connection(self):
        actual = scenarios.pragmas(scenarios.store_at(self.root / "pragmas.db"))
        self.assertEqual("wal", actual["journal_mode"])
        self.assertEqual(2, actual["synchronous"])
        self.assertEqual(1, actual["foreign_keys"])
        self.assertEqual(5000, actual["busy_timeout"])

    def test_unknown_measurement_rejected(self):
        path = self.root / "seed.db"
        manifest = scenarios.seed_spawn(path)
        with self.assertRaises(ValueError):
            scenarios.operation("made-up", path, manifest)


class CapacityCollectorTest(unittest.TestCase):
    def test_quick_subprocess_reports_raw_cold_warm_samples_without_release_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/stability_benchmark.py"), "--quick", "--report", str(path)],
                cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONHASHSEED": "0"},
                capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            raw = path.read_bytes()
            document = json.loads(raw)
            self.assertEqual(raw, benchmark.canonical_bytes(document))
            digest = document.pop("report_digest")
            self.assertEqual(digest, hashlib.sha256(benchmark.canonical_bytes(document)).hexdigest())
            self.assertEqual("stability-benchmark-v1", document["protocol_version"])
            self.assertFalse(document["threshold_enforced"])
            self.assertFalse(document["environment_qualified"])
            self.assertIsNone(document["release_pass"])
            self.assertEqual([], document["errors"])
            self.assertIn("soak_24h", document["pending_scenarios"])
            for name, result in document["results"].items():
                self.assertIsNone(result["passed"])
                if name in {"concurrent_spawn_100", "heartbeat_takeover_1000", "mcp_pending_100_notification_storm"}:
                    expected_modes = {"load"}
                else:
                    expected_modes = {"write"} if name == "uncontended_spawn_transaction" else {"cold", "warm"}
                self.assertEqual(expected_modes, set(result["modes"]))
                for mode in result["modes"].values():
                    self.assertEqual(1, len(mode["batches"]))
                    for batch in mode["batches"]:
                        self.assertEqual(benchmark.summary(batch["raw_ns"]), batch)
            self.assertEqual(["soak_24h"], document["pending_scenarios"])
            for name in ("concurrent_spawn_100", "heartbeat_takeover_1000", "mcp_pending_100_notification_storm"):
                mode = document["results"][name]["modes"]["load"]
                self.assertEqual(1, len(mode["observations_by_batch"]))
                self.assertEqual(mode["batches"][0]["raw_ns"],
                                 [item["duration_ns"] for item in mode["observations_by_batch"][0]])


if __name__ == "__main__":
    unittest.main()
