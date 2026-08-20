"""MCP 2025-06-18 JSON-RPC 2.0 message boundary (stdlib only).

This module owns the exact wire contract: strict JSON parsing with duplicate
key / NaN / depth / node limits, stable error codes, and canonical payload
serialization.  Raw message bodies never appear in exceptions or reprs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


MCP_PROTOCOL_VERSION = "2025-06-18"

INITIALIZE = "initialize"
INITIALIZED_NOTIFICATION = "notifications/initialized"
TOOLS_LIST = "tools/list"
TOOLS_CALL = "tools/call"
TOOLS_LIST_CHANGED_NOTIFICATION = "notifications/tools/list_changed"

JSONRPC_VERSION = "2.0"

MAX_MESSAGE_CHARS = 1_048_576
MAX_MESSAGE_DEPTH = 32
MAX_MESSAGE_NODES = 100_000
MAX_METHOD_CHARS = 256
MAX_ERROR_MESSAGE_CHARS = 1_024

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_ERROR_BASE = -32000
SERVER_ERROR_LIMIT = -32099


class McpProtocolError(ValueError):
    """Stable, content-free MCP protocol failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _bounded_text(value: Any, name: str, maximum: int, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise McpProtocolError(code)
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise McpProtocolError(code) from None
    if "\x00" in value:
        raise McpProtocolError(code)
    return value


def _message_id(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise McpProtocolError("invalid_request_id")
    if value < 0 or value > (2**63 - 1):
        raise McpProtocolError("invalid_request_id")
    return value


@dataclass(frozen=True, slots=True)
class ErrorObject:
    code: int
    message: str
    data: Any | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, int) or isinstance(self.code, bool):
            raise McpProtocolError("invalid_response")
        _bounded_text(
            self.message,
            "message",
            MAX_ERROR_MESSAGE_CHARS,
            "invalid_response",
        )


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    jsonrpc: str
    id: int
    method: str
    params: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.jsonrpc != JSONRPC_VERSION:
            raise McpProtocolError("unsupported_jsonrpc_version")
        _message_id(self.id)
        _bounded_text(self.method, "method", MAX_METHOD_CHARS, "invalid_method")
        if self.params is not None and not isinstance(self.params, dict):
            raise McpProtocolError("invalid_params")


@dataclass(frozen=True, slots=True)
class JsonRpcResponse:
    jsonrpc: str
    id: int
    result: Any | None = None
    error: ErrorObject | None = None

    def __post_init__(self) -> None:
        if self.jsonrpc != JSONRPC_VERSION:
            raise McpProtocolError("unsupported_jsonrpc_version")
        _message_id(self.id)
        if (self.result is None) == (self.error is None):
            # One of result/error must be present; result may legitimately be
            # null only when error is absent, so None-vs-None means neither.
            if self.result is None and self.error is None:
                raise McpProtocolError("invalid_response")
            if self.result is not None and self.error is not None:
                raise McpProtocolError("invalid_response")


@dataclass(frozen=True, slots=True)
class JsonRpcNotification:
    jsonrpc: str
    method: str
    params: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.jsonrpc != JSONRPC_VERSION:
            raise McpProtocolError("unsupported_jsonrpc_version")
        _bounded_text(self.method, "method", MAX_METHOD_CHARS, "invalid_method")
        if self.params is not None and not isinstance(self.params, dict):
            raise McpProtocolError("invalid_params")


Notification = JsonRpcNotification


def _strict_json_object(raw: str) -> dict[str, Any]:
    def unique_pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise McpProtocolError("duplicate_key")
            result[key] = value
        return result

    def invalid_constant(_: str) -> Any:
        raise McpProtocolError("non_json_number")

    if not isinstance(raw, str) or len(raw) > MAX_MESSAGE_CHARS:
        raise McpProtocolError("protocol_size_exceeded")
    try:
        document = json.loads(
            raw,
            object_pairs_hook=unique_pairs,
            parse_constant=invalid_constant,
        )
    except McpProtocolError:
        raise
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise McpProtocolError("malformed_json") from None
    if not isinstance(document, dict):
        raise McpProtocolError("malformed_json")
    _validate_limits(document, depth=0, nodes=0)
    return document


def _validate_limits(value: Any, *, depth: int, nodes: int) -> int:
    if depth > MAX_MESSAGE_DEPTH:
        raise McpProtocolError("protocol_size_exceeded")
    if nodes > MAX_MESSAGE_NODES:
        raise McpProtocolError("protocol_size_exceeded")
    if isinstance(value, dict):
        count = nodes + 1
        for item in value.values():
            count = _validate_limits(item, depth=depth + 1, nodes=count)
        return count
    if isinstance(value, list):
        count = nodes + 1
        for item in value:
            count = _validate_limits(item, depth=depth + 1, nodes=count)
        return count
    return nodes + 1


def parse_message(raw: str) -> JsonRpcRequest | JsonRpcResponse | JsonRpcNotification:
    """Parse one strict JSON-RPC 2.0 message from an MCP peer."""

    document = _strict_json_object(raw)
    if document.get("jsonrpc") != JSONRPC_VERSION:
        raise McpProtocolError("unsupported_jsonrpc_version")
    has_id = "id" in document
    method = document.get("method")
    if "method" in document:
        if not isinstance(method, str) or not method or len(method) > MAX_METHOD_CHARS:
            raise McpProtocolError("invalid_method")
        params = document.get("params")
        if params is not None and not isinstance(params, dict):
            raise McpProtocolError("invalid_params")
        if has_id:
            return JsonRpcRequest(JSONRPC_VERSION, _message_id(document["id"]), method, params)
        return JsonRpcNotification(JSONRPC_VERSION, method, params)
    if not has_id:
        raise McpProtocolError("invalid_response")
    request_id = _message_id(document["id"])
    result_present = "result" in document
    error_present = "error" in document
    if result_present == error_present:
        raise McpProtocolError("invalid_response")
    if error_present:
        raw_error = document["error"]
        if not isinstance(raw_error, Mapping):
            raise McpProtocolError("invalid_response")
        code = raw_error.get("code")
        message = raw_error.get("message")
        if not isinstance(code, int) or isinstance(code, bool):
            raise McpProtocolError("invalid_response")
        if not isinstance(message, str) or not message or len(message) > MAX_ERROR_MESSAGE_CHARS:
            raise McpProtocolError("invalid_response")
        try:
            message.encode("utf-8", "strict")
        except UnicodeError:
            raise McpProtocolError("invalid_response") from None
        if "\x00" in message:
            raise McpProtocolError("invalid_response")
        return JsonRpcResponse(
            JSONRPC_VERSION,
            request_id,
            None,
            ErrorObject(code, message, raw_error.get("data")),
        )
    return JsonRpcResponse(JSONRPC_VERSION, request_id, document["result"], None)


def _canonical_dump(document: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError):
        raise McpProtocolError("invalid_payload") from None


def request_payload(
    request_id: int,
    method: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    _message_id(request_id)
    _bounded_text(method, "method", MAX_METHOD_CHARS, "invalid_method")
    document: dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "method": method,
    }
    if params is not None:
        document["params"] = dict(params)
    return _canonical_dump(document)


def notification_payload(
    method: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    _bounded_text(method, "method", MAX_METHOD_CHARS, "invalid_method")
    document: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        document["params"] = dict(params)
    return _canonical_dump(document)


def response_payload(request_id: int, result: Any) -> str:
    _message_id(request_id)
    return _canonical_dump(
        {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}
    )


def error_payload(request_id: int, code: int, message: str) -> str:
    _message_id(request_id)
    _bounded_text(message, "message", MAX_ERROR_MESSAGE_CHARS, "invalid_payload")
    return _canonical_dump(
        {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )
