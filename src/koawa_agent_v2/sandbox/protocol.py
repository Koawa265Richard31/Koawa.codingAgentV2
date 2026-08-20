"""D8 immutable Docker-sandbox protocol and persisted allocation identity.

This module contains data only.  Docker clients, processes, callbacks, and host
environment values deliberately stay in the runtime layer and are never part of
an allocation document.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Mapping, TypeAlias
from uuid import UUID


MANAGED_LABEL = "io.koawa.v2.managed"
ALLOCATION_ID_LABEL = "io.koawa.v2.allocation"
OWNER_EXECUTION_ID_LABEL = "io.koawa.v2.owner"
OWNER_NONCE_LABEL = "io.koawa.v2.owner-nonce"
IMAGE_ID_LABEL = "io.koawa.v2.image-id"
MOUNT_DIGEST_LABEL = "io.koawa.v2.mount-digest"
PROFILE_DIGEST_LABEL = "io.koawa.v2.profile-digest"
COMMAND_DIGEST_LABEL = "io.koawa.v2.command-digest"
MANAGED_LABEL_VALUE = "true"

MANAGED_LABEL_KEYS = (
    MANAGED_LABEL,
    ALLOCATION_ID_LABEL,
    OWNER_EXECUTION_ID_LABEL,
    OWNER_NONCE_LABEL,
    IMAGE_ID_LABEL,
    MOUNT_DIGEST_LABEL,
    PROFILE_DIGEST_LABEL,
    COMMAND_DIGEST_LABEL,
)

SAFE_CONTAINER_ENV_NAMES = frozenset(
    {
        "LANG",
        "LC_ALL",
        "PYTHONHASHSEED",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "TZ",
    }
)

_STABLE_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_PROFILE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_OUTCOME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SECRET_LIKE_ARGUMENT = re.compile(
    r"(?i)(?:bearer\s+|password=|token=|api[_-]?key=|secret=|sk-[a-z0-9])"
)

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class SandboxError(RuntimeError):
    """Stable, content-free sandbox failure safe to persist or return."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _STABLE_CODE.fullmatch(code):
            raise ValueError("invalid sandbox error code")
        self.code = code
        super().__init__(code)


