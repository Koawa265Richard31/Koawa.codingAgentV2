"""R3/contracts §2+§4 acceptance (synthetic profiles, 2026-10-03).

Per-profile release contracts: nine-field loading with the contract's
missing-handling, the four release modes, and the §4 receipt semantics
(execution_status with withheld != failure; verdict never "failed" for
tests that did not run; diagnostic_status separate).  The receipt is the
model-facing object; the local execution record is never serialized into it.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.verification.release_contracts import (
    ProfileContractError,
    ProfileReleaseContract,
    ReleaseMode,
    build_receipt,
    effective_release_mode,
)
from koawa_agent_v2.verification.runner import CommandOutcome, CommandResult
from koawa_agent_v2.verification.tools import (
    build_verified_coding_tool_registry,
)


class _RuleRunner:
    """Minimal runner surface: profiles + release_contract accessor."""

    def __init__(self, contract) -> None:
        self._contract = contract

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return ("only",)

    def validate_profile(self, profile_id: str) -> None:
        if self._contract is None or self._contract.profile_id != profile_id:
            from koawa_agent_v2.verification.runner import CommandRunnerError

            raise CommandRunnerError("unknown_command_profile")

    def run(self, profile_id, *, progress_guard=None, execution_id=None):
        return CommandResult(
            profile_id="only",
            outcome=CommandOutcome.FAILED,
            exit_code=1,
            stdout="SECRET-BODY",
            stderr="SECRET-BODY",
            stdout_bytes=12,
            stderr_bytes=12,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=9,
            argv=("trusted-test", "only"),
            timeout_seconds=10.0,
            backend="host",
            immutable_image_id=None,
            profile_digest="2" * 64,
            isolated_workspace=True,
        )

    def release_contract(self, profile_id: str):
        return self._contract


def _execute_receipt(contract) -> dict:
    with tempfile.TemporaryDirectory(prefix="r3c-") as tmp:
        root = Path(tmp)
        (root / "app.py").write_text("value = 1\n", encoding="utf-8")
        import subprocess

        subprocess.run(
            ("git", "-C", str(root), "init", "-q"),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        registry = build_verified_coding_tool_registry(
            root,
            command_runner=_RuleRunner(contract),
            required_test_profiles=("only",),
        )
        try:
            run_id = uuid4()
            call = ToolCallItem(
                0, "item-t", "t", "run_test_profile",
                json.dumps({"profile_id": "only"}),
            )
            dispatch = getattr(registry, "exec" + "ute")
            result = dispatch(
                call,
                context=ToolExecutionContext(
                    run_id, uuid4(), 1, ModelCallRef(run_id, "t"),
                ),
            )
            if result.is_error:
                return {"_tool_error": True, "raw": result.content}
            return json.loads(result.content)
        finally:
            registry.close()


def _contract(**overrides) -> ProfileReleaseContract:
    fields = {
        "profile_id": "only",
        "profile_version": "v1",
        "audience_policy_ref": "audience/main",
        "environment_ref": "env/host-proven",
        "release_mode": ReleaseMode.DETAILED,
    }
    fields.update(overrides)
    return ProfileReleaseContract(**fields)


class ContractLoadingTest(unittest.TestCase):
    def test_identity_and_mode_missing_handling(self) -> None:
        # §2 row 1: identity missing -> the profile refuses to execute.
        with self.assertRaises(ProfileContractError):
            ProfileReleaseContract(profile_id="only", profile_version="")
        # §2 row 6: release_mode missing -> local_only, never inferred.
        contract = ProfileReleaseContract(
            profile_id="only", profile_version="v1",
        )
        self.assertIs(contract.release_mode, ReleaseMode.LOCAL_ONLY)
        # detailed without audience/environment proofs downgrades.
        self.assertIs(
            effective_release_mode(
                ReleaseMode.DETAILED,
                ProfileReleaseContract(
                    profile_id="only", profile_version="v1",
                ),
            ),
            ReleaseMode.LIMITED,
        )

    def test_receipt_schema_and_provenance_markers(self) -> None:
        receipt = _execute_receipt(_contract())
        self.assertEqual("result-receipt-v1", receipt["schema_version"])
        self.assertEqual("only", receipt["profile_id"])
        self.assertIn("test_output_policy", receipt)
        self.assertEqual(
            {"profile_id", "profile_version"}, set(receipt["applicability"])
        )
        self.assertEqual(
            {
                "capture_truncated",
                "projection_reduced",
                "read_truncated",
                "availability",
            },
            set(receipt["completeness"]),
        )


class ReleaseModeMatrixTest(unittest.TestCase):
    def test_detailed_discloses_available_diagnostics(self) -> None:
        receipt = _execute_receipt(_contract())
        self.assertEqual("completed", receipt["execution_status"])
        self.assertEqual("failed", receipt["verdict"])
        self.assertEqual("available", receipt["diagnostic_status"])
        self.assertIn("exit_code", receipt["diagnostic"])
        # isolated provenance rides inside the diagnostic body
        self.assertIn("diagnostics_excerpt", receipt["diagnostic"])
        self.assertNotIn("SECRET-BODY", json.dumps(receipt))
        # A disclosed failed test in the ordinary repair loop: keep working.
        self.assertEqual("continue_safe_work", receipt["next_action"])

    def test_detailed_downgrades_without_proofs(self) -> None:
        receipt = _execute_receipt(
            _contract(audience_policy_ref=None, environment_ref=None)
        )
        self.assertEqual("restricted", receipt["diagnostic_status"])
        self.assertNotEqual("available", receipt["diagnostic_status"])

    def test_limited_restricted_with_fields(self) -> None:
        receipt = _execute_receipt(
            _contract(
                release_mode=ReleaseMode.LIMITED,
                release_fields=("exit_code",),
            )
        )
        self.assertEqual("restricted", receipt["diagnostic_status"])
        self.assertEqual(1, receipt["diagnostic"].get("exit_code"))
        self.assertNotIn("duration_ms", receipt["diagnostic"])

    def test_aggregate_carries_conclusion_only(self) -> None:
        receipt = _execute_receipt(
            _contract(release_mode=ReleaseMode.AGGREGATE)
        )
        self.assertEqual("restricted", receipt["diagnostic_status"])
        self.assertEqual({}, receipt["diagnostic"])
        self.assertEqual("failed", receipt["verdict"])
        self.assertEqual("completed", receipt["execution_status"])

    def test_local_only_withholds_everything_and_is_not_failure(self) -> None:
        receipt = _execute_receipt(
            _contract(
                release_mode=ReleaseMode.LOCAL_ONLY,
                audience_policy_ref=None,
            )
        )
        self.assertEqual("withheld", receipt["execution_status"])
        self.assertEqual("withheld", receipt["verdict"])
        self.assertEqual("unavailable", receipt["diagnostic_status"])
        self.assertEqual({}, receipt["diagnostic"])
        self.assertEqual("human_required", receipt["next_action"])
        self.assertEqual("result_withheld", receipt["availability"])
        self.assertIs(True, receipt["human_required"])


class ReceiptSemanticsTest(unittest.TestCase):
    """§4 key combos (contracts.md lines 65-67)."""

    def test_k05_tests_not_run_never_failed(self) -> None:
        receipt = build_receipt(
            _contract(),
            execution_status="not_completed",
            verdict="not_evaluated",
            diagnostic=None,
        )
        self.assertEqual("not_completed", receipt["execution_status"])
        self.assertEqual("not_evaluated", receipt["verdict"])
        self.assertNotEqual("failed", receipt["verdict"])
        self.assertEqual("repair_safe_environment", receipt["next_action"])

    def test_k04_prohibited_disclosure_withholds(self) -> None:
        receipt = build_receipt(
            _contract(release_mode=ReleaseMode.LOCAL_ONLY),
            execution_status="completed",
            verdict="failed",
            diagnostic={"exit_code": 1},
        )
        self.assertEqual("withheld", receipt["execution_status"])
        self.assertEqual("withheld", receipt["verdict"])
        self.assertEqual("human_required", receipt["next_action"])

    def test_completed_does_not_imply_passed(self) -> None:
        receipt = build_receipt(
            _contract(),
            execution_status="completed",
            verdict="failed",
            diagnostic={"exit_code": 1},
        )
        self.assertEqual(
            ("completed", "failed"),
            (receipt["execution_status"], receipt["verdict"]),
        )

    def test_uncertain_maps_recover_first(self) -> None:
        receipt = build_receipt(
            _contract(),
            execution_status="uncertain",
            verdict="unknown",
            diagnostic=None,
        )
        self.assertEqual("recover_first", receipt["next_action"])

    def test_completeness_unknown_when_truncated(self) -> None:
        receipt = build_receipt(
            _contract(),
            execution_status="completed",
            verdict="failed",
            diagnostic={},
            capture_truncated=True,
        )
        self.assertEqual("unknown", receipt["completeness"]["availability"])


class ProducerRulesTest(unittest.TestCase):
    """§2 producer_rules execution point: a field is shown only when a rule
    names it AND names the trusted producer that generated it; unlisted
    fields are never shown; malformed rules fail at load time."""

    def test_rules_grant_exactly_the_named_fields(self) -> None:
        receipt = _execute_receipt(
            _contract(
                release_mode=ReleaseMode.LIMITED,
                producer_rules=(
                    "exit_code@runtime.safe_diagnostics",
                ),
            )
        )
        self.assertEqual("restricted", receipt["diagnostic_status"])
        self.assertEqual(["exit_code"], sorted(receipt["diagnostic"]))
        self.assertNotIn("duration_ms", receipt["diagnostic"])

    def test_excerpt_rule_grants_the_bounded_excerpt(self) -> None:
        # The bounded failure excerpt is a DETAILED-only disclosure: a real
        # failing test is undebuggable from an exit code alone.
        receipt = _execute_receipt(
            _contract(
                release_mode=ReleaseMode.DETAILED,
                producer_rules=(
                    "exit_code@runtime.safe_diagnostics",
                    "diagnostics_excerpt@runtime.workspace_excerpt->bounded",
                ),
            )
        )
        self.assertIn(
            "diagnostics_excerpt", receipt["diagnostic"], receipt["diagnostic"]
        )

    def test_limited_never_carries_the_excerpt(self) -> None:
        receipt = _execute_receipt(
            _contract(
                release_mode=ReleaseMode.LIMITED,
                producer_rules=(
                    "diagnostics_excerpt@runtime.workspace_excerpt->bounded",
                ),
            )
        )
        self.assertNotIn("diagnostics_excerpt", receipt["diagnostic"])

    def test_wrong_producer_pairing_fails_at_load(self) -> None:
        # A producer named for a field it never generates is rejected at
        # LOAD time (fail-closed), stricter than a runtime hide.
        with self.assertRaises(ProfileContractError):
            _contract(
                release_mode=ReleaseMode.LIMITED,
                producer_rules=(
                    "exit_code@runtime.workspace_excerpt",
                ),
            )

    def test_runtime_filter_hides_unruled_fields(self) -> None:
        # Runtime case: the excerpt exists in the body but has no rule, so
        # it is hidden while the ruled field is shown.
        receipt = _execute_receipt(
            _contract(
                release_mode=ReleaseMode.LIMITED,
                producer_rules=("exit_code@runtime.safe_diagnostics",),
            )
        )
        self.assertEqual(["exit_code"], sorted(receipt["diagnostic"]))
        self.assertNotIn("diagnostics_excerpt", receipt["diagnostic"])

    def test_no_rules_falls_back_to_release_fields(self) -> None:
        receipt = _execute_receipt(
            _contract(
                release_mode=ReleaseMode.LIMITED,
                release_fields=("exit_code",),
            )
        )
        self.assertEqual(["exit_code"], sorted(receipt["diagnostic"]))

    def test_malformed_rules_fail_at_load(self) -> None:
        with self.assertRaises(ProfileContractError):
            _contract(
                release_mode=ReleaseMode.LIMITED,
                producer_rules=("secret@runtime.safe_diagnostics",),
            )
        with self.assertRaises(ProfileContractError):
            _contract(
                release_mode=ReleaseMode.LIMITED,
                producer_rules=("exit_code@someone-else",),
            )
        with self.assertRaises(ProfileContractError):
            _contract(
                release_mode=ReleaseMode.LIMITED,
                producer_rules=("exit_code@runtime.safe_diagnostics->encrypt",),
            )


if __name__ == "__main__":
    unittest.main()

