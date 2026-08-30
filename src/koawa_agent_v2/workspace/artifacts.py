"""I7 content-addressed, JSON-only artifact packages and durable references."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from ..agents.graph import AgentError
from ..control.event_store import EventMetadata, NewEvent, StreamId, StreamWrite
from .subprocesses import run_bounded


PACKAGE_SCHEMA_VERSION = 2
MAX_PACKAGE_JSON_BYTES = 96 * 1024 * 1024
MAX_PACKAGE_CONTENT_BYTES = 64 * 1024 * 1024
MAX_ENTRY_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 10_000


@dataclass(frozen=True, slots=True)
class TestEvidenceRef:
    stream_id: StreamId
    stream_version: int
    event_id: UUID
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class ArtifactPackageRef:
    package_ref: str
    package_digest: str
    package_json_bytes: int


@dataclass(frozen=True, slots=True)
class ArtifactPackageEntry:
    path_bytes: bytes
    kind: str
    mode: int
    content_or_target: bytes
    content_sha256: str

    @classmethod
    def create(
        cls, *, path_bytes: bytes, kind: str, mode: int, content_or_target: bytes
    ) -> "ArtifactPackageEntry":
        if kind not in {"untracked_file", "symlink", "gitlink"}:
            raise AgentError("artifact_package_entry_kind_invalid")
        _validate_path(path_bytes)
        if not isinstance(mode, int) or isinstance(mode, bool) or mode < 0:
            raise AgentError("artifact_package_entry_mode_invalid")
        if len(content_or_target) > MAX_ENTRY_BYTES:
            raise AgentError("artifact_package_entry_too_large")
        return cls(path_bytes, kind, mode, content_or_target, _digest(content_or_target))

    def document(self) -> dict[str, object]:
        return {
            "path_bytes_base64": _b64(self.path_bytes),
            "kind": self.kind,
            "mode": self.mode,
            "content_or_target_base64": _b64(self.content_or_target),
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class ArtifactPackageV2:
    repository_identity_digest: str
    base_commit: str
    tracked_binary_patch: bytes
    entries: tuple[ArtifactPackageEntry, ...]
    prestate_digest: str
    poststate_manifest_digest: str

    def document(self) -> dict[str, object]:
        entries = tuple(sorted(self.entries, key=lambda item: item.path_bytes))
        if entries != self.entries:
            raise AgentError("artifact_package_entries_not_sorted")
        if len(entries) > MAX_PACKAGE_ENTRIES:
            raise AgentError("artifact_package_entry_limit_exceeded")
        total = len(self.tracked_binary_patch) + sum(
            len(item.content_or_target) for item in entries
        )
        if total > MAX_PACKAGE_CONTENT_BYTES:
            raise AgentError("artifact_package_content_limit_exceeded")
        _sha(self.repository_identity_digest, "repository_identity_digest")
        _commit(self.base_commit)
        _sha(self.prestate_digest, "prestate_digest")
        _sha(self.poststate_manifest_digest, "poststate_manifest_digest")
        return {
            "package_schema_version": PACKAGE_SCHEMA_VERSION,
            "repository_identity_digest": self.repository_identity_digest,
            "base_commit": self.base_commit,
            "tracked_binary_patch_base64": _b64(self.tracked_binary_patch),
            "entries": [entry.document() for entry in entries],
            "prestate_digest": self.prestate_digest,
            "poststate_manifest_digest": self.poststate_manifest_digest,
        }


@dataclass(frozen=True, slots=True)
class ArtifactV2:
    artifact_id: UUID
    agent_id: UUID
    run_id: UUID
    repository_identity_digest: str
    base_commit: str
    repo_prestate_digest: str
    package: ArtifactPackageRef
    diff_digest: str
    working_tree_content_digest: str
    test_evidence_ref: TestEvidenceRef
    sandbox_image_digest: str
    sandbox_profile_digest: str
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        agent_id: UUID,
        run_id: UUID,
        repository_identity_digest: str,
        base_commit: str,
        repo_prestate_digest: str,
        package: ArtifactPackageRef,
        diff_digest: str,
        working_tree_content_digest: str,
        test_evidence_ref: TestEvidenceRef,
        sandbox_image_digest: str,
        sandbox_profile_digest: str,
        created_at: datetime | None = None,
    ) -> "ArtifactV2":
        for value, name in (
            (repository_identity_digest, "repository_identity_digest"),
            (repo_prestate_digest, "repo_prestate_digest"),
            (diff_digest, "diff_digest"),
            (working_tree_content_digest, "working_tree_content_digest"),
            (sandbox_image_digest.removeprefix("sha256:"), "sandbox_image_digest"),
            (sandbox_profile_digest, "sandbox_profile_digest"),
        ):
            _sha(value, name)
        _commit(base_commit)
        artifact_id = uuid5(
            NAMESPACE_URL, f"koawa-v2:artifact:{run_id}:{package.package_digest}"
        )
        return cls(
            artifact_id, agent_id, run_id, repository_identity_digest, base_commit,
            repo_prestate_digest, package, diff_digest, working_tree_content_digest,
            test_evidence_ref, sandbox_image_digest, sandbox_profile_digest,
            (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc),
        )


class ArtifactPackageStore:
    """Atomic package storage with optional event-backed pin/release fences."""

    def __init__(self, state_root: Path, *, event_store: Any | None = None) -> None:
        self.root = Path(state_root).resolve() / "artifact-packages"
        self.root.mkdir(parents=True, exist_ok=True)
        self._event_store = event_store

    def put(self, package: ArtifactPackageV2) -> ArtifactPackageRef:
        document = package.document()
        encoded = _canonical(document)
        if len(encoded) > MAX_PACKAGE_JSON_BYTES:
            raise AgentError("artifact_package_json_limit_exceeded")
        digest = _digest(encoded)
        target = self.root / f"{digest}.json"
        if target.exists():
            if target.read_bytes() != encoded:
                raise AgentError("artifact_package_digest_collision")
        else:
            descriptor, temporary = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=self.root)
            try:
                if os.name != "nt":
                    os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                _fsync_directory(self.root)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return ArtifactPackageRef(f"sha256:{digest}", digest, len(encoded))

    def load(self, reference: ArtifactPackageRef) -> ArtifactPackageV2:
        _validate_ref(reference)
        target = self.root / f"{reference.package_digest}.json"
        try:
            encoded = target.read_bytes()
        except OSError:
            raise AgentError("artifact_package_missing") from None
        if len(encoded) != reference.package_json_bytes or _digest(encoded) != reference.package_digest:
            raise AgentError("artifact_package_tampered")
        try:
            document = json.loads(encoded.decode("utf-8", "strict"))
        except (UnicodeError, json.JSONDecodeError):
            raise AgentError("artifact_package_invalid") from None
        if _canonical(document) != encoded:
            raise AgentError("artifact_package_noncanonical")
        return _parse_package(document)

    def pin(self, *, artifact_id: UUID, package: ArtifactPackageRef, command_id: UUID) -> None:
        if self._event_store is None:
            raise AgentError("artifact_package_event_store_required")
        _validate_ref(package)
        event = NewEvent(
            uuid5(command_id, "event:artifact-package-pinned"),
            "workspace.artifact-package-pinned.v1", 1, datetime.now(timezone.utc),
            {"artifact_id": str(artifact_id), "package_ref": package.package_ref,
             "package_digest": package.package_digest, "package_json_bytes": package.package_json_bytes},
            EventMetadata(command_id, command_id, actor="artifact-package-store"),
        )
        self._event_store.append_batch(
            (StreamWrite(StreamId("workspace-artifact", artifact_id), -1, (event,)),),
            idempotency_key=command_id,
            request_fingerprint=_canonical(dict(event.payload)).decode("utf-8"),
        )

    def release(self, *, artifact_id: UUID, expected_version: int, command_id: UUID) -> None:
        if self._event_store is None:
            raise AgentError("artifact_package_event_store_required")
        event = NewEvent(
            uuid5(command_id, "event:artifact-package-released"),
            "workspace.artifact-package-released.v1", 1, datetime.now(timezone.utc),
            {"artifact_id": str(artifact_id)},
            EventMetadata(command_id, command_id, actor="artifact-package-store"),
        )
        self._event_store.append_batch(
            (StreamWrite(StreamId("workspace-artifact", artifact_id), expected_version, (event,)),),
            idempotency_key=command_id,
            request_fingerprint=_canonical(dict(event.payload)).decode("utf-8"),
        )

    def collect_unpinned(self) -> tuple[str, ...]:
        """Delete only packages proven to have no active durable pin."""
        if self._event_store is None:
            raise AgentError("artifact_package_event_store_required")
        active: dict[UUID, str] = {}
        cursor = 0
        while True:
            page = self._event_store.read_all(after_position=cursor, limit=500)
            for event in page:
                if event.event_type == "workspace.artifact-package-pinned.v1":
                    try:
                        artifact_id = UUID(event.payload["artifact_id"])
                        digest = event.payload["package_digest"]
                    except (KeyError, TypeError, ValueError):
                        raise AgentError("artifact_package_pin_corrupt") from None
                    if (
                        not isinstance(digest, str)
                        or len(digest) != 64
                        or any(c not in "0123456789abcdef" for c in digest)
                    ):
                        raise AgentError("artifact_package_pin_corrupt")
                    active[artifact_id] = digest
                elif event.event_type == "workspace.artifact-package-released.v1":
                    try:
                        artifact_id = UUID(event.payload["artifact_id"])
                    except (KeyError, TypeError, ValueError):
                        raise AgentError("artifact_package_pin_corrupt") from None
                    active.pop(artifact_id, None)
            if len(page) < 500:
                break
            cursor = page[-1].global_position
        protected = frozenset(active.values())
        deleted: list[str] = []
        for target in sorted(self.root.iterdir(), key=lambda item: item.name):
            name = target.name
            if (
                not target.is_file()
                or len(name) != 69
                or not name.endswith(".json")
                or any(c not in "0123456789abcdef" for c in name[:64])
            ):
                continue
            digest = name[:64]
            if digest in protected:
                continue
            target.unlink()
            deleted.append("sha256:" + digest)
        if deleted:
            _fsync_directory(self.root)
        return tuple(deleted)


def package_from_snapshot(
    *, repository_identity_digest: str, base_commit: str,
    tracked_binary_patch: bytes, entries: tuple[ArtifactPackageEntry, ...],
    prestate_digest: str, poststate_manifest_digest: str,
) -> ArtifactPackageV2:
    return ArtifactPackageV2(
        repository_identity_digest, base_commit, tracked_binary_patch,
        tuple(sorted(entries, key=lambda item: item.path_bytes)),
        prestate_digest, poststate_manifest_digest,
    )


def apply_package(
    package: ArtifactPackageV2,
    *,
    worktree: Path,
    git_binary: str,
) -> None:
    """Apply one verified package without allowing paths outside the worktree."""

    root = Path(worktree).resolve()
    if package.tracked_binary_patch:
        checked = run_bounded(
            [git_binary, "-c", "core.hooksPath=", "-C", str(root),
             "apply", "--check", "--binary", "-"],
            cwd=root, environment=None, timeout=60,
            output_limit=4 * 1024 * 1024,
            failure_code="artifact_package_apply_failed",
            output_limit_code="artifact_package_apply_output_limit",
            input_bytes=package.tracked_binary_patch,
        )
        if checked.returncode:
            raise AgentError("artifact_package_apply_conflict")
        applied = run_bounded(
            [git_binary, "-c", "core.hooksPath=", "-C", str(root),
             "apply", "--binary", "-"],
            cwd=root, environment=None, timeout=60,
            output_limit=4 * 1024 * 1024,
            failure_code="artifact_package_apply_failed",
            output_limit_code="artifact_package_apply_output_limit",
            input_bytes=package.tracked_binary_patch,
        )
        if applied.returncode:
            raise AgentError("artifact_package_apply_failed")
    for entry in package.entries:
        target = _safe_target(root, entry.path_bytes)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise AgentError("artifact_package_entry_conflict")
        if entry.kind == "untracked_file":
            target.write_bytes(entry.content_or_target)
            if os.name != "nt":
                target.chmod(entry.mode)
        elif entry.kind == "symlink":
            try:
                os.symlink(os.fsdecode(entry.content_or_target), target)
            except OSError:
                raise AgentError("artifact_package_symlink_failed") from None
        elif entry.kind == "gitlink":
            raise AgentError("artifact_package_gitlink_requires_checkout")
        else:
            raise AgentError("artifact_package_entry_kind_invalid")


def _safe_target(root: Path, path_bytes: bytes) -> Path:
    _validate_path(path_bytes)
    relative = Path(os.fsdecode(path_bytes.replace(b"/", os.sep.encode())))
    candidate = root / relative
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise AgentError("artifact_package_path_symlink")
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        raise AgentError("artifact_package_path_escape") from None
    return candidate


def _parse_package(value: object) -> ArtifactPackageV2:
    if not isinstance(value, dict) or set(value) != {
        "package_schema_version", "repository_identity_digest", "base_commit",
        "tracked_binary_patch_base64", "entries", "prestate_digest",
        "poststate_manifest_digest",
    }:
        raise AgentError("artifact_package_schema_invalid")
    if value["package_schema_version"] != PACKAGE_SCHEMA_VERSION:
        raise AgentError("artifact_package_schema_invalid")
    raw_entries = value["entries"]
    if not isinstance(raw_entries, list):
        raise AgentError("artifact_package_schema_invalid")
    entries: list[ArtifactPackageEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict) or set(item) != {
            "path_bytes_base64", "kind", "mode", "content_or_target_base64", "content_sha256"
        }:
            raise AgentError("artifact_package_schema_invalid")
        path = _unb64(item["path_bytes_base64"])
        content = _unb64(item["content_or_target_base64"])
        entry = ArtifactPackageEntry.create(
            path_bytes=path, kind=item["kind"], mode=item["mode"], content_or_target=content
        )
        if entry.content_sha256 != item["content_sha256"]:
            raise AgentError("artifact_package_content_digest_mismatch")
        entries.append(entry)
    package = package_from_snapshot(
        repository_identity_digest=value["repository_identity_digest"],
        base_commit=value["base_commit"],
        tracked_binary_patch=_unb64(value["tracked_binary_patch_base64"]),
        entries=tuple(entries), prestate_digest=value["prestate_digest"],
        poststate_manifest_digest=value["poststate_manifest_digest"],
    )
    package.document()
    return package


def _validate_ref(value: ArtifactPackageRef) -> None:
    if not isinstance(value, ArtifactPackageRef):
        raise TypeError("reference must be ArtifactPackageRef")
    _sha(value.package_digest, "package_digest")
    if value.package_ref != f"sha256:{value.package_digest}" or value.package_json_bytes < 1:
        raise AgentError("artifact_package_ref_invalid")


def _validate_path(value: bytes) -> None:
    if not isinstance(value, bytes) or not value or len(value) > 4096:
        raise AgentError("artifact_package_path_invalid")
    normalized = value.replace(b"\\", b"/")
    if normalized.startswith(b"/") or b"\x00" in normalized or any(part in {b"", b".", b".."} for part in normalized.split(b"/")):
        raise AgentError("artifact_package_path_invalid")


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise AgentError("artifact_package_base64_invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        raise AgentError("artifact_package_base64_invalid") from None
    if _b64(decoded) != value:
        raise AgentError("artifact_package_base64_noncanonical")
    return decoded


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise AgentError(f"artifact_{name}_invalid")
    return value


def _commit(value: object) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        raise AgentError("artifact_base_commit_invalid")
    return value


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError):
        raise AgentError("artifact_package_json_invalid") from None


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ArtifactPackageEntry", "ArtifactPackageRef", "ArtifactPackageStore",
    "ArtifactPackageV2", "ArtifactV2", "TestEvidenceRef", "package_from_snapshot",
    "apply_package",
]
