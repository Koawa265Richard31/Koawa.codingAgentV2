"""I9 release-audit truth derivation contracts."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import release_audit


class ReleaseAuditTruthTest(unittest.TestCase):
    def test_empty_durable_store_reports_unknown_trace_reason(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "empty.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE events(event_type TEXT NOT NULL)")
            connection.commit()
            connection.close()
            report = release_audit.audit_database(path)
        self.assertFalse(report["workspace_effects"]["inventory_available"])
        self.assertEqual("no_workspace_inventory_events", report["workspace_effects"]["inventory_unavailable_reason"])
        dropped = report["trace"]["dropped_diagnostics"]
        self.assertEqual("unknown", dropped["status"])
        self.assertIn("process_local", dropped["reason"])

    def test_workspace_effect_and_trace_truth_is_derived_from_streams(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "populated.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE streams(stream_id TEXT PRIMARY KEY, category TEXT, aggregate_id TEXT);
                CREATE TABLE events(global_position INTEGER PRIMARY KEY, stream_id TEXT,
                                    event_type TEXT, payload_json TEXT);
                """
            )
            events = [
                (1, "workspace-1", "workspace", "agent-1", "workspace.inventory-active.v2", {"allocation_id": "allocation-1", "state": "active"}),
                (2, "workspace-1", "workspace", "agent-1", "workspace.inventory-state.v2", {"allocation_id": "allocation-1", "state": "reaped"}),
                (3, "effect-1", "workspace-effect", "effect-1", "workspace.effect-intended.v2", {"effect_id": "effect-1"}),
                (4, "effect-1", "workspace-effect", "effect-1", "workspace.effect-applied.v2", {"effect_id": "effect-1"}),
                (5, "trace-1", "trace", "trace-1", "trace.tool.v1", {"kind": "s5.trace.drop"}),
            ]
            connection.executemany(
                "INSERT INTO streams VALUES (?, ?, ?)",
                sorted({(stream_id, category, aggregate) for _, stream_id, category, aggregate, _, _ in events}),
            )
            connection.executemany(
                "INSERT INTO events VALUES (?, ?, ?, ?)",
                [(position, stream_id, event_type, json.dumps(payload)) for position, stream_id, _, _, event_type, payload in events],
            )
            connection.commit()
            connection.close()
            report = release_audit.audit_database(path)
        workspace = report["workspace_effects"]
        self.assertTrue(workspace["inventory_available"])
        self.assertEqual(1, workspace["inventory_records"])
        self.assertEqual(0, workspace["active_inventory"])
        self.assertEqual({"reaped": 1}, workspace["inventory_states"])
        self.assertEqual({"applied": 1}, workspace["effect_states"])
        self.assertEqual(1, report["trace"]["dropped_diagnostics"]["durable_drop_events"])


if __name__ == "__main__":
    unittest.main()
