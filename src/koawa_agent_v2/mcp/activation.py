"""I6 MCP activation: launch identity, durable admin grants, allocation ledger.

Implements §8.4 (launch identity + TOCTOU staging) and §8.5 (activation and
allocation events over the EventStore).  The activation service is the ONLY
path that may hand a launcher an AuthorizedLaunchTicket; the launcher never
creates an OS process without an atomically consumed ticket.

Streams are fixed as ``mcp-activation-{request_id}`` and
``mcp-allocation-{allocation_id}``; every payload carries only digests/ids/
profile/principal - never env values, stderr, credentials or argv bodies.
"""

from __future__ import annotations
from koawa_agent_v2.telemetry.faults import FaultPoint

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from ..control.event_store import (
    EventMetadata,
    EventStore,
    NewEvent,
    StreamId,
    StreamPrecondition,
    StreamWrite,
    WrongExpectedVersion,
)
from ..telemetry.faults import FaultPort, NO_OP_FAULT_PORT
# runtime.config / runtime.subprocess_env are imported lazily: the runtime
# package __init__ imports app -> assembly -> mcp, so a module-level runtime
# import here would cycle back through koawa_agent_v2.runtime mid-init.


class McpActivationError(RuntimeError):
    # Stable, content-free activation/allocation failure.

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _mcp_execution_profile():
    from ..runtime.config import McpExecutionProfile

    return McpExecutionProfile


def _mcp_resource_limits():
    from ..runtime.config import McpResourceLimits

    return McpResourceLimits


def _environment_identities():
    from ..runtime.subprocess_env import environment_identities

    return environment_identities


# ---- §8.4 launch identity ------------------------------------------------

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    canonical_path: str
    platform_file_id: str
    size: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class CodeArtifactIdentity:
    role: str
    argv_index: int
    source: ExecutableIdentity
    staged: ExecutableIdentity


@dataclass(frozen=True, slots=True)
class EnvironmentIdentity:
    name: str
    value_digest: str


@dataclass(frozen=True, slots=True)
class ReadOnlyMountIdentity:
    container_path: str
    source_file_id: str
    manifest_digest: str


@dataclass(frozen=True, slots=True)
class McpLaunchIdentity:
    server_id: str
    execution_profile: str
    code_artifacts: tuple[CodeArtifactIdentity, ...]
    argv_digest: str
    cwd_identity_digest: str | None
    environment: tuple[EnvironmentIdentity, ...]
    image_digest: str | None
    read_only_mounts: tuple[ReadOnlyMountIdentity, ...]
    resource_digest: str
    deadline_limit_digest: str
    config_digest: str

    def to_audit_document(self) -> dict[str, object]:
        # Bounded audit projection: digests only, never argv/env values.
        return {
            "server_id": self.server_id,
            "execution_profile": self.execution_profile,
            "argv_digest": self.argv_digest,
            "cwd_identity_digest": self.cwd_identity_digest,
            "image_digest": self.image_digest,
            "resource_digest": self.resource_digest,
            "deadline_limit_digest": self.deadline_limit_digest,
            "config_digest": self.config_digest,
        }


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "strict")).hexdigest()


def _platform_file_id(path: Path, lst: os.stat_result) -> str:
    # Best-effort platform file index; drifts when the file is replaced.
    # Windows uses an OS file index opened with FILE_FLAG_OPEN_REPARSE_POINT;
    # other platforms fall back to the lstat device/index pair.
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            open_reparse = 0x00200000
            share = 0x1 | 0x2 | 0x4
            handle = kernel32.CreateFileW(
                str(path),
                0,
                share,
                None,
                3,
                open_reparse,
                None,
            )
            if handle not in (0, -1):
                try:
                    info = (ctypes.c_ulong * 20)()
                    if kernel32.GetFileInformationByHandle(handle, info):
                        volume = info[7]
                        index_high = info[11]
                        index_low = info[12]
                        return f"win:{volume:x}:{index_high:x}:{index_low:x}"
                finally:
                    kernel32.CloseHandle(handle)
        except Exception:
            pass
    return f"dev:{lst.st_dev}:ino:{lst.st_ino}"


def _identity_of(path: Path) -> ExecutableIdentity:
    # No-follow identity of one executable/code artifact file.  Symlinks,
    # directories and unreadable paths fail closed; hashing is bounded.
    if not isinstance(path, Path):
        raise TypeError("path must be Path")
    try:
        lst = os.lstat(path)
    except OSError:
        raise McpActivationError("mcp_artifact_unreadable") from None
    if stat.S_ISLNK(lst.st_mode):
        raise McpActivationError("mcp_artifact_symlink_forbidden")
    if stat.S_ISDIR(lst.st_mode):
        raise McpActivationError("mcp_artifact_directory_forbidden")
    if not stat.S_ISREG(lst.st_mode):
        raise McpActivationError("mcp_artifact_unreadable")
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise McpActivationError("mcp_artifact_too_large")
                digest.update(chunk)
    except McpActivationError:
        raise
    except OSError:
        raise McpActivationError("mcp_artifact_unreadable") from None
    canonical = str(path.resolve(strict=False))
    return ExecutableIdentity(
        canonical,
        _platform_file_id(path, lst),
        size,
        digest.hexdigest(),
    )


