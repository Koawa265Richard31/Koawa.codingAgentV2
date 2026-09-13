"""P0 application layer: real run/resume/status/cancel/doctor commands.

The deterministic legacy CLI functions in ``runtime.cli`` remain available for
tests; production CLI uses this module when ``--config`` is supplied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from ..agents.graph import AgentError
from ..approval_service import ApprovalRecord, ApprovalStatus
from ..control.models import TERMINAL_TURN_STATUSES, TurnStatus
from ..execution.loop import AgentLoopApprovalWaiting
from ..execution.worker import TurnWorkerResult
from ..ledger.recovery import ToolRecoveryManager
from ..mcp.activation import ActivationView, McpActivationError
from ..recovery import CheckpointStore, RecoveryCoordinator
from ..sandbox.runtime import DockerSandboxDoctor
from .assembly import (
    ActivationPending,
    AssembledRuntime,
    ControlPlaneRuntime,
    GrantedExecutionPlan,
    RuntimeAssemblyError,
    assemble_control_plane,
    assemble_execution_plane,
    assemble_runtime,
    preflight_execution_activation,
)
from .config import (
    RuntimeConfig,
    RuntimeConfigError,
    SandboxRunner,
    load_runtime_config,
    resolve_api_key,
)
from .session import SessionHistory, SessionHistoryError
from .truth import RuntimeTruthVerifier

EventSink = Callable[[object], None]


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    ok: bool
    code: str
    payload: Mapping[str, Any]

    def to_json(self) -> str:
        return json.dumps(
            {"ok": self.ok, "code": self.code, "payload": dict(self.payload)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


class _ActivationPendingError(Exception):
    # Preflight found durable mcp_process_start ASKs; no Turn was created.

    def __init__(self, plan: ActivationPending) -> None:
        self.plan = plan
        super().__init__("mcp_process_activation_pending")


class AppRuntime:
    # One configured durable runtime for a repository task.
    # I6 §8.5/§8.9: construction assembles only the CONTROL plane (no model
    # client, no MCP spawn).  run / chat / resume preflight activation first;
    # a pending host_trusted grant returns a bounded request_id and creates
    # no Turn.  status / doctor / approvals / approve / deny / cancel run
    # from the control plane only and never spawn the execution plane.

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        model_client: object | None = None,
        api_key: str | None = None,
        reasoning_sink: Callable[[str], None] | None = None,
        config_base_dir: str | Path | None = None,
        launcher_builder=None,
    ) -> None:
        self.config = config
        self.assembled: ControlPlaneRuntime = assemble_control_plane(
            config,
            config_base_dir=(
                None if config_base_dir is None else Path(config_base_dir)
            ),
        )
        self._model_client = model_client
        self._api_key = api_key
        self._reasoning_sink = reasoning_sink
        self._launcher_builder = launcher_builder
        self._execution_plane: AssembledRuntime | None = None
        # Turns started through chat() resume without the D5 completion gate.
        self._chat_turn_ids: set[UUID] = set()

    def _ensure_execution_plane(self) -> AssembledRuntime:
        # Preflight activation, then lazily assemble the execution plane.
        # Raises _ActivationPendingError BEFORE any Turn/Thread creation
        # when a host_trusted grant is pending (doc §8.5).
        if self._execution_plane is not None:
            return self._execution_plane
        plan = preflight_execution_activation(
            self.assembled, command_context="run",
        )
        if isinstance(plan, ActivationPending):
            raise _ActivationPendingError(plan)
        execution = assemble_execution_plane(
            self.assembled,
            plan,
            model_client=self._model_client,
            api_key=self._api_key,
            reasoning_sink=self._reasoning_sink,
            launcher_builder=self._launcher_builder,
            # RT-1: optional trusted control-exercise tool registrars
            # (e.g. the frozen loopback egress probe), set by the operator
            # bridge before the first execution-plane build.
            post_build_registrars=tuple(
                getattr(self, "_post_build_registrars", ()) or ()
            ),
        )
        self._execution_plane = execution
        return execution

    @classmethod
    def from_config_file(
        cls,
        path: str | Path,
        *,
        repo_override: str | Path | None = None,
        model_client: object | None = None,
        api_key: str | None = None,
        reasoning_sink: Callable[[str], None] | None = None,
        launcher_builder=None,
    ) -> "AppRuntime":
        config = load_runtime_config(path)
        if repo_override is not None:
            config = replace(
                config,
                repo=Path(repo_override).expanduser().resolve(),
            )
        return cls(
            config,
            model_client=model_client,
            api_key=api_key,
            reasoning_sink=reasoning_sink,
            config_base_dir=Path(path).parent,
            launcher_builder=launcher_builder,
        )

    def close(self) -> None:
        self.assembled.close()

    def __enter__(self) -> "AppRuntime":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def run(
        self,
        task: str,
        *,
        event_sink: EventSink | None = None,
    ) -> CommandOutcome:
        try:
            if not isinstance(task, str) or not task.strip():
                raise RuntimeAssemblyError("task_required")
            execution = self._ensure_execution_plane()
            thread = execution.runtime.create_thread(
                f"task-{self.config.repo.name}",
            )
            queued = execution.runtime.create_turn(
                thread.thread_id,
                task,
                expected_thread_version=thread.version,
            )
            result = self._execute(queued.turn_id, queued.version, event_sink)
            return self._truth_outcome(result)
        except _ActivationPendingError as exc:
            return CommandOutcome(
                False,
                "mcp_process_activation_pending",
                _activation_pending_payload(exc.plan),
            )
        except (RuntimeConfigError, RuntimeAssemblyError, AgentError) as exc:
            return _failure(exc)

    def chat(
        self,
        message: str,
        *,
        thread_id: str | UUID | None = None,
        history: SessionHistory | None = None,
        event_sink: EventSink | None = None,
    ) -> CommandOutcome:
        try:
            if not isinstance(message, str) or not message.strip():
                raise RuntimeAssemblyError("task_required")
            runtime = self.assembled.runtime
            if thread_id is None:
                thread = runtime.create_thread(
                    f"chat-{self.config.repo.name}",
                )
                resolved_thread = thread.thread_id
            else:
                resolved_thread = UUID(str(thread_id))
                thread = runtime.get_thread(resolved_thread)
            queued = runtime.create_turn(
                resolved_thread,
                message,
                expected_thread_version=thread.version,
            )
            self._chat_turn_ids.add(queued.turn_id)
            execution = self._ensure_execution_plane()
            initial_context = (
                history.context_items() if history is not None else ()
            )
            worker = execution.build_worker(
                initial_context,
                task_mode=False,
                claim_gate=True,
            )
            result = worker.execute(
                queued.turn_id,
                queued.version,
                event_sink=event_sink,
            )
            return self._truth_outcome(result)
        except _ActivationPendingError as exc:
            return CommandOutcome(
                False,
                "mcp_process_activation_pending",
                _activation_pending_payload(exc.plan),
            )
        except (
            RuntimeConfigError,
            RuntimeAssemblyError,
            AgentError,
            SessionHistoryError,
        ) as exc:
            return _failure(exc)
        except ValueError:
            return CommandOutcome(False, "invalid_thread_id", {})

    def resume(
        self,
        turn_id: str | UUID,
        *,
        event_sink: EventSink | None = None,
    ) -> CommandOutcome:
        try:
            resolved = UUID(str(turn_id))
            current = self.assembled.runtime.get_turn(resolved)
            if current.status in TERMINAL_TURN_STATUSES:
                return CommandOutcome(
                    True, "turn_already_terminal", _turn_document_from_state(current),
                )
            if current.status is TurnStatus.RUNNING:
                claimed = self._claim_stale(resolved)
                result = self._execute(
                    claimed.turn.turn_id, claimed.turn.version, event_sink,
                )
                return self._truth_outcome(result)
            result = self._execute(resolved, current.version, event_sink)
            return self._truth_outcome(result)
        except _ActivationPendingError as exc:
            return CommandOutcome(
                False,
                "mcp_process_activation_pending",
                _activation_pending_payload(exc.plan),
            )
        except (RuntimeConfigError, RuntimeAssemblyError, AgentError) as exc:
            return _failure(exc)
        except (ValueError, AttributeError):
            return CommandOutcome(False, "invalid_turn_id", {})

    def cancel(self, turn_id: str | UUID) -> CommandOutcome:
        try:
            resolved = UUID(str(turn_id))
            current = self.assembled.runtime.get_turn(resolved)
            if current.status in TERMINAL_TURN_STATUSES:
                return CommandOutcome(
                    True, "turn_already_terminal", _turn_document_from_state(current),
                )
            updated = self.assembled.runtime.cancel_turn(
                current.turn_id,
                "operator-cancel",
                expected_version=current.version,
            )
            return CommandOutcome(
                True, "turn_cancelled", _turn_document_from_state(updated),
            )
        except (RuntimeAssemblyError, AgentError) as exc:
            return _failure(exc)
        except ValueError:
            return CommandOutcome(False, "invalid_turn_id", {})

    def status(self) -> CommandOutcome:
        try:
            turn_ids = _turn_id_events(self.assembled.store)
            payload = {
                "threads": len(_thread_id_events(self.assembled.store)),
                "turns": [
                    _turn_document_from_state(
                        self.assembled.runtime.get_turn(turn_id),
                    )
                    for turn_id in turn_ids
                ],
                "pending_approvals": self._pending_approval_documents(),
                "trace_diagnostics": _trace_diagnostics(
                    self.assembled.trace_sink,
                ),
            }
            return CommandOutcome(True, "ok", payload)
        except (RuntimeAssemblyError, AgentError) as exc:
            return _failure(exc)

    def pending_approvals(self) -> CommandOutcome:
        try:
            return CommandOutcome(
                True,
                "ok",
                {"pending_approvals": self._pending_approval_documents()},
            )
        except (RuntimeAssemblyError, AgentError) as exc:
            return _failure(exc)

    def _pending_approval_documents(self) -> list[dict[str, Any]]:
        # §8.5: the pending/approve/deny API lists BOTH D9 tool approvals and
        # mcp_process_start activation requests.
        documents = [
            _approval_document(item) for item in self._pending_approval_records()
        ]
        documents.extend(
            _activation_document(item)
            for item in self.assembled.activation.pending_requests()
        )
        return documents
    def resolve_approval(
        self,
        request_id: str | UUID,
        approved: bool,
        *,
        expected_version: int | None = None,
        resume_after: bool = True,
    ) -> CommandOutcome:
        try:
            resolved_id = UUID(str(request_id))
            # mcp_process_start activation grant: writes ONLY the durable
            # grant; the operator re-issues the same semantic command (I6 §8.5).
            activation_request = self.assembled.activation.get_activation(resolved_id)
            if activation_request is not None:
                if expected_version is None:
                    return CommandOutcome(
                        False, "activation_expected_version_required", {},
                    )
                view = self.assembled.activation.resolve_activation(
                    resolved_id, approved, expected_version=expected_version,
                    approver_principal_id="operator",
                )
                document = _activation_document(view)
                if view.execution_profile == "host_trusted":
                    document["risk_note"] = (
                        "host_trusted MCP process runs with host-user permissions "
                        "and can bypass the MCP protocol to read files or reach "
                        "the network directly",
                    )
                return CommandOutcome(
                    approved, f"activation_{view.status}", document,
                )
            pending = next(
                item
                for item in self._pending_approval_records()
                if item.request_id == resolved_id
            )
            turn = self.assembled.runtime.get_turn(pending.turn_id)
            updated = self.assembled.approvals.resolve(
                pending,
                approved,
                expected_approval_version=pending.version,
                expected_turn_version=turn.version,
                interrupt_id=pending.interrupt_id,
                approver_principal_id="operator",
                command_id=uuid4(),
            )
            payload = {"approval": _approval_document(updated)}
            if resume_after:
                resumed = self.resume(pending.turn_id)
                payload["resume"] = resumed.payload
                if not resumed.ok:
                    return CommandOutcome(False, resumed.code, payload)
            return CommandOutcome(True, f"approval_{updated.status.value}", payload)
        except StopIteration:
            return CommandOutcome(False, "approval_request_not_found", {})
        except (RuntimeAssemblyError, AgentError, McpActivationError) as exc:
            return _failure(exc)
        except ValueError:
            return CommandOutcome(False, "invalid_approval_request_id", {})
    def _pending_approval_records(self) -> tuple[ApprovalRecord, ...]:
        records: dict[UUID, ApprovalRecord] = {}
        for event in _all_events(self.assembled.store):
            if event.event_type != "approval.requested.v1":
                continue
            subject_id = UUID(event.payload["subject_id"])
            record = self.assembled.approvals.load(subject_id)
            if record is not None and record.status is ApprovalStatus.PENDING:
                records[record.request_id] = record
        return tuple(
            records[key]
            for key in sorted(records, key=lambda value: str(value))
        )

    def doctor(self) -> CommandOutcome:
        checks: dict[str, Any] = {
            "repo": _check(self.config.repo.is_dir(), self.config.repo),
            "db": _check(
                self.assembled.store.database_path.exists(),
                self.assembled.store.database_path,
            ),
        }
        try:
            resolve_api_key(self.config.provider)
            checks["provider_key"] = _check(
                True, f"env:{self.config.provider.api_key_env}",
            )
        except RuntimeConfigError:
            checks["provider_key"] = _check(
                False, self.config.provider.api_key_env,
            )
        if self.config.sandbox.runner is SandboxRunner.DOCKER:
            try:
                doctor = DockerSandboxDoctor(
                    self.config.sandbox.docker_executable,
                ).check(self.config.sandbox.image_id or "")
                checks["docker"] = _check(
                    doctor.ready, doctor.error_code or "ready",
                )
            except Exception as exc:
                checks["docker"] = _check(
                    False, getattr(exc, "code", "docker_doctor_failed"),
                )
        else:
            checks["docker"] = _check(True, "host runner configured")
        return CommandOutcome(
            all(item["ok"] for item in checks.values()), "doctor", checks,
        )

    def _execute(
        self,
        turn_id: UUID,
        version: int,
        event_sink: EventSink | None,
    ) -> TurnWorkerResult:
        execution = self._ensure_execution_plane()
        worker = (
            # Audit F16: chat/resume turns must keep the D22 anti-hallucination
            # claim gate the first chat turn already has (chat passes
            # claim_gate=True); the resume path used the default False.
            execution.build_worker((), task_mode=False, claim_gate=True)
            if turn_id in self._chat_turn_ids
            else execution.worker
        )
        result = worker.execute(
            turn_id,
            version,
            event_sink=event_sink,
        )
        self._record_turn_conclusion(result.turn)
        return result

    def _record_turn_conclusion(self, turn) -> None:
        """Audit F13: persist a TurnConclusion for every terminal Turn.

        Best-effort and gated on memory.conclusions_enabled: a conclusion is
        an enhancement for later sessions (the SessionHistory conclusion
        block), never a turn-terminal side effect.  Non-terminal Turns
        (waiting/approval) are rejected by build() and simply skipped.
        """
        if not self.config.memory.conclusions_enabled:
            return
        try:
            from .turn_conclusion import TurnConclusionStore

            store = TurnConclusionStore(self.assembled.store, self.assembled.runtime)
            conclusion = store.build(turn.turn_id)
            store.persist(conclusion)
        except Exception:
            return

    def _truth_outcome(self, result: TurnWorkerResult) -> CommandOutcome:
        truth = RuntimeTruthVerifier(
            self.assembled.runtime, self.assembled.store
        ).read(result.turn.turn_id)
        payload = _turn_document_from_state(truth.turn)
        payload.update({
            "run_id": None if truth.run is None else str(truth.run.run_id),
            "run_status": None if truth.run is None else truth.run.status.value,
            "evidence_digest": (
                None if truth.completion_evidence is None
                else truth.completion_evidence.evidence_digest
            ),
            "ledger_uncertain": truth.ledger_uncertain,
            "workspace_uncertain": truth.workspace_uncertain,
        })
        return CommandOutcome(
            truth.turn.status is TurnStatus.COMPLETED
            and not truth.ledger_uncertain and not truth.workspace_uncertain,
            truth.outcome_code,
            payload,
        )

    def _claim_stale(self, turn_id: UUID):
        checkpoints = CheckpointStore(self.assembled.store)
        matches = [
            item
            for item in RecoveryCoordinator(
                self.assembled.runtime,
                checkpoints,
                owner_id=f"{self.config.owner_id}-resume",
                lease_seconds=self.config.lease_seconds,
            ).list_recoverable_turns()
            if item.turn_id == turn_id
        ]
        if not matches:
            raise RuntimeAssemblyError("turn_not_recoverable")
        return RecoveryCoordinator(
            self.assembled.runtime,
            checkpoints,
            owner_id=f"{self.config.owner_id}-resume",
            lease_seconds=self.config.lease_seconds,
            tool_recovery=ToolRecoveryManager(self.assembled.ledger),
        ).claim_stale(matches[0], force=True)


def _failure(exc: BaseException) -> CommandOutcome:
    return CommandOutcome(False, getattr(exc, "code", "runtime_error"), {})


def _check(ok: bool, value: Any) -> dict[str, Any]:
    return {"ok": bool(ok), "value": "" if value is None else str(value)}


def _trace_diagnostics(trace_sink: object) -> dict[str, Any]:
    """Return explicitly process-local trace health without probing storage."""

    read = getattr(trace_sink, "diagnostics", None)
    if not callable(read):
        return {
            "scope": "process_local",
            "dropped_since_start": 0,
            "last_error_code": None,
            "last_failure_at": None,
        }
    diagnostic = read()
    failure_at = getattr(diagnostic, "last_failure_at", None)
    return {
        "scope": "process_local",
        "dropped_since_start": int(
            getattr(diagnostic, "dropped_since_start", 0),
        ),
        "last_error_code": getattr(diagnostic, "last_error_code", None),
        "last_failure_at": (
            None if failure_at is None else failure_at.isoformat()
        ),
    }


def _approval_document(record: ApprovalRecord) -> dict[str, Any]:
    return {
        "kind": "tool",
        "request_id": str(record.request_id),
        "turn_id": str(record.turn_id),
        "status": record.status.value,
        "tool_name": "",
        "action_digest": record.action_digest,
        "principal_id": record.principal_id,
        "policy_version": record.policy_version,
        "capability_scope": list(record.capability_scope),
        "expires_at": record.expires_at.isoformat(),
        "version": record.version,
    }

def _activation_document(view: ActivationView) -> dict[str, Any]:
    return {
        "kind": "mcp_process_start",
        "request_id": view.request_id_str,
        "server_id": view.server_id,
        "status": view.status,
        "execution_profile": view.execution_profile,
        "launch_identity_digest": view.launch_identity_digest,
        "principal_id": view.principal_id,
        "capability_scope": [view.scope],
        "expires_at": (
            None if view.expires_at is None else view.expires_at.isoformat()
        ),
        "version": view.version,
    }


def _activation_pending_payload(plan: ActivationPending) -> dict[str, Any]:
    return {
        "requests": [
            _activation_document(view) for view in plan.requests.values()
        ],
        "hint": (
            "approve the listed mcp_process_start request, then re-issue the "
            "same run command; approval alone never auto-resumes the task",
        ),
    }


def _turn_document(result: TurnWorkerResult) -> dict[str, Any]:
    turn = result.turn
    return {
        "turn_id": str(turn.turn_id),
        "thread_id": str(turn.thread_id),
        "status": turn.status.value,
        "final_text": None if result.loop_result is None else result.loop_result.final_text,
        "model_rounds": None if result.loop_result is None else result.loop_result.model_rounds,
        "tool_calls": None if result.loop_result is None else result.loop_result.tool_calls,
        "error": turn.error,
    }


def _turn_document_from_state(turn) -> dict[str, Any]:
    return {
        "turn_id": str(turn.turn_id),
        "thread_id": str(turn.thread_id),
        "status": turn.status.value,
        # TurnState.outcome persists the worker final_text on completion.
        "final_text": turn.outcome,
        "error": turn.error,
    }


def _thread_id_events(store) -> list[UUID]:
    return [
        UUID(event.payload["thread_id"])
        for event in _all_events(store)
        if event.event_type == "thread.created.v1"
    ]


def _turn_id_events(store) -> list[UUID]:
    return [
        UUID(event.payload["turn_id"])
        for event in _all_events(store)
        if event.event_type == "turn.created.v1"
    ]


def _all_events(store):
    values = []
    cursor = 0
    while True:
        page = store.read_all(after_position=cursor, limit=500)
        values.extend(page)
        if len(page) < 500:
            return values
        cursor = page[-1].global_position
