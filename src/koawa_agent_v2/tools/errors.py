"""D3 工具层可以安全跨边界传播的稳定错误。

配置错误在 durable Turn 启动前抛出；模型可修正的调用错误则被编码成
有界、确定性的 ``ToolExecutionResult``，且不包含原始参数或底层异常正文。
"""

from __future__ import annotations

import json
import re

from ..execution.loop import ToolExecutionResult


MAX_TOOL_ERROR_CONTENT_CHARS = 512

_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_ERROR_REASON = re.compile(r"[a-z][a-z0-9_]{0,127}")
_ERROR_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}(?:\[[0-9]{1,10}\])?")


class ToolConfigurationError(ValueError):
    """启动期工具配置无效；异常正文只包含稳定分类。"""

    def __init__(self, code: str) -> None:
        self.code = _stable_token(code, _ERROR_CODE, "code")
        super().__init__(self.code)


class ToolRegistryError(RuntimeError):
    """Registry 或 handler 违反本地执行合同。"""

    def __init__(self, code: str) -> None:
        self.code = _stable_token(code, _ERROR_CODE, "code")
        super().__init__(self.code)


class ToolArgumentError(Exception):
    """模型可修正的参数错误；不保存导致失败的原始值。"""

    code = "invalid_tool_arguments"

    def __init__(self, reason: str, *, field: str | None = None) -> None:
        self.reason = _stable_token(reason, _ERROR_REASON, "reason")
        if field is not None:
            field = _stable_token(field, _ERROR_FIELD, "field")
        self.field = field
        super().__init__(self.code)


def tool_error_result(
    code: str,
    *,
    reason: str | None = None,
    field: str | None = None,
) -> ToolExecutionResult:
    """构造模型可见的稳定 JSON 错误，不回显工具名、参数值或异常正文。"""
    code = _stable_token(code, _ERROR_CODE, "code")
    error: dict[str, str] = {"code": code}
    if reason is not None:
        error["reason"] = _stable_token(reason, _ERROR_REASON, "reason")
    if field is not None:
        error["field"] = _stable_token(field, _ERROR_FIELD, "field")
    content = json.dumps(
        {"error": error},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(content) > MAX_TOOL_ERROR_CONTENT_CHARS:  # defensive invariant
        raise ToolRegistryError("tool_error_result_too_large")
    return ToolExecutionResult(content, is_error=True)


def argument_error_result(error: ToolArgumentError) -> ToolExecutionResult:
    """把内部参数拒绝转换成 Registry 的模型可见结果。"""
    if not isinstance(error, ToolArgumentError):
        raise TypeError("error must be ToolArgumentError")
    return tool_error_result(error.code, reason=error.reason, field=error.field)


def _stable_token(value: str, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid tool error {name}")
    return value