def _resolve_executable(config, base_dir: Path) -> Path:
    # command[0] resolves to an absolute trusted file at preflight (§8.3).
    if not isinstance(config.command, tuple) or not config.command:
        raise McpActivationError("mcp_command_required")
    raw = config.command[0]
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    candidate = candidate.resolve(strict=False)
    try:
        lst = os.lstat(candidate)
    except OSError:
        raise McpActivationError("mcp_executable_not_found") from None
    if stat.S_ISLNK(lst.st_mode):
        raise McpActivationError("mcp_executable_must_not_be_symlink")
    if not stat.S_ISREG(lst.st_mode):
        raise McpActivationError("mcp_executable_not_found")
    return candidate



def _mount_identity(source: Path) -> ReadOnlyMountIdentity:
    # No-follow identity for one approved read-only mount source.  Directories
    # are currently rejected (§8.4: no bare directory hashing).
    identity = _identity_of(source)
    return ReadOnlyMountIdentity(
        container_path="__unset__",
        source_file_id=identity.platform_file_id,
        manifest_digest=identity.content_sha256,
    )


def resolve_launch_identity(
    config,
    *,
    base_dir: Path,
) -> McpLaunchIdentity:
    # Compute the exact launch identity for one server config (§8.4).
    if not isinstance(config, object):
        raise TypeError("config must be McpServerConfig")
    executable = _resolve_executable(config, base_dir)
    code_indexes: dict[int, tuple[str, Path]] = {
        0: ("executable", executable),
    }
    if config.execution_profile is _mcp_execution_profile().HOST_TRUSTED:
        for artifact in config.code_artifacts:
            if artifact.argv_index == 0:
                continue
            raw = config.command[artifact.argv_index]
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = base_dir / candidate
            code_indexes[artifact.argv_index] = (
                artifact.role, candidate.resolve(strict=False),
            )
    artifacts: list[CodeArtifactIdentity] = []
    for index in sorted(code_indexes):
        role, artifact_path = code_indexes[index]
        source = _identity_of(artifact_path)
        artifacts.append(
            CodeArtifactIdentity(role, index, source, source),
        )
    argv_digest = _sha256(_canonical_json({"argv": list(config.command)}))
    cwd_identity_digest = (
        None
        if config.cwd is None
        else _sha256(str(config.cwd.resolve(strict=False)))
    )
    environment = tuple(
        EnvironmentIdentity(name, value_digest)
        for name, value_digest in _environment_identities()(dict(config.environment))
    )
    image_digest = (
        _sha256(config.image_id) if config.image_id else None
    )
    read_only_mounts: tuple[ReadOnlyMountIdentity, ...] = ()
    if config.execution_profile is _mcp_execution_profile().HOST_TRUSTED:
        mounts: list[ReadOnlyMountIdentity] = []
        for source_raw, container_path in config.read_only_mounts:
            source = Path(source_raw).resolve(strict=False)
            identity = _mount_identity(source)
            mounts.append(
                ReadOnlyMountIdentity(
                    container_path,
                    identity.source_file_id,
                    identity.manifest_digest,
                ),
            )
        read_only_mounts = tuple(mounts)
    limits = config.resource_limits or _mcp_resource_limits()()
    resource_digest = _sha256(_canonical_json(limits.to_document()))
    deadline_limit_digest = _sha256(_canonical_json({
        "process_start_timeout_seconds": config.process_start_timeout_seconds,
        "initialize_timeout_seconds": config.initialize_timeout_seconds,
        "tools_list_timeout_seconds": config.tools_list_timeout_seconds,
        "tool_call_timeout_seconds": config.tool_call_timeout_seconds,
        "io_poll_timeout_seconds": config.io_poll_timeout_seconds,
        "shutdown_timeout_seconds": config.shutdown_timeout_seconds,
        "max_pending_requests": config.max_pending_requests,
        "max_inbound_messages": config.max_inbound_messages,
        "max_list_pages": config.max_list_pages,
        "max_tools": config.max_tools,
        "max_notifications_per_window": config.max_notifications_per_window,
        "max_cursor_bytes": config.max_cursor_bytes,
        "max_frame_bytes": config.max_frame_bytes,
        "max_stderr_bytes": config.max_stderr_bytes,
        "max_result_bytes": config.max_result_bytes,
    }))
    profile = (
        "legacy"
        if config.execution_profile is None
        else config.execution_profile.value
    )
    config_digest = _sha256(_canonical_json({
        "server_id": config.server_id,
        "execution_profile": profile,
        "argv_digest": argv_digest,
        "cwd_identity_digest": cwd_identity_digest,
        "environment": [
            {"name": entry.name, "value_digest": entry.value_digest}
            for entry in environment
        ],
        "image_digest": image_digest,
        "read_only_mounts": [
            {
                "container_path": mount.container_path,
                "source_file_id": mount.source_file_id,
                "manifest_digest": mount.manifest_digest,
            }
            for mount in read_only_mounts
        ],
        "resource_digest": resource_digest,
        "deadline_limit_digest": deadline_limit_digest,
        "code_artifacts": [
            {
                "role": artifact.role,
                "argv_index": artifact.argv_index,
                "source_canonical_path": artifact.source.canonical_path,
                "source_file_id": artifact.source.platform_file_id,
                "source_content_sha256": artifact.source.content_sha256,
                "source_size": artifact.source.size,
                "staged_canonical_path": artifact.staged.canonical_path,
                "staged_content_sha256": artifact.staged.content_sha256,
            }
            for artifact in artifacts
        ],
    }))
    return McpLaunchIdentity(
        server_id=config.server_id,
        execution_profile=profile,
        code_artifacts=tuple(artifacts),
        argv_digest=argv_digest,
        cwd_identity_digest=cwd_identity_digest,
        environment=environment,
        image_digest=image_digest,
        read_only_mounts=read_only_mounts,
        resource_digest=resource_digest,
        deadline_limit_digest=deadline_limit_digest,
        config_digest=config_digest,
    )


