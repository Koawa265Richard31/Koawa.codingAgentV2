from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from collections import deque
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.ledger import LedgerExecutor, READ_ONLY_PROFILE, ToolLedgerStore
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.verification.finalization import VerificationError, VerificationLimits
from koawa_agent_v2.verification.runner import (
    CommandOutcome,
    CommandResult,
    CommandRunnerError,
)
from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry


def _git(root: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    if executable is None:
        raise unittest.SkipTest("git is not installed")
    subprocess.run(
        (executable, "-C", str(root), *arguments),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def _call(name: str, arguments: dict[str, object], call_id: str) -> ToolCallItem:
    return ToolCallItem(
        0,
        f"item-{call_id}",
        call_id,
        name,
        json.dumps(arguments, separators=(",", ":")),
    )


def _context(
    run_id: UUID,
    call_id: str,
    *,
    model_turn_id: UUID | None = None,
    turn_id: UUID | None = None,
    turn_version: int | None = None,
) -> ToolExecutionContext:
    model_turn_id = model_turn_id or uuid4()
    return ToolExecutionContext(
        run_id,
        model_turn_id,
        1,
        ModelCallRef(model_turn_id, call_id),
        turn_id=turn_id,
        turn_version=turn_version,
    )


class _ScriptedRunner:
    def __init__(self, outcomes: dict[str, list[CommandOutcome]]) -> None:
        self._outcomes = {key: deque(value) for key, value in outcomes.items()}
        self.calls: list[str] = []

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._outcomes))

    def validate_profile(self, profile_id: str) -> None:
        if profile_id not in self._outcomes:
            raise CommandRunnerError("unknown_command_profile")

    def run(self, profile_id: str, *, progress_guard=None, execution_id=None):
        del execution_id
        self.validate_profile(profile_id)
        if progress_guard is not None:
            progress_guard()
        self.calls.append(profile_id)
        outcome = self._outcomes[profile_id].popleft()
        return CommandResult(
            profile_id=profile_id,
            outcome=outcome,
            exit_code=0 if outcome is CommandOutcome.PASSED else 1,
            stdout="",
            stderr="",
            stdout_bytes=0,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=1,
            argv=("trusted-test", profile_id),
            timeout_seconds=10.0,
            backend="docker",
            immutable_image_id="sha256:" + "1" * 64,
            profile_digest="2" * 64,
        )


