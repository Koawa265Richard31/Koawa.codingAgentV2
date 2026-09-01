"""Fail-closed I9 release evidence manifest verifier."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
TOP_LEVEL = {
    "manifest_schema_version", "candidate_id", "release_commit", "build_artifact_digest",
    "canonical_config_digest", "docker_image_digest", "approved_skip_manifest_digest",
    "generated_at", "lane_reports", "release_audit_report", "fresh_demo_report",
    "migration_reports", "manifest_digest",
}
REQUIRED_SERIES = {
    ("pr-fast", "windows"): 3, ("pr-fast", "linux"): 3,
    ("integration", "windows"): 3, ("integration", "linux"): 3,
    ("mcp", "windows"): 3, ("mcp", "linux"): 3,
    ("docker", "linux"): 3, ("golden", "linux"): 3,
}
KNOWN_LANES = frozenset({"pr-fast", "integration", "mcp", "docker", "golden", "soak", "provider-opt-in"})
KNOWN_OS = frozenset({"windows", "linux"})
APPROVED_SKIP_KEYS = {"test_id", "lane", "reason_code", "replacement_lane", "replacement_test_id", "expires_at"}


class ReleaseManifestError(ValueError):
    pass


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8", "strict")


def _digest(document: dict, field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise ReleaseManifestError(f"{field}_invalid")
    body = dict(document); body.pop(field, None)
    if hashlib.sha256(canonical_bytes(body)).hexdigest() != value:
        raise ReleaseManifestError(f"{field}_mismatch")
    return value


def _parse_utc(value: object, code: str) -> datetime:
    if not isinstance(value, str):
        raise ReleaseManifestError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseManifestError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReleaseManifestError(code)
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path) -> dict:
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError("report_unreadable") from exc
    if not isinstance(value, dict): raise ReleaseManifestError("report_shape_invalid")
    return value


def _report(
    root: Path,
    descriptor: dict,
    *,
    release_commit: str,
    cutoff: datetime,
    max_age_days: int = 7,
) -> dict:
    if not isinstance(descriptor, dict) or set(descriptor) != {"path", "digest"}:
        raise ReleaseManifestError("report_descriptor_invalid")
    relative = descriptor["path"]
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ReleaseManifestError("report_path_invalid")
    digest = descriptor["digest"]
    if not isinstance(digest, str):
        raise ReleaseManifestError("report_digest_invalid")
    expected = digest.removeprefix("sha256:")
    if not HEX64.fullmatch(expected):
        raise ReleaseManifestError("report_digest_invalid")
    path = (root / relative).resolve()
    if root.resolve() not in path.parents:
        raise ReleaseManifestError("report_path_invalid")
    report = _read_json(path)
    actual = hashlib.sha256(canonical_bytes({k: v for k, v in report.items() if k != "report_digest"})).hexdigest()
    if actual != expected or ("report_digest" in report and report.get("report_digest") != actual):
        raise ReleaseManifestError("report_digest_mismatch")
    if report.get("commit") != release_commit:
        raise ReleaseManifestError("report_commit_mismatch")
    generated = _parse_utc(report.get("generated_at"), "report_generated_at_invalid")
    finished = _parse_utc(report.get("finished_at"), "report_finished_at_invalid")
    if generated > finished or finished > cutoff:
        raise ReleaseManifestError("report_time_invalid")
    if (cutoff - generated).total_seconds() > max_age_days * 86400:
        raise ReleaseManifestError("report_expired")
    tests = report.get("tests")
    if isinstance(tests, list):
        if report.get("ok") is not True:
            raise ReleaseManifestError("report_not_passed")
        statuses = {item.get("status") for item in tests if isinstance(item, dict)}
        if any(status not in {"PASS", "SKIP"} for status in statuses) or any(
            not isinstance(item, dict) for item in tests
        ):
            raise ReleaseManifestError("report_not_passed")
    elif report.get("ok") is not True and report.get("release_pass") is not True:
        raise ReleaseManifestError("report_not_passed")
    return report


def _validate_report_identity(
    report: dict,
    manifest: dict,
    *,
    image_required: bool = False,
    require_build_identity: bool = False,
) -> None:
    """Ensure optional evidence identity fields never contradict the candidate.

    Older locally generated lane reports contain only ``commit``.  Such reports
    remain useful for the standalone gate, while a release report that carries
    build/config/image identity is checked strictly here.
    """
    for field in ("build_artifact_digest", "canonical_config_digest"):
        value = report.get(field)
        if value is not None and value != manifest[field]:
            raise ReleaseManifestError("report_identity_mismatch")
        if value is not None and (not isinstance(value, str) or not HEX64.fullmatch(value)):
            raise ReleaseManifestError("report_identity_invalid")
        if require_build_identity and value is None:
            raise ReleaseManifestError("report_identity_missing")
    image = report.get("docker_image_digest")
    if image is not None and image != manifest["docker_image_digest"]:
        raise ReleaseManifestError("report_identity_mismatch")
    if image is not None and (not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image)):
        raise ReleaseManifestError("report_identity_invalid")
    if image_required and image != manifest["docker_image_digest"]:
        raise ReleaseManifestError("report_image_missing")


def _validate_provider_evidence(report: dict) -> None:
    """Require explicit real-provider evidence; contracts cannot certify it."""
    if report.get("real_provider_executed") is not True:
        raise ReleaseManifestError("provider_evidence_not_real")
    if report.get("execution_mode") != "real-provider":
        raise ReleaseManifestError("provider_evidence_mode_invalid")
    for field in ("provider", "model"):
        value = report.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ReleaseManifestError("provider_evidence_identity_missing")
    scope_digest = report.get("credential_scope_digest")
    if not isinstance(scope_digest, str) or not HEX64.fullmatch(scope_digest):
        raise ReleaseManifestError("provider_evidence_scope_invalid")
    config_digest = report.get("safe_config_digest")
    if not isinstance(config_digest, str) or not HEX64.fullmatch(config_digest):
        raise ReleaseManifestError("provider_evidence_config_invalid")
    identity = report.get("candidate_identity")
    if not isinstance(identity, dict) or set(identity) != {
        "release_commit", "build_artifact_digest", "measured_at"
    }:
        raise ReleaseManifestError("provider_evidence_identity_invalid")
    if identity["release_commit"] != report.get("commit"):
        raise ReleaseManifestError("provider_evidence_identity_invalid")
    if identity["build_artifact_digest"] != report.get("build_artifact_digest"):
        raise ReleaseManifestError("provider_evidence_identity_invalid")
    if not isinstance(identity["release_commit"], str) or not HEX40.fullmatch(identity["release_commit"]):
        raise ReleaseManifestError("provider_evidence_identity_invalid")
    if not isinstance(identity["build_artifact_digest"], str) or not HEX64.fullmatch(identity["build_artifact_digest"]):
        raise ReleaseManifestError("provider_evidence_identity_invalid")
    if not isinstance(identity["measured_at"], str) or not identity["measured_at"].strip():
        raise ReleaseManifestError("provider_evidence_identity_invalid")
    _parse_utc(identity["measured_at"], "provider_evidence_identity_invalid")
    required_scenarios = ["smoke", "multi_turn", "empty_completion", "timeout_cancel"]
    if report.get("required_scenarios") != required_scenarios:
        raise ReleaseManifestError("provider_evidence_scenarios_invalid")
    scenarios = report.get("scenarios")
    if not isinstance(scenarios, dict) or set(scenarios) != set(required_scenarios):
        raise ReleaseManifestError("provider_evidence_scenarios_invalid")
    allowed_terminals = {"TurnCompleted", "EmptyCompletionRetried", "TimeoutAndCancelled"}
    stream = report.get("canonical_stream")
    if not isinstance(stream, dict) or set(stream) != {
        "event_count", "terminal", "sequence_contiguous"
    }:
        raise ReleaseManifestError("provider_evidence_stream_invalid")
    if (
        type(stream["event_count"]) is not int
        or stream["event_count"] < 1
        or stream["terminal"] != "TurnCompleted"
        or stream["sequence_contiguous"] is not True
    ):
        raise ReleaseManifestError("provider_evidence_stream_invalid")
    cleanup = report.get("resource_cleanup")
    sample_keys = {"non_daemon_threads", "active_children", "handles_or_fds"}
    if not isinstance(cleanup, dict) or set(cleanup) != {"before", "after", "delta", "zero_delta"}:
        raise ReleaseManifestError("provider_evidence_cleanup_invalid")
    if cleanup["zero_delta"] is not True:
        raise ReleaseManifestError("provider_evidence_cleanup_invalid")
    before, after, delta = cleanup["before"], cleanup["after"], cleanup["delta"]
    if any(not isinstance(sample, dict) or set(sample) != sample_keys for sample in (before, after, delta)):
        raise ReleaseManifestError("provider_evidence_cleanup_invalid")
    if any(type(value) is not int or value < 0 for sample in (before, after) for value in sample.values()):
        raise ReleaseManifestError("provider_evidence_cleanup_invalid")
    if any(type(value) is not int for value in delta.values()):
        raise ReleaseManifestError("provider_evidence_cleanup_invalid")
    if any(delta[key] != after[key] - before[key] or delta[key] != 0 for key in sample_keys):
        raise ReleaseManifestError("provider_evidence_cleanup_invalid")
    for scenario in required_scenarios:
        item = scenarios[scenario]
        if not isinstance(item, dict) or set(item) != {
            "status", "outcome", "observed", "event_count", "terminal",
            "sequence_contiguous", "resource_cleanup",
        }:
            raise ReleaseManifestError("provider_evidence_scenarios_invalid")
        if item["status"] != "PASS" or not isinstance(item["outcome"], str) or not item["outcome"].strip():
            raise ReleaseManifestError("provider_evidence_scenarios_invalid")
        if type(item["event_count"]) is not int or item["event_count"] < 0:
            raise ReleaseManifestError("provider_evidence_scenarios_invalid")
        if item["terminal"] not in allowed_terminals or item["sequence_contiguous"] is not True:
            raise ReleaseManifestError("provider_evidence_scenarios_invalid")
        observed = item["observed"]
        if not isinstance(observed, dict):
            raise ReleaseManifestError("provider_evidence_scenarios_invalid")
        if scenario == "smoke" and observed != {"completion": "non_empty"}:
            raise ReleaseManifestError("provider_evidence_scenarios_invalid")
        if scenario == "multi_turn" and observed != {"turns": 2, "context_continuity": True}:
            raise ReleaseManifestError("provider_evidence_scenarios_invalid")
        if scenario == "empty_completion" and (
            set(observed) != {"empty_completion", "stable_error"}
            or observed["empty_completion"] is not True
            or observed["stable_error"] not in {None, "openai.empty_completion_retried"}
        ):
            raise ReleaseManifestError("provider_evidence_scenarios_invalid")
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
            raise ReleaseManifestError("provider_evidence_scenarios_invalid")
        scenario_cleanup = item["resource_cleanup"]
        if not isinstance(scenario_cleanup, dict) or set(scenario_cleanup) != {
            "before", "after", "delta", "zero_delta"
        } or scenario_cleanup["zero_delta"] is not True:
            raise ReleaseManifestError("provider_evidence_cleanup_invalid")
        scenario_before = scenario_cleanup["before"]
        scenario_after = scenario_cleanup["after"]
        scenario_delta = scenario_cleanup["delta"]
        if any(not isinstance(sample, dict) or set(sample) != sample_keys for sample in (
            scenario_before, scenario_after, scenario_delta
        )):
            raise ReleaseManifestError("provider_evidence_cleanup_invalid")
        if any(type(value) is not int or value < 0 for sample in (
            scenario_before, scenario_after
        ) for value in sample.values()):
            raise ReleaseManifestError("provider_evidence_cleanup_invalid")
        if any(
            type(value) is not int
            or value != scenario_after[key] - scenario_before[key]
            or value != 0
            for key, value in scenario_delta.items()
        ):
            raise ReleaseManifestError("provider_evidence_cleanup_invalid")


def verify_manifest(path: Path) -> dict:
    root = path.resolve().parent
    document = _read_json(path)
    if set(document) != TOP_LEVEL or document.get("manifest_schema_version") != 1:
        raise ReleaseManifestError("manifest_shape_invalid")
    if not isinstance(document["candidate_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", document["candidate_id"]):
        raise ReleaseManifestError("candidate_id_invalid")
    if not isinstance(document["release_commit"], str) or not HEX40.fullmatch(document["release_commit"]):
        raise ReleaseManifestError("release_commit_invalid")
    for field in ("build_artifact_digest", "canonical_config_digest", "approved_skip_manifest_digest"):
        if not isinstance(document[field], str) or not HEX64.fullmatch(document[field]):
            raise ReleaseManifestError(f"{field}_invalid")
    if not isinstance(document["docker_image_digest"], str) or not document["docker_image_digest"].startswith("sha256:"):
        raise ReleaseManifestError("docker_image_digest_invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", document["docker_image_digest"]):
        raise ReleaseManifestError("docker_image_digest_invalid")
    generated_at = _parse_utc(document["generated_at"], "generated_at_invalid")
    _digest(document, "manifest_digest")
    approved = root / "docs" / "stability-approved-skips.json"
    if not approved.is_file(): raise ReleaseManifestError("approved_skip_manifest_missing")
    skip_document = _read_json(approved)
    if set(skip_document) != {"schema_version", "generated_for_commit", "entries", "manifest_digest"} or skip_document["schema_version"] != 1 or skip_document["generated_for_commit"] != document["release_commit"]:
        raise ReleaseManifestError("approved_skip_manifest_invalid")
    _digest(skip_document, "manifest_digest")
    if skip_document["manifest_digest"] != document["approved_skip_manifest_digest"]:
        raise ReleaseManifestError("approved_skip_manifest_digest_mismatch")
    entries = skip_document["entries"]
    if not isinstance(entries, list):
        raise ReleaseManifestError("approved_skip_manifest_invalid")
    approved: dict[tuple[str, str], dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != APPROVED_SKIP_KEYS:
            raise ReleaseManifestError("approved_skip_entry_invalid")
        if any(not isinstance(entry[key], str) or not entry[key].strip() for key in APPROVED_SKIP_KEYS - {"expires_at"}):
            raise ReleaseManifestError("approved_skip_entry_invalid")
        if entry["lane"] not in KNOWN_LANES or entry["replacement_lane"] not in KNOWN_LANES:
            raise ReleaseManifestError("approved_skip_entry_invalid")
        expiry = _parse_utc(entry["expires_at"], "approved_skip_expiry_invalid")
        if expiry <= generated_at:
            raise ReleaseManifestError("approved_skip_expired")
        identity = (entry["lane"], entry["test_id"])
        if identity in approved:
            raise ReleaseManifestError("approved_skip_duplicate")
        approved[identity] = entry

    reports = document["lane_reports"]
    if not isinstance(reports, list) or not reports: raise ReleaseManifestError("lane_reports_invalid")
    series: dict[tuple[str, str], list[int]] = {}
    report_lookup: dict[tuple[str, str, int], dict] = {}
    for item in reports:
        if not isinstance(item, dict) or set(item) != {"lane", "os", "series_id", "ordinal", "report_path", "report_digest"}:
            raise ReleaseManifestError("lane_descriptor_invalid")
        lane, os_name, ordinal = item["lane"], item["os"], item["ordinal"]
        if not isinstance(lane, str) or not isinstance(os_name, str) or lane not in KNOWN_LANES or os_name not in KNOWN_OS or type(ordinal) is not int or ordinal < 1:
            raise ReleaseManifestError("lane_descriptor_invalid")
        if item["series_id"] != f"{document['candidate_id']}:{lane}:{os_name}":
            raise ReleaseManifestError("series_id_invalid")
        key = (lane, os_name, ordinal)
        if key in report_lookup:
            raise ReleaseManifestError("lane_series_duplicate")
        report = _report(
            root,
            {"path": item["report_path"], "digest": item["report_digest"]},
            release_commit=document["release_commit"],
            cutoff=generated_at,
            max_age_days=14 if lane == "soak" else 7,
        )
        _validate_report_identity(
            report,
            document,
            image_required=lane in {"docker", "golden"},
            require_build_identity=True,
        )
        if lane == "provider-opt-in":
            _validate_provider_evidence(report)
        report_lookup[key] = report
        series.setdefault((lane, os_name), []).append(ordinal)
    for key, required in REQUIRED_SERIES.items():
        if sorted(series.get(key, ())) != list(range(1, required + 1)):
            raise ReleaseManifestError("lane_series_incomplete")
    if sorted(series.get(("soak", "linux"), ())) != [1]: raise ReleaseManifestError("soak_series_incomplete")
    if not any(key[0] == "provider-opt-in" and ordinals for key, ordinals in series.items()):
        raise ReleaseManifestError("provider_series_missing")
    # Every skip in a report must have an explicit, unexpired exception.  An
    # exception is valid only when its exact replacement test passed in the
    # declared alternate lane; a passing count is not sufficient evidence.
    for (lane, os_name, _), report in report_lookup.items():
        for test in report.get("tests", ()) if isinstance(report.get("tests"), list) else ():
            if not isinstance(test, dict) or test.get("status") != "SKIP":
                continue
            entry = approved.get((lane, test.get("test_id")))
            if entry is None:
                raise ReleaseManifestError("unexpected_skip")
            replacement = [
                value for (r_lane, _, _), value in report_lookup.items()
                if r_lane == entry["replacement_lane"] and value.get("tests")
                and any(isinstance(item, dict) and item.get("test_id") == entry["replacement_test_id"] and item.get("status") == "PASS" for item in value["tests"])
            ]
            if not replacement:
                raise ReleaseManifestError("replacement_test_missing")
    for field in ("release_audit_report", "fresh_demo_report"):
        report = _report(
            root,
            document[field],
            release_commit=document["release_commit"],
            cutoff=generated_at,
        )
        _validate_report_identity(report, document)
    if not isinstance(document["migration_reports"], list) or not document["migration_reports"]:
        raise ReleaseManifestError("migration_reports_missing")
    for item in document["migration_reports"]:
        if not isinstance(item, dict) or set(item) != {"kind", "path", "digest"} or item["kind"] not in {"current-next", "legacy-export"}:
            raise ReleaseManifestError("migration_descriptor_invalid")
        report = _report(
            root,
            {"path": item["path"], "digest": item["digest"]},
            release_commit=document["release_commit"],
            cutoff=generated_at,
        )
        _validate_report_identity(report, document)
    return {"ok": True, "candidate_id": document["candidate_id"], "manifest_digest": document["manifest_digest"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify"); verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try: result = verify_manifest(args.manifest)
    except ReleaseManifestError as exc:
        print(json.dumps({"ok": False, "code": str(exc)}, sort_keys=True)); return 1
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == "__main__": raise SystemExit(main())
