from __future__ import annotations

import json
import unittest
from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from koawa_agent_v2.execution.loop import AgentLoop, ToolExecutionContext
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
from koawa_agent_v2.editing.tools import build_coding_tool_registry


def _call(name: str, arguments: dict[str, object], *, call_id: str = "call-1") -> ToolCallItem:
    return ToolCallItem(
        0,
        f"item-{call_id}",
        call_id,
        name,
        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    )


def _context(call_id: str = "call-1") -> ToolExecutionContext:
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


def _tool_result(request: ModelRequest, call_id: str) -> dict[str, object]:
    result = next(
        item
        for item in request.input_items
        if isinstance(item, ToolResultMessage) and item.call_ref.call_id == call_id
    )
    document = json.loads(result.content)
    if result.is_error:
        raise AssertionError(document)
    return document


class _PatchModel:
    def __init__(self) -> None:
        self.round = 0

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        self.round += 1
        if self.round == 1:
            self.assert_catalog(request)
            return _stream(
                request,
                _call(
                    "read_file",
                    {"path": "app.py", "start_line": 1, "max_lines": 20},
                    call_id="read-before",
                ),
                FinishReason.TOOL_CALLS,
                "response-read-before",
            )
        if self.round == 2:
            read = _tool_result(request, "read-before")
            patch_json = json.dumps(
                {
                    "schema_version": 1,
                    "changes": [
                        {
                            "operation": "update",
                            "path": "app.py",
                            "base_sha256": read["sha256"],
                            "hunks": [
                                {
                                    "old_start": 1,
                                    "old_lines": ["VALUE = 1"],
                                    "new_lines": ["VALUE = 2"],
                                }
                            ],
                        }
                    ],
                },
                separators=(",", ":"),
            )
            return _stream(
                request,
                _call("apply_patch", {"patch_json": patch_json}, call_id="patch"),
                FinishReason.TOOL_CALLS,
                "response-patch",
            )
        if self.round == 3:
            patched = _tool_result(request, "patch")
            if patched.get("changed_files") != 1:
                raise AssertionError("patch result is missing changed-file evidence")
            return _stream(
                request,
                _call(
                    "read_file",
                    {"path": "app.py", "start_line": 1, "max_lines": 20},
                    call_id="read-after",
                ),
                FinishReason.TOOL_CALLS,
                "response-read-after",
            )
        if self.round == 4:
            read = _tool_result(request, "read-after")
            if read.get("content") != "VALUE = 2":
                raise AssertionError("post-patch read did not observe the new content")
            return _stream(
                request,
                AssistantTextItem(0, "item-final", "已修改并重新读取 app.py。"),
                FinishReason.STOP,
                "response-final",
            )
        raise AssertionError("unexpected model round")

    @staticmethod
    def assert_catalog(request: ModelRequest) -> None:
        names = {item.name for item in request.tool_definitions}
        if names != {"apply_patch", "list_files", "read_file", "search_text"}:
            raise AssertionError(names)


class PatchToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="koawa-d4-tools-")
        self.root = Path(self.temporary.name)
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_registry_returns_stable_patch_errors_without_writes(self) -> None:
        before = (self.root / "app.py").read_bytes()
        with build_coding_tool_registry(self.root) as registry:
            self.assertEqual(
                ["apply_patch", "list_files", "read_file", "search_text"],
                [item.name for item in registry.definitions()],
            )
            result = registry.execute(
                _call("apply_patch", {"patch_json": "not-json"}),
                context=_context(),
            )

        self.assertTrue(result.is_error)
        self.assertEqual(
            "invalid_patch_document", json.loads(result.content)["error"]["code"]
        )
        self.assertEqual(before, (self.root / "app.py").read_bytes())

    def test_agent_loop_reads_patches_rereads_and_finishes(self) -> None:
        model = _PatchModel()
        with build_coding_tool_registry(self.root) as registry:
            result = AgentLoop(model, tool_executor=registry).run(
                run_id=UUID("00000000-0000-0000-0000-000000000444"),
                input_items=(UserMessage("input-d4", "把 VALUE 改成 2 并验证。"),),
                provider="scripted",
                model="d4-offline",
            )

        self.assertEqual(4, result.model_rounds)
        self.assertEqual(3, result.tool_calls)
        self.assertEqual("已修改并重新读取 app.py。", result.final_text)
        self.assertEqual("VALUE = 2\n", (self.root / "app.py").read_text(encoding="utf-8"))

    def test_internal_transaction_namespace_is_hidden_and_reserved(self) -> None:
        internal = self.root / ".koawa-patch-stage-visible.tmp"
        internal.write_text("must-not-enter-model", encoding="utf-8")
        add_patch = json.dumps(
            {
                "schema_version": 1,
                "changes": [
                    {
                        "operation": "add",
                        "path": ".koawa-patch-user.tmp",
                        "content": "x",
                        "newline": "lf",
                        "utf8_bom": False,
                    }
                ],
            },
            separators=(",", ":"),
        )
        with build_coding_tool_registry(self.root) as registry:
            listing = registry.execute(
                _call(
                    "list_files",
                    {"path": ".", "max_depth": 1, "max_entries": 20},
                    call_id="list-internal",
                ),
                context=_context("list-internal"),
            )
            read = registry.execute(
                _call(
                    "read_file",
                    {
                        "path": internal.name,
                        "start_line": 1,
                        "max_lines": 10,
                    },
                    call_id="read-internal",
                ),
                context=_context("read-internal"),
            )
            write = registry.execute(
                _call(
                    "apply_patch",
                    {"patch_json": add_patch},
                    call_id="write-internal",
                ),
                context=_context("write-internal"),
            )

        self.assertNotIn(".koawa-patch-", listing.content)
        self.assertEqual(
            "repository_control_path_forbidden",
            json.loads(read.content)["error"]["code"],
        )
        self.assertEqual(
            "repository_control_path_forbidden",
            json.loads(write.content)["error"]["code"],
        )


if __name__ == "__main__":
    unittest.main()
