"""Audit F10 regression: apply_patch must be self-describing to a model.

The tool description carries the full patch document schema, and every
parse-level rejection carries a machine-readable detail so the caller can
self-correct from the error alone (a real model made 26 consecutive
format failures when neither was present).
"""
from __future__ import annotations

import json
import unittest

from koawa_agent_v2.editing.protocol import PatchError, parse_patch_document
from koawa_agent_v2.editing.tools import apply_patch_tool_spec


class PatchSelfDescribeTest(unittest.TestCase):
    def test_tool_description_documents_update_shape(self) -> None:
        spec = apply_patch_tool_spec()
        schema = json.loads(spec.input_schema_json)
        description = schema["properties"]["patch_json"]["description"]
        self.assertIn('"schema_version": 1', description)
        self.assertIn('"old_start"', description)
        self.assertIn('"base_sha256"', description)

    def test_update_field_set_mismatch_details_missing_field(self) -> None:
        document = json.dumps(
            {
                "schema_version": 1,
                "changes": [
                    {"operation": "update", "path": "a.txt", "base_sha256": "0" * 64}
                ],
            }
        )
        with self.assertRaises(PatchError) as raised:
            parse_patch_document(document)
        self.assertEqual("invalid_patch_change", raised.exception.code)
        self.assertEqual("missing_field:hunks", raised.exception.detail)

    def test_hunk_shape_details_missing_field(self) -> None:
        document = json.dumps(
            {
                "schema_version": 1,
                "changes": [
                    {
                        "operation": "update",
                        "path": "a.txt",
                        "base_sha256": "0" * 64,
                        "hunks": [{"old_lines": ["x"], "new_lines": ["y"]}],
                    }
                ],
            }
        )
        with self.assertRaises(PatchError) as raised:
            parse_patch_document(document)
        self.assertEqual("invalid_patch_hunk", raised.exception.code)
        self.assertEqual("missing_field:old_start", raised.exception.detail)

    def test_non_dict_change_details_shape(self) -> None:
        document = json.dumps({"schema_version": 1, "changes": ["nope"]})
        with self.assertRaises(PatchError) as raised:
            parse_patch_document(document)
        self.assertEqual("invalid_patch_change", raised.exception.code)
        self.assertEqual(
            "change_must_be_object_with_operation", raised.exception.detail
        )


if __name__ == "__main__":
    unittest.main()
