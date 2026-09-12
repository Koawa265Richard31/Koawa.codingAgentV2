"""D4/D7 isolated fault windows. No production files modified."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import UUID, uuid4
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.editing.tools import build_coding_tool_registry
from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.ledger import LedgerExecutor, ToolLedgerStore, ToolRecoveryManager, IDEMPOTENT_WRITE_PROFILE, READ_ONLY_PROFILE
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem

def make_call():
    digest = hashlib.sha256(b"old\n").hexdigest()
    doc = {"schema_version": 1, "changes": [{"operation": "update", "path": p, "base_sha256": digest,
        "hunks": [{"old_start": 1, "old_lines": ["old"], "new_lines": ["new"]}]} for p in ("a.txt", "b.txt")]}
    return ToolCallItem(0, "item", "patch", "apply_patch", json.dumps({"patch_json": json.dumps(doc)}))

def snapshot(root):
    return {p.name: p.read_text() for p in sorted(root.iterdir()) if p.is_file()}

def child(root, db, state, point):
    store = SqliteEventStore(db)
    runtime = ThreadRuntime(store)
    thread = runtime.create_thread("audit")
    queued = runtime.create_turn(thread.thread_id, "patch two files", expected_thread_version=thread.version)
    running = runtime.start_turn(queued.turn_id, queued.version)
    mid = uuid4()
    state.write_text(json.dumps({"turn": str(running.turn_id), "run": str(running.current_run_id), "version": running.version, "mid": str(mid)}))
    def patch_fault(where, path):
        if where == point and path == "a.txt":
            os._exit(77)
    def ledger_fault(where, record):
        if point == "after_handler" and where == point:
            os._exit(77)
    registry = build_coding_tool_registry(root, fault_injector=patch_fault)
    profiles = {d.name: IDEMPOTENT_WRITE_PROFILE if d.name == "apply_patch" else READ_ONLY_PROFILE for d in registry.definitions()}
    executor = LedgerExecutor(registry, ToolLedgerStore(store), profiles, fault_hook=ledger_fault)
    executor.execute(make_call(), context=ToolExecutionContext(running.current_run_id, mid, 1, ModelCallRef(mid, "patch"), turn_id=running.turn_id, turn_version=running.version))

def main():
    output = []
    for point in ("after_original_moved", "after_new_installed", "after_handler"):
        with tempfile.TemporaryDirectory(prefix="koawa-audit-patch-") as tmp:
            base = Path(tmp); root = base / "repo"; root.mkdir()
            for p in ("a.txt", "b.txt"): (root / p).write_bytes(b"old\n")
            db, state = base / "db.sqlite", base / "state.json"
            child_result = subprocess.run([sys.executable, "-B", __file__, "child", str(root), str(db), str(state), point], capture_output=True, timeout=30)
            assert child_result.returncode == 77, (child_result.returncode, child_result.stderr.decode(errors="replace"))
            s = json.loads(state.read_text()); tid, mid = UUID(s["turn"]), UUID(s["mid"])
            store = SqliteEventStore(db); runtime = ThreadRuntime(store); ledger = ToolLedgerStore(store)
            before = snapshot(root)
            record = ledger.load_for_call(tid, mid, "patch")
            safe = ToolRecoveryManager(ledger).reconcile_pending(tid, [{"model_turn_id": str(mid), "call_id": "patch"}])
            paused = runtime.pause_turn(tid, s["version"], "audit dead process", run_id=UUID(s["run"]))
            queued = runtime.request_resume(tid, paused.version)
            resumed = runtime.start_turn(tid, queued.version)
            registry = build_coding_tool_registry(root)
            try:
                profiles = {d.name: IDEMPOTENT_WRITE_PROFILE if d.name == "apply_patch" else READ_ONLY_PROFILE for d in registry.definitions()}
                executor = LedgerExecutor(registry, ledger, profiles)
                result = executor.execute(make_call(), context=ToolExecutionContext(resumed.current_run_id, mid, 1, ModelCallRef(mid, "patch"), turn_id=tid, turn_version=resumed.version, recovered_call=True))
                output.append({"point": point, "process_exit": child_result.returncode, "before_recovery": before,
                    "ledger_before": record.state.value, "recovery_says_safe": safe, "result": json.loads(result.content),
                    "ledger_after": ledger.load_for_call(tid, mid, "patch").state.value, "after_recovery": snapshot(root)})
            finally: registry.close()
    with tempfile.TemporaryDirectory(prefix="koawa-audit-rollback-") as tmp:
        root = Path(tmp)
        for p in ("a.txt", "b.txt"): (root / p).write_bytes(b"old\n")
        def fault(point, path):
            if point in ("after_original_moved", "before_rollback"):
                raise OSError("injected filesystem failure")
        registry = build_coding_tool_registry(root, fault_injector=fault)
        try:
            mid = uuid4()
            result = registry.execute(make_call(), context=ToolExecutionContext(uuid4(), mid, 1, ModelCallRef(mid, "patch")))
            output.append({"point": "commit_and_rollback_failure", "result": json.loads(result.content), "remaining_files": snapshot(root)})
        finally: registry.close()
    print(json.dumps(output, indent=2))

if __name__ == "__main__":
    if len(sys.argv) > 1:
        child(*(Path(v) for v in sys.argv[2:5]), sys.argv[5])
    else: main()
