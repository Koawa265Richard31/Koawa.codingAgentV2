"""Deterministic I9 lane runner.

This runner deliberately treats skipped mandatory tests as failures.  Platform
exceptions must be recorded in the approved-skip manifest and verified against
an alternate lane; they are never silently accepted here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for directory in (REPO, REPO / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
LANES = {
    "pr-fast": ("tests",),
    "integration": ("tests.test_d11_agent_process_kill", "tests.test_d12_workspace_integration", "tests.test_stability_fault_matrix"),
    "mcp": ("tests.test_d10_connection_binding", "tests.test_d10_integration", "tests.test_d11_mcp_activation", "tests.test_stability_fault_matrix"),
    "docker": ("tests.test_d8_docker_integration", "tests.test_d12_workspace_integration"),
    "golden": ("tests.test_golden_composite_e2e",),
    "soak": ("tests.test_stability_soak", "tests.test_stability_load", "tests.test_stability_resources"),
    "provider-opt-in": ("tests.test_i9_provider_evidence",),
}
MANDATORY_LANES = frozenset({"docker", "golden", "mcp", "integration", "soak", "provider-opt-in"})
PROVIDER_EVIDENCE_ENV = "KOAWA_I9_PROVIDER_EVIDENCE_FILE"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8", "strict")


def _atomic_json(path: Path, document: dict) -> str:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(document)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        body.pop("report_digest", None)
        digest = hashlib.sha256(canonical_bytes(body)).hexdigest()
        document = {**body, "report_digest": digest}
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(document)); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        return digest
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


class LaneResult(unittest.TestResult):
    def __init__(self) -> None:
        super().__init__()
        self.started = time.perf_counter()
        self.tests: list[dict] = []
        self._starts: dict[unittest.case.TestCase, float] = {}

    def startTest(self, test):
        self._starts[test] = time.perf_counter(); super().startTest(test)

    def _record(self, test, status: str, detail: str = "") -> None:
        self.tests.append({"test_id": test.id(), "status": status,
                           "detail": detail[:512],
                           "duration_ms": round((time.perf_counter() - self._starts.pop(test, time.perf_counter())) * 1000, 3)})

    def addSuccess(self, test): self._record(test, "PASS"); super().addSuccess(test)
    def addFailure(self, test, err): self._record(test, "FAIL", self._exc_info_to_string(err, test)); super().addFailure(test, err)
    def addError(self, test, err): self._record(test, "ERROR", self._exc_info_to_string(err, test)); super().addError(test, err)
    def addSkip(self, test, reason): self._record(test, "SKIP", str(reason)); super().addSkip(test, reason)
    def addExpectedFailure(self, test, err):
        self._record(test, "FAIL", self._exc_info_to_string(err, test)); super().addExpectedFailure(test, err)
    def addUnexpectedSuccess(self, test):
        self._record(test, "FAIL", "unexpected_success"); super().addUnexpectedSuccess(test)


def _suite_for(loader: unittest.TestLoader, lane: str) -> unittest.TestSuite:
    names = LANES[lane]
    if names == ("tests",):
        return loader.discover(str(REPO / "tests"))
    suite = unittest.TestSuite()
    for name in names:
        suite.addTests(loader.loadTestsFromName(name))
    return suite


def _provider_evidence(
    path: Path,
    *,
    expected_commit: str | None = None,
    expected_build: str | None = None,
) -> tuple[dict | None, str | None]:
    """Read and validate only the non-secret facts emitted by the opt-in runner."""
    try:
        raw = path.read_bytes()
    except (OSError, UnicodeError):
        return None, "provider_evidence_missing"
    if any(marker in raw for marker in (b"OPENAI_API_KEY", b"Authorization: Bearer ", b"sk-")):
        return None, "provider_evidence_canary"
    try:
        value = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "provider_evidence_invalid"
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "provider", "model", "credential_scope_digest",
        "safe_config_digest", "execution_mode", "real_provider_executed",
        "started_at", "finished_at", "candidate_identity",
        "required_scenarios", "scenarios", "canonical_stream", "resource_cleanup",
    } or value["schema_version"] != 1:
        return None, "provider_evidence_invalid"
    if any(not isinstance(value[field], str) or not value[field].strip() for field in ("provider", "model")):
        return None, "provider_evidence_identity"
    if not isinstance(value["credential_scope_digest"], str) or not _HEX64.fullmatch(value["credential_scope_digest"]):
        return None, "provider_evidence_scope"
    if not isinstance(value["safe_config_digest"], str) or not _HEX64.fullmatch(value["safe_config_digest"]):
        return None, "provider_evidence_config"
    if value["execution_mode"] != "real-provider" or value["real_provider_executed"] is not True:
        return None, "provider_evidence_not_real"
    identity = value["candidate_identity"]
    if not isinstance(identity, dict) or set(identity) != {"release_commit", "build_artifact_digest", "measured_at"}:
        return None, "provider_evidence_identity"
    if not isinstance(identity["release_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", identity["release_commit"]):
        return None, "provider_evidence_identity"
    if not isinstance(identity["build_artifact_digest"], str) or not _HEX64.fullmatch(identity["build_artifact_digest"]):
        return None, "provider_evidence_identity"
    if not isinstance(identity["measured_at"], str) or not identity["measured_at"].strip():
        return None, "provider_evidence_identity"
    try:
        measured_at = datetime.fromisoformat(identity["measured_at"].replace("Z", "+00:00"))
    except ValueError:
        return None, "provider_evidence_identity"
    if measured_at.tzinfo is None or measured_at.utcoffset() is None:
        return None, "provider_evidence_identity"
    if expected_commit is not None and identity["release_commit"] != expected_commit:
        return None, "provider_evidence_identity"
    if expected_build is None or identity["build_artifact_digest"] != expected_build:
        return None, "provider_evidence_identity"
    required_scenarios = ("smoke", "multi_turn", "empty_completion", "timeout_cancel")
    if value["required_scenarios"] != list(required_scenarios):
        return None, "provider_evidence_scenarios"
    scenarios = value["scenarios"]
    if not isinstance(scenarios, dict) or set(scenarios) != set(required_scenarios):
        return None, "provider_evidence_scenarios"
    sample_keys = {"non_daemon_threads", "active_children", "handles_or_fds"}
    allowed_terminals = {"TurnCompleted", "EmptyCompletionRetried", "TimeoutAndCancelled"}
    for scenario in required_scenarios:
        item = scenarios[scenario]
        if not isinstance(item, dict) or set(item) != {
            "status", "outcome", "observed", "event_count", "terminal",
            "sequence_contiguous", "resource_cleanup",
        }:
            return None, "provider_evidence_scenarios"
        if item["status"] != "PASS" or not isinstance(item["outcome"], str) or not item["outcome"].strip():
            return None, "provider_evidence_scenarios"
        if type(item["event_count"]) is not int or item["event_count"] < 0 or item["terminal"] not in allowed_terminals or item["sequence_contiguous"] is not True:
            return None, "provider_evidence_scenarios"
        observed = item["observed"]
        expected_observed = {
            "smoke": {"completion": "non_empty"},
            "multi_turn": {"turns": 2, "context_continuity": True},
            "empty_completion": None,
            "timeout_cancel": None,
        }[scenario]
        if not isinstance(observed, dict):
            return None, "provider_evidence_scenarios"
        if scenario == "smoke" and observed != expected_observed:
            return None, "provider_evidence_scenarios"
        if scenario == "multi_turn" and observed != expected_observed:
            return None, "provider_evidence_scenarios"
        if scenario == "empty_completion":
            if (
                set(observed) != {"empty_completion", "stable_error"}
                or observed["empty_completion"] is not True
                or observed["stable_error"] not in {None, "openai.empty_completion_retried"}
            ):
                return None, "provider_evidence_scenarios"
        if scenario == "timeout_cancel" and (
            set(observed) != {"timeout_observed", "cancel_observed", "cancel_guard_checks", "timeout_seconds"}
            or observed["timeout_observed"] is not True
            or observed["cancel_observed"] is not True
            or type(observed["cancel_guard_checks"]) is not int
            or observed["cancel_guard_checks"] < 2
            or not isinstance(observed["timeout_seconds"], (int, float))
            or isinstance(observed["timeout_seconds"], bool)
            or not 0.05 <= float(observed["timeout_seconds"]) <= 30.0
        ):
            return None, "provider_evidence_scenarios"
        cleanup = item["resource_cleanup"]
        if not isinstance(cleanup, dict) or set(cleanup) != {"before", "after", "delta", "zero_delta"} or cleanup["zero_delta"] is not True:
            return None, "provider_evidence_cleanup"
        before, after, delta = cleanup["before"], cleanup["after"], cleanup["delta"]
        if any(not isinstance(sample, dict) or set(sample) != sample_keys for sample in (before, after, delta)):
            return None, "provider_evidence_cleanup"
        if any(type(item_value) is not int or item_value < 0 for sample in (before, after) for item_value in sample.values()):
            return None, "provider_evidence_cleanup"
        if any(type(item_value) is not int or item_value != after[key] - before[key] or item_value != 0 for key, item_value in delta.items()):
            return None, "provider_evidence_cleanup"
    stream = value["canonical_stream"]
    if not isinstance(stream, dict) or set(stream) != {"event_count", "terminal", "sequence_contiguous"}:
        return None, "provider_evidence_stream"
    if type(stream["event_count"]) is not int or stream["event_count"] < 1 or stream["terminal"] != "TurnCompleted" or stream["sequence_contiguous"] is not True:
        return None, "provider_evidence_stream"
    cleanup = value["resource_cleanup"]
    sample_keys = {"non_daemon_threads", "active_children", "handles_or_fds"}
    if not isinstance(cleanup, dict) or set(cleanup) != {"before", "after", "delta", "zero_delta"} or cleanup["zero_delta"] is not True:
        return None, "provider_evidence_cleanup"
    before, after, delta = cleanup["before"], cleanup["after"], cleanup["delta"]
    if any(not isinstance(sample, dict) or set(sample) != sample_keys for sample in (before, after, delta)):
        return None, "provider_evidence_cleanup"
    if any(type(item) is not int or item < 0 for sample in (before, after) for item in sample.values()):
        return None, "provider_evidence_cleanup"
    if any(type(item) is not int or item != 0 or item != after[key] - before[key] for key, item in delta.items()):
        return None, "provider_evidence_cleanup"
    return {key: value[key] for key in (
        "provider", "model", "credential_scope_digest", "execution_mode",
        "safe_config_digest", "real_provider_executed", "candidate_identity",
        "required_scenarios", "scenarios", "canonical_stream", "resource_cleanup",
    )}, None


def run_lane(
    lane: str,
    *,
    build_artifact_digest: str | None = None,
    canonical_config_digest: str | None = None,
    docker_image_digest: str | None = None,
) -> dict:
    if lane not in LANES:
        raise ValueError("unknown_lane")
    loader = unittest.TestLoader()
    suite = _suite_for(loader, lane)
    result = LaneResult()
    started_at = datetime.now(timezone.utc).isoformat()
    # Keep the process-wide warning filter untouched for callers embedding the
    # gate, while still making every ResourceWarning raised by a test an
    # ordinary unittest ERROR.
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        if lane == "provider-opt-in":
            with tempfile.TemporaryDirectory(prefix="koawa-i9-provider-") as raw:
                evidence_path = Path(raw) / "evidence.json"
                previous = os.environ.get(PROVIDER_EVIDENCE_ENV)
                build_env = "KOAWA_I9_PROVIDER_BUILD_ARTIFACT_DIGEST"
                previous_build = os.environ.get(build_env)
                os.environ[PROVIDER_EVIDENCE_ENV] = str(evidence_path)
                if build_artifact_digest is None:
                    os.environ.pop(build_env, None)
                else:
                    os.environ[build_env] = build_artifact_digest
                try:
                    suite.run(result)
                finally:
                    if previous is None:
                        os.environ.pop(PROVIDER_EVIDENCE_ENV, None)
                    else:
                        os.environ[PROVIDER_EVIDENCE_ENV] = previous
                    if previous_build is None:
                        os.environ.pop(build_env, None)
                    else:
                        os.environ[build_env] = previous_build
                provider_facts, provider_error = _provider_evidence(
                    evidence_path,
                    expected_commit=_git_head(),
                    expected_build=build_artifact_digest,
                )
        else:
            suite.run(result)
            provider_facts, provider_error = None, None
    skipped = [item for item in result.tests if item["status"] == "SKIP"]
    failures = [item for item in result.tests if item["status"] in {"FAIL", "ERROR"}]
    passed = [item for item in result.tests if item["status"] == "PASS"]
    document = {
        "report_schema_version": 1, "lane": lane, "started_at": started_at,
        "generated_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round((time.perf_counter() - result.started) * 1000, 3),
        "commit": _git_head(), "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "os": platform.platform(), "sqlite": _sqlite_version(),
        "build_artifact_digest": build_artifact_digest,
        "canonical_config_digest": canonical_config_digest,
        "docker_image_digest": docker_image_digest,
        "discovered": len(result.tests),
        "passed": len(passed), "failed": sum(item["status"] == "FAIL" for item in result.tests),
        "errors": len(result.errors), "skipped": len(result.skipped),
        "unexpected_skips": skipped if lane in MANDATORY_LANES else [],
        "tests": sorted(result.tests, key=lambda item: item["test_id"]),
        "ok": not failures and (lane not in MANDATORY_LANES or not skipped),
    }
    if lane == "provider-opt-in":
        if provider_facts is not None and not failures and not skipped:
            document.update(provider_facts)
        else:
            document["provider_evidence_error"] = provider_error or "provider_evidence_tests_failed"
            document["ok"] = False
    return document


def _git_head() -> str | None:
    try: value = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError): return None
    return value if len(value) == 40 else None


def _sqlite_version() -> str:
    import sqlite3
    return sqlite3.sqlite_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", choices=sorted(LANES), required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--build-artifact-digest", default=None)
    parser.add_argument("--canonical-config-digest", default=None)
    parser.add_argument("--docker-image-digest", default=None)
    args = parser.parse_args(argv)
    document = run_lane(
        args.lane,
        build_artifact_digest=args.build_artifact_digest,
        canonical_config_digest=args.canonical_config_digest,
        docker_image_digest=args.docker_image_digest,
    )
    digest = _atomic_json(args.report, document)
    print(json.dumps({"lane": args.lane, "report": str(args.report.resolve()), "digest": digest, "ok": document["ok"]}, sort_keys=True))
    return 0 if document["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
