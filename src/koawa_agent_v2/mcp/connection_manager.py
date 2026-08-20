"""Durable MCP session lifecycle: initialize, catalog, call, refresh, close."""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID, uuid4

from ..execution.loop import ToolExecutionContext, ToolExecutionResult
from ..recovery.redaction import redact_text
from .protocol import (
    INITIALIZE,
    INITIALIZED_NOTIFICATION,
    MCP_PROTOCOL_VERSION,
    TOOLS_CALL,
    TOOLS_LIST,
    TOOLS_LIST_CHANGED_NOTIFICATION,
    JsonRpcNotification,
    JsonRpcResponse,
    McpProtocolError,
    notification_payload,
    request_payload,
)
from .tool_binding import McpBinding, McpBindingError, McpCatalog, bind_catalog
from .transport import TransportError, TransportTimeout
from ..telemetry.trace import TraceStore


class McpSessionError(RuntimeError):
    """Stable, content-free MCP session failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class McpOutcomeUncertain(Exception):
    """The server may have applied the call; the Runtime must not retry blindly."""


@dataclass(frozen=True, slots=True)
class McpCallResult:
    content: str
    is_error: bool
    uncertain: bool


class _PendingCall:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: JsonRpcResponse | None = None
        self.error: McpSessionError | None = None


class McpSession:
    """One MCP stdio session with an immutable per-generation tool catalog."""

    CREATED = "created"
    CONNECTING = "connecting"
    READY = "ready"
    REFRESHING = "refreshing"
    CLOSED = "closed"
    FAILED = "failed"

    def __init__(
        self,
        server_id: str,
        transport,
        *,
        protocol_version: str = MCP_PROTOCOL_VERSION,
        request_timeout: float = 10.0,
        max_tools: int = 128,
        max_result_chars: int = 262_144,
        auto_refresh: bool = False,
        trace_store: TraceStore | None = None,
        correlation_id: Any | None = None,
    ) -> None:
        from .tool_binding import _TOOL_NAME

        if not isinstance(server_id, str) or not _TOOL_NAME.fullmatch(server_id):
            raise McpSessionError("invalid_mcp_server_id")
        for method in ("open", "send", "read", "close"):
            if not callable(getattr(transport, method, None)):
                raise TypeError(f"transport must implement {method}()")
        self._server_id = server_id
        self._transport = transport
        self._protocol_version = protocol_version
        self._request_timeout = request_timeout
        self._max_tools = max_tools
        self._max_result_chars = max_result_chars
        self._auto_refresh = auto_refresh
        self._trace_store = trace_store
        self._correlation_id = correlation_id
        self._next_id = itertools.count(1)
        self._pending: dict[int, _PendingCall] = {}
        self._pending_lock = threading.Lock()
        self._catalog: McpCatalog | None = None
        self._generation = 0
        self._state = self.CREATED
        self._state_lock = threading.Lock()
        self._notify_thread: threading.Thread | None = None
        self._closed = False
        self._pending_refresh = False
        self._unknown_response_count = 0

    @property
    def server_id(self) -> str:
        return self._server_id

    @property
    def state(self) -> str:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def catalog(self) -> McpCatalog | None:
        return self._catalog

    @property
    def pending_refresh(self) -> bool:
        return self._pending_refresh

    @property
    def unknown_response_count(self) -> int:
        return self._unknown_response_count

    def connect(self) -> McpCatalog:
        with self._state_lock:
            if self._state not in (self.CREATED, self.FAILED):
                raise McpSessionError("mcp_session_state_invalid")
            self._state = self.CONNECTING
        try:
            self._transport.open()
        except TransportError as error:
            self._state = self.FAILED
            raise McpSessionError(error.code) from None
        self._notify_thread = threading.Thread(
            target=self._notification_loop,
            name=f"mcp-notify-{self._server_id}",
            daemon=True,
        )
        self._notify_thread.start()
        try:
            initialize_result = self._request(
                INITIALIZE,
                {
                    "protocolVersion": self._protocol_version,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "koawa-agent-v2",
                        "version": "0.1.0",
                    },
                },
            )
            if not isinstance(initialize_result, Mapping):
                raise McpSessionError("mcp_initialize_failed")
            if initialize_result.get("protocolVersion") != self._protocol_version:
                raise McpSessionError("mcp_protocol_version_mismatch")
            self._transport.send(notification_payload(INITIALIZED_NOTIFICATION))
            catalog = self._list_tools(1)
            self._catalog = catalog
            self._generation = 1
            self._state = self.READY
            return catalog
        except McpSessionError:
            self._state = self.FAILED
            self._shutdown_transport()
            raise
        except (TransportError, McpProtocolError) as error:
            self._state = self.FAILED
            self._shutdown_transport()
            raise McpSessionError(
                getattr(error, "code", "mcp_transport_closed")
            ) from None

    def refresh(self) -> McpCatalog:
        with self._state_lock:
            if self._state != self.READY:
                raise McpSessionError("mcp_session_not_ready")
            self._state = self.REFRESHING
        try:
            catalog = self._list_tools(self._generation + 1)
            self._catalog = catalog
            self._generation += 1
            self._pending_refresh = False
            return catalog
        except (McpSessionError, TransportError, McpProtocolError) as error:
            raise McpSessionError(
                getattr(error, "code", "mcp_refresh_failed")
            ) from None
        finally:
            self._state = self.READY

    def call(
        self,
        binding: McpBinding,
        arguments_json: str,
        *,
        timeout: float | None = None,
    ) -> McpCallResult:
        if self._state != self.READY:
            raise McpSessionError("mcp_session_not_ready")
        if (
            binding.server_id != self._server_id
            or binding.session_generation != self._generation
        ):
            raise McpSessionError("mcp_binding_stale")
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError:
            raise McpSessionError("invalid_mcp_arguments") from None
        if not isinstance(arguments, dict):
            raise McpSessionError("invalid_mcp_arguments")
        pending = _PendingCall()
        request_id = next(self._next_id)
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            self._transport.send(
                request_payload(
                    request_id,
                    TOOLS_CALL,
                    {"name": binding.tool_name, "arguments": arguments},
                )
            )
        except TransportError as error:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise McpSessionError(error.code) from None
        deadline = self._request_timeout if timeout is None else timeout
        if not pending.event.wait(deadline):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            self._trace("mcp", "call_uncertain", {"server_id": self._server_id})
            return McpCallResult("mcp_call_timeout", True, True)
        if pending.error is not None:
            raise pending.error
        message = pending.result
        if message is None or message.error is not None:
            content = "mcp_request_failed" if message is None else message.error.message
            self._trace(
                "mcp",
                "call_error",
                {"server_id": self._server_id, "result_code": "error"},
            )
            return McpCallResult(content, True, False)
        result = self._extract_result(message.result)
        self._trace(
            "mcp",
            "call_result",
            {
                "server_id": self._server_id,
                "result_code": "error" if result.is_error else "ok",
            },
        )
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._state_lock:
            self._state = self.CLOSED
        with self._pending_lock:
            for pending in self._pending.values():
                pending.error = McpSessionError("mcp_session_closed")
                pending.event.set()
            self._pending.clear()
        try:
            self._transport.close()
        except TransportError:
            pass
        if self._notify_thread is not None:
            self._notify_thread.join(timeout=5)

    def _shutdown_transport(self) -> None:
        try:
            self._transport.close()
        except TransportError:
            pass
        if self._notify_thread is not None:
            self._notify_thread.join(timeout=5)

    def handler(self, binding: McpBinding):
        """Return the D3-style typed handler bound to this session/binding."""

        return bind_tool_handler(self, binding)

    def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        pending = _PendingCall()
        request_id = next(self._next_id)
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            self._transport.send(request_payload(request_id, method, dict(params)))
        except TransportError as error:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise McpSessionError(error.code) from None
        deadline = self._request_timeout if timeout is None else timeout
        if not pending.event.wait(deadline):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise McpSessionError("mcp_request_timeout")
        if pending.error is not None:
            raise pending.error
        message = pending.result
        if message is None or message.error is not None:
            raise McpSessionError("mcp_request_failed")
        return message.result

    def _list_tools(self, generation: int) -> McpCatalog:
        tools: list[Any] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            result = self._request(TOOLS_LIST, params)
            if not isinstance(result, Mapping):
                raise McpSessionError("mcp_tools_list_failed")
            page = result.get("tools")
            if not isinstance(page, list):
                raise McpSessionError("mcp_tools_list_failed")
            tools.extend(page)
            if len(tools) > self._max_tools:
                raise McpSessionError("mcp_tool_limit_exceeded")
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        try:
            return bind_catalog(self._server_id, generation, tools)
        except McpBindingError as error:
            raise McpSessionError(error.code) from None

    def _extract_result(self, result: Any) -> McpCallResult:
        if not isinstance(result, Mapping):
            return McpCallResult("mcp_invalid_result", True, False)
        is_error = bool(result.get("isError", False))
        content = result.get("content")
        parts: list[str] = []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, Mapping):
                    continue
                kind = item.get("type")
                if kind == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif kind == "image":
                    mime = item.get("mimeType")
                    parts.append(f"[mcp-image:{mime or 'unknown'}]")
        joined = "\n".join(parts)
        budget = max(self._max_result_chars - 2_048, 0)
        if len(joined) > budget:
            joined = joined[:budget]
            is_error = True
        return McpCallResult(joined, is_error, False)

    def _notification_loop(self) -> None:
        while not self._closed:
            try:
                message = self._transport.read(self._request_timeout)
            except TransportTimeout:
                # No message arrived within the poll window; the session is
                # still healthy and the caller's own deadline decides timeouts.
                continue
            except TransportError as error:
                self._fail_pending(
                    McpSessionError(
                        getattr(error, "code", "mcp_transport_closed")
                    )
                )
                if self._state != self.CLOSED:
                    self._state = self.FAILED
                return
            if isinstance(message, JsonRpcNotification):
                if message.method == TOOLS_LIST_CHANGED_NOTIFICATION:
                    self._pending_refresh = True
                    if self._auto_refresh and self._state == self.READY:
                        threading.Thread(
                            target=self._safe_refresh,
                            name=f"mcp-refresh-{self._server_id}",
                            daemon=True,
                        ).start()
                continue
            if isinstance(message, JsonRpcResponse):
                with self._pending_lock:
                    pending = self._pending.pop(message.id, None)
                if pending is None:
                    self._unknown_response_count += 1
                    continue
                pending.result = message
                pending.event.set()
                continue
            # Unexpected message types fail the session closed.
            self._fail_pending(McpSessionError("mcp_protocol_error"))
            self._state = self.FAILED
            return

    def _fail_pending(self, error: McpSessionError) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = error
            item.event.set()

    def _trace(self, stream: str, kind: str, fields: Mapping[str, Any]) -> None:
        if self._trace_store is None:
            return
        correlation_id = self._correlation_id
        if not isinstance(correlation_id, UUID):
            correlation_id = uuid4()
        self._trace_store.append(
            correlation_id=correlation_id,
            stream=stream,
            kind=kind,
            fields=fields,
        )

    def _safe_refresh(self) -> None:
        try:
            self.refresh()
        except McpSessionError:
            pass


def bind_tool_handler(session: McpSession, binding: McpBinding):
    """Build a D3 typed handler that routes one binding to the live session."""

    def handler(arguments: Any, *, context: ToolExecutionContext) -> ToolExecutionResult:
        document = asdict(arguments)
        arguments_json = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        result = session.call(binding, arguments_json)
        if result.uncertain:
            raise McpOutcomeUncertain("mcp_call_timeout")
        redacted = redact_text(result.content)
        envelope = {
            "untrusted_mcp_result": True,
            "server_id": binding.server_id,
            "tool": binding.tool_name,
            "result": redacted,
        }
        content = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return ToolExecutionResult(content, result.is_error)

    return handler
