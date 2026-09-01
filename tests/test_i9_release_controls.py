"""I9 gate, audit and manifest fail-closed contracts."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from koawa_agent_v2.runtime import release_audit
from scripts import release_manifest, stability_gate


class I9GateTest(unittest.TestCase):
    def test_failure_detail_preserves_traceback_head_and_tail_with_bound(self):
        class LongFailureProbe(unittest.TestCase):
            def test_long_failure(self):
                raise RuntimeError(
                    ("x" * 2_000)
                    + " stable-error-tail Authorization: Bearer "
                    + "sk-do-not-persist-1234567890"
                )

        suite = unittest.TestSuite((LongFailureProbe("test_long_failure"),))
        with patch.object(stability_gate, "_suite_for", return_value=suite):
            document = stability_gate.run_lane("golden")
        detail = document["tests"][0]["detail"]
        self.assertEqual(stability_gate._DETAIL_LIMIT, len(detail))
        self.assertTrue(detail.startswith("Traceback (most recent call last):"))
        self.assertIn(stability_gate._DETAIL_TRUNCATION_MARKER, detail)
        # The bounded suffix retains the stable part of the innermost error
        # even when its message itself is very long.
        self.assertIn("stable-error-tail", detail)
        self.assertNotIn("sk-do-not-persist-1234567890", detail)

    def test_mandatory_skip_is_not_a_pass(self):
        class MandatorySkipProbe(unittest.TestCase):
            def test_explicit_mandatory_skip(self):
                self.skipTest("deterministic mandatory-lane probe")

        class PassingProbe(unittest.TestCase):
            def test_control_pass(self):
                pass

        suite = unittest.TestSuite((
            MandatorySkipProbe("test_explicit_mandatory_skip"),
            PassingProbe("test_control_pass"),
        ))
        # Inject a known skip so this contract does not depend on whether the
        # host currently has Docker or another platform prerequisite.
        with patch.object(stability_gate, "_suite_for", return_value=suite):
            document = stability_gate.run_lane("golden")
        self.assertEqual(2, document["discovered"])
        self.assertEqual(1, document["skipped"])
        self.assertFalse(document["ok"])
        self.assertEqual(
            document["skipped"],
            sum(item["status"] == "SKIP" for item in document["tests"]),
        )
        self.assertIn("PASS", [item["status"] for item in document["tests"]])

    def test_atomic_report_digest_excludes_self(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "report.json"
            digest = stability_gate._atomic_json(path, {"report_schema_version": 1, "ok": True})
            document = json.loads(path.read_text(encoding="utf-8"))
            body = dict(document); body.pop("report_digest")
            self.assertEqual(digest, hashlib.sha256(stability_gate.canonical_bytes(body)).hexdigest())


class I9AuditTest(unittest.TestCase):
    def test_audit_is_read_only_and_reports_canary_presence_only(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE events(event_type TEXT NOT NULL)")
                connection.execute("INSERT INTO events VALUES ('message.delivered.v2')")
                connection.commit()
            finally:
                connection.close()
            before = path.read_bytes()
            report = release_audit.audit_database(path)
            self.assertTrue(report["read_only"])
            self.assertEqual(1, report["messages"]["delivered"])
            self.assertFalse(Path(report["database"]).is_absolute())
            self.assertIn("commit", report)
            self.assertIn("generated_at", report)
            self.assertIn("finished_at", report)
            self.assertEqual(before, path.read_bytes())

    def test_audit_derives_active_streams_and_closes_read_connection(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.db"
            connection = sqlite3.connect(path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE streams(stream_id TEXT PRIMARY KEY, category TEXT, aggregate_id TEXT);
                    CREATE TABLE events(global_position INTEGER PRIMARY KEY, stream_id TEXT, event_type TEXT, payload_json TEXT);
                    INSERT INTO streams VALUES ('turn-1', 'turn', '1');
                    INSERT INTO events VALUES (1, 'turn-1', 'turn.started.v1', '{}');
                    """
                )
                connection.commit()
            finally:
                connection.close()
            report = release_audit.audit_database(path)
            self.assertEqual(1, report["active_run_turn_agent"]["turns"])
            self.assertEqual(1, report["active_run_turn_agent"]["recoverable_turns"] if "recoverable_turns" in report["active_run_turn_agent"] else 0)

    def test_release_audit_cli_returns_stable_error_without_path(self):
        from koawa_agent_v2.runtime.cli import main

        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "missing-config.json"
            report = Path(raw) / "report.json"
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([
                    "koawa-agent-v2", "release-audit",
                    "--config", str(config), "--report", str(report),
                ])
            self.assertEqual(1, code)
            self.assertNotIn(str(config), output.getvalue())
            self.assertNotIn(str(report), output.getvalue())
            self.assertIn('"ok": false', output.getvalue())

    def test_release_audit_console_script_imports_outside_repo_root(self):
        # The installed console script must reach the release-audit import
        # chain without the repository root on sys.path; before the module
        # moved into the package this crashed with ModuleNotFoundError.
        import shutil
        import subprocess

        if shutil.which("koawa-agent-v2") is None:
            self.skipTest("console_script_not_on_path")
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "missing-config.json"
            report = Path(raw) / "report.json"
            completed = subprocess.run(
                [
                    "koawa-agent-v2", "release-audit",
                    "--config", str(config), "--report", str(report),
                ],
                cwd=raw,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(1, completed.returncode)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertNotIn("Traceback", completed.stdout)
        self.assertIn('"ok": false', completed.stdout)


class I9ManifestTest(unittest.TestCase):
    def test_lane_report_requires_build_identity_and_fresh_timestamps(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = {
                "commit": "a" * 40,
                "generated_at": "2026-08-30T10:00:00+00:00",
                "finished_at": "2026-08-30T10:00:01+00:00",
                "ok": True,
            }
            report_path = root / "lane.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            digest = hashlib.sha256(
                release_manifest.canonical_bytes(report)
            ).hexdigest()
            loaded = release_manifest._report(
                root,
                {"path": "lane.json", "digest": digest},
                release_commit="a" * 40,
                cutoff=release_manifest._parse_utc(
                    "2026-08-30T11:00:00+00:00", "invalid"
                ),
            )
            with self.assertRaises(release_manifest.ReleaseManifestError) as raised:
                release_manifest._validate_report_identity(
                    loaded,
                    {
                        "build_artifact_digest": "b" * 64,
                        "canonical_config_digest": "c" * 64,
                        "docker_image_digest": "sha256:" + "d" * 64,
                    },
                    require_build_identity=True,
                )
            self.assertEqual("report_identity_missing", str(raised.exception))

    def test_lane_report_future_or_expired_timestamp_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = {
                "commit": "a" * 40,
                "generated_at": "2026-08-01T10:00:00+00:00",
                "finished_at": "2026-08-01T10:00:01+00:00",
                "ok": True,
            }
            report_path = root / "lane.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            digest = hashlib.sha256(release_manifest.canonical_bytes(report)).hexdigest()
            with self.assertRaises(release_manifest.ReleaseManifestError) as raised:
                release_manifest._report(
                    root,
                    {"path": "lane.json", "digest": digest},
                    release_commit="a" * 40,
                    cutoff=release_manifest._parse_utc(
                        "2026-08-30T11:00:00+00:00", "invalid"
                    ),
                )
            self.assertEqual("report_expired", str(raised.exception))

    def test_all_pass_tests_with_false_report_ok_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = {
                "commit": "a" * 40,
                "generated_at": "2026-08-30T10:00:00+00:00",
                "finished_at": "2026-08-30T10:00:01+00:00",
                "ok": False,
                "tests": [{"test_id": "tests.example", "status": "PASS"}],
            }
            report_path = root / "lane.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            digest = hashlib.sha256(release_manifest.canonical_bytes(report)).hexdigest()
            with self.assertRaises(release_manifest.ReleaseManifestError) as raised:
                release_manifest._report(
                    root,
                    {"path": "lane.json", "digest": digest},
                    release_commit="a" * 40,
                    cutoff=release_manifest._parse_utc(
                        "2026-08-30T11:00:00+00:00", "invalid"
                    ),
                )
            self.assertEqual("report_not_passed", str(raised.exception))

    def test_missing_approved_skip_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "release-manifest.v1.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(release_manifest.ReleaseManifestError):
                release_manifest.verify_manifest(path)

    def test_malformed_lane_descriptor_fails_with_stable_error(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "release-manifest.v1.json"
            path.write_text(json.dumps({"lane_reports": [None]}), encoding="utf-8")
            with self.assertRaises(release_manifest.ReleaseManifestError):
                release_manifest.verify_manifest(path)


if __name__ == "__main__":
    unittest.main()
