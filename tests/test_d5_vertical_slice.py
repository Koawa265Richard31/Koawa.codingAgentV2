from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import unittest
from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from koawa_agent_v2.execution.loop import AgentLoop, AgentLoopError, ToolExecutionContext
from koawa_agent_v2.verification.finalization import VerificationLimits
from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelCallRef,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    OutputKind,
    StreamHeader,
    ToolCallItem,
    ToolResultMessage,
    TurnCompleted,
    TurnStarted,
    UserMessage,
)
from koawa_agent_v2.verification.runner import (
    CommandOutcome,
    CommandProfile,
    CommandRunnerError,
    RepositoryTrust,
    TrustedCommandRunner,
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


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "d5@example.invalid")
    _git(root, "config", "user.name", "D5 Fixture")
    _git(root, "config", "core.fsmonitor", "false")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "core.filemode", "false")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "fixture baseline")


def _profile(*, timeout: float = 30.0, max_stdout: int = 100_000) -> CommandProfile:
    return CommandProfile(
        "unit",
        (
            str(Path(sys.executable).resolve()),
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ),
        timeout_seconds=timeout,
        max_stdout_bytes=max_stdout,
        max_stderr_bytes=100_000,
    )


def _call(name: str, arguments: dict[str, object], call_id: str) -> ToolCallItem:
    return ToolCallItem(
        0,
        f"item-{call_id}",
        call_id,
        name,
        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    )


def _context(call_id: str = "direct") -> ToolExecutionContext:
    model_turn_id = uuid4()
    return ToolExecutionContext(
        uuid4(), model_turn_id, 1, ModelCallRef(model_turn_id, call_id)
    )


def _header(request: ModelRequest, response_id: str, sequence: int) -> StreamHeader:
    return StreamHeader(
        request.model_turn_id,
        request.provider,
        response_id,
        sequence,
        sequence,
    )


def _stream(
    request: ModelRequest,
    item: ToolCallItem | AssistantTextItem,
    finish: FinishReason,
    response_id: str,
) -> tuple[ModelStreamEvent, ...]:
    kind = OutputKind.TOOL_CALL if isinstance(item, ToolCallItem) else OutputKind.ASSISTANT_TEXT
    if isinstance(item, ToolCallItem):
        started = ItemStarted(
            _header(request, response_id, 1),
            0,
            item.item_id,
            kind,
            item.call_id,
            item.name,
        )
    else:
        started = ItemStarted(_header(request, response_id, 1), 0, item.item_id, kind)
    turn = ModelTurn(
        request.model_turn_id,
        request.provider,
        request.model,
        response_id,
        (item,),
        finish,
    )
    return (
        TurnStarted(_header(request, response_id, 0), request.model),
        started,
        ItemCompleted(_header(request, response_id, 2), item),
        TurnCompleted(_header(request, response_id, 3), turn),
    )


def _result(request: ModelRequest, call_id: str) -> tuple[dict[str, object], bool]:
    item = next(
        value
        for value in request.input_items
        if isinstance(value, ToolResultMessage) and value.call_ref.call_id == call_id
    )
    return json.loads(item.content), item.is_error