@dataclass(frozen=True, slots=True)
class StagedLaunchPlan:
    # Resolved + staged material ready for one spawn (never serialized).
    # staged_argv rewrites every code-bearing argument to the controller
    # content-addressed staging path (TOCTOU window closure, §8.4).

    config: object
    identity: McpLaunchIdentity
    staged_argv: tuple[str, ...]
    code_artifacts: tuple[CodeArtifactIdentity, ...]
    staged_dir: Path

    def revalidate_sources(self) -> None:
        # Re-check every source and staged identity right before spawn;
        # any drift raises so the prior grant is invalidated before the OS
        # create (launcher call count stays zero).
        for artifact in self.code_artifacts:
            for entry in (artifact.source, artifact.staged):
                current = _identity_of(Path(entry.canonical_path))
                if (
                    current.canonical_path != entry.canonical_path
                    or current.platform_file_id != entry.platform_file_id
                    or current.size != entry.size
                    or current.content_sha256 != entry.content_sha256
                ):
                    raise McpActivationError("mcp_launch_drift_detected")


def stage_code_artifacts(
    config,
    *,
    base_dir: Path,
    staging_root: Path,
) -> StagedLaunchPlan:
    # Copy host artifacts into the controller staging dir (TOCTOU).  For
    # HOST_TRUSTED servers every code-bearing argv entry is re-staged and the
    # plan argv is rewritten to the staged path.  Legacy and SANDBOXED
    # profiles resolve identity only for audit (legacy fixture semantics never
    # claim isolation); the sandboxed path executes inside a container.
    identity = resolve_launch_identity(config, base_dir=base_dir)
    if config.execution_profile is not _mcp_execution_profile().HOST_TRUSTED:
        return StagedLaunchPlan(
            config=config,
            identity=identity,
            staged_argv=tuple(config.command),
            code_artifacts=identity.code_artifacts,
            staged_dir=staging_root,
        )
    staging_root.mkdir(parents=True, exist_ok=True)
    try:
        staging_root.chmod(0o700)
    except OSError:
        pass
    staged_argv = list(config.command)
    staged_artifacts: list[CodeArtifactIdentity] = []
    for artifact in identity.code_artifacts:
        source_path = Path(artifact.source.canonical_path)
        staged_path = (
            staging_root
            / (
                f"{artifact.role}-{artifact.argv_index}-"
                f"{artifact.source.content_sha256[:24]}{source_path.suffix}"
            )
        )
        if staged_path.exists():
            staged_identity = _identity_of(staged_path)
            if staged_identity.content_sha256 != artifact.source.content_sha256:
                raise McpActivationError("staged_artifact_corrupt")
        else:
            try:
                shutil.copy2(
                    source_path,
                    staged_path,
                    follow_symlinks=False,
                )
                os.chmod(staged_path, 0o500)
            except OSError:
                raise McpActivationError("staged_artifact_copy_failed") from None
            staged_identity = _identity_of(staged_path)
            if (
                staged_identity.content_sha256 != artifact.source.content_sha256
                or staged_identity.size != artifact.source.size
            ):
                raise McpActivationError("staged_artifact_corrupt")
        staged_argv[artifact.argv_index] = str(staged_path)
        staged_artifacts.append(
            CodeArtifactIdentity(
                artifact.role,
                artifact.argv_index,
                artifact.source,
                staged_identity,
            ),
        )
    return StagedLaunchPlan(
        config=config,
        identity=identity,
        staged_argv=tuple(staged_argv),
        code_artifacts=tuple(staged_artifacts),
        staged_dir=staging_root,
    )


# ---- §8.5 activation & allocation state --------------------------------


