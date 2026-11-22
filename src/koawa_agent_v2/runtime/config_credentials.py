"""运行时配置的环境凭据解析。

``resolve_api_key`` / ``resolve_canary_key`` 只从既定环境入口 ``os.environ``
读取凭据值,配置对象里永远只保存环境变量名。配置类型在 ``config_models``;
本模块单向依赖 ``config_models``,不得依赖 config_loading 或兼容入口 config。
"""

from __future__ import annotations

import os

from .config_models import ProviderConfig, RuntimeConfig, RuntimeConfigError


def resolve_canary_key(config: "RuntimeConfig") -> bytes | None:
    """RT/J J2 / PSEC semantics-C: HMAC canary key from the configured env var.

    ``canary_key_env=None`` (default) means the gate is inactive and None is
    returned.  A configured env var that is missing or trivially short is a
    hard error (fail closed): a typo must not silently disable security.  The
    key value is read from the environment only and never persisted — config
    stores the variable name, mirroring ``api_key_env``.
    """
    env_name = config.canary_key_env
    if env_name is None:
        return None
    value = os.environ.get(env_name)
    if value is None:
        raise RuntimeConfigError("canary_key_missing")
    if len(value.encode("utf-8", "strict")) < 16:
        raise RuntimeConfigError("canary_key_invalid")
    return value.encode("utf-8", "strict")


def resolve_api_key(provider: ProviderConfig) -> str:
    """Read the configured provider key from the process environment only."""
    value = os.environ.get(provider.api_key_env)
    if value is None or not value or value != value.strip():
        raise RuntimeConfigError("api_key_missing")
    if "\r" in value or "\n" in value:
        raise RuntimeConfigError("api_key_invalid")
    return value
