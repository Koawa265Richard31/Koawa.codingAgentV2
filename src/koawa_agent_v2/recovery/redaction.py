"""Deterministic redaction for D6 persisted model context and tool data."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping


REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|private[_-]?key|password|passwd|secret|token|credential)s?(?:$|[_-])",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)"
    r"\s*([:=])\s*([^\s,;]+)"
)


def redact_text(value: str) -> str:
    """Remove common credential shapes while preserving useful surrounding text."""

    redacted = _BEARER.sub(REDACTED, value)
    redacted = _OPENAI_KEY.sub(REDACTED, redacted)
    return _ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", redacted)


def redact_json_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact sensitive fields and strings from a JSON-compatible value."""

    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_json_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [redact_json_value(item) for item in value]
    return value


def redact_arguments_json(value: str) -> str:
    """Redact a tool arguments document without changing whether it is valid JSON."""

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return redact_text(value)
    return json.dumps(
        redact_json_value(parsed),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
