"""I4 durable JSON hard boundaries and canonical user text.

This module is the single authoritative implementation of the durable JSON
ingress/egress contract (section 6.2) and the canonical text DTO (section 6.3):

- fixed immutable protocol profiles for reading historical payloads/metadata/
  receipts/checkpoints/config (later RuntimeConfig changes cannot shrink them);
- adjustable runtime ingress limits (exact 11-key policy) that only constrain
  events/text written by the current process;
- a non-recursive strict JSON validator (nodes/depth/bytes/members/items/keys)
  and a strict bytes loader that rejects duplicate keys, non-finite numbers
  and invalid UTF-8 at any depth before any counting happens;
- the CanonicalText DTO with a fixed canonicalization order
  (type -> CRLF/CR->LF -> NFC -> control/surrogate check -> credential-shape
  redaction -> UTF-8 byte limit; emptiness is decided by strip only, and the
  surrounding whitespace of the value is never trimmed).

The EventStore's job is to validate and reject; it never silently rewrites a
business payload.  Text redaction happens at the domain entry point
(ThreadRuntime/config), so the same canonical value flows into the first
model request, idempotency identity, persisted events and kill/resume.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# content-free exceptions
# ---------------------------------------------------------------------------


class DurableJsonError(RuntimeError):
    """Content-free durable JSON failure; the message never echoes user text."""

    def __init__(self, code: str, path: str = "payload") -> None:
        self.code = code
        self.path = path
        super().__init__(code + " at " + path)


class DurableJsonLimitExceeded(DurableJsonError):
    """A durable JSON hard limit was exceeded (limit+1 must fail like this)."""


class CanonicalTextError(RuntimeError):
    """Content-free canonical text failure; carries only a stable code/path."""

    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(code + " at " + path)


# ---------------------------------------------------------------------------
# DurableJsonLimits + fixed immutable protocol profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DurableJsonLimits:
    """One durable JSON policy: byte/depth/node/string/member/item/key caps.

    max_utf8_bytes bounds the whole UTF-8 encoding of the document,
    max_depth counts the root as depth 1, max_nodes counts every JSON
    value (root included), and the remaining fields bound string values,
    object members, array items and object key bytes respectively.
    """

    max_utf8_bytes: int
    max_depth: int
    max_nodes: int
    max_string_utf8_bytes: int
    max_object_members: int
    max_array_items: int
    max_key_utf8_bytes: int

    _FIELDS = (
        "max_utf8_bytes", "max_depth", "max_nodes",
        "max_string_utf8_bytes", "max_object_members",
        "max_array_items", "max_key_utf8_bytes",
    )

    def __post_init__(self) -> None:
        for name in self._FIELDS:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(name + " must be a positive integer")


# Historical reads always use these immutable protocol profiles.  A later
# smaller RuntimeConfig must never change how old payloads are read.
EVENT_PAYLOAD_READ_V1 = DurableJsonLimits(
    max_utf8_bytes=4_194_304, max_depth=32, max_nodes=100_000,
    max_string_utf8_bytes=2_097_152, max_object_members=20_000,
    max_array_items=20_000, max_key_utf8_bytes=256,
)
EVENT_METADATA_READ_V1 = DurableJsonLimits(
    max_utf8_bytes=16_384, max_depth=16, max_nodes=2_048,
    max_string_utf8_bytes=8_192, max_object_members=512,
    max_array_items=512, max_key_utf8_bytes=256,
)
IDEMPOTENCY_RECEIPT_READ_V1 = DurableJsonLimits(
    max_utf8_bytes=1_048_576, max_depth=24, max_nodes=20_000,
    max_string_utf8_bytes=262_144, max_object_members=4_096,
    max_array_items=4_096, max_key_utf8_bytes=256,
)
CHECKPOINT_READ_V2 = DurableJsonLimits(
    max_utf8_bytes=4_194_304, max_depth=32, max_nodes=100_000,
    max_string_utf8_bytes=2_097_152, max_object_members=20_000,
    max_array_items=20_000, max_key_utf8_bytes=256,
)
CONFIG_READ_V1 = DurableJsonLimits(
    max_utf8_bytes=1_048_576, max_depth=24, max_nodes=50_000,
    max_string_utf8_bytes=262_144, max_object_members=4_096,
    max_array_items=4_096, max_key_utf8_bytes=128,
)


def validate_json_document(
    value: Any,
    limits: DurableJsonLimits,
    *,
    path: str = "payload",
    require_object: bool = True,
    reject_string_controls: bool = True,
) -> None:
    """Validate one already-parsed JSON-compatible value against the limits.

    Non-recursive explicit stack: a pathological nesting depth cannot overflow
    the Python stack.  Raises content-free DurableJsonError on any violation;
    never mutates or rewrites the value.  reject_string_controls=False keeps
    NUL/surrogate scanning to the DTO preflight layer (used by the config
    loader, whose §6.5 steps stop at the JSON profile; free-text DTOs apply
    CanonicalText afterwards).
    """
    if require_object and not isinstance(value, Mapping):
        raise DurableJsonError("invalid_value_type", path)
    validate_json_value(
        value,
        limits,
        path=path,
        reject_string_controls=reject_string_controls,
    )


def validate_json_value(
    value: Any,
    limits: DurableJsonLimits,
    *,
    path: str = "payload",
    reject_string_controls: bool = True,
) -> None:
    """Validate any JSON value (object/array/scalar) against the limits.

    The explicit stack carries enter/exit markers so cyclic references are
    detected and rejected instead of looping or overflowing the Python stack.
    """
    nodes = 0
    active_containers: set[int] = set()
    # (value, depth, enter): enter=True opens a container, enter=False closes.
    stack: list[tuple[Any, int, bool]] = [(value, 1, True)]
    while stack:
        node, depth, enter = stack.pop()
        if not enter:
            if isinstance(node, (Mapping, tuple, list)):
                active_containers.discard(id(node))
            continue
        nodes += 1
        if nodes > limits.max_nodes:
            raise DurableJsonLimitExceeded("nodes", path)
        if depth > limits.max_depth:
            raise DurableJsonLimitExceeded("depth", path)
        if node is None or isinstance(node, bool):
            continue
        if isinstance(node, int):
            continue
        if isinstance(node, float):
            if not isfinite(node):
                raise DurableJsonError("non_finite_number", path)
            continue
        if isinstance(node, str):
            _check_string(
                node, path, limits, reject_controls=reject_string_controls
            )
            continue
        if isinstance(node, Mapping):
            if id(node) in active_containers:
                raise DurableJsonError("cyclic_reference", path)
            if len(node) > limits.max_object_members:
                raise DurableJsonLimitExceeded("object_members", path)
            active_containers.add(id(node))
            stack.append((node, depth, False))
            for key, item in node.items():
                if not isinstance(key, str):
                    raise DurableJsonError("non_string_key", path)
                if len(key.encode("utf-8")) > limits.max_key_utf8_bytes:
                    raise DurableJsonLimitExceeded("key_bytes", path)
                stack.append((item, depth + 1, True))
            continue
        if isinstance(node, (tuple, list)):
            if id(node) in active_containers:
                raise DurableJsonError("cyclic_reference", path)
            if len(node) > limits.max_array_items:
                raise DurableJsonLimitExceeded("array_items", path)
            active_containers.add(id(node))
            stack.append((node, depth, False))
            for item in node:
                stack.append((item, depth + 1, True))
            continue
        raise DurableJsonError("invalid_value_type", path)


def _check_string(
    value: str,
    path: str,
    limits: DurableJsonLimits,
    *,
    reject_controls: bool,
) -> None:
    """Enforce the per-string UTF-8 byte cap (and optionally NUL/surrogates)."""
    if reject_controls:
        for char in value:
            code = ord(char)
            if code == 0:
                raise DurableJsonError("nul_character", path)
            if 0xD800 <= code <= 0xDFFF:
                raise DurableJsonError("surrogate", path)
    if len(value.encode("utf-8")) > limits.max_string_utf8_bytes:
        raise DurableJsonLimitExceeded("string_bytes", path)


def effective_payload_limits(
    ingress: Mapping[str, int] | None,
) -> DurableJsonLimits:
    """Write-time payload policy: min(EVENT_PAYLOAD_READ_V1, runtime ingress).

    A smaller runtime ingress rejects new writes at the smaller threshold,
    while historical events are still read with the immutable read profile.
    """
    if ingress is None:
        return EVENT_PAYLOAD_READ_V1
    return DurableJsonLimits(
        max_utf8_bytes=min(
            EVENT_PAYLOAD_READ_V1.max_utf8_bytes, ingress["event_payload_max_utf8_bytes"]
        ),
        max_depth=min(
            EVENT_PAYLOAD_READ_V1.max_depth, ingress["event_payload_max_depth"]
        ),
        max_nodes=min(
            EVENT_PAYLOAD_READ_V1.max_nodes, ingress["event_payload_max_nodes"]
        ),
        max_string_utf8_bytes=min(
            EVENT_PAYLOAD_READ_V1.max_string_utf8_bytes,
            ingress["event_payload_max_string_utf8_bytes"],
        ),
        max_object_members=min(
            EVENT_PAYLOAD_READ_V1.max_object_members,
            ingress["event_payload_max_object_members"],
        ),
        max_array_items=min(
            EVENT_PAYLOAD_READ_V1.max_array_items, ingress["event_payload_max_array_items"]
        ),
        max_key_utf8_bytes=min(
            EVENT_PAYLOAD_READ_V1.max_key_utf8_bytes, ingress["event_payload_max_key_utf8_bytes"]
        ),
    )


# ---------------------------------------------------------------------------
# strict bytes loader
# ---------------------------------------------------------------------------


class _DuplicateKeyDetected(ValueError):
    pass


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """object_pairs_hook: reject a duplicate key at ANY nesting level."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyDetected(key)
        result[key] = value
    return result