class D5RequiredProfilesTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="koawa-d5-required-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "d5-required@example.invalid")
        _git(self.root, "config", "user.name", "D5 Required Fixture")
        _git(self.root, "config", "core.fsmonitor", "false")
        _git(self.root, "config", "core.autocrlf", "false")
        _git(self.root, "config", "core.filemode", "false")
        _git(self.root, "add", "--all")
        _git(self.root, "commit", "-qm", "fixture baseline")

    def _registry(self, outcomes, *, required=("a", "b"), max_runs=8):
        runner = _ScriptedRunner(outcomes)
        registry = build_verified_coding_tool_registry(
            self.root,
            command_runner=runner,
            required_test_profiles=required,
            verification_limits=VerificationLimits(max_test_runs=max_runs),
        )
        self.addCleanup(registry.close)
        return registry, runner

    def _run_test(self, registry, run_id, profile_id, suffix=""):
        return registry.execute(
            _call("run_test_profile", {"profile_id": profile_id}, f"{profile_id}{suffix}"),
            context=_context(run_id, f"{profile_id}{suffix}"),
        )

    def _finalize(self, registry, run_id):
        return registry.execute(
            _call("finalize_task", {}, "final"),
            context=_context(run_id, "final"),
        )

    def _error(self, result):
        self.assertTrue(result.is_error, result.content)
        return json.loads(result.content)["error"]

    def _patch_and_git_evidence(self, registry, run_id):
        base = hashlib.sha256((self.root / "app.py").read_bytes()).hexdigest()
        patch_json = json.dumps(
            {
                "schema_version": 1,
                "changes": [{
                    "operation": "update",
                    "path": "app.py",
                    "base_sha256": base,
                    "hunks": [{
                        "old_start": 1,
                        "old_lines": ["value = 1"],
                        "new_lines": ["value = 2"],
                    }],
                }],
            },
            separators=(",", ":"),
        )
        patch = registry.execute(
            _call("apply_patch", {"patch_json": patch_json}, "patch"),
            context=_context(run_id, "patch"),
        )
        self.assertFalse(patch.is_error, patch.content)

    def _record_git_evidence(self, registry, run_id):
        for name in ("git_status", "git_diff"):
            result = registry.execute(
                _call(name, {}, name), context=_context(run_id, name)
            )
            self.assertFalse(result.is_error, result.content)

    def test_required_failure_cannot_be_replaced_by_other_profile_pass(self):
        registry, _ = self._registry({
            "a": [CommandOutcome.FAILED],
            "b": [CommandOutcome.PASSED],
        })
        run_id = uuid4()
        self._run_test(registry, run_id, "a")
        self._run_test(registry, run_id, "b")
        error = self._error(self._finalize(registry, run_id))
        self.assertEqual("required_test_profile_not_passing", error["code"])
        self.assertEqual("a", error["detail"])

    def test_missing_required_profile_is_rejected(self):
        registry, _ = self._registry({
            "a": [CommandOutcome.PASSED],
            "b": [CommandOutcome.PASSED],
        })
        run_id = uuid4()
        self._run_test(registry, run_id, "a")
        error = self._error(self._finalize(registry, run_id))
        self.assertEqual("required_test_profile_not_run", error["code"])
        self.assertEqual("b", error["detail"])

    def test_patch_invalidates_all_required_profile_evidence(self):
        registry, _ = self._registry({
            "a": [CommandOutcome.PASSED],
            "b": [CommandOutcome.PASSED],
        })
        run_id = uuid4()
        self._run_test(registry, run_id, "a")
        self._run_test(registry, run_id, "b")
        self._patch_and_git_evidence(registry, run_id)
        error = self._error(self._finalize(registry, run_id))
        self.assertEqual("required_test_profile_stale", error["code"])
        self.assertEqual("a", error["detail"])

    def test_latest_pass_replaces_same_profile_failure(self):
        registry, _ = self._registry({
            "a": [CommandOutcome.FAILED, CommandOutcome.PASSED],
            "b": [CommandOutcome.PASSED],
        })
        run_id = uuid4()
        self._patch_and_git_evidence(registry, run_id)
        self._run_test(registry, run_id, "a", "-fail")
        self._run_test(registry, run_id, "a", "-pass")
        self._run_test(registry, run_id, "b")
        self._record_git_evidence(registry, run_id)
        final = self._finalize(registry, run_id)
        self.assertFalse(final.is_error, final.content)
        report = json.loads(final.content)
        self.assertNotIn("test", report)
        a = next(item for item in report["required_tests"] if item["profile_id"] == "a")
        self.assertEqual("passed", a["latest"]["outcome"])
        self.assertEqual(1, a["historical_failure_count"])

    def test_latest_timeout_blocks_even_after_historical_pass(self):
        registry, _ = self._registry({
            "a": [CommandOutcome.PASSED, CommandOutcome.TIMED_OUT],
            "b": [CommandOutcome.PASSED],
        })
        run_id = uuid4()
        self._run_test(registry, run_id, "a", "-pass")
        self._run_test(registry, run_id, "a", "-timeout")
        self._run_test(registry, run_id, "b")
        error = self._error(self._finalize(registry, run_id))
        self.assertEqual("required_test_profile_not_passing", error["code"])
        self.assertEqual("a", error["detail"])

    def test_optional_failure_is_disclosed_but_does_not_block(self):
        registry, _ = self._registry(
            {
                "a": [CommandOutcome.PASSED],
                "b": [CommandOutcome.PASSED],
                "c": [CommandOutcome.FAILED],
            },
            required=("a", "b"),
        )
        run_id = uuid4()
        self._patch_and_git_evidence(registry, run_id)
        for profile_id in ("a", "b", "c"):
            self._run_test(registry, run_id, profile_id)
        self._record_git_evidence(registry, run_id)
        final = self._finalize(registry, run_id)
        self.assertFalse(final.is_error, final.content)
        report = json.loads(final.content)
        self.assertEqual("failed", report["optional_tests"][0]["latest"]["outcome"])
        self.assertEqual(1, report["optional_tests"][0]["historical_failure_count"])

    def test_multi_profile_registry_cannot_omit_required_contract(self):
        with self.assertRaises(VerificationError) as raised:
            build_verified_coding_tool_registry(
                self.root,
                command_runner=_ScriptedRunner({
                    "a": [CommandOutcome.PASSED],
                    "b": [CommandOutcome.PASSED],
                }),
            )
        self.assertEqual("required_test_profiles_required", raised.exception.code)

    def test_durable_result_reuse_does_not_create_fresh_verification_evidence(self):
        database = self.root.parent / f"{self.root.name}-ledger.sqlite3"
        store = SqliteEventStore(database)
        runtime = ThreadRuntime(store, actor="d5-required-test")
        thread = runtime.create_thread("reuse")
        queued = runtime.create_turn(
            thread.thread_id, "test", expected_thread_version=thread.version
        )
        running = runtime.start_turn(queued.turn_id, queued.version)
        ledger = ToolLedgerStore(store)
        model_turn_id = uuid4()
        call = _call("run_test_profile", {"profile_id": "unit"}, "test")
        context = _context(
            running.current_run_id,
            call.call_id,
            model_turn_id=model_turn_id,
            turn_id=running.turn_id,
            turn_version=running.version,
        )

        first_runner = _ScriptedRunner({"unit": [CommandOutcome.PASSED]})
        with build_verified_coding_tool_registry(
            self.root, command_runner=first_runner
        ) as first_registry:
            profiles = {item.name: READ_ONLY_PROFILE for item in first_registry.definitions()}
            first = LedgerExecutor(first_registry, ledger, profiles).execute(
                call, context=context
            )
            self.assertFalse(first.is_error, first.content)
        self.assertEqual(["unit"], first_runner.calls)

        second_runner = _ScriptedRunner({"unit": [CommandOutcome.PASSED]})
        with build_verified_coding_tool_registry(
            self.root, command_runner=second_runner
        ) as second_registry:
            profiles = {item.name: READ_ONLY_PROFILE for item in second_registry.definitions()}
            second_executor = LedgerExecutor(second_registry, ledger, profiles)
            reused = second_executor.execute(call, context=context)
            self.assertFalse(reused.is_error, reused.content)
            final_call = _call("finalize_task", {}, "final")
            final_context = _context(
                running.current_run_id,
                final_call.call_id,
                turn_id=running.turn_id,
                turn_version=running.version,
            )
            error = self._error(
                second_executor.execute(final_call, context=final_context)
            )
        self.assertEqual([], second_runner.calls)
        self.assertEqual("required_test_profile_not_run", error["code"])


if __name__ == "__main__":
    unittest.main()
