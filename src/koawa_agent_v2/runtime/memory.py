"""D23-B unified memory configuration and hard limits.

``MemoryConfig`` is the single typed envelope for every memory-plane knob in
the D23 design (docs/day-23-memory-layer-upgrade.md §8): cross-turn
conclusions, failed-turn echo, in-run group compaction, recall scan, and
journal reminders.  It is a pure data structure — no I/O and no import of
``runtime.config`` (which would create an import cycle), so the module is fully
self-contained.

Every field is validated in ``__post_init__``:

* strict types: a ``bool`` must be a ``bool`` (never an ``int``), an ``int``
  must be an ``int`` (never a ``bool``);
* positive bounds: every ``int`` is >= 1; the chars-class fields cap at
  2_000_000 and the remaining ``int`` fields cap at 1_000_000;
* the §8 relationship invariants: ``target < soft < hard``,
  ``reserve < hard - target``, and ``conclusion/summary <= hard``.

All validation failures raise ``MemoryConfigError`` carrying a stable,
content-free lowercase snake_case code — the offending value is never echoed.
``from_mapping`` additionally fails closed on unknown keys, and
``to_document`` renders the normalized document used to bind the config
identity into the execution seed (consumed later by I5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

_MEMORY_ERROR = re.compile(r"[a-z][a-z0-9_]{0,127}")

# Field groups drive the strict type/range validation in __post_init__.
_BOOL_FIELDS = (
    "conclusions_enabled",
    "conclusion_model_summary",
    "in_run_compaction_enabled",
    "journal_inject_latest",
)
# Chars-class fields have a 2_000_000 ceiling; other int fields cap at
# 1_000_000 (D23 §8 "统一配置与硬上限").
_CHAR_FIELDS = (
    "conclusion_max_chars",
    "request_context_soft_chars",
    "request_context_hard_chars",
    "request_context_reserve_chars",
    "compaction_target_chars",
    "compaction_summary_max_chars",
)
_INT_FIELDS = (
    "conclusion_recent_limit",
    "failed_echo_max_turns",
    "in_run_keep_groups",
    "max_compaction_epochs_per_run",
    "max_compaction_source_groups",
    "recall_scan_max_turns",
    "journal_remind_turns",
    "journal_remind_changed_files",
)

# Canonical field order (matches the §8 declaration) and membership set.
_FIELD_ORDER = (
    "conclusions_enabled",
    "conclusion_max_chars",
    "conclusion_recent_limit",
    "conclusion_model_summary",
    "failed_echo_max_turns",
    "in_run_compaction_enabled",
    "in_run_keep_groups",
    "request_context_soft_chars",
    "request_context_hard_chars",
    "request_context_reserve_chars",
    "compaction_target_chars",
    "max_compaction_epochs_per_run",
    "max_compaction_source_groups",
    "compaction_summary_max_chars",
    "recall_scan_max_turns",
    "journal_remind_turns",
    "journal_remind_changed_files",
    "journal_inject_latest",
)
_FIELDS = frozenset(_FIELD_ORDER)

_CHAR_FIELD_MAX = 2_000_000
_INT_FIELD_MAX = 1_000_000


class MemoryConfigError(RuntimeError):
    """Stable, content-free memory-configuration failure safe to print.

    ``code`` is a lowercase snake_case identifier (``[a-z][a-z0-9_]{0,127}``)
    that never contains the offending value, so it may be shown to an
    operator or persisted in a trace without leaking configuration.
    """

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _MEMORY_ERROR.fullmatch(code):
            raise ValueError("invalid memory config error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class MemoryConfig:
    conclusions_enabled: bool = True
    conclusion_max_chars: int = 512
    conclusion_recent_limit: int = 8
    conclusion_model_summary: bool = False
    failed_echo_max_turns: int = 3
    in_run_compaction_enabled: bool = True
    in_run_keep_groups: int = 4
    request_context_soft_chars: int = 48_000
    request_context_hard_chars: int = 64_000
    request_context_reserve_chars: int = 8_000
    compaction_target_chars: int = 36_000
    max_compaction_epochs_per_run: int = 16
    max_compaction_source_groups: int = 32
    compaction_summary_max_chars: int = 2_048
    recall_scan_max_turns: int = 256
    journal_remind_turns: int = 10
    journal_remind_changed_files: int = 20
    journal_inject_latest: bool = False

    def __post_init__(self) -> None:
        for name in _BOOL_FIELDS:
            if not isinstance(getattr(self, name), bool):
                raise MemoryConfigError("invalid_memory_value")
        for name in _CHAR_FIELDS:
            _validate_int(getattr(self, name), maximum=_CHAR_FIELD_MAX)
        for name in _INT_FIELDS:
            _validate_int(getattr(self, name), maximum=_INT_FIELD_MAX)
        # §8 relationship invariants (single stable code for all of them).
        if not (
            self.compaction_target_chars
            < self.request_context_soft_chars
            < self.request_context_hard_chars
        ):
            raise MemoryConfigError("invalid_memory_relation")
        if not (
            self.request_context_reserve_chars
            < self.request_context_hard_chars - self.compaction_target_chars
        ):
            raise MemoryConfigError("invalid_memory_relation")
        if self.conclusion_max_chars > self.request_context_hard_chars:
            raise MemoryConfigError("invalid_memory_relation")
        if self.compaction_summary_max_chars > self.request_context_hard_chars:
            raise MemoryConfigError("invalid_memory_relation")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "MemoryConfig":
        """Build from a JSON-decoded mapping; missing keys fall back to defaults.

        ``None`` or an empty mapping yields the all-defaults config.  Unknown
        keys fail closed (``memory_unknown_field``) rather than being silently
        ignored.  Only keys explicitly present in ``raw`` are consumed.
        """
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise MemoryConfigError("invalid_memory_value")
        if not _FIELDS.issuperset(raw):
            raise MemoryConfigError("memory_unknown_field")
        return cls(**_explicit_kwargs(raw))

    def to_document(self) -> dict[str, Any]:
        """Normalized, JSON-serializable document (all keys, correct types)."""
        return {name: getattr(self, name) for name in _FIELD_ORDER}

    def __repr__(self) -> str:
        """Concise: only fields that differ from the defaults are shown."""
        changed = [
            f"{name}={getattr(self, name)!r}"
            for name in _FIELD_ORDER
            if getattr(self, name) != _DEFAULTS[name]
        ]
        if not changed:
            return "MemoryConfig(<default>)"
        return f"MemoryConfig({', '.join(changed)})"


def _validate_int(value: Any, *, maximum: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > maximum
    ):
        raise MemoryConfigError("invalid_memory_value")


def _explicit_kwargs(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {name: raw.get(name, _DEFAULTS[name]) for name in _FIELD_ORDER}


# Canonical defaults, derived from the class itself so the two can never drift.
_DEFAULTS = {name: getattr(MemoryConfig(), name) for name in _FIELD_ORDER}
