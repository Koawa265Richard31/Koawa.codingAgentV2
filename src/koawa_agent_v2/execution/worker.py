"""把 D2 ``AgentLoop`` 接到 D1 durable Thread/Turn 生命周期。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .loop import (
    AgentLoop,
    AgentLoopCancelled,
    AgentLoopApprovalWaiting,
    AgentLoopError,
    AgentLoopRecoveryBlocked,
    AgentLoopResult,
    CancellationToken,
    ModelEventSink,
)
from ..control.event_store import StreamId, WrongExpectedVersion
from ..model.protocol import (
    InstructionMessage,
    ModelError,
    ModelStreamFailure,
    StreamFailureKind,
    UserMessage,
)
from ..control.models import TurnState, TurnStatus
from ..recovery import (
    CheckpointStore,
    DurableExecutionRecorder,
    LeaseKeeper,
    RunPhase,
    context_from_document,
    execution_seed,
    reconstruct_execution,
)
from ..recovery.context import ReconstructionError
from ..control.runtime import ThreadRuntime


class ContextUnavailable(Exception):
    """续跑需要 D6 CheckpointStore，但当前 Worker 未配置或阶段不安全。"""

    def __init__(self) -> None:
        self.code = "durable_context_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True, repr=False)
class TurnWorkerResult:
    """D1 终态和可选 D2 进程内结果的组合。"""

    turn: TurnState
    loop_result: AgentLoopResult | None

    def __repr__(self) -> str:
        return (
            f"TurnWorkerResult(turn_id={self.turn.turn_id}, "
            f"status={self.turn.status.value!r}, "
            f"loop_result_present={self.loop_result is not None})"
        )


class TurnWorker:
    """以 D1 run_id 作为 fence，安全完成或失败一个 D2 Agent Loop。"""

    def __init__(
        self,
        runtime: ThreadRuntime,
        loop: AgentLoop,
        *,
        provider: str,
        model: str,
        instructions: Sequence[InstructionMessage] = (),
        max_output_tokens: int = 4096,
        checkpoint_store: CheckpointStore | None = None,
        owner_id: str | None = None,
        lease_seconds: int = 30,
    ) -> None:
        if not isinstance(runtime, ThreadRuntime):
            raise TypeError("runtime must be ThreadRuntime")
        if not isinstance(loop, AgentLoop):
            raise TypeError("loop must be AgentLoop")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be non-empty")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be non-empty")
        if (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be positive")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        if owner_id is not None and (not isinstance(owner_id, str) or not owner_id.strip()):
            raise ValueError("owner_id must be non-empty text or None")
        copied_instructions = tuple(instructions)
        if not all(isinstance(item, InstructionMessage) for item in copied_instructions):
            raise TypeError("instructions contains an invalid item")
        if (
            checkpoint_store is not None
            and loop.has_tools
            and not loop.durable_tool_execution
        ):
            raise ValueError("durable tool ledger required")
        self._runtime = runtime
        self._loop = loop
        self._provider = provider
        self._model = model
        self._instructions = copied_instructions
        self._max_output_tokens = max_output_tokens
        self._checkpoint_store = checkpoint_store
        self._owner_id = owner_id or f"turn-worker-{uuid4()}"
        self._lease_seconds = lease_seconds

    def execute(
        self,
        turn_id: UUID | str,
        expected_version: int,
        *,
        cancellation: CancellationToken | None = None,
        event_sink: ModelEventSink | None = None,
    ) -> TurnWorkerResult:
        """从 QUEUED 启动新 Run，并用同一 fence 提交最终状态。"""
        resolved_turn_id = _as_uuid(turn_id, "turn_id")
        expected_version = _expected_version(expected_version)
        resolved_execution_id = uuid4()
        token = cancellation or CancellationToken()
        queued = self._runtime.get_turn(resolved_turn_id)
        if queued.version != expected_version:
            raise WrongExpectedVersion(
                StreamId("turn", resolved_turn_id),
                expected_version,
                queued.version,
            )

        if token.cancelled:
            cancelled = self._runtime.cancel_turn(
                resolved_turn_id,
                "d2:agent_loop_cancelled",
                expected_version=expected_version,
                command_id=_command_id(resolved_execution_id, "cancel-before-start"),
            )
            return TurnWorkerResult(cancelled, None)

        resume = None
        resume_calls = ()
        execution_facts = ()
        if queued.attempt != 0 or queued.last_resume_response is not None:
            if self._checkpoint_store is None:
                raise ContextUnavailable()
            execution_facts = _read_execution(self._checkpoint_store, queued.turn_id)
            if execution_facts:
                try:
                    resume = reconstruct_execution(execution_facts)
                except ReconstructionError as exc:
                    raise ContextUnavailable() from exc
                if (
                    resume.phase.value
                    in ("tool_in_progress", "blocked_uncertain_side_effect")
                    and not self._loop.durable_tool_execution
                ):
                    raise ContextUnavailable()
                context = tuple(context_from_document(item) for item in resume.context)
                resume_calls = tuple(item for item in (context_from_document(doc) for doc in resume.pending_tool_calls) if hasattr(item, "call_ref"))
            else:
                # A legacy direct Runtime start can die before constructing its
                # recorder. No D6 model/tool fact exists, so restart from the
                # configured instructions plus the durable original input.
                context = (
                    *self._instructions,
                    UserMessage(
                        input_id=f"turn:{queued.turn_id}:original",
                        content=queued.user_input,
                    ),
                )
        else:
            context = (*self._instructions, UserMessage(input_id=f"turn:{queued.turn_id}:original", content=queued.user_input))

        resume_phase = RunPhase.READY_FOR_MODEL if resume is None else resume.phase
        if queued.last_resume_response is not None:
            if queued.last_resume_version is None:
                raise ContextUnavailable()
            response_input_id = (
                f"turn:{queued.turn_id}:resume:{queued.last_resume_version}"
            )
            if not any(
                isinstance(item, UserMessage) and item.input_id == response_input_id
                for item in context
            ):
                response_text = (
                    queued.last_resume_response
                    if isinstance(queued.last_resume_response, str)
                    else (
                        "Approval response: approved."
                        if queued.last_resume_response
                        else "Approval response: denied."
                    )
                )
                context = (
                    *context,
                    UserMessage(
                        input_id=response_input_id,
                        content=response_text,
                        source_interrupt_id=(
                            str(queued.last_resume_interrupt_id)
                            if queued.last_resume_interrupt_id is not None
                            else f"resume:{queued.last_resume_version}"
                        ),
                    ),
                )
            # A fresh operator/user response must be observed by the model before
            # any older pending tool call can be considered again.
            resume_phase = RunPhase.READY_FOR_MODEL
            resume_calls = ()

        execution_version = (
            execution_facts[-1].stream_version if execution_facts else -1
        )
        seed = None
        if self._checkpoint_store is not None:
            seed = execution_seed(
                context,
                model_round=0 if resume is None else resume.model_round,
                tool_count=0 if resume is None else resume.tool_count,
                output_chars=0 if resume is None else resume.output_chars,
                input_tokens=0 if resume is None else resume.input_tokens,
                output_tokens=0 if resume is None else resume.output_tokens,
                phase=resume_phase,
                pending_calls=(
                    ()
                    if queued.last_resume_response is not None or resume is None
                    else resume.pending_tool_calls
                ),
                final_text=None if resume is None else resume.final_text,
            )

        running = self._runtime.start_turn(
            resolved_turn_id,
            expected_version,
            command_id=_command_id(resolved_execution_id, "start"),
            execution_seed=seed,
            execution_expected_version=(
                execution_version if seed is not None else None
            ),
            lease_owner_id=self._owner_id if seed is not None else None,
            lease_seconds=self._lease_seconds if seed is not None else None,
        )
        if running.current_run_id is None:
            raise RuntimeError("started turn is missing run_id")
        recorder = None
        keeper = None
        if self._checkpoint_store is not None:
            store = self._checkpoint_store.event_store
            lease = self._checkpoint_store.get_active_lease(
                running.turn_id,
                running.current_run_id,
                self._owner_id,
            )
            recorder = DurableExecutionRecorder(
                store,
                self._checkpoint_store,
                thread_id=running.thread_id,
                turn_id=running.turn_id,
                run_id=running.current_run_id,
                turn_version=running.version,
                initial_context=context,
                model_round=0 if resume is None else resume.model_round,
                tool_count=0 if resume is None else resume.tool_count,
                output_chars=0 if resume is None else resume.output_chars,
                input_tokens=0 if resume is None else resume.input_tokens,
                output_tokens=0 if resume is None else resume.output_tokens,
                pending_calls=(
                    ()
                    if queued.last_resume_response is not None or resume is None
                    else resume.pending_tool_calls
                ),
                phase=resume_phase,
            )
            keeper = LeaseKeeper(self._checkpoint_store, lease, self._lease_seconds)
            keeper.start()

        def finish_durable() -> None:
            if keeper is not None: keeper.stop()
            if self._checkpoint_store is not None:
                self._checkpoint_store.finish_run(running.turn_id, running.current_run_id)

        def assert_run_ownership() -> None:
            """在下一次外部副作用前确认 D1 Run 仍归本 Worker。"""
            if keeper is not None: keeper.assert_owned()
            current = self._runtime.get_turn(running.turn_id)
            if (
                current.version != running.version
                or current.status is not TurnStatus.RUNNING
                or current.current_run_id != running.current_run_id
            ):
                raise WrongExpectedVersion(
                    StreamId("turn", running.turn_id),
                    running.version,
                    current.version,
                )

        try:
            if resume is not None and resume.phase.value == "ready_to_finalize" and resume.final_text:
                loop_result = AgentLoopResult(resume.final_text, context, (), resume.model_round, resume.tool_count)
            else:
                loop_result = self._loop.run(
                    run_id=running.current_run_id,
                    turn_id=running.turn_id,
                    turn_version=running.version,
                    input_items=context,
                    provider=self._provider,
                    model=self._model,
                    max_output_tokens=self._max_output_tokens,
                    cancellation=token,
                    event_sink=event_sink,
                    ownership_guard=assert_run_ownership,
                    durable_sink=recorder,
                    initial_model_rounds=0 if resume is None else resume.model_round,
                    initial_tool_calls=0 if resume is None else resume.tool_count,
                    initial_output_chars=0 if resume is None else resume.output_chars,
                    resume_tool_calls=resume_calls,
                )
        except AgentLoopApprovalWaiting:
            if keeper is not None:
                keeper.stop()
            waiting = self._runtime.get_turn(running.turn_id)
            if waiting.status is not TurnStatus.WAITING_FOR_APPROVAL:
                raise RuntimeError("approval wait did not persist the Turn state")
            finish_durable()
            return TurnWorkerResult(waiting, None)
        except AgentLoopRecoveryBlocked:
            if keeper is not None:
                keeper.stop()
            raise
        except AgentLoopCancelled:
            cancelled = self._runtime.cancel_turn(
                running.turn_id,
                "d2:agent_loop_cancelled",
                expected_version=running.version,
                command_id=_command_id(resolved_execution_id, "cancel-running"),
            )
            finish_durable(); return TurnWorkerResult(cancelled, None)
        except ModelStreamFailure as exc:
            if exc.kind is StreamFailureKind.CANCELLED:
                cancelled = self._runtime.cancel_turn(
                    running.turn_id,
                    f"d2:{exc.code}",
                    expected_version=running.version,
                    command_id=_command_id(resolved_execution_id, "cancel-provider"),
                )
                finish_durable(); return TurnWorkerResult(cancelled, None)
            failed = self._runtime.fail_turn(
                running.turn_id,
                f"d2:{exc.code}",
                expected_version=running.version,
                run_id=running.current_run_id,
                command_id=_command_id(resolved_execution_id, "fail-stream"),
            )
            finish_durable(); return TurnWorkerResult(failed, None)
        except (AgentLoopError, ModelError) as exc:
            failed = self._runtime.fail_turn(
                running.turn_id,
                f"d2:{exc.code}",
                expected_version=running.version,
                run_id=running.current_run_id,
                command_id=_command_id(resolved_execution_id, "fail"),
            )
            finish_durable(); return TurnWorkerResult(failed, None)
        except BaseException:
            if keeper is not None: keeper.stop()
            try:
                current = self._runtime.get_turn(running.turn_id)
                if current.is_terminal and self._checkpoint_store is not None:
                    self._checkpoint_store.finish_run(running.turn_id, running.current_run_id)
            except Exception:
                pass
            raise

        try:
            completed = self._runtime.complete_turn(
                running.turn_id,
                loop_result.final_text,
                expected_version=running.version,
                run_id=running.current_run_id,
                command_id=_command_id(resolved_execution_id, "complete"),
            )
        except BaseException:
            if keeper is not None: keeper.stop()
            raise
        finish_durable()
        return TurnWorkerResult(completed, loop_result)


def _command_id(execution_id: UUID, slot: str) -> UUID:
    """为一次 Worker invocation 的 D1 状态命令派生稳定 ID。"""
    return uuid5(
        NAMESPACE_URL,
        f"koawa-agent-v2:{execution_id}:turn-worker:{slot}",
    )


def _as_uuid(value: UUID | str, name: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be UUID") from exc


def _expected_version(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("expected_version must be an integer >= 0")
    return value


def _read_execution(store: CheckpointStore, turn_id: UUID) -> tuple:
    stream = StreamId("run-execution", turn_id); cursor = -1; values = []
    while True:
        page = store.event_store.read_stream(stream, after_version=cursor, limit=500)
        values.extend(page)
        if len(page) < 500: return tuple(values)
        cursor = page[-1].stream_version
