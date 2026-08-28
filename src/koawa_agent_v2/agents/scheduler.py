"""D11 attempt runner: durable result, no blind retry, minimal LeaseKeeper.

I2 (section 4.6/4.7) scheduler order: start/takeover/resume with an immediate
keeper, ACK only for RESULT_RECORDED, UNRESOLVED -> WAITING, then minimum
sequence QUEUED; provider outcome is persisted as RESULT_RECORDED and the
agent terminal follows the recorded facts instead of hard-coded completion.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID
from ..telemetry.faults import FaultPoint, adapt_fault_callback

from .control import (
    AgentControlPlane,
    NO_FAULTS,
    FaultInjector,
    terminal_result_identity,
)
from .graph import AgentError, AgentState
from .messages import MessageKind, MessageRecord, MessageStatus


D11_READ_ONLY_TOOLS = frozenset({"read_file", "list_files", "search_text"})


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    state: AgentState
    summary: str
    result_refs: tuple[str, ...] = ()


class ScriptedAgentProvider:
    """Deterministic provider for D11 tests and examples (no real model)."""

    def __init__(self, script: dict[str, str]) -> None:
        self.script = dict(script)
        self.calls: list[str] = []
        self.tools_seen: list[str] = []

    def run(self, task: str, *, tool_allowlist: frozenset[str]) -> str:
        self.calls.append(task)
        outcome = self.script.get(task, "ok")
        if outcome.startswith("raise:"):
            raise AgentError(outcome[len("raise:"):])
        if outcome.startswith("tool:"):
            tool = outcome[len("tool:"):]
            self.tools_seen.append(tool)
            if tool not in tool_allowlist:
                raise AgentError("d11_write_forbidden")
        return outcome


class WaitStrategy(Protocol):
    """Blocking wait used by the keeper heartbeat loop.

    wait(timeout) returns True when stop was requested. Tests inject a manual
    strategy plus a fake clock so heartbeats are deterministic (no sleeps).
    """

    def wait(self, timeout: float) -> bool: ...

    def request_stop(self) -> None: ...


class DefaultWaitStrategy:
    """Bounded wait on a stop event; returns as soon as stop is requested."""

    def __init__(self) -> None:
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def wait(self, timeout: float) -> bool:
        return self._stop.wait(timeout)


class AgentLeaseKeeper:
    """Minimal DB-clock LeaseKeeper for one agent run (I2).

    The thread only stores the control port, agent/run/attempt and stable
    configuration; it never holds provider, runtime or message bodies. On a
    lost lease or run change it records the stable error code and stops.
    """

    def __init__(
        self,
        control: AgentControlPlane,
        *,
        agent_id: UUID,
        run_id: UUID,
        attempt: int,
        lease_seconds: int = 30,
        heartbeat_interval_seconds: float | None = None,
        max_cas_retries: int = 3,
        join_timeout_seconds: float = 5.0,
        wait_strategy: WaitStrategy | None = None,
        faults: FaultInjector = NO_FAULTS,
        fault_port=None,
    ) -> None:
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool):
            raise ValueError("lease_seconds must be an integer")
        if not 3 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be within 3..3600")
        if heartbeat_interval_seconds is None:
            heartbeat_interval_seconds = lease_seconds / 3
        if (
            not isinstance(heartbeat_interval_seconds, float)
            and not isinstance(heartbeat_interval_seconds, int)
        ):
            raise ValueError("heartbeat_interval_seconds must be numeric")
        if (
            not 0.1 <= float(heartbeat_interval_seconds) <= lease_seconds / 2
        ):
            raise ValueError(
                "heartbeat_interval_seconds must be within 0.1..lease/2"
            )
        if not 0.1 <= join_timeout_seconds <= 30.0:
            raise ValueError("join_timeout_seconds must be within 0.1..30")
        if not isinstance(max_cas_retries, int) or max_cas_retries < 1:
            raise ValueError("max_cas_retries must be a positive integer")
        if not callable(faults):
            raise TypeError("faults must be callable")
        self._control = control
        self._agent_id = agent_id
        self._run_id = run_id
        self._attempt = attempt
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self._max_cas_retries = max_cas_retries
        self._join_timeout_seconds = join_timeout_seconds
        self._faults = adapt_fault_callback(faults, fault_port)
        self._wait: WaitStrategy = wait_strategy or DefaultWaitStrategy()
        self._failed_event = threading.Event()
        self._failed_code: str | None = None
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"koawa-agent-lease-{agent_id}-{attempt}",
            daemon=False,
        )

    def start(self) -> None:
        if self._started:
            raise RuntimeError("keeper already started")
        self._started = True
        self._thread.start()

    def stop(self, *, assert_owned: bool = True) -> None:
        if self._started:
            try:
                self._wait.request_stop()
                self._thread.join(self._join_timeout_seconds)
                if self._thread.is_alive():
                    raise AgentError("agent_lease_keeper_failed")
            finally:
                self._started = False
        if assert_owned:
            if self._failed_code is not None:
                raise AgentError(self._failed_code)
            self.assert_owned()

    def assert_owned(self) -> None:
        record = self._control.graph.load(self._agent_id)
        if record is None:
            raise AgentError("agent_missing")
        if (
            record.state is not AgentState.RUNNING
            or record.run_id != self._run_id
            or record.attempt != self._attempt
        ):
            raise AgentError("agent_lease_lost")
        if (
            record.lease_expires_at is not None
            and record.lease_expires_at <= self._control.now()
        ):
            raise AgentError("agent_lease_lost")

    def _run(self) -> None:
        beat = 0
        while True:
            try:
                if self._wait.wait(self._heartbeat_interval_seconds):
                    return
            except Exception:
                return
            if self._failed_event.is_set():
                return
            beat += 1
            try:
                self._control.heartbeat(
                    self._agent_id,
                    run_id=self._run_id,
                    attempt=self._attempt,
                    lease_seconds=self._lease_seconds,
                    max_cas_retries=self._max_cas_retries,
                    beat_number=beat,
                )
            except AgentError as error:
                self._failed_code = error.code
                self._failed_event.set()
                return
            except Exception:
                self._failed_code = "agent_lease_keeper_failed"
                self._failed_event.set()
                return


class AgentScheduler:
    """Run one durable attempt: fence, deliver, execute, record, terminal."""

    def __init__(
        self,
        control: AgentControlPlane,
        *,
        provider,
        tool_allowlist: frozenset[str] = D11_READ_ONLY_TOOLS,
        lease_seconds: int = 30,
        faults: FaultInjector = NO_FAULTS,
        fault_port=None,
        keeper_wait_strategy: WaitStrategy | None = None,
    ) -> None:
        if not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        if not callable(faults):
            raise TypeError("faults must be callable")
        self._control = control
        self._provider = provider
        self._tool_allowlist = tool_allowlist
        self._lease_seconds = lease_seconds
        self._faults = adapt_fault_callback(faults, fault_port)
        self._keeper_wait_strategy = keeper_wait_strategy

    def run_attempt(self, agent_id: UUID) -> AgentRunResult:
        record = self._control.graph.load(agent_id)
        if record is None:
            raise AgentError("agent_missing")
        if record.state not in (
            AgentState.CREATED,
            AgentState.ORPHANED,
            AgentState.WAITING,
        ):
            raise AgentError("agent_attempt_state_invalid")
        running = self._control.start_attempt(
            agent_id,
            expected_version=record.version,
            lease_seconds=self._lease_seconds,
        )
        if running.run_id is None:
            raise AgentError("agent_attempt_state_invalid")
        run_id = running.run_id
        attempt = running.attempt
        keeper = AgentLeaseKeeper(
            self._control,
            agent_id=agent_id,
            run_id=run_id,
            attempt=attempt,
            lease_seconds=self._lease_seconds,
            wait_strategy=self._keeper_wait_strategy,
            faults=self._faults,
        )
        keeper.start()
        cancelled = False
        try:
            while True:
                snapshot = self._control.mailbox.snapshot(agent_id)
                # 1. RESULT_RECORDED first: only ack, never call the provider.
                for message in snapshot.messages:
                    if message.status is MessageStatus.RESULT_RECORDED:
                        if message.cancel_requested:
                            cancelled = True
                        self._control.ack_message(
                            agent_id, message.message_id, run_id=run_id
                        )
                snapshot = self._control.mailbox.snapshot(agent_id)
                # 2. Bare DELIVERED of a foreign run: fail closed, never guess.
                for message in snapshot.messages:
                    if (
                        message.status is MessageStatus.DELIVERED
                        and message.delivered_run_id != run_id
                    ):
                        raise AgentError("message_outcome_unresolved")
                # 3. UNRESOLVED remains: stop the keeper and wait.
                unresolved = [
                    message
                    for message in snapshot.messages
                    if message.status is MessageStatus.UNRESOLVED
                ]
                if unresolved:
                    keeper.stop(assert_owned=True)
                    self._control.enter_waiting_for_resolution(
                        agent_id, run_id=run_id, attempt=attempt
                    )
                    return AgentRunResult(
                        AgentState.WAITING, "waiting_for_resolution"
                    )
                # 4. Minimum sequence QUEUED.
                queued = [
                    message
                    for message in snapshot.messages
                    if message.status is MessageStatus.QUEUED
                ]
                if not queued:
                    if snapshot.unfinished():
                        raise AgentError("message_outcome_unresolved")
                    keeper.stop(assert_owned=True)
                    return self._terminate(
                        agent_id,
                        run_id,
                        snapshot,
                        cancelled=cancelled,
                    )
                message = min(queued, key=lambda item: item.sequence)
                if message.kind is MessageKind.CANCEL:
                    cancelled = self._process_cancel(
                        agent_id, run_id, message, keeper
                    ) or cancelled
                    continue
                terminal = self._process_task(agent_id, run_id, message, keeper)
                if terminal is not None:
                    return terminal
        except AgentError as error:
            self._stop_keeper_safely(keeper)
            return AgentRunResult(AgentState.ORPHANED, error.code)
        finally:
            self._stop_keeper_safely(keeper)

    # ------------------------------------------------------------------

    def _process_task(
        self,
        agent_id: UUID,
        run_id: UUID,
        message: MessageRecord,
        keeper: AgentLeaseKeeper,
    ) -> AgentRunResult | None:
        delivered = self._control.deliver_message(
            agent_id,
            message.message_id,
            run_id=run_id,
            lease_seconds=self._lease_seconds,
        )
        delivery_attempt = delivered.delivery_attempt
        self._fault(
            FaultPoint.D11_PROVIDER_ENTERED,
            self._facts(agent_id, message, delivered),
        )
        try:
            task = message.body_ref or message.idempotency_key
            outcome = self._provider.run(
                task, tool_allowlist=self._tool_allowlist
            )
        except AgentError as error:
            self._fault(
                FaultPoint.D11_PROVIDER_RETURNED,
                self._facts(agent_id, message, delivered),
            )
            self._control.record_message_result(
                agent_id,
                message.message_id,
                run_id=run_id,
                expected_delivery_attempt=delivery_attempt,
                outcome=None,
                error_code=error.code,
            )
            self._control.ack_message(
                agent_id, message.message_id, run_id=run_id
            )
            keeper.stop(assert_owned=True)
            snapshot = self._control.mailbox.snapshot(agent_id)
            attempt = self._control.graph.load(agent_id).attempt
            result_ref, result_digest = terminal_result_identity(
                agent_id,
                run_id,
                AgentState.FAILED,
                error.code,
                snapshot.messages,
            )
            self._control.terminal(
                agent_id,
                run_id=run_id,
                expected_attempt=attempt,
                state=AgentState.FAILED,
                reason=error.code,
                result_ref=result_ref,
                result_digest=result_digest,
                source_message_ids=tuple(
                    item.message_id
                    for item in snapshot.messages
                    if item.status is MessageStatus.ACKED
                ),
            )
            return AgentRunResult(
                AgentState.FAILED, error.code, (result_ref,)
            )
        self._fault(
            FaultPoint.D11_PROVIDER_RETURNED,
            self._facts(agent_id, message, delivered),
        )
        keeper.assert_owned()
        self._control.record_message_result(
            agent_id,
            message.message_id,
            run_id=run_id,
            expected_delivery_attempt=delivery_attempt,
            outcome=outcome,
        )
        self._control.ack_message(
            agent_id, message.message_id, run_id=run_id
        )
        return None

    def _process_cancel(
        self,
        agent_id: UUID,
        run_id: UUID,
        message: MessageRecord,
        keeper: AgentLeaseKeeper,
    ) -> bool:
        delivered = self._control.deliver_message(
            agent_id,
            message.message_id,
            run_id=run_id,
            lease_seconds=self._lease_seconds,
        )
        delivery_attempt = delivered.delivery_attempt
        self._control.record_message_result(
            agent_id,
            message.message_id,
            run_id=run_id,
            expected_delivery_attempt=delivery_attempt,
            outcome="cancelled",
        )
        self._control.ack_message(
            agent_id, message.message_id, run_id=run_id
        )
        return True

    def _terminate(
        self,
        agent_id: UUID,
        run_id: UUID,
        snapshot,
        *,
        cancelled: bool,
    ) -> AgentRunResult:
        messages = snapshot.messages
        error_code = self._first_error_code(messages)
        if error_code is not None:
            return self._terminal(
                agent_id, run_id, AgentState.FAILED, error_code, messages
            )
        cancel_intent = cancelled or any(
            message.cancel_requested for message in messages
        ) or any(
            message.status is MessageStatus.CANCELLED for message in messages
        )
        if cancel_intent:
            return self._terminal(
                agent_id, run_id, AgentState.CANCELLED, "cancelled", messages
            )
        return self._terminal(
            agent_id, run_id, AgentState.COMPLETED, None, messages
        )

    def _terminal(
        self,
        agent_id: UUID,
        run_id: UUID,
        state: AgentState,
        reason: str | None,
        messages,
    ) -> AgentRunResult:
        """Compute the exact aggregate identity and call the atomic terminal."""
        result_ref, result_digest = terminal_result_identity(
            agent_id, run_id, state, reason, messages
        )
        source_message_ids = tuple(
            message.message_id
            for message in messages
            if message.status is MessageStatus.ACKED
        )
        attempt = self._control.graph.load(agent_id).attempt
        self._control.terminal(
            agent_id,
            run_id=run_id,
            expected_attempt=attempt,
            state=state,
            reason=reason,
            result_ref=result_ref,
            result_digest=result_digest,
            source_message_ids=source_message_ids,
        )
        return AgentRunResult(
            state,
            reason or "completed",
            (result_ref,),
        )

    @staticmethod
    def _first_error_code(messages) -> str | None:
        for message in messages:
            if (
                message.status is MessageStatus.ACKED
                and message.result_is_error
                and message.result_error_code is not None
            ):
                return message.result_error_code
        return None

    @staticmethod
    def _completion_outcome(messages) -> str | None:
        acked = [
            message
            for message in messages
            if message.status is MessageStatus.ACKED
            and message.result_digest is not None
        ]
        if not acked:
            return "no_messages"
        last = max(acked, key=lambda item: item.sequence)
        return "result:" + last.result_digest

    @staticmethod
    def _facts(
        agent_id: UUID, message: MessageRecord, delivered: MessageRecord
    ) -> dict[str, object]:
        return {
            "agent_id": str(agent_id),
            "message_id": str(message.message_id),
            "attempt": delivered.delivered_agent_attempt or 0,
            "delivery_attempt": delivered.delivery_attempt,
        }

    def _fault(self, point: str, facts: dict[str, object]) -> None:
        self._faults(point, facts)

    @staticmethod
    def _stop_keeper_safely(keeper: AgentLeaseKeeper) -> None:
        try:
            keeper.stop(assert_owned=False)
        except AgentError:
            pass