def _reject_constant(token: str) -> Any:
    """parse_constant: reject NaN/Infinity instead of silently accepting them."""
    raise ValueError("non-finite number token " + token)


def strict_json_loads_bytes(
    data: bytes | bytearray | memoryview,
    limits: DurableJsonLimits,
    *,
    path: str = "payload",
    reject_string_controls: bool = True,
) -> Any:
    """Strictly decode UTF-8 bytes and parse strict JSON, then validate.

    Duplicate keys (any depth) and non-finite numbers are rejected during
    parsing — never collapsed first and counted later.  The caller must have
    bounded the byte length before invoking this (measure-then-decode).
    """
    try:
        text = bytes(data).decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise DurableJsonError("invalid_utf8", path) from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except _DuplicateKeyDetected:
        raise DurableJsonError("duplicate_key", path) from None
    except ValueError:
        raise DurableJsonError("non_finite_number", path) from None
    except json.JSONDecodeError as exc:
        raise DurableJsonError("invalid_json", path) from exc
    validate_json_document(
        parsed,
        limits,
        path=path,
        reject_string_controls=reject_string_controls,
    )
    return parsed


def strict_json_loads_text(
    text: str,
    limits: DurableJsonLimits,
    *,
    path: str = "payload",
    reject_string_controls: bool = True,
) -> Any:
    """Strict parse of an already decoded (strict UTF-8) JSON text string."""
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except _DuplicateKeyDetected:
        raise DurableJsonError("duplicate_key", path) from None
    except ValueError:
        raise DurableJsonError("non_finite_number", path) from None
    except json.JSONDecodeError as exc:
        raise DurableJsonError("invalid_json", path) from exc
    validate_json_document(
        parsed,
        limits,
        path=path,
        reject_string_controls=reject_string_controls,
    )
    return parsed


