"""D6: destroy runtime objects, discover the Turn, and finalize without model replay."""

from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.execution.loop import AgentLoop
from koawa_agent_v2.recovery import (
    CheckpointStore,
    DurableExecutionRecorder,
    RecoveryCoordinator,
)
from koawa_agent_v2.model.protocol import AssistantMessage, AssistantTextItem, FinishReason, ModelTurn, UserMessage
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.worker import TurnWorker


class NeverCalledClient:
    def stream(self, request):
        raise AssertionError("the completed durable ModelTurn must not be replayed")


def main() -> None:
    with TemporaryDirectory(prefix="koawa-day06-") as directory:
        database = Path(directory) / "runtime.db"
        store = SqliteEventStore(database)
        checkpoints = CheckpointStore(store)  # installs atomic recovery projections
        runtime = ThreadRuntime(store)
        thread = runtime.create_thread("demo-workspace")
        queued = runtime.create_turn(thread.thread_id, "return a durable answer", expected_thread_version=thread.version)
        running = runtime.start_turn(queued.turn_id, queued.version)
        context = (UserMessage(f"turn:{queued.turn_id}:original", queued.user_input),)
        recorder = DurableExecutionRecorder(store, checkpoints, thread_id=thread.thread_id, turn_id=queued.turn_id, run_id=running.current_run_id, turn_version=running.version, initial_context=context)

        output = AssistantTextItem(0, "final-item", "resumed from canonical execution facts")
        model_turn = ModelTurn(uuid4(), "offline", "fixture", "response-1", (output,), FinishReason.STOP)
        recorder.model_completed(model_turn, (AssistantMessage("offline", model_turn.model_turn_id, output),), 1, len(output.text), False)

        # Process boundary: no Python Runtime/Worker/Recorder object is reused.
        del recorder, runtime, checkpoints, store
        restarted_store = SqliteEventStore(database)
        restarted_checkpoints = CheckpointStore(restarted_store)
        restarted_runtime = ThreadRuntime(restarted_store)
        coordinator = RecoveryCoordinator(restarted_runtime, restarted_checkpoints, owner_id="restart-demo")
        candidate = coordinator.list_recoverable_turns()[0]
        claim = coordinator.claim_stale(candidate)  # bootstrap lease is already expired
        worker = TurnWorker(restarted_runtime, AgentLoop(NeverCalledClient()), provider="offline", model="fixture", checkpoint_store=restarted_checkpoints)
        result = worker.execute(claim.turn.turn_id, claim.turn.version)
        print(f"status={result.turn.status.value}")
        print(f"attempt={result.turn.attempt}")
        print(f"outcome={result.turn.outcome}")
        print(f"recoverable_remaining={len(restarted_checkpoints.list_recoverable_turns())}")


if __name__ == "__main__":
    main()
