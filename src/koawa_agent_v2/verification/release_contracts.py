"""Release-contract surface (contracts.md §2/§4, first protocol version).

§2: every test profile carries a TRUSTED, config-loaded release contract;
the model only references existing profile ids and can never submit JSON as
an approved contract.  Missing fields degrade exactly as the contract table
prescribes (never by inferring permissions):

- profile_id/profile_version missing      -> the profile refuses to execute
- audience_policy_ref missing             -> no model-readable body is produced
- input_policy_ref missing                -> unknown resources are not mounted
- candidate_admission_ref missing         -> sensitive validation stays closed
- environment_ref missing                 -> no safe-diagnostics claim
- release_mode missing                    -> local_only (never inferred)
- release_fields unlisted                 -> fields are not shown to the model
- scratch_policy_ref missing              -> scratch-requiring runs not started
- budget_policy_ref missing               -> budget-dependent capabilities off

§4: the model receipt is a DISTINCT object from the local execution record.
States are never merged:

- execution_status: completed / not_completed / uncertain / withheld
  (withheld is NOT a failure)
- verdict: passed / failed / not_evaluated / unknown / withheld
  (tests that did not run must never say failed; completed does not imply
  passed)
- diagnostic_status: available / restricted / unavailable (body readability
  only; it never changes the local result)

Release modes (contract table): local_only leaks nothing about completion,
duration or size; aggregate/limited disclose only explicitly allowed facts;
detailed additionally requires the audience proof (audience_policy_ref) and
the environment proof (environment_ref), otherwise it downgrades — it never
claims a safe-diagnostics environment it cannot prove.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .output_policy import POLICY_VERSION

RECEIPT_SCHEMA_VERSION = "result-receipt-v1"


class ReleaseMode(StrEnum):
    LOCAL_ONLY = "local_only"
    AGGREGATE = "aggregate"
    LIMITED = "limited"
    DETAILED = "detailed"


class ProfileContractError(RuntimeError):
    """Content-free contract failure carrying a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProfileReleaseContract:
    """§2 per-profile release contract instance (trusted config only)."""

    profile_id: str
    profile_version: str
    audience_policy_ref: str | None = None
    input_policy_ref: str | None = None
    candidate_admission_ref: str | None = None
    environment_ref: str | None = None
    release_mode: ReleaseMode = ReleaseMode.LOCAL_ONLY
    release_fields: tuple[str, ...] | None = None
    producer_rules: tuple[str, ...] = ()
    scratch_policy_ref: str | None = None
    budget_policy_ref: str | None = None
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ProfileContractError("profile_identity_required")
        if (
            not isinstance(self.profile_version, str)
            or not self.profile_version.strip()
        ):
            # §2: without a fixed identity version the profile refuses to run.
            raise ProfileContractError("profile_version_required")
        if not isinstance(self.release_mode, ReleaseMode):
            raise ProfileContractError("invalid_release_mode")
        if self.release_fields is not None:
            if (
                not isinstance(self.release_fields, tuple)
                or not self.release_fields
                or len(set(self.release_fields)) != len(self.release_fields)
                or any(not isinstance(item, str) or not item for item in self.release_fields)
            ):
                raise ProfileContractError("invalid_release_fields")
        if self.producer_rules is not None and not isinstance(
            self.producer_rules, tuple
        ):
            raise ProfileContractError("invalid_producer_rules")
        if not isinstance(self.sensitive, bool):
            raise TypeError("sensitive must be bool")

    # -- §2 capability flags derived from missing-handling -------------------

    @property
    def audience_proven(self) -> bool:
        """detailed may only claim model-readable bodies with this proof."""

        return bool(self.audience_policy_ref)

    @property
    def environment_proven(self) -> bool:
        return bool(self.environment_ref)

    @property
    def sensitive_validation_open(self) -> bool:
        return bool(self.candidate_admission_ref)

    @property
    def scratch_allowed(self) -> bool:
        return bool(self.scratch_policy_ref)


    def effective_mode_allows_body(self) -> bool:
        """Whether the (downgraded) mode may carry ANY diagnostic body."""

        from .output_policy import POLICY_VERSION  # noqa: F401 (marker parity)

        return effective_release_mode(self.release_mode, self) in (
            ReleaseMode.LIMITED,
            ReleaseMode.DETAILED,
        )


def effective_release_mode(
    mode: ReleaseMode, contract: ProfileReleaseContract
) -> ReleaseMode:
    """§2: detailed alone authorizes nothing — it downgrades without proofs."""

    if mode is ReleaseMode.DETAILED and not (
        contract.audience_proven and contract.environment_proven
    ):
        return ReleaseMode.LIMITED
    return mode


def build_receipt(
    contract: ProfileReleaseContract,
    *,
    execution_status: str,
    verdict: str,
    diagnostic: dict | None,
    capture_truncated: bool = False,
    projection_reduced: bool = False,
    read_truncated: bool = False,
    receipt_ref: str | None = None,
) -> dict:
    """§4 receipt: generated FROM the release contract, never a serialization
    of the local execution record.  States stay separate; unauthorized
    diagnostics collapse to the fixed unavailable hint without reasons."""

    mode = effective_release_mode(contract.release_mode, contract)

    # local_only discloses nothing — not even completion or timing.
    if mode is ReleaseMode.LOCAL_ONLY:
        execution_status = "withheld"
        verdict = "withheld"
        diagnostic_status = "unavailable"
        diagnostic_body: dict = {}
        next_action = "human_required"
    else:
        if diagnostic is None:
            diagnostic_status = "unavailable"
            diagnostic_body = {}
        elif mode is ReleaseMode.AGGREGATE:
            diagnostic_status = "restricted"
            diagnostic_body = {}
        else:  # limited / detailed
            diagnostic_status = (
                "available" if mode is ReleaseMode.DETAILED else "restricted"
            )
            diagnostic_body = dict(diagnostic)

    # Unproven environment caps the body even in detailed-downgraded modes.
    if (
        mode in (ReleaseMode.LIMITED, ReleaseMode.DETAILED)
        and diagnostic_status == "available"
        and not contract.environment_proven
    ):
        diagnostic_status = "restricted"

    if verdict == "withheld" and execution_status == "withheld":
        next_action = "human_required"
    elif execution_status == "uncertain":
        next_action = "recover_first"
    elif execution_status == "not_completed":
        next_action = "repair_safe_environment"
    else:
        next_action = "continue_safe_work"

    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_ref": receipt_ref,
        "execution_status": execution_status,
        "verdict": verdict,
        "diagnostic_status": diagnostic_status,
        "diagnostic": diagnostic_body,
        "applicability": {
            "profile_id": contract.profile_id,
            "profile_version": contract.profile_version,
        },
        "completeness": {
            "capture_truncated": bool(capture_truncated),
            "projection_reduced": bool(projection_reduced),
            "read_truncated": bool(read_truncated),
            "availability": (
                "unknown"
                if (capture_truncated or projection_reduced or read_truncated)
                else "complete"
            ),
        },
        "next_action": next_action,
        # provenance markers: always ride for scan binding / audit.
        "profile_id": contract.profile_id,
        "test_output_policy": POLICY_VERSION,
    }
    if mode is ReleaseMode.LOCAL_ONLY:
        receipt["availability"] = "result_withheld"
        receipt["human_required"] = True
    return receipt
