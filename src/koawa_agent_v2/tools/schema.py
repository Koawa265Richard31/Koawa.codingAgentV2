"""D3 ToolSpec 与受限 JSON Schema 编译器。

这里刻意只实现 Coding Tools 所需的小子集。ToolSpec 构造时把同一份 schema
编译为 canonical ``ToolDefinition`` 和运行时 validator/typed dataclass decoder，
避免 Provider schema、参数校验与 handler 入参三套规则彼此漂移。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, fields, is_dataclass
from typing import Any, Generic, TypeVar, get_args, get_origin, get_type_hints

from ..model.protocol import ToolDefinition
from .errors import ToolArgumentError, ToolConfigurationError


ArgumentsT = TypeVar("ArgumentsT")

MAX_TOOL_ARGUMENT_JSON_CHARS = 262_144

_MAX_PROPERTIES = 64
_MAX_DESCRIPTION_CHARS = 2_048
_MAX_STRING_CHARS = 1_000_000
_MAX_ARRAY_ITEMS = 10_000
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PROPERTY_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ROOT_KEYWORDS = {"type", "properties", "required", "additionalProperties"}
_COMMON_VALUE_KEYWORDS = {"type", "description"}
_STRING_KEYWORDS = _COMMON_VALUE_KEYWORDS | {"minLength", "maxLength"}
_INTEGER_KEYWORDS = _COMMON_VALUE_KEYWORDS | {"minimum", "maximum"}
_BOOLEAN_KEYWORDS = _COMMON_VALUE_KEYWORDS
_ARRAY_KEYWORDS = _COMMON_VALUE_KEYWORDS | {"items", "minItems", "maxItems"}


@dataclass(frozen=True, slots=True)
class _ValueSchema:
    kind: str
    description: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    min_length: int | None = None
    max_length: int | None = None
    min_items: int | None = None
    max_items: int | None = None
    items: "_ValueSchema | None" = None

    def document(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.kind}
        if self.description is not None:
            result["description"] = self.description
        if self.kind == "string":
            result["minLength"] = self.min_length
            result["maxLength"] = self.max_length
        elif self.kind == "integer":
            result["minimum"] = self.minimum
            result["maximum"] = self.maximum
        elif self.kind == "array":
            if self.items is None:  # impossible after startup compilation
                raise RuntimeError("compiled array schema is missing items")
            result["items"] = self.items.document()
            result["minItems"] = self.min_items
            result["maxItems"] = self.max_items
        return result

    def validate(self, value: Any, *, field: str) -> Any:
        if self.kind == "string":
            if not isinstance(value, str):
                raise ToolArgumentError(
                    "wrong_type", field=field, expected="value-type:string"
                )
            # json.loads 会接受 ``\ud800`` 这类未配对 surrogate，但它不能被
            # UTF-8 编码。若让它进入 ToolResult，下一轮 Provider 请求才会在
            # 序列化阶段失败，错误位置也会从参数边界漂移到 transport 边界。
            try:
                value.encode("utf-8", "strict")
            except UnicodeError:
                raise ToolArgumentError("invalid_unicode", field=field) from None
            if self.min_length is not None and len(value) < self.min_length:
                raise ToolArgumentError("too_short", field=field)
            if self.max_length is not None and len(value) > self.max_length:
                raise ToolArgumentError("too_long", field=field)
            return value
        if self.kind == "integer":
            # bool 是 int 的子类，但不是 JSON Schema integer 的本地工具语义。
            if not isinstance(value, int) or isinstance(value, bool):
                raise ToolArgumentError(
                    "wrong_type", field=field, expected="value-type:integer"
                )
            if self.minimum is not None and value < self.minimum:
                raise ToolArgumentError(
                    "below_minimum",
                    field=field,
                    expected=f"value-type:integer minimum:{self.minimum}",
                )
            if self.maximum is not None and value > self.maximum:
                raise ToolArgumentError(
                    "above_maximum",
                    field=field,
                    expected=f"value-type:integer maximum:{self.maximum}",
                )
            return value
        if self.kind == "boolean":
            if not isinstance(value, bool):
                raise ToolArgumentError(
                    "wrong_type", field=field, expected="value-type:boolean"
                )
            return value
        if self.kind == "array":
            if not isinstance(value, list):
                raise ToolArgumentError(
                    "wrong_type", field=field, expected="value-type:array"
                )
            if self.min_items is not None and len(value) < self.min_items:
                raise ToolArgumentError("too_few_items", field=field)
            if self.max_items is not None and len(value) > self.max_items:
                raise ToolArgumentError("too_many_items", field=field)
            if self.items is None:  # impossible after startup compilation
                raise RuntimeError("compiled array schema is missing items")
            return [
                self.items.validate(item, field=f"{field}[{index}]")
                for index, item in enumerate(value)
            ]
        raise RuntimeError("compiled tool schema has an unknown kind")


@dataclass(frozen=True, slots=True)
class _CompiledProperty:
    name: str
    schema: _ValueSchema
    required: bool
    array_container: str | None


@dataclass(frozen=True, slots=True)
class _CompiledObject(Generic[ArgumentsT]):
    arguments_type: type[ArgumentsT]
    properties: tuple[_CompiledProperty, ...]
    schema_document: dict[str, Any]

    def decode(self, arguments_json: str) -> ArgumentsT:
        document = _strict_arguments_object(arguments_json)
        known = {item.name for item in self.properties}
        extras = sorted(set(document) - known)
        if extras:
            unsafe_name = extras[0]
            field = unsafe_name if _PROPERTY_NAME.fullmatch(unsafe_name) else None
            raise ToolArgumentError("additional_property", field=field)
        missing = [
            item.name
            for item in self.properties
            if item.required and item.name not in document
        ]
        if missing:
            raise ToolArgumentError("missing_required", field=missing[0])

        values: dict[str, Any] = {}
        for item in self.properties:
            if item.name not in document:
                continue
            value = item.schema.validate(document[item.name], field=item.name)
            if item.array_container == "tuple":
                value = tuple(value)
            elif item.array_container == "list":
                value = list(value)
            values[item.name] = value
        try:
            return self.arguments_type(**values)
        except (TypeError, ValueError):
            # 自定义 __post_init__ 不得把参数正文带出 Registry。
            raise ToolArgumentError("typed_decoder_rejected") from None


@dataclass(frozen=True, slots=True, repr=False, init=False)
class ToolSpec(Generic[ArgumentsT]):
    """一份同时生成 Provider definition、validator 和 typed decoder 的工具规格。"""

    name: str
    description: str | None
    arguments_type: type[ArgumentsT]
    _definition: ToolDefinition
    _compiled: _CompiledObject[ArgumentsT]

    def __init__(
        self,
        name: str,
        description: str | None,
        arguments_type: type[ArgumentsT],
        input_schema: Mapping[str, Any],
    ) -> None:
        name = _tool_name(name)
        description = _description(description, required=False)
        compiled = _compile_object_schema(arguments_type, input_schema)
        schema_json = json.dumps(
            compiled.schema_document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "arguments_type", arguments_type)
        object.__setattr__(
            self,
            "_definition",
            ToolDefinition(name, description, schema_json),
        )
        object.__setattr__(self, "_compiled", compiled)

    @property
    def input_schema_json(self) -> str:
        return self._definition.input_schema_json

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._definition.input_schema

    def definition(self) -> ToolDefinition:
        """返回由已编译 schema 产生的 immutable D2 定义。"""
        return self._definition

    def decode(self, arguments_json: str) -> ArgumentsT:
        """严格验证 JSON 后构造 handler 专用 typed arguments dataclass。"""
        return self._compiled.decode(arguments_json)

    def __repr__(self) -> str:
        return (
            f"ToolSpec(name={self.name!r}, arguments_type={self.arguments_type.__name__!r}, "
            f"schema_length={len(self.input_schema_json)})"
        )


def _compile_object_schema(
    arguments_type: type[ArgumentsT],
    raw: Mapping[str, Any],
) -> _CompiledObject[ArgumentsT]:
    if not isinstance(raw, Mapping):
        raise ToolConfigurationError("invalid_tool_schema")
    _reject_unknown_keywords(raw, _ROOT_KEYWORDS)
    if set(raw) != _ROOT_KEYWORDS:
        raise ToolConfigurationError("invalid_tool_schema")
    if raw.get("type") != "object":
        raise ToolConfigurationError("invalid_tool_schema")
    if raw.get("additionalProperties") is not False:
        raise ToolConfigurationError("invalid_tool_schema")
    raw_properties = raw.get("properties")
    raw_required = raw.get("required")
    if not isinstance(raw_properties, Mapping) or len(raw_properties) > _MAX_PROPERTIES:
        raise ToolConfigurationError("invalid_tool_schema")
    if not isinstance(raw_required, list):
        raise ToolConfigurationError("invalid_tool_schema")
    if any(not isinstance(name, str) for name in raw_properties):
        raise ToolConfigurationError("invalid_tool_schema")
    if any(not isinstance(name, str) for name in raw_required):
        raise ToolConfigurationError("invalid_tool_schema")
    if len(set(raw_required)) != len(raw_required):
        raise ToolConfigurationError("invalid_tool_schema")
    if any(name not in raw_properties for name in raw_required):
        raise ToolConfigurationError("invalid_tool_schema")

    required = set(raw_required)
    value_schemas: dict[str, _ValueSchema] = {}
    for name in sorted(raw_properties):
        if not _PROPERTY_NAME.fullmatch(name):
            raise ToolConfigurationError("invalid_tool_schema")
        value_schemas[name] = _compile_value_schema(raw_properties[name], allow_array=True)

    typed_properties = _bind_arguments_type(arguments_type, value_schemas, required)
    canonical_properties = {
        item.name: item.schema.document() for item in typed_properties
    }
    document = {
        "type": "object",
        "properties": canonical_properties,
        "required": sorted(required),
        "additionalProperties": False,
    }
    return _CompiledObject(arguments_type, typed_properties, document)


def _compile_value_schema(raw: Any, *, allow_array: bool) -> _ValueSchema:
    if not isinstance(raw, Mapping):
        raise ToolConfigurationError("invalid_tool_schema")
    kind = raw.get("type")
    if kind == "string":
        _reject_unknown_keywords(raw, _STRING_KEYWORDS)
        minimum = _bounded_non_negative_int(raw.get("minLength", 0), _MAX_STRING_CHARS)
        if "maxLength" not in raw:
            raise ToolConfigurationError("invalid_tool_schema")
        maximum = _bounded_non_negative_int(raw["maxLength"], _MAX_STRING_CHARS)
        if minimum > maximum:
            raise ToolConfigurationError("invalid_tool_schema")
        return _ValueSchema(
            "string",
            _description(raw.get("description"), required=False),
            min_length=minimum,
            max_length=maximum,
        )
    if kind == "integer":
        _reject_unknown_keywords(raw, _INTEGER_KEYWORDS)
        if "minimum" not in raw or "maximum" not in raw:
            raise ToolConfigurationError("invalid_tool_schema")
        minimum = _integer(raw["minimum"])
        maximum = _integer(raw["maximum"])
        if minimum > maximum:
            raise ToolConfigurationError("invalid_tool_schema")
        return _ValueSchema(
            "integer",
            _description(raw.get("description"), required=False),
            minimum=minimum,
            maximum=maximum,
        )
    if kind == "boolean":
        _reject_unknown_keywords(raw, _BOOLEAN_KEYWORDS)
        return _ValueSchema(
            "boolean",
            _description(raw.get("description"), required=False),
        )
    if kind == "array" and allow_array:
        _reject_unknown_keywords(raw, _ARRAY_KEYWORDS)
        if "items" not in raw or "maxItems" not in raw:
            raise ToolConfigurationError("invalid_tool_schema")
        minimum = _bounded_non_negative_int(raw.get("minItems", 0), _MAX_ARRAY_ITEMS)
        maximum = _bounded_non_negative_int(raw["maxItems"], _MAX_ARRAY_ITEMS)
        if minimum > maximum:
            raise ToolConfigurationError("invalid_tool_schema")
        items = _compile_value_schema(raw["items"], allow_array=False)
        return _ValueSchema(
            "array",
            _description(raw.get("description"), required=False),
            min_items=minimum,
            max_items=maximum,
            items=items,
        )
    if kind not in {"string", "integer", "boolean", "array"}:
        # $ref/composition-only documents also land here after keyword checking below.
        _reject_unknown_keywords(raw, _COMMON_VALUE_KEYWORDS | {"items"})
    raise ToolConfigurationError("invalid_tool_schema")


def _bind_arguments_type(
    arguments_type: type[ArgumentsT],
    schemas: Mapping[str, _ValueSchema],
    required: set[str],
) -> tuple[_CompiledProperty, ...]:
    if not isinstance(arguments_type, type) or not is_dataclass(arguments_type):
        raise ToolConfigurationError("invalid_tool_arguments_type")
    params = getattr(arguments_type, "__dataclass_params__", None)
    if params is None or not params.frozen:
        raise ToolConfigurationError("invalid_tool_arguments_type")
    dataclass_fields = tuple(fields(arguments_type))
    if any(not item.init for item in dataclass_fields):
        raise ToolConfigurationError("invalid_tool_arguments_type")
    if {item.name for item in dataclass_fields} != set(schemas):
        raise ToolConfigurationError("tool_arguments_type_mismatch")
    try:
        hints = get_type_hints(arguments_type)
    except (NameError, TypeError):
        raise ToolConfigurationError("invalid_tool_arguments_type") from None

    compiled: list[_CompiledProperty] = []
    for item in dataclass_fields:
        schema = schemas[item.name]
        has_default = item.default is not MISSING or item.default_factory is not MISSING
        if (item.name in required) == has_default:
            raise ToolConfigurationError("tool_arguments_type_mismatch")
        if item.default_factory is not MISSING:
            raise ToolConfigurationError("invalid_tool_default")
        annotation = hints.get(item.name, item.type)
        container = _match_annotation(annotation, schema)
        if has_default:
            default = item.default
            candidate = list(default) if schema.kind == "array" and isinstance(default, tuple) else default
            try:
                schema.validate(candidate, field=item.name)
            except ToolArgumentError:
                raise ToolConfigurationError("invalid_tool_default") from None
        compiled.append(
            _CompiledProperty(item.name, schema, item.name in required, container)
        )
    return tuple(compiled)


def _match_annotation(annotation: Any, schema: _ValueSchema) -> str | None:
    scalar_types = {"string": str, "integer": int, "boolean": bool}
    if schema.kind in scalar_types:
        if annotation is not scalar_types[schema.kind]:
            raise ToolConfigurationError("tool_arguments_type_mismatch")
        return None
    if schema.kind != "array" or schema.items is None:
        raise ToolConfigurationError("tool_arguments_type_mismatch")
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    expected_item = scalar_types[schema.items.kind]
    if origin is list and arguments == (expected_item,):
        return "list"
    if origin is tuple and arguments == (expected_item, Ellipsis):
        return "tuple"
    raise ToolConfigurationError("tool_arguments_type_mismatch")


def _strict_arguments_object(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw) > MAX_TOOL_ARGUMENT_JSON_CHARS:
        raise ToolArgumentError("malformed_json")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ToolArgumentError("duplicate_key")
            result[key] = value
        return result

    def invalid_constant(_: str) -> Any:
        raise ToolArgumentError("non_json_number")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=invalid_constant,
        )
    except ToolArgumentError:
        raise
    except (json.JSONDecodeError, UnicodeError):
        raise ToolArgumentError("malformed_json") from None
    if not isinstance(value, dict):
        raise ToolArgumentError("wrong_top_level_type")
    return value


def _reject_unknown_keywords(raw: Mapping[str, Any], allowed: set[str]) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in raw):
        raise ToolConfigurationError("unsupported_schema_keyword")


def _tool_name(value: str) -> str:
    if not isinstance(value, str) or not _TOOL_NAME.fullmatch(value):
        raise ToolConfigurationError("invalid_tool_name")
    return value


def _description(value: Any, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_DESCRIPTION_CHARS
        or "\x00" in value
    ):
        raise ToolConfigurationError("invalid_tool_schema")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise ToolConfigurationError("invalid_tool_schema") from None
    return value


def _bounded_non_negative_int(value: Any, upper_bound: int) -> int:
    value = _integer(value)
    if value < 0 or value > upper_bound:
        raise ToolConfigurationError("invalid_tool_schema")
    return value


def _integer(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolConfigurationError("invalid_tool_schema")
    return value
