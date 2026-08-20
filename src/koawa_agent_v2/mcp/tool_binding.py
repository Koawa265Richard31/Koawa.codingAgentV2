"""Immutable MCP tool catalogs bound to a session generation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, make_dataclass
from types import MappingProxyType
from typing import Any

from ..model.protocol import ToolDefinition
from ..tools.errors import ToolConfigurationError
from ..tools.registry import ToolRegistry
from ..tools.schema import ToolSpec


_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PROPERTY_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")

_MAX_DESCRIPTION_CHARS = 2_048
_MAX_PROPERTIES = 64
_MAX_STRING_CHARS = 1_000_000
_MAX_ARRAY_ITEMS = 10_000


class McpBindingError(RuntimeError):
    """Stable, content-free catalog binding failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _bounded_text(value: Any, code: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise McpBindingError(code)
    if len(value) > maximum or "\x00" in value:
        raise McpBindingError(code)
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise McpBindingError(code) from None
    return value


def _integer(value: Any, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise McpBindingError(code)
    return value


def _validate_value_schema(value: Any, *, allow_array: bool) -> None:
    if not isinstance(value, Mapping):
        raise McpBindingError("unsupported_mcp_schema")
    kind = value.get("type")
    if kind == "string":
        if set(value) - {"type", "description", "minLength", "maxLength"}:
            raise McpBindingError("unsupported_mcp_schema")
        if "minLength" not in value or "maxLength" not in value:
            raise McpBindingError("unsupported_mcp_schema")
        minimum = _integer(value["minLength"], "unsupported_mcp_schema")
        maximum = _integer(value["maxLength"], "unsupported_mcp_schema")
        if (
            minimum < 0
            or maximum < minimum
            or maximum > _MAX_STRING_CHARS
        ):
            raise McpBindingError("unsupported_mcp_schema")
    elif kind == "integer":
        if set(value) - {"type", "description", "minimum", "maximum"}:
            raise McpBindingError("unsupported_mcp_schema")
        if "minimum" not in value or "maximum" not in value:
            raise McpBindingError("unsupported_mcp_schema")
        minimum = _integer(value["minimum"], "unsupported_mcp_schema")
        maximum = _integer(value["maximum"], "unsupported_mcp_schema")
        if minimum > maximum:
            raise McpBindingError("unsupported_mcp_schema")
    elif kind == "boolean":
        if set(value) - {"type", "description"}:
            raise McpBindingError("unsupported_mcp_schema")
    elif kind == "array" and allow_array:
        if set(value) - {"type", "description", "items", "minItems", "maxItems"}:
            raise McpBindingError("unsupported_mcp_schema")
        if "items" not in value or "maxItems" not in value:
            raise McpBindingError("unsupported_mcp_schema")
        minimum = _integer(value.get("minItems", 0), "unsupported_mcp_schema")
        maximum = _integer(value["maxItems"], "unsupported_mcp_schema")
        if (
            minimum < 0
            or maximum < minimum
            or maximum > _MAX_ARRAY_ITEMS
        ):
            raise McpBindingError("unsupported_mcp_schema")
        _validate_value_schema(value["items"], allow_array=False)
    else:
        raise McpBindingError("unsupported_mcp_schema")


def _validate_input_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        raise McpBindingError("unsupported_mcp_schema")
    if set(schema) != {"type", "properties", "required", "additionalProperties"}:
        raise McpBindingError("unsupported_mcp_schema")
    if schema.get("type") != "object":
        raise McpBindingError("unsupported_mcp_schema")
    if schema.get("additionalProperties") is not False:
        raise McpBindingError("unsupported_mcp_schema")
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, Mapping) or len(properties) > _MAX_PROPERTIES:
        raise McpBindingError("unsupported_mcp_schema")
    if (
        not isinstance(required, list)
        or any(not isinstance(item, str) for item in required)
    ):
        raise McpBindingError("unsupported_mcp_schema")
    if set(required) - set(properties):
        raise McpBindingError("unsupported_mcp_schema")
    for name, property_schema in properties.items():
        if not _PROPERTY_NAME.fullmatch(str(name)):
            raise McpBindingError("unsupported_mcp_schema")
        _validate_value_schema(property_schema, allow_array=True)
    return dict(schema)


def validate_server_tool(server_id: str, tool: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one MCP tool declaration into a binding-ready document."""

    if not isinstance(server_id, str) or not _TOOL_NAME.fullmatch(server_id):
        raise McpBindingError("invalid_mcp_server_id")
    if not isinstance(tool, Mapping):
        raise McpBindingError("invalid_mcp_tool")
    name = tool.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        raise McpBindingError("invalid_mcp_tool_name")
    description = _bounded_text(
        tool.get("description", ""),
        "invalid_mcp_tool_description",
        maximum=_MAX_DESCRIPTION_CHARS,
        allow_empty=True,
    )
    schema = _validate_input_schema(tool.get("inputSchema"))
    return {
        "name": name,
        "description": description or None,
        "inputSchema": schema,
        "result": tool.get("result"),
        "isError": bool(tool.get("isError", False)),
    }


def _canonical_schema(schema: Mapping[str, Any]) -> str:
    return json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _annotation_and_default(property_schema: Mapping[str, Any]) -> tuple[Any, Any]:
    kind = property_schema["type"]
    if kind == "string":
        return str, ""
    if kind == "integer":
        minimum = property_schema.get("minimum", 0)
        return int, minimum
    if kind == "boolean":
        return bool, False
    item_kind = property_schema["items"]["type"]
    item_annotation = {"string": str, "integer": int, "boolean": bool}[item_kind]
    return list[item_annotation], ()


def _build_arguments_type(
    registry_name: str,
    schema: Mapping[str, Any],
) -> type:
    properties = schema["properties"]
    required = set(schema["required"])
    fields = []
    ordered = sorted(required) + sorted(set(properties) - required)
    for name in ordered:
        property_schema = properties[name]
        annotation, default = _annotation_and_default(property_schema)
        if name in required:
            fields.append((name, annotation))
        else:
            fields.append((name, annotation, default))
    digest = hashlib.sha256(registry_name.encode("utf-8")).hexdigest()[:8]
    return make_dataclass(f"McpArgs_{digest}", fields, frozen=True)


def _schema_hash(schema: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_schema(schema).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class McpBinding:
    """One immutable (server, generation, tool, schema) binding."""

    server_id: str
    session_generation: int
    tool_name: str
    registry_name: str
    schema_hash: str
    spec: ToolSpec[Any]
    arguments_type: type

    @property
    def binding_digest(self) -> str:
        document = {
            "server_id": self.server_id,
            "session_generation": self.session_generation,
            "tool_name": self.tool_name,
            "schema_hash": self.schema_hash,
        }
        canonical = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class McpCatalog:
    generation: int
    bindings: Mapping[str, McpBinding]
    definitions: tuple[ToolDefinition, ...]


def bind_catalog(
    server_id: str,
    generation: int,
    tools: list[Mapping[str, Any]],
) -> McpCatalog:
    """Build the immutable generation catalog; any invalid tool fails closed."""

    if not isinstance(server_id, str) or not _TOOL_NAME.fullmatch(server_id):
        raise McpBindingError("invalid_mcp_server_id")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise McpBindingError("invalid_mcp_generation")
    bindings: dict[str, McpBinding] = {}
    definitions: list[ToolDefinition] = []
    for tool in sorted(tools, key=lambda item: str(item.get("name", ""))):
        validated = validate_server_tool(server_id, tool)
        registry_name = f"{server_id}__{validated['name']}"
        if not _TOOL_NAME.fullmatch(registry_name):
            raise McpBindingError("invalid_mcp_registry_name")
        if registry_name in bindings:
            raise McpBindingError("duplicate_mcp_tool_name")
        arguments_type = _build_arguments_type(
            registry_name, validated["inputSchema"],
        )
        try:
            spec = ToolSpec(
                registry_name,
                validated["description"],
                arguments_type,
                validated["inputSchema"],
            )
        except (ToolConfigurationError, TypeError, ValueError):
            raise McpBindingError("unsupported_mcp_schema") from None
        binding = McpBinding(
            server_id=server_id,
            session_generation=generation,
            tool_name=validated["name"],
            registry_name=registry_name,
            schema_hash=_schema_hash(validated["inputSchema"]),
            spec=spec,
            arguments_type=arguments_type,
        )
        bindings[registry_name] = binding
        definitions.append(spec.definition())
    return McpCatalog(
        generation=generation,
        bindings=MappingProxyType(bindings),
        definitions=tuple(definitions),
    )


class McpRegistryAdapter:
    """D3 ToolExecutor facade that adds the binding digest to D7 identity."""

    def __init__(
        self,
        registry: ToolRegistry,
        bindings: Mapping[str, McpBinding],
    ) -> None:
        self._registry = registry
        self._bindings = dict(bindings)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._registry.definitions()

    def prepare(self, call):
        return self._registry.prepare(call)

    def invoke_prepared(self, prepared, *, context, authority):
        return self._registry.invoke_prepared(
            prepared, context=context, authority=authority,
        )

    def bind_policy_authority(self, authority) -> None:
        self._registry.bind_policy_authority(authority)

    def discard_prepared(self, prepared, *, authority) -> bool:
        return self._registry.discard_prepared(prepared, authority=authority)

    def execute(self, call, *, context):
        return self._registry.execute(call, context=context)

    def binding_digest(self, tool_name: str) -> str | None:
        binding = self._bindings.get(tool_name)
        return None if binding is None else binding.binding_digest


def build_mcp_registry(session, catalog: McpCatalog) -> McpRegistryAdapter:
    """Register one generation catalog into a D3 registry with live handlers."""

    registry = ToolRegistry()
    bindings: dict[str, McpBinding] = {}
    for registry_name in sorted(catalog.bindings):
        binding = catalog.bindings[registry_name]
        registry.register(binding.spec, session.handler(binding))
        bindings[registry_name] = binding
    return McpRegistryAdapter(registry, bindings)