class _VerticalModel:
    def __init__(self) -> None:
        self.round = 0
        self.finalize_observation: tuple[dict[str, object], bool] | None = None
        self.last_observation: tuple[str, dict[str, object], bool] | None = None

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        self.round += 1
        if self.round == 1:
            self.assert_catalog(request)
            return _stream(
                request,
                _call("run_test_profile", {"profile_id": "unit"}, "test-before"),
                FinishReason.TOOL_CALLS,
                "r-test-before",
            )
        if self.round == 2:
            failed, is_error = _result(request, "test-before")
            self.last_observation = ("test-before", failed, is_error)
            if is_error or failed["outcome"] != "failed":
                raise AssertionError(failed)
            return _stream(
                request,
                _call(
                    "read_file",
                    {"path": "app.py", "start_line": 1, "max_lines": 50},
                    "read",
                ),
                FinishReason.TOOL_CALLS,
                "r-read",
            )
        if self.round == 3:
            read, is_error = _result(request, "read")
            self.last_observation = ("read", read, is_error)
            if is_error:
                raise AssertionError(read)
            patch = json.dumps(
                {
                    "schema_version": 1,
                    "changes": [
                        {
                            "operation": "update",
                            "path": "app.py",
                            "base_sha256": read["sha256"],
                            "hunks": [
                                {
                                    "old_start": 2,
                                    "old_lines": ["    return left - right"],
                                    "new_lines": ["    return left + right  # fixed"],
                                }
                            ],
                        }
                    ],
                },
                separators=(",", ":"),
            )
            return _stream(
                request,
                _call("apply_patch", {"patch_json": patch}, "patch"),
                FinishReason.TOOL_CALLS,
                "r-patch",
            )
        if self.round == 4:
            patched, is_error = _result(request, "patch")
            self.last_observation = ("patch", patched, is_error)
            if is_error or patched["changed_files"] != 1:
                raise AssertionError(patched)
            return _stream(
                request,
                _call("run_test_profile", {"profile_id": "unit"}, "test-after"),
                FinishReason.TOOL_CALLS,
                "r-test-after",
            )
        if self.round == 5:
            passed, is_error = _result(request, "test-after")
            self.last_observation = ("test-after", passed, is_error)
            if is_error or passed["outcome"] != "passed":
                raise AssertionError(passed)
            return _stream(
                request,
                _call("git_status", {}, "status"),
                FinishReason.TOOL_CALLS,
                "r-status",
            )
        if self.round == 6:
            status, is_error = _result(request, "status")
            self.last_observation = ("status", status, is_error)
            if is_error or status["agent_changed_paths"] != ["app.py"]:
                raise AssertionError(status)
            return _stream(
                request,
                _call("git_diff", {}, "diff"),
                FinishReason.TOOL_CALLS,
                "r-diff",
            )
        if self.round == 7:
            diff, is_error = _result(request, "diff")
            self.last_observation = ("diff", diff, is_error)
            if is_error or "left + right  # fixed" not in diff["diff"]:
                raise AssertionError(diff)
            return _stream(
                request,
                _call("finalize_task", {}, "finalize"),
                FinishReason.TOOL_CALLS,
                "r-finalize",
            )
        if self.round == 8:
            report, is_error = _result(request, "finalize")
            self.finalize_observation = (report, is_error)
            if is_error or report["test"]["outcome"] != "passed":
                raise AssertionError(report)
            return _stream(
                request,
                AssistantTextItem(0, "final", "修复完成，测试通过且 diff 已核验。"),
                FinishReason.STOP,
                "r-final",
            )
        raise AssertionError("unexpected model round")

    @staticmethod
    def assert_catalog(request: ModelRequest) -> None:
        names = {item.name for item in request.tool_definitions}
        expected = {
            "apply_patch",
            "finalize_task",
            "git_diff",
            "git_status",
            "list_files",
            "read_file",
            "run_test_profile",
            "search_text",
        }
        if names != expected:
            raise AssertionError(names)


