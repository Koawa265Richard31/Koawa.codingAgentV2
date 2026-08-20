"""KoawaAgent V2 stable public API over slice-oriented subpackages."""

from .control import EventStoreError, SqliteEventStore, ThreadRuntime
from .execution import (
    AgentLoop,
    AgentLoopLimits,
    CancellationToken,
    ToolExecutionResult,
    TurnWorker,
)
from .model import OpenAICompatibleChatClient
from .tools import (
    RepositoryToolLimits,
    RepositoryToolRegistry,
    ToolRegistry,
    ToolSpec,
    WorkspacePathError,
    WorkspacePathResolver,
    build_repository_tool_registry,
)
from .verification import (
    CodingToolRegistry,
    CommandProfile,
    RepositoryTrust,
    TrustedCommandRunner,
    build_verified_coding_tool_registry,
)

__all__ = [
    "AgentLoop",
    "AgentLoopLimits",
    "CancellationToken",
    "CodingToolRegistry",
    "CommandProfile",
    "EventStoreError",
    "OpenAICompatibleChatClient",
    "RepositoryToolLimits",
    "RepositoryToolRegistry",
    "RepositoryTrust",
    "SqliteEventStore",
    "ThreadRuntime",
    "ToolExecutionResult",
    "ToolRegistry",
    "ToolSpec",
    "TurnWorker",
    "TrustedCommandRunner",
    "WorkspacePathError",
    "WorkspacePathResolver",
    "build_repository_tool_registry",
    "build_verified_coding_tool_registry",
]
