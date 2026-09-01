"""Strict reference-machine attestation for I8 benchmark and soak lanes.

The attestation supplies facts which Python cannot prove portably (filesystem,
power profile, non-rotational local storage, and reserved logical CPUs).  It is
not a replacement for the reference digest: both the structured evidence and
the digest of the resulting runtime identity must match.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


PROTOCOL_VERSION = "stability-reference-attestation-v1"
MAX_ATTESTATION_BYTES = 64 * 1024
_FACT_KEYS = {"filesystem", "power_profile", "local_ssd", "exclusive_cpus"}
_EVIDENCE_KEYS = {"audited_at", "filesystem", "power_profile", "local_ssd", "exclusive_cpus"}


class ReferenceAttestationError(ValueError):
    """The reference attestation is malformed or incomplete."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8", "strict")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReferenceAttestationError("reference_attestation_duplicate_key")
        result[key] = value
    return result


def _bounded_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8", "strict")) > 512:
        raise ReferenceAttestationError(f"reference_attestation_{field}_invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ReferenceAttestationError(f"reference_attestation_{field}_invalid")
    return value


def validate_attestation(document: object, *, logical_cpu_count: int | None = None) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {
        "protocol_version", "facts", "evidence",
    }:
        raise ReferenceAttestationError("reference_attestation_shape_invalid")
    if document["protocol_version"] != PROTOCOL_VERSION:
        raise ReferenceAttestationError("reference_attestation_protocol_invalid")
    facts, evidence = document["facts"], document["evidence"]
    if not isinstance(facts, dict) or set(facts) != _FACT_KEYS:
        raise ReferenceAttestationError("reference_attestation_facts_invalid")
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_KEYS:
        raise ReferenceAttestationError("reference_attestation_evidence_invalid")
    filesystem = _bounded_text(facts["filesystem"], "filesystem")
    power_profile = _bounded_text(facts["power_profile"], "power_profile")
    if facts["local_ssd"] is not True:
        raise ReferenceAttestationError("reference_attestation_local_ssd_required")
    cpus = facts["exclusive_cpus"]
    if not isinstance(cpus, list) or len(cpus) < 4:
        raise ReferenceAttestationError("reference_attestation_exclusive_cpus_required")
    if any(type(cpu) is not int or cpu < 0 for cpu in cpus) or len(set(cpus)) != len(cpus):
        raise ReferenceAttestationError("reference_attestation_exclusive_cpus_invalid")
    if logical_cpu_count is not None and any(cpu >= logical_cpu_count for cpu in cpus):
        raise ReferenceAttestationError("reference_attestation_exclusive_cpus_invalid")
    normalized_evidence = {
        key: _bounded_text(value, f"evidence_{key}") for key, value in evidence.items()
    }
    normalized = {
        "protocol_version": PROTOCOL_VERSION,
        "facts": {
            "filesystem": filesystem,
            "power_profile": power_profile,
            "local_ssd": True,
            "exclusive_cpus": sorted(cpus),
        },
        "evidence": normalized_evidence,
    }
    normalized["attestation_digest"] = hashlib.sha256(canonical_bytes(normalized)).hexdigest()
    return normalized


def load_attestation(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    raw = resolved.read_bytes()
    if len(raw) > MAX_ATTESTATION_BYTES:
        raise ReferenceAttestationError("reference_attestation_too_large")
    try:
        document = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_object_pairs)
    except ReferenceAttestationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReferenceAttestationError("reference_attestation_json_invalid") from exc
    return validate_attestation(document)


def merge_identity(identity: Mapping[str, Any], attestation: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(identity)
    if attestation is None:
        return merged
    checked = validate_attestation(
        {key: attestation[key] for key in ("protocol_version", "facts", "evidence")},
        logical_cpu_count=merged.get("logical_cpu_count"),
    )
    merged.update(checked["facts"])
    merged["reference_attestation_digest"] = checked["attestation_digest"]
    return merged


def identity_qualification_reasons(
    identity: Mapping[str, Any],
    *,
    reference_digest: str | None,
    environment_digest: str,
    attestation: Mapping[str, Any] | None,
) -> list[str]:
    reasons: list[str] = []
    if attestation is None:
        reasons.append("hardware_attestation_missing")
    if reference_digest != environment_digest:
        reasons.append("reference_environment_mismatch")
    if not str(identity.get("python", "")).startswith("3.12."):
        reasons.append("python_not_3_12")
    if identity.get("python_implementation") != "CPython" or identity.get("python_debug") is not False:
        reasons.append("python_runtime_not_qualified")
    if str(identity.get("machine", "")).lower() not in {"amd64", "x86_64", "arm64", "aarch64"}:
        reasons.append("machine_not_qualified")
    memory = identity.get("memory_bytes")
    if type(memory) is not int or memory < 16 * 1024**3:
        reasons.append("memory_under_16_gib")
    if not isinstance(identity.get("filesystem"), str) or not identity["filesystem"]:
        reasons.append("filesystem_not_attested")
    if not isinstance(identity.get("power_profile"), str) or not identity["power_profile"]:
        reasons.append("power_profile_not_attested")
    if identity.get("local_ssd") is not True:
        reasons.append("local_ssd_not_attested")
    cpus = identity.get("exclusive_cpus")
    if not isinstance(cpus, list) or len(cpus) < 4:
        reasons.append("exclusive_cpus_under_4")
    affinity = identity.get("affinity_cpus")
    if isinstance(cpus, list) and (
        not isinstance(affinity, list) or set(affinity) != set(cpus)
    ):
        reasons.append("exclusive_cpu_affinity_mismatch")
    return reasons
