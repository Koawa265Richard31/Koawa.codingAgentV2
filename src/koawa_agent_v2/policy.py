"""D9 policy-domain types, canonical action identity, and safe preflight gates.

This module deliberately has no filesystem, DNS, proxy, credential-provider, or
container side effects.  Callers resolve resources through injected functions,
then pass the immutable :class:`ResolvedAction` to :class:`PolicyEngine`.
Untrusted MCP declarations are retained only for audit/digest purposes; they
never participate in an allow decision.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeAlias
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

POLICY_SCHEMA_VERSION = 1
MAX_ARGUMENT_JSON_BYTES = 1_048_576

_STABLE_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}")
_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}")
_SCOPE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
ResourceResolver: TypeAlias = Callable[[str], "ResolvedResource"]
DNSResolver: TypeAlias = Callable[[str], Sequence[str]]


class PolicyError(RuntimeError):
    """Content-free D9 contract failure with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _STABLE_CODE.fullmatch(code):
            raise ValueError("invalid policy error code")
        self.code = code
        super().__init__(code)


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class ActionKind(StrEnum):
    BUILTIN_TOOL = "builtin_tool"
    MCP_TOOL = "mcp_tool"
    AGENT_SPAWN = "agent_spawn"


class SideEffectClass(StrEnum):
    """Policy-local risk classification, kept independent of ledger imports."""

    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    NON_IDEMPOTENT_WRITE = "non_idempotent_write"


@dataclass(frozen=True, slots=True)
class Principal:
    """Local policy identity and its already-granted capability scopes."""

    principal_id: str
    scopes: tuple[str, ...] = ()
    parent_principal_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "principal_id", _identifier(self.principal_id, "invalid_principal")
        )
        scopes = _canonical_identifiers(
            self.scopes, pattern=_SCOPE, code="invalid_principal_scope"
        )
        if self.parent_principal_id is not None:
            parent = _identifier(
                self.parent_principal_id, "invalid_parent_principal"
            )
            if parent == self.principal_id:
                raise PolicyError("invalid_parent_principal")
            object.__setattr__(self, "parent_principal_id", parent)
        object.__setattr__(self, "scopes", scopes)

    def narrow(
        self,
        child_principal_id: str,
        requested_scopes: Sequence[str],
    ) -> "Principal":
        """Create a child whose authority is the parent/request intersection."""

        requested = _canonical_identifiers(
            requested_scopes, pattern=_SCOPE, code="invalid_principal_scope"
        )
        inherited = tuple(sorted(set(self.scopes).intersection(requested)))
        return Principal(
            principal_id=child_principal_id,
            scopes=inherited,
            parent_principal_id=self.principal_id,
        )

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "principal_id": self.principal_id,
            "scopes": list(self.scopes),
            "parent_principal_id": self.parent_principal_id,
        }


def narrow_child_principal(
    parent: Principal,
    *,
    child_principal_id: str,
    requested_scopes: Sequence[str],
) -> Principal:
    if not isinstance(parent, Principal):
        raise TypeError("parent must be Principal")
    return parent.narrow(child_principal_id, requested_scopes)


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedResource:
    """A caller-resolved resource identity included verbatim in action identity."""

    resource_kind: str
    requested: str
    resolved: str
    identity: str
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resource_kind",
            _identifier(self.resource_kind, "invalid_resource_kind"),
        )
        for field_name in ("requested", "resolved", "identity"):
            _bounded_text(
                getattr(self, field_name),
                maximum_bytes=16_384,
                code="invalid_resolved_resource",
            )
        metadata = _canonical_metadata(self.metadata)
        object.__setattr__(self, "metadata", metadata)

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "resource_kind": self.resource_kind,
            "requested": self.requested,
            "resolved": self.resolved,
            "identity": self.identity,
            "metadata": [
                {"name": name, "value": value} for name, value in self.metadata
            ],
        }

    def __repr__(self) -> str:
        return (
            f"ResolvedResource(resource_kind={self.resource_kind!r}, "
            f"requested_length={len(self.requested)}, "
            f"resolved_length={len(self.resolved)}, identity_length={len(self.identity)})"
        )


@dataclass(frozen=True, slots=True)
class Origin:
    scheme: str
    host: str
    port: int

    def __post_init__(self) -> None:
        if not isinstance(self.scheme, str) or self.scheme.lower() != "https":
            raise PolicyError("https_required")
        host = _normalize_host(self.host)
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise TypeError("port must be int")
        if self.port < 1 or self.port > 65_535:
            raise PolicyError("invalid_network_port")
        object.__setattr__(self, "scheme", "https")
        object.__setattr__(self, "host", host)

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == 443 else f"{host}:{self.port}"

    def to_document(self) -> dict[str, JsonValue]:
        return {"scheme": self.scheme, "host": self.host, "port": self.port}


