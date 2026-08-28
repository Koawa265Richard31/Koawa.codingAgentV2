"""D12 per-agent worktree, container, and artifact integration."""

from .store import AgentWorkspaceStore, WorkspaceRecord
from .worktree import WorktreeManager
from .container import ContainerResult, InjectedContainerRunner, DockerContainerRunner
from .integration import (
    Artifact, ArtifactIntegrator, DurableArtifactIntegrator,
    DurableIntegrationResult, IntegrationReceiptRef,
)
from .artifacts import (
    ArtifactPackageEntry, ArtifactPackageRef, ArtifactPackageStore,
    ArtifactPackageV2, ArtifactV2, TestEvidenceRef,
)
from .content import ContentSnapshot, ManifestEntry, RepoPrestate, capture_repository
from .effects import (
    WorkspaceEffectConflict,
    WorkspaceEffectError,
    WorkspaceEffectKind,
    WorkspaceEffectRecord,
    WorkspaceEffectResolvedState,
    WorkspaceEffectResultKind,
    WorkspaceEffectState,
    WorkspaceEffectStore,
    WorkspaceEffectWrite,
    workspace_effect_id,
    workspace_resource_nonce,
)

__all__ = [
    "AgentWorkspaceStore",
    "Artifact",
    "ArtifactIntegrator",
    "ArtifactPackageEntry",
    "ArtifactPackageRef",
    "ArtifactPackageStore",
    "ArtifactPackageV2",
    "ArtifactV2",
    "ContentSnapshot",
    "ContainerResult",
    "DockerContainerRunner",
    "InjectedContainerRunner",
    "DurableArtifactIntegrator",
    "DurableIntegrationResult",
    "IntegrationReceiptRef",
    "ManifestEntry",
    "RepoPrestate",
    "TestEvidenceRef",
    "WorkspaceRecord",
    "WorktreeManager",
    "WorkspaceEffectConflict",
    "WorkspaceEffectError",
    "WorkspaceEffectKind",
    "WorkspaceEffectRecord",
    "WorkspaceEffectResolvedState",
    "WorkspaceEffectResultKind",
    "WorkspaceEffectState",
    "WorkspaceEffectStore",
    "WorkspaceEffectWrite",
    "workspace_effect_id",
    "workspace_resource_nonce",
    "capture_repository",
]
