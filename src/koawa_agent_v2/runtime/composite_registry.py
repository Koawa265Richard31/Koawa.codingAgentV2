"""Composite prepared tool registry for built-in + MCP delegates.

Every delegate already implements the D3 prepared-invocation contract.  This
facade keeps one prepared token per delegate and forwards the D9 authority so
LedgerExecutor sees exactly one sealed ``ToolExecutor``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..execution.loop import ToolExecutionContext, ToolExecutionResult
from ..model.protocol import ToolCallItem, ToolDefinition
from ..tools.errors import ToolConfigurationError


@dataclass(frozen=True, slots=True)
class _PreparedCall:
    delegate: Any
    prepared: Any

    @property
    def early_result(self):
        return getattr(self.prepared, "early_result", None)

    @property
    def binding_digest(self) -> str | None:
        return getattr(self.prepared, "binding_digest", None)

    @property
    def semantic_binding(self):
        return getattr(self.prepared, "semantic_binding", None)

    @property
    def physical_fence(self):
        return getattr(self.prepared, "physical_fence", None)


@dataclass(frozen=True, slots=True)
class ToolCatalogSnapshot:
    """I6 §8.8 frozen catalog consumed atomically by one model round.

    Contains definitions, recovery profiles, action resolvers, semantic
    bindings and the prepared delegate handles in ONE immutable object; a
    refresh publishes the NEXT snapshot, never a partial registry swap.
    """

    catalog_epoch_id: Any
    definitions: tuple[ToolDefinition, ...]
    profiles: Mapping[str, Any]
    resolvers: Mapping[str, Any]
    semantic_bindings: Mapping[str, Any]
    delegates: tuple[Any, ...]

    def tool_names(self) -> frozenset[str]:
        return frozenset(item.name for item in self.definitions)

    def prepare(self, call: ToolCallItem) -> _PreparedCall:
        if not isinstance(call, ToolCallItem):
            raise TypeError("call must be ToolCallItem")
        for delegate in self.delegates:
            if call.name in {item.name for item in delegate.definitions()}:
                inner = delegate.prepare(call)
                return _PreparedCall(delegate, inner)
        raise ToolConfigurationError("unknown_tool")


class CompositeToolRegistry:
    """A read-only composition of already-built prepared registries."""

    def __init__(self, delegates: tuple[Any, ...]) -> None:
        if not delegates:
            raise ToolConfigurationError("empty_composite_registry")
        for delegate in delegates:
            for name in ("prepare", "invoke_prepared", "discard_prepared", "bind_policy_authority"):
                if not callable(getattr(delegate, name, None)):
                    raise ToolConfigurationError("invalid_composite_delegate")
        self._delegates = delegates
        definitions: list[ToolDefinition] = []
        names: set[str] = set()
        for delegate in delegates:
            for definition in delegate.definitions():
                if definition.name in names:
                    raise ToolConfigurationError("duplicate_tool_name")
                names.add(definition.name)
                definitions.append(definition)
        self._definitions = tuple(sorted(definitions, key=lambda item: item.name))

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    def bind_policy_authority(self, authority: object) -> None:
        for delegate in self._delegates:
            delegate.bind_policy_authority(authority)

    def prepare(self, call: ToolCallItem) -> _PreparedCall:
        if not isinstance(call, ToolCallItem):
            raise TypeError("call must be ToolCallItem")
        for delegate in self._delegates:
            if call.name in {item.name for item in delegate.definitions()}:
                return _PreparedCall(delegate, delegate.prepare(call))
        raise ToolConfigurationError("unknown_tool")

    def invoke_prepared(
        self,
        prepared: _PreparedCall,
        *,
        context: ToolExecutionContext,
        authority: object,
    ) -> ToolExecutionResult:
        if not isinstance(prepared, _PreparedCall):
            raise TypeError("prepared must be _PreparedCall")
        return prepared.delegate.invoke_prepared(
            prepared.prepared,
            context=context,
            authority=authority,
        )

    def discard_prepared(
        self,
        prepared: _PreparedCall,
        *,
        authority: object,
    ) -> bool:
        if not isinstance(prepared, _PreparedCall):
            raise TypeError("prepared must be _PreparedCall")
        return prepared.delegate.discard_prepared(
            prepared.prepared,
            authority=authority,
        )

    def execute(
        self,
        call: ToolCallItem,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        prepared = self.prepare(call)
        early = getattr(prepared.prepared, "early_result", None)
        if early is not None:
            return early
        raise ToolConfigurationError("policy_authorization_required")

    def binding_digest(self, tool_name: str) -> str | None:
        for delegate in self._delegates:
            method = getattr(delegate, "binding_digest", None)
            if callable(method):
                digest = method(tool_name)
                if digest is not None:
                    return digest
        return None

    def assert_complete(self, run_id: object) -> None:
        """D5 CompletionGate facade (§8.9 E-stage gap).

        Delegates the evidence gate to the builtin CodingToolRegistry when
        present; a composite without any gate fails closed so AgentLoop can
        still be constructed but cannot fabricate an empty completion.
        """
        for delegate in self._delegates:
            method = getattr(delegate, "assert_complete", None)
            if callable(method):
                method(run_id)
                return
        raise ToolConfigurationError("completion_gate_unavailable")

    def current_snapshot(
        self,
        *,
        profiles: Mapping[str, object] | None = None,
        resolvers: Mapping[str, object] | None = None,
    ) -> ToolCatalogSnapshot:
        """One frozen snapshot over CURRENT delegate maps (I6 §8.8).

        profiles/resolvers are supplied by the assembly that owns policy;
        when omitted they are empty so the snapshot still carries the
        definitions and semantic bindings for the model round.
        """
        semantic: dict[str, object] = {}
        for delegate in self._delegates:
            method = getattr(delegate, "semantic_bindings", None)
            if callable(method):
                semantic.update(method())
        epoch = None
        for delegate in self._delegates:
            method = getattr(delegate, "catalog_epoch_id", None)
            if callable(method):
                epoch = method() or epoch
            elif getattr(delegate, "catalog_epoch_id", None) is not None:
                epoch = delegate.catalog_epoch_id
        return ToolCatalogSnapshot(
            catalog_epoch_id=epoch,
            definitions=self._definitions,
            profiles=MappingProxyType(dict(profiles or {})),
            resolvers=MappingProxyType(dict(resolvers or {})),
            semantic_bindings=MappingProxyType(semantic),
            delegates=self._delegates,
        )
