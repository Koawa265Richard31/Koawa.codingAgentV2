"""Child process for D7: parent kills it at a durable ledger boundary."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from threading import Event
from uuid import uuid4

from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import ToolExecutionContext, ToolExecutionResult
from koawa_agent_v2.ledger import (
    LedgerExecutor,
    MANUAL_WRITE_PROFILE,
    READ_ONLY_PROFILE,
    ToolLedgerStore,
)
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem, ToolDefinition


DATABASE = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
SIDE_EFFECT = Path(sys.argv[3])
POINT = sys.argv[4]

PROBE = ToolDefinition(
    "probe",
    "D7 kill fixture",
    '{"type":"object","properties":{"value":{"type":"integer"}}}',
)

identity: dict[str, object] = {}


def ready(record) -> None:
    MARKER.write_text(
        json.dumps(
            {
                "point": POINT,
                "execution_id": str(record.execution_id),
                **identity,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    Event().wait()


class SideEffectExecutor:
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (PROBE,)

    def execute(
        self,
        call: ToolCallItem,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        count = int(SIDE_EFFECT.read_text(encoding="utf-8")) if SIDE_EFFECT.exists() else 0
        SIDE_EFFECT.write_text(str(count + 1), encoding="utf-8")
        if POINT == "handler_in_progress":
            record = ToolLedgerStore(store).load_for_call(
                context.turn_id,
                context.model_turn_id,
                call.call_id,
            )
            ready(record)
        return ToolExecutionResult("durable-result")


def fault(point, record) -> None:
    expected = {
        "before_claim": "after_prepare",
        "after_claim": "after_claim",
        "after_handler": "after_handler",
        "after_result_commit": "after_result_commit",
    }.get(POINT)
    if point == expected:
        ready(record)


store = SqliteEventStore(DATABASE)
runtime = ThreadRuntime(store, actor="d7-kill-child")
thread = runtime.create_thread("d7-kill-repo")
queued = runtime.create_turn(
    thread.thread_id,
    f"kill at {POINT}",
    expected_thread_version=thread.version,
)
running = runtime.start_turn(queued.turn_id, queued.version)
model_turn_id = uuid4()
call = ToolCallItem(0, "item-call-1", "call-1", "probe", '{"value":1}')
identity.update(
    {
        "turn_id": str(running.turn_id),
        "turn_version": running.version,
        "run_id": str(running.current_run_id),
        "model_turn_id": str(model_turn_id),
        "call_id": call.call_id,
    }
)
profile = (
    MANUAL_WRITE_PROFILE
    if POINT in {"handler_in_progress", "after_handler", "after_result_commit"}
    else READ_ONLY_PROFILE
)
executor = LedgerExecutor(
    SideEffectExecutor(),
    ToolLedgerStore(store),
    {"probe": profile},
    fault_hook=fault,
)
executor.execute(
    call,
    context=ToolExecutionContext(
        running.current_run_id,
        model_turn_id,
        1,
        ModelCallRef(model_turn_id, call.call_id),
        turn_id=running.turn_id,
        turn_version=running.version,
    ),
)