@dataclass(frozen=True, slots=True, repr=False)
class NetworkTarget:
    """Normalized HTTPS destination pinned to an all-global DNS answer set."""

    normalized_url: str
    origin: Origin
    resolved_addresses: tuple[str, ...]
    via_proxy: bool

    def __post_init__(self) -> None:
        if not isinstance(self.origin, Origin):
            raise TypeError("origin must be Origin")
        normalized, origin = _normalize_https_url_parts(self.normalized_url)
        if normalized != self.normalized_url or origin != self.origin:
            raise PolicyError("noncanonical_network_target")
        if not isinstance(self.via_proxy, bool):
            raise TypeError("via_proxy must be bool")
        addresses = _canonical_global_addresses(self.resolved_addresses)
        object.__setattr__(self, "resolved_addresses", addresses)

    @property
    def resolution_digest(self) -> str:
        return _sha256_document(
            {
                "schema_version": POLICY_SCHEMA_VERSION,
                "origin": self.origin.to_document(),
                "addresses": list(self.resolved_addresses),
            }
        )

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "normalized_url": self.normalized_url,
            "origin": self.origin.to_document(),
            "resolved_addresses": list(self.resolved_addresses),
            "resolution_digest": self.resolution_digest,
            "via_proxy": self.via_proxy,
        }

    def __repr__(self) -> str:
        return (
            f"NetworkTarget(origin={self.origin!r}, "
            f"address_count={len(self.resolved_addresses)}, via_proxy={self.via_proxy})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CredentialScope:
    """Credential identifiers and approved recipients; never credential values."""

    scope_id: str
    origins: tuple[Origin, ...] = ()
    server_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scope_id", _identifier(self.scope_id, "invalid_credential_scope")
        )
        if not isinstance(self.origins, tuple):
            raise TypeError("origins must be tuple")
        if any(not isinstance(origin, Origin) for origin in self.origins):
            raise TypeError("origins contains a non-Origin item")
        origins = tuple(
            sorted(set(self.origins), key=lambda item: (item.scheme, item.host, item.port))
        )
        servers = _canonical_identifiers(
            self.server_ids,
            pattern=_IDENTIFIER,
            code="invalid_credential_server",
        )
        if not origins and not servers:
            raise PolicyError("empty_credential_scope")
        object.__setattr__(self, "origins", origins)
        object.__setattr__(self, "server_ids", servers)

    def permits_origin(self, origin: Origin) -> bool:
        if not isinstance(origin, Origin):
            raise TypeError("origin must be Origin")
        return origin in self.origins

    def permits_server(self, server_id: str) -> bool:
        server_id = _identifier(server_id, "invalid_credential_server")
        return server_id in self.server_ids

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "scope_id": self.scope_id,
            "origins": [origin.to_document() for origin in self.origins],
            "server_ids": list(self.server_ids),
        }

    def __repr__(self) -> str:
        return (
            f"CredentialScope(scope_id={self.scope_id!r}, "
            f"origin_count={len(self.origins)}, server_count={len(self.server_ids)})"
        )


def normalize_https_url(url: str) -> str:
    """Return a strict ASCII-only canonical HTTPS URL without doing DNS I/O."""

    normalized, _ = _normalize_https_url_parts(url)
    return normalized


def origin_from_url(url: str) -> Origin:
    _, origin = _normalize_https_url_parts(url)
    return origin


def resolve_network_target(
    url: str,
    resolver: DNSResolver,
    *,
    previous: NetworkTarget | None = None,
    via_proxy: bool = False,
) -> NetworkTarget:
    """Resolve through an injected resolver and fail closed on unsafe/drifted DNS."""

    if not callable(resolver):
        raise TypeError("resolver must be callable")
    normalized, origin = _normalize_https_url_parts(url)
    if previous is not None:
        if not isinstance(previous, NetworkTarget):
            raise TypeError("previous must be NetworkTarget or None")
        if previous.normalized_url != normalized or previous.origin != origin:
            raise PolicyError("network_target_drift")
    try:
        raw_addresses = resolver(origin.host)
    except PolicyError:
        raise
    except Exception:
        raise PolicyError("dns_resolution_failed") from None
    addresses = _canonical_global_addresses(raw_addresses)
    target = NetworkTarget(normalized, origin, addresses, via_proxy)
    if previous is not None and previous.resolved_addresses != target.resolved_addresses:
        raise PolicyError("dns_rebinding_detected")
    return target


def resolve_redirect(
    current: NetworkTarget,
    location: str,
    resolver: DNSResolver,
    *,
    credential_scope: CredentialScope | None = None,
    via_proxy: bool = True,
) -> NetworkTarget:
    """Resolve one redirect and forbid sending credentials to another origin."""

    if not isinstance(current, NetworkTarget):
        raise TypeError("current must be NetworkTarget")
    _ascii_url_text(location)
    redirected_url = urljoin(current.normalized_url, location)
    target = resolve_network_target(redirected_url, resolver, via_proxy=via_proxy)
    if credential_scope is not None:
        if not isinstance(credential_scope, CredentialScope):
            raise TypeError("credential_scope must be CredentialScope or None")
        if target.origin != current.origin or not credential_scope.permits_origin(
            target.origin
        ):
            raise PolicyError("credential_redirect_forbidden")
    return target