# ---------------------------------------------------------------------------
# canonical text DTO
# ---------------------------------------------------------------------------

REDACTION_POLICY_VERSION = 1
REDACTED = "[REDACTED]"

# section 6.3 default text ceilings (UTF-8 bytes).
USER_INPUT_MAX_UTF8_BYTES = 65_536
RESUME_INTERRUPT_MAX_UTF8_BYTES = 16_384
TERMINAL_TEXT_MAX_UTF8_BYTES = 65_536
INSTRUCTION_MAX_UTF8_BYTES = 131_072

_CONTROL_REJECTED = frozenset(
    chr(code) for code in range(0x20) if code not in (0x0A, 0x09)
)


@dataclass(frozen=True, slots=True)
class CanonicalText:
    """The single canonical value used for model/event/history/resume.

    value is the finalized text: newline-normalized, NFC-normalized,
    control-checked and credential-shape redacted, with surrounding whitespace
    preserved.  utf8_bytes is its strict UTF-8 length, digest its SHA-256 hex
    digest and redaction_count the number of redaction marks this pass
    introduced (0 for already canonical input).
    """

    value: str
    utf8_bytes: int
    digest: str
    redaction_policy_version: int
    redaction_count: int


@dataclass(frozen=True, slots=True)
class CanonicalTextPolicy:
    """Byte ceilings per canonical text kind (defaults = section 6.2 table)."""

    user_input_max_utf8_bytes: int = USER_INPUT_MAX_UTF8_BYTES
    resume_interrupt_max_utf8_bytes: int = RESUME_INTERRUPT_MAX_UTF8_BYTES
    terminal_text_max_utf8_bytes: int = TERMINAL_TEXT_MAX_UTF8_BYTES
    instruction_max_utf8_bytes: int = INSTRUCTION_MAX_UTF8_BYTES

    _FIELDS = (
        "user_input_max_utf8_bytes",
        "resume_interrupt_max_utf8_bytes",
        "terminal_text_max_utf8_bytes",
        "instruction_max_utf8_bytes",
    )

    def __post_init__(self) -> None:
        for name in self._FIELDS:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(name + " must be a positive integer")

    @classmethod
    def from_ingress(
        cls, ingress: Mapping[str, int] | None
    ) -> "CanonicalTextPolicy":
        """Build the text policy from the exact-key runtime ingress policy."""
        values = {} if ingress is None else ingress
        return cls(
            user_input_max_utf8_bytes=values.get(
                "user_input_max_utf8_bytes", USER_INPUT_MAX_UTF8_BYTES
            ),
            resume_interrupt_max_utf8_bytes=values.get(
                "resume_interrupt_max_utf8_bytes", RESUME_INTERRUPT_MAX_UTF8_BYTES
            ),
            terminal_text_max_utf8_bytes=values.get(
                "terminal_text_max_utf8_bytes", TERMINAL_TEXT_MAX_UTF8_BYTES
            ),
            instruction_max_utf8_bytes=values.get(
                "instruction_max_utf8_bytes", INSTRUCTION_MAX_UTF8_BYTES
            ),
        )


