"""Trusted Docker control plane, allocation ledger, and exact container reaper.

The model selects only an immutable SandboxCommandProfile.  This module owns the
Docker argv, persists an allocation intent before create, and never places a
Docker client, process handle, or host environment snapshot in durable state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from ..control.event_store import (
    EventMetadata,
    EventStore,
    EventStoreError,
    NewEvent,
    StreamId,
    StreamWrite,
    WrongExpectedVersion,
)
from ..verification.runner import (
    CommandOutcome,
    CommandResult,
    CommandRunnerError,
)
from .protocol import (
    ALLOCATION_ID_LABEL,
    MANAGED_LABEL,
    MANAGED_LABEL_VALUE,
    OWNER_EXECUTION_ID_LABEL,
    AllocationState,
    DockerDoctorReport,
    ReapReport,
    SandboxAllocation,
    SandboxCommandProfile,
    SandboxError,
    SandboxLimits,
    canonical_sha256,
    validate_immutable_image_id,
)


_ALLOCATION_CATEGORY = "sandbox-allocation"
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_STABLE_REASON = re.compile(r"[a-z][a-z0-9_]{0,127}")
_MISSING_CONTAINER = re.compile(
    r"(?i)(?:no such (?:container|object)|not found)"
)
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class SandboxAllocationStore:
    """Append-only allocation aggregate using exact EventStore versions."""

    def __init__(self, event_store: EventStore) -> None:
        if not all(
            hasattr(event_store, name)
            for name in ("append_batch", "read_stream", "read_all")
        ):
            raise TypeError("event_store must implement EventStore")
        self.event_store = event_store

    def intent(
        self,
        *,
        owner_execution_id: UUID,
        image_id: str,
        mount_digest: str,
        profile_digest: str,
        command_digest: str,
        deadline_at: datetime,
        allocation_id: UUID | None = None,
        owner_nonce: str | None = None,
    ) -> SandboxAllocation:
        """Persist identity and expected labels before Docker create is legal."""

        resolved_id = uuid4() if allocation_id is None else allocation_id
        if not isinstance(resolved_id, UUID):
            raise TypeError("allocation_id must be UUID or None")
        existing = self.load(resolved_id)
        if existing is not None:
            expected = (
                owner_execution_id,
                image_id,
                mount_digest,
                profile_digest,
                command_digest,
                _utc(deadline_at),
            )
            actual = (
                existing.owner_execution_id,
                existing.image_id,
                existing.mount_digest,
                existing.profile_digest,
                existing.command_digest,
                existing.deadline_at,
            )
            if actual != expected or (
                owner_nonce is not None and existing.owner_nonce != owner_nonce
            ):
                raise SandboxError("allocation_identity_conflict")
            return existing
        allocation = SandboxAllocation(
            allocation_id=resolved_id,
            owner_execution_id=owner_execution_id,
            owner_nonce=secrets.token_hex(32) if owner_nonce is None else owner_nonce,
            image_id=image_id,
            mount_digest=mount_digest,
            profile_digest=profile_digest,
            command_digest=command_digest,
            deadline_at=deadline_at,
        )
        try:
            self._append(
                allocation,
                expected_version=-1,
                event_type="sandbox.allocation-intended.v1",
                payload=allocation.to_document(),
                actor="sandbox",
            )
        except WrongExpectedVersion:
            concurrent = self.load(resolved_id)
            if concurrent is None:
                raise
            expected = (
                owner_execution_id,
                image_id,
                mount_digest,
                profile_digest,
                command_digest,
                _utc(deadline_at),
            )
            actual = (
                concurrent.owner_execution_id,
                concurrent.image_id,
                concurrent.mount_digest,
                concurrent.profile_digest,
                concurrent.command_digest,
                concurrent.deadline_at,
            )
            if actual != expected or (
                owner_nonce is not None
                and concurrent.owner_nonce != owner_nonce
            ):
                raise SandboxError("allocation_identity_conflict") from None
            return concurrent
        value = self.load(resolved_id)
        if value is None:  # pragma: no cover - impossible for a conforming store
            raise EventStoreError("allocation intent append was not observable")
        return value

    create_intent = intent

    def load(self, allocation_id: UUID) -> SandboxAllocation | None:
        if not isinstance(allocation_id, UUID):
            raise TypeError("allocation_id must be UUID")
        events = self._read_stream(StreamId(_ALLOCATION_CATEGORY, allocation_id))
        if not events:
            return None
        value = _reconstruct_allocation(events)
        if value.allocation_id != allocation_id:
            raise EventStoreError("sandbox allocation stream identity mismatch")
        return value

    def list_open(self) -> tuple[SandboxAllocation, ...]:
        """Discover all non-released allocations from the global append log."""

        identities: set[UUID] = set()
        cursor = 0
        while True:
            page = self.event_store.read_all(after_position=cursor, limit=500)
            for event in page:
                if event.stream_id.category == _ALLOCATION_CATEGORY:
                    identities.add(event.stream_id.aggregate_id)
            if len(page) < 500:
                break
            cursor = page[-1].global_position
        values = []
        for allocation_id in sorted(identities, key=str):
            value = self.load(allocation_id)
            if value is not None and value.state is not AllocationState.RELEASED:
                values.append(value)
        return tuple(values)

    def bind(
        self,
        allocation: SandboxAllocation | UUID,
        container_id: str,
        *,
        actor: str = "sandbox",
    ) -> SandboxAllocation:
        current = self._current(allocation)
        _require_container_id(container_id)
        if current.state is AllocationState.BOUND:
            if current.container_id != container_id:
                raise SandboxError("allocation_container_conflict")
            return current
        if current.state is not AllocationState.INTENDED:
            raise SandboxError("invalid_allocation_transition")
        self._append(
            current,
            expected_version=current.version,
            event_type="sandbox.container-bound.v1",
            payload={
                "allocation_id": str(current.allocation_id),
                "container_id": container_id,
            },
            actor=actor,
        )
        return self._require(current.allocation_id)

    def start(
        self,
        allocation: SandboxAllocation | UUID,
        *,
        actor: str = "sandbox",
    ) -> SandboxAllocation:
        current = self._current(allocation)
        if current.state is AllocationState.STARTED:
            return current
        if current.state is not AllocationState.BOUND:
            raise SandboxError("invalid_allocation_transition")
        self._append(
            current,
            expected_version=current.version,
            event_type="sandbox.container-started.v1",
            payload={
                "allocation_id": str(current.allocation_id),
                "container_id": current.container_id,
            },
            actor=actor,
        )
        return self._require(current.allocation_id)

    mark_started = start

    def finish(
        self,
        allocation: SandboxAllocation | UUID,
        *,
        outcome: str,
        exit_code: int | None,
        oom_killed: bool | None,
        finished_at: datetime | None = None,
        actor: str = "sandbox",
    ) -> SandboxAllocation:
        current = self._current(allocation)
        completed_at = datetime.now(timezone.utc) if finished_at is None else _utc(finished_at)
        if current.state is AllocationState.FINISHED:
            if (
                current.outcome,
                current.exit_code,
                current.oom_killed,
            ) != (outcome, exit_code, oom_killed):
                raise SandboxError("allocation_outcome_conflict")
            return current
        if current.state not in (
            AllocationState.INTENDED,
            AllocationState.BOUND,
            AllocationState.STARTED,
        ):
            raise SandboxError("invalid_allocation_transition")
        if not isinstance(outcome, str) or _STABLE_REASON.fullmatch(outcome) is None:
            raise SandboxError("invalid_allocation_outcome")
        self._append(
            current,
            expected_version=current.version,
            event_type="sandbox.container-finished.v1",
            payload={
                "allocation_id": str(current.allocation_id),
                "container_id": current.container_id,
                "outcome": outcome,
                "exit_code": exit_code,
                "oom_killed": oom_killed,
                "finished_at": _timestamp(completed_at),
            },
            actor=actor,
        )
        return self._require(current.allocation_id)

    def release(
        self,
        allocation: SandboxAllocation | UUID,
        *,
        reason: str = "removed",
        actor: str = "sandbox",
    ) -> SandboxAllocation:
        current = self._current(allocation)
        if current.state is AllocationState.RELEASED:
            return current
        if current.state is not AllocationState.FINISHED:
            raise SandboxError("invalid_allocation_transition")
        if not isinstance(reason, str) or _STABLE_REASON.fullmatch(reason) is None:
            raise SandboxError("invalid_release_reason")
        self._append(
            current,
            expected_version=current.version,
            event_type="sandbox.container-released.v1",
            payload={
                "allocation_id": str(current.allocation_id),
                "container_id": current.container_id,
                "reason": reason,
            },
            actor=actor,
        )
        return self._require(current.allocation_id)

    def _current(
        self, allocation: SandboxAllocation | UUID
    ) -> SandboxAllocation:
        if isinstance(allocation, UUID):
            return self._require(allocation)
        if not isinstance(allocation, SandboxAllocation):
            raise TypeError("allocation must be SandboxAllocation or UUID")
        current = self._require(allocation.allocation_id)
        if current.version != allocation.version:
            raise SandboxError("stale_allocation_version")
        return current

    def _require(self, allocation_id: UUID) -> SandboxAllocation:
        value = self.load(allocation_id)
        if value is None:
            raise SandboxError("allocation_not_found")
        return value

    def _append(
        self,
        allocation: SandboxAllocation,
        *,
        expected_version: int,
        event_type: str,
        payload: Mapping[str, Any],
        actor: str,
    ) -> None:
        request = {
            "actor": actor,
            "allocation_id": str(allocation.allocation_id),
            "event_type": event_type,
            "expected_version": expected_version,
            "payload": dict(payload),
        }
        fingerprint = json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        fingerprint_hash = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        command_id = uuid5(
            NAMESPACE_URL,
            f"koawa-d8:{allocation.allocation_id}:{expected_version + 1}:{fingerprint_hash}",
        )
        event = NewEvent(
            event_id=uuid5(command_id, "event"),
            event_type=event_type,
            schema_version=1,
            occurred_at=datetime.now(timezone.utc),
            payload=payload,
            metadata=EventMetadata(
                command_id=command_id,
                correlation_id=allocation.owner_execution_id,
                actor=actor,
            ),
        )
        self.event_store.append_batch(
            (
                StreamWrite(
                    StreamId(_ALLOCATION_CATEGORY, allocation.allocation_id),
                    expected_version,
                    (event,),
                ),
            ),
            idempotency_key=command_id,
            request_fingerprint=fingerprint,
        )

    def _read_stream(self, stream_id: StreamId) -> tuple[Any, ...]:
        values: list[Any] = []
        cursor = -1
        while True:
            page = self.event_store.read_stream(
                stream_id, after_version=cursor, limit=500
            )
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version


def _reconstruct_allocation(events: tuple[Any, ...]) -> SandboxAllocation:
    allocation: SandboxAllocation | None = None
    for event in events:
        payload = event.payload
        if event.event_type == "sandbox.allocation-intended.v1":
            if allocation is not None or event.stream_version != 0:
                raise EventStoreError("invalid allocation intent history")
            allocation = SandboxAllocation(
                allocation_id=UUID(payload["allocation_id"]),
                owner_execution_id=UUID(payload["owner_execution_id"]),
                owner_nonce=payload["owner_nonce"],
                image_id=payload["image_id"],
                mount_digest=payload["mount_digest"],
                profile_digest=payload["profile_digest"],
                command_digest=payload["command_digest"],
                deadline_at=_parse_timestamp(payload["deadline_at"]),
                version=event.stream_version,
            )
            continue
        if allocation is None or event.stream_version != allocation.version + 1:
            raise EventStoreError("invalid allocation event order")
        if payload.get("allocation_id") != str(allocation.allocation_id):
            raise EventStoreError("allocation event identity mismatch")
        if event.event_type == "sandbox.container-bound.v1":
            if allocation.state is not AllocationState.INTENDED:
                raise EventStoreError("invalid allocation bind transition")
            allocation = replace(
                allocation,
                state=AllocationState.BOUND,
                version=event.stream_version,
                container_id=payload["container_id"],
            )
        elif event.event_type == "sandbox.container-started.v1":
            if (
                allocation.state is not AllocationState.BOUND
                or payload.get("container_id") != allocation.container_id
            ):
                raise EventStoreError("invalid allocation start transition")
            allocation = replace(
                allocation,
                state=AllocationState.STARTED,
                version=event.stream_version,
            )
        elif event.event_type == "sandbox.container-finished.v1":
            if allocation.state not in (
                AllocationState.INTENDED,
                AllocationState.BOUND,
                AllocationState.STARTED,
            ) or payload.get("container_id") != allocation.container_id:
                raise EventStoreError("invalid allocation finish transition")
            allocation = replace(
                allocation,
                state=AllocationState.FINISHED,
                version=event.stream_version,
                outcome=payload["outcome"],
                exit_code=payload.get("exit_code"),
                oom_killed=payload.get("oom_killed"),
                finished_at=_parse_timestamp(payload["finished_at"]),
            )
        elif event.event_type == "sandbox.container-released.v1":
            if (
                allocation.state is not AllocationState.FINISHED
                or payload.get("container_id") != allocation.container_id
            ):
                raise EventStoreError("invalid allocation release transition")
            allocation = replace(
                allocation,
                state=AllocationState.RELEASED,
                version=event.stream_version,
            )
        else:
            raise EventStoreError("unknown sandbox allocation event")
    if allocation is None:  # pragma: no cover - guarded by load
        raise EventStoreError("empty sandbox allocation stream")
    return allocation


class DockerSandboxDoctor:
    """Read-only daemon and immutable-image compatibility check."""

    def __init__(self, docker_executable: str | os.PathLike[str] = "docker") -> None:
        self._requested_executable = os.fspath(docker_executable)

    def check(self, image_id: str) -> DockerDoctorReport:
        try:
            validate_immutable_image_id(image_id)
        except SandboxError:
            return DockerDoctorReport(False, error_code="immutable_image_id_required")
        try:
            executable = _resolve_executable(self._requested_executable)
        except SandboxError:
            return DockerDoctorReport(False, error_code="docker_executable_unavailable")
        version = _run_cli(
            executable,
            ("version", "--format", "{{json .}}"),
            timeout_seconds=10.0,
        )
        if version.returncode != 0 or version.timed_out:
            return DockerDoctorReport(False, error_code="docker_daemon_unavailable")
        try:
            document = json.loads(version.stdout.decode("utf-8", "strict"))
            client = document["Client"]
            server = document["Server"]
            client_version = str(client["Version"])
            server_version = str(server["Version"])
            server_os = str(server["Os"]).lower()
            server_arch = str(server["Arch"]).lower()
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return DockerDoctorReport(False, error_code="docker_doctor_invalid_response")
        if server_os != "linux" or server_arch not in ("amd64", "x86_64"):
            return DockerDoctorReport(
                False,
                error_code="unsupported_docker_platform",
            )
        inspected = _run_cli(
            executable,
            ("image", "inspect", image_id),
            timeout_seconds=15.0,
        )
        if inspected.returncode != 0 or inspected.timed_out:
            return DockerDoctorReport(False, error_code="sandbox_image_unavailable")
        try:
            images = json.loads(inspected.stdout.decode("utf-8", "strict"))
            image = images[0]
            actual_id = image["Id"]
            image_os = str(image["Os"]).lower()
            image_arch = str(image["Architecture"]).lower()
        except (IndexError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return DockerDoctorReport(False, error_code="docker_image_invalid_response")
        if (
            actual_id != image_id
            or image_os != "linux"
            or image_arch not in ("amd64", "x86_64")
        ):
            return DockerDoctorReport(False, error_code="sandbox_image_identity_mismatch")
        return DockerDoctorReport(
            True,
            client_version=client_version,
            server_version=server_version,
            server_os="linux",
            server_architecture="amd64",
            image_id=image_id,
        )


def resolve_workspace_mount(
    workspace_root: str | os.PathLike[str],
    *,
    max_entries: int = 100_000,
) -> tuple[Path, str]:
    """Resolve a bind source and reject descendant link/reparse escapes."""

    try:
        root = Path(workspace_root).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        raise SandboxError("invalid_workspace_root") from None
    if not root.is_dir():
        raise SandboxError("invalid_workspace_root")
    text = str(root)
    if any(value in text for value in (",", "\r", "\n", "\x00")):
        raise SandboxError("unsafe_workspace_mount_path")
    if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries <= 0:
        raise SandboxError("invalid_workspace_scan_limit")
    count = 1
    try:
        def fail_walk(error: OSError) -> None:
            raise error

        for current, directories, files in os.walk(
            root,
            followlinks=False,
            onerror=fail_walk,
        ):
            for name in (*directories, *files):
                count += 1
                if count > max_entries:
                    raise SandboxError("workspace_scan_limit_exceeded")
                candidate = Path(current, name)
                info = candidate.lstat()
                is_reparse = bool(
                    getattr(info, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
                )
                if candidate.is_symlink() or is_reparse:
                    raise SandboxError("workspace_mount_link_escape")
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    raise SandboxError("workspace_mount_hardlink_escape")
    except SandboxError:
        raise
    except OSError:
        raise SandboxError("workspace_mount_scan_failed") from None
    info = root.stat()
    identity = {
        "schema_version": 1,
        "canonical_path": os.path.normcase(str(root)),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }
    return root, canonical_sha256(identity)


@dataclass(frozen=True, slots=True)
class _CliResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


def _run_cli(
    executable: Path,
    argv: Sequence[str],
    *,
    timeout_seconds: float,
) -> _CliResult:
    try:
        completed = subprocess.run(
            (str(executable), *tuple(argv)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _CliResult(None, exc.stdout or b"", exc.stderr or b"", True)
    except (OSError, ValueError):
        return _CliResult(None, b"", b"", False)
    return _CliResult(completed.returncode, completed.stdout, completed.stderr)


def _resolve_executable(value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SandboxError("docker_executable_unavailable")
    candidate = Path(value)
    found = str(candidate) if candidate.is_absolute() else shutil.which(value)
    if not found:
        raise SandboxError("docker_executable_unavailable")
    try:
        resolved = Path(found).resolve(strict=True)
    except (OSError, ValueError):
        raise SandboxError("docker_executable_unavailable") from None
    if not resolved.is_file():
        raise SandboxError("docker_executable_unavailable")
    return resolved


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise SandboxError("naive_allocation_timestamp")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise EventStoreError("invalid allocation timestamp")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, TypeError, SandboxError):
        raise EventStoreError("invalid allocation timestamp") from None


def _require_container_id(value: str) -> str:
    if not isinstance(value, str) or _CONTAINER_ID.fullmatch(value) is None:
        raise SandboxError("invalid_container_id")
    return value


def _is_within(value: Path, root: Path) -> bool:
    try:
        value.relative_to(root)
    except ValueError:
        return False
    return True


SandboxFaultHook = Callable[[str, SandboxAllocation, str | None], None]


@dataclass(frozen=True, slots=True)
class _AttachResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_bytes: int
    stderr_bytes: int
    duration_ms: int
    timed_out: bool = False
    output_limit: bool = False
    start_failed: bool = False
    cancelled: BaseException | None = None


class _BoundedBytes:
    def __init__(self, maximum: int, overflow: threading.Event) -> None:
        self.maximum = maximum
        self.overflow = overflow
        self.total = 0
        self.data = bytearray()
        self._lock = threading.Lock()

    def drain(self, stream: Any) -> None:
        try:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                with self._lock:
                    self.total += len(chunk)
                    remaining = self.maximum - len(self.data)
                    if remaining > 0:
                        self.data.extend(chunk[:remaining])
                    if self.total > self.maximum:
                        self.overflow.set()
        except (OSError, ValueError):
            return

    def snapshot(self) -> tuple[bytes, int]:
        with self._lock:
            return bytes(self.data), self.total


class ContainerReaper:
    """Reconcile only containers whose durable intent and exact labels agree."""

    def __init__(
        self,
        allocation_store: SandboxAllocationStore,
        docker_executable: str | os.PathLike[str] = "docker",
    ) -> None:
        if not isinstance(allocation_store, SandboxAllocationStore):
            raise TypeError("allocation_store must be SandboxAllocationStore")
        self._store = allocation_store
        self._docker = _resolve_executable(os.fspath(docker_executable))

    def reap(
        self,
        *,
        allocation_id: UUID | None = None,
        owner_execution_id: UUID | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> ReapReport:
        if allocation_id is not None and not isinstance(allocation_id, UUID):
            raise TypeError("allocation_id must be UUID or None")
        if owner_execution_id is not None and not isinstance(owner_execution_id, UUID):
            raise TypeError("owner_execution_id must be UUID or None")
        if force and allocation_id is None and owner_execution_id is None:
            raise SandboxError("force_reap_scope_required")
        current_time = datetime.now(timezone.utc) if now is None else _utc(now)
        listed = _run_cli(
            self._docker,
            (
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label={MANAGED_LABEL}={MANAGED_LABEL_VALUE}",
            ),
            timeout_seconds=20.0,
        )
        if listed.returncode != 0 or listed.timed_out:
            return ReapReport(errors=("docker_reaper_scan_failed",))

        removed: list[str] = []
        refused: list[str] = []
        released: list[UUID] = []
        errors: list[str] = []
        protected_allocations: set[UUID] = set()
        handled_containers: set[str] = set()

        for raw in listed.stdout.decode("utf-8", "replace").splitlines():
            container_id = raw.strip()
            try:
                _require_container_id(container_id)
                inspected = _inspect_container(self._docker, container_id)
            except SandboxError:
                errors.append("docker_reaper_inspect_failed")
                continue
            if inspected is None:
                continue
            config = inspected.get("Config")
            labels = (
                config.get("Labels") or {}
                if isinstance(config, Mapping)
                else {}
            )
            raw_allocation_id = labels.get(ALLOCATION_ID_LABEL)
            if (
                allocation_id is not None
                and raw_allocation_id != str(allocation_id)
            ):
                continue
            try:
                candidate_id = UUID(raw_allocation_id)
            except (TypeError, ValueError):
                if (
                    owner_execution_id is None
                    or labels.get(OWNER_EXECUTION_ID_LABEL)
                    == str(owner_execution_id)
                ):
                    refused.append(container_id)
                continue
            record = self._store.load(candidate_id)
            if record is None:
                if (
                    owner_execution_id is None
                    or labels.get(OWNER_EXECUTION_ID_LABEL)
                    == str(owner_execution_id)
                ):
                    refused.append(container_id)
                continue
            if (
                owner_execution_id is not None
                and record.owner_execution_id != owner_execution_id
            ):
                continue
            protected_allocations.add(record.allocation_id)
            if (
                record.state is not AllocationState.RELEASED
                and not force
                and record.deadline_at > current_time
            ):
                continue
            handled_containers.add(container_id)
            outcome = self._reconcile(record, inspected)
            self._collect_outcome(
                outcome,
                removed,
                refused,
                released,
                errors,
            )

        for record in self._store.list_open():
            if allocation_id is not None and record.allocation_id != allocation_id:
                continue
            if (
                owner_execution_id is not None
                and record.owner_execution_id != owner_execution_id
            ):
                continue
            reference = (
                record.container_id
                if record.container_id is not None
                else _expected_container_name(record.allocation_id)
            )
            try:
                inspected = _inspect_container_reference(
                    self._docker,
                    reference,
                )
            except SandboxError:
                protected_allocations.add(record.allocation_id)
                errors.append("docker_reaper_inspect_failed")
                continue
            if inspected is not None:
                container_id = _require_container_id(inspected["Id"])
                protected_allocations.add(record.allocation_id)
                if container_id not in handled_containers and (
                    force or record.deadline_at <= current_time
                ):
                    outcome = self._reconcile(record, inspected)
                    self._collect_outcome(
                        outcome,
                        removed,
                        refused,
                        released,
                        errors,
                    )
                continue
            if record.allocation_id in protected_allocations:
                continue
            if record.state is AllocationState.INTENDED:
                if record.deadline_at > current_time:
                    continue
            elif not force and record.deadline_at > current_time:
                continue
            try:
                current = record
                if current.state is not AllocationState.FINISHED:
                    current = self._store.finish(
                        current,
                        outcome="container_not_found",
                        exit_code=None,
                        oom_killed=None,
                        actor="reaper",
                    )
                current = self._store.release(
                    current,
                    reason="not_found",
                    actor="reaper",
                )
                released.append(current.allocation_id)
            except (EventStoreError, SandboxError):
                errors.append("docker_reaper_reconcile_failed")

        return ReapReport(
            removed=tuple(dict.fromkeys(removed)),
            refused=tuple(dict.fromkeys(refused)),
            released=tuple(dict.fromkeys(released)),
            errors=tuple(dict.fromkeys(errors)),
        )

    def _reconcile(
        self,
        record: SandboxAllocation,
        inspected: Mapping[str, Any],
    ) -> tuple[str | None, str | None, UUID | None, str | None]:
        container_id = _require_container_id(inspected.get("Id"))
        if inspected.get("Name") != f"/{_expected_container_name(record.allocation_id)}":
            return None, container_id, None, None
        if (
            record.state is not AllocationState.INTENDED
            and record.container_id != container_id
        ):
            return None, container_id, None, None
        if not _container_matches_allocation(inspected, record):
            return None, container_id, None, None
        try:
            if record.state is AllocationState.RELEASED:
                if _remove_exact(self._docker, container_id):
                    return container_id, None, None, None
                return None, None, None, "docker_reaper_remove_failed"
            current = record
            if current.state is AllocationState.INTENDED:
                current = self._store.bind(
                    current,
                    container_id,
                    actor="reaper",
                )
            state = inspected.get("State") or {}
            if bool(state.get("Running")) and not _kill_exact(
                self._docker,
                container_id,
            ):
                return None, None, None, "docker_reaper_kill_failed"
            if current.state is not AllocationState.FINISHED:
                current = self._store.finish(
                    current,
                    outcome="reaped",
                    exit_code=_optional_int(state.get("ExitCode")),
                    oom_killed=bool(state.get("OOMKilled", False)),
                    actor="reaper",
                )
            if not _remove_exact(self._docker, container_id):
                return None, None, None, "docker_reaper_remove_failed"
            current = self._store.release(
                current,
                reason="reaped",
                actor="reaper",
            )
            return container_id, None, current.allocation_id, None
        except (EventStoreError, SandboxError):
            return None, None, None, "docker_reaper_reconcile_failed"

    @staticmethod
    def _collect_outcome(
        outcome: tuple[str | None, str | None, UUID | None, str | None],
        removed: list[str],
        refused: list[str],
        released: list[UUID],
        errors: list[str],
    ) -> None:
        removed_id, refused_id, released_id, error = outcome
        if removed_id is not None:
            removed.append(removed_id)
        if refused_id is not None:
            refused.append(refused_id)
        if released_id is not None:
            released.append(released_id)
        if error is not None:
            errors.append(error)


class DockerCommandRunner:
    """D8 real-container backend for administrator-defined command profiles."""

    backend_name = "docker"

    def __init__(
        self,
        workspace_root: str | os.PathLike[str],
        profiles: Sequence[SandboxCommandProfile],
        event_store: EventStore,
        image_id: str,
        *,
        docker_executable: str | os.PathLike[str] = "docker",
        limits: SandboxLimits | None = None,
        fault_hook: SandboxFaultHook | None = None,
    ) -> None:
        validate_immutable_image_id(image_id)
        resolved_limits = limits or SandboxLimits()
        if not isinstance(resolved_limits, SandboxLimits):
            raise TypeError("limits must be SandboxLimits or None")
        entries: dict[str, SandboxCommandProfile] = {}
        for profile in profiles:
            if not isinstance(profile, SandboxCommandProfile):
                raise TypeError("profiles must contain SandboxCommandProfile")
            if profile.profile_id in entries:
                raise CommandRunnerError("duplicate_command_profile")
            entries[profile.profile_id] = profile
        if not entries:
            raise CommandRunnerError("empty_command_profiles")
        if fault_hook is not None and not callable(fault_hook):
            raise TypeError("fault_hook must be callable or None")
        executable = _resolve_executable(os.fspath(docker_executable))
        doctor = DockerSandboxDoctor(executable).check(image_id)
        if not doctor.ready:
            raise CommandRunnerError(
                doctor.error_code or "docker_doctor_not_ready"
            )
        root, mount_digest = resolve_workspace_mount(
            workspace_root,
            max_entries=resolved_limits.max_workspace_entries,
        )
        self._root = root
        self._mount_digest = mount_digest
        self._profiles = entries
        self._store = SandboxAllocationStore(event_store)
        self._image_id = image_id
        self._docker = executable
        self._limits = resolved_limits
        self._fault_hook = fault_hook
        self._reaper = ContainerReaper(self._store, executable)

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    @property
    def allocation_store(self) -> SandboxAllocationStore:
        return self._store

    @property
    def reaper(self) -> ContainerReaper:
        return self._reaper

    def validate_profile(self, profile_id: str) -> None:
        if profile_id not in self._profiles:
            raise CommandRunnerError("unknown_command_profile")

    def run(
        self,
        profile_id: str,
        *,
        progress_guard: Callable[[], None] | None = None,
        execution_id: UUID | None = None,
    ) -> CommandResult:
        self.validate_profile(profile_id)
        if not isinstance(execution_id, UUID) or execution_id.int == 0:
            raise CommandRunnerError("sandbox_execution_identity_required")
        try:
            if progress_guard is not None:
                progress_guard()
            recovery = self._reaper.reap(
                owner_execution_id=execution_id,
                force=True,
            )
            if recovery.refused or recovery.errors:
                raise CommandRunnerError("sandbox_recovery_blocked")
            if any(
                value.owner_execution_id == execution_id
                for value in self._store.list_open()
            ):
                raise CommandRunnerError("sandbox_recovery_blocked")
            if progress_guard is not None:
                progress_guard()
            return self._run_profile(
                self._profiles[profile_id],
                execution_id,
                progress_guard,
            )
        except CommandRunnerError:
            raise
        except SandboxError as error:
            raise CommandRunnerError(error.code) from None
        except EventStoreError:
            raise CommandRunnerError("sandbox_persistence_failed") from None

    def run_profile_object(
        self,
        profile: SandboxCommandProfile,
        *,
        progress_guard: Callable[[], None] | None = None,
        execution_id: UUID | None = None,
    ) -> CommandResult:
        """Run one host-defined profile object without pre-registration."""

        if not isinstance(profile, SandboxCommandProfile):
            raise TypeError("profile must be SandboxCommandProfile")
        if not isinstance(execution_id, UUID) or execution_id.int == 0:
            raise CommandRunnerError("sandbox_execution_identity_required")
        try:
            if progress_guard is not None:
                progress_guard()
            recovery = self._reaper.reap(
                owner_execution_id=execution_id,
                force=True,
            )
            if recovery.refused or recovery.errors:
                raise CommandRunnerError("sandbox_recovery_blocked")
            if any(
                value.owner_execution_id == execution_id
                for value in self._store.list_open()
            ):
                raise CommandRunnerError("sandbox_recovery_blocked")
            if progress_guard is not None:
                progress_guard()
            return self._run_profile(
                profile,
                execution_id,
                progress_guard,
            )
        except CommandRunnerError:
            raise
        except SandboxError as error:
            raise CommandRunnerError(error.code) from None
        except EventStoreError:
            raise CommandRunnerError("sandbox_persistence_failed") from None

    def _run_profile(
        self,
        profile: SandboxCommandProfile,
        execution_id: UUID,
        progress_guard: Callable[[], None] | None,
    ) -> CommandResult:
        root, digest = resolve_workspace_mount(
            self._root,
            max_entries=self._limits.max_workspace_entries,
        )
        if root != self._root or digest != self._mount_digest:
            raise SandboxError("workspace_mount_identity_changed")
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=60.0
            + profile.timeout_seconds
            + self._limits.cleanup_grace_seconds
        )
        allocation = self._store.intent(
            owner_execution_id=execution_id,
            image_id=self._image_id,
            mount_digest=self._mount_digest,
            profile_digest=profile.profile_digest,
            command_digest=profile.command_digest,
            deadline_at=deadline,
        )
        self._fault("after_intent", allocation, None)
        create_started = time.monotonic()
        created = _run_cli(
            self._docker,
            self._create_arguments(profile, allocation),
            timeout_seconds=30.0,
        )
        if (
            created.returncode != 0
            or created.timed_out
            or not created.stdout.strip()
        ):
            return self._result(
                profile,
                allocation,
                outcome=CommandOutcome.START_FAILED,
                exit_code=None,
                stdout=created.stdout,
                stderr=created.stderr,
                stdout_bytes=len(created.stdout),
                stderr_bytes=len(created.stderr),
                duration_ms=int(
                    (time.monotonic() - create_started) * 1000
                ),
            )
        container_id = _require_container_id(
            created.stdout.decode("ascii", "strict").strip()
        )
        try:
            self._fault("after_create_before_bind", allocation, container_id)
        except BaseException:
            self._reaper.reap(
                allocation_id=allocation.allocation_id,
                force=True,
            )
            raise
        inspected = _inspect_container(self._docker, container_id)
        if not _container_security_matches(
            inspected,
            allocation,
            profile,
            self._limits,
        ):
            current = self._store.finish(
                allocation,
                outcome="security_rejected",
                exit_code=None,
                oom_killed=None,
            )
            if _remove_exact(self._docker, container_id):
                self._store.release(
                    current,
                    reason="security_rejected",
                )
            raise CommandRunnerError("sandbox_container_security_mismatch")
        allocation = self._store.bind(allocation, container_id)
        try:
            self._fault("after_bind", allocation, container_id)
        except BaseException:
            self._reaper.reap(
                allocation_id=allocation.allocation_id,
                force=True,
            )
            raise
        allocation = self._store.start(allocation)
        attached = _attach_container(
            self._docker,
            container_id,
            timeout_seconds=profile.timeout_seconds,
            max_stdout_bytes=profile.max_stdout_bytes,
            max_stderr_bytes=profile.max_stderr_bytes,
            progress_guard=progress_guard,
        )
        if attached.cancelled is not None:
            pending_cancellation = attached.cancelled
            _kill_exact(self._docker, container_id)
            try:
                inspected = _inspect_container(self._docker, container_id)
                state = (
                    {}
                    if inspected is None
                    else (inspected.get("State") or {})
                )
                current = self._store.finish(
                    allocation,
                    outcome=CommandOutcome.CANCELLED.value,
                    exit_code=_optional_int(state.get("ExitCode")),
                    oom_killed=bool(state.get("OOMKilled", False)),
                )
                if _remove_exact(self._docker, container_id):
                    self._store.release(current, reason="cancelled")
            except BaseException:
                try:
                    _remove_exact(self._docker, container_id)
                except BaseException:
                    pass
            raise pending_cancellation
        if (
            attached.timed_out
            or attached.output_limit
        ):
            _kill_exact(self._docker, container_id)
        inspected = _inspect_container(self._docker, container_id)
        state = {} if inspected is None else (inspected.get("State") or {})
        exit_code = _optional_int(state.get("ExitCode"))
        oom_killed = bool(state.get("OOMKilled", False))
        if attached.output_limit:
            outcome = CommandOutcome.OUTPUT_LIMIT
        elif attached.timed_out:
            outcome = CommandOutcome.TIMED_OUT
        elif oom_killed:
            outcome = CommandOutcome.OOM_KILLED
        elif attached.start_failed or inspected is None:
            outcome = CommandOutcome.START_FAILED
        elif exit_code == 0:
            outcome = CommandOutcome.PASSED
        else:
            outcome = CommandOutcome.FAILED
        allocation = self._store.finish(
            allocation,
            outcome=outcome.value,
            exit_code=exit_code,
            oom_killed=oom_killed,
        )
        removed = _remove_exact(self._docker, container_id)
        if removed:
            allocation = self._store.release(allocation, reason="removed")
        else:
            outcome = CommandOutcome.CLEANUP_FAILED
        result = self._result(
            profile,
            allocation,
            outcome=outcome,
            exit_code=exit_code,
            stdout=attached.stdout,
            stderr=attached.stderr,
            stdout_bytes=attached.stdout_bytes,
            stderr_bytes=attached.stderr_bytes,
            duration_ms=attached.duration_ms,
        )
        return result

    def _create_arguments(
        self,
        profile: SandboxCommandProfile,
        allocation: SandboxAllocation,
    ) -> tuple[str, ...]:
        values = [
            "container",
            "create",
            "--name",
            _expected_container_name(allocation.allocation_id),
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self._limits.pids_limit),
            "--cpus",
            format(self._limits.cpus, "g"),
            "--memory",
            str(self._limits.memory_bytes),
            "--memory-swap",
            str(self._limits.memory_bytes),
            "--tmpfs",
            (
                "/tmp:rw,noexec,nosuid,nodev,"
                f"size={self._limits.tmpfs_bytes},mode=1777"
            ),
            "--mount",
            f"type=bind,src={self._root},dst=/workspace,readonly",
            "--workdir",
            profile.working_directory,
            "--init",
            "--log-driver",
            "none",
        ]
        for name, value in allocation.expected_labels:
            values.extend(("--label", f"{name}={value}"))
        for name, value in profile.environment:
            values.extend(("--env", f"{name}={value}"))
        values.extend(
            (
                "--entrypoint",
                profile.argv[0],
                self._image_id,
                *profile.argv[1:],
            )
        )
        return tuple(values)

    def _result(
        self,
        profile: SandboxCommandProfile,
        allocation: SandboxAllocation,
        *,
        outcome: CommandOutcome,
        exit_code: int | None,
        stdout: bytes,
        stderr: bytes,
        stdout_bytes: int,
        stderr_bytes: int,
        duration_ms: int,
    ) -> CommandResult:
        return CommandResult(
            profile_id=profile.profile_id,
            outcome=outcome,
            exit_code=exit_code,
            stdout=stdout.decode("utf-8", "replace"),
            stderr=stderr.decode("utf-8", "replace"),
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=stdout_bytes > len(stdout),
            stderr_truncated=stderr_bytes > len(stderr),
            duration_ms=duration_ms,
            argv=profile.argv,
            timeout_seconds=profile.timeout_seconds,
            backend="docker",
            immutable_image_id=self._image_id,
            profile_digest=profile.profile_digest,
            allocation_id=allocation.allocation_id,
            container_id=allocation.container_id,
        )

    def _fault(
        self,
        point: str,
        allocation: SandboxAllocation,
        container_id: str | None,
    ) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point, allocation, container_id)


def _attach_container(
    docker: Path,
    container_id: str,
    *,
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    progress_guard: Callable[[], None] | None,
) -> _AttachResult:
    start = time.monotonic()
    creationflags = (
        subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    )
    try:
        process = subprocess.Popen(
            (str(docker), "container", "start", "--attach", container_id),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
        )
    except (OSError, ValueError):
        return _AttachResult(
            None,
            b"",
            b"",
            0,
            0,
            int((time.monotonic() - start) * 1000),
            start_failed=True,
        )
    overflow = threading.Event()
    stdout = _BoundedBytes(max_stdout_bytes, overflow)
    stderr = _BoundedBytes(max_stderr_bytes, overflow)
    out_thread = threading.Thread(
        target=stdout.drain,
        args=(process.stdout,),
        daemon=True,
    )
    err_thread = threading.Thread(
        target=stderr.drain,
        args=(process.stderr,),
        daemon=True,
    )
    out_thread.start()
    err_thread.start()
    deadline = start + timeout_seconds
    timed_out = False
    cancelled: BaseException | None = None
    try:
        while process.poll() is None:
            if progress_guard is not None:
                try:
                    progress_guard()
                except BaseException as error:
                    cancelled = error
                    break
            if overflow.is_set():
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.025)
        if process.poll() is None:
            _kill_exact(docker, container_id)
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)
    finally:
        out_thread.join(timeout=5.0)
        err_thread.join(timeout=5.0)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    out, out_total = stdout.snapshot()
    err, err_total = stderr.snapshot()
    return _AttachResult(
        process.returncode,
        out,
        err,
        out_total,
        err_total,
        int((time.monotonic() - start) * 1000),
        timed_out=timed_out,
        output_limit=overflow.is_set(),
        cancelled=cancelled,
    )


def _inspect_container(docker: Path, container_id: str) -> dict[str, Any] | None:
    _require_container_id(container_id)
    value = _inspect_container_reference(docker, container_id)
    if value is not None and value.get("Id") != container_id:
        raise SandboxError("docker_inspect_identity_mismatch")
    return value


def _inspect_container_reference(
    docker: Path,
    reference: str,
) -> dict[str, Any] | None:
    if (
        not isinstance(reference, str)
        or not reference
        or "\x00" in reference
        or "\r" in reference
        or "\n" in reference
    ):
        raise SandboxError("docker_inspect_identity_mismatch")
    inspected = _run_cli(
        docker,
        ("container", "inspect", reference),
        timeout_seconds=15.0,
    )
    if inspected.timed_out:
        raise SandboxError("docker_inspect_failed")
    if inspected.returncode != 0:
        if _MISSING_CONTAINER.search(inspected.stderr.decode("utf-8", "replace")):
            return None
        raise SandboxError("docker_inspect_failed")
    try:
        values = json.loads(inspected.stdout.decode("utf-8", "strict"))
        value = values[0]
    except (IndexError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise SandboxError("docker_inspect_failed") from None
    if not isinstance(value, dict):
        raise SandboxError("docker_inspect_failed")
    _require_container_id(value.get("Id"))
    return value


def _container_matches_allocation(
    inspected: Mapping[str, Any],
    allocation: SandboxAllocation,
) -> bool:
    config = inspected.get("Config")
    if not isinstance(config, Mapping):
        return False
    labels = config.get("Labels")
    if not isinstance(labels, Mapping):
        return False
    if any(labels.get(name) != value for name, value in allocation.expected_labels):
        return False
    if inspected.get("Image") != allocation.image_id:
        return False
    mounts = inspected.get("Mounts")
    if not isinstance(mounts, list):
        return False
    bind_mounts = [
        value
        for value in mounts
        if isinstance(value, Mapping)
        and value.get("Type") == "bind"
        and value.get("Destination") == "/workspace"
    ]
    if len(bind_mounts) != 1 or bool(bind_mounts[0].get("RW")):
        return False
    try:
        _, digest = resolve_workspace_mount(
            bind_mounts[0]["Source"],
            max_entries=1_000_000,
        )
    except (KeyError, SandboxError):
        return False
    return digest == allocation.mount_digest


def _container_security_matches(
    inspected: Mapping[str, Any] | None,
    allocation: SandboxAllocation,
    profile: SandboxCommandProfile,
    limits: SandboxLimits,
) -> bool:
    if inspected is None or not _container_matches_allocation(
        inspected,
        allocation,
    ):
        return False
    config = inspected.get("Config")
    host = inspected.get("HostConfig")
    if not isinstance(config, Mapping) or not isinstance(host, Mapping):
        return False
    if (
        inspected.get("Name")
        != f"/{_expected_container_name(allocation.allocation_id)}"
        or config.get("User") != "65532:65532"
        or config.get("WorkingDir") != profile.working_directory
        or inspected.get("Path") != profile.argv[0]
        or tuple(inspected.get("Args") or ()) != profile.argv[1:]
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("PidsLimit") != limits.pids_limit
        or host.get("NanoCpus") != int(limits.cpus * 1_000_000_000)
        or host.get("Memory") != limits.memory_bytes
        or host.get("MemorySwap") != limits.memory_bytes
        or host.get("Init") is not True
    ):
        return False
    cap_drop = host.get("CapDrop")
    security_options = host.get("SecurityOpt")
    log_config = host.get("LogConfig")
    tmpfs = host.get("Tmpfs")
    if (
        not isinstance(cap_drop, list)
        or set(cap_drop) != {"ALL"}
        or not isinstance(security_options, list)
        or "no-new-privileges" not in security_options
        or not isinstance(log_config, Mapping)
        or log_config.get("Type") != "none"
        or not isinstance(tmpfs, Mapping)
    ):
        return False
    tmp_options = tmpfs.get("/tmp")
    if not isinstance(tmp_options, str):
        return False
    required_tmp_options = {
        "rw",
        "noexec",
        "nosuid",
        "nodev",
        f"size={limits.tmpfs_bytes}",
        "mode=1777",
    }
    if not required_tmp_options.issubset(set(tmp_options.split(","))):
        return False
    expected_environment = {
        f"{name}={value}" for name, value in profile.environment
    }
    actual_environment = config.get("Env")
    if not isinstance(actual_environment, list) or not expected_environment.issubset(
        set(actual_environment)
    ):
        return False
    return True


def _kill_exact(docker: Path, container_id: str) -> bool:
    _require_container_id(container_id)
    killed = _run_cli(
        docker,
        ("container", "kill", container_id),
        timeout_seconds=15.0,
    )
    if killed.returncode == 0:
        return True
    message = killed.stderr.decode("utf-8", "replace")
    return bool(
        _MISSING_CONTAINER.search(message)
        or "is not running" in message.lower()
    )


def _remove_exact(docker: Path, container_id: str) -> bool:
    _require_container_id(container_id)
    removed = _run_cli(
        docker,
        ("container", "rm", "--force", container_id),
        timeout_seconds=20.0,
    )
    if removed.returncode != 0:
        message = removed.stderr.decode("utf-8", "replace")
        if not _MISSING_CONTAINER.search(message):
            return False
    return _inspect_container(docker, container_id) is None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _expected_container_name(allocation_id: UUID) -> str:
    if not isinstance(allocation_id, UUID) or allocation_id.int == 0:
        raise SandboxError("nil_allocation_identity")
    return f"koawa-v2-{allocation_id.hex}"