class D5VerticalSliceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="koawa-d5-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_fixture(self) -> None:
        (self.root / "tests").mkdir()
        (self.root / "app.py").write_text(
            "def add(left, right):\n    return left - right\n", encoding="utf-8"
        )
        (self.root / "tests" / "test_app.py").write_text(
            "import unittest\nfrom app import add\n\n"
            "class AppTest(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(5, add(2, 3))\n",
            encoding="utf-8",
        )
        _init_repo(self.root)

    def test_real_failure_repair_pass_diff_and_finalization_gate(self) -> None:
        self._write_fixture()
        model = _VerticalModel()
        with build_verified_coding_tool_registry(
            self.root,
            command_profiles=(_profile(),),
            repository_trust=RepositoryTrust.BUILTIN_FIXTURE,
        ) as registry:
            try:
                result = AgentLoop(
                    model,
                    tool_executor=registry,
                    completion_gate=registry,
                ).run(
                    run_id=UUID("00000000-0000-0000-0000-000000000555"),
                    input_items=(UserMessage("d5", "修复 add 并运行测试。"),),
                    provider="scripted",
                    model="d5-offline",
                )
            except AgentLoopError:
                self.fail(
                    f"vertical loop failed at round {model.round}: "
                    f"{model.finalize_observation!r}; last={model.last_observation!r}"
                )
        self.assertEqual(8, result.model_rounds)
        self.assertEqual(7, result.tool_calls)
        self.assertIn("left + right  # fixed", (self.root / "app.py").read_text(encoding="utf-8"))

    def test_dirty_baseline_file_is_protected_from_agent_patch(self) -> None:
        self._write_fixture()
        (self.root / "app.py").write_text("user dirty\n", encoding="utf-8")
        digest = hashlib.sha256((self.root / "app.py").read_bytes()).hexdigest()
        patch = json.dumps(
            {
                "schema_version": 1,
                "changes": [
                    {
                        "operation": "delete",
                        "path": "app.py",
                        "base_sha256": digest,
                    }
                ],
            },
            separators=(",", ":"),
        )
        with build_verified_coding_tool_registry(
            self.root,
            command_profiles=(_profile(),),
            repository_trust=RepositoryTrust.BUILTIN_FIXTURE,
        ) as registry:
            result = registry.execute(
                _call("apply_patch", {"patch_json": patch}, "dirty"),
                context=_context("dirty"),
            )
        self.assertTrue(result.is_error)
        self.assertEqual(
            "baseline_dirty_path_forbidden",
            json.loads(result.content)["error"]["code"],
        )
        self.assertEqual("user dirty\n", (self.root / "app.py").read_text(encoding="utf-8"))

    def test_final_text_without_verification_is_rejected(self) -> None:
        self._write_fixture()

        class FinalOnly:
            def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
                return _stream(
                    request,
                    AssistantTextItem(0, "premature", "已经完成。"),
                    FinishReason.STOP,
                    "premature-response",
                )

        with build_verified_coding_tool_registry(
            self.root,
            command_profiles=(_profile(),),
            repository_trust=RepositoryTrust.BUILTIN_FIXTURE,
        ) as registry:
            with self.assertRaisesRegex(AgentLoopError, "verification_required"):
                AgentLoop(
                    FinalOnly(),
                    tool_executor=registry,
                    completion_gate=registry,
                ).run(
                    run_id=uuid4(),
                    input_items=(UserMessage("early", "修复"),),
                    provider="scripted",
                    model="d5-offline",
                )

    def test_runner_is_profile_only_bounded_and_trust_gated(self) -> None:
        profile = CommandProfile(
            "bounded",
            (
                str(Path(sys.executable).resolve()),
                "-c",
                "import sys;sys.stdout.write('x'*10000)",
            ),
            timeout_seconds=10,
            max_stdout_bytes=128,
            max_stderr_bytes=128,
        )
        untrusted = TrustedCommandRunner(self.root, (profile,))
        with self.assertRaisesRegex(CommandRunnerError, "repository_not_trusted"):
            untrusted.run("bounded")
        trusted = TrustedCommandRunner(
            self.root, (profile,), trust=RepositoryTrust.BUILTIN_FIXTURE
        )
        result = trusted.run("bounded")
        self.assertEqual(CommandOutcome.PASSED, result.outcome)
        self.assertEqual(128, len(result.stdout))
        self.assertTrue(result.stdout_truncated)
        with self.assertRaisesRegex(CommandRunnerError, "unknown_command_profile"):
            trusted.run("model-supplied-command")

    def test_runner_timeout_is_typed(self) -> None:
        profile = CommandProfile(
            "slow",
            (str(Path(sys.executable).resolve()), "-c", "import time;time.sleep(5)"),
            timeout_seconds=0.1,
            max_stdout_bytes=128,
            max_stderr_bytes=128,
        )
        runner = TrustedCommandRunner(
            self.root, (profile,), trust=RepositoryTrust.BUILTIN_FIXTURE
        )
        result = runner.run("slow")
        self.assertEqual(CommandOutcome.TIMED_OUT, result.outcome)
        self.assertIsNotNone(result.exit_code)

    def test_git_facade_disables_repository_configured_executors(self) -> None:
        self._write_fixture()
        marker = self.root / ".git" / "must-not-execute"
        if sys.platform == "win32":
            executable = self.root / ".git" / "evil.cmd"
            executable.write_text(
                f"@echo off\r\necho executed>\"{marker}\"\r\nexit /b 0\r\n",
                encoding="utf-8",
            )
        else:
            executable = self.root / ".git" / "evil.sh"
            executable.write_text(
                f"#!/bin/sh\nprintf executed > '{marker}'\nexit 0\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
        _git(self.root, "config", "diff.external", str(executable))
        _git(self.root, "config", "diff.evil.textconv", str(executable))
        (self.root / ".gitattributes").write_text("*.py diff=evil\n", encoding="utf-8")

        with build_verified_coding_tool_registry(
            self.root,
            command_profiles=(_profile(),),
            repository_trust=RepositoryTrust.BUILTIN_FIXTURE,
        ) as registry:
            base = hashlib.sha256((self.root / "app.py").read_bytes()).hexdigest()
            patch = json.dumps(
                {
                    "schema_version": 1,
                    "changes": [
                        {
                            "operation": "update",
                            "path": "app.py",
                            "base_sha256": base,
                            "hunks": [
                                {
                                    "old_start": 2,
                                    "old_lines": ["    return left - right"],
                                    "new_lines": [
                                        "    return left + right  # changed size"
                                    ],
                                }
                            ],
                        }
                    ],
                },
                separators=(",", ":"),
            )
            patch_result = registry.execute(
                _call("apply_patch", {"patch_json": patch}, "safe-patch"),
                context=_context("safe-patch"),
            )
            result = registry.execute(
                _call("git_diff", {}, "safe-diff"),
                context=_context("safe-diff"),
            )

        self.assertFalse(patch_result.is_error, patch_result.content)
        self.assertFalse(result.is_error, result.content)
        self.assertIn(
            "left + right  # changed size",
            json.loads(result.content)["diff"],
            result.content,
        )
        self.assertFalse(marker.exists(), "repository-defined executor ran on the host")

    def test_failed_test_and_test_budget_cannot_be_finalized(self) -> None:
        self._write_fixture()
        context = _context("budget")
        with build_verified_coding_tool_registry(
            self.root,
            command_profiles=(_profile(),),
            repository_trust=RepositoryTrust.BUILTIN_FIXTURE,
            verification_limits=VerificationLimits(max_test_runs=1),
        ) as registry:
            first = registry.execute(
                _call("run_test_profile", {"profile_id": "unit"}, "first"),
                context=context,
            )
            second = registry.execute(
                _call("run_test_profile", {"profile_id": "unit"}, "second"),
                context=context,
            )
            final = registry.execute(
                _call("finalize_task", {}, "final"),
                context=context,
            )
        self.assertFalse(first.is_error)
        self.assertEqual("failed", json.loads(first.content)["outcome"])
        self.assertEqual(
            "test_run_budget_exceeded",
            json.loads(second.content)["error"]["code"],
        )
        self.assertEqual(
            "tests_not_passing",
            json.loads(final.content)["error"]["code"],
        )


if __name__ == "__main__":
    unittest.main()
