"""C1 (WP-C): controlled dependency preparation.

Design (2026-09-22): the candidate dependency list is derived from project
config, lock files and test entry points; preparation happens in an isolated
environment WITHOUT sensitive inputs or host credentials; anything outside
the authorised range needs its own approval and produces a fixed
requires-authorization status (never a silent fallback to whole-repo mounts,
host execution or open network).

This module is the trusted decision surface: it classifies each requested
dependency against an authorised scope and returns a plan the caller may
execute.  It performs no installation itself - the executor is a separate,
reviewable step - so the decision is testable without network.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PREP_READY = "ready"
PREP_REQUIRES_AUTHORIZATION = "requires_authorization"
PREP_REFUSED = "refused"

# Package name shape we accept for a preparation request.
import re as _re

_NAME = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DependencyPreparationError(RuntimeError):
    """Content-free preparation failure with a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DependencyScope:
    """The maintainer-authorised preparation scope.

    ``allowed_packages`` is the exact set that may be installed automatically
    (or the prefix list when a package name starts with an entry ending in
    ``*``).  ``allow_install`` gates the whole capability: with it false, a
    missing dependency is a fixed "requires authorization" state.
    """

    allowed_packages: tuple[str, ...] = ()
    allow_install: bool = False
    max_packages: int = 8
    index_urls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_packages, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.allowed_packages
        ):
            raise DependencyPreparationError("invalid_dependency_scope")
        if not isinstance(self.allow_install, bool):
            raise DependencyPreparationError("invalid_dependency_scope")
        if (
            not isinstance(self.max_packages, int)
            or isinstance(self.max_packages, bool)
            or not 1 <= self.max_packages <= 64
        ):
            raise DependencyPreparationError("invalid_dependency_scope")
        if not isinstance(self.index_urls, tuple) or any(
            not isinstance(item, str) or not item.startswith("https://")
            for item in self.index_urls
        ):
            raise DependencyPreparationError("invalid_dependency_index")


@dataclass(frozen=True, slots=True)
class PreparationDecision:
    status: str
    install: tuple[str, ...] = ()
    requires_authorization: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    reason: str | None = None


def _allowed(name: str, scope: DependencyScope) -> bool:
    lowered = name.lower()
    for entry in scope.allowed_packages:
        needle = entry.lower()
        if needle.endswith("*"):
            if lowered.startswith(needle[:-1]):
                return True
        elif lowered == needle:
            return True
    return False


def plan_preparation(
    requested: tuple[str, ...],
    scope: DependencyScope,
    *,
    missing: tuple[str, ...] | None = None,
) -> PreparationDecision:
    """Classify a preparation request; never installs anything.

    - malformed name              -> refused (invalid_package_name);
    - scope.allow_install false   -> requires_authorization (install_disabled);
    - name outside the scope      -> requires_authorization (out_of_scope);
    - more than max_packages      -> requires_authorization (budget_exceeded);
    - otherwise                   -> ready with the exact install list.

    ``missing`` (the discovered-but-absent set) narrows the install list; a
    request whose missing set is empty is ready with nothing to install.
    """

    if not isinstance(requested, tuple) or any(
        not isinstance(item, str) for item in requested
    ):
        raise DependencyPreparationError("invalid_dependency_request")
    for name in requested:
        if not _NAME.fullmatch(name):
            return PreparationDecision(
                status=PREP_REFUSED,
                refused=(name,),
                reason="invalid_package_name",
            )
    if not scope.allow_install:
        return PreparationDecision(
            status=PREP_REQUIRES_AUTHORIZATION,
            requires_authorization=tuple(requested),
            reason="install_disabled",
        )
    targets = requested if missing is None else tuple(
        name for name in requested if name in missing
    )
    out_of_scope = tuple(name for name in targets if not _allowed(name, scope))
    if out_of_scope:
        return PreparationDecision(
            status=PREP_REQUIRES_AUTHORIZATION,
            requires_authorization=out_of_scope,
            reason="out_of_scope",
        )
    if len({name.lower() for name in targets}) > scope.max_packages:
        return PreparationDecision(
            status=PREP_REQUIRES_AUTHORIZATION,
            requires_authorization=targets,
            reason="budget_exceeded",
        )
    return PreparationDecision(status=PREP_READY, install=tuple(targets))
