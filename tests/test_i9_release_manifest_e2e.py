"""End-to-end fixtures for the I9 release-manifest verifier.

The fixtures are deliberately created under a temporary root.  They exercise
the verifier's portable evidence contract without checking in a release
commit, timestamp, or provider result that did not actually happen.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest.mock import patch

from scripts import release_manifest
from scripts import stability_gate


COMMIT = "a" * 40
BUILD = "b" * 64
CONFIG = "c" * 64
IMAGE = "sha256:" + "d" * 64


def _report_digest(document: dict) -> str:
    body = dict(document)
    body.pop("report_digest", None)
    return hashlib.sha256(release_manifest.canonical_bytes(body)).hexdigest()


def _manifest_digest(document: dict) -> str:
    body = dict(document)
    body.pop("manifest_digest", None)
    return hashlib.sha256(release_manifest.canonical_bytes(body)).hexdigest()


def _write_report(path: Path, document: dict) -> str:
    document = dict(document)
    document.pop("report_digest", None)
    document["report_digest"] = _report_digest(document)
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    return document["report_digest"]


def _write_json(path: Path, document: dict) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


class ManifestFixture:
    """A complete, self-consistent release evidence tree."""

    def __init__(
        self,
        root: Path,
        *,
        commit: str = COMMIT,
        build: str = BUILD,
        config: str = CONFIG,
        image_digest: str = IMAGE,
        generated_at: datetime | None = None,
        include_provider: bool = True,
        include_soak: bool = True,
        skip_replacement_status: str = "PASS",
        report_age_days: int = 0,
    ) -> None:
        self.root = root
        self.commit, self.build, self.config, self.image_digest = commit, build, config, image_digest
        self.root.joinpath("docs").mkdir(parents=True)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.generated_at = generated_at or now
        report_time = now - timedelta(days=report_age_days, seconds=2)
        self.manifest = self._base_manifest(self.generated_at)
        self._write_approved_skips(self.generated_at)

        lane_descriptors: list[dict] = []
        required = {
            ("pr-fast", "windows"): 3,
            ("pr-fast", "linux"): 3,
            ("integration", "windows"): 3,
            ("integration", "linux"): 3,
            ("mcp", "windows"): 3,
            ("mcp", "linux"): 3,
            ("docker", "linux"): 3,
            ("golden", "linux"): 3,
        }
        for (lane, os_name), count in required.items():
            for ordinal in range(1, count + 1):
                skipped = lane == "pr-fast" and os_name == "windows" and ordinal == 1
                tests = (
                    [{"test_id": "tests.platform.optional", "status": "SKIP"}]
                    if skipped
                    else [{"test_id": f"tests.{lane}.{os_name}.{ordinal}", "status": "PASS"}]
                )
                if lane == "pr-fast" and os_name == "windows" and ordinal == 2:
                    tests = [{"test_id": "tests.platform.replacement", "status": skip_replacement_status}]
                image = self.image_digest if lane in {"docker", "golden"} else None
                descriptor = self._make_report(
                    lane,
                    os_name,
                    ordinal,
                    report_time,
                    tests,
                    image=image,
                )
                lane_descriptors.append(descriptor)

        if include_soak:
            lane_descriptors.append(
                self._make_report(
                    "soak",
                    "linux",
                    1,
                    report_time,
                    [{"test_id": "tests.soak.reference_24h", "status": "PASS"}],
                    extra={
                        "reference_qualified": True,
                        "reference_duration_hours": 24,
                    },
                )
            )
        if include_provider:
            lane_descriptors.append(
                self._make_report(
                    "provider-opt-in",
                    "linux",
                    1,
                    report_time,
                    [{"test_id": "provider.contract.scope_and_cleanup", "status": "PASS"}],
                    extra={
                        "provider": "qualified-provider-fixture",
                        "model": "qualified-fixture-model",
                        "credential_scope_digest": "e" * 64,
                        "safe_config_digest": "f" * 64,
                        # This is a synthetic, temporary fixture used to test
                        # the verifier's qualified shape; it is not a claim
                        # that this machine contacted a provider.
                        "execution_mode": "real-provider",
                        "real_provider_executed": True,
                        "candidate_identity": {
                            "release_commit": self.commit,
                            "build_artifact_digest": self.build,
                            "measured_at": report_time.isoformat(),
                        },
                        "required_scenarios": [
                            "smoke", "multi_turn", "empty_completion", "timeout_cancel"
                        ],
                        "scenarios": {
                            name: {
                                "status": "PASS",
                                "outcome": "fixture-qualified-shape",
                                "observed": (
                                    {"completion": "non_empty"}
                                    if name == "smoke"
                                    else {"turns": 2, "context_continuity": True}
                                    if name == "multi_turn"
                                    else {"empty_completion": True, "stable_error": "openai.empty_completion_retried"}
                                    if name == "empty_completion"
                                    else {"timeout_observed": True, "cancel_observed": True, "cancel_guard_checks": 2, "timeout_seconds": 0.5}
                                ),
                                "event_count": 1,
                                "terminal": "TurnCompleted",
                                "sequence_contiguous": True,
                                "resource_cleanup": {
                                    "before": {"non_daemon_threads": 1, "active_children": 0, "handles_or_fds": 4},
                                    "after": {"non_daemon_threads": 1, "active_children": 0, "handles_or_fds": 4},
                                    "delta": {"non_daemon_threads": 0, "active_children": 0, "handles_or_fds": 0},
                                    "zero_delta": True,
                                },
                            }
                            for name in ("smoke", "multi_turn", "empty_completion", "timeout_cancel")
                        },
                        "canonical_stream": {
                            "event_count": 4,
                            "terminal": "TurnCompleted",
                            "sequence_contiguous": True,
                        },
                        "resource_cleanup": {
                            "before": {"non_daemon_threads": 1, "active_children": 0, "handles_or_fds": 4},
                            "after": {"non_daemon_threads": 1, "active_children": 0, "handles_or_fds": 4},
                            "delta": {"non_daemon_threads": 0, "active_children": 0, "handles_or_fds": 0},
                            "zero_delta": True,
                        },
                    },
                )
            )
        self.manifest["lane_reports"] = lane_descriptors
        self.manifest["manifest_digest"] = _manifest_digest(self.manifest)
        _write_json(self.root / "release-manifest.v1.json", self.manifest)

    def _base_manifest(self, now: datetime) -> dict:
        return {
            "manifest_schema_version": 1,
            "candidate_id": "candidate-i9-fixture",
            "release_commit": self.commit,
            "build_artifact_digest": self.build,
            "canonical_config_digest": self.config,
            "docker_image_digest": self.image_digest,
            "approved_skip_manifest_digest": "0" * 64,
            "generated_at": now.isoformat(),
            "lane_reports": [],
            "release_audit_report": {},
            "fresh_demo_report": {},
            "migration_reports": [],
            "manifest_digest": "0" * 64,
        }

    def _write_approved_skips(self, now: datetime) -> None:
        document = {
            "schema_version": 1,
            "generated_for_commit": self.commit,
            "entries": [
                {
                    "test_id": "tests.platform.optional",
                    "lane": "pr-fast",
                    "reason_code": "platform_unavailable",
                    "replacement_lane": "pr-fast",
                    "replacement_test_id": "tests.platform.replacement",
                    "expires_at": (now + timedelta(days=1)).isoformat(),
                }
            ],
            "manifest_digest": "0" * 64,
        }
        document["manifest_digest"] = _manifest_digest(document)
        _write_json(self.root / "docs" / "stability-approved-skips.json", document)
        self.manifest["approved_skip_manifest_digest"] = document["manifest_digest"]

    def _make_report(
        self,
        lane: str,
        os_name: str,
        ordinal: int,
        report_time: datetime,
        tests: list[dict],
        *,
        image: str | None = None,
        extra: dict | None = None,
    ) -> dict:
        report = {
            "commit": self.commit,
            "build_artifact_digest": self.build,
            "canonical_config_digest": self.config,
            "generated_at": report_time.isoformat(),
            "finished_at": (report_time + timedelta(seconds=1)).isoformat(),
            "ok": True,
            "tests": tests,
        }
        if image is not None:
            report["docker_image_digest"] = image
        if extra:
            report.update(extra)
        relative = Path("reports") / f"{lane}-{os_name}-{ordinal}.json"
        path = self.root / relative
        path.parent.mkdir(exist_ok=True)
        digest = _write_report(path, report)
        return {
            "lane": lane,
            "os": os_name,
            "series_id": f"{self.manifest['candidate_id']}:{lane}:{os_name}",
            "ordinal": ordinal,
            "report_path": relative.as_posix(),
            "report_digest": digest,
        }

    def add_auxiliary_reports(self) -> None:
        """Add audit/demo/migration reports after the lane fixture exists."""
        report_time = self.generated_at - timedelta(seconds=2)
        def auxiliary(name: str, payload: dict) -> dict:
            relative = Path("reports") / name
            digest = _write_report(
                self.root / relative,
                {
                    "commit": self.commit,
                    "generated_at": report_time.isoformat(),
                    "finished_at": (report_time + timedelta(seconds=1)).isoformat(),
                    "release_pass": True,
                    **payload,
                },
            )
            return {"path": relative.as_posix(), "digest": digest}

        self.manifest["release_audit_report"] = auxiliary(
            "release-audit.json", {"active_resources": {"runs": 0}}
        )
        self.manifest["fresh_demo_report"] = auxiliary(
            "fresh-demo.json", {"demo": "offline", "passed": True}
        )
        self.manifest["migration_reports"] = [
            {"kind": "current-next", **auxiliary("migration-current-next.json", {})},
            {"kind": "legacy-export", **auxiliary("migration-legacy-export.json", {})},
        ]
        self.manifest["manifest_digest"] = _manifest_digest(self.manifest)
        _write_json(self.root / "release-manifest.v1.json", self.manifest)


class I9ReleaseManifestE2ETest(unittest.TestCase):
    def _fixture(self, **kwargs) -> tuple[tempfile.TemporaryDirectory, ManifestFixture]:
        temporary = tempfile.TemporaryDirectory()
        fixture = ManifestFixture(Path(temporary.name), **kwargs)
        fixture.add_auxiliary_reports()
        return temporary, fixture

    def test_complete_manifest_passes_all_required_series_and_auxiliary_evidence(self):
        temporary, fixture = self._fixture()
        self.addCleanup(temporary.cleanup)
        result = release_manifest.verify_manifest(
            Path(temporary.name) / "release-manifest.v1.json"
        )
        self.assertTrue(result["ok"])
        self.assertEqual("candidate-i9-fixture", result["candidate_id"])
        series = [(item["lane"], item["os"], item["ordinal"]) for item in fixture.manifest["lane_reports"]]
        self.assertEqual(3, sum(lane == "pr-fast" and os_name == "windows" for lane, os_name, _ in series))
        self.assertEqual(3, sum(lane == "docker" and os_name == "linux" for lane, os_name, _ in series))
        self.assertEqual([1], [ordinal for lane, os_name, ordinal in series if lane == "soak" and os_name == "linux"])
        self.assertTrue(any(lane == "provider-opt-in" for lane, _, _ in series))

    def test_series_missing_ordinal_fails_closed(self):
        temporary, fixture = self._fixture()
        self.addCleanup(temporary.cleanup)
        fixture.manifest["lane_reports"] = [
            item for item in fixture.manifest["lane_reports"]
            if not (item["lane"] == "mcp" and item["os"] == "linux" and item["ordinal"] == 3)
        ]
        fixture.manifest["manifest_digest"] = _manifest_digest(fixture.manifest)
        _write_json(Path(temporary.name) / "release-manifest.v1.json", fixture.manifest)
        with self.assertRaisesRegex(release_manifest.ReleaseManifestError, "lane_series_incomplete"):
            release_manifest.verify_manifest(Path(temporary.name) / "release-manifest.v1.json")

    def test_report_identity_mismatch_fails_closed(self):
        temporary, fixture = self._fixture()
        self.addCleanup(temporary.cleanup)
        descriptor = next(item for item in fixture.manifest["lane_reports"] if item["lane"] == "integration")
        path = Path(temporary.name) / descriptor["report_path"]
        report = json.loads(path.read_text(encoding="utf-8"))
        report["build_artifact_digest"] = "f" * 64
        descriptor["report_digest"] = _write_report(path, report)
        fixture.manifest["manifest_digest"] = _manifest_digest(fixture.manifest)
        _write_json(Path(temporary.name) / "release-manifest.v1.json", fixture.manifest)
        with self.assertRaisesRegex(release_manifest.ReleaseManifestError, "report_identity_mismatch"):
            release_manifest.verify_manifest(Path(temporary.name) / "release-manifest.v1.json")

    def test_stale_report_fails_closed(self):
        temporary, _ = self._fixture(report_age_days=8)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(release_manifest.ReleaseManifestError, "report_expired"):
            release_manifest.verify_manifest(Path(temporary.name) / "release-manifest.v1.json")

    def test_skip_requires_exact_replacement_test_pass(self):
        temporary, _ = self._fixture(skip_replacement_status="SKIP")
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(release_manifest.ReleaseManifestError, "replacement_test_missing"):
            release_manifest.verify_manifest(Path(temporary.name) / "release-manifest.v1.json")

    def test_soak_must_occur_exactly_once(self):
        temporary, fixture = self._fixture()
        self.addCleanup(temporary.cleanup)
        original = next(item for item in fixture.manifest["lane_reports"] if item["lane"] == "soak")
        duplicate = dict(original)
        duplicate["ordinal"] = 2
        fixture.manifest["lane_reports"].append(duplicate)
        fixture.manifest["manifest_digest"] = _manifest_digest(fixture.manifest)
        _write_json(Path(temporary.name) / "release-manifest.v1.json", fixture.manifest)
        with self.assertRaisesRegex(release_manifest.ReleaseManifestError, "soak_series_incomplete"):
            release_manifest.verify_manifest(Path(temporary.name) / "release-manifest.v1.json")

    def test_provider_opt_in_is_required(self):
        temporary, fixture = self._fixture(include_provider=False)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(release_manifest.ReleaseManifestError, "provider_series_missing"):
            release_manifest.verify_manifest(Path(temporary.name) / "release-manifest.v1.json")

    def test_contract_only_provider_report_cannot_certify_release(self):
        temporary, fixture = self._fixture()
        self.addCleanup(temporary.cleanup)
        descriptor = next(
            item for item in fixture.manifest["lane_reports"] if item["lane"] == "provider-opt-in"
        )
        path = Path(temporary.name) / descriptor["report_path"]
        report = json.loads(path.read_text(encoding="utf-8"))
        report["execution_mode"] = "contract-only"
        report["real_provider_executed"] = False
        descriptor["report_digest"] = _write_report(path, report)
        fixture.manifest["manifest_digest"] = _manifest_digest(fixture.manifest)
        _write_json(Path(temporary.name) / "release-manifest.v1.json", fixture.manifest)
        with self.assertRaisesRegex(release_manifest.ReleaseManifestError, "provider_evidence_not_real"):
            release_manifest.verify_manifest(Path(temporary.name) / "release-manifest.v1.json")

    def test_provider_missing_required_scenario_cannot_certify_release(self):
        temporary, fixture = self._fixture()
        self.addCleanup(temporary.cleanup)
        descriptor = next(
            item for item in fixture.manifest["lane_reports"] if item["lane"] == "provider-opt-in"
        )
        path = Path(temporary.name) / descriptor["report_path"]
        report = json.loads(path.read_text(encoding="utf-8"))
        report["scenarios"].pop("empty_completion")
        descriptor["report_digest"] = _write_report(path, report)
        fixture.manifest["manifest_digest"] = _manifest_digest(fixture.manifest)
        _write_json(Path(temporary.name) / "release-manifest.v1.json", fixture.manifest)
        with self.assertRaisesRegex(release_manifest.ReleaseManifestError, "provider_evidence_scenarios_invalid"):
            release_manifest.verify_manifest(Path(temporary.name) / "release-manifest.v1.json")

    def test_provider_unknown_scenario_cannot_certify_release(self):
        temporary, fixture = self._fixture()
        self.addCleanup(temporary.cleanup)
        descriptor = next(
            item for item in fixture.manifest["lane_reports"] if item["lane"] == "provider-opt-in"
        )
        path = Path(temporary.name) / descriptor["report_path"]
        report = json.loads(path.read_text(encoding="utf-8"))
        report["required_scenarios"].append("provider_specific_extra")
        report["scenarios"]["provider_specific_extra"] = dict(report["scenarios"]["smoke"])
        descriptor["report_digest"] = _write_report(path, report)
        fixture.manifest["manifest_digest"] = _manifest_digest(fixture.manifest)
        _write_json(Path(temporary.name) / "release-manifest.v1.json", fixture.manifest)
        with self.assertRaisesRegex(release_manifest.ReleaseManifestError, "provider_evidence_scenarios_invalid"):
            release_manifest.verify_manifest(Path(temporary.name) / "release-manifest.v1.json")

    def test_provider_nonzero_cleanup_cannot_certify_release(self):
        temporary, fixture = self._fixture()
        self.addCleanup(temporary.cleanup)
        descriptor = next(
            item for item in fixture.manifest["lane_reports"] if item["lane"] == "provider-opt-in"
        )
        path = Path(temporary.name) / descriptor["report_path"]
        report = json.loads(path.read_text(encoding="utf-8"))
        report["resource_cleanup"]["delta"]["handles_or_fds"] = 1
        descriptor["report_digest"] = _write_report(path, report)
        fixture.manifest["manifest_digest"] = _manifest_digest(fixture.manifest)
        _write_json(Path(temporary.name) / "release-manifest.v1.json", fixture.manifest)
        with self.assertRaisesRegex(release_manifest.ReleaseManifestError, "provider_evidence_cleanup_invalid"):
            release_manifest.verify_manifest(Path(temporary.name) / "release-manifest.v1.json")

    def test_unmeasured_cleanup_cannot_certify_release(self):
        temporary, fixture = self._fixture()
        self.addCleanup(temporary.cleanup)
        descriptor = next(
            item for item in fixture.manifest["lane_reports"] if item["lane"] == "provider-opt-in"
        )
        path = Path(temporary.name) / descriptor["report_path"]
        report = json.loads(path.read_text(encoding="utf-8"))
        report["resource_cleanup"] = {"processes": 0, "threads": 0, "handles": 0}
        descriptor["report_digest"] = _write_report(path, report)
        fixture.manifest["manifest_digest"] = _manifest_digest(fixture.manifest)
        _write_json(Path(temporary.name) / "release-manifest.v1.json", fixture.manifest)
        with self.assertRaisesRegex(release_manifest.ReleaseManifestError, "provider_evidence_cleanup_invalid"):
            release_manifest.verify_manifest(Path(temporary.name) / "release-manifest.v1.json")

    def test_provider_lane_targets_real_i9_runner_and_contract_is_not_release_evidence(self):
        self.assertEqual(("tests.test_i9_provider_evidence",), stability_gate.LANES["provider-opt-in"])
        self.assertIsNone(importlib.util.find_spec("tests.test_d23_provider_evidence"))
        from tests.test_i9_provider_contract import ProviderEvidenceContractTest

        self.assertTrue(issubclass(ProviderEvidenceContractTest, unittest.TestCase))

    def test_direct_stability_gate_script_resolves_tests_from_repo_root(self):
        with tempfile.TemporaryDirectory() as raw:
            report_path = Path(raw) / "provider-direct.json"
            environment = os.environ.copy()
            for key in (
                "KOAWA_I9_PROVIDER_BASE_URL", "KOAWA_I9_PROVIDER_MODEL",
                "KOAWA_I9_PROVIDER_API_KEY_ENV", "KOAWA_I9_PROVIDER_NAME",
                "KOAWA_I9_PROVIDER_EVIDENCE_FILE",
            ):
                environment.pop(key, None)
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "stability_gate.py"),
                 "--lane", "provider-opt-in", "--report", str(report_path)],
                cwd=Path(__file__).resolve().parents[1], env=environment,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(1, completed.returncode)
            self.assertNotIn("ModuleNotFoundError", completed.stdout + completed.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["ok"])
            self.assertEqual("provider_evidence_missing", report["provider_evidence_error"])

    def test_gate_merges_qualified_provider_facts_without_secret_into_manifest(self):
        from koawa_agent_v2.model.protocol import (
            AssistantTextItem,
            FinishReason,
            ItemCompleted,
            ItemStarted,
            ModelTurn,
            OutputKind,
            StreamHeader,
            TurnCompleted,
            TurnStarted,
        )
        from tests import test_i9_provider_evidence as provider_runner

        secret = "provider-secret-never-persisted"
        build, config = "1" * 64, "2" * 64

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.received_key = args[1]

            def stream(self, request):
                from koawa_agent_v2.model.openai_client import OpenAICompatibleClientError

                prompt = " ".join(
                    item.content for item in request.input_items
                    if hasattr(item, "content")
                )
                if "empty visible completion" in prompt:
                    raise OpenAICompatibleClientError("openai.empty_completion_retried")
                if "timeout probe" in prompt:
                    from time import sleep
                    sleep(0.45)
                    raise OpenAICompatibleClientError("openai.stream_deadline_exceeded")
                def header(sequence):
                    return StreamHeader(
                        request.model_turn_id,
                        request.provider,
                        "fixture-response",
                        sequence,
                        sequence,
                    )

                item = AssistantTextItem(0, "fixture-item", "ok")
                turn = ModelTurn(
                    request.model_turn_id,
                    request.provider,
                    request.model,
                    "fixture-response",
                    (item,),
                    FinishReason.STOP,
                )
                yield TurnStarted(header(0), request.model)
                yield ItemStarted(header(1), 0, "fixture-item", OutputKind.ASSISTANT_TEXT)
                yield ItemCompleted(header(2), item)
                yield TurnCompleted(header(3), turn)

            def stream_controlled(self, request, *, progress_guard):
                for event in self.stream(request):
                    progress_guard()
                    yield event

        environment = {
            "KOAWA_I9_PROVIDER_BASE_URL": "https://provider.example/v1",
            "KOAWA_I9_PROVIDER_MODEL": "fixture-model",
            "KOAWA_I9_PROVIDER_API_KEY_ENV": "I9_TEST_PROVIDER_KEY",
            "I9_TEST_PROVIDER_KEY": secret,
        }
        with tempfile.TemporaryDirectory() as user_output:
            user_path = Path(user_output) / "user-selected-evidence.json"
            environment[stability_gate.PROVIDER_EVIDENCE_ENV] = str(user_path)
            with patch.dict(os.environ, environment, clear=False), patch.object(
                provider_runner, "OpenAICompatibleChatClient", FakeClient
            ):
                report = stability_gate.run_lane(
                    "provider-opt-in",
                    build_artifact_digest=build,
                    canonical_config_digest=config,
                    docker_image_digest=IMAGE,
                )
            self.assertFalse(user_path.exists())
        self.assertTrue(report["ok"])
        self.assertEqual("fixture-model", report["model"])
        self.assertTrue(report["real_provider_executed"])
        self.assertTrue(report["resource_cleanup"]["zero_delta"])
        self.assertNotIn(secret, json.dumps(report, sort_keys=True))

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = ManifestFixture(
                root,
                commit=report["commit"],
                build=build,
                config=config,
                image_digest=IMAGE,
                generated_at=datetime.fromisoformat(report["finished_at"]) + timedelta(seconds=1),
            )
            fixture.add_auxiliary_reports()
            provider_descriptor = next(
                item for item in fixture.manifest["lane_reports"] if item["lane"] == "provider-opt-in"
            )
            relative = Path("reports") / "provider-gate.json"
            provider_descriptor["report_path"] = relative.as_posix()
            provider_descriptor["report_digest"] = _write_report(root / relative, report)
            fixture.manifest["manifest_digest"] = _manifest_digest(fixture.manifest)
            _write_json(root / "release-manifest.v1.json", fixture.manifest)
            verified = release_manifest.verify_manifest(root / "release-manifest.v1.json")
            self.assertTrue(verified["ok"])


if __name__ == "__main__":
    unittest.main()
