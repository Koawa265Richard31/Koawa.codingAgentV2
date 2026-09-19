"""D12 artifact acceptance, integration worktree, and gated delivery."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from .container import ContainerResult, ContainerRunner
from ..agents.graph import AgentError
from ..control.event_store import EventMetadata, NewEvent, StreamId, StreamWrite
from .artifacts import (
    ArtifactPackageStore, ArtifactV2, apply_package,
)
from .content import capture_repository
from .effects import (
    WorkspaceEffectKind, WorkspaceEffectResultKind, WorkspaceEffectState,
    WorkspaceEffectStore, workspace_effect_id,
)
from .subprocesses import run_bounded


@dataclass(frozen=True, slots=True)
class Artifact:
    agent_id: UUID
    run_id: UUID
    base_commit: str
    head_commit: str
    diff: str
    test_evidence: str
    image_digest: str

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            {
                "agent_id": str(self.agent_id),
                "run_id": str(self.run_id),
                "base_commit": self.base_commit,
                "head_commit": self.head_commit,
                "diff": self.diff,
                "test_evidence": self.test_evidence,
                "image_digest": self.image_digest,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ArtifactIntegrator:
    """Integrate accepted artifacts, retest, then gate delivery on HEAD hash."""

    def __init__(
        self,
        *,
        repo_root: Path,
        integration_root: Path,
        runner: ContainerRunner,
        git_binary: str = "git",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.integration_root = Path(integration_root).resolve()
        self.runner = runner
        self.git_binary = git_binary

    def accept(
        self,
        artifact: Artifact,
        *,
        expected_run_id: UUID,
        expected_base_commit: str,
    ) -> str:
        if artifact.run_id != expected_run_id:
            raise AgentError("artifact_run_fenced")
        if artifact.base_commit != expected_base_commit:
            raise AgentError("artifact_base_mismatch")
        if artifact.head_commit == artifact.base_commit and not artifact.diff:
            raise AgentError("artifact_empty")
        if not artifact.test_evidence:
            raise AgentError("artifact_missing_evidence")
        return artifact.digest

    def integrate(
        self,
        artifacts: list[Artifact],
        *,
        test_argv: list[str],
        timeout: float = 60.0,
    ) -> tuple[ContainerResult, str]:
        """Apply diffs serially in one integration worktree; conflicts raise."""

        self._git("worktree", "add", "--detach", str(self.integration_root), artifacts[0].base_commit)
        try:
            applied: list[str] = []
            for artifact in artifacts:
                try:
                    self._apply_diff(self.integration_root, artifact.diff)
                except AgentError:
                    raise AgentError("artifact_conflict") from None
                applied.append(str(artifact.agent_id))
            result = self.runner.run(self.integration_root, test_argv, timeout=timeout)
            if result.exit_code != 0:
                raise AgentError("artifact_retest_failed")
            head = self._git("-C", str(self.integration_root), "rev-parse", "HEAD").strip()
            return result, head
        finally:
            self._git("worktree", "remove", "--force", str(self.integration_root))

    def deliver(self, artifacts: list[Artifact], *, user_base_commit: str) -> None:
        """Apply the integrated diff set to the user workspace after HEAD gate."""

        current = self._git("-C", str(self.repo_root), "rev-parse", "HEAD").strip()
        if current != user_base_commit:
            raise AgentError("user_workspace_drift")
        combined = "\n".join(artifact.diff for artifact in artifacts)
        self._apply_diff(self.repo_root, combined)

    def _apply_diff(self, worktree: Path, diff: str) -> None:
        try:
            check = subprocess.run(
                [self.git_binary, "-C", str(worktree), "apply", "--check", "--binary", "-"],
                input=diff,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            raise AgentError("git_apply_failed") from None
        if check.returncode != 0:
            raise AgentError("git_apply_conflict")
        applied = subprocess.run(
            [self.git_binary, "-C", str(worktree), "apply", "--binary", "-"],
            input=diff,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if applied.returncode != 0:
            raise AgentError("git_apply_failed")

    def _git(self, *arguments: str) -> str:
        try:
            result = subprocess.run(
                [self.git_binary, *arguments],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            raise AgentError("git_integration_failed") from None
        if result.returncode != 0:
            raise AgentError("git_integration_failed")
        return result.stdout


@dataclass(frozen=True, slots=True)
class IntegrationReceiptRef:
    receipt_id: UUID
    stream_version: int
    event_id: UUID
    receipt_digest: str


@dataclass(frozen=True, slots=True)
class DurableIntegrationResult:
    receipt: IntegrationReceiptRef
    test_exit_code: int
    test_result_kind: str


class DurableArtifactIntegrator:
    """I7 crash-resumable package integration and receipt-only delivery."""

    def __init__(
        self, *, event_store, package_store: ArtifactPackageStore,
        effect_store: WorkspaceEffectStore, repo_root: Path,
        integration_root: Path, runner: ContainerRunner,
        git_binary: str | None = None, owner_id: str = "artifact-integrator",
    ) -> None:
        self.event_store = event_store
        self.packages = package_store
        self.effects = effect_store
        self.repo_root = Path(repo_root).resolve()
        self.integration_root = Path(integration_root).resolve()
        candidate = git_binary or shutil.which("git")
        if not candidate:
            raise AgentError("git_executable_unavailable")
        self.git_binary = str(Path(candidate).resolve())
        self.runner = runner
        self.owner_id = owner_id

    def integrate(
        self, artifacts: list[ArtifactV2], *, test_argv: list[str],
        timeout: float = 60.0, command_id: UUID,
    ) -> DurableIntegrationResult:
        ordered = tuple(sorted(artifacts, key=lambda item: str(item.artifact_id)))
        if not ordered:
            raise AgentError("artifact_set_empty")
        repositories = {item.repository_identity_digest for item in ordered}
        bases = {item.base_commit for item in ordered}
        if len(repositories) != 1 or len(bases) != 1:
            raise AgentError("artifact_set_identity_mismatch")
        for artifact in ordered:
            package = self.packages.load(artifact.package)
            if (
                package.repository_identity_digest != artifact.repository_identity_digest
                or package.base_commit != artifact.base_commit
                or package.prestate_digest != artifact.repo_prestate_digest
            ):
                raise AgentError("artifact_package_identity_mismatch")
            self._verify_test_evidence(artifact)
        set_digest = _digest_doc([str(item.artifact_id) for item in ordered])
        receipt_id = uuid5(NAMESPACE_URL, f"koawa-v2:integration:{set_digest}")
        if existing := self._load_receipt_head(receipt_id):
            # D12-I7-001: replay the RECORDED result, never a canned success -
            # a persisted known_negative receipt must surface as one.
            payload = self._load_receipt(existing)
            exit_code = payload.get("test_exit_code")
            kind = payload.get("test_result_kind")
            if (
                not isinstance(exit_code, int)
                or isinstance(exit_code, bool)
                or kind not in ("success", "known_negative")
            ):
                raise AgentError("integration_receipt_result_invalid")
            return DurableIntegrationResult(existing, exit_code, kind)
        if self.integration_root.exists():
            if self._common_git_dir(self.integration_root) != self._common_git_dir(self.repo_root):
                raise AgentError("integration_root_identity_mismatch")
            existing_root = capture_repository(
                self.integration_root, base_commit=ordered[0].base_commit,
                git_binary=self.git_binary,
            )
            if existing_root.prestate.head_commit != ordered[0].base_commit:
                raise AgentError("integration_root_identity_mismatch")
        else:
            self._git(
                "worktree", "add", "--detach", str(self.integration_root),
                ordered[0].base_commit,
            )
        effect_refs: list[dict[str, object]] = []
        try:
            for index, artifact in enumerate(ordered):
                semantic = uuid5(command_id, f"artifact-apply:{index}:{artifact.artifact_id}")
                applied = self._run_apply_effect(semantic, artifact)
                effect_refs.append(_effect_ref(applied))
            post = capture_repository(
                self.integration_root, base_commit=ordered[0].base_commit,
                git_binary=self.git_binary,
            )
            test_effect, result = self._run_retest_effect(
                uuid5(command_id, "artifact-retest"), ordered, test_argv, timeout, post
            )
            effect_refs.append(_effect_ref(test_effect))
            result_kind = "success" if result.exit_code == 0 else "known_negative"
            payload = {
                "receipt_id": str(receipt_id),
                "repository_identity_digest": ordered[0].repository_identity_digest,
                "base_commit": ordered[0].base_commit,
                "artifact_ids": [str(item.artifact_id) for item in ordered],
                "artifact_set_digest": set_digest,
                "package_refs": [item.package.package_ref for item in ordered],
                "package_digests": [item.package.package_digest for item in ordered],
                "package_json_bytes": [item.package.package_json_bytes for item in ordered],
                "integrated_content_digest": post.working_tree_content_digest,
                "effect_refs": effect_refs,
                "test_result_kind": result_kind,
                "test_exit_code": result.exit_code,
                "sandbox_image_digest": result.image_digest,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            receipt = self._record_receipt(receipt_id, payload, command_id)
            return DurableIntegrationResult(receipt, result.exit_code, result_kind)
        finally:
            try:
                self._git("worktree", "remove", "--force", str(self.integration_root))
            except AgentError:
                # Cleanup is diagnostic and must not replace the primary result.
                pass

    def deliver(self, receipt_ref: IntegrationReceiptRef, *, command_id: UUID) -> None:
        payload = self._load_receipt(receipt_ref)
        if payload["test_result_kind"] != "success":
            raise AgentError("integration_known_negative_not_deliverable")
        lock = self.repo_root / ".git" / "koawa-v2-delivery.lock"
        descriptor = None
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            _lock_descriptor(descriptor)
        except (BlockingIOError, OSError):
            if descriptor is not None:
                os.close(descriptor)
            raise AgentError("repository_delivery_locked") from None
        lease_version = None
        lease_token = None
        try:
            semantic = uuid5(command_id, "artifact-deliver")
            existing = self.effects.load(
                workspace_effect_id(WorkspaceEffectKind.ARTIFACT_DELIVER, semantic)
            )
            if existing is not None:
                if existing.state is WorkspaceEffectState.APPLIED:
                    self._release_matching_delivery_lease(
                        payload["repository_identity_digest"], command_id,
                    )
                    return
                if existing.state is WorkspaceEffectState.OUTCOME_UNKNOWN:
                    self._release_matching_delivery_lease(
                        payload["repository_identity_digest"], command_id,
                    )
                    raise AgentError("workspace_outcome_unknown")
                if existing.state is WorkspaceEffectState.CLAIMED:
                    self.effects.record_outcome_unknown(
                        existing.effect_id, expected_version=existing.version,
                        claim_epoch=existing.claim_epoch,
                        claim_token=existing.claim_token,
                        uncertainty_code="artifact_delivery_uncertain",
                        evidence_digest=None,
                    )
                    self._release_matching_delivery_lease(
                        payload["repository_identity_digest"], command_id,
                    )
                    raise AgentError("workspace_outcome_unknown")
            lease_version, lease_token = self._acquire_delivery_lease(
                payload["repository_identity_digest"], command_id
            )
            before = capture_repository(
                self.repo_root, base_commit=payload["base_commit"], git_binary=self.git_binary
            )
            if before.prestate.head_commit != payload["base_commit"]:
                raise AgentError("user_workspace_drift")
            if existing is not None:
                if (
                    existing.repository_identity_digest
                    != payload["repository_identity_digest"]
                    or existing.agent_id is not None
                    or existing.run_id != UUID(payload["receipt_id"])
                    or existing.base_digest != _sha(payload["base_commit"].encode())
                    or existing.input_digest != payload["artifact_set_digest"]
                    or existing.precondition_digest != before.prestate.prestate_digest
                    or existing.expected_postcondition_digest
                    != payload["integrated_content_digest"]
                ):
                    raise AgentError("artifact_effect_identity_mismatch")
                intended_record = existing
            else:
                intended_record = self.effects.intend(
                    semantic_command_id=semantic, kind=WorkspaceEffectKind.ARTIFACT_DELIVER,
                    repository_identity_digest=payload["repository_identity_digest"],
                    agent_id=None, run_id=UUID(payload["receipt_id"]),
                    resource_ref="user-worktree", base_digest=_sha(payload["base_commit"].encode()),
                    input_digest=payload["artifact_set_digest"],
                    precondition_digest=before.prestate.prestate_digest,
                    expected_postcondition_digest=payload["integrated_content_digest"],
                ).record
            claimed = self.effects.claim(
                intended_record.effect_id, expected_version=intended_record.version,
                owner_id=self.owner_id,
            ).record
            try:
                for index, reference in enumerate(payload["package_refs"]):
                    digest = reference.removeprefix("sha256:")
                    matching = payload["package_digests"][index]
                    if digest != matching:
                        raise AgentError("integration_receipt_package_mismatch")
                    package_ref = __import__(
                        "koawa_agent_v2.workspace.artifacts", fromlist=["ArtifactPackageRef"]
                    ).ArtifactPackageRef(
                        reference, digest, payload["package_json_bytes"][index]
                    )
                    apply_package(
                        self.packages.load(package_ref), worktree=self.repo_root,
                        git_binary=self.git_binary,
                    )
                after = capture_repository(
                    self.repo_root, base_commit=payload["base_commit"], git_binary=self.git_binary
                )
                if after.working_tree_content_digest != payload["integrated_content_digest"]:
                    raise AgentError("artifact_delivery_postcheck_failed")
            except Exception:
                self.effects.record_outcome_unknown(
                    claimed.effect_id, expected_version=claimed.version,
                    claim_epoch=claimed.claim_epoch, claim_token=claimed.claim_token,
                    uncertainty_code="artifact_delivery_uncertain", evidence_digest=None,
                )
                raise
            self.effects.record_applied(
                claimed.effect_id, expected_version=claimed.version,
                claim_epoch=claimed.claim_epoch, claim_token=claimed.claim_token,
                result_kind=WorkspaceEffectResultKind.SUCCESS,
                result_code="artifact_delivered", exit_code=0,
                postcondition_digest=after.working_tree_content_digest,
                evidence_digest=_digest_doc({"receipt": str(receipt_ref.receipt_id),
                                             "post": after.working_tree_content_digest}),
            )
        finally:
            if lease_version is not None and lease_token is not None:
                self._release_delivery_lease(
                    payload["repository_identity_digest"],
                    lease_version,
                    lease_token,
                    command_id,
                )
            if descriptor is not None:
                _unlock_descriptor(descriptor)
                os.close(descriptor)

    def _acquire_delivery_lease(
        self, repository_digest: str, command_id: UUID
    ) -> tuple[int, UUID]:
        lease_id = uuid5(NAMESPACE_URL, "koawa-v2:delivery-lease:" + repository_digest)
        stream = StreamId("workspace-delivery-lease", lease_id)
        events = self.event_store.read_stream(stream, after_version=-1, limit=10_000)
        if events and events[-1].event_type == "workspace.delivery-lease-acquired.v1":
            latest = events[-1]
            epoch = latest.payload.get("epoch")
            if type(epoch) is int:
                acquire_command = uuid5(
                    command_id, f"delivery-lease-acquire:{epoch}",
                )
                token = uuid5(acquire_command, "delivery-lease-token")
                if latest.payload.get("lease_token") == str(token):
                    return latest.stream_version, token
            raise AgentError("repository_delivery_lease_held")
        expected = events[-1].stream_version if events else -1
        epoch = (expected + 2) // 2
        acquire_command = uuid5(command_id, f"delivery-lease-acquire:{epoch}")
        token = uuid5(acquire_command, "delivery-lease-token")
        payload = {
            "repository_identity_digest": repository_digest,
            "lease_token": str(token),
            "owner_id": self.owner_id,
            "epoch": epoch,
        }
        event = NewEvent(
            uuid5(acquire_command, "event:delivery-lease-acquired"),
            "workspace.delivery-lease-acquired.v1",
            1,
            datetime.now(timezone.utc),
            payload,
            EventMetadata(acquire_command, lease_id, actor=self.owner_id),
        )
        receipt = self.event_store.append_batch(
            (StreamWrite(stream, expected, (event,)),),
            idempotency_key=acquire_command,
            request_fingerprint="delivery-lease-acquire:" + _digest_doc(payload),
        )
        version = next(
            write.last_version
            for write in receipt.streams
            if write.stream_id == stream
        )
        return version, token

    def _release_matching_delivery_lease(
        self, repository_digest: str, command_id: UUID,
    ) -> None:
        lease_id = uuid5(
            NAMESPACE_URL, "koawa-v2:delivery-lease:" + repository_digest,
        )
        stream = StreamId("workspace-delivery-lease", lease_id)
        events = self.event_store.read_stream(
            stream, after_version=-1, limit=10_000,
        )
        if not events or events[-1].event_type != "workspace.delivery-lease-acquired.v1":
            return
        latest = events[-1]
        epoch = latest.payload.get("epoch")
        if type(epoch) is not int:
            raise AgentError("repository_delivery_lease_held")
        acquire_command = uuid5(command_id, f"delivery-lease-acquire:{epoch}")
        token = uuid5(acquire_command, "delivery-lease-token")
        if latest.payload.get("lease_token") != str(token):
            raise AgentError("repository_delivery_lease_held")
        self._release_delivery_lease(
            repository_digest, latest.stream_version, token, command_id,
        )

    def _release_delivery_lease(
        self,
        repository_digest: str,
        expected_version: int,
        token: UUID,
        command_id: UUID,
    ) -> None:
        lease_id = uuid5(NAMESPACE_URL, "koawa-v2:delivery-lease:" + repository_digest)
        stream = StreamId("workspace-delivery-lease", lease_id)
        release_command = uuid5(command_id, f"delivery-lease-release:{expected_version}")
        payload = {
            "repository_identity_digest": repository_digest,
            "lease_token": str(token),
        }
        event = NewEvent(
            uuid5(release_command, "event:delivery-lease-released"),
            "workspace.delivery-lease-released.v1",
            1,
            datetime.now(timezone.utc),
            payload,
            EventMetadata(release_command, lease_id, actor=self.owner_id),
        )
        self.event_store.append_batch(
            (StreamWrite(stream, expected_version, (event,)),),
            idempotency_key=release_command,
            request_fingerprint="delivery-lease-release:" + _digest_doc(payload),
        )

    def _run_apply_effect(self, semantic: UUID, artifact: ArtifactV2):
        effect_id = workspace_effect_id(WorkspaceEffectKind.ARTIFACT_APPLY, semantic)
        existing = self.effects.load(effect_id)
        if existing is not None:
            if (
                existing.repository_identity_digest != artifact.repository_identity_digest
                or existing.agent_id != artifact.agent_id
                or existing.run_id != artifact.run_id
                or existing.base_digest != _sha(artifact.base_commit.encode())
                or existing.input_digest != artifact.package.package_digest
                or existing.expected_postcondition_digest
                != artifact.working_tree_content_digest
            ):
                raise AgentError("artifact_effect_identity_mismatch")
            if existing.state is WorkspaceEffectState.APPLIED:
                observed = capture_repository(
                    self.integration_root, base_commit=artifact.base_commit,
                    git_binary=self.git_binary,
                )
                if observed.working_tree_content_digest != existing.postcondition_digest:
                    apply_package(
                        self.packages.load(artifact.package),
                        worktree=self.integration_root,
                        git_binary=self.git_binary,
                    )
                    observed = capture_repository(
                        self.integration_root, base_commit=artifact.base_commit,
                        git_binary=self.git_binary,
                    )
                    if observed.working_tree_content_digest != existing.postcondition_digest:
                        raise AgentError("artifact_projection_rebuild_failed")
                return existing
            if existing.state is WorkspaceEffectState.OUTCOME_UNKNOWN:
                raise AgentError("workspace_outcome_unknown")
            if existing.state is WorkspaceEffectState.CLAIMED:
                observed = capture_repository(
                    self.integration_root, base_commit=artifact.base_commit,
                    git_binary=self.git_binary,
                )
                self.effects.record_outcome_unknown(
                    existing.effect_id, expected_version=existing.version,
                    claim_epoch=existing.claim_epoch,
                    claim_token=existing.claim_token,
                    uncertainty_code="artifact_apply_uncertain",
                    evidence_digest=_digest_doc({
                        "post": observed.working_tree_content_digest,
                    }),
                )
                raise AgentError("workspace_outcome_unknown")
        intended_record = existing
        if intended_record is None:
            intended_record = self.effects.intend(
                semantic_command_id=semantic, kind=WorkspaceEffectKind.ARTIFACT_APPLY,
                repository_identity_digest=artifact.repository_identity_digest,
                agent_id=artifact.agent_id, run_id=artifact.run_id,
                resource_ref="integration-worktree", base_digest=_sha(artifact.base_commit.encode()),
                input_digest=artifact.package.package_digest,
                precondition_digest=artifact.repo_prestate_digest,
                expected_postcondition_digest=artifact.working_tree_content_digest,
            ).record
        claimed = self.effects.claim(
            intended_record.effect_id, expected_version=intended_record.version,
            owner_id=self.owner_id,
        ).record
        try:
            apply_package(
                self.packages.load(artifact.package), worktree=self.integration_root,
                git_binary=self.git_binary,
            )
            post = capture_repository(
                self.integration_root, base_commit=artifact.base_commit,
                git_binary=self.git_binary,
            )
        except Exception:
            self.effects.record_outcome_unknown(
                claimed.effect_id, expected_version=claimed.version,
                claim_epoch=claimed.claim_epoch, claim_token=claimed.claim_token,
                uncertainty_code="artifact_apply_uncertain", evidence_digest=None,
            )
            raise
        return self.effects.record_applied(
            claimed.effect_id, expected_version=claimed.version,
            claim_epoch=claimed.claim_epoch, claim_token=claimed.claim_token,
            result_kind=WorkspaceEffectResultKind.SUCCESS, result_code="artifact_applied",
            exit_code=0, postcondition_digest=post.working_tree_content_digest,
            evidence_digest=_digest_doc({"artifact": str(artifact.artifact_id),
                                         "post": post.working_tree_content_digest}),
        ).record

    def _verify_test_evidence(self, artifact: ArtifactV2) -> None:
        reference = artifact.test_evidence_ref
        page = self.event_store.read_stream(
            reference.stream_id, after_version=reference.stream_version - 1, limit=1
        )
        if len(page) != 1:
            raise AgentError("artifact_test_evidence_invalid")
        event = page[0]
        if event.stream_version != reference.stream_version or event.event_id != reference.event_id:
            raise AgentError("artifact_test_evidence_invalid")
        digest = _digest_doc(dict(event.payload))
        if digest != reference.evidence_digest:
            raise AgentError("artifact_test_evidence_invalid")

    def _run_retest_effect(self, semantic, artifacts, argv, timeout, post):
        effect_id = workspace_effect_id(WorkspaceEffectKind.ARTIFACT_RETEST, semantic)
        existing = self.effects.load(effect_id)
        if existing is not None:
            if (
                existing.repository_identity_digest
                != artifacts[0].repository_identity_digest
                or existing.agent_id is not None
                or existing.run_id != artifacts[0].run_id
                or existing.base_digest != _sha(artifacts[0].base_commit.encode())
                or existing.input_digest != _digest_doc(argv)
                or existing.expected_postcondition_digest
                != post.working_tree_content_digest
            ):
                raise AgentError("artifact_effect_identity_mismatch")
            if existing.state is WorkspaceEffectState.APPLIED:
                image_digest = (
                    existing.result.get("image_digest")
                    if existing.result is not None else None
                )
                if not isinstance(image_digest, str):
                    raise AgentError("integration_retest_evidence_missing")
                return existing, ContainerResult(
                    existing.exit_code, "", "", image_digest,
                )
            if existing.state is WorkspaceEffectState.OUTCOME_UNKNOWN:
                raise AgentError("workspace_outcome_unknown")
            if existing.state is WorkspaceEffectState.CLAIMED:
                self.effects.record_outcome_unknown(
                    existing.effect_id, expected_version=existing.version,
                    claim_epoch=existing.claim_epoch,
                    claim_token=existing.claim_token,
                    uncertainty_code="artifact_retest_uncertain",
                    evidence_digest=None,
                )
                raise AgentError("workspace_outcome_unknown")
        intended_record = existing
        if intended_record is None:
            intended_record = self.effects.intend(
                semantic_command_id=semantic, kind=WorkspaceEffectKind.ARTIFACT_RETEST,
                repository_identity_digest=artifacts[0].repository_identity_digest,
                agent_id=None, run_id=artifacts[0].run_id,
                resource_ref="integration-worktree", base_digest=_sha(artifacts[0].base_commit.encode()),
                input_digest=_digest_doc(argv), precondition_digest=post.prestate.prestate_digest,
                expected_postcondition_digest=post.working_tree_content_digest,
            ).record
        claimed = self.effects.claim(
            intended_record.effect_id, expected_version=intended_record.version,
            owner_id=self.owner_id,
        ).record
        try:
            result = self.runner.run(self.integration_root, argv, timeout=timeout)
        except Exception:
            self.effects.record_outcome_unknown(
                claimed.effect_id, expected_version=claimed.version,
                claim_epoch=claimed.claim_epoch, claim_token=claimed.claim_token,
                uncertainty_code="artifact_retest_uncertain", evidence_digest=None,
            )
            raise
        kind = (WorkspaceEffectResultKind.SUCCESS if result.exit_code == 0
                else WorkspaceEffectResultKind.KNOWN_NEGATIVE)
        effect = self.effects.record_applied(
            claimed.effect_id, expected_version=claimed.version,
            claim_epoch=claimed.claim_epoch, claim_token=claimed.claim_token,
            result_kind=kind, result_code=("tests_passed" if result.exit_code == 0 else "tests_failed"),
            exit_code=result.exit_code, postcondition_digest=post.working_tree_content_digest,
            evidence_digest=_digest_doc({"exit_code": result.exit_code,
                                         "image_digest": result.image_digest}),
            result={"image_digest": result.image_digest},
        ).record
        return effect, result

    def _common_git_dir(self, root: Path) -> Path:
        raw = self._git(
            "-C", str(root), "rev-parse", "--git-common-dir",
        ).strip()
        candidate = Path(os.fsdecode(raw))
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate.resolve()

    def _record_receipt(self, receipt_id: UUID, payload: dict, command_id: UUID) -> IntegrationReceiptRef:
        digest = _digest_doc(payload)
        event = NewEvent(
            uuid5(command_id, "event:integration-recorded"),
            "workspace.integration-recorded.v1", 1, datetime.now(timezone.utc), payload,
            EventMetadata(command_id, command_id, actor="artifact-integrator"),
        )
        stream = StreamId("workspace-integration", receipt_id)
        receipt = self.event_store.append_batch(
            (StreamWrite(stream, -1, (event,)),), idempotency_key=command_id,
            request_fingerprint=f"integration:{digest}",
        )
        version = next(item.last_version for item in receipt.streams if item.stream_id == stream)
        return IntegrationReceiptRef(receipt_id, version, event.event_id, digest)

    def _load_receipt_head(self, receipt_id: UUID) -> IntegrationReceiptRef | None:
        page = self.event_store.read_stream(StreamId("workspace-integration", receipt_id), after_version=-1, limit=2)
        if not page:
            return None
        event = page[-1]
        return IntegrationReceiptRef(receipt_id, event.stream_version, event.event_id, _digest_doc(dict(event.payload)))

    def _load_receipt(self, reference: IntegrationReceiptRef) -> dict:
        if not isinstance(reference, IntegrationReceiptRef):
            raise TypeError("delivery requires IntegrationReceiptRef")
        page = self.event_store.read_stream(
            StreamId("workspace-integration", reference.receipt_id),
            after_version=reference.stream_version - 1, limit=2,
        )
        if len(page) != 1:
            raise AgentError("integration_receipt_stale")
        event = page[0]
        payload = dict(event.payload)
        if (event.event_id != reference.event_id
                or event.event_type != "workspace.integration-recorded.v1"
                or _digest_doc(payload) != reference.receipt_digest):
            raise AgentError("integration_receipt_invalid")
        return payload

    def _git(self, *arguments: str) -> bytes:
        environment = {
            "PATH": str(Path(self.git_binary).parent),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GIT_PAGER": "cat",
            "GIT_EXTERNAL_DIFF": "", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C",
        }
        result = run_bounded(
            [self.git_binary, "-c", "core.hooksPath=", *arguments],
            cwd=self.repo_root, environment=environment, timeout=60,
            output_limit=4 * 1024 * 1024,
            failure_code="git_integration_failed",
            output_limit_code="git_integration_output_limit",
        )
        if result.returncode:
            raise AgentError("git_integration_failed")
        return result.stdout


def _effect_ref(record) -> dict[str, object]:
    return {"effect_id": str(record.effect_id), "stream_version": record.version,
            "event_id": str(record.last_event_id), "state": record.state.value}


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_doc(value: object) -> str:
    return _sha(json.dumps(_plain_json(value), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode())


def _plain_json(value):
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _lock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)
