""""I1 shared minimal subprocess environment builder (S4-A).

One audited environment contract for every spawned child (D5 runner, MCP
fixture, future sandbox helpers). Never inherits the parent environment:
platform variables are resolved from trusted system directories, secrets
and injection vectors are rejected, and only caller-allowlisted explicit
names pass. Output is immutable; digest is for audit only.

Allowlist note: PYTHONPATH is NOT in the deny set because the D5 command
runner legitimately allowlists it for admin-configured test commands; MCP
configs simply do not list it. Injection vectors that are never useful
in a child (PYTHONSTARTUP, LD_PRELOAD, ...) are hard-denied for everyone.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SECRET_NAME = re.compile(
    r"(?i)secret|token|password|api[-_]?key|authorization|credential|signature|private[-_]?key"
)
_INJECTION_NAMES = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
    "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONPLUGLIBDIR",
    "BASH_ENV", "ENV", "PERL5LIB", "RUBYOPT", "NODE_OPTIONS",
})
# Security-side deny sets are compared casefolded on EVERY platform (env var
# names are case-insensitive on Windows, and over-denying on POSIX only blocks
# hostile casing variants).
_INJECTION_NAMES_CASEFOLD = frozenset(
    {name.casefold() for name in _INJECTION_NAMES}
)
_ALWAYS_OWNED = frozenset({
    "LANG", "LC_ALL",
    "PYTHONHASHSEED", "PYTHONUTF8", "PYTHONIOENCODING",
    "PYTHONDONTWRITEBYTECODE",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
})
_ALWAYS_OWNED_CASEFOLD = frozenset(
    {name.casefold() for name in _ALWAYS_OWNED}
)
_FIXED_PYTHON_ENV = {
    "PYTHONHASHSEED": "0",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONDONTWRITEBYTECODE": "1",
}


class SubprocessEnvError(RuntimeError):
    """Stable content-free environment contract failure."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise ValueError("invalid subprocess env error code")
        self.code = code
        super().__init__(code)


def _windows_system_root() -> str:
    """Resolve SystemRoot from the OS, never from the parent environment."""
    if os.name != "nt":
        raise SubprocessEnvError("not_windows")
    try:
        kernel32 = ctypes.windll.kernel32
        buffer = ctypes.create_unicode_buffer(512)
        length = kernel32.GetSystemWindowsDirectoryW(buffer, 512)
    except Exception:
        raise SubprocessEnvError("system_root_unavailable") from None
    if not 1 <= length < 512:
        raise SubprocessEnvError("system_root_unavailable")
    return str(Path(buffer.value).resolve(strict=False))


def environment_identities(
    explicit: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    """I6 §8.3/§8.4: digest of the configured env (sorted name, sha256(value)).

    Only names and value digests are returned; values never leave this
    module as part of a launch/config digest.  Windows keys are casefolded
    before sorting so equal spellings cannot produce two parallel entries.
    """
    if not isinstance(explicit, Mapping):
        raise TypeError("explicit must be a mapping")
    entries: list[tuple[str, str]] = []
    for name, value in explicit.items():
        if not isinstance(name, str) or not name:
            raise SubprocessEnvError("invalid_environment_name")
        if not isinstance(value, str):
            raise SubprocessEnvError("invalid_environment_value")
        folded = name.casefold() if os.name == "nt" else name
        value_digest = hashlib.sha256(value.encode("utf-8", "strict")).hexdigest()
        entries.append((folded, value_digest))
    entries.sort(key=lambda pair: (pair[0], pair[1]))
    return tuple(entries)


def environment_identity_digest(
    explicit: Mapping[str, str],
) -> str:
    """Stable digest over the typed (name, value_digest) identities."""
    identities = environment_identities(explicit)
    canonical = json.dumps(
        list(identities),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8", "strict")).hexdigest()


def build_minimal_environment(
    explicit: Mapping[str, str],
    *,
    allowed_names: frozenset[str],
    private_temp: Path,
) -> Mapping[str, str]:
    """Build the immutable minimal environment for one child process.

    Raises SubprocessEnvError with stable codes on any disallowed entry.
    """
    if not isinstance(explicit, Mapping):
        raise TypeError("explicit must be a mapping")
    if not isinstance(allowed_names, frozenset) or any(
        not isinstance(name, str) or not name for name in allowed_names
    ):
        raise TypeError("allowed_names must be a frozenset of non-empty str")
    if not isinstance(private_temp, Path) or not private_temp.is_absolute():
        raise TypeError("private_temp must be an absolute Path")

    environment: dict[str, str] = {}
    if os.name == "nt":
        system_root = _windows_system_root()
        private = str(private_temp)
        environment.update({
            "SystemRoot": system_root,
            "WINDIR": system_root,
            "COMSPEC": str(Path(system_root) / "System32" / "cmd.exe"),
            "TEMP": private,
            "TMP": private,
        })
    else:
        environment.update({
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": str(private_temp),
        })
    environment.update(_FIXED_PYTHON_ENV)

    allow_keys = frozenset(
        {name.casefold() if os.name == "nt" else name for name in allowed_names}
    )
    seen: set[str] = set()
    for name, value in explicit.items():
        if not isinstance(name, str) or not name:
            raise SubprocessEnvError("invalid_environment_name")
        key = name.casefold() if os.name == "nt" else name
        canonical = key.casefold()
        if canonical in _INJECTION_NAMES_CASEFOLD:
            raise SubprocessEnvError("injection_variable_forbidden")
        if _SECRET_NAME.search(name):
            raise SubprocessEnvError("secret_variable_forbidden")
        if key not in allow_keys or key in seen:
            raise SubprocessEnvError("environment_not_allowlisted")
        if canonical in _ALWAYS_OWNED_CASEFOLD:
            raise SubprocessEnvError("reserved_variable_overridden")
        if not isinstance(value, str) or "\x00" in value or len(value) > 16_384:
            raise SubprocessEnvError("invalid_environment_value")
        seen.add(key)
        environment[name] = value
    return MappingProxyType(environment)