def canonicalize_text(
    value: Any,
    max_utf8_bytes: int,
    *,
    name: str = "text",
) -> CanonicalText:
    """Apply the fixed canonicalization order (section 6.3) to untrusted text.

    Order: type check -> CRLF/CR to LF -> Unicode NFC -> reject NUL/surrogate/
    illegal control (only LF/TAB allowed) -> credential-shape redaction ->
    UTF-8 byte limit.  Emptiness is decided by strip() by the caller only;
    this function never trims surrounding whitespace.
    """
    if not isinstance(value, str):
        raise CanonicalTextError("text_type", name)
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = unicodedata.normalize("NFC", normalized)
    if "\x00" in normalized:
        raise CanonicalTextError("nul_character", name)
    if any(0xD800 <= ord(char) <= 0xDFFF for char in normalized):
        raise CanonicalTextError("surrogate", name)
    if any(char in _CONTROL_REJECTED or ord(char) == 0x7F for char in normalized):
        raise CanonicalTextError("invalid_control", name)
    # Credential-shape redaction lives in the single authoritative
    # implementation (recovery.redaction); import lazily to avoid a module
    # cycle between the control plan and the recovery package.
    from ..recovery.redaction import redact_text

    redacted_value = redact_text(normalized)
    value_bytes = redacted_value.encode("utf-8")
    if len(value_bytes) > max_utf8_bytes:
        raise CanonicalTextError("text_too_large", name)
    redaction_count = redacted_value.count(REDACTED) - normalized.count(REDACTED)
    return CanonicalText(
        value=redacted_value,
        utf8_bytes=len(value_bytes),
        digest=hashlib.sha256(value_bytes).hexdigest(),
        redaction_policy_version=REDACTION_POLICY_VERSION,
        redaction_count=max(0, redaction_count),
    )


