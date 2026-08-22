"""D20 tool repair: enriched patch diagnostics and argument repair hints."""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass

from koawa_agent_v2.editing.protocol import (
    PatchError,
    PatchLimits,
    apply_update,
    parse_patch_document,
)
from koawa_agent_v2.tools.errors import (
    ToolArgumentError,
    argument_error_result,
    tool_error_result,
)
from koawa_agent_v2.tools.registry import _minimal_example
from koawa_agent_v2.tools.schema import ToolSpec


@dataclass(frozen=True, slots=True)
class _ReadArgs:
    path: str
    start_line: int


@dataclass(frozen=True, slots=True)
class _SingleArgs:
    path: str = ""


def _read_spec() -> ToolSpec:
    return ToolSpec(
        "read_file",
        "read a file",
        _ReadArgs,
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "maxLength": 1024},
                "start_line": {"type": "integer", "minimum": 1, "maximum": 2147483647},
            },
            "required": ["path", "start_line"],
        },
    )


def _patch_error(document: str) -> PatchError:
    try:
        parse_patch_document(document)
    except PatchError as error:
        return error
    raise AssertionError("expected PatchError")


class PatchDiagnosticsTest(unittest.TestCase):
    def test_add_missing_field_reports_field(self) -> None:
        document = json.dumps(
            {
                "schema_version": 1,
                "changes": [
                    {"operation": "add", "path": "index.html", "content": "hi"}
                ],
            }
        )
        error = _patch_error(document)
        self.assertEqual("invalid_patch_change", error.code)
        self.assertEqual("missing_field:newline", error.detail)

    def test_add_unexpected_field_reports_field(self) -> None:
        document = json.dumps(
            {
                "schema_version": 1,
                "changes": [
                    {
                        "operation": "add",
                        "path": "index.html",
                        "content": "hi",
                        "newline": "lf",
                        "utf8_bom": False,
                        "base_sha256": "abc",
                    }
                ],
            }
        )
        error = _patch_error(document)
        self.assertEqual("invalid_patch_change", error.code)
        self.assertEqual("unexpected_field:base_sha256", error.detail)

    def test_update_hunk_mismatch_reports_first_line(self) -> None:
        document = json.dumps(
            {
                "schema_version": 1,
                "changes": [
                    {
                        "operation": "update",
                        "path": "calc.py",
                        "base_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "hunks": [
                            {
                                "old_start": 2,
                                "old_lines": ["    return a - b"],
                                "new_lines": ["    return a + b"],
                            }
                        ],
                    }
                ],
            }
        )
        patch = parse_patch_document(document)
        with self.assertRaises(PatchError) as raised:
            apply_update(
                b"def add(a, b):\n    return a + b\n",
                patch.changes[0],
                PatchLimits(),
            )
        self.assertEqual("patch_context_mismatch", raised.exception.code)
        self.assertEqual("first_mismatch_line:2", raised.exception.detail)

    def test_repair_hint_has_expected_and_example(self) -> None:
        spec = _read_spec()
        with self.assertRaises(ToolArgumentError) as raised:
            spec.decode('{"path": "calc.py", "start_line": "1"}')
        error = raised.exception
        self.assertEqual("value-type:integer", error.expected)
        result = argument_error_result(error, example=_minimal_example(spec))
        parsed = json.loads(result.content)
        self.assertEqual("invalid_tool_arguments", parsed["error"]["code"])
        self.assertEqual("value-type:integer", parsed["error"]["expected"])
        self.assertIn("example", parsed["error"])
        self.assertEqual(
            {"path": "<str>", "start_line": 1},
            json.loads(parsed["error"]["example"]),
        )

    def test_detail_never_leaks_file_content(self) -> None:
        document = json.dumps(
            {
                "schema_version": 1,
                "changes": [
                    {
                        "operation": "update",
                        "path": "calc.py",
                        "base_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "hunks": [
                            {
                                "old_start": 2,
                                "old_lines": ["    return a - b"],
                                "new_lines": ["    return a + b"],
                            }
                        ],
                    }
                ],
            }
        )
        patch = parse_patch_document(document)
        with self.assertRaises(PatchError) as raised:
            apply_update(
                b"def add(a, b):\n    return a + b\n",
                patch.changes[0],
                PatchLimits(),
            )
        detail = raised.exception.detail or ""
        self.assertNotIn("return a", detail)
        self.assertNotIn("add(", detail)

    def test_example_builder_degrade_keeps_stable_code(self) -> None:
        spec = ToolSpec(
            "no_props",
            "no properties",
            _SingleArgs,
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"path": {"type": "string", "maxLength": 1024}},
                "required": [],
            },
        )
        self.assertIsNone(_minimal_example(spec))
        result = argument_error_result(
            ToolArgumentError(
                "wrong_type", field="path", expected="value-type:string"
            ),
            example=None,
        )
        parsed = json.loads(result.content)
        self.assertEqual("invalid_tool_arguments", parsed["error"]["code"])
        self.assertNotIn("example", parsed["error"])


if __name__ == "__main__":
    unittest.main()
