from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryDirectory

from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    OutputKind,
    StreamHeader,
    ToolCallItem,
    ToolResultMessage,
    TurnCompleted,
    TurnStarted,
)
from koawa_agent_v2.runtime.app import AppRuntime
from koawa_agent_v2.runtime.config import (
    PolicyConfig,
    ProviderConfig,
    RepositoryTrustMode,
    RuntimeConfig,
    SandboxConfig,
    SandboxRunner,
    TestProfileConfig,
)


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
    _git(root, "config", "user.email", "assembly@example.invalid")
    _git(root, "config", "user.name", "Assembly Fixture")
    _git(root, "config", "core.fsmonitor", "false")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "core.filemode", "false")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "fixture baseline")


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
    kind = (
        OutputKind.TOOL_CALL
        if isinstance(item, ToolCallItem)
        else OutputKind.ASSISTANT_TEXT
    )
    started = (
        ItemStarted(
            _header(request, response_id, 1),
            0,
            item.item_id,
            kind,
            item.call_id,
            item.name,
        )
        if isinstance(item, ToolCallItem)
        else ItemStarted(_header(request, response_id, 1), 0, item.item_id, kind)
    )
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


def _call(name: str, arguments: dict[str, object], call_id: str) -> ToolCallItem:
    return ToolCallItem(
        0,
        f"item-{call_id}",
        call_id,
        name,
        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    )


def _result(request: ModelRequest, call_id: str) -> tuple[dict[str, object], bool]:
    item = next(
        value
        for value in request.input_items
        if isinstance(value, ToolResultMessage)
        and value.call_ref.call_id == call_id
    )
    return json.loads(item.content), item.is_error


class _RepairModel:
    def __init__(self) -> None:
        self.round = 0

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        self.round += 1
        if self.round == 1:
            return _stream(
                request,
                _call("run_test_profile", {"profile_id": "unit"}, "test-before"),
                FinishReason.TOOL_CALLS,
                "r1",
            )
        if self.round == 2:
            failed, is_error = _result(request, "test-before")
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
                "r2",
            )
        if self.round == 3:
            read, is_error = _result(request, "read")
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
                "r3",
            )
        if self.round == 4:
            patched, is_error = _result(request, "patch")
            if is_error or patched["changed_files"] != 1:
                raise AssertionError(patched)
            return _stream(
                request,
                _call("run_test_profile", {"profile_id": "unit"}, "test-after"),
                FinishReason.TOOL_CALLS,
                "r4",
            )
        if self.round == 5:
            passed, is_error = _result(request, "test-after")
            if is_error or passed["outcome"] != "passed":
                raise AssertionError(passed)
            return _stream(
                request,
                _call("git_status", {}, "status"),
                FinishReason.TOOL_CALLS,
                "r5",
            )
        if self.round == 6:
            status, is_error = _result(request, "status")
            if is_error or status["agent_changed_paths"] != ["app.py"]:
                raise AssertionError(status)
            return _stream(
                request,
                _call("git_diff", {}, "diff"),
                FinishReason.TOOL_CALLS,
                "r6",
            )
        if self.round == 7:
            diff, is_error = _result(request, "diff")
            if is_error or "left + right  # fixed" not in diff["diff"]:
                raise AssertionError(diff)
            return _stream(
                request,
                _call("finalize_task", {}, "finalize"),
                FinishReason.TOOL_CALLS,
                "r7",
            )
        if self.round == 8:
            report, is_error = _result(request, "finalize")
            if is_error or report["test"]["outcome"] != "passed":
                raise AssertionError(report)
            return _stream(
                request,
                AssistantTextItem(0, "final", "fixed and verified"),
                FinishReason.STOP,
                "r8",
            )
        raise AssertionError("unexpected model round")


class RuntimeAssemblyTest(unittest.TestCase):
    def test_real_assembly_runs_verified_registry_through_policy_and_ledger(self) -> None:
        with TemporaryDirectory(prefix="koawa-p0-assembly-") as temporary:
            base = Path(temporary)
            root = base / "repo"
            root.mkdir()
            (root / "tests").mkdir()
            (root / "app.py").write_text(
                "def add(left, right):\n    return left - right\n",
                encoding="utf-8",
            )
            (root / "tests" / "test_app.py").write_text(
                "import unittest\n"
                "from app import add\n\n"
                "class AppTest(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(5, add(2, 3))\n",
                encoding="utf-8",
            )
            _init_repo(root)
            config = RuntimeConfig(
                repo=root,
                db=base / "state" / "agent.sqlite3",
                provider=ProviderConfig(
                    base_url="http://127.0.0.1:1/v1",
                    api_key_env="P0_TEST_KEY",
                    model="test-model",
                ),
                sandbox=SandboxConfig(
                    runner=SandboxRunner.HOST,
                    host_trust=RepositoryTrustMode.BUILTIN_FIXTURE,
                ),
                test_profiles=(
                    TestProfileConfig(
                        "unit",
                        (
                            str(Path(sys.executable).resolve()),
                            "-B",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                        ),
                        timeout_seconds=30,
                    ),
                ),
                policy=PolicyConfig(),
                system_prompt="Repair the failing test and finalize evidence.",
            )
            app = AppRuntime(config, model_client=_RepairModel())
            outcome = app.run("make tests pass")
            self.assertTrue(outcome.ok, outcome.payload)
            self.assertEqual("completed", outcome.payload["status"])
            self.assertIn("fixed and verified", outcome.payload["final_text"])
            events = app.assembled.store.read_all(after_position=0, limit=500)
            event_types = {event.event_type for event in events}
            self.assertIn("tool.execution-prepared.v1", event_types)
            self.assertIn("tool.execution-succeeded.v1", event_types)

    def test_non_git_repo_fails_with_clear_code(self) -> None:
        """非 git 目录装配时报 not_a_git_repository，而不是模糊的 runtime_assembly_failed。"""
        from koawa_agent_v2.runtime.assembly import RuntimeAssemblyError
        from koawa_agent_v2.runtime.config import RuntimeConfig, PolicyConfig

        with TemporaryDirectory(prefix="koawa-p0-nongit-") as temporary:
            base = Path(temporary)
            root = base / "nongit"
            root.mkdir()
            config = RuntimeConfig(
                repo=root,
                db=base / "state.sqlite3",
                provider=ProviderConfig(
                    base_url="http://127.0.0.1:1/v1",
                    api_key_env="P0_TEST_KEY",
                    model="test-model",
                ),
                sandbox=SandboxConfig(
                    runner=SandboxRunner.HOST,
                    host_trust=RepositoryTrustMode.BUILTIN_FIXTURE,
                ),
                test_profiles=(
                    TestProfileConfig(
                        "unit",
                        (str(Path(sys.executable).resolve()), "-B"),
                        timeout_seconds=30,
                    ),
                ),
                policy=PolicyConfig(),
                system_prompt="s",
            )
            with self.assertRaises(RuntimeAssemblyError) as raised:
                AppRuntime(config, model_client=_RepairModel())
            self.assertEqual("not_a_git_repository", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