class AllocationState(StrEnum):
    INTENDED = "intended"
    BOUND = "bound"
    STARTED = "started"
    FINISHED = "finished"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Trusted host policy for one container; values are never model supplied."""

    cpus: float = 1.0
    memory_bytes: int = 256 * 1024 * 1024
    pids_limit: int = 64
    tmpfs_bytes: int = 64 * 1024 * 1024
    cleanup_grace_seconds: float = 15.0
    max_workspace_entries: int = 100_000

    def __post_init__(self) -> None:
        cpus = _finite_number(self.cpus, "invalid_sandbox_limits")
        grace = _finite_number(
            self.cleanup_grace_seconds, "invalid_sandbox_limits"
        )
        if cpus <= 0 or cpus > 64 or grace <= 0 or grace > 300:
            raise SandboxError("invalid_sandbox_limits")
        _bounded_int(
            self.memory_bytes,
            minimum=32 * 1024 * 1024,
            maximum=64 * 1024 * 1024 * 1024,
            code="invalid_sandbox_limits",
        )
        _bounded_int(
            self.pids_limit,
            minimum=1,
            maximum=4_096,
            code="invalid_sandbox_limits",
        )
        _bounded_int(
            self.tmpfs_bytes,
            minimum=1 * 1024 * 1024,
            maximum=16 * 1024 * 1024 * 1024,
            code="invalid_sandbox_limits",
        )
        _bounded_int(
            self.max_workspace_entries,
            minimum=1,
            maximum=1_000_000,
            code="invalid_sandbox_limits",
        )
        object.__setattr__(self, "cpus", cpus)
        object.__setattr__(self, "cleanup_grace_seconds", grace)

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "cpus": self.cpus,
            "memory_bytes": self.memory_bytes,
            "pids_limit": self.pids_limit,
            "tmpfs_bytes": self.tmpfs_bytes,
            "cleanup_grace_seconds": self.cleanup_grace_seconds,
            "max_workspace_entries": self.max_workspace_entries,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.to_document())


@dataclass(frozen=True, slots=True, repr=False)
class SandboxCommandProfile:
    """Immutable command selected by ID; argv and environment are host-defined."""

    profile_id: str
    argv: tuple[str, ...]
    working_directory: str = "/workspace"
    timeout_seconds: float = 60.0
    max_stdout_bytes: int = 256_000
    max_stderr_bytes: int = 256_000
    environment: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not _PROFILE_ID.fullmatch(
            self.profile_id
        ):
            raise SandboxError("invalid_sandbox_profile")
        if not isinstance(self.argv, tuple) or not 1 <= len(self.argv) <= 128:
            raise SandboxError("invalid_sandbox_command")
        total_bytes = 0
        for index, value in enumerate(self.argv):
            _command_text(value, allow_empty=index != 0)
            total_bytes += len(value.encode("utf-8", "strict"))
            if _SECRET_LIKE_ARGUMENT.search(value) is not None:
                raise SandboxError("secret_like_command_argument")
        if total_bytes > 256 * 1024:
            raise SandboxError("invalid_sandbox_command")
        executable = PurePosixPath(self.argv[0])
        if (
            not executable.is_absolute()
            or self.argv[0] == "/"
            or self.argv[0].startswith("//")
            or str(executable) != self.argv[0]
            or ".." in executable.parts
        ):
            raise SandboxError("command_executable_must_be_posix_absolute")
        _workspace_directory(self.working_directory)
        timeout = _finite_number(
            self.timeout_seconds, "invalid_sandbox_profile"
        )
        if timeout <= 0 or timeout > 3_600:
            raise SandboxError("invalid_sandbox_profile")
        for value in (self.max_stdout_bytes, self.max_stderr_bytes):
            _bounded_int(
                value,
                minimum=1,
                maximum=16 * 1024 * 1024,
                code="invalid_sandbox_profile",
            )
        if not isinstance(self.environment, tuple):
            raise SandboxError("invalid_sandbox_environment")
        seen: set[str] = set()
        normalized_environment: list[tuple[str, str]] = []
        for item in self.environment:
            if not isinstance(item, tuple) or len(item) != 2:
                raise SandboxError("invalid_sandbox_environment")
            name, value = item
            if name not in SAFE_CONTAINER_ENV_NAMES or name in seen:
                raise SandboxError("unsafe_sandbox_environment")
            _environment_text(value)
            if _SECRET_LIKE_ARGUMENT.search(value) is not None:
                raise SandboxError("secret_like_environment_value")
            seen.add(name)
            normalized_environment.append((name, value))
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(
            self, "environment", tuple(sorted(normalized_environment))
        )

    @property
    def cwd(self) -> str:
        return self.working_directory

    def command_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "argv": list(self.argv),
            "working_directory": self.working_directory,
            "environment": [
                {"name": name, "value": value}
                for name, value in self.environment
            ],
        }

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "profile_id": self.profile_id,
            "command": self.command_document(),
            "timeout_seconds": self.timeout_seconds,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.to_document())

    @property
    def command_digest(self) -> str:
        return canonical_sha256(self.command_document())

    @property
    def profile_digest(self) -> str:
        return canonical_sha256(self.to_document())

    def __repr__(self) -> str:
        return (
            f"SandboxCommandProfile(profile_id={self.profile_id!r}, "
            f"argv_count={len(self.argv)}, environment_count={len(self.environment)}, "
            f"timeout_seconds={self.timeout_seconds})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class DockerDoctorReport:
    ready: bool
    client_version: str | None = None
    server_version: str | None = None
    server_os: str | None = None
    server_architecture: str | None = None
    image_id: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ready, bool):
            raise TypeError("ready must be bool")
        for value in (
            self.client_version,
            self.server_version,
            self.server_os,
            self.server_architecture,
        ):
            if value is not None:
                _bounded_text(value, 256, "invalid_doctor_report")
        if self.image_id is not None:
            validate_immutable_image_id(self.image_id)
        if self.error_code is not None and (
            not isinstance(self.error_code, str)
            or not _STABLE_CODE.fullmatch(self.error_code)
        ):
            raise SandboxError("invalid_doctor_report")
        if self.ready:
            if (
                any(
                    value is None
                    for value in (
                        self.client_version,
                        self.server_version,
                        self.server_os,
                        self.server_architecture,
                        self.image_id,
                    )
                )
                or self.error_code is not None
            ):
                raise SandboxError("invalid_doctor_report")
        elif self.error_code is None:
            raise SandboxError("invalid_doctor_report")

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "ready": self.ready,
            "client_version": self.client_version,
            "server_version": self.server_version,
            "server_os": self.server_os,
            "server_architecture": self.server_architecture,
            "image_id": self.image_id,
            "error_code": self.error_code,
        }

    def __repr__(self) -> str:
        return (
            f"DockerDoctorReport(ready={self.ready}, "
            f"server_version={self.server_version!r}, error_code={self.error_code!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SandboxAllocation:
    """JSON-projectable allocation aggregate; no live Docker object is retained."""

    allocation_id: UUID
    owner_execution_id: UUID
    owner_nonce: str
    image_id: str
    mount_digest: str
    profile_digest: str
    command_digest: str
    deadline_at: datetime
    state: AllocationState = AllocationState.INTENDED
    version: int = 0
    container_id: str | None = None
    outcome: str | None = None
    exit_code: int | None = None
    oom_killed: bool | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        _nonzero_uuid(self.allocation_id, "allocation_id")
        _nonzero_uuid(self.owner_execution_id, "owner_execution_id")
        if not isinstance(self.owner_nonce, str) or not _SHA256.fullmatch(
            self.owner_nonce
        ):
            raise SandboxError("invalid_owner_nonce")
        validate_immutable_image_id(self.image_id)
        for value in (
            self.mount_digest,
            self.profile_digest,
            self.command_digest,
        ):
            validate_sha256_digest(value)
        deadline = _aware_utc(self.deadline_at, "deadline_at")
        if not isinstance(self.state, AllocationState):
            raise TypeError("state must be AllocationState")
        _bounded_int(
            self.version,
            minimum=0,
            maximum=2**63 - 1,
            code="invalid_allocation_version",
        )
        if self.container_id is not None and (
            not isinstance(self.container_id, str)
            or not _CONTAINER_ID.fullmatch(self.container_id)
        ):
            raise SandboxError("invalid_container_id")
        if self.state in (AllocationState.BOUND, AllocationState.STARTED) and (
            self.container_id is None
        ):
            raise SandboxError("allocation_container_not_bound")
        if self.state is AllocationState.INTENDED and self.container_id is not None:
            raise SandboxError("allocation_state_conflict")
        if self.outcome is not None and (
            not isinstance(self.outcome, str) or not _OUTCOME.fullmatch(self.outcome)
        ):
            raise SandboxError("invalid_allocation_outcome")
        if self.state in (
            AllocationState.INTENDED,
            AllocationState.BOUND,
            AllocationState.STARTED,
        ) and any(
            value is not None
            for value in (
                self.outcome,
                self.exit_code,
                self.oom_killed,
                self.finished_at,
            )
        ):
            raise SandboxError("allocation_state_conflict")
        if self.state in (AllocationState.FINISHED, AllocationState.RELEASED) and (
            self.outcome is None
        ):
            raise SandboxError("allocation_outcome_missing")
        if self.exit_code is not None:
            _bounded_int(
                self.exit_code,
                minimum=-(2**31),
                maximum=2**31 - 1,
                code="invalid_container_exit_code",
            )
        if self.oom_killed is not None and not isinstance(self.oom_killed, bool):
            raise TypeError("oom_killed must be bool or None")
        finished = (
            None
            if self.finished_at is None
            else _aware_utc(self.finished_at, "finished_at")
        )
        object.__setattr__(self, "deadline_at", deadline)
        object.__setattr__(self, "finished_at", finished)

    @property
    def expected_labels(self) -> tuple[tuple[str, str], ...]:
        return (
            (MANAGED_LABEL, MANAGED_LABEL_VALUE),
            (ALLOCATION_ID_LABEL, str(self.allocation_id)),
            (OWNER_EXECUTION_ID_LABEL, str(self.owner_execution_id)),
            (OWNER_NONCE_LABEL, self.owner_nonce),
            (IMAGE_ID_LABEL, self.image_id),
            (MOUNT_DIGEST_LABEL, self.mount_digest),
            (PROFILE_DIGEST_LABEL, self.profile_digest),
            (COMMAND_DIGEST_LABEL, self.command_digest),
        )

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "allocation_id": str(self.allocation_id),
            "owner_execution_id": str(self.owner_execution_id),
            "owner_nonce": self.owner_nonce,
            "image_id": self.image_id,
            "mount_digest": self.mount_digest,
            "profile_digest": self.profile_digest,
            "command_digest": self.command_digest,
            "deadline_at": _timestamp(self.deadline_at),
            "state": self.state.value,
            "version": self.version,
            "container_id": self.container_id,
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "oom_killed": self.oom_killed,
            "finished_at": (
                None if self.finished_at is None else _timestamp(self.finished_at)
            ),
            "expected_labels": {
                name: value for name, value in self.expected_labels
            },
        }

    def __repr__(self) -> str:
        return (
            f"SandboxAllocation(allocation_id={self.allocation_id}, "
            f"owner_execution_id={self.owner_execution_id}, state={self.state.value!r}, "
            f"version={self.version}, container_bound={self.container_id is not None})"
        )


@dataclass(frozen=True, slots=True)
class ReapReport:
    removed: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    released: tuple[UUID, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, values in (("removed", self.removed), ("refused", self.refused)):
            if not isinstance(values, tuple) or len(set(values)) != len(values):
                raise SandboxError("invalid_reap_report")
            if any(
                not isinstance(value, str) or not _CONTAINER_ID.fullmatch(value)
                for value in values
            ):
                raise SandboxError("invalid_reap_report")
        if set(self.removed).intersection(self.refused):
            raise SandboxError("invalid_reap_report")
        if not isinstance(self.released, tuple) or len(set(self.released)) != len(
            self.released
        ):
            raise SandboxError("invalid_reap_report")
        for value in self.released:
            _nonzero_uuid(value, "released allocation_id")
        if not isinstance(self.errors, tuple) or len(set(self.errors)) != len(
            self.errors
        ):
            raise SandboxError("invalid_reap_report")
        if any(
            not isinstance(value, str) or not _STABLE_CODE.fullmatch(value)
            for value in self.errors
        ):
            raise SandboxError("invalid_reap_report")

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    @property
    def refused_count(self) -> int:
        return len(self.refused)

    @property
    def released_count(self) -> int:
        return len(self.released)

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "removed": list(self.removed),
            "refused": list(self.refused),
            "released": [str(value) for value in self.released],
            "errors": list(self.errors),
        }


def validate_immutable_image_id(value: str) -> str:
    """Accept only a complete lowercase Docker content-addressed image ID."""

    if not isinstance(value, str) or not _IMAGE_ID.fullmatch(value):
        raise SandboxError("immutable_image_id_required")
    return value


def validate_sha256_digest(value: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise SandboxError("invalid_sha256_digest")
    return value


def canonical_sha256(document: Mapping[str, JsonValue]) -> str:
    if not isinstance(document, Mapping):
        raise TypeError("document must be a mapping")
    return hashlib.sha256(_canonical_json(document).encode("utf-8", "strict")).hexdigest()


def _canonical_json(document: Mapping[str, JsonValue]) -> str:
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError):
        raise SandboxError("invalid_json_document") from None


def _command_text(value: object, *, allow_empty: bool) -> None:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise SandboxError("invalid_sandbox_command")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        raise SandboxError("invalid_sandbox_command") from None
    if len(encoded) > 16_384:
        raise SandboxError("invalid_sandbox_command")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise SandboxError("invalid_sandbox_command")


def _environment_text(value: object) -> None:
    if not isinstance(value, str):
        raise SandboxError("invalid_sandbox_environment")
    _bounded_text(value, 16_384, "invalid_sandbox_environment")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise SandboxError("invalid_sandbox_environment")


def _bounded_text(value: object, maximum_bytes: int, code: str) -> None:
    if not isinstance(value, str):
        raise SandboxError(code)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        raise SandboxError(code) from None
    if not encoded or len(encoded) > maximum_bytes:
        raise SandboxError(code)


def _workspace_directory(value: object) -> None:
    if not isinstance(value, str):
        raise SandboxError("invalid_sandbox_working_directory")
    _bounded_text(value, 4_096, "invalid_sandbox_working_directory")
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or str(path) != value
        or ".." in path.parts
        or path.parts[:2] != ("/", "workspace")
    ):
        raise SandboxError("invalid_sandbox_working_directory")


def _finite_number(value: object, code: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SandboxError(code)
    normalized = float(value)
    if not math.isfinite(normalized):
        raise SandboxError(code)
    return normalized


def _bounded_int(value: object, *, minimum: int, maximum: int, code: str) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise SandboxError(code)


def _nonzero_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be UUID")
    if value.int == 0:
        raise SandboxError("nil_allocation_identity")


def _aware_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise SandboxError("naive_allocation_timestamp")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
