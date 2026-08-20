"""D11 attempt runner: read-only tools only, durable result, no blind retry."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .control import AgentControlPlane
from .graph import AgentError, AgentRecord, AgentState
from .messages import MessageKind


D11_READ_ONLY_TOOLS = frozenset({"read_file", "list_files", "search_text"})


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    state: AgentState
    summary: str


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


class AgentScheduler:
    """Run one durable attempt: fence, deliver, execute, terminal."""

    def __init__(
        self,
        control: AgentControlPlane,
        *,
        provider,
        tool_allowlist: frozenset[str] = D11_READ_ONLY_TOOLS,
        lease_seconds: int = 30,
    ) -> None:
        self._control = control
        self._provider = provider
        self._tool_allowlist = tool_allowlist
        self._lease_seconds = lease_seconds

    def run_attempt(self, agent_id: UUID) -> AgentRunResult:
        record = self._control.graph.load(agent_id)
        if record is None:
            raise AgentError("agent_missing")
        if record.state not in (AgentState.CREATED, AgentState.ORPHANED):
            raise AgentError("agent_attempt_state_invalid")
        running = self._control.start_attempt(
            agent_id,
            expected_version=record.version,
            lease_seconds=self._lease_seconds,
        )
        run_id = running.run_id
        try:
            while True:
                self._control.heartbeat(
                    agent_id, run_id=run_id, lease_seconds=self._lease_seconds
                )
                queued = self._control.mailbox.queued(agent_id)
                if not queued:
                    return self._finish(
                        agent_id,
                        run_id,
                        AgentState.COMPLETED,
                        "no_more_messages",
                        outcome="completed",
                    )
                message = queued[0]
                if message.kind is MessageKind.CANCEL:
                    self._control.deliver_message(
                        agent_id, message.message_id, run_id=run_id
                    )
                    return self._finish(
                        agent_id,
                        run_id,
                        AgentState.CANCELLED,
                        "cancelled",
                    )
                self._control.deliver_message(
                    agent_id, message.message_id, run_id=run_id
                )
                try:
                    task = message.body_ref or message.idempotency_key
                    outcome = self._provider.run(
                        task, tool_allowlist=self._tool_allowlist
                    )
                except AgentError as error:
                    return self._finish(
                        agent_id,
                        run_id,
                        AgentState.FAILED,
                        error.code,
                    )
                self._control.ack_message(
                    agent_id, message.message_id, run_id=run_id
                )
        except AgentError as error:
            # A takeover fenced this run (stale run id) or the agent terminal
            # raced; the durable state already reflects the winner.
            return AgentRunResult(AgentState.ORPHANED, error.code)

    def _finish(
        self,
        agent_id: UUID,
        run_id: UUID,
        state: AgentState,
        reason: str,
        *,
        outcome: str | None = None,
    ) -> AgentRunResult:
        self._control.terminal(
            agent_id,
            run_id=run_id,
            state=state,
            reason=reason,
            outcome=outcome,
        )
        return AgentRunResult(state, reason)
