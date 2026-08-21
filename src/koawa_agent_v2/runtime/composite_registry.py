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
