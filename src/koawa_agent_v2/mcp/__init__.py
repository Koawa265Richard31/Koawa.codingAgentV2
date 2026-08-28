"""D10 MCP lifecycle: stdio transport, sessions, and tool bindings."""

from .protocol import (
    INITIALIZE,
    INITIALIZED_NOTIFICATION,
    MCP_PROTOCOL_VERSION,
    TOOLS_CALL,
    TOOLS_LIST,
    TOOLS_LIST_CHANGED_NOTIFICATION,
    ErrorObject,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    McpProtocolError,
    Notification,
    parse_message,
)
from .transport import StdioTransport, spawn_fixture_command
from .tool_binding import (
    McpBinding,
    McpBindingError,
    McpCatalog,
    McpRegistryAdapter,
    bind_catalog,
    build_mcp_registry,
)
from .connection_manager import (
    McpCallResult,
    McpOutcomeUncertain,
    McpSession,
    McpSessionError,
    bind_tool_handler,
)
from .activation import ActivationService, McpActivationError
from .launcher import (
    HostTrustedLauncher,
    McpProcessLauncher,
    SandboxedLauncher,
)

__all__ = [
    "INITIALIZE",
    "INITIALIZED_NOTIFICATION",
    "MCP_PROTOCOL_VERSION",
    "TOOLS_CALL",
    "TOOLS_LIST",
    "TOOLS_LIST_CHANGED_NOTIFICATION",
    "ErrorObject",
    "JsonRpcNotification",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "McpProtocolError",
    "Notification",
    "parse_message",
    "StdioTransport",
    "spawn_fixture_command",
    "McpBinding",
    "McpBindingError",
    "McpCatalog",
    "McpRegistryAdapter",
    "bind_catalog",
    "build_mcp_registry",
    "McpCallResult",
    "McpOutcomeUncertain",
    "McpSession",
    "McpSessionError",
    "bind_tool_handler",
    "ActivationService",
    "McpActivationError",
    "HostTrustedLauncher",
    "McpProcessLauncher",
    "SandboxedLauncher",
]