@dataclass(frozen=True, slots=True)
class ResourceBudgetLimits:
    """Trusted preflight ceilings; Docker must independently enforce runtime limits."""

    max_cpus: float = 1.0
    max_memory_bytes: int = 256 * 1024 * 1024
    max_pids: int = 64
    max_tmpfs_bytes: int = 64 * 1024 * 1024
    max_duration_seconds: float = 60.0
    max_stdout_bytes: int = 256_000
    max_stderr_bytes: int = 256_000
    max_network_bytes: int = 0
    max_tool_calls: int = 1
    max_subagents: int = 0

    def __post_init__(self) -> None:
        _positive_finite(self.max_cpus, "invalid_resource_budget")
        _positive_finite(self.max_duration_seconds, "invalid_resource_budget")
        for value in (
            self.max_memory_bytes,
            self.max_pids,
            self.max_tmpfs_bytes,
            self.max_stdout_bytes,
            self.max_stderr_bytes,
            self.max_tool_calls,
        ):
            _positive_int(value, "invalid_resource_budget")
        for value in (self.max_network_bytes, self.max_subagents):
            _non_negative_int(value, "invalid_resource_budget")

    def preflight(self, request: "ResourceRequest") -> None:
        if not isinstance(request, ResourceRequest):
            raise TypeError("request must be ResourceRequest")
        checks = (
            request.cpus <= self.max_cpus,
            request.memory_bytes <= self.max_memory_bytes,
            request.pids <= self.max_pids,
            request.tmpfs_bytes <= self.max_tmpfs_bytes,
            request.duration_seconds <= self.max_duration_seconds,
            request.stdout_bytes <= self.max_stdout_bytes,
            request.stderr_bytes <= self.max_stderr_bytes,
            request.network_bytes <= self.max_network_bytes,
            request.tool_calls <= self.max_tool_calls,
            request.subagents <= self.max_subagents,
        )
        if not all(checks):
            raise PolicyError("resource_budget_exceeded")

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "max_cpus": float(self.max_cpus),
            "max_memory_bytes": self.max_memory_bytes,
            "max_pids": self.max_pids,
            "max_tmpfs_bytes": self.max_tmpfs_bytes,
            "max_duration_seconds": float(self.max_duration_seconds),
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "max_network_bytes": self.max_network_bytes,
            "max_tool_calls": self.max_tool_calls,
            "max_subagents": self.max_subagents,
        }


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    cpus: float = 1.0
    memory_bytes: int = 256 * 1024 * 1024
    pids: int = 64
    tmpfs_bytes: int = 64 * 1024 * 1024
    duration_seconds: float = 60.0
    stdout_bytes: int = 256_000
    stderr_bytes: int = 256_000
    network_bytes: int = 0
    tool_calls: int = 1
    subagents: int = 0

    def __post_init__(self) -> None:
        _positive_finite(self.cpus, "invalid_resource_request")
        _positive_finite(self.duration_seconds, "invalid_resource_request")
        for value in (
            self.memory_bytes,
            self.pids,
            self.tmpfs_bytes,
            self.stdout_bytes,
            self.stderr_bytes,
            self.tool_calls,
        ):
            _positive_int(value, "invalid_resource_request")
        for value in (self.network_bytes, self.subagents):
            _non_negative_int(value, "invalid_resource_request")

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "cpus": float(self.cpus),
            "memory_bytes": self.memory_bytes,
            "pids": self.pids,
            "tmpfs_bytes": self.tmpfs_bytes,
            "duration_seconds": float(self.duration_seconds),
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "network_bytes": self.network_bytes,
            "tool_calls": self.tool_calls,
            "subagents": self.subagents,
        }


def preflight_resource_request(
    request: ResourceRequest,
    limits: ResourceBudgetLimits,
) -> None:
    if not isinstance(limits, ResourceBudgetLimits):
        raise TypeError("limits must be ResourceBudgetLimits")
    limits.preflight(request)


