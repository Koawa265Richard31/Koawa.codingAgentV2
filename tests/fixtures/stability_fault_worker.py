"""I8 OS-kill fixture. Signals only after checking a fresh connection.

The parent kills the waiting process; recovery runs in another interpreter.
This worker never fabricates an injection by calling hit() itself: hooks are
armed after setup, then the production command reaches the requested window.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from threading import Event
from uuid import UUID

from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.schema import register_fault_hook
from koawa_agent_v2.telemetry.faults import FAULT_SPECS, NoOpFaultPort
from scripts.stability_benchmark import atomic_json
from scripts.stability_scenarios import event_digest, identity, store_at


TERMINAL_POINTS = (
    "s5.run.before_terminal_append", "s5.run.after_terminal_commit",
    "s3.event.after_validate_before_begin", "s3.event.mid_batch_before_receipt",
)


class KillPort(NoOpFaultPort):
    def __init__(self, root: Path, point: str):
        self.root, self.point = root, point
        self.armed = False
        self.baseline = 0

    def hit(self, point: str, facts) -> None:
        super().hit(point, facts)
        if not self.armed or point != self.point:
            return
        # The constructor and reads below use a NEW connection, never the
        # writer's uncommitted transaction. IN_TRANSACTION must still see old.
        visible = store_at(self.root / "runtime.db").read_all()
        expected_delta = 3 if point == "s5.run.after_terminal_commit" else 0
        if len(visible) != self.baseline + expected_delta:
            raise AssertionError("kill_marker_durable_visibility_mismatch")
        atomic_json(self.root / "ready.json", {
            "point": point, "point_class": FAULT_SPECS[point].point_class.value,
            "baseline_count": self.baseline, "visible_count": len(visible),
            "visible_digest": event_digest(store_at(self.root / "runtime.db")),
        })
        Event().wait()


def crash(root: Path, point: str) -> None:
    if point not in TERMINAL_POINTS:
        raise ValueError("unsupported kill scenario")
    store = store_at(root / "runtime.db")
    port = KillPort(root, point)
    runtime = ThreadRuntime(store, fault_port=port)
    thread = runtime.create_thread("fixture", command_id=identity("kill-thread"))
    turn = runtime.create_turn(
        thread.thread_id, "fixture", expected_thread_version=thread.version,
        command_id=identity("kill-turn"),
    )
    running = runtime.start_turn(turn.turn_id, turn.version, command_id=identity("kill-start"))
    command_id = identity("kill-terminal")
    port.baseline = len(store.read_all())
    atomic_json(root / "request.json", {
        "turn_id": str(turn.turn_id), "run_id": str(running.current_run_id),
        "expected_version": running.version, "command_id": str(command_id),
        "baseline_count": port.baseline,
    })
    port.armed = True
    if point.startswith("s3."):
        register_fault_hook(point, lambda: port.hit(point, {}))
    runtime.complete_turn(running.turn_id, "done", expected_version=running.version,
                          run_id=running.current_run_id, command_id=command_id)
    raise AssertionError("production command did not reach kill point")


def recover(root: Path) -> None:
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    marker = json.loads((root / "ready.json").read_text(encoding="utf-8"))
    store = store_at(root / "runtime.db")
    runtime = ThreadRuntime(store)
    before = len(store.read_all())
    if before != marker["visible_count"]:
        raise AssertionError("kill changed committed truth")
    if event_digest(store) != marker["visible_digest"]:
        raise AssertionError("kill changed committed content")
    turn = runtime.get_turn(request["turn_id"])
    expected_status = TurnStatus.COMPLETED if marker["point"] == "s5.run.after_terminal_commit" else TurnStatus.RUNNING
    if turn.status is not expected_status:
        raise AssertionError("unexpected recovered state")
    kwargs = {"expected_version": request["expected_version"],
              "run_id": UUID(request["run_id"]), "command_id": UUID(request["command_id"])}
    for _ in range(2):
        result = runtime.complete_turn(request["turn_id"], "done", **kwargs)
        if result.status is not TurnStatus.COMPLETED:
            raise AssertionError("terminal retry did not settle")
    events = store.read_all()
    if len(events) != request["baseline_count"] + 3:
        raise AssertionError("terminal response-loss duplicated or lost a fact")
    final_batch = [event for event in events if event.commit_id == events[-1].commit_id]
    if len(final_batch) != 3 or len({event.stream_id for event in final_batch}) != 3:
        raise AssertionError("terminal transition is not atomic")
    atomic_json(root / "recovered.json", {
        "point": marker["point"], "before_count": before, "final_count": len(events),
        "final_status": result.status.value, "normalized_event_digest": event_digest(store),
        "recovery_pid": __import__("os").getpid(),
    })


if __name__ == "__main__":
    mode, directory = sys.argv[1:3]
    root = Path(directory)
    if mode == "crash":
        from tests.fixtures.stability_s3_faults import S3_POINTS, crash_s3
        from tests.fixtures.stability_trace_faults import TRACE_POINTS, crash_trace
        from tests.fixtures.stability_worktree_faults import WORKTREE_POINTS, crash_worktree
        from tests.fixtures.stability_activation_faults import ACTIVATION_POINTS, crash_activation
        if sys.argv[3] in S3_POINTS:
            crash_s3(root, sys.argv[3])
        elif sys.argv[3] in TRACE_POINTS:
            crash_trace(root, sys.argv[3])
        elif sys.argv[3] in WORKTREE_POINTS:
            crash_worktree(root, sys.argv[3])
        elif sys.argv[3] in ACTIVATION_POINTS:
            crash_activation(root, sys.argv[3], sys.argv[4])
        else:
            crash(root, sys.argv[3])
    elif mode == "recover":
        from tests.fixtures.stability_s3_faults import S3_POINTS, recover_s3
        from tests.fixtures.stability_trace_faults import TRACE_POINTS, recover_trace
        from tests.fixtures.stability_worktree_faults import WORKTREE_POINTS, recover_worktree
        from tests.fixtures.stability_activation_faults import ACTIVATION_POINTS, recover_activation
        point = json.loads((root / "ready.json").read_text(encoding="utf-8"))["point"]
        if point in S3_POINTS:
            recover_s3(root)
        elif point in TRACE_POINTS:
            recover_trace(root)
        elif point in WORKTREE_POINTS:
            recover_worktree(root)
        elif point in ACTIVATION_POINTS:
            recover_activation(root)
        else:
            recover(root)
    else:
        raise ValueError("unknown fixture mode")