# ---------------------------------------------------------------------------
# runtime ingress policy (exact 11 keys, section 6.2 table)
# ---------------------------------------------------------------------------

INGRESS_DEFAULTS: dict[str, int] = {
    "event_payload_max_utf8_bytes": 4_194_304,
    "event_payload_max_depth": 32,
    "event_payload_max_nodes": 100_000,
    "event_payload_max_string_utf8_bytes": 2_097_152,
    "event_payload_max_object_members": 20_000,
    "event_payload_max_array_items": 20_000,
    "event_payload_max_key_utf8_bytes": 256,
    "user_input_max_utf8_bytes": 65_536,
    "resume_interrupt_max_utf8_bytes": 16_384,
    "terminal_text_max_utf8_bytes": 65_536,
    "instruction_max_utf8_bytes": 131_072,
}

INGRESS_BOUNDS: dict[str, tuple[int, int]] = {
    "event_payload_max_utf8_bytes": (1_024, 4_194_304),
    "event_payload_max_depth": (4, 32),
    "event_payload_max_nodes": (64, 100_000),
    "event_payload_max_string_utf8_bytes": (256, 2_097_152),
    "event_payload_max_object_members": (16, 20_000),
    "event_payload_max_array_items": (16, 20_000),
    "event_payload_max_key_utf8_bytes": (32, 256),
    "user_input_max_utf8_bytes": (1_024, 65_536),
    "resume_interrupt_max_utf8_bytes": (256, 16_384),
    "terminal_text_max_utf8_bytes": (256, 65_536),
    "instruction_max_utf8_bytes": (1_024, 131_072),
}

INGRESS_KEYS: tuple[str, ...] = tuple(sorted(INGRESS_DEFAULTS))


def validate_runtime_ingress(value: Any) -> dict[str, int]:
    """Validate an exact-key durable-limits ingress policy.

    None means defaults are used; when an object is supplied it must
    contain all eleven keys — partial implicit merging of policies is
    forbidden.  Returns the normalized immutable dict.
    """
    if value is None:
        return dict(INGRESS_DEFAULTS)
    if not isinstance(value, Mapping):
        raise ValueError("durable_limits must be a mapping or None")
    if set(value) != set(INGRESS_DEFAULTS):
        missing = set(INGRESS_DEFAULTS) - set(value)
        extra = set(value) - set(INGRESS_DEFAULTS)
        raise ValueError(
            "durable_limits must contain exactly the eleven documented keys; "
            "missing=" + ",".join(sorted(missing)) + " extra=" + ",".join(sorted(extra))
        )
    normalized: dict[str, int] = {}
    for key in INGRESS_DEFAULTS:
        minimum, maximum = INGRESS_BOUNDS[key]
        item = value[key]
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError("durable_limits." + key + " must be an integer")
        if not minimum <= item <= maximum:
            raise ValueError(
                "durable_limits." + key + " must be within "
                + str(minimum) + ".." + str(maximum)
            )
        normalized[key] = item
    return normalized
