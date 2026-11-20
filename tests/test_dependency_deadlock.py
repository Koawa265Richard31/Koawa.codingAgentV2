"""Issue #7 A/B regressions.

A: a missing dependency must never be reported as "your code failed", and
   the only route to a prepared environment is the runtime's own observation.
B: the verification gate must refuse an assembly whose test surface the
   model is allowed to rewrite.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.runtime.config import RuntimeConfigError, load_runtime_config
from koawa_agent_v2.verification.dependency_preparation import (
    PREP_READY,
    PREP_REQUIRES_AUTHORIZATION,
    DependencyScope,
    plan_preparation,
)
from koawa_agent_v2.verification.environment_signals import (
    INDEX_UNREACHABLE,
    MISSING_DEPENDENCY,
    MISSING_EXECUTABLE,
    classify_environment_failure,
    parse_test_count,
)
from koawa_agent_v2.verification.finalization import TestEvidence
from koawa_agent_v2.verification.release_contracts import (
    ProfileReleaseContract,
    ReleaseMode,
)
from koawa_agent_v2.verification.runner import (
    CommandOutcome,
    CommandResult,
    CommandRunnerError,
)
from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry


def _result(
    outcome: CommandOutcome,
    stdout: str = "",
    stderr: str = "",
    *,
    backend: str = "host",
) -> CommandResult:
    return CommandResult(
        profile_id="unit",
        outcome=outcome,
        exit_code=0 if outcome is CommandOutcome.PASSED else 1,
        stdout=stdout,
        stderr=stderr,
        stdout_bytes=len(stdout),
        stderr_bytes=len(stderr),
        stdout_truncated=False,
        stderr_truncated=False,
        duration_ms=5,
        argv=("pytest",),
        timeout_seconds=10.0,
        backend=backend,
        immutable_image_id=(
            "sha256:" + "4" * 64 if backend == "docker" else None
        ),
        isolated_workspace=backend == "docker",
    )


class _ScriptedRunner:
    def __init__(self, contract: ProfileReleaseContract | None = None) -> None:
        self._queue: list[CommandResult] = []
        self._contract = contract

    def queue(self, result: CommandResult) -> None:
        self._queue.append(result)

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return ("unit",)

    def validate_profile(self, profile_id: str) -> None:
        if profile_id != "unit":
            raise CommandRunnerError("unknown_command_profile")

    def release_contract(self, profile_id: str):
        return self._contract if profile_id == "unit" else None

    def run(self, profile_id, *, progress_guard=None, execution_id=None):
        if not self._queue:
            return _result(CommandOutcome.PASSED)
        return self._queue.pop(0)


class EnvironmentSignalTest(unittest.TestCase):
    def test_missing_module_is_actionable(self) -> None:
        signal, names = classify_environment_failure(
            "", "ModuleNotFoundError: No module named 'hypothesis'"
        )
        self.assertEqual(MISSING_DEPENDENCY, signal)
        self.assertEqual(("hypothesis",), names)

    def test_pytest_import_error_is_actionable(self) -> None:
        self.assertEqual(
            (MISSING_DEPENDENCY, ("hypothesis",)),
            classify_environment_failure(
                "", "E   ModuleNotFoundError: No module named 'hypothesis'\r\n"
            ),
        )

    def test_dotted_module_is_not_a_distribution(self) -> None:
        # A dotted name is almost always a module inside the candidate tree:
        # that is a code fault, not an installable dependency.
        signal, names = classify_environment_failure(
            "ModuleNotFoundError: No module named 'app.utils'", ""
        )
        self.assertIsNone(signal)
        self.assertEqual((), names)

    def test_distribution_lines(self) -> None:
        signal, names = classify_environment_failure(
            "ERROR: No matching distribution found for pytest-xdist", ""
        )
        self.assertEqual(MISSING_DEPENDENCY, signal)
        self.assertEqual(("pytest-xdist",), names)

    def test_index_unreachable(self) -> None:
        signal, names = classify_environment_failure(
            "Could not fetch URL https://index.invalid/simple/: Connection refused",
            "",
        )
        self.assertEqual(INDEX_UNREACHABLE, signal)
        self.assertEqual((), names)

    def test_unsatisfied_version_does_not_claim_network_failure(self) -> None:
        self.assertEqual(
            (MISSING_DEPENDENCY, ("rich",)),
            classify_environment_failure(
                "ERROR: Could not find a version that satisfies the requirement rich",
                "",
            ),
        )

    def test_missing_executable_both_platforms(self) -> None:
        signal, names = classify_environment_failure(
            "grep: command not found", ""
        )
        self.assertEqual(MISSING_EXECUTABLE, signal)
        self.assertEqual(("grep",), names)
        signal, names = classify_environment_failure(
            "", "'py.test' is not recognized as an internal or external command"
        )
        self.assertEqual(MISSING_EXECUTABLE, signal)
        self.assertEqual(("py.test",), names)

    def test_assertion_failure_is_not_an_environment_signal(self) -> None:
        signal, names = classify_environment_failure(
            "FAILED tests/test_x.py::test_a - AssertionError: 1 != 2", ""
        )
        self.assertIsNone(signal)
        self.assertEqual((), names)

    def test_import_text_quoted_in_an_assertion_is_not_a_signal(self) -> None:
        self.assertEqual(
            (None, ()),
            classify_environment_failure(
                "AssertionError: expected ModuleNotFoundError: No module named 'rich'",
                "",
            ),
        )

    def test_names_are_bounded_and_deduplicated(self) -> None:
        text = "\n".join(
            "ModuleNotFoundError: No module named 'pkg%d'" % index
            for index in range(20)
        )
        _, names = classify_environment_failure(text, "")
        self.assertEqual(8, len(names))


class _RepoCase(unittest.TestCase):
    prefix = "koawa-dep-"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix=self.prefix)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name, "repo")
        self.root.mkdir()
        self.run_id = uuid4()
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(
            ("git", "-C", str(self.root), "init", "-q"),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _invoke(self, registry, name: str, arguments: dict, *, run_id=None) -> dict:
        model_turn_id = uuid4()
        call = ToolCallItem(0, "i", "c", name, json.dumps(arguments))
        context = ToolExecutionContext(
            run_id or self.run_id, model_turn_id, 1, ModelCallRef(model_turn_id, "c")
        )
        return json.loads(registry.execute(call, context=context).content)


class TestCountTest(unittest.TestCase):
    def test_pytest_count(self) -> None:
        self.assertEqual(10, parse_test_count("collected 10 items\n"))

    def test_unittest_count(self) -> None:
        self.assertEqual(3, parse_test_count("Ran 3 tests in 0.100s\n"))

    def test_no_count_is_unknown(self) -> None:
        self.assertIsNone(parse_test_count("all good\n"))
        self.assertIsNone(parse_test_count(""))

    def test_larger_count_does_not_hide_smaller_report(self) -> None:
        self.assertEqual(2, parse_test_count("collected 99 items\nRan 2 tests in 0.01s\n"))

    def test_passing_process_with_changed_inputs_is_not_accepted_evidence(self) -> None:
        result = replace(_result(CommandOutcome.PASSED), inputs_changed=True)
        self.assertTrue(result.passed)
        self.assertFalse(TestEvidence(0, result).accepted)


class EnvironmentReceiptTest(_RepoCase):
    def _registry(self, *, mode=ReleaseMode.DETAILED, fields=None, rules=()):
        contract = ProfileReleaseContract(
            profile_id="unit",
            profile_version="v1",
            audience_policy_ref="aud-v1" if mode is ReleaseMode.DETAILED else None,
            environment_ref="env-v1" if mode is ReleaseMode.DETAILED else None,
            release_mode=mode,
            release_fields=fields,
            producer_rules=rules,
        )
        runner = _ScriptedRunner(contract)
        registry = build_verified_coding_tool_registry(
            self.root,
            command_runner=runner,
            required_test_profiles=("unit",),
            dependency_scope=DependencyScope(
                allowed_packages=("hypothesis", "pytest-*"), allow_install=True
            ),
        )
        self.addCleanup(registry.close)
        return runner, registry

    def test_missing_dependency_is_not_completed(self) -> None:
        runner, registry = self._registry()
        runner.queue(
            _result(
                CommandOutcome.FAILED,
                stderr="ModuleNotFoundError: No module named 'hypothesis'",
            )
        )
        receipt = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertEqual("not_completed", receipt["execution_status"])
        self.assertEqual("not_evaluated", receipt["verdict"])
        self.assertEqual("repair_safe_environment", receipt["next_action"])
        self.assertEqual(["hypothesis"], receipt["missing_dependencies"])
        self.assertEqual("missing_dependency", receipt["environment_signal"])

    def test_real_assertion_failure_stays_failed(self) -> None:
        runner, registry = self._registry()
        runner.queue(
            _result(
                CommandOutcome.FAILED,
                stdout="FAILED tests/test_x.py - AssertionError: 1 != 2",
            )
        )
        receipt = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertEqual("completed", receipt["execution_status"])
        self.assertEqual("failed", receipt["verdict"])
        self.assertNotIn("missing_dependencies", receipt)

    def test_limited_mode_still_sees_the_missing_set(self) -> None:
        runner, registry = self._registry(mode=ReleaseMode.LIMITED)
        runner.queue(
            _result(
                CommandOutcome.FAILED,
                stderr="ModuleNotFoundError: No module named 'hypothesis'",
            )
        )
        receipt = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertEqual("not_completed", receipt["execution_status"])
        self.assertEqual(["hypothesis"], receipt["missing_dependencies"])
        # Limited carries structured diagnostics but never a raw body.
        self.assertNotIn("diagnostics_excerpt", receipt["diagnostic"])

    def test_unapproved_log_identifiers_never_enter_completed_receipt(self) -> None:
        runner, registry = self._registry()
        runner.queue(_result(
            CommandOutcome.FAILED,
            stderr=(
                "ModuleNotFoundError: No module named 'internal_label'\n"
                "ERROR: No matching distribution found for pytest-confidential\n"
                "private diagnostic details\n"
            ),
            backend="docker",
        ))
        tool_result = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertEqual("missing_dependency", tool_result["environment_signal"])
        self.assertNotIn("missing_dependencies", tool_result)
        self.assertNotIn("internal_label", json.dumps(tool_result))
        self.assertNotIn("private diagnostic details", json.dumps(tool_result))

    def test_field_and_producer_rules_gate_names_and_the_request(self) -> None:
        for fields, rules in (
            (("exit_code",), ()),
            (None, ("exit_code@runtime.safe_diagnostics",)),
        ):
            with self.subTest(fields=fields, rules=rules):
                runner, registry = self._registry(fields=fields, rules=rules)
                runner.queue(_result(
                    CommandOutcome.FAILED,
                    stderr="ModuleNotFoundError: No module named 'hypothesis'",
                ))
                receipt = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
                self.assertNotIn("missing_dependencies", receipt)
                request = self._invoke(
                    registry, "request_dependency_preparation", {"profile_id": "unit"}
                )
                self.assertEqual("dependency_preparation_unavailable", request["error"]["code"])

    def test_local_only_discloses_nothing(self) -> None:
        runner, registry = self._registry(mode=ReleaseMode.LOCAL_ONLY)
        runner.queue(
            _result(
                CommandOutcome.FAILED,
                stderr="ModuleNotFoundError: No module named 'hypothesis'",
            )
        )
        receipt = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertEqual("withheld", receipt["execution_status"])
        self.assertNotIn("missing_dependencies", receipt)
        self.assertNotIn("environment_signal", receipt)
        request = self._invoke(
            registry, "request_dependency_preparation", {"profile_id": "unit"}
        )
        self.assertEqual("dependency_preparation_unavailable", request["error"]["code"])
        self.assertNotIn("hypothesis", json.dumps(request))

    def test_receipt_declares_evidence_isolation(self) -> None:
        runner, registry = self._registry()
        runner.queue(_result(CommandOutcome.PASSED, stdout="collected 3 items\n"))
        receipt = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertEqual("none", receipt["isolation"])

    def test_declared_environment_is_not_proven_by_host_evidence(self) -> None:
        runner, registry = self._registry()
        runner.queue(
            _result(
                CommandOutcome.FAILED,
                stdout="FAILED tests/test_x.py - AssertionError: 1 != 2",
                backend="host",
            )
        )
        receipt = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertTrue(receipt["environment_unproven"])
        # detailed downgrades to restricted without a container proof.
        self.assertEqual("restricted", receipt["diagnostic_status"])
        self.assertNotIn("diagnostics_excerpt", receipt["diagnostic"])

    def test_container_evidence_keeps_environment_proof(self) -> None:
        runner, registry = self._registry()
        runner.queue(
            _result(
                CommandOutcome.FAILED,
                stdout="FAILED tests/test_x.py - AssertionError: 1 != 2",
                backend="docker",
            )
        )
        receipt = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertNotIn("environment_unproven", receipt)
        self.assertEqual("available", receipt["diagnostic_status"])
        self.assertEqual("container", receipt["isolation"])

    def test_collection_regression_is_not_a_pass(self) -> None:
        runner, registry = self._registry()
        runner.queue(
            _result(CommandOutcome.PASSED, stdout="collected 10 items\n")
        )
        first = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertEqual("passed", first["verdict"])

        runner.queue(
            _result(CommandOutcome.PASSED, stdout="collected 2 items\n")
        )
        second = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertEqual("not_completed", second["execution_status"])
        self.assertEqual("not_evaluated", second["verdict"])
        self.assertEqual(
            {"expected": 10, "observed": 2},
            second["test_collection_regression"],
        )
        final = self._invoke(registry, "finalize_task", {})
        self.assertEqual("required_test_collection_regressed", final["error"]["code"])

    def test_initial_zero_tests_are_not_a_pass(self) -> None:
        runner, registry = self._registry()
        runner.queue(_result(CommandOutcome.PASSED, stdout="Ran 0 tests in 0.01s\n"))
        receipt = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertEqual("not_evaluated", receipt["verdict"])
        self.assertEqual(0, receipt["test_collection_regression"]["observed"])

    def test_missing_count_does_not_replace_counted_evidence(self) -> None:
        runner, registry = self._registry()
        runner.queue(_result(CommandOutcome.PASSED, stdout="collected 8 items\n"))
        self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        runner.queue(_result(CommandOutcome.PASSED, stdout="done\n"))
        receipt = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertEqual("not_evaluated", receipt["verdict"])
        self.assertEqual(
            {"expected": 8, "observed": None}, receipt["test_collection_regression"]
        )

    def test_first_observation_sets_the_baseline(self) -> None:
        runner, registry = self._registry()
        runner.queue(_result(CommandOutcome.PASSED, stdout="collected 2 items\n"))
        receipt = self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        self.assertEqual("passed", receipt["verdict"])


class PreparationToolTest(_RepoCase):
    prefix = "koawa-prep-"

    def _registry(self, *, allow_install: bool):
        contract = ProfileReleaseContract(
            profile_id="unit",
            profile_version="v1",
            release_mode=ReleaseMode.DETAILED,
            audience_policy_ref="aud-v1",
            environment_ref="env-v1",
        )
        runner = _ScriptedRunner(contract)
        scope = DependencyScope(
            allowed_packages=("hypothesis", "rich"), allow_install=allow_install,
        )
        registry = build_verified_coding_tool_registry(
            self.root,
            command_runner=runner,
            required_test_profiles=("unit",),
            dependency_scope=scope,
        )
        self.addCleanup(registry.close)
        return runner, registry

    def _fail_with_missing(self, registry, runner) -> None:
        runner.queue(
            _result(
                CommandOutcome.FAILED,
                stderr="ModuleNotFoundError: No module named 'hypothesis'",
            )
        )
        self._invoke(registry, "run_test_profile", {"profile_id": "unit"})

    def test_request_without_observed_failure_is_refused(self) -> None:
        _, registry = self._registry(allow_install=True)
        reply = self._invoke(
            registry, "request_dependency_preparation", {"profile_id": "unit"}
        )
        self.assertEqual("no_environment_failure_observed", reply["error"]["code"])

    def test_free_form_name_is_refused(self) -> None:
        runner, registry = self._registry(allow_install=True)
        self._fail_with_missing(registry, runner)
        reply = self._invoke(
            registry,
            "request_dependency_preparation",
            {"profile_id": "unit", "packages": ["requests"]},
        )
        self.assertEqual("unexpected_dependency_request", reply["error"]["code"])

    def test_install_disabled_requires_authorization(self) -> None:
        runner, registry = self._registry(allow_install=False)
        self._fail_with_missing(registry, runner)
        reply = self._invoke(
            registry, "request_dependency_preparation", {"profile_id": "unit"}
        )
        self.assertEqual("requires_authorization", reply["status"])
        self.assertEqual("human_required", reply["next_action"])
        self.assertTrue(reply["human_required"])

    def test_ready_plan_without_executor_hands_off(self) -> None:
        runner, registry = self._registry(allow_install=True)
        self._fail_with_missing(registry, runner)
        reply = self._invoke(
            registry, "request_dependency_preparation", {"profile_id": "unit"}
        )
        self.assertEqual("executor_unavailable", reply["status"])
        self.assertEqual(["hypothesis"], reply["install"])
        self.assertEqual("human_required", reply["next_action"])

    def test_handoff_is_idempotent(self) -> None:
        runner, registry = self._registry(allow_install=True)
        self._fail_with_missing(registry, runner)
        first = self._invoke(
            registry, "request_dependency_preparation", {"profile_id": "unit"}
        )
        second = self._invoke(
            registry, "request_dependency_preparation", {"profile_id": "unit"}
        )
        self.assertEqual("executor_unavailable", first["status"])
        self.assertIsNone(first["environment_ref"])
        self.assertEqual("human_required", first["next_action"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first, {k: v for k, v in second.items() if k != "idempotent_replay"})

    def test_observation_is_not_visible_to_other_runs(self) -> None:
        runner, registry = self._registry(allow_install=True)
        self._fail_with_missing(registry, runner)
        reply = self._invoke(
            registry, "request_dependency_preparation", {"profile_id": "unit"},
            run_id=uuid4(),
        )
        self.assertEqual("no_environment_failure_observed", reply["error"]["code"])
        self.assertNotIn("hypothesis", json.dumps(reply))

    def test_distinct_plan_budget_stops_third_but_replay_still_works(self) -> None:
        runner, registry = self._registry(allow_install=True)
        runner.queue(_result(
            CommandOutcome.FAILED,
            stderr=(
                "ModuleNotFoundError: No module named 'hypothesis'\n"
                "ModuleNotFoundError: No module named 'rich'"
            ),
        ))
        self._invoke(registry, "run_test_profile", {"profile_id": "unit"})
        for name in ("hypothesis", "rich"):
            reply = self._invoke(
                registry, "request_dependency_preparation",
                {"profile_id": "unit", "packages": [name]},
            )
            self.assertEqual("executor_unavailable", reply["status"])
        denied = self._invoke(
            registry, "request_dependency_preparation", {"profile_id": "unit"}
        )
        self.assertEqual("dependency_preparation_budget_exceeded", denied["error"]["code"])
        replay = self._invoke(
            registry, "request_dependency_preparation",
            {"profile_id": "unit", "packages": ["rich", "rich"]},
        )
        self.assertTrue(replay["idempotent_replay"])


class DependencyScopeDecisionTest(unittest.TestCase):
    def test_ready_when_authorised(self) -> None:
        decision = plan_preparation(
            ("hypothesis",),
            DependencyScope(allowed_packages=("hypothesis",), allow_install=True),
        )
        self.assertEqual(PREP_READY, decision.status)
        self.assertEqual(("hypothesis",), decision.install)

    def test_out_of_scope_requires_authorization(self) -> None:
        decision = plan_preparation(
            ("requests",),
            DependencyScope(allowed_packages=("hypothesis",), allow_install=True),
        )
        self.assertEqual(PREP_REQUIRES_AUTHORIZATION, decision.status)
        self.assertEqual("out_of_scope", decision.reason)


class ProtectedTestSurfaceConfigTest(unittest.TestCase):
    def _write(self, document: dict) -> Path:
        temporary = tempfile.TemporaryDirectory(prefix="koawa-cfg-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        (directory / "repo").mkdir()
        path = Path(directory, "config.json")
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def _document(self) -> dict:
        return {
            "repo": "repo",
            "db": "agent.sqlite3",
            "provider": {
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key_env": "SF_CodingAgentTestKey",
                "model": "Qwen/Qwen3-8B",
            },
            "sandbox": {"runner": "host", "allow_unisolated_execution": True},
            "test_profiles": [
                {
                    "profile_id": "unit",
                    "argv": ["/usr/local/bin/python", "-m", "unittest"],
                }
            ],
            "policy": {},
        }

    def test_test_surface_is_protected_by_default(self) -> None:
        config = load_runtime_config(self._write(self._document()))
        self.assertIn("tests/", config.policy.protected_paths)
        self.assertIn("pyproject.toml", config.policy.protected_paths)

    def test_custom_protected_paths_extend_required_defaults(self) -> None:
        document = self._document()
        document["policy"]["protected_paths"] = ["quality/"]
        config = load_runtime_config(self._write(document))
        self.assertIn("quality/", config.policy.verification_protected_paths)
        self.assertIn("tests/", config.policy.verification_protected_paths)

    def test_empty_protection_with_required_tests_is_rejected(self) -> None:
        document = self._document()
        document["policy"]["protected_paths"] = []
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(self._write(document))
        self.assertEqual(
            "verification_requires_protected_tests", raised.exception.code
        )

    def test_host_runner_requires_explicit_opt_in(self) -> None:
        document = self._document()
        document["sandbox"] = {"runner": "host"}
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(self._write(document))
        self.assertEqual(
            "unisolated_execution_forbidden", raised.exception.code
        )

    def test_strings_do_not_count_as_maintainer_consent(self) -> None:
        document = self._document()
        document["sandbox"]["allow_unisolated_execution"] = "true"
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(self._write(document))
        self.assertEqual("invalid_sandbox_config", raised.exception.code)
        document["sandbox"]["allow_unisolated_execution"] = True
        document["policy"]["dependency_allow_install"] = "false"
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(self._write(document))
        self.assertEqual("invalid_dependency_scope", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