@dataclass(frozen=True, slots=True, repr=False)
class ActionRequest:
    """Unresolved local action proposal; it grants no authority by itself."""

    kind: ActionKind
    tool_name: str
    arguments_json: str
    principal: Principal
    side_effect_class: SideEffectClass
    sandbox_profile_id: str
    resource_references: tuple[str, ...] = ()
    resolved_resources: tuple[ResolvedResource, ...] = ()
    network_url: str | None = None
    credential_scope: CredentialScope | None = None
    resource_request: ResourceRequest | None = None
    mcp_claimed_capabilities: tuple[str, ...] = ()
    mcp_server_id: str | None = None
    mcp_session_generation: int | None = None
    mcp_schema_hash: str | None = None
    # I6 §8.8: semantic binding identity enters the action digest so a
    # refreshed catalog cannot inherit an old approval.
    mcp_binding_digest: str | None = None
    mcp_server_identity_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActionKind):
            raise TypeError("kind must be ActionKind")
        object.__setattr__(self, "tool_name", _tool_name(self.tool_name))
        canonical_arguments(self.arguments_json)
        if not isinstance(self.principal, Principal):
            raise TypeError("principal must be Principal")
        if not isinstance(self.side_effect_class, SideEffectClass):
            raise TypeError("side_effect_class must be SideEffectClass")
        object.__setattr__(
            self,
            "sandbox_profile_id",
            _identifier(self.sandbox_profile_id, "invalid_sandbox_profile"),
        )
        references = _canonical_texts(
            self.resource_references,
            maximum_bytes=16_384,
            code="invalid_resource_reference",
        )
        resources = _canonical_resources(self.resolved_resources)
        if references and resources:
            raise PolicyError("ambiguous_resource_resolution")
        if self.network_url is not None:
            normalize_https_url(self.network_url)
        if self.credential_scope is not None and not isinstance(
            self.credential_scope, CredentialScope
        ):
            raise TypeError("credential_scope must be CredentialScope or None")
        if self.resource_request is not None and not isinstance(
            self.resource_request, ResourceRequest
        ):
            raise TypeError("resource_request must be ResourceRequest or None")
        claims = _canonical_identifiers(
            self.mcp_claimed_capabilities,
            pattern=_SCOPE,
            code="invalid_mcp_capability_claim",
        )
        if self.kind is not ActionKind.MCP_TOOL and claims:
            raise PolicyError("mcp_claim_on_non_mcp_action")
        if self.mcp_server_id is not None:
            server_id = _identifier(
                self.mcp_server_id, "invalid_credential_server"
            )
            if self.kind is not ActionKind.MCP_TOOL:
                raise PolicyError("mcp_server_on_non_mcp_action")
            object.__setattr__(self, "mcp_server_id", server_id)
        object.__setattr__(
            self,
            "mcp_session_generation",
            _mcp_session_generation(self.mcp_session_generation, self.kind),
        )
        object.__setattr__(
            self,
            "mcp_schema_hash",
            _mcp_schema_hash(self.mcp_schema_hash, self.kind),
        )
        object.__setattr__(
            self,
            "mcp_binding_digest",
            _mcp_binding_digest(self.mcp_binding_digest, self.kind),
        )
        object.__setattr__(
            self,
            "mcp_server_identity_digest",
            _mcp_identity_digest(self.mcp_server_identity_digest, self.kind),
        )
        object.__setattr__(self, "resource_references", references)
        object.__setattr__(self, "resolved_resources", resources)
        object.__setattr__(self, "mcp_claimed_capabilities", claims)

    def __repr__(self) -> str:
        return (
            f"ActionRequest(kind={self.kind.value!r}, tool_name={self.tool_name!r}, "
            f"argument_bytes={len(self.arguments_json.encode('utf-8', 'strict'))}, "
            f"resource_count={len(self.resource_references) + len(self.resolved_resources)}, "
            f"network_present={self.network_url is not None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedAction:
    """Immutable, fully canonical action evaluated and bound by an approval."""

    kind: ActionKind
    tool_name: str
    canonical_arguments_json: str
    principal: Principal
    side_effect_class: SideEffectClass
    sandbox_profile_id: str
    policy_version: str
    resources: tuple[ResolvedResource, ...] = ()
    network_target: NetworkTarget | None = None
    credential_scope: CredentialScope | None = None
    resource_request: ResourceRequest | None = None
    mcp_claimed_capabilities: tuple[str, ...] = ()
    mcp_server_id: str | None = None
    mcp_session_generation: int | None = None
    mcp_schema_hash: str | None = None
    mcp_binding_digest: str | None = None
    mcp_server_identity_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActionKind):
            raise TypeError("kind must be ActionKind")
        object.__setattr__(self, "tool_name", _tool_name(self.tool_name))
        canonical = canonical_arguments(self.canonical_arguments_json)
        if canonical != self.canonical_arguments_json:
            raise PolicyError("noncanonical_tool_arguments")
        if not isinstance(self.principal, Principal):
            raise TypeError("principal must be Principal")
        if not isinstance(self.side_effect_class, SideEffectClass):
            raise TypeError("side_effect_class must be SideEffectClass")
        object.__setattr__(
            self,
            "sandbox_profile_id",
            _identifier(self.sandbox_profile_id, "invalid_sandbox_profile"),
        )
        object.__setattr__(
            self,
            "policy_version",
            _identifier(self.policy_version, "invalid_policy_version"),
        )
        object.__setattr__(self, "resources", _canonical_resources(self.resources))
        if self.network_target is not None and not isinstance(
            self.network_target, NetworkTarget
        ):
            raise TypeError("network_target must be NetworkTarget or None")
        if self.credential_scope is not None and not isinstance(
            self.credential_scope, CredentialScope
        ):
            raise TypeError("credential_scope must be CredentialScope or None")
        if self.resource_request is not None and not isinstance(
            self.resource_request, ResourceRequest
        ):
            raise TypeError("resource_request must be ResourceRequest or None")
        claims = _canonical_identifiers(
            self.mcp_claimed_capabilities,
            pattern=_SCOPE,
            code="invalid_mcp_capability_claim",
        )
        if self.kind is not ActionKind.MCP_TOOL and claims:
            raise PolicyError("mcp_claim_on_non_mcp_action")
        if self.mcp_server_id is not None:
            server_id = _identifier(
                self.mcp_server_id, "invalid_credential_server"
            )
            if self.kind is not ActionKind.MCP_TOOL:
                raise PolicyError("mcp_server_on_non_mcp_action")
            object.__setattr__(self, "mcp_server_id", server_id)
        object.__setattr__(
            self,
            "mcp_session_generation",
            _mcp_session_generation(self.mcp_session_generation, self.kind),
        )
        object.__setattr__(
            self,
            "mcp_schema_hash",
            _mcp_schema_hash(self.mcp_schema_hash, self.kind),
        )
        object.__setattr__(
            self,
            "mcp_binding_digest",
            _mcp_binding_digest(self.mcp_binding_digest, self.kind),
        )
        object.__setattr__(
            self,
            "mcp_server_identity_digest",
            _mcp_identity_digest(self.mcp_server_identity_digest, self.kind),
        )
        object.__setattr__(self, "mcp_claimed_capabilities", claims)

    @property
    def action_digest(self) -> str:
        return compute_action_digest(self)

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "kind": self.kind.value,
            "tool_name": self.tool_name,
            "arguments": _parse_arguments(self.canonical_arguments_json),
            "resources": [resource.to_document() for resource in self.resources],
            "side_effect_class": self.side_effect_class.value,
            "sandbox_profile_id": self.sandbox_profile_id,
            "network_target": (
                None if self.network_target is None else self.network_target.to_document()
            ),
            "credential_scope": (
                None if self.credential_scope is None else self.credential_scope.to_document()
            ),
            "resource_request": (
                None if self.resource_request is None else self.resource_request.to_document()
            ),
            "policy_version": self.policy_version,
            "principal": self.principal.to_document(),
            "mcp_server_id": self.mcp_server_id,
            "mcp_session_generation": self.mcp_session_generation,
            "mcp_schema_hash": self.mcp_schema_hash,
            "mcp_binding_digest": self.mcp_binding_digest,
            "mcp_server_identity_digest": self.mcp_server_identity_digest,
            # Untrusted MCP declarations are bound for tamper evidence only.
            "mcp_claimed_capabilities": list(self.mcp_claimed_capabilities),
        }

    def __repr__(self) -> str:
        return (
            f"ResolvedAction(kind={self.kind.value!r}, tool_name={self.tool_name!r}, "
            f"action_digest={self.action_digest!r}, resource_count={len(self.resources)}, "
            f"network_present={self.network_target is not None})"
        )


def action_request_from_tool_call(
    *,
    kind: ActionKind,
    tool_name: str,
    arguments_json: str,
    principal: Principal,
    side_effect_class: SideEffectClass,
    sandbox_profile_id: str,
    resource_references: Sequence[str] = (),
    network_url: str | None = None,
    credential_scope: CredentialScope | None = None,
    resource_request: ResourceRequest | None = None,
    mcp_claimed_capabilities: Sequence[str] = (),
    mcp_server_id: str | None = None,
    mcp_session_generation: int | None = None,
    mcp_schema_hash: str | None = None,
    mcp_binding_digest: str | None = None,
    mcp_server_identity_digest: str | None = None,
) -> ActionRequest:
    """Adapter-friendly constructor without importing Provider/Registry types."""

    return ActionRequest(
        kind=kind,
        tool_name=tool_name,
        arguments_json=arguments_json,
        principal=principal,
        side_effect_class=side_effect_class,
        sandbox_profile_id=sandbox_profile_id,
        resource_references=tuple(resource_references),
        network_url=network_url,
        credential_scope=credential_scope,
        resource_request=resource_request,
        mcp_claimed_capabilities=tuple(mcp_claimed_capabilities),
        mcp_server_id=mcp_server_id,
        mcp_session_generation=mcp_session_generation,
        mcp_schema_hash=mcp_schema_hash,
        mcp_binding_digest=mcp_binding_digest,
        mcp_server_identity_digest=mcp_server_identity_digest,
    )


