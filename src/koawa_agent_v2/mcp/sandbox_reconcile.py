"""D25 W3: dual-ledger binding and crash-window reconciliation.

关联合同：``ticket.allocation_id`` 同时是 MCP allocation 与 sandbox
allocation 的聚合 ID（owner 使用 ``ticket.request_id``），两条事件流由此
交叉核对，不产生第二个无关联随机 ID。崩溃窗口的恢复只基于持久事实与
Docker inspect 证据；无法证明的结果进入 UNKNOWN，绝不猜测修复。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from ..sandbox.runtime import (
    AllocationState,
    SandboxAllocationStore,
    SandboxError,
)

ZERO_MOUNT_DIGEST_SOURCE = "koawa-d25-zero-mount:v1"


def zero_mount_digest() -> str:
    return hashlib.sha256(ZERO_MOUNT_DIGEST_SOURCE.encode("ascii")).hexdigest()


def ensure_sandbox_intent(
    sandbox_store: SandboxAllocationStore,
    ticket,
    *,
    image_id: str,
    launch_identity_digest: str,
    deadline_at: datetime,
    owner_nonce: str | None = None,
) -> None:
    """Write the sandbox intent before Docker create is legal (§4.2).

    The MCP allocation id IS the sandbox allocation id; the activation
    request id is the sandbox owner.  Idempotent: an existing intent with
    identical facts returns unchanged, any drift raises.
    """
    sandbox_store.intent(
        owner_execution_id=ticket.request_id,
        image_id=image_id,
        mount_digest=zero_mount_digest(),
        profile_digest=launch_identity_digest,
        command_digest=launch_identity_digest,
        deadline_at=deadline_at,
        allocation_id=ticket.allocation_id,
        owner_nonce=owner_nonce,
    )


@dataclass(frozen=True, slots=True)
class ReconcileFacts:
    allocation_id: UUID
    image_id: str
    launch_identity_digest: str
    evidence_kind: str
    evidence_digest: str
    reconciler_principal_id: str
    deadline_at: datetime


def _fail(code: str) -> None:
    raise SandboxError(code)


class DualLedgerReconciler:
    """Cross-ledger recovery for sandboxed MCP launches (§W3 window table)."""

    def __init__(
        self,
        *,
        activation,
        sandbox_store: SandboxAllocationStore,
        docker_adapter,
        docker_executable: str,
        container_labels: tuple[tuple[str, str], ...],
    ) -> None:
        self._activation = activation
        self._sandbox = sandbox_store
        self._docker = docker_adapter
        self._docker_executable = docker_executable
        self._labels = container_labels

    # -- helpers -----------------------------------------------------------

    def _from_path(self):
        from pathlib import Path

        return Path(self._docker_executable)

    def _find_managed_container(self, image_id: str) -> str | None:
        """Probe for one managed container by exact labels + image match."""
        from pathlib import Path

        candidates = self._docker.list_by_labels(
            Path(self._docker_executable), self._labels
        )
        for container_id in candidates:
            try:
                document = self._docker.inspect(
                    Path(self._docker_executable), container_id, timeout=15.0
                )
            except Exception:
                continue
            config = document.get("Config") if isinstance(document, dict) else None
            if isinstance(config, dict) and config.get("Image") == image_id:
                return container_id
        return None

    def _container_running(self, container_id: str) -> bool | None:
        from pathlib import Path

        try:
            document = self._docker.inspect(
                Path(self._docker_executable), container_id, timeout=15.0
            )
        except Exception as error:
            # A "No such object" answer is provable absence (already removed
            # by a graceful shutdown), not an unavailable oracle.
            if getattr(error, "code", "") == "mcp_container_absent":
                return False
            return None
        state = document.get("State") if isinstance(document, dict) else None
        if not isinstance(state, dict):
            return None
        running = state.get("Running")
        return running if isinstance(running, bool) else None

    def _stop_remove(self, container_id: str) -> bool:
        from pathlib import Path

        try:
            document_probe = None
            try:
                self._docker.inspect(
                    Path(self._docker_executable), container_id, timeout=15.0
                )
            except Exception as error:
                if getattr(error, "code", "") == "mcp_container_absent":
                    return True  # already removed: provable, nothing to do
                return False
            del document_probe
            self._docker.stop_and_remove(
                Path(self._docker_executable),
                container_id,
                stop_timeout_seconds=5.0,
                cli_timeout_seconds=20.0,
            )
        except Exception:
            return False
        return True

    def _mcp_recover(self, facts: ReconcileFacts, *, outcome: str, view) -> None:
        self._activation.reconcile_allocation(
            facts.allocation_id,
            expected_version=view.version,
            outcome=outcome,
            evidence_kind=facts.evidence_kind,
            evidence_digest=facts.evidence_digest,
            reconciler_principal_id=facts.reconciler_principal_id,
        )

    def _evidence_digest(self, container_id: str | None, detail: str) -> str:
        return hashlib.sha256(
            f"{container_id or 'none'}:{detail}".encode("utf-8")
        ).hexdigest()

    # -- windows -----------------------------------------------------------

    def reconcile(self, facts: ReconcileFacts) -> str:
        """Evaluate the crash-window table and drive recovery to a fact.

        Returns the recovery outcome code; raises ``mcp_dual_ledger_divergence``
        when the two ledgers disagree about identity (manual audit state).
        """
        view = self._activation.get_allocation(facts.allocation_id)
        if view is None:
            _fail("mcp_allocation_missing")
        if view.launch_identity_digest != facts.launch_identity_digest:
            _fail("mcp_dual_ledger_divergence")
        sandbox = self._sandbox.load(facts.allocation_id)
        if sandbox is not None and sandbox.state in (
            AllocationState.RELEASED,
            AllocationState.FINISHED,
        ):
            # A previous reconcile (or terminal run) already closed this
            # allocation: the loser of a reconcile race must not re-fire.
            _fail("mcp_reconcile_already_final")
        if sandbox is not None:
            if sandbox.image_id != facts.image_id:
                _fail("mcp_dual_ledger_divergence")
            if sandbox.profile_digest != facts.launch_identity_digest:
                _fail("mcp_dual_ledger_divergence")

        # Window 1: MCP claimed/intended, no sandbox intent at all.
        if sandbox is None:
            if self._find_managed_container(facts.image_id) is not None:
                # A container with our labels exists without an intent: this
                # is exactly the unprovable case.
                self._mcp_recover(facts, outcome="outcome_unknown", view=view)
                return "outcome_unknown"
            self._mcp_recover(facts, outcome="failed_before_start", view=view)
            return "failed_before_start"

        # Window 3: sandbox intent committed, create happened before bind.
        container_id = sandbox.container_id
        if container_id is None:
            found = self._find_managed_container(facts.image_id)
            if found is None:
                # Window 2: intent without container -> release + failed.
                self._sandbox.finish(
                    facts.allocation_id,
                    outcome="failed_before_start",
                    exit_code=None,
                    oom_killed=None,
                )
                self._sandbox.release(facts.allocation_id, reason="reconciled_no_container")
                self._mcp_recover(facts, outcome="failed_before_start", view=view)
                return "failed_before_start"
            self._sandbox.bind(facts.allocation_id, found)
            container_id = found

        running = self._container_running(container_id)
        if running is None:
            # Inspect unavailable: nothing about this container is provable.
            self._mcp_recover(facts, outcome="outcome_unknown", view=view)
            return "outcome_unknown"

        if running is False:
            # Window 4 (bind before start) or already-exited container:
            # removal is provable now.
            removed = self._stop_remove(container_id)
            if removed:
                self._sandbox.finish(
                    facts.allocation_id,
                    outcome="reconciled_exited",
                    exit_code=None,
                    oom_killed=None,
                )
                self._sandbox.release(facts.allocation_id, reason="reconciled_exited")
                outcome = (
                    "stopped"
                    if view.status in ("started", "ready")
                    else "failed_before_start"
                )
                self._mcp_recover(facts, outcome=outcome, view=view)
                return outcome
            self._mcp_recover(facts, outcome="outcome_unknown", view=view)
            return "outcome_unknown"

        # Window 5/6: container provably running - stop+remove it; the stop
        # is evidence, the earlier side effects stay ledger-bound.
        removed = self._stop_remove(container_id)
        if not removed:
            self._mcp_recover(facts, outcome="outcome_unknown", view=view)
            return "outcome_unknown"
        self._sandbox.finish(
            facts.allocation_id,
            outcome="reconciled_stopped",
            exit_code=None,
            oom_killed=None,
        )
        self._sandbox.release(facts.allocation_id, reason="reconciled_running")
        if view.status in ("started", "ready"):
            self._mcp_recover(facts, outcome="stopped", view=view)
            return "stopped"
        self._mcp_recover(facts, outcome="outcome_unknown", view=view)
        return "outcome_unknown"


def default_deadline(seconds: float = 300.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)
