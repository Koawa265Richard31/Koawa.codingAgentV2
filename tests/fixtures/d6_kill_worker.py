"""Child process used by D6 tests; the parent forcibly terminates it at a marker."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from threading import Event

from koawa_agent_v2.execution.loop import AgentLoop, ToolExecutionResult
from koawa_agent_v2.ledger import LedgerExecutor, READ_ONLY_PROFILE, ToolLedgerStore
from koawa_agent_v2.recovery import Checkpoint, CheckpointStore, RunPhase
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.worker import TurnWorker
from tests.test_agent_loop import (
    READ_FILE,
    RecordingToolExecutor,
    ScriptedClient,
    _final_script,
    _tool_script,
)


DATABASE = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
POINT = sys.argv[3]


def _ready(checkpoint: Checkpoint | None = None) -> None:
    document = {
        "point": POINT,
        "phase": None if checkpoint is None else checkpoint.phase.value,
    }
    MARKER.write_text(json.dumps(document), encoding="utf-8")
    Event().wait()


class BlockingCheckpointStore(CheckpointStore):
    def __init__(
        self,
        event_store: SqliteEventStore,
        *,
        phase: RunPhase,
        before_save: bool,
    ) -> None:
        self._target_phase = phase
        self._before_save = before_save
        super().__init__(event_store)

    def save(self, checkpoint: Checkpoint) -> None:
        if checkpoint.phase is self._target_phase and self._before_save:
            _ready(checkpoint)
        super().save(checkpoint)
        if checkpoint.phase is self._target_phase and not self._before_save:
            _ready(checkpoint)


class BlockingModelClient:
    def stream(self, request):
        _ready()


def main() -> None:
    store = SqliteEventStore(DATABASE)
    if POINT == "model_event_no_checkpoint":
        checkpoints = BlockingCheckpointStore(
            store,
            phase=RunPhase.READY_TO_FINALIZE,
            before_save=True,
        )
    elif POINT == "checkpoint_saved":
        checkpoints = BlockingCheckpointStore(
            store,
            phase=RunPhase.READY_TO_FINALIZE,
            before_save=False,
        )
    elif POINT == "ready_for_tool":
        checkpoints = BlockingCheckpointStore(
            store,
            phase=RunPhase.READY_FOR_TOOL,
            before_save=False,
        )
    elif POINT == "tool_in_progress":
        checkpoints = BlockingCheckpointStore(
            store,
            phase=RunPhase.BLOCKED_UNCERTAIN_SIDE_EFFECT,
            before_save=False,
        )
    elif POINT == "tool_result_saved":
        checkpoints = BlockingCheckpointStore(
            store,
            phase=RunPhase.READY_FOR_MODEL,
            before_save=False,
        )
    else:
        checkpoints = CheckpointStore(store)

    runtime = ThreadRuntime(store)
    thread = runtime.create_thread("repo")
    queued = runtime.create_turn(
        thread.thread_id,
        f"kill point: {POINT}",
        expected_thread_version=thread.version,
    )

    if POINT == "atomic_started":
        loop = AgentLoop(BlockingModelClient())
    elif POINT in ("model_event_no_checkpoint", "checkpoint_saved"):
        loop = AgentLoop(ScriptedClient(_final_script("durable final", "final")))
    else:
        client = ScriptedClient(
            _tool_script((("c1", "read_file", '{"path":"a.txt"}'),), "tools"),
            _final_script("done", "after-tool"),
        )
        executor = RecordingToolExecutor(
            ToolExecutionResult("file contents"),
            definitions=(READ_FILE,),
        )
        loop = AgentLoop(
            client,
            tool_executor=LedgerExecutor(
                executor,
                ToolLedgerStore(store),
                {"read_file": READ_ONLY_PROFILE},
            ),
        )

    TurnWorker(
        runtime,
        loop,
        provider="test",
        model="model",
        checkpoint_store=checkpoints,
        owner_id=f"child-{POINT}",
        lease_seconds=60,
    ).execute(queued.turn_id, queued.version)


if __name__ == "__main__":
    main()