@dataclass(frozen=True, slots=True)
class ActivationView:
    request_id: UUID
    server_id: str
    launch_identity_digest: str
    execution_profile: str
    principal_id: str
    scope: str
    status: str
    version: int
    expires_at: datetime | None

    @property
    def request_id_str(self) -> str:
        return str(self.request_id)


@dataclass(frozen=True, slots=True)
class AllocationIntent:
    request_id: UUID
    allocation_id: UUID
    attempt: int
    activation_stream_version: int


@dataclass(frozen=True, slots=True)
class AuthorizedLaunchTicket:
    # One-time process-local ticket issued only after the claim commits.
    # Never serialized, never written to an event (doc §8.5).

    request_id: UUID
    grant_stream_version: int
    grant_digest: str
    allocation_id: UUID
    allocation_stream_version: int
    claim_epoch: int
    claim_token: UUID
    nonce: UUID
    launch_identity_digest: str
    not_after: datetime



_ACTIVATION_NAME = "koawa-mcp-activation-v1"


def process_start_scope(profile) -> str:
    # Required capability scope for one server profile (§8.5): host_trusted
    # needs the explicit mcp.host_process.execute scope; others share mcp.use.
    if profile is _mcp_execution_profile().HOST_TRUSTED:
        return "mcp.host_process.execute"
    return "mcp.use"


