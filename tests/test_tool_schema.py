from __future__ import annotations

import json
import unittest
from dataclasses import dataclass

from koawa_agent_v2.tools.errors import ToolArgumentError, ToolConfigurationError
from koawa_agent_v2.tools.schema import ToolSpec


@dataclass(frozen=True, slots=True)
class RepositoryQueryArgs:
    path: str
    count: int
    case_sensitive: bool
    globs: tuple[str, ...]
    root: str = "."


def _schema() -> dict[str, object]:
    return {
        "required": ["path", "globs", "case_sensitive", "count"],
        "properties": {
            "root": {"type": "string", "maxLength": 64},
            "path": {
                "maxLength": 32,
                "type": "string",
                "minLength": 1,
                "description": "Workspace-relative path",
            },
            "count": {"maximum": 20, "type": "integer", "minimum": 1},
            "case_sensitive": {"type": "boolean"},
            "globs": {
                "maxItems": 3,
                "type": "array",
                "items": {"maxLength": 16, "type": "string", "minLength": 1},
            },
        },
        "additionalProperties": False,
        "type": "object",
    }


class ToolSchemaTest(unittest.TestCase):
    def spec(self) -> ToolSpec[RepositoryQueryArgs]:
        return ToolSpec(
            "search_text",
            "Search bounded repository text",
            RepositoryQueryArgs,
            _schema(),
        )

    def test_one_spec_generates_canonical_definition_and_typed_arguments(self) -> None:
        spec = self.spec()

        self.assertEqual("search_text", spec.definition().name)
        self.assertEqual(spec.input_schema_json, spec.definition().input_schema_json)
        self.assertEqual(
            spec.input_schema_json,
            json.dumps(
                spec.input_schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self.assertEqual(
            ["case_sensitive", "count", "globs", "path"],
            spec.input_schema["required"],
        )
        self.assertEqual(0, spec.input_schema["properties"]["root"]["minLength"])
        self.assertEqual(0, spec.input_schema["properties"]["globs"]["minItems"])

        arguments = spec.decode(
            '{"path":"src","count":2,"case_sensitive":false,'
            '"globs":["*.py","*.md"]}'
        )

        self.assertIsInstance(arguments, RepositoryQueryArgs)
        self.assertEqual("src", arguments.path)
        self.assertEqual(2, arguments.count)
        self.assertIs(False, arguments.case_sensitive)
        self.assertEqual(("*.py", "*.md"), arguments.globs)
        self.assertEqual(".", arguments.root)

    def test_schema_is_copied_and_input_schema_property_is_defensive(self) -> None:
        source = _schema()
        spec = ToolSpec(
            "search_text",
            "Search bounded repository text",
            RepositoryQueryArgs,
            source,
        )
        original = spec.input_schema_json

        source["properties"]["path"]["maxLength"] = 999  # type: ignore[index]
        exposed = spec.input_schema
        exposed["properties"]["path"]["maxLength"] = 888

        self.assertEqual(original, spec.input_schema_json)
        self.assertEqual(32, spec.input_schema["properties"]["path"]["maxLength"])

    def test_decode_rejects_missing_extra_wrong_types_and_bounds(self) -> None:
        cases = (
            ("{}", "missing_required", "path"),
            (
                '{"path":"x","count":1,"case_sensitive":false,"globs":[],'
                '"secret_value":"do-not-echo"}',
                "additional_property",
                "secret_value",
            ),
            (
                '{"path":"x","count":true,"case_sensitive":false,"globs":[]}',
                "wrong_type",
                "count",
            ),
            (
                '{"path":"x","count":0,"case_sensitive":false,"globs":[]}',
                "below_minimum",
                "count",
            ),
            (
                '{"path":"x","count":21,"case_sensitive":false,"globs":[]}',
                "above_maximum",
                "count",
            ),
            (
                '{"path":"","count":1,"case_sensitive":false,"globs":[]}',
                "too_short",
                "path",
            ),
            (
                '{"path":"x","count":1,"case_sensitive":false,'
                '"globs":["a","b","c","d"]}',
                "too_many_items",
                "globs",
            ),
            (
                '{"path":"x","count":1,"case_sensitive":false,"globs":[1]}',
                "wrong_type",
                "globs[0]",
            ),
            (
                '{"path":"\\ud800","count":1,"case_sensitive":false,"globs":[]}',
                "invalid_unicode",
                "path",
            ),
            (
                '{"path":"x","count":1,"case_sensitive":false,'
                '"globs":["\\udfff"]}',
                "invalid_unicode",
                "globs[0]",
            ),
        )

        for raw, reason, field in cases:
            with self.subTest(reason=reason, field=field):
                with self.assertRaises(ToolArgumentError) as raised:
                    self.spec().decode(raw)
                self.assertEqual("invalid_tool_arguments", raised.exception.code)
                self.assertEqual(reason, raised.exception.reason)
                self.assertEqual(field, raised.exception.field)

    def test_decode_rejects_malformed_duplicate_and_non_object_json(self) -> None:
        cases = (
            ('{"path":', "malformed_json"),
            ('{"path":"a","path":"b"}', "duplicate_key"),
            ('{"nested":{"x":1,"x":2}}', "duplicate_key"),
            ('{"count":NaN}', "non_json_number"),
            ("[]", "wrong_top_level_type"),
        )
        for raw, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(ToolArgumentError) as raised:
                    self.spec().decode(raw)
                self.assertEqual(reason, raised.exception.reason)

    def test_unknown_schema_keywords_are_rejected_at_startup(self) -> None:
        cases = []

        root_unknown = _schema()
        root_unknown["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        cases.append(root_unknown)

        property_unknown = _schema()
        property_unknown["properties"]["path"]["pattern"] = ".*"  # type: ignore[index]
        cases.append(property_unknown)

        item_unknown = _schema()
        item_unknown["properties"]["globs"]["items"]["enum"] = ["*.py"]  # type: ignore[index]
        cases.append(item_unknown)

        for schema in cases:
            with self.subTest(schema=schema):
                with self.assertRaises(ToolConfigurationError) as raised:
                    ToolSpec(
                        "search_text",
                        "Search bounded repository text",
                        RepositoryQueryArgs,
                        schema,
                    )
                self.assertEqual("unsupported_schema_keyword", raised.exception.code)

    def test_schema_descriptions_must_be_valid_utf8_text(self) -> None:
        with self.assertRaises(ToolConfigurationError) as raised:
            ToolSpec(
                "search_text",
                "invalid-\ud800-description",
                RepositoryQueryArgs,
                _schema(),
            )
        self.assertEqual("invalid_tool_schema", raised.exception.code)

    def test_root_is_closed_and_arrays_only_contain_scalars(self) -> None:
        open_object = _schema()
        open_object["additionalProperties"] = True

        nested_array = _schema()
        nested_array["properties"]["globs"]["items"] = {  # type: ignore[index]
            "type": "array",
            "items": {"type": "boolean"},
            "maxItems": 2,
        }

        for schema in (open_object, nested_array):
            with self.subTest(schema=schema):
                with self.assertRaises(ToolConfigurationError) as raised:
                    ToolSpec(
                        "search_text",
                        "Search bounded repository text",
                        RepositoryQueryArgs,
                        schema,
                    )
                self.assertEqual("invalid_tool_schema", raised.exception.code)

    def test_schema_integer_constraints_reject_bool(self) -> None:
        schema = _schema()
        schema["properties"]["count"]["minimum"] = False  # type: ignore[index]

        with self.assertRaises(ToolConfigurationError) as raised:
            ToolSpec(
                "search_text",
                "Search bounded repository text",
                RepositoryQueryArgs,
                schema,
            )

        self.assertEqual("invalid_tool_schema", raised.exception.code)

    def test_dataclass_shape_required_defaults_and_annotations_must_match(self) -> None:
        @dataclass(frozen=True, slots=True)
        class WrongType:
            path: int

        wrong_type_schema = {
            "type": "object",
            "properties": {"path": {"type": "string", "maxLength": 8}},
            "required": ["path"],
            "additionalProperties": False,
        }

        with self.assertRaises(ToolConfigurationError) as wrong_type:
            ToolSpec("read_file", "Read a file", WrongType, wrong_type_schema)
        self.assertEqual("tool_arguments_type_mismatch", wrong_type.exception.code)

        @dataclass(frozen=True, slots=True)
        class RequiredWithDefault:
            path: str = "."

        with self.assertRaises(ToolConfigurationError) as default_mismatch:
            ToolSpec(
                "read_file",
                "Read a file",
                RequiredWithDefault,
                wrong_type_schema,
            )
        self.assertEqual("tool_arguments_type_mismatch", default_mismatch.exception.code)

        @dataclass(slots=True)
        class MutableArguments:
            path: str

        with self.assertRaises(ToolConfigurationError) as mutable:
            ToolSpec("read_file", "Read a file", MutableArguments, wrong_type_schema)
        self.assertEqual("invalid_tool_arguments_type", mutable.exception.code)


if __name__ == "__main__":
    unittest.main()
