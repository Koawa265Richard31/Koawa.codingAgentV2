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
from ..control.models import TurnStatus
from ..execution.loop import AgentLoopApprovalWaiting
from ..execution.worker import TurnWorkerResult
from ..ledger.recovery import ToolRecoveryManager
from ..recovery import CheckpointStore, RecoveryCoordinator
from ..sandbox.runtime import DockerSandboxDoctor
from .assembly import AssembledRuntime, RuntimeAssemblyError, assemble_runtime
from .config import (
    RuntimeConfig,
    RuntimeConfigError,
    SandboxRunner,
    load_runtime_config,
    resolve_api_key,
)
from .session import SessionHistory, SessionHistoryError

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


class AppRuntime:
    """One configured durable runtime for a repository task."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        model_client: object | None = None,
        api_key: str | None = None,
        reasoning_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.assembled = assemble_runtime(
            config,
            model_client=model_client,
            api_key=api_key,
            reasoning_sink=reasoning_sink,
        )
        # Turns started through chat() resume without the D5 completion gate.
        self._chat_turn_ids: set[UUID] = set()

    @classmethod
    def from_config_file(
        cls,
        path: str | Path,
        *,
        repo_override: str | Path | None = None,
        model_client: object | None = None,
        api_key: str | None = None,
        reasoning_sink: Callable[[str], None] | None = None,
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
        )

    def close(self) -> None:
        """Release every owned resource (delegates to the assembly).

        Idempotent: ``AssembledRuntime.close()`` is itself idempotent, so
        repeated close() calls and context-manager exit are safe.
        """
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
            thread = self.assembled.runtime.create_thread(f"task-{self.config.repo.name}")
            queued = self.assembled.runtime.create_turn(
                thread.thread_id,
                task,
                expected_thread_version=thread.version,
            )
            result = self._execute(queued.turn_id, queued.version, event_sink)
            return CommandOutcome(
                result.turn.status is TurnStatus.COMPLETED,
                f"turn_{result.turn.status.value}",
                _turn_document(result),
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
        """Run one conversational turn on a thread, seeded with session history.

        history carries the bounded whitelist projection of prior turns; the
        worker's fresh-turn context becomes instructions + history + new input.
        """
        try:
            if not isinstance(message, str) or not message.strip():
                raise RuntimeAssemblyError("task_required")
            runtime = self.assembled.runtime
            if thread_id is None:
                thread = runtime.create_thread(f"chat-{self.config.repo.name}")
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
            initial_context = (
                history.context_items() if history is not None else ()
            )
            worker = self.assembled.build_worker(
                initial_context,
                task_mode=False,
                claim_gate=True,
            )
            result = worker.execute(
                queued.turn_id,
                queued.version,
                event_sink=event_sink,
            )
            return CommandOutcome(
                result.turn.status is TurnStatus.COMPLETED,
                f"turn_{result.turn.status.value}",
                _turn_document(result),
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
            if current.status in (
                TurnStatus.COMPLETED,
                TurnStatus.FAILED,
                TurnStatus.CANCELLED,
            ):
                return CommandOutcome(True, "turn_already_terminal", _turn_document_from_state(current))
            if current.status is TurnStatus.RUNNING:
                claimed = self._claim_stale(resolved)
                result = self._execute(claimed.turn.turn_id, claimed.turn.version, event_sink)
                return CommandOutcome(
                    result.turn.status is TurnStatus.COMPLETED,
                    f"turn_{result.turn.status.value}",
                    _turn_document(result),
                )
            result = self._execute(resolved, current.version, event_sink)
            return CommandOutcome(
                result.turn.status is TurnStatus.COMPLETED,
                f"turn_{result.turn.status.value}",
                _turn_document(result),
            )
        except (RuntimeConfigError, RuntimeAssemblyError, AgentError) as exc:
            return _failure(exc)
        except (ValueError, AttributeError):
            return CommandOutcome(False, "invalid_turn_id", {})

    def cancel(self, turn_id: str | UUID) -> CommandOutcome:
        try:
            resolved = UUID(str(turn_id))
            current = self.assembled.runtime.get_turn(resolved)
            if current.status in (
                TurnStatus.COMPLETED,
                TurnStatus.FAILED,
                TurnStatus.CANCELLED,
            ):
                return CommandOutcome(True, "turn_already_terminal", _turn_document_from_state(current))
            updated = self.assembled.runtime.cancel_turn(
                current.turn_id,
                "operator-cancel",
                expected_version=current.version,
            )
            return CommandOutcome(True, "turn_cancelled", _turn_document_from_state(updated))
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
                    _turn_document_from_state(self.assembled.runtime.get_turn(turn_id))
                    for turn_id in turn_ids
                ],
                "pending_approvals": [
                    _approval_document(item)
                    for item in self._pending_approval_records()
                ],
            }
            return CommandOutcome(True, "ok", payload)
        except (RuntimeAssemblyError, AgentError) as exc:
            return _failure(exc)

    def pending_approvals(self) -> CommandOutcome:
        try:
            return CommandOutcome(
                True,
                "ok",
                {
                    "pending_approvals": [
                        _approval_document(item)
                        for item in self._pending_approval_records()
                    ]
                },
            )
        except (RuntimeAssemblyError, AgentError) as exc:
            return _failure(exc)

    def resolve_approval(
        self,
        request_id: str | UUID,
        approved: bool,
        *,
        resume_after: bool = True,
    ) -> CommandOutcome:
        """Resolve one exact durable approval request, optionally resuming the Turn."""
        try:
            resolved_id = UUID(str(request_id))
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
        except (RuntimeAssemblyError, AgentError) as exc:
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
            "db": _check(self.assembled.store.database_path.exists(), self.assembled.store.database_path),
        }
        try:
            if self.assembled.client is None:
                checks["provider_key"] = _check(False, "model client injected")
            else:
                resolve_api_key(self.config.provider)
                checks["provider_key"] = _check(
                    True, f"env:{self.config.provider.api_key_env}"
                )
        except RuntimeConfigError:
            checks["provider_key"] = _check(False, self.config.provider.api_key_env)
        if self.config.sandbox.runner is SandboxRunner.DOCKER:
            try:
                doctor = DockerSandboxDoctor(
                    self.config.sandbox.docker_executable
                ).check(self.config.sandbox.image_id or "")
                checks["docker"] = _check(doctor.ready, doctor.error_code or "ready")
            except Exception as exc:
                checks["docker"] = _check(False, getattr(exc, "code", "docker_doctor_failed"))
        else:
            checks["docker"] = _check(True, "host runner configured")
        return CommandOutcome(all(item["ok"] for item in checks.values()), "doctor", checks)

    def _execute(
        self,
        turn_id: UUID,
        version: int,
        event_sink: EventSink | None,
    ) -> TurnWorkerResult:
        worker = (
            self.assembled.build_worker((), task_mode=False)
            if turn_id in self._chat_turn_ids
            else self.assembled.worker
        )
        return worker.execute(
            turn_id,
            version,
            event_sink=event_sink,
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


def _approval_document(record: ApprovalRecord) -> dict[str, Any]:
    return {
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
        # TurnState.outcome persists the worker's final_text on completion.
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