def resolve_action_request(
    request: ActionRequest,
    *,
    policy_version: str,
    resource_resolver: ResourceResolver | None = None,
    dns_resolver: DNSResolver | None = None,
    previous_network_target: NetworkTarget | None = None,
    via_proxy: bool = False,
) -> ResolvedAction:
    """Resolve an action with injected pure boundaries and compute canonical identity."""

    if not isinstance(request, ActionRequest):
        raise TypeError("request must be ActionRequest")
    resources = request.resolved_resources
    if request.resource_references:
        if not callable(resource_resolver):
            raise PolicyError("resource_resolver_required")
        resolved: list[ResolvedResource] = []
        for reference in request.resource_references:
            try:
                resource = resource_resolver(reference)
            except PolicyError:
                raise
            except Exception:
                raise PolicyError("resource_resolution_failed") from None
            if not isinstance(resource, ResolvedResource):
                raise PolicyError("invalid_resolved_resource")
            if resource.requested != reference:
                raise PolicyError("resource_resolution_mismatch")
            resolved.append(resource)
        resources = _canonical_resources(tuple(resolved))

    network_target: NetworkTarget | None = None
    if request.network_url is not None:
        if dns_resolver is None:
            raise PolicyError("dns_resolver_required")
        network_target = resolve_network_target(
            request.network_url,
            dns_resolver,
            previous=previous_network_target,
            via_proxy=via_proxy,
        )
    elif previous_network_target is not None:
        raise PolicyError("network_target_drift")

    return ResolvedAction(
        kind=request.kind,
        tool_name=request.tool_name,
        canonical_arguments_json=canonical_arguments(request.arguments_json),
        principal=request.principal,
        side_effect_class=request.side_effect_class,
        sandbox_profile_id=request.sandbox_profile_id,
        policy_version=policy_version,
        resources=resources,
        network_target=network_target,
        credential_scope=request.credential_scope,
        resource_request=request.resource_request,
        mcp_claimed_capabilities=request.mcp_claimed_capabilities,
        mcp_server_id=request.mcp_server_id,
        mcp_session_generation=request.mcp_session_generation,
        mcp_schema_hash=request.mcp_schema_hash,
        mcp_binding_digest=request.mcp_binding_digest,
        mcp_server_identity_digest=request.mcp_server_identity_digest,
    )


def canonical_arguments(value: str | Mapping[str, Any]) -> str:
    """Canonicalize a strict JSON object with duplicate/NaN/surrogate rejection."""

    document = _parse_arguments(value) if isinstance(value, str) else value
    if not isinstance(document, Mapping):
        raise PolicyError("invalid_tool_arguments")
    normalized, nodes = _normalize_json_value(document, depth=0, nodes=0)
    if nodes > 100_000 or not isinstance(normalized, dict):
        raise PolicyError("invalid_tool_arguments")
    try:
        result = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded = result.encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError):
        raise PolicyError("invalid_tool_arguments") from None
    if len(encoded) > MAX_ARGUMENT_JSON_BYTES:
        raise PolicyError("tool_arguments_too_large")
    return result


def compute_action_digest(action: ResolvedAction) -> str:
    if not isinstance(action, ResolvedAction):
        raise TypeError("action must be ResolvedAction")
    return _sha256_document(action.to_document())


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """A local administrator rule. Empty selectors mean wildcard."""

    rule_id: str
    decision: Decision
    action_kinds: tuple[ActionKind, ...] = ()
    tool_names: tuple[str, ...] = ()
    principal_ids: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()
    side_effect_classes: tuple[SideEffectClass, ...] = ()
    sandbox_profile_ids: tuple[str, ...] = ()
    allowed_origins: tuple[Origin, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "rule_id", _identifier(self.rule_id, "invalid_policy_rule")
        )
        if not isinstance(self.decision, Decision):
            raise TypeError("decision must be Decision")
        if not isinstance(self.action_kinds, tuple) or any(
            not isinstance(value, ActionKind) for value in self.action_kinds
        ):
            raise TypeError("action_kinds contains an invalid item")
        action_kinds = tuple(sorted(set(self.action_kinds), key=lambda item: item.value))
        tool_names = tuple(sorted({_tool_name(value) for value in self.tool_names}))
        principals = _canonical_identifiers(
            self.principal_ids,
            pattern=_IDENTIFIER,
            code="invalid_principal",
        )
        scopes = _canonical_identifiers(
            self.required_scopes,
            pattern=_SCOPE,
            code="invalid_principal_scope",
        )
        if not isinstance(self.side_effect_classes, tuple) or any(
            not isinstance(value, SideEffectClass)
            for value in self.side_effect_classes
        ):
            raise TypeError("side_effect_classes contains an invalid item")
        side_effects = tuple(
            sorted(set(self.side_effect_classes), key=lambda item: item.value)
        )
        profiles = _canonical_identifiers(
            self.sandbox_profile_ids,
            pattern=_IDENTIFIER,
            code="invalid_sandbox_profile",
        )
        if not isinstance(self.allowed_origins, tuple) or any(
            not isinstance(origin, Origin) for origin in self.allowed_origins
        ):
            raise TypeError("allowed_origins contains an invalid item")
        origins = tuple(
            sorted(
                set(self.allowed_origins),
                key=lambda item: (item.scheme, item.host, item.port),
            )
        )
        object.__setattr__(self, "action_kinds", action_kinds)
        object.__setattr__(self, "tool_names", tool_names)
        object.__setattr__(self, "principal_ids", principals)
        object.__setattr__(self, "required_scopes", scopes)
        object.__setattr__(self, "side_effect_classes", side_effects)
        object.__setattr__(self, "sandbox_profile_ids", profiles)
        object.__setattr__(self, "allowed_origins", origins)

    def matches(self, action: ResolvedAction) -> bool:
        if not isinstance(action, ResolvedAction):
            raise TypeError("action must be ResolvedAction")
        selectors = (
            not self.action_kinds or action.kind in self.action_kinds,
            not self.tool_names or action.tool_name in self.tool_names,
            not self.principal_ids
            or action.principal.principal_id in self.principal_ids,
            not self.side_effect_classes
            or action.side_effect_class in self.side_effect_classes,
            not self.sandbox_profile_ids
            or action.sandbox_profile_id in self.sandbox_profile_ids,
            not self.allowed_origins
            or (
                action.network_target is not None
                and action.network_target.origin in self.allowed_origins
            ),
            set(self.required_scopes).issubset(action.principal.scopes),
        )
        return all(selectors)


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    decision: Decision
    code: str
    policy_version: str
    action_digest: str
    matched_rule_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.decision, Decision):
            raise TypeError("decision must be Decision")
        if not isinstance(self.code, str) or not _STABLE_CODE.fullmatch(self.code):
            raise PolicyError("invalid_verdict")
        object.__setattr__(
            self,
            "policy_version",
            _identifier(self.policy_version, "invalid_policy_version"),
        )
        if not isinstance(self.action_digest, str) or not _SHA256.fullmatch(
            self.action_digest
        ):
            raise PolicyError("invalid_action_digest")
        rules = _canonical_identifiers(
            self.matched_rule_ids,
            pattern=_IDENTIFIER,
            code="invalid_policy_rule",
        )
        object.__setattr__(self, "matched_rule_ids", rules)