class ActivationService:
    # Durable activation/allocation ledger + one-time launch tickets.

    STATUS_REQUESTED = "requested"
    STATUS_GRANTED = "granted"
    STATUS_DENIED = "denied"
    STATUS_EXPIRED = "expired"
    STATUS_REVOKED = "revoked"

    def __init__(
        self,
        event_store: EventStore,
        *,
        clock: Callable[[], datetime] | None = None,
        approval_ttl_seconds: int = 300,
        fault_port: FaultPort = NO_OP_FAULT_PORT,
    ) -> None:
        if not hasattr(event_store, "append_batch") or not hasattr(
            event_store, "read_stream",
        ):
            raise TypeError("event_store must implement EventStore")
        if (
            not isinstance(approval_ttl_seconds, int)
            or isinstance(approval_ttl_seconds, bool)
            or not 1 <= approval_ttl_seconds <= 86_400
        ):
            raise ValueError("approval_ttl_seconds must be in 1..86400")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or None")
        self._store = event_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ttl = timedelta(seconds=approval_ttl_seconds)
        if not callable(getattr(fault_port, "hit", None)):
            raise TypeError("fault_port must implement FaultPort")
        self._fault_port = fault_port
        self._tickets: dict[UUID, AuthorizedLaunchTicket] = {}

    # -- public planning ---------------------------------------------------

    def plan_start(
        self,
        identity: McpLaunchIdentity,
        *,
        principal_id: str,
        scope: str,
        decision: str,
    ) -> ActivationView:
        # Resolve one server launch against policy; never spawns.  An
        # existing effective grant is returned unchanged; a duplicate ASK is
        # idempotent; allow auto-grants with an authorization fact; deny
        # writes the denial and raises.
        request_id = self._request_id(
            identity.server_id, identity.config_digest, principal_id,
        )
        view = self._reconstruct(request_id)
        if view is not None:
            if view.status == self.STATUS_GRANTED and not self._expired(view):
                return view
            if view.status == self.STATUS_DENIED:
                raise McpActivationError("mcp_process_denied")
        now = self._now()
        if decision == "allow":
            self._append_activation(
                request_id,
                "mcp.activation-granted.v1",
                {
                    "request_id": str(request_id),
                    "server_id": identity.server_id,
                    "launch_identity_digest": identity.config_digest,
                    "execution_profile": identity.execution_profile,
                    "principal_id": principal_id,
                    "scope": scope,
                    "approver_principal_id": "policy",
                    "reason": "policy_allow",
                    "expires_at": (now + self._ttl).isoformat(),
                },
                -1 if view is None else view.version,
            )
            return self._require_view(request_id)
        if decision == "deny":
            self._append_activation(
                request_id,
                "mcp.activation-denied.v1",
                {
                    "request_id": str(request_id),
                    "server_id": identity.server_id,
                    "launch_identity_digest": identity.config_digest,
                    "execution_profile": identity.execution_profile,
                    "principal_id": principal_id,
                    "scope": scope,
                    "reason": "policy_denied",
                },
                -1 if view is None else view.version,
            )
            raise McpActivationError("mcp_process_denied")
        if view is not None and view.status == self.STATUS_REQUESTED:
            return view
        self._append_activation(
            request_id,
            "mcp.activation-requested.v1",
            {
                "schema_version": 1,
                "request_id": str(request_id),
                "server_id": identity.server_id,
                "launch_identity_digest": identity.config_digest,
                "execution_profile": identity.execution_profile,
                "principal_id": principal_id,
                "scope": scope,
                "requested_at": now.isoformat(),
            },
            -1 if view is None else view.version,
        )
        return self._require_view(request_id)

    def resolve_activation(
        self,
        request_id: UUID,
        approved: bool,
        *,
        approver_principal_id: str,
    ) -> ActivationView:
        # Approve/deny ONE exact request; the grant is identity-bound.  Only
        # writes the grant/denial - never auto-resumes a turn.
        if not isinstance(request_id, UUID):
            raise TypeError("request_id must be UUID")
        if type(approved) is not bool:
            raise TypeError("approved must be bool")
        view = self._require_view(request_id)
        if view.status != self.STATUS_REQUESTED:
            # A committed operator approval may have lost its response. Replay
            # only the identical current decision; never renew TTL, reinstate
            # a revoked grant, or treat a policy grant as an operator receipt.
            events = self._read_all(self._activation_stream(request_id))
            latest = events[-1]
            if (approved and view.status == self.STATUS_GRANTED
                    and latest.stream_version == view.version
                    and latest.event_type == "mcp.activation-granted.v1"
                    and latest.payload.get("reason") == "operator_approved"
                    and latest.payload.get("approver_principal_id") == approver_principal_id):
                return view
            raise McpActivationError("activation_already_resolved")
        now = self._now()
        if approved:
            self._append_activation(
                request_id,
                "mcp.activation-granted.v1",
                {
                    "request_id": str(request_id),
                    "server_id": view.server_id,
                    "launch_identity_digest": view.launch_identity_digest,
                    "execution_profile": view.execution_profile,
                    "principal_id": view.principal_id,
                    "scope": view.scope,
                    "approver_principal_id": approver_principal_id,
                    "reason": "operator_approved",
                    "expires_at": (now + self._ttl).isoformat(),
                },
                view.version,
            )
        else:
            self._append_activation(
                request_id,
                "mcp.activation-denied.v1",
                {
                    "request_id": str(request_id),
                    "server_id": view.server_id,
                    "launch_identity_digest": view.launch_identity_digest,
                    "execution_profile": view.execution_profile,
                    "principal_id": view.principal_id,
                    "scope": view.scope,
                    "reason": "operator_denied",
                },
                view.version,
            )
        return self._require_view(request_id)

    def pending_requests(self) -> tuple[ActivationView, ...]:
        # Pending mcp_process_start ASKs for status/approve/deny APIs.
        requested_ids: set[UUID] = set()
        cursor = 0
        while True:
            page = self._store.read_all(after_position=cursor, limit=500)
            for event in page:
                if event.event_type == "mcp.activation-requested.v1":
                    try:
                        raw = event.payload["request_id"]
                        requested_ids.add(UUID(raw))
                    except (KeyError, TypeError, ValueError):
                        continue
            if len(page) < 500:
                break
            cursor = page[-1].global_position
        pending: list[ActivationView] = []
        for request_id in sorted(requested_ids, key=str):
            view = self._reconstruct(request_id)
            if view is not None and view.status == self.STATUS_REQUESTED:
                pending.append(view)
        return tuple(pending)

    # -- allocation --------------------------------------------------------

    def intend(
        self,
        view: ActivationView,
    ) -> AllocationIntent:
        # Linearize intent before any OS create (mcp.process-intended).
        if view.status != self.STATUS_GRANTED or self._expired(view):
            raise McpActivationError("mcp_no_effective_grant")
        activation_stream = self._activation_stream(view.request_id)
        history = self._read_all(activation_stream)
        head = -1 if not history else history[-1].stream_version
        if head != view.version or history[-1].event_type != "mcp.activation-granted.v1":
            raise McpActivationError("mcp_no_effective_grant")
        attempt = sum(
            1
            for event in self._store.read_all(after_position=0, limit=10_000)
            if event.event_type == "mcp.process-intended.v1"
            and event.payload.get("request_id") == str(view.request_id)
        )
        allocation_id = uuid5(
            view.request_id, f"allocation:{attempt}",
        )
        allocation_stream = self._allocation_stream(allocation_id)
        event = self._event(
            "mcp-process-intend",
            "mcp.process-intended.v1",
            {
                "request_id": str(view.request_id),
                "allocation_id": str(allocation_id),
                "grant_digest": view.launch_identity_digest,
                "grant_version": view.version,
                "launch_identity_digest": view.launch_identity_digest,
                "execution_profile": view.execution_profile,
                "principal_id": view.principal_id,
                "attempt": attempt,
                "expires_at": view.expires_at.isoformat()
                if view.expires_at is not None
                else None,
            },
            view.request_id,
        )
        command_id = event.metadata.command_id
        fingerprint = dict(
            action="intend",
            request_id=str(view.request_id),
            allocation_id=str(allocation_id),
            attempt=attempt,
        )
        self._store.append_batch(
            (StreamWrite(allocation_stream, -1, (event,)),),
            idempotency_key=command_id,
            request_fingerprint=_canonical_json(fingerprint),
            preconditions=(
                StreamPrecondition(
                    activation_stream,
                    view.version,
                    "mcp.activation-granted.v1",
                    {
                        "request_id": str(view.request_id),
                        "launch_identity_digest": view.launch_identity_digest,
                        "principal_id": view.principal_id,
                    },
                ),
            ),
        )
        self._fault_port.hit(
            FaultPoint.S4_ALLOCATION_AFTER_INTENT_COMMIT,
            {"request_id": str(view.request_id),
             "allocation_id": str(allocation_id), "attempt": attempt},
        )
        return AllocationIntent(
            view.request_id, allocation_id, attempt, view.version,
        )


    def claim(
        self,
        intent: AllocationIntent,
        view: ActivationView,
        *,
        principal_id: str,
    ) -> AuthorizedLaunchTicket:
        # Atomically claim one allocation against the exact grant: the batch
        # writes mcp.process-claimed.v1 to the allocation stream AND applies
        # an exact precondition on the activation stream; any drift means
        # zero writes and zero launcher calls.
        allocation_stream = self._allocation_stream(intent.allocation_id)
        claim_epoch = 1
        command_document = {
            "action": "claim",
            "request_id": str(intent.request_id),
            "allocation_id": str(intent.allocation_id),
            "claim_epoch": claim_epoch,
            "launch_identity_digest": view.launch_identity_digest,
            "principal_id": principal_id,
        }
        fingerprint = _canonical_json(command_document)
        command_id = uuid5(
            NAMESPACE_URL,
            "koawa-mcp:claim:" + str(intent.request_id) + ":"
            + str(intent.allocation_id) + ":" + _sha256(fingerprint),
        )
        claim_token = uuid5(command_id, "claim-token")
        nonce = uuid5(command_id, "nonce")
        claimed = self._event(
            "mcp-process-claim",
            "mcp.process-claimed.v1",
            {
                "request_id": str(intent.request_id),
                "allocation_id": str(intent.allocation_id),
                "claim_token": str(claim_token),
                "claim_epoch": claim_epoch,
                "launch_identity_digest": view.launch_identity_digest,
                "execution_profile": view.execution_profile,
                "principal_id": principal_id,
                "grant_digest": view.launch_identity_digest,
            },
            intent.request_id,
            command_id=command_id,
        )
        precondition = StreamPrecondition(
            self._activation_stream(intent.request_id),
            intent.activation_stream_version,
            "mcp.activation-granted.v1",
            {
                "request_id": str(intent.request_id),
                "launch_identity_digest": view.launch_identity_digest,
                "principal_id": principal_id,
            },
        )
        try:
            self._store.append_batch(
                (
                    StreamWrite(
                        allocation_stream, 0, (claimed,),
                    ),
                ),
                idempotency_key=command_id,
                request_fingerprint=fingerprint,
                preconditions=(precondition,),
            )
        except WrongExpectedVersion:
            raise McpActivationError("mcp_claim_rejected") from None
        self._fault_port.hit(
            FaultPoint.S4_ALLOCATION_AFTER_CLAIM_COMMIT,
            {"request_id": str(intent.request_id),
             "allocation_id": str(intent.allocation_id),
             "claim_epoch": claim_epoch},
        )
        not_after = view.expires_at or (self._now() + self._ttl)
        ticket = AuthorizedLaunchTicket(
            request_id=intent.request_id,
            grant_stream_version=intent.activation_stream_version,
            grant_digest=view.launch_identity_digest,
            allocation_id=intent.allocation_id,
            allocation_stream_version=1,
            claim_epoch=claim_epoch,
            claim_token=claim_token,
            nonce=nonce,
            launch_identity_digest=view.launch_identity_digest,
            not_after=not_after,
        )
        self._tickets[ticket.nonce] = ticket
        return ticket

    def consume_ticket(self, ticket: AuthorizedLaunchTicket) -> None:
        # Atomically consume one ticket; at most one OS create per ticket.
        # Replay, expiry or allocation drift yield zero OS creates.
        if not isinstance(ticket, AuthorizedLaunchTicket):
            raise TypeError("ticket must be AuthorizedLaunchTicket")
        if self._tickets.pop(ticket.nonce, None) is None:
            raise McpActivationError("mcp_ticket_replayed_or_unknown")
        if self._now() >= ticket.not_after:
            raise McpActivationError("mcp_ticket_expired")
        events = self._read_all(self._allocation_stream(ticket.allocation_id))
        if not events:
            raise McpActivationError("mcp_claim_missing")
        latest = events[-1]
        if (
            latest.event_type != "mcp.process-claimed.v1"
            or latest.payload.get("claim_token") != str(ticket.claim_token)
            or int(latest.payload.get("claim_epoch", -1)) != ticket.claim_epoch
        ):
            raise McpActivationError("mcp_allocation_drift")

    # -- allocation records ------------------------------------------------

    def _append_allocation(
        self,
        ticket: AuthorizedLaunchTicket,
        event_type: str,
        extra: Mapping[str, Any],
        *,
        actor: str,
    ) -> None:
        stream = self._allocation_stream(ticket.allocation_id)
        events = self._read_all(stream)
        head = -1 if not events else events[-1].stream_version
        payload: dict[str, Any] = {
            "request_id": str(ticket.request_id),
            "allocation_id": str(ticket.allocation_id),
            "claim_token": str(ticket.claim_token),
            "launch_identity_digest": ticket.launch_identity_digest,
        }
        payload.update(extra)
        event = self._event(actor, event_type, payload, ticket.request_id)
        command_id = event.metadata.command_id
        self._store.append_batch(
            (StreamWrite(stream, head, (event,)),),
            idempotency_key=command_id,
            request_fingerprint=_canonical_json({
                "action": actor,
                "allocation_id": str(ticket.allocation_id),
                "event_type": event_type,
                "claim_token": str(ticket.claim_token),
            }),
        )
        point = {
            "mcp.process-started.v1": FaultPoint.S4_ALLOCATION_AFTER_STARTED_COMMIT,
            "mcp.process-ready.v1": FaultPoint.S4_ALLOCATION_AFTER_READY_COMMIT,
        }.get(event_type)
        if point is not None:
            self._fault_port.hit(
                point,
                {"allocation_id": str(ticket.allocation_id),
                 "stream_version": head + 1},
            )

    def record_started(self, ticket: AuthorizedLaunchTicket) -> None:
        self._fault_port.hit(
            FaultPoint.S4_LAUNCH_AFTER_EXTERNAL_CREATE_BEFORE_STARTED,
            {"allocation_id": str(ticket.allocation_id),
             "claim_epoch": ticket.claim_epoch},
        )
        self._append_allocation(
            ticket, "mcp.process-started.v1", {}, actor="mcp-process-start",
        )

    def record_ready(self, ticket: AuthorizedLaunchTicket) -> None:
        self._append_allocation(
            ticket, "mcp.process-ready.v1", {}, actor="mcp-process-ready",
        )

    def record_stopped(
        self, ticket: AuthorizedLaunchTicket, *, reason: str = "closed",
    ) -> None:
        self._fault_port.hit(
            FaultPoint.S4_MCP_CLOSE_AFTER_TERMINATE_BEFORE_STOPPED,
            {"allocation_id": str(ticket.allocation_id),
             "claim_epoch": ticket.claim_epoch},
        )
        self._append_allocation(
            ticket,
            "mcp.process-stopped.v1",
            {"reason": reason},
            actor="mcp-process-stop",
        )

    def record_failed_before_start(
        self, ticket: AuthorizedLaunchTicket, *, reason: str,
    ) -> None:
        self._append_allocation(
            ticket,
            "mcp.process-failed-before-start.v1",
            {"reason": reason},
            actor="mcp-process-fail-before-start",
        )

    def record_outcome_unknown(
        self, ticket: AuthorizedLaunchTicket, *, reason: str,
    ) -> None:
        self._append_allocation(
            ticket,
            "mcp.process-outcome-unknown.v1",
            {"reason": reason},
            actor="mcp-process-outcome-unknown",
        )

    def record_start_observed(
        self, ticket: AuthorizedLaunchTicket, *, evidence_digest: str,
    ) -> None:
        # Recovery: a started ACK was lost but the external process exists.
        # Uses the same state transition as started and immediately
        # converges to STOPPED after reaping (doc §8.5).
        self._append_allocation(
            ticket,
            "mcp.process-start-observed.v1",
            {
                "evidence_kind": "process_identity",
                "evidence_digest": evidence_digest,
            },
            actor="mcp-process-start-observed",
        )
        self._append_allocation(
            ticket,
            "mcp.process-stopped.v1",
            {"reason": "observed_then_stopped"},
            actor="mcp-process-stop",
        )

    def record_outcome_resolved(
        self,
        ticket: AuthorizedLaunchTicket,
        *,
        resolved_state: str,
        evidence_digest: str,
        reconciler_principal_id: str,
    ) -> None:
        # Resolve an OUTCOME_UNKNOWN with reproducible evidence only.
        self._append_allocation(
            ticket,
            "mcp.process-outcome-resolved.v1",
            {
                "resolved_state": resolved_state,
                "evidence_kind": "typed",
                "evidence_digest": evidence_digest,
                "reconciler_principal_id": reconciler_principal_id,
            },
            actor="mcp-process-outcome-resolved",
        )

    # -- internals ---------------------------------------------------------

    def _request_id(
        self, server_id: str, launch_digest: str, principal_id: str,
    ) -> UUID:
        namespace = uuid5(NAMESPACE_URL, _ACTIVATION_NAME)
        return uuid5(
            namespace,
            f"{server_id}:{launch_digest}:{principal_id}",
        )

    def _activation_stream(self, request_id: UUID) -> StreamId:
        return StreamId("mcp-activation", request_id)

    def _allocation_stream(self, allocation_id: UUID) -> StreamId:
        return StreamId("mcp-allocation", allocation_id)

    def _expired(self, view: ActivationView) -> bool:
        return (
            view.expires_at is not None
            and self._now() >= view.expires_at
        )

    def _require_view(self, request_id: UUID) -> ActivationView:
        view = self._reconstruct(request_id)
        if view is None:
            raise McpActivationError("activation_request_missing")
        return view

    def _reconstruct(self, request_id: UUID) -> ActivationView | None:
        events = self._read_all(self._activation_stream(request_id))
        if not events:
            return None
        requested = next(
            (
                event
                for event in events
                if event.event_type == "mcp.activation-requested.v1"
            ),
            events[0],
        )
        latest = events[-1]
        payload = requested.payload
        server_id = str(payload.get("server_id", ""))
        launch_digest = str(payload.get("launch_identity_digest", ""))
        execution_profile = str(payload.get("execution_profile", "legacy"))
        principal_id = str(payload.get("principal_id", ""))
        scope = str(payload.get("scope", "mcp.use"))
        status = self.STATUS_REQUESTED
        expires_at: datetime | None = None
        for event in events:
            event_type = event.event_type
            if event_type == "mcp.activation-requested.v1":
                status = self.STATUS_REQUESTED
            elif event_type == "mcp.activation-granted.v1":
                status = self.STATUS_GRANTED
            elif event_type == "mcp.activation-denied.v1":
                status = self.STATUS_DENIED
            elif event_type == "mcp.activation-expired.v1":
                status = self.STATUS_EXPIRED
            elif event_type == "mcp.activation-revoked.v1":
                status = self.STATUS_REVOKED
        if status == self.STATUS_GRANTED:
            for event in reversed(events):
                if event.event_type == "mcp.activation-granted.v1":
                    raw_expiry = event.payload.get("expires_at")
                    if isinstance(raw_expiry, str):
                        try:
                            expires_at = datetime.fromisoformat(raw_expiry)
                        except ValueError:
                            expires_at = None
                    break
        return ActivationView(
            request_id=request_id,
            server_id=server_id,
            launch_identity_digest=launch_digest,
            execution_profile=execution_profile,
            principal_id=principal_id,
            scope=scope,
            status=status,
            version=latest.stream_version,
            expires_at=expires_at,
        )

    def _append_activation(
        self,
        request_id: UUID,
        event_type: str,
        payload: Mapping[str, Any],
        expected_version: int,
    ) -> None:
        if event_type == "mcp.activation-granted.v1":
            self._fault_port.hit(
                FaultPoint.S4_ACTIVATION_BEFORE_GRANT_APPEND,
                {"request_id": str(request_id),
                 "expected_version": expected_version},
            )
        event = self._event("mcp-activation", event_type, dict(payload), request_id)
        command_id = event.metadata.command_id
        stream = self._activation_stream(request_id)
        self._store.append_batch(
            (StreamWrite(stream, expected_version, (event,)),),
            idempotency_key=command_id,
            request_fingerprint=_canonical_json({
                "action": "activation",
                "request_id": str(request_id),
                "event_type": event_type,
                "payload": dict(payload),
                "expected_version": expected_version,
            }),
        )
        point = {
            "mcp.activation-requested.v1": FaultPoint.S4_ACTIVATION_AFTER_REQUEST_COMMIT,
            "mcp.activation-granted.v1": FaultPoint.S4_ACTIVATION_AFTER_GRANT_COMMIT,
        }.get(event_type)
        if point is not None:
            self._fault_port.hit(
                point,
                {"request_id": str(request_id),
                 "stream_version": expected_version + 1},
            )

    def _event(
        self, slot: str, event_type: str, payload: Mapping[str, Any], request_id: UUID,
        command_id: UUID | None = None,
    ) -> NewEvent:
        document = dict(payload)
        command_document = {
            "action": slot,
            "request_id": str(request_id),
            "event_type": event_type,
            "payload": document,
        }
        resolved_command = command_id
        if resolved_command is None:
            resolved_command = uuid5(
                NAMESPACE_URL,
                "koawa-mcp:" + _sha256(_canonical_json(command_document)),
            )
        occurred_at = self._now()
        return NewEvent(
            uuid5(resolved_command, "event:" + slot),
            event_type,
            1,
            occurred_at,
            document,
            EventMetadata(
                resolved_command,
                request_id,
                thread_id=None,
                turn_id=None,
                run_id=None,
                actor="mcp-activation",
            ),
        )

    def _read_all(self, stream: StreamId) -> tuple:
        values = []
        cursor = -1
        while True:
            page = self._store.read_stream(
                stream, after_version=cursor, limit=500,
            )
            values.extend(page)
            if len(page) < 500:
                return tuple(values)
            cursor = page[-1].stream_version

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)


__all__ = [
    "ActivationService",
    "ActivationView",
    "AllocationIntent",
    "AuthorizedLaunchTicket",
    "CodeArtifactIdentity",
    "EnvironmentIdentity",
    "ExecutableIdentity",
    "McpActivationError",
    "McpLaunchIdentity",
    "ReadOnlyMountIdentity",
    "StagedLaunchPlan",
    "process_start_scope",
    "resolve_launch_identity",
    "stage_code_artifacts",
]
