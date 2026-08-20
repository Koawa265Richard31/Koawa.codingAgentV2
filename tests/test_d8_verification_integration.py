from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import (
    AgentLoopCancelled,
    ToolExecutionContext,
    ToolExecutionResult,
)
from koawa_agent_v2.ledger import (
    LedgerExecutor,
    READ_ONLY_PROFILE,
    ToolExecutionState,
    ToolLedgerStore,
)
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem, ToolDefinition
from koawa_agent_v2.tools.errors import ToolConfigurationError
from koawa_agent_v2.verification.runner import (
    CommandOutcome,
    CommandProfile,
    CommandResult,
    CommandRunnerError,
    RepositoryTrust,
)
from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry


IMAGE_ID = "sha256:" + "1" * 64
PROFILE_DIGEST = "2" * 64
ALLOCATION_ID = UUID("33333333-3333-4333-8333-333333333333")
CONTAINER_ID = "4" * 64
EXECUTION_ID = UUID("55555555-5555-4555-8555-555555555555")


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
        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    )


def _context(run_id: UUID, call_id: str, execution_id: UUID) -> ToolExecutionContext:
    model_turn_id = uuid4()
    return ToolExecutionContext(
        run_id,
        model_turn_id,
        1,
        ModelCallRef(model_turn_id, call_id),
        execution_id=execution_id,
    )


class _FakeDockerRunner:
    def __init__(self) -> None:
        self.execution_ids: list[UUID | None] = []

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return ("unit",)

    def validate_profile(self, profile_id: str) -> None:
        if profile_id != "unit":
            raise CommandRunnerError("unknown_command_profile")

    def run(
        self,
        profile_id: str,
        *,
        progress_guard=None,
        execution_id: UUID | None = None,
    ) -> CommandResult:
        self.validate_profile(profile_id)
        if progress_guard is not None:
            progress_guard()
        self.execution_ids.append(execution_id)
        return CommandResult(
            profile_id=profile_id,
            outcome=CommandOutcome.PASSED,
            exit_code=0,
            stdout="docker tests passed\n",
            stderr="",
            stdout_bytes=20,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=12,
            argv=("python", "-m", "unittest"),
            timeout_seconds=30.0,
            backend="docker",
            immutable_image_id=IMAGE_ID,
            profile_digest=PROFILE_DIGEST,
            allocation_id=ALLOCATION_ID,
            container_id=CONTAINER_ID,
        )


PROBE = ToolDefinition(
    "cancel_probe",
    "D8 cancellation propagation probe",
    '{"type":"object","properties":{}}',
)