Verdict = PolicyVerdict


@dataclass(frozen=True, slots=True)
class PolicyEngine:
    """Deterministic local authority: DENY > ASK > ALLOW, otherwise DENY."""

    policy_version: str
    rules: tuple[PolicyRule, ...] = ()
    network_enabled: bool = False
    proxy_required: bool = True
    allowed_origins: tuple[Origin, ...] = ()
    resource_limits: ResourceBudgetLimits | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_version",
            _identifier(self.policy_version, "invalid_policy_version"),
        )
        if not isinstance(self.rules, tuple) or any(
            not isinstance(rule, PolicyRule) for rule in self.rules
        ):
            raise TypeError("rules contains an invalid item")
        rules = tuple(sorted(self.rules, key=lambda rule: rule.rule_id))
        if len({rule.rule_id for rule in rules}) != len(rules):
            raise PolicyError("duplicate_policy_rule")
        if not isinstance(self.network_enabled, bool):
            raise TypeError("network_enabled must be bool")
        if not isinstance(self.proxy_required, bool):
            raise TypeError("proxy_required must be bool")
        if not isinstance(self.allowed_origins, tuple) or any(
            not isinstance(origin, Origin) for origin in self.allowed_origins
        ):
            raise TypeError("allowed_origins contains an invalid item")
        origins = tuple(
            sorted(
                set(self.allowed_origins),
                key=lambda item: (item.scheme, item.host, item.port),
            )
        )
        if not self.proxy_required:
            raise PolicyError("network_proxy_required")
        if self.network_enabled and not origins:
            raise PolicyError("network_allowlist_required")
        if self.resource_limits is not None and not isinstance(
            self.resource_limits, ResourceBudgetLimits
        ):
            raise TypeError("resource_limits must be ResourceBudgetLimits or None")
        object.__setattr__(self, "rules", rules)
        object.__setattr__(self, "allowed_origins", origins)

    def evaluate(self, action: ResolvedAction) -> PolicyVerdict:
        if not isinstance(action, ResolvedAction):
            raise TypeError("action must be ResolvedAction")
        digest = action.action_digest
        if action.policy_version != self.policy_version:
            return self._verdict(Decision.DENY, "stale_policy_version", digest)
        if action.kind is ActionKind.MCP_TOOL:
            if (
                action.mcp_session_generation is None
                or action.mcp_schema_hash is None
            ):
                return self._verdict(Decision.DENY, "mcp_binding_required", digest)

        if self.resource_limits is not None:
            if action.resource_request is None:
                return self._verdict(Decision.DENY, "resource_request_required", digest)
            try:
                self.resource_limits.preflight(action.resource_request)
            except PolicyError as error:
                return self._verdict(Decision.DENY, error.code, digest)

        target = action.network_target
        if target is not None:
            if not self.network_enabled:
                return self._verdict(Decision.DENY, "network_disabled", digest)
            if not target.via_proxy:
                return self._verdict(Decision.DENY, "proxy_required", digest)
            if target.origin not in self.allowed_origins:
                return self._verdict(Decision.DENY, "network_origin_denied", digest)
            if (
                action.credential_scope is not None
                and action.kind is not ActionKind.MCP_TOOL
                and not action.credential_scope.permits_origin(target.origin)
            ):
                return self._verdict(Decision.DENY, "credential_scope_denied", digest)
        if action.credential_scope is not None:
            if action.kind is ActionKind.MCP_TOOL:
                if action.mcp_server_id is None:
                    return self._verdict(
                        Decision.DENY, "credential_server_missing", digest
                    )
                if not action.credential_scope.permits_server(action.mcp_server_id):
                    return self._verdict(
                        Decision.DENY, "credential_scope_denied", digest
                    )
            elif target is None:
                return self._verdict(
                    Decision.DENY, "credential_target_missing", digest
                )

        matching = tuple(rule for rule in self.rules if rule.matches(action))
        matching_ids = tuple(rule.rule_id for rule in matching)
        if any(rule.decision is Decision.DENY for rule in matching):
            return self._verdict(Decision.DENY, "rule_denied", digest, matching_ids)
        if any(rule.decision is Decision.ASK for rule in matching):
            return self._verdict(
                Decision.ASK, "approval_required", digest, matching_ids
            )
        if any(rule.decision is Decision.ALLOW for rule in matching):
            return self._verdict(Decision.ALLOW, "allowed", digest, matching_ids)
        return self._verdict(Decision.DENY, "denied_by_default", digest)

    def _verdict(
        self,
        decision: Decision,
        code: str,
        digest: str,
        rules: tuple[str, ...] = (),
    ) -> PolicyVerdict:
        return PolicyVerdict(
            decision=decision,
            code=code,
            policy_version=self.policy_version,
            action_digest=digest,
            matched_rule_ids=rules,
        )


