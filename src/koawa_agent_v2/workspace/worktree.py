"""I7 effect-ledgered detached Git worktree orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from ..agents.graph import AgentError
from .content import repository_identity
from .effects import (
    WorkspaceEffectKind, WorkspaceEffectResultKind, WorkspaceEffectStore,
    WorkspaceEffectState, WorkspaceEffectResolvedState,
    workspace_effect_id, workspace_resource_nonce,
)
from .store import AgentWorkspaceStore
from .subprocesses import run_bounded


class WorktreeManager:
    """Brackets every physical add/remove with an exact workspace effect."""

    def __init__(
        self, store: AgentWorkspaceStore, *, repo_root: Path,
        git_binary: str | None = None, effect_store: WorkspaceEffectStore | None = None,
        owner_id: str = "worktree-manager",
    ) -> None:
        self.store = store
        self.repo_root = Path(repo_root).resolve()
        candidate = git_binary or shutil.which("git")
        if not candidate or not Path(candidate).resolve().is_file():
            raise AgentError("git_executable_unavailable")
        self.git_binary = str(Path(candidate).resolve())
        self.effects = effect_store or WorkspaceEffectStore(store.event_store)
        self.owner_id = owner_id
        self.repository_identity_digest = repository_identity(
            self.repo_root, git_binary=self.git_binary
        )

    def user_worktree_dirty(self) -> bool:
        return bool(self._git("status", "--porcelain=v2", "-z"))

    def create(
        self, agent_id: UUID, *, run_id: UUID, base_commit: str,
        branch: str | None = None, write_agent: bool,
        semantic_command_id: UUID | None = None,
    ) -> Path:
        with self._operation_lock():
            return self._create(agent_id, run_id=run_id, base_commit=base_commit,
                                branch=branch, write_agent=write_agent,
                                semantic_command_id=semantic_command_id)

    def _create(
        self, agent_id: UUID, *, run_id: UUID, base_commit: str,
        branch: str | None = None, write_agent: bool,
        semantic_command_id: UUID | None = None,
    ) -> Path:
        """Create a detached worktree; ``branch`` is ignored legacy input."""
        if write_agent and self.user_worktree_dirty():
            raise AgentError("dirty_user_worktree_requires_snapshot")
        semantic = semantic_command_id or uuid5(
            NAMESPACE_URL, f"koawa-v2:worktree-add:{agent_id}:{run_id}:{base_commit}"
        )
        effect_id = workspace_effect_id(WorkspaceEffectKind.WORKTREE_ADD, semantic)
        nonce = workspace_resource_nonce(effect_id)
        relative = Path(str(agent_id)) / str(run_id) / str(nonce)
        target = (self.store.managed_root / relative).resolve()
        self._safe_target(relative.as_posix())
        prior = self.effects.load(effect_id)
        intended = self.effects.intend(
            semantic_command_id=semantic, kind=WorkspaceEffectKind.WORKTREE_ADD,
            repository_identity_digest=self.repository_identity_digest,
            agent_id=agent_id, run_id=run_id, resource_ref=relative.as_posix(),
            base_digest=_sha(base_commit.encode()),
            input_digest=_doc_digest({"base_commit": base_commit, "detached": True}),
            precondition_digest=(prior.precondition_digest if prior else
                                 _doc_digest({"path_absent": not target.exists()})),
            expected_postcondition_digest=_doc_digest({"head": base_commit, "registered": True}),
        )
        if intended.record.state is not WorkspaceEffectState.INTENDED:
            recovered = self._reconcile(intended.record, base_commit)
            if recovered.state is WorkspaceEffectState.APPLIED:
                return target
            raise AgentError("workspace_outcome_unknown" if recovered.state is WorkspaceEffectState.OUTCOME_UNKNOWN
                             else "worktree_effect_not_applied")
        claimed = self.effects.claim(
            effect_id, expected_version=intended.record.version, owner_id=self.owner_id
        ).record
        try:
            self._git("worktree", "add", "--detach", str(target), base_commit)
            self._verify_active(target, base_commit)
        except AgentError:
            self._record_create_failure(claimed, target)
            raise
        applied = self.effects.record_applied(
            effect_id, expected_version=claimed.version,
            claim_epoch=claimed.claim_epoch, claim_token=claimed.claim_token,
            result_kind=WorkspaceEffectResultKind.SUCCESS, result_code="worktree_added",
            exit_code=0,
            postcondition_digest=_doc_digest({"head": base_commit, "registered": True}),
            evidence_digest=_doc_digest({"resource_nonce": str(nonce), "head": base_commit}),
            result={"resource_ref": relative.as_posix()},
        ).record
        record = self.store.allocate(
            agent_id, run_id=run_id, worktree_path=target, base_commit=base_commit,
            allocation_id=effect_id, resource_nonce=nonce, effect_id=effect_id,
            command_id=uuid5(semantic, "inventory-active"),
        )
        if record.effect_id != applied.effect_id:
            raise AgentError("workspace_projection_corrupt")
        return Path(record.worktree_path)

    def reap(
        self, agent_id: UUID, *, run_id: UUID, reason: str = "completed",
        semantic_command_id: UUID | None = None,
    ):
        with self._operation_lock():
            return self._reap(agent_id, run_id=run_id, reason=reason,
                              semantic_command_id=semantic_command_id)

    def _reap(
        self, agent_id: UUID, *, run_id: UUID, reason: str = "completed",
        semantic_command_id: UUID | None = None,
    ):
        current = self.store.load(agent_id, run_id=run_id)
        if current is None or current.legacy_unverified:
            raise AgentError("stale_workspace_fenced")
        target = Path(current.worktree_path)
        if not self.store._inside_managed(target.resolve()):
            raise AgentError("workspace_outside_managed_root")
        semantic = semantic_command_id or uuid5(
            current.allocation_id, f"worktree-remove:{reason}"
        )
        relative = target.relative_to(self.store.managed_root).as_posix()
        effect_id = workspace_effect_id(WorkspaceEffectKind.WORKTREE_REMOVE, semantic)
        self._safe_target(relative)
        prior = self.effects.load(effect_id)
        if current.state != "active" and prior is None:
            raise AgentError("stale_workspace_fenced")
        intended = self.effects.intend(
            semantic_command_id=semantic, kind=WorkspaceEffectKind.WORKTREE_REMOVE,
            repository_identity_digest=self.repository_identity_digest,
            agent_id=agent_id, run_id=run_id, resource_ref=relative,
            base_digest=_sha(current.base_commit.encode()),
            input_digest=_doc_digest({"allocation_id": str(current.allocation_id), "reason": reason}),
            precondition_digest=(prior.precondition_digest if prior else
                                 _doc_digest({"path_exists": target.exists()})),
            expected_postcondition_digest=_doc_digest({"path_absent": True, "registered": False}),
        )
        if intended.record.state is not WorkspaceEffectState.INTENDED:
            recovered = self._reconcile(intended.record, current.base_commit)
            if recovered.state is WorkspaceEffectState.APPLIED:
                return self.store.load(agent_id, run_id=run_id)
            raise AgentError("workspace_outcome_unknown")
        claimed = self.effects.claim(
            effect_id, expected_version=intended.record.version, owner_id=self.owner_id
        ).record
        self.store.mark_reap_pending(
            agent_id, run_id=run_id, effect_id=effect_id,
            command_id=uuid5(semantic, "inventory-reap-pending"),
        )
        try:
            self._git("worktree", "remove", "--force", str(target))
            if target.exists() or self._registered(target) or self._metadata_present(target):
                raise AgentError("worktree_remove_postcheck_failed")
        except AgentError:
            self.effects.record_outcome_unknown(
                effect_id, expected_version=claimed.version,
                claim_epoch=claimed.claim_epoch, claim_token=claimed.claim_token,
                uncertainty_code="worktree_remove_uncertain",
                evidence_digest=_doc_digest({"path_exists": target.exists(), "registered": self._registered(target)}),
            )
            self.store.mark_unknown(
                agent_id, run_id=run_id, effect_id=effect_id,
                command_id=uuid5(semantic, "inventory-unknown"),
            )
            raise
        self.effects.record_applied(
            effect_id, expected_version=claimed.version,
            claim_epoch=claimed.claim_epoch, claim_token=claimed.claim_token,
            result_kind=WorkspaceEffectResultKind.SUCCESS, result_code="worktree_removed",
            exit_code=0,
            postcondition_digest=_doc_digest({"path_absent": True, "registered": False}),
            evidence_digest=_doc_digest({"resource_nonce": str(current.resource_nonce), "path_absent": True}),
        )
        return self.store.mark_reaped(
            agent_id, run_id=run_id, effect_id=effect_id,
            command_id=uuid5(semantic, "inventory-reaped"),
        )

    def reconcile(self, effect_id: UUID, *, expected_version: int, base_commit: str):
        """Inspect one interrupted effect; never rerun an uncertain Git command."""
        with self._operation_lock():
            record = self.effects.load(effect_id)
            if record is None:
                raise AgentError("workspace_effect_missing")
            if type(expected_version) is not int or record.version != expected_version:
                raise AgentError("workspace_effect_version_conflict")
            return self._reconcile(record, base_commit)

    def _reconcile(self, record, base_commit):
        if record.kind not in (WorkspaceEffectKind.WORKTREE_ADD, WorkspaceEffectKind.WORKTREE_REMOVE):
            raise AgentError("workspace_effect_kind_mismatch")
        if record.agent_id is None or record.repository_identity_digest != repository_identity(
            self.repo_root, git_binary=self.git_binary
        ) or record.base_digest != _sha(base_commit.encode()):
            raise AgentError("workspace_effect_identity_mismatch")
        target = self._safe_target(record.resource_ref)
        inventory = self.store.load(record.agent_id, run_id=record.run_id)
        if record.kind is WorkspaceEffectKind.WORKTREE_ADD:
            expected_ref = f"{record.agent_id}/{record.run_id}/{record.resource_nonce}"
        else:
            if inventory is None or inventory.legacy_unverified:
                raise AgentError("stale_workspace_fenced")
            expected_ref = f"{record.agent_id}/{record.run_id}/{inventory.resource_nonce}"
        if record.resource_ref != expected_ref:
            raise AgentError("workspace_effect_identity_mismatch")
        expected_postcondition = _doc_digest(
            {"head": base_commit, "registered": True} if record.kind is WorkspaceEffectKind.WORKTREE_ADD
            else {"path_absent": True, "registered": False}
        )
        if record.expected_postcondition_digest != expected_postcondition:
            raise AgentError("workspace_effect_identity_mismatch")
        if record.kind is WorkspaceEffectKind.WORKTREE_ADD and record.input_digest != _doc_digest({
            "base_commit": base_commit, "detached": True,
        }):
            raise AgentError("workspace_effect_identity_mismatch")
        if record.state is WorkspaceEffectState.INTENDED:
            return record  # No claim means the normal command can still start.
        if record.state is WorkspaceEffectState.FAILED_BEFORE_EFFECT:
            return record
        registered = self._registered(target)
        absent = not target.exists() and not registered and not self._metadata_present(target)
        applied = absent if record.kind is WorkspaceEffectKind.WORKTREE_REMOVE else False
        if record.kind is WorkspaceEffectKind.WORKTREE_ADD and target.is_dir() and registered:
            try:
                self._verify_active(target, base_commit)
                applied = True
            except AgentError:
                applied = False
        evidence = _doc_digest({"effect_id": str(record.effect_id),
                                "resource_ref": record.resource_ref, "base_commit": base_commit,
                                "absent": absent, "registered": registered, "applied": applied})
        if record.state is WorkspaceEffectState.CLAIMED:
            record = self.effects.record_outcome_unknown(
                record.effect_id, expected_version=record.version,
                claim_epoch=record.claim_epoch, claim_token=record.claim_token,
                uncertainty_code="worktree_owner_interrupted", evidence_digest=evidence,
            ).record
        if record.state is WorkspaceEffectState.OUTCOME_UNKNOWN and (
            applied or (record.kind is WorkspaceEffectKind.WORKTREE_ADD and absent)
        ):
            record = self.effects.resolve_unknown(
                record.effect_id, expected_version=record.version,
                claim_epoch=record.claim_epoch, claim_token=record.claim_token,
                unknown_event_id=record.unknown_event_id,
                resolved_state=(WorkspaceEffectResolvedState.APPLIED if applied else
                                WorkspaceEffectResolvedState.FAILED_BEFORE_EFFECT),
                result_kind=WorkspaceEffectResultKind.SUCCESS if applied else None,
                reconciler_principal=self.owner_id,
                evidence_kind="exact_postcondition" if applied else "authoritative_absence",
                evidence_digest=evidence,
            ).record
        if record.state is WorkspaceEffectState.APPLIED:
            if not applied:
                raise AgentError("workspace_postcondition_changed")
            if record.kind is WorkspaceEffectKind.WORKTREE_ADD:
                self.store.allocate(
                    record.agent_id, run_id=record.run_id, worktree_path=target,
                    base_commit=base_commit, allocation_id=record.effect_id,
                    resource_nonce=record.resource_nonce, effect_id=record.effect_id,
                    command_id=uuid5(record.semantic_command_id, "inventory-active"),
                )
            else:
                self.store.mark_reaped(
                    record.agent_id, run_id=record.run_id, effect_id=record.effect_id,
                    command_id=uuid5(record.semantic_command_id, "inventory-reaped"),
                )
        elif record.state is WorkspaceEffectState.OUTCOME_UNKNOWN and inventory is not None:
            self.store.mark_unknown(
                record.agent_id, run_id=record.run_id, effect_id=record.effect_id,
                command_id=uuid5(record.semantic_command_id, "inventory-unknown"),
            )
        return record

    def _safe_target(self, resource_ref: str) -> Path:
        target = self.store.managed_root / resource_ref
        if not self.store._inside_managed(target.resolve()):
            raise AgentError("workspace_outside_managed_root")
        current = self.store.managed_root
        for component in (None, *Path(resource_ref).parts):
            if component is not None:
                current = current / component
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise AgentError("workspace_reparse_point_forbidden")
        return target

    @contextmanager
    def _operation_lock(self):
        from .integration import _lock_descriptor, _unlock_descriptor
        common = Path(os.fsdecode(self._git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()))
        lock_path = common / "koawa-v2-worktree.lock"
        descriptor = None
        locked = False
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            info = lock_path.lstat()
            actual = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or actual.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400
                    or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino)):
                raise AgentError("workspace_lock_identity_mismatch")
            try:
                _lock_descriptor(descriptor)
            except OSError:
                raise AgentError("workspace_operation_locked") from None
            locked = True
            yield
        finally:
            if descriptor is not None:
                try:
                    if locked:
                        _unlock_descriptor(descriptor)
                finally:
                    os.close(descriptor)

    def diff(self, agent_id: UUID, *, base_commit: str, run_id: UUID | None = None) -> str:
        record = self.store.load(agent_id, run_id=run_id)
        if record is None or record.state != "active":
            raise AgentError("workspace_missing")
        raw = self._git(
            "-C", record.worktree_path, "diff", "--binary", "--full-index",
            "--no-ext-diff", "--no-textconv", base_commit, "--",
        ).decode("utf-8", "surrogateescape")
        # Legacy Artifact carries a text patch and its subprocess input performs
        # platform newline conversion.  Normalize here; V2 packages retain the
        # raw bytes from workspace.content instead.
        return raw.replace("\r\n", "\n")

    def _record_create_failure(self, claimed, target: Path) -> None:
        registered = self._registered(target)
        kwargs = dict(
            expected_version=claimed.version, claim_epoch=claimed.claim_epoch,
            claim_token=claimed.claim_token,
            evidence_digest=_doc_digest({"path_exists": target.exists(), "registered": registered}),
        )
        if not target.exists() and not registered and not self._metadata_present(target):
            self.effects.record_failed_before_effect(
                claimed.effect_id, error_code="git_worktree_add_failed", **kwargs
            )
        else:
            self.effects.record_outcome_unknown(
                claimed.effect_id, uncertainty_code="worktree_add_uncertain", **kwargs
            )

    def _verify_active(self, target: Path, base_commit: str) -> None:
        if not target.is_dir():
            raise AgentError("worktree_add_postcheck_failed")
        head = self._git("-C", str(target), "rev-parse", "HEAD").strip().decode("ascii")
        if head != base_commit or not self._registered(target):
            raise AgentError("worktree_add_postcheck_failed")
        target_common = Path(os.fsdecode(self._git("-C", str(target), "rev-parse", "--path-format=absolute", "--git-common-dir").strip())).resolve()
        repo_common = Path(os.fsdecode(self._git("rev-parse", "--path-format=absolute", "--git-common-dir").strip())).resolve()
        top = Path(os.fsdecode(self._git("-C", str(target), "rev-parse", "--show-toplevel").strip())).resolve()
        branch = self._git("-C", str(target), "rev-parse", "--abbrev-ref", "HEAD").strip()
        if target_common != repo_common or top != target.resolve() or branch != b"HEAD":
            raise AgentError("worktree_add_postcheck_failed")

    def _registered(self, target: Path) -> bool:
        # A failed registry query is not authoritative absence.
        output = self._git("worktree", "list", "--porcelain").decode("utf-8", "replace")
        expected = os.path.normcase(str(target.resolve()))
        return any(
            line.startswith("worktree ") and os.path.normcase(str(Path(line[9:]).resolve())) == expected
            for line in output.splitlines()
        )

    def _metadata_present(self, target: Path) -> bool:
        """Do not mistake an incomplete/prunable Git admin entry for absence."""
        common = Path(os.fsdecode(self._git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()))
        admin_root = common / "worktrees"
        try:
            info = admin_root.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise AgentError("workspace_metadata_unverifiable")
        with os.scandir(admin_root) as entries:
            for count, entry in enumerate(entries):
                if count >= 10000:
                    raise AgentError("workspace_metadata_limit")
                # Git uses the target basename, adding a numeric suffix on a
                # collision. Controller targets use unique UUID nonces.
                suffix = entry.name.removeprefix(target.name)
                if entry.name == target.name or (entry.name.startswith(target.name) and suffix.isdecimal()):
                    return True
                # Administrative directories may have been renamed independently
                # of the worktree. Their gitdir pointer, not the basename, is the
                # remaining resource identity. Unreadable metadata is not absence.
                pointer = self._metadata_pointer(Path(entry.path))
                if os.path.normcase(str(pointer)) == os.path.normcase(str(target / ".git")):
                    return True
        try:
            after = admin_root.lstat()
        except OSError:
            raise AgentError("workspace_metadata_unverifiable") from None
        if ((after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns)
                != (info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns)):
            raise AgentError("workspace_metadata_unverifiable")
        return False

    @staticmethod
    def _metadata_pointer(directory: Path) -> Path:
        try:
            directory_info = directory.lstat()
            if (not stat.S_ISDIR(directory_info.st_mode)
                    or getattr(directory_info, "st_file_attributes", 0) & 0x400):
                raise ValueError("unsafe directory")
            path = directory / "gitdir"
            before = path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size > 4096 or getattr(before, "st_file_attributes", 0) & 0x400):
                raise ValueError("unsafe pointer")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            try:
                opened = os.fstat(descriptor)
                raw = os.read(descriptor, 4097)
                after = os.fstat(descriptor)
                final = path.lstat()
            finally:
                os.close(descriptor)
            def identity(info):
                return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                        info.st_size, info.st_mtime_ns)
            # On this Windows runtime lstat's ctime is creation time while
            # fstat's ctime is change time. Compare ctime only within each API;
            # file ID/type/size/mtime must still agree across path and handle.
            if (any(identity(value) != identity(before) for value in (opened, after, final))
                    or before.st_ctime_ns != final.st_ctime_ns
                    or opened.st_ctime_ns != after.st_ctime_ns
                    or len(raw) != before.st_size
                    or identity(directory.lstat()) != identity(directory_info)):
                raise ValueError("pointer changed")
            raw = raw.rstrip(b"\r\n")
            if not raw or any(char in raw for char in (b"\x00", b"\r", b"\n")):
                raise ValueError("invalid pointer")
            raw_text = os.fsdecode(raw)
            pointer = Path(raw_text)
            # Git writes absolute Windows pointers with forward slashes even
            # though normpath() returns backslashes.  Compare after normalizing
            # only the equivalent separator spelling; retain strict rejection
            # of traversal, dot segments, internal duplicate separators, and
            # trailing separators through the canonical-text check below.
            canonical_text = raw_text.replace("\\", "/")
            if (
                not pointer.is_absolute()
                or ".." in pointer.parts
                or any(part == "." for part in canonical_text.split("/"))
                or os.path.normpath(raw_text).replace("\\", "/") != canonical_text
            ):
                raise ValueError("relative pointer")
            return pointer
        except (OSError, ValueError):
            raise AgentError("workspace_metadata_unverifiable") from None

    def _git(self, *arguments: str) -> bytes:
        environment = {
            "PATH": str(Path(self.git_binary).parent), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "", "GIT_PAGER": "cat", "GIT_EXTERNAL_DIFF": "",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
        return self._run_bounded(
            [self.git_binary, "-c", "core.hooksPath=", *arguments],
            environment=environment,
        )

    def _run_bounded(self, arguments: list[str], *, environment: dict[str, str]) -> bytes:
        result = run_bounded(
            arguments, cwd=self.repo_root, environment=environment,
            timeout=60, output_limit=4 * 1024 * 1024,
            failure_code="git_worktree_failed",
            output_limit_code="git_worktree_output_limit",
        )
        if result.returncode:
            raise AgentError("git_worktree_failed")
        return result.stdout


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _doc_digest(value: object) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


__all__ = ["WorktreeManager"]
