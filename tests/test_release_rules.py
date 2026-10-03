"""R3 regressions (closure review 2026-09-25): per-profile field release.

Default profiles keep the full safe-diagnostic set; an explicit contract
whitelists exactly the named fields; a sensitive profile WITHOUT a contract
returns the fixed withheld state (distinct from failure, human handoff
flagged).  Policy markers always ride for provenance.  The released receipt
is the input to the plan-B projection, so the delivered content inherits
the contract automatically.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.verification.output_policy import (
    HUMAN_REQUIRED,
    RESULT_WITHHELD,
    ReleaseRule,
    apply_release_rule,
    withheld_receipt,
)
from koawa_agent_v2.verification.runner import (
    CommandOutcome,
    CommandProfile,
    CommandResult,
    TrustedCommandRunner,
)
from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry


def _result() -> CommandResult:
    return CommandResult(
        profile_id="only",
        outcome=CommandOutcome.PASSED,
        exit_code=0,
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
    )


class _RuleRunner:
    """Minimal runner surface: profiles + release_rule accessor."""

    def __init__(self, rule) -> None:
        self._rule = rule

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return ("only",)

    def validate_profile(self, profile_id: str) -> None:
        pass

    def run(self, profile_id, *, progress_guard=None, execution_id=None):
        return _result()

    def release_rule(self, profile_id: str):
        return self._rule


def _execute_receipt(rule) -> dict:
    with tempfile.TemporaryDirectory(prefix="r3-") as tmp:
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
            command_runner=_RuleRunner(rule),
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
            self_error = result.is_error
            document = json.loads(result.content) if not result.is_error else {
                "raw": result.content
            }
            return self_error, document
        finally:
            registry.close()


class ReleaseRuleTest(unittest.TestCase):
    def test_default_profile_keeps_full_safe_diagnostics(self) -> None:
        is_error, document = _execute_receipt(None)
        self.assertFalse(is_error)
        self.assertEqual(0, document["exit_code"])
        self.assertIn("argv", document)
        self.assertIn("duration_ms", document)
        self.assertNotIn("stdout", document)

    def test_explicit_contract_whitelists_exactly_named_fields(self) -> None:
        rule = ReleaseRule(fields=("exit_code", "outcome"))
        is_error, document = _execute_receipt(rule)
        self.assertFalse(is_error)
        # Released.
        self.assertEqual(0, document["exit_code"])
        self.assertEqual("passed", document["outcome"])
        # Withheld by contract.
        self.assertNotIn("argv", document)
        self.assertNotIn("duration_ms", document)
        self.assertNotIn("stdout_bytes", document)
        self.assertNotIn("immutable_image_id", document)
        # Provenance markers always ride.
        self.assertIn("test_output_policy", document)
        self.assertEqual("only", document["profile_id"])

    def test_sensitive_without_contract_returns_withheld_state(self) -> None:
        rule = ReleaseRule(sensitive=True)
        is_error, document = _execute_receipt(rule)
        # Withheld is NOT an error: no error patching, no probing retests.
        self.assertFalse(is_error)
        self.assertEqual(RESULT_WITHHELD, document["availability"])
        self.assertIs(True, document["human_required"])
        self.assertEqual("only", document["profile_id"])
        self.assertNotIn("exit_code", document)
        self.assertNotIn("argv", document)
        self.assertNotIn("outcome", document)
        self.assertIn("test_output_policy", document)

    def test_sensitive_with_contract_releases_only_the_contract(self) -> None:
        rule = ReleaseRule(fields=("outcome",), sensitive=True)
        is_error, document = _execute_receipt(rule)
        self.assertFalse(is_error)
        self.assertEqual("passed", document["outcome"])
        self.assertNotIn(RESULT_WITHHELD, document.values())
        self.assertNotIn("argv", document)
        self.assertNotIn("exit_code", document)

    def test_contract_units(self) -> None:
        with self.assertRaises(ValueError):
            ReleaseRule(fields=("stdout",))  # bodies are never releaseable
        with self.assertRaises(ValueError):
            ReleaseRule(fields=())  # empty contract is not a contract
        with self.assertRaises(ValueError):
            ReleaseRule(fields=("exit_code", "exit_code"))
        # apply_release_rule keeps policy markers under any contract.
        payload = apply_release_rule(
            {
                "profile_id": "p",
                "test_output_policy": "v",
                "exit_code": 0,
                "argv": ["x"],
            },
            ReleaseRule(fields=("exit_code",)),
        )
        self.assertEqual(
            {"profile_id", "test_output_policy", "exit_code"}, set(payload)
        )
        # The withheld receipt is the fixed shape.
        self.assertEqual(
            {
                "profile_id",
                "test_output_policy",
                "test_output_visibility",
                "availability",
                "human_required",
            },
            set(withheld_receipt("p")),
        )
        self.assertIs(True, withheld_receipt("p")["human_required"])

    def test_profile_config_validation_and_runner_mapping(self) -> None:
        # CommandProfile validates the contract at construction.
        from koawa_agent_v2.verification.runner import CommandRunnerError

        with self.assertRaises(CommandRunnerError):
            CommandProfile(
                "p1",
                (r"C:\Windows\System32\cmd.exe", "/c", "exit", "0"),
                release_fields=("stdout",),
            )
        # TrustedCommandRunner exposes the configured rule.
        import sys

        profile = CommandProfile(
            "p1",
            (str(Path(sys.executable).resolve()), "-B", "-c", "pass"),
            release_fields=("exit_code",),
            sensitive=True,
        )
        with tempfile.TemporaryDirectory(prefix="r3-runner-") as tmp:
            runner = TrustedCommandRunner(Path(tmp), (profile,))
            rule = runner.release_rule("p1")
            self.assertEqual(("exit_code",), rule.fields)
            self.assertIs(True, rule.sensitive)
            self.assertIsNone(runner.release_rule("missing"))


if __name__ == "__main__":
    unittest.main()
