"""P0 runtime configuration: strict JSON config for the real model runtime.

The config file is trusted local administrator input.  It never contains API
keys; a provider key is loaded only from the environment variable named by
``api_key_env``.

Compatibility entry: the implementation now lives in ``config_models`` /
``config_loading`` / ``config_credentials``; this module only re-exports the
original public surface (the types are shared objects, not redefined).
"""

from __future__ import annotations

from .config_credentials import resolve_api_key, resolve_canary_key
from .config_loading import (
    CONFIG_MAX_BYTES,
    CONFIG_SCHEMA_VERSION,
    DEFAULT_SYSTEM_PROMPT,
    load_runtime_config,
)
from .config_models import (
    MCP_CODE_ARTIFACT_ROLES,
    McpCodeArtifact,
    McpExecutionProfile,
    McpResourceLimits,
    McpServerConfig,
    PolicyConfig,
    ProviderConfig,
    RepositoryTrustMode,
    RuntimeConfig,
    RuntimeConfigError,
    SandboxConfig,
    SandboxRunner,
    TestProfileConfig,
)
