"""Deterministic classification of environment failures (issue #7 A1).

A failing test run is not automatically a code failure.  When the image is
missing a dependency the process still starts and exits non-zero, so the
plain outcome mapping says ``completed + failed`` and the model spends its
remaining rounds "fixing" code that was never wrong.

Classify LOCAL captured output against a closed pattern set. These are
diagnostic hints, not proof: test output is still untrusted. Callers must
separately authorize any identifier release and may not use this to install.
"""

from __future__ import annotations

import re

# Signals are a closed set: an unrecognised failure stays unrecognised.
MISSING_DEPENDENCY = "missing_dependency"
MISSING_EXECUTABLE = "missing_executable"
INDEX_UNREACHABLE = "index_unreachable"

MAX_NAMES = 8

# A package / module name has to survive the same shape the preparation
# decision surface accepts, otherwise it is not an actionable identifier.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_MODULE_MISSING = re.compile(
    r"^[ \t]*(?:E[ \t]+)?ModuleNotFoundError: No module named "
    r"'([^'\r\n]{1,128})'[ \t]*\r?$",
    re.MULTILINE,
)
_DIST_MISSING = re.compile(
    r"^[ \t]*(?:ERROR: )?(?:No matching distribution found for|"
    r"Could not find a version that satisfies the requirement) "
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?=[ \t\r\n<>=!~;]|$)",
    re.MULTILINE,
)
_INDEX_UNREACHABLE = re.compile(
    r"^(?:(?:ERROR|WARNING): )?Could not fetch URL [^\r\n]+:"
    r" (?:There was a problem confirming the ssl certificate|"
    r"Connection refused|Network is unreachable)",
    re.MULTILINE,
)
# Windows / POSIX "the executable is not on PATH".
_COMMAND_NOT_FOUND = re.compile(
    r"(?:^|[\s'\"`])([A-Za-z0-9._+-]{1,64}): command not found", re.MULTILINE
)
_COMMAND_NOT_RECOGNIZED = re.compile(
    r"'([A-Za-z0-9._+-]{1,64})' is not recognized as an internal or external command"
)
# Counts are runner-reported evidence, not an independent test inventory.
_COLLECTED_PYTEST = re.compile(r"^collected ([0-9]{1,9}) items?\b", re.MULTILINE)
_RAN_UNITTEST = re.compile(r"^Ran ([0-9]{1,9}) tests? in ", re.MULTILINE)


def parse_test_count(text: str | None) -> int | None:
    """Return the number of tests a runner reported collecting/running.

    ``None`` means the output carried no recognisable count - callers must
    not infer one.
    """

    if not text:
        return None
    # A larger, conflicting line must not mask a smaller reported count.
    return min(
        (
            int(match.group(1))
            for pattern in (_COLLECTED_PYTEST, _RAN_UNITTEST)
            for match in pattern.finditer(text)
        ),
        default=None,
    )


def evidence_isolation(backend: str | None) -> str:
    """Issue #7 D: which boundary produced a test result.

    ``none`` means the process ran with the invoking account's full rights;
    ``unknown`` must never be read as container-isolated.
    """

    if backend == "docker":
        return "container"
    if backend == "host":
        return "none"
    return "unknown"


def _dedupe(names: list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for name in names:
        if not _NAME.fullmatch(name):
            continue
        seen.setdefault(name, None)
        if len(seen) >= MAX_NAMES:
            break
    return tuple(seen)


def _top_level(name: str) -> str | None:
    """The installable/distribution candidate for a dotted module path.

    ``requests`` stays ``requests``; a dotted name is almost always a module
    inside the candidate tree (a code fault), so it is not reported as a
    missing dependency.  The preparation scope still has the final say.
    """

    if "." in name:
        return None
    return name


def classify_environment_failure(
    stdout: str | None,
    stderr: str | None,
) -> tuple[str | None, tuple[str, ...]]:
    """Return ``(signal, names)`` for a local captured output.

    ``signal`` is one of the module constants or ``None``.  ``names`` is a
    bounded, de-duplicated tuple of actionable identifiers (may be empty for
    ``index_unreachable`` when the message carries no matching name).
    """

    text = (stdout or "") + "\n" + (stderr or "")
    if not text.strip():
        return None, ()

    modules: list[str] = []
    for match in _MODULE_MISSING.finditer(text):
        top = _top_level(match.group(1))
        if top is not None:
            modules.append(top)
    names = _dedupe(modules)
    if names:
        return MISSING_DEPENDENCY, names

    if _INDEX_UNREACHABLE.search(text):
        return INDEX_UNREACHABLE, ()

    distributions = [match.group(1) for match in _DIST_MISSING.finditer(text)]
    if distributions:
        return MISSING_DEPENDENCY, _dedupe(distributions)

    executables = [match.group(1) for match in _COMMAND_NOT_FOUND.finditer(text)]
    executables.extend(
        match.group(1) for match in _COMMAND_NOT_RECOGNIZED.finditer(text)
    )
    if executables:
        return MISSING_EXECUTABLE, _dedupe(executables)

    return None, ()
