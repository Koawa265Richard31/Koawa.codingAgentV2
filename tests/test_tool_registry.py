from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from uuid import uuid4

from koawa_agent_v2.execution.loop import ToolExecutionContext, ToolExecutionResult
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.tools.errors import (
    MAX_TOOL_ERROR_CONTENT_CHARS,
    ToolConfigurationError,
    ToolRegistryError,
)
from koawa_agent_v2.tools.registry import ToolRegistry
from koawa_agent_v2.tools.schema import ToolSpec


@dataclass(frozen=True, slots=True)
class ReadArgs:
    path: str
    line: int = 1


def _spec(name: str) -> ToolSpec[ReadArgs]:
    return ToolSpec(
        name,
        f"Execute {name}",
        ReadArgs,
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 128},
                "line": {"type": "integer", "minimum": 1, "maximum": 10_000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )


def _call(name: str, arguments_json: str) -> ToolCallItem:
    return ToolCallItem(0, "tool-item", "call-1", name, arguments_json)


def _context() -> ToolExecutionContext:
    model_turn_id = uuid4()
    return ToolExecutionContext(
        run_id=uuid4(),
        model_turn_id=model_turn_id,
        model_round=1,
        call_ref=ModelCallRef(model_turn_id, "call-1"),
    )


class ToolRegistryTest(unittest.TestCase):
    def test_definitions_are_sorted_deterministic_and_first_snapshot_seals(self) -> None:
        registry = ToolRegistry()
        handler = lambda arguments, *, context: ToolExecutionResult(arguments.path)
        registry.register(_spec("zeta_tool"), handler)
        registry.register(_spec("alpha_tool"), handler)

        first = registry.definitions()
        second = registry.definitions()

        self.assertEqual(["alpha_tool", "zeta_tool"], [item.name for item in first])
        self.assertIs(first, second)
        self.assertIs(True, registry.sealed)
        with self.assertRaises(ToolConfigurationError) as raised:
            registry.register(_spec("later_tool"), handler)
        self.assertEqual("tool_registry_sealed", raised.exception.code)

    def test_duplicate_name_and_non_callable_handler_fail_during_registration(self) -> None:
        registry = ToolRegistry()
        handler = lambda arguments, *, context: ToolExecutionResult(arguments.path)
        registry.register(_spec("read_file"), handler)

        with self.assertRaises(ToolConfigurationError) as duplicate:
            registry.register(_spec("read_file"), handler)
        self.assertEqual("duplicate_tool_name", duplicate.exception.code)

        with self.assertRaises(ToolConfigurationError) as invalid_handler:
            ToolRegistry().register(_spec("read_file"), object())  # type: ignore[arg-type]
        self.assertEqual("invalid_tool_handler", invalid_handler.exception.code)

    def test_execute_decodes_typed_arguments_and_passes_execution_context(self) -> None:
        registry = ToolRegistry()
        received: list[tuple[ReadArgs, ToolExecutionContext]] = []

        def handler(
            arguments: ReadArgs,
            *,
            context: ToolExecutionContext,
        ) -> ToolExecutionResult:
            received.append((arguments, context))
            return ToolExecutionResult(f"{arguments.path}:{arguments.line}")

        registry.register(_spec("read_file"), handler)
        context = _context()

        result = registry.execute(
            _call("read_file", '{"path":"README.md","line":7}'),
            context=context,
        )

        self.assertEqual("README.md:7", result.content)
        self.assertIs(False, result.is_error)
        self.assertEqual([(ReadArgs("README.md", 7), context)], received)
        self.assertIs(True, registry.sealed)

    def test_invalid_arguments_return_bounded_stable_json_without_calling_handler(self) -> None:
        registry = ToolRegistry()
        calls = 0

        def handler(
            arguments: ReadArgs,
            *,
            context: ToolExecutionContext,
        ) -> ToolExecutionResult:
            nonlocal calls
            calls += 1
            return ToolExecutionResult(arguments.path)

        registry.register(_spec("read_file"), handler)
        result = registry.execute(
            _call("read_file", '{"path":"README.md","secret_value":"do-not-echo"}'),
            context=_context(),
        )

        self.assertIs(True, result.is_error)
        self.assertLessEqual(len(result.content), MAX_TOOL_ERROR_CONTENT_CHARS)
        self.assertEqual(
            {
                "error": {
                    "code": "invalid_tool_arguments",
                    "field": "secret_value",
                    "reason": "additional_property",
                }
            },
            json.loads(result.content),
        )
        self.assertNotIn("do-not-echo", result.content)
        self.assertEqual(0, calls)

    def test_unknown_tool_returns_stable_error_without_echoing_name(self) -> None:
        registry = ToolRegistry()
        result = registry.execute(
            _call("delete_everything", "{}"),
            context=_context(),
        )

        self.assertEqual({"error": {"code": "unknown_tool"}}, json.loads(result.content))
        self.assertIs(True, result.is_error)
        self.assertNotIn("delete_everything", result.content)
        self.assertIs(True, registry.sealed)

    def test_handler_exception_is_not_swallowed_or_rewritten(self) -> None:
        registry = ToolRegistry()

        def broken_handler(
            arguments: ReadArgs,
            *,
            context: ToolExecutionContext,
        ) -> ToolExecutionResult:
            raise RuntimeError("backend-secret-detail")

        registry.register(_spec("read_file"), broken_handler)

        with self.assertRaisesRegex(RuntimeError, "backend-secret-detail"):
            registry.execute(
                _call("read_file", '{"path":"README.md"}'),
                context=_context(),
            )

    def test_invalid_handler_result_is_a_registry_contract_failure(self) -> None:
        registry = ToolRegistry()
        registry.register(
            _spec("read_file"),
            lambda arguments, *, context: "not-a-result",  # type: ignore[arg-type,return-value]
        )

        with self.assertRaises(ToolRegistryError) as raised:
            registry.execute(
                _call("read_file", '{"path":"README.md"}'),
                context=_context(),
            )

        self.assertEqual("invalid_tool_handler_result", raised.exception.code)

    def test_concurrent_registration_is_rejected_after_execution_enters_handler(self) -> None:
        registry = ToolRegistry()
        entered = Event()
        release = Event()

        def blocking_handler(
            arguments: ReadArgs,
            *,
            context: ToolExecutionContext,
        ) -> ToolExecutionResult:
            entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("test release timeout")
            return ToolExecutionResult(arguments.path)

        registry.register(_spec("read_file"), blocking_handler)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                registry.execute,
                _call("read_file", '{"path":"README.md"}'),
                context=_context(),
            )
            self.assertTrue(entered.wait(timeout=3))
            try:
                with self.assertRaises(ToolConfigurationError) as raised:
                    registry.register(
                        _spec("search_text"),
                        lambda arguments, *, context: ToolExecutionResult("unused"),
                    )
                self.assertEqual("tool_registry_sealed", raised.exception.code)
            finally:
                release.set()
            self.assertEqual("README.md", future.result(timeout=3).content)


if __name__ == "__main__":
    unittest.main()