class _CancellingDelegate:
    def __init__(self) -> None:
        self.cancelled = AgentLoopCancelled()

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (PROBE,)

    def execute(
        self,
        call: ToolCallItem,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del call, context
        raise self.cancelled


class D8VerificationIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="koawa-d8-verification-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "app.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "d8@example.invalid")
        _git(self.root, "config", "user.name", "D8 Fixture")
        _git(self.root, "config", "core.fsmonitor", "false")
        _git(self.root, "config", "core.autocrlf", "false")
        _git(self.root, "config", "core.filemode", "false")
        _git(self.root, "add", "--all")
        _git(self.root, "commit", "-qm", "fixture baseline")

    def test_untrusted_repository_accepts_injected_docker_runner_and_persists_evidence(
        self,
    ) -> None:
        runner = _FakeDockerRunner()
        with build_verified_coding_tool_registry(
            self.root,
            command_runner=runner,
            repository_trust=RepositoryTrust.UNTRUSTED,
        ) as registry:
            command, report = self._complete_verification(registry, EXECUTION_ID)

        self.assertEqual([EXECUTION_ID], runner.execution_ids)
        for payload in (command, report["test"]):
            self.assertEqual("docker", payload["backend"])
            self.assertEqual(IMAGE_ID, payload["immutable_image_id"])
            self.assertEqual(PROFILE_DIGEST, payload["profile_digest"])
            self.assertEqual(str(ALLOCATION_ID), payload["allocation_id"])
            self.assertEqual(CONTAINER_ID, payload["container_id"])
        self.assertNotIn("warning", report)

    def test_host_bootstrap_runner_remains_compatible_and_warns(self) -> None:
        profile = CommandProfile(
            "unit",
            (str(Path(sys.executable).resolve()), "-c", "raise SystemExit(0)"),
            timeout_seconds=10,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )
        with build_verified_coding_tool_registry(
            self.root,
            command_profiles=(profile,),
            repository_trust=RepositoryTrust.BUILTIN_FIXTURE,
        ) as registry:
            command, report = self._complete_verification(registry, EXECUTION_ID)

        self.assertEqual("host", command["backend"])
        self.assertEqual("host", report["test"]["backend"])
        self.assertEqual(
            "host_runner_dev_only_until_d8_container_sandbox",
            report["warning"],
        )

    def test_injected_runner_and_host_profiles_are_ambiguous(self) -> None:
        profile = CommandProfile(
            "unit",
            (str(Path(sys.executable).resolve()), "-c", "raise SystemExit(0)"),
        )
        with self.assertRaises(ToolConfigurationError) as caught:
            build_verified_coding_tool_registry(
                self.root,
                command_profiles=(profile,),
                command_runner=_FakeDockerRunner(),
                repository_trust=RepositoryTrust.UNTRUSTED,
            )
        self.assertEqual(
            "ambiguous_command_runner_configuration", caught.exception.code
        )

    def test_ledger_propagates_cancellation_identity_without_marking_unknown(self) -> None:
        database = self.root / "ledger.sqlite3"
        event_store = SqliteEventStore(database)
        runtime = ThreadRuntime(event_store, actor="d8-test")
        thread = runtime.create_thread("d8-cancel")
        queued = runtime.create_turn(
            thread.thread_id,
            "cancel sandbox command",
            expected_thread_version=thread.version,
        )
        running = runtime.start_turn(queued.turn_id, queued.version)
        self.assertIsNotNone(running.current_run_id)
        ledger = ToolLedgerStore(event_store)
        delegate = _CancellingDelegate()
        executor = LedgerExecutor(
            delegate,
            ledger,
            {"cancel_probe": READ_ONLY_PROFILE},
        )
        model_turn_id = uuid4()
        call = ToolCallItem(
            0,
            "item-cancel",
            "call-cancel",
            "cancel_probe",
            "{}",
        )
        context = ToolExecutionContext(
            running.current_run_id,
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, call.call_id),
            turn_id=running.turn_id,
            turn_version=running.version,
        )

        with self.assertRaises(AgentLoopCancelled) as caught:
            executor.execute(call, context=context)
        self.assertIs(delegate.cancelled, caught.exception)
        record = ledger.load_for_call(
            running.turn_id,
            model_turn_id,
            call.call_id,
        )
        self.assertIsNotNone(record)
        self.assertEqual(ToolExecutionState.CLAIMED, record.state)
        self.assertIsNone(record.result)
        self.assertIsNone(record.unknown_reason)

    def _complete_verification(self, registry, execution_id: UUID):
        run_id = uuid4()
        base_digest = hashlib.sha256((self.root / "app.py").read_bytes()).hexdigest()
        patch_json = json.dumps(
            {
                "schema_version": 1,
                "changes": [
                    {
                        "operation": "update",
                        "path": "app.py",
                        "base_sha256": base_digest,
                        "hunks": [
                            {
                                "old_start": 2,
                                "old_lines": ["    return 1"],
                                "new_lines": ["    return 2"],
                            }
                        ],
                    }
                ],
            },
            separators=(",", ":"),
        )
        patch = registry.execute(
            _call("apply_patch", {"patch_json": patch_json}, "patch"),
            context=_context(run_id, "patch", execution_id),
        )
        self.assertFalse(patch.is_error, patch.content)
        test = registry.execute(
            _call("run_test_profile", {"profile_id": "unit"}, "test"),
            context=_context(run_id, "test", execution_id),
        )
        self.assertFalse(test.is_error, test.content)
        status = registry.execute(
            _call("git_status", {}, "status"),
            context=_context(run_id, "status", execution_id),
        )
        self.assertFalse(status.is_error, status.content)
        diff = registry.execute(
            _call("git_diff", {}, "diff"),
            context=_context(run_id, "diff", execution_id),
        )
        self.assertFalse(diff.is_error, diff.content)
        final = registry.execute(
            _call("finalize_task", {}, "final"),
            context=_context(run_id, "final", execution_id),
        )
        self.assertFalse(final.is_error, final.content)
        return json.loads(test.content), json.loads(final.content)


if __name__ == "__main__":
    unittest.main()
