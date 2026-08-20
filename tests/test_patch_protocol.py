from __future__ import annotations

import hashlib
import json
import unittest

from koawa_agent_v2.editing.protocol import (
    AddFileChange,
    DeleteFileChange,
    NewlineStyle,
    PatchError,
    PatchLimits,
    UpdateFileChange,
    apply_update,
    decode_text_document,
    encode_add,
    parse_patch_document,
)
from koawa_agent_v2.tools.errors import ToolConfigurationError


def _json(document: object) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


class PatchProtocolTest(unittest.TestCase):
    def test_versioned_document_builds_typed_add_update_delete(self) -> None:
        base = hashlib.sha256(b"old\n").hexdigest()
        patch = parse_patch_document(
            _json(
                {
                    "schema_version": 1,
                    "changes": [
                        {
                            "operation": "add",
                            "path": "new.txt",
                            "content": "new\n",
                            "newline": "lf",
                            "utf8_bom": False,
                        },
                        {
                            "operation": "update",
                            "path": "old.txt",
                            "base_sha256": base,
                            "hunks": [
                                {
                                    "old_start": 1,
                                    "old_lines": ["old"],
                                    "new_lines": ["changed"],
                                }
                            ],
                        },
                        {
                            "operation": "delete",
                            "path": "gone.txt",
                            "base_sha256": base,
                        },
                    ],
                }
            )
        )

        self.assertIsInstance(patch.changes[0], AddFileChange)
        self.assertIsInstance(patch.changes[1], UpdateFileChange)
        self.assertIsInstance(patch.changes[2], DeleteFileChange)
        self.assertEqual(64, len(patch.document_sha256))
        self.assertNotIn("changed", repr(patch))

    def test_parser_rejects_duplicates_unknown_fields_schema_and_limits(self) -> None:
        valid_add = {
            "operation": "add",
            "path": "new.txt",
            "content": "new",
            "newline": "lf",
            "utf8_bom": False,
        }
        cases = (
            ('{"schema_version":1,"schema_version":1,"changes":[]}', "invalid_patch_document"),
            (_json({"schema_version": 2, "changes": [valid_add]}), "unsupported_patch_schema"),
            (
                _json(
                    {
                        "schema_version": 1,
                        "changes": [{**valid_add, "unexpected": True}],
                    }
                ),
                "invalid_patch_change",
            ),
            (
                _json(
                    {
                        "schema_version": 1,
                        "changes": [valid_add, {**valid_add, "content": "two"}],
                    }
                ),
                "duplicate_patch_path",
            ),
        )
        for value, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(PatchError) as raised:
                    parse_patch_document(value)
                self.assertEqual(code, raised.exception.code)

        with self.assertRaises(ToolConfigurationError):
            PatchLimits(max_files=True)
        with self.assertRaises(ToolConfigurationError):
            PatchLimits(max_file_bytes=17 * 1024 * 1024)

    def test_update_is_exact_and_preserves_bom_crlf_and_final_newline(self) -> None:
        before = b"\xef\xbb\xbffirst\r\nold\r\nlast\r\n"
        base = hashlib.sha256(before).hexdigest()
        change = parse_patch_document(
            _json(
                {
                    "schema_version": 1,
                    "changes": [
                        {
                            "operation": "update",
                            "path": "a.txt",
                            "base_sha256": base,
                            "hunks": [
                                {
                                    "old_start": 2,
                                    "old_lines": ["old"],
                                    "new_lines": ["new", "extra"],
                                }
                            ],
                        }
                    ],
                }
            )
        ).changes[0]
        assert isinstance(change, UpdateFileChange)

        after, additions, deletions = apply_update(before, change, PatchLimits())

        self.assertEqual(b"\xef\xbb\xbffirst\r\nnew\r\nextra\r\nlast\r\n", after)
        self.assertEqual((2, 1), (additions, deletions))
        decoded = decode_text_document(after)
        self.assertTrue(decoded.utf8_bom)
        self.assertEqual("\r\n", decoded.newline)
        self.assertTrue(decoded.final_newline)

    def test_context_mismatch_overlap_and_noop_fail_before_any_io(self) -> None:
        before = b"one\ntwo\nthree"
        base = hashlib.sha256(before).hexdigest()
        documents = (
            [
                {"old_start": 2, "old_lines": ["wrong"], "new_lines": ["x"]}
            ],
            [
                {"old_start": 1, "old_lines": ["one", "two"], "new_lines": ["x"]},
                {"old_start": 2, "old_lines": ["two"], "new_lines": ["y"]},
            ],
            [{"old_start": 1, "old_lines": ["one"], "new_lines": ["one"]}],
        )
        expected = (
            "patch_context_mismatch",
            "overlapping_patch_hunks",
            "patch_no_changes",
        )
        for hunks, code in zip(documents, expected, strict=True):
            patch = parse_patch_document(
                _json(
                    {
                        "schema_version": 1,
                        "changes": [
                            {
                                "operation": "update",
                                "path": "a.txt",
                                "base_sha256": base,
                                "hunks": hunks,
                            }
                        ],
                    }
                )
            )
            change = patch.changes[0]
            assert isinstance(change, UpdateFileChange)
            with self.subTest(code=code), self.assertRaises(PatchError) as raised:
                apply_update(before, change, PatchLimits())
            self.assertEqual(code, raised.exception.code)

    def test_add_has_explicit_newline_bom_and_terminal_rule(self) -> None:
        change = AddFileChange(
            operation=change_operation("add"),
            path="new.txt",
            content="one\ntwo\n",
            newline=NewlineStyle.CRLF,
            utf8_bom=True,
        )
        self.assertEqual(
            b"\xef\xbb\xbfone\r\ntwo\r\n",
            encode_add(change, PatchLimits()),
        )

    def test_text_line_and_total_limit_configuration_are_hard_bounded(self) -> None:
        with self.assertRaises(PatchError) as raised:
            decode_text_document(b"\n" * 10, max_lines=5)
        self.assertEqual("patch_file_line_limit_exceeded", raised.exception.code)
        with self.assertRaises(ToolConfigurationError):
            PatchLimits(max_file_lines=1_000_001)
        with self.assertRaises(ToolConfigurationError):
            PatchLimits(max_total_input_bytes=100, max_file_bytes=101)
        with self.assertRaises(ToolConfigurationError):
            PatchLimits(max_files=32, max_path_chars=1_024, max_result_chars=512)


def change_operation(value: str):
    from koawa_agent_v2.editing.protocol import PatchOperation

    return PatchOperation(value)


if __name__ == "__main__":
    unittest.main()
