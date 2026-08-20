"""D3 的确定性 Tool Registry 与唯一 handler 分发入口。"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any, Generic, Protocol, TypeVar

from ..execution.loop import ToolExecutionContext, ToolExecutionResult
from ..model.protocol import ToolCallItem, ToolDefinition
from .errors import (
    ToolArgumentError,
    ToolConfigurationError,
    ToolRegistryError,
    argument_error_result,
    tool_error_result,
)
from .schema import ToolSpec


ArgumentsT = TypeVar("ArgumentsT")


class ToolHandler(Protocol[ArgumentsT]):
    """Registry 在 schema gate 后调用的 typed handler。"""

    def __call__(
        self,
        arguments: ArgumentsT,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult: ...


@dataclass(frozen=True, slots=True)
class _Entry(Generic[ArgumentsT]):
    spec: ToolSpec[ArgumentsT]
    handler: ToolHandler[ArgumentsT]


@dataclass(frozen=True, slots=True, repr=False)
class PreparedToolInvocation:
    """Schema-gated invocation token whose repr never exposes arguments.

    Instances are bound to the issuing registry, its exact registered entry and
    the original immutable call object.  The private issuance token lets the
    registry reject look-alike instances before a handler can be reached.
    """

    _registry: "ToolRegistry"
    _entry: _Entry[Any] | None
    _call: ToolCallItem
    _arguments: Any | None
    _early_result: ToolExecutionResult | None
    _issuance_token: object

    @property
    def call(self) -> ToolCallItem:
        return self._call

    @property
    def spec(self) -> ToolSpec[Any] | None:
        entry = self._entry
        return None if entry is None else entry.spec

    @property
    def arguments(self) -> Any | None:
        return self._arguments

    @property
    def early_result(self) -> ToolExecutionResult | None:
        return self._early_result

    def __repr__(self) -> str:
        return "PreparedToolInvocation(<redacted>)"


_UNBOUND_POLICY_AUTHORITY = object()


class ToolRegistry:
    """注册期可变、首次模型快照后不可变的本地工具目录。"""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry[Any]] = {}
        self._definitions: tuple[ToolDefinition, ...] | None = None
        self._sealed = False
        self._policy_authority: object = _UNBOUND_POLICY_AUTHORITY
        self._issuance_token = object()
        self._issued_preparations: dict[int, PreparedToolInvocation] = {}
        self._lock = RLock()

    @property
    def sealed(self) -> bool:
        with self._lock:
            return self._sealed

    def register(
        self,
        spec: ToolSpec[ArgumentsT],
        handler: ToolHandler[ArgumentsT],
    ) -> None:
        """注册同源 spec/handler；重名与运行期变更在启动边界失败。"""
        if not isinstance(spec, ToolSpec):
            raise TypeError("spec must be ToolSpec")
        if not callable(handler):
            raise ToolConfigurationError("invalid_tool_handler")
        with self._lock:
            if self._sealed:
                raise ToolConfigurationError("tool_registry_sealed")
            if spec.name in self._entries:
                raise ToolConfigurationError("duplicate_tool_name")
            self._entries[spec.name] = _Entry(spec, handler)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """返回按名称排序的固定 D2 definitions；第一次调用原子地 seal。"""
        with self._lock:
            if self._definitions is None:
                self._definitions = tuple(
                    self._entries[name].spec.definition()
                    for name in sorted(self._entries)
                )
                self._sealed = True
            return self._definitions

    def bind_policy_authority(self, authority: object) -> None:
        """Bind the sole D9 policy authority; rebinding cannot replace it."""
        if authority is None:
            raise TypeError("authority must not be None")
        with self._lock:
            if self._policy_authority is _UNBOUND_POLICY_AUTHORITY:
                self._policy_authority = authority
                return
            if self._policy_authority is not authority:
                raise ToolConfigurationError("policy_authority_already_bound")

    def prepare(self, call: ToolCallItem) -> PreparedToolInvocation:
        """Seal, look up and schema-decode a call without invoking its handler."""
        if not isinstance(call, ToolCallItem):
            raise TypeError("call must be ToolCallItem")
        self.definitions()
        with self._lock:
            entry = self._entries.get(call.name)
        if entry is None:
            prepared = PreparedToolInvocation(
                self,
                None,
                call,
                None,
                tool_error_result("unknown_tool"),
                self._issuance_token,
            )
            with self._lock:
                self._issued_preparations[id(prepared)] = prepared
            return prepared
        try:
            arguments = entry.spec.decode(call.arguments_json)
        except ToolArgumentError as error:
            prepared = PreparedToolInvocation(
                self,
                entry,
                call,
                None,
                argument_error_result(error),
                self._issuance_token,
            )
            with self._lock:
                self._issued_preparations[id(prepared)] = prepared
            return prepared
        prepared = PreparedToolInvocation(
            self,
            entry,
            call,
            arguments,
            None,
            self._issuance_token,
        )
        with self._lock:
            self._issued_preparations[id(prepared)] = prepared
        return prepared

    def invoke_prepared(
        self,
        prepared: PreparedToolInvocation,
        *,
        context: ToolExecutionContext,
        authority: object,
    ) -> ToolExecutionResult:
        """Invoke one registry-issued preparation under the bound authority."""
        if not isinstance(prepared, PreparedToolInvocation):
            raise TypeError("prepared must be PreparedToolInvocation")
        if not isinstance(context, ToolExecutionContext):
            raise TypeError("context must be ToolExecutionContext")
        with self._lock:
            if (
                self._policy_authority is _UNBOUND_POLICY_AUTHORITY
                or authority is not self._policy_authority
            ):
                raise ToolRegistryError("invalid_policy_authority")
            self._validate_prepared_locked(prepared)
            del self._issued_preparations[id(prepared)]
        return self._invoke_prepared(prepared, context=context)

    def discard_prepared(
        self,
        prepared: PreparedToolInvocation,
        *,
        authority: object,
    ) -> bool:
        """Consume an issued preparation; False if it was already consumed."""
        if not isinstance(prepared, PreparedToolInvocation):
            raise TypeError("prepared must be PreparedToolInvocation")
        with self._lock:
            if (
                self._policy_authority is _UNBOUND_POLICY_AUTHORITY
                or authority is not self._policy_authority
            ):
                raise ToolRegistryError("invalid_policy_authority")
            if self._issued_preparations.get(id(prepared)) is not prepared:
                if (
                    prepared._registry is self
                    and prepared._issuance_token is self._issuance_token
                ):
                    return False
                raise ToolRegistryError("invalid_prepared_invocation")
            del self._issued_preparations[id(prepared)]
            return True

    def execute(
        self,
        call: ToolCallItem,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """完成 name/schema gate 后调用 typed handler；预期参数错误模型可见。"""
        if not isinstance(call, ToolCallItem):
            raise TypeError("call must be ToolCallItem")
        if not isinstance(context, ToolExecutionContext):
            raise TypeError("context must be ToolExecutionContext")
        with self._lock:
            if self._policy_authority is not _UNBOUND_POLICY_AUTHORITY:
                raise ToolRegistryError("policy_authorization_required")
        prepared = self.prepare(call)
        with self._lock:
            self._validate_prepared_locked(prepared)
            del self._issued_preparations[id(prepared)]
        return self._invoke_prepared(prepared, context=context)

    def _validate_prepared_locked(self, prepared: PreparedToolInvocation) -> None:
        if (
            prepared._registry is not self
            or prepared._issuance_token is not self._issuance_token
            or self._issued_preparations.get(id(prepared)) is not prepared
        ):
            raise ToolRegistryError("invalid_prepared_invocation")
        entry = prepared._entry
        if entry is None:
            valid = (
                prepared._early_result is not None
                and prepared._arguments is None
                and self._entries.get(prepared._call.name) is None
            )
        else:
            valid = (
                self._entries.get(prepared._call.name) is entry
                and entry.spec.name == prepared._call.name
                and (
                    (prepared._early_result is None)
                    != (prepared._arguments is None)
                )
            )
        if not valid:
            raise ToolRegistryError("invalid_prepared_invocation")

    @staticmethod
    def _invoke_prepared(
        prepared: PreparedToolInvocation,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        early_result = prepared._early_result
        if early_result is not None:
            return early_result
        entry = prepared._entry
        if entry is None or prepared._arguments is None:
            raise ToolRegistryError("invalid_prepared_invocation")
        try:
            arguments = entry.spec.decode(prepared._call.arguments_json)
        except ToolArgumentError:
            raise ToolRegistryError("prepared_invocation_schema_drift") from None

        # 不捕获 handler 异常：AgentLoop 负责脱敏为 tool_executor_failed。
        result = entry.handler(arguments, context=context)
        if not isinstance(result, ToolExecutionResult):
            raise ToolRegistryError("invalid_tool_handler_result")
        return result