def _parse_arguments(value: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise TypeError("arguments_json must be str")
    try:
        if len(value.encode("utf-8", "strict")) > MAX_ARGUMENT_JSON_BYTES:
            raise PolicyError("tool_arguments_too_large")
    except UnicodeError:
        raise PolicyError("invalid_tool_arguments") from None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise PolicyError("duplicate_tool_argument_key")
            result[key] = item
        return result

    def reject_constant(_: str) -> Any:
        raise PolicyError("invalid_tool_arguments")

    try:
        document = json.loads(
            value,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except PolicyError:
        raise
    except (json.JSONDecodeError, UnicodeError):
        raise PolicyError("invalid_tool_arguments") from None
    if not isinstance(document, dict):
        raise PolicyError("invalid_tool_arguments")
    return document


def _normalize_json_value(
    value: Any,
    *,
    depth: int,
    nodes: int,
) -> tuple[JsonValue, int]:
    nodes += 1
    if depth > 64 or nodes > 100_000:
        raise PolicyError("invalid_tool_arguments")
    if value is None or isinstance(value, bool):
        return value, nodes
    if isinstance(value, int):
        if abs(value) > 2**63 - 1:
            raise PolicyError("invalid_tool_arguments")
        return value, nodes
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PolicyError("invalid_tool_arguments")
        if value == 0:
            return 0, nodes
        if value.is_integer() and abs(value) <= 2**53:
            return int(value), nodes
        return value, nodes
    if isinstance(value, str):
        _bounded_text(
            value,
            maximum_bytes=MAX_ARGUMENT_JSON_BYTES,
            code="invalid_tool_arguments",
            allow_empty=True,
        )
        return value, nodes
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key in result:
                raise PolicyError("invalid_tool_arguments")
            _bounded_text(
                key,
                maximum_bytes=16_384,
                code="invalid_tool_arguments",
                allow_empty=True,
            )
            normalized, nodes = _normalize_json_value(
                item, depth=depth + 1, nodes=nodes
            )
            result[key] = normalized
        return result, nodes
    if isinstance(value, (list, tuple)):
        result_list: list[JsonValue] = []
        for item in value:
            normalized, nodes = _normalize_json_value(
                item, depth=depth + 1, nodes=nodes
            )
            result_list.append(normalized)
        return result_list, nodes
    raise PolicyError("invalid_tool_arguments")


def _normalize_https_url_parts(url: str) -> tuple[str, Origin]:
    _ascii_url_text(url)
    try:
        split = urlsplit(url)
    except ValueError:
        raise PolicyError("invalid_network_url") from None
    if split.scheme.lower() != "https":
        raise PolicyError("https_required")
    if not split.netloc or split.username is not None or split.password is not None:
        raise PolicyError("network_userinfo_forbidden")
    if split.fragment:
        raise PolicyError("network_fragment_forbidden")
    try:
        port = 443 if split.port is None else split.port
        raw_host = split.hostname
    except ValueError:
        raise PolicyError("invalid_network_port") from None
    if raw_host is None:
        raise PolicyError("invalid_network_host")
    host = _normalize_host(raw_host)
    origin = Origin("https", host, port)
    path = _normalize_url_component(split.path or "/", is_path=True)
    query = _normalize_url_component(split.query, is_path=False)
    authority = origin.authority
    normalized = urlunsplit(SplitResult("https", authority, path, query, ""))
    return normalized, origin


def _normalize_host(host: str) -> str:
    if not isinstance(host, str) or not host or not host.isascii():
        raise PolicyError("invalid_network_host")
    if any(character.isspace() or ord(character) < 0x20 for character in host):
        raise PolicyError("invalid_network_host")
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host or "%" in host:
        raise PolicyError("invalid_network_host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ":" in host or re.fullmatch(r"[0-9.]+", host):
            raise PolicyError("invalid_network_host") from None
        if len(host) > 253:
            raise PolicyError("invalid_network_host")
        labels = host.split(".")
        if any(not _DNS_LABEL.fullmatch(label) for label in labels):
            raise PolicyError("invalid_network_host")
        # The Python standard library codec is IDNA2003.  D9 has no approved
        # IDNA2008 dependency, so both Unicode labels and their ASCII A-label
        # spelling stay fail-closed instead of being misrepresented as verified.
        if any(label.startswith("xn--") for label in labels):
            raise PolicyError("network_idna2008_required")
        return host
    return address.compressed.lower()


def _normalize_url_component(value: str, *, is_path: bool) -> str:
    if not isinstance(value, str) or not value.isascii() or "\\" in value:
        raise PolicyError("invalid_network_url")
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if ord(character) < 0x20 or ord(character) == 0x7F or character.isspace():
            raise PolicyError("invalid_network_url")
        if character != "%":
            result.append(character)
            index += 1
            continue
        escape = value[index : index + 3]
        if len(escape) != 3 or not _PERCENT_ESCAPE.fullmatch(escape):
            raise PolicyError("invalid_network_url")
        decoded = chr(int(escape[1:], 16))
        result.append(decoded if decoded in _UNRESERVED else escape.upper())
        index += 3
    normalized = "".join(result)
    if is_path:
        trailing_slash = normalized.endswith("/")
        normalized = posixpath.normpath(normalized)
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        if trailing_slash and normalized != "/":
            normalized += "/"
    return normalized


def _ascii_url_text(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16_384
        or not value.isascii()
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or "\\" in value
    ):
        raise PolicyError("invalid_network_url")


def _canonical_global_addresses(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise PolicyError("invalid_dns_response")
    addresses: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.isascii():
            raise PolicyError("invalid_dns_response")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise PolicyError("invalid_dns_response") from None
        mapped = getattr(address, "ipv4_mapped", None)
        checked = mapped if mapped is not None else address
        if (
            not checked.is_global
            or checked.is_private
            or checked.is_loopback
            or checked.is_link_local
            or checked.is_multicast
            or checked.is_reserved
            or checked.is_unspecified
        ):
            raise PolicyError("non_global_network_address")
        addresses.add(address.compressed.lower())
    if not addresses:
        raise PolicyError("empty_dns_response")
    return tuple(sorted(addresses, key=lambda item: (ipaddress.ip_address(item).version, item)))


def _canonical_resources(
    resources: Sequence[ResolvedResource],
) -> tuple[ResolvedResource, ...]:
    if not isinstance(resources, tuple):
        raise TypeError("resources must be tuple")
    if any(not isinstance(resource, ResolvedResource) for resource in resources):
        raise TypeError("resources contains an invalid item")
    ordered = tuple(
        sorted(
            resources,
            key=lambda resource: (
                resource.resource_kind,
                resource.requested,
                resource.resolved,
                resource.identity,
                resource.metadata,
            ),
        )
    )
    documents = [_canonical_json(resource.to_document()) for resource in ordered]
    if len(set(documents)) != len(documents):
        raise PolicyError("duplicate_resolved_resource")
    return ordered


def _canonical_metadata(
    metadata: Sequence[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(metadata, tuple):
        raise TypeError("metadata must be tuple")
    normalized: list[tuple[str, str]] = []
    for item in metadata:
        if not isinstance(item, tuple) or len(item) != 2:
            raise PolicyError("invalid_resource_metadata")
        name, value = item
        name = _identifier(name, "invalid_resource_metadata")
        _bounded_text(value, 4096, "invalid_resource_metadata", allow_empty=True)
        normalized.append((name, value))
    normalized.sort()
    if len({name for name, _ in normalized}) != len(normalized):
        raise PolicyError("duplicate_resource_metadata")
    return tuple(normalized)


def _canonical_identifiers(
    values: Sequence[str],
    *,
    pattern: re.Pattern[str],
    code: str,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("identifier collection must be a sequence")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise PolicyError(code)
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise PolicyError(code)
    return tuple(sorted(normalized))


def _canonical_texts(
    values: Sequence[str],
    *,
    maximum_bytes: int,
    code: str,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("text collection must be a sequence")
    normalized: list[str] = []
    for value in values:
        _bounded_text(value, maximum_bytes, code)
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise PolicyError(code)
    return tuple(sorted(normalized))


def _tool_name(value: str) -> str:
    if not isinstance(value, str) or not _TOOL_NAME.fullmatch(value):
        raise PolicyError("invalid_tool_name")
    return value


def _identifier(value: str, code: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise PolicyError(code)
    return value


def _mcp_session_generation(value: Any, kind: ActionKind) -> int | None:
    if value is None:
        return None
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise PolicyError("invalid_mcp_binding")
    if kind is not ActionKind.MCP_TOOL:
        raise PolicyError("mcp_binding_on_non_mcp_action")
    return value


def _mcp_binding_digest(value: Any, kind: ActionKind) -> str | None:
    if value is None:
        return None
    if kind is not ActionKind.MCP_TOOL:
        raise PolicyError("mcp_binding_on_non_mcp_action")
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PolicyError("invalid_mcp_binding_digest")
    return value


def _mcp_identity_digest(value: Any, kind: ActionKind) -> str | None:
    if value is None:
        return None
    if kind is not ActionKind.MCP_TOOL:
        raise PolicyError("mcp_binding_on_non_mcp_action")
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PolicyError("invalid_mcp_identity_digest")
    return value


def _mcp_schema_hash(value: Any, kind: ActionKind) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PolicyError("invalid_mcp_binding")
    if kind is not ActionKind.MCP_TOOL:
        raise PolicyError("mcp_binding_on_non_mcp_action")
    return value


def _bounded_text(
    value: Any,
    maximum_bytes: int,
    code: str,
    *,
    allow_empty: bool = False,
) -> None:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise PolicyError(code)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        raise PolicyError(code) from None
    if len(encoded) > maximum_bytes or "\x00" in value:
        raise PolicyError(code)


def _positive_finite(value: Any, code: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise PolicyError(code)


def _positive_int(value: Any, code: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PolicyError(code)


def _non_negative_int(value: Any, code: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PolicyError(code)


def _canonical_json(document: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError):
        raise PolicyError("invalid_policy_document") from None


def _sha256_document(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(document).encode("utf-8", "strict")).hexdigest()


__all__ = [
    "POLICY_SCHEMA_VERSION",
    "MAX_ARGUMENT_JSON_BYTES",
    "PolicyError",
    "Decision",
    "ActionKind",
    "SideEffectClass",
    "Principal",
    "narrow_child_principal",
    "ResolvedResource",
    "Origin",
    "NetworkTarget",
    "CredentialScope",
    "DNSResolver",
    "ResourceResolver",
    "normalize_https_url",
    "origin_from_url",
    "resolve_network_target",
    "resolve_redirect",
    "ResourceBudgetLimits",
    "ResourceRequest",
    "preflight_resource_request",
    "ActionRequest",
    "ResolvedAction",
    "action_request_from_tool_call",
    "resolve_action_request",
    "canonical_arguments",
    "compute_action_digest",
    "PolicyRule",
    "PolicyVerdict",
    "Verdict",
    "PolicyEngine",
]
