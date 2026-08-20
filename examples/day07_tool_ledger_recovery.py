"""D7: durable tool ledger reuses known results and blocks uncertain writes.

Run from ``v2/`` with::

    $env:PYTHONPATH = "src"
    python -B examples/day07_tool_ledger_recovery.py

The example uses one real SQLite Thread/Turn.  A read-only call records the
PREPARED -> CLAIMED -> SUCCEEDED write-ahead sequence and is then reused after
all runtime objects are destroyed.  A non-idempotent, unqueryable write is
crashed after its handler returns; recovery converts the orphaned claim to
OUTCOME_UNKNOWN and automatic execution is denied.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import (
    ToolExecutionContext,
    ToolExecutionResult,
)
from koawa_agent_v2.ledger import (
    LedgerExecutor,
    MANUAL_WRITE_PROFILE,
    READ_ONLY_PROFILE,
    ToolExecutionState,
    ToolLedgerStore,
    ToolOutcomeBlocked,
    ToolRecoveryManager,
    logical_execution_id,
)
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem, ToolDefinition


class SimulatedProcessDeath(BaseException):
    """Bypass normal Exception cleanup like an ungraceful process exit."""


class DemoTools:
    """Small handler set whose invocation counts reveal accidental repeats."""

    def __init__(self) -> None:
        self.read_calls = 0
        self.charge_calls = 0

    def definitions(self) -> tuple[ToolDefinition, ...]:
        schema = '{"type":"object","additionalProperties":false}'
        return (
            ToolDefinition("read_counter", "Read a stable value", schema),
            ToolDefinition("charge_account", "Issue a non-idempotent charge", schema),
        )

    def execute(
        self,
        call: ToolCallItem,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        context.check_progress()
        if call.name == "read_counter":
            self.read_calls += 1
            return ToolExecutionResult('{"value":7}')
        if call.name == "charge_account":
            self.charge_calls += 1
            return ToolExecutionResult('{"charge_id":"charge-1"}')
        raise AssertionError(f"unexpected tool: {call.name}")


def _call(call_id: str, name: str) -> ToolCallItem:
    return ToolCallItem(0, f"item-{call_id}", call_id, name, "{}")


def _context(
    *,
    turn_id: UUID,
    turn_version: int,
    run_id: UUID,
    model_turn_id: UUID,
    call_id: str,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        run_id=run_id,
        model_turn_id=model_turn_id,
        model_round=1,
        call_ref=ModelCallRef(model_turn_id, call_id),
        turn_id=turn_id,
        turn_version=turn_version,
    )


def main() -> None:
    with TemporaryDirectory(prefix="koawa-day07-") as directory:
        database = Path(directory) / "runtime.sqlite3"
        store = SqliteEventStore(database)
        runtime = ThreadRuntime(store, actor="day07-demo")
        thread = runtime.create_thread("demo-workspace")
        queued = runtime.create_turn(
            thread.thread_id,
            "demonstrate durable tool recovery",
            expected_thread_version=thread.version,
        )
        running = runtime.start_turn(queued.turn_id, queued.version)
        assert running.current_run_id is not None

        model_turn_id = uuid4()
        read_call = _call("read-1", "read_counter")
        read_context = _context(
            turn_id=running.turn_id,
            turn_version=running.version,
            run_id=running.current_run_id,
            model_turn_id=model_turn_id,
            call_id=read_call.call_id,
        )
        trace: list[str] = []
        first_tools = DemoTools()
        first_executor = LedgerExecutor(
            first_tools,
            ToolLedgerStore(store),
            {
                "read_counter": READ_ONLY_PROFILE,
                "charge_account": MANUAL_WRITE_PROFILE,
            },
            fault_hook=lambda point, record: trace.append(
                f"{point}:{record.state.value}:v{record.version}"
            ),
        )
        first_result = first_executor.execute(read_call, context=read_context)
        read_execution_id = logical_execution_id(
            running.turn_id,
            model_turn_id,
            read_call.call_id,
        )
        assert first_tools.read_calls == 1
        assert first_result.content == '{"value":7}'
        assert trace == [
            "after_prepare:prepared:v0",
            "after_claim:claimed:v1",
            "before_handler:claimed:v1",
            "after_handler:claimed:v1",
            "after_result_commit:succeeded:v2",
        ]

        # Logical identity deliberately ignores the physical claimant Run.
        other_run_context = _context(
            turn_id=running.turn_id,
            turn_version=running.version,
            run_id=uuid4(),
            model_turn_id=model_turn_id,
            call_id=read_call.call_id,
        )
        assert other_run_context.run_id != read_context.run_id
        assert read_execution_id == logical_execution_id(
            other_run_context.turn_id,
            other_run_context.model_turn_id,
            other_run_context.call_ref.call_id,
        )

        # Process boundary: the fresh delegate must not receive the known call.
        del first_executor, first_tools, runtime, store
        restarted_store = SqliteEventStore(database)
        restarted_ledger = ToolLedgerStore(restarted_store)
        replay_tools = DemoTools()
        replay_executor = LedgerExecutor(
            replay_tools,
            restarted_ledger,
            {
                "read_counter": READ_ONLY_PROFILE,
                "charge_account": MANUAL_WRITE_PROFILE,
            },
        )
        replayed_result = replay_executor.execute(read_call, context=read_context)
        replayed_record = restarted_ledger.load(read_execution_id)
        assert replayed_record is not None
        assert replayed_record.state is ToolExecutionState.SUCCEEDED
        assert replayed_result.content == first_result.content
        assert replay_tools.read_calls == 0

        charge_call = _call("charge-1", "charge_account")
        charge_context = _context(
            turn_id=running.turn_id,
            turn_version=running.version,
            run_id=running.current_run_id,
            model_turn_id=model_turn_id,
            call_id=charge_call.call_id,
        )

        def die_after_handler(point: str, record) -> None:
            if point == "after_handler" and record.call_id == charge_call.call_id:
                raise SimulatedProcessDeath()

        crashing_tools = DemoTools()
        crashing_executor = LedgerExecutor(
            crashing_tools,
            restarted_ledger,
            {
                "read_counter": READ_ONLY_PROFILE,
                "charge_account": MANUAL_WRITE_PROFILE,
            },
            fault_hook=die_after_handler,
        )
        try:
            crashing_executor.execute(charge_call, context=charge_context)
        except SimulatedProcessDeath:
            pass
        else:  # pragma: no cover - the demonstration must hit the fault boundary
            raise AssertionError("expected simulated process death")
        assert crashing_tools.charge_calls == 1

        claimed = restarted_ledger.load_for_call(
            running.turn_id,
            model_turn_id,
            charge_call.call_id,
        )
        assert claimed is not None
        assert claimed.state is ToolExecutionState.CLAIMED

        # Another process opens the same database and refuses a blind replay.
        del crashing_executor, crashing_tools, restarted_ledger, restarted_store
        recovered_store = SqliteEventStore(database)
        recovered_ledger = ToolLedgerStore(recovered_store)
        recovery = ToolRecoveryManager(recovered_ledger)
        safe_to_resume = recovery.reconcile_pending(
            running.turn_id,
            (
                {
                    "model_turn_id": str(model_turn_id),
                    "call_id": charge_call.call_id,
                },
            ),
        )
        unknown = recovered_ledger.load_for_call(
            running.turn_id,
            model_turn_id,
            charge_call.call_id,
        )
        assert safe_to_resume is False
        assert unknown is not None
        assert unknown.state is ToolExecutionState.OUTCOME_UNKNOWN

        blocked_tools = DemoTools()
        blocked_executor = LedgerExecutor(
            blocked_tools,
            recovered_ledger,
            {
                "read_counter": READ_ONLY_PROFILE,
                "charge_account": MANUAL_WRITE_PROFILE,
            },
        )
        try:
            blocked_executor.execute(charge_call, context=charge_context)
        except ToolOutcomeBlocked as exc:
            assert exc.code == "tool_outcome_unknown"
        else:  # pragma: no cover - uncertain writes must never be retried
            raise AssertionError("uncertain charge was not blocked")
        assert blocked_tools.charge_calls == 0

        print("D7 durable tool ledger:")
        print(f"  execution_id       = {read_execution_id}")
        print("  execution_id_basis = turn_id + model_turn_id + call_id (no run_id)")
        print(f"  write_ahead_trace  = {' -> '.join(trace)}")
        print(f"  restart_reused     = {replay_tools.read_calls == 0}")
        print(f"  uncertain_state    = {unknown.state.value}")
        print(f"  automatic_repeat   = {blocked_tools.charge_calls}")


if __name__ == "__main__":
    main()
