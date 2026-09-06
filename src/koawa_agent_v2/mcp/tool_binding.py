"""Immutable MCP tool catalogs bound to a session generation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, make_dataclass
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from ..model.protocol import ToolDefinition
from ..tools.errors import ToolConfigurationError
from ..tools.registry import ToolRegistry
from ..tools.schema import ToolSpec


_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PROPERTY_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")

__all__ = [
    "McpBinding",
    "McpBindingError",
    "McpCatalog",
    "McpRegistryAdapter",
    "PhysicalSessionFence",
    "PreparedMcpInvocation",
    "SemanticMcpBinding",
    "bind_catalog",
    "build_mcp_registry",
]

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


_META_KEYS = frozenset({"$schema", "$id", "title", "description", "examples", "default"})
_SAFE_STRING_MAX = 4_096
_SAFE_INT_MIN = -(2**53)
_SAFE_INT_MAX = 2**53


def _normalize_third_party_value(value: Any, *, allow_array: bool) -> dict[str, Any]:
    """Normalize one untrusted property schema in place (constraint-neutral).

    Meta keywords carry no constraint semantics and are stripped; missing
    bounds get conservative defaults so the compiled decoder stays typed and
    bounded.  Constraint keywords the compiler cannot model are left in
    place and fail closed in the strict validator below.
    """
    if not isinstance(value, Mapping):
        raise McpBindingError("unsupported_mcp_schema")
    normalized = {key: item for key, item in value.items() if key not in _META_KEYS}
    kind = normalized.get("type")
    if kind == "string":
        normalized.setdefault("minLength", 0)
        normalized.setdefault("maxLength", _SAFE_STRING_MAX)
    elif kind == "integer":
        normalized.setdefault("minimum", _SAFE_INT_MIN)
        normalized.setdefault("maximum", _SAFE_INT_MAX)
    elif kind == "array" and allow_array:
        normalized.setdefault("minItems", 0)
        normalized.setdefault("maxItems", 64)
        if "items" in normalized:
            normalized["items"] = _normalize_third_party_value(
                normalized["items"], allow_array=False
            )
    return {key: normalized[key] for key in sorted(normalized)}


def _normalize_third_party_schema(schema: Any) -> dict[str, Any]:
    """Bring an untrusted third-party inputSchema to the D3 boundary shape.

    Only constraint-free meta keywords are removed; *stricter* defaults are
    introduced for absent keys (absent bounds become safe caps).  One
    exception is deliberate (D25: 强制 ``additionalProperties:false``，只严不松):
    ``additionalProperties`` is FORCED to ``False`` even when the third-party
    server explicitly declares ``True`` — the runtime never honors a request
    to widen its own argument surface.  Anything genuinely outside the
    modeled subset still fails closed in ``_validate_input_schema``.
    """
    if not isinstance(schema, Mapping):
        raise McpBindingError("unsupported_mcp_schema")
    normalized = {key: item for key, item in schema.items() if key not in _META_KEYS}
    normalized["additionalProperties"] = False
    properties = normalized.get("properties")
    if isinstance(properties, Mapping):
        normalized["properties"] = {
            str(name): _normalize_third_party_value(prop, allow_array=True)
            for name, prop in properties.items()
        }
    return {key: normalized[key] for key in sorted(normalized)}


def _validate_input_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        raise McpBindingError("unsupported_mcp_schema")
    normalized = dict(schema)
    if "required" not in normalized:
        # Normalization: a third-party object schema may declare no required
        # list; absence means the same thing as an empty one.
        normalized["required"] = []
    if set(normalized) != {"type", "properties", "required", "additionalProperties"}:
        raise McpBindingError("unsupported_mcp_schema")
    if normalized.get("type") != "object":
        raise McpBindingError("unsupported_mcp_schema")
    if normalized.get("additionalProperties") is not False:
        raise McpBindingError("unsupported_mcp_schema")
    properties = normalized.get("properties")
    required = normalized.get("required")
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
    return dict(normalized)


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
    schema = _validate_input_schema(_normalize_third_party_schema(tool.get("inputSchema")))
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


# ---- I6 §8.8 semantic vs physical binding ------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class SemanticMcpBinding:
    """Cross-process, recovery-stable binding identity.

    Same approved launch + same catalog -> same semantic binding in every
    session; excluded are the session instance / connection epoch / live
    catalog generation (those live in PhysicalSessionFence only).
    """

    server_id: str
    launch_identity_digest: str
    catalog_digest: str
    catalog_epoch_id: UUID
    tool_name: str
    schema_hash: str
    binding_digest: str

    def to_document(self) -> dict[str, object]:
        return {
            "server_id": self.server_id,
            "launch_identity_digest": self.launch_identity_digest,
            "catalog_digest": self.catalog_digest,
            "catalog_epoch_id": str(self.catalog_epoch_id),
            "tool_name": self.tool_name,
            "schema_hash": self.schema_hash,
        }

    def __repr__(self) -> str:
        return (
            f"SemanticMcpBinding(server_id={self.server_id!r}, "
            f"tool_name={self.tool_name!r}, binding_digest={self.binding_digest!r})"
        )


@dataclass(frozen=True, slots=True)
class PhysicalSessionFence:
    """Live-handle guard; never enters ledger/approval/recovery identity."""

    session_instance_id: UUID
    connection_epoch: int = 0
    catalog_generation: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.session_instance_id, UUID):
            raise TypeError("session_instance_id must be UUID")
        if (
            not isinstance(self.connection_epoch, int)
            or isinstance(self.connection_epoch, bool)
            or self.connection_epoch < 0
        ):
            raise ValueError("connection_epoch must be int >= 0")
        if (
            not isinstance(self.catalog_generation, int)
            or isinstance(self.catalog_generation, bool)
            or self.catalog_generation < 1
        ):
            raise ValueError("catalog_generation must be int >= 1")


@dataclass(frozen=True, slots=True, repr=False)
class PreparedMcpInvocation:
    """Immutable prepared handle + semantic binding + physical fence.

    ``prepared_call`` is the delegate's own prepared token; the caller may
    NOT re-query the dynamic registry by tool name after prepare (§8.8).
    """

    prepared_call: object
    semantic_binding: SemanticMcpBinding
    physical_fence: PhysicalSessionFence

    @property
    def early_result(self):
        return getattr(self.prepared_call, "early_result", None)

    @property
    def binding_digest(self) -> str | None:
        return self.semantic_binding.binding_digest

    def __repr__(self) -> str:
        return (
            f"PreparedMcpInvocation(tool={self.semantic_binding.tool_name!r}, "
            f"binding_digest={self.binding_digest!r})"
        )


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
    catalog_digest: str = ""
    catalog_epoch_id: UUID | None = None
    semantic_bindings: Mapping[str, SemanticMcpBinding | None] = MappingProxyType({})


def _catalog_digest_for(
    server_id: str,
    tools: list[Mapping[str, Any]],
) -> str:
    """I6 §8.8: sorted tool names + exact schemas (generation independent)."""
    validated: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise McpBindingError("invalid_mcp_tool")
        name = tool.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise McpBindingError("invalid_mcp_tool_name")
        schema = tool.get("inputSchema")
        validated.append({"name": name, "schema": _canonical_schema(schema)})
    validated.sort(key=lambda item: item["name"])
    return hashlib.sha256(
        json.dumps(
            validated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    ).hexdigest()


def _catalog_epoch_id(
    server_id: str,
    catalog_digest: str,
    launch_identity_digest: str | None,
) -> UUID:
    if launch_identity_digest is None:
        namespace = uuid5(NAMESPACE_URL, f"koawa-mcp:{server_id}")
    else:
        namespace = uuid5(NAMESPACE_URL, f"koawa-mcp-launch:{launch_identity_digest}")
    return uuid5(namespace, "catalog:" + catalog_digest)


def bind_catalog(
    server_id: str,
    generation: int,
    tools: list[Mapping[str, Any]],
    *,
    launch_identity_digest: str | None = None,
    tool_allowlist: frozenset[str] | None = None,
) -> McpCatalog:
    """Build the immutable generation catalog; any invalid tool fails closed.

    ``tool_allowlist`` (D25) is an admin-declared subset of server tool names;
    tools outside it never reach validation or the registry - they simply do
    not exist downstream (calls deny with ``mcp_binding_required``).  Without
    an allowlist every tool must validate or the catalog fails closed.
    """
    if tool_allowlist is not None:
        if not isinstance(tool_allowlist, frozenset):
            raise McpBindingError("invalid_mcp_tool_allowlist")
        tools = [
            tool for tool in tools
            if isinstance(tool, Mapping)
            and tool.get("name") in tool_allowlist
        ]

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
    catalog_digest = _catalog_digest_for(server_id, tools)
    catalog_epoch_id = _catalog_epoch_id(
        server_id, catalog_digest, launch_identity_digest,
    )
    semantic: dict[str, SemanticMcpBinding | None] = {}
    for registry_name, binding in bindings.items():
        semantic[registry_name] = _semantic_binding(
            binding,
            launch_identity_digest=launch_identity_digest,
            catalog_digest=catalog_digest,
            catalog_epoch_id=catalog_epoch_id,
        )
    return McpCatalog(
        generation=generation,
        bindings=MappingProxyType(bindings),
        definitions=tuple(definitions),
        catalog_digest=catalog_digest,
        catalog_epoch_id=catalog_epoch_id,
        semantic_bindings=MappingProxyType(semantic),
    )


def _semantic_binding(
    binding: McpBinding,
    *,
    launch_identity_digest: str | None,
    catalog_digest: str,
    catalog_epoch_id: UUID,
) -> SemanticMcpBinding:
    digest = hashlib.sha256(
        json.dumps(
            {
                "server_id": binding.server_id,
                "launch_identity_digest": launch_identity_digest,
                "catalog_digest": catalog_digest,
                "catalog_epoch_id": str(catalog_epoch_id),
                "tool_name": binding.tool_name,
                "schema_hash": binding.schema_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    ).hexdigest()
    return SemanticMcpBinding(
        server_id=binding.server_id,
        launch_identity_digest=launch_identity_digest or "legacy",
        catalog_digest=catalog_digest,
        catalog_epoch_id=catalog_epoch_id,
        tool_name=binding.tool_name,
        schema_hash=binding.schema_hash,
        binding_digest=digest,
    )


class McpRegistryAdapter:
    """D3 ToolExecutor facade exposing semantic + physical binding identity."""

    def __init__(
        self,
        registry: ToolRegistry,
        bindings: Mapping[str, McpBinding],
        *,
        semantic_bindings: Mapping[str, SemanticMcpBinding | None] | None = None,
        session_instance_id: UUID | None = None,
    ) -> None:
        self._registry = registry
        self._bindings = dict(bindings)
        self._semantic = dict(semantic_bindings or {})
        self._session_instance_id = session_instance_id or uuid4()
        self._catalog_generation = max(
            (binding.session_generation for binding in self._bindings.values()),
            default=1,
        )
        self._catalog_epoch_id = None

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._registry.definitions()

    def prepare(self, call):
        """Return an immutable prepared + semantic/physical binding (I6 §8.8).

        The caller must not re-query the live registry by tool name after
        prepare; the semantic binding is carried on the prepared token.
        """
        registry_name = call.name
        prepared = self._registry.prepare(call)
        semantic = self._semantic.get(registry_name)
        if semantic is None and registry_name in self._bindings:
            binding = self._bindings[registry_name]
            semantic = _semantic_binding(
                binding,
                launch_identity_digest=None,
                catalog_digest="",
                catalog_epoch_id=uuid5(
                    NAMESPACE_URL, "koawa-mcp:" + binding.server_id,
                ),
            )
        fence = PhysicalSessionFence(
            session_instance_id=self._session_instance_id,
            connection_epoch=0,
            catalog_generation=self._catalog_generation,
        )
        return PreparedMcpInvocation(
            prepared_call=prepared,
            semantic_binding=semantic,
            physical_fence=fence,
        )

    def invoke_prepared(self, prepared, *, context, authority):
        return self._registry.invoke_prepared(
            _unwrap(prepared), context=context, authority=authority,
        )

    def bind_policy_authority(self, authority) -> None:
        self._registry.bind_policy_authority(authority)

    def discard_prepared(self, prepared, *, authority) -> bool:
        return self._registry.discard_prepared(
            _unwrap(prepared), authority=authority,
        )

    def execute(self, call, *, context):
        return self._registry.execute(call, context=context)

    def binding_digest(self, tool_name: str) -> str | None:
        binding = self._bindings.get(tool_name)
        return None if binding is None else binding.binding_digest

    def semantic_bindings(self) -> Mapping[str, SemanticMcpBinding | None]:
        return MappingProxyType(self._semantic)

    def set_catalog_epoch_id(self, epoch_id: UUID | None) -> None:
        self._catalog_epoch_id = epoch_id

    def catalog_epoch_id(self) -> UUID | None:
        return self._catalog_epoch_id


def build_mcp_registry(session, catalog: McpCatalog) -> McpRegistryAdapter:
    """Register one generation catalog into a D3 registry with live handlers."""

    registry = ToolRegistry()
    bindings: dict[str, McpBinding] = {}
    for registry_name in sorted(catalog.bindings):
        binding = catalog.bindings[registry_name]
        registry.register(binding.spec, session.handler(binding))
        bindings[registry_name] = binding
    session_instance_id = getattr(session, "session_instance_id", None)
    adapter = McpRegistryAdapter(
        registry,
        bindings,
        semantic_bindings=catalog.semantic_bindings,
        session_instance_id=session_instance_id,
    )
    adapter.set_catalog_epoch_id(catalog.catalog_epoch_id)
    return adapter


def _unwrap(prepared):
    """Return the raw delegate prepared token from an I6 wrapper."""
    if isinstance(prepared, PreparedMcpInvocation):
        return prepared.prepared_call
    return prepared
