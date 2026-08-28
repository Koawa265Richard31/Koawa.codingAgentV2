"""Real SQLite trace contention and OS-kill recovery after a tool effect.

The race adapter inserts a competing trace commit, then submits the original
stale write to SQLite. It never fabricates WrongExpectedVersion or calls hit().
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event
from uuid import UUID

from koawa_agent_v2.control.durable_json import canonical_json_bytes_v1
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.loop import ToolExecutionContext, ToolExecutionResult
from koawa_agent_v2.ledger import LedgerExecutor, MANUAL_WRITE_PROFILE, ToolLedgerStore
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem, ToolDefinition
from koawa_agent_v2.telemetry.faults import FAULT_SPECS, NoOpFaultPort
from koawa_agent_v2.telemetry.trace import BestEffortTraceSink, TraceStore
from scripts.stability_benchmark import atomic_json
from scripts.stability_scenarios import identity, store_at


TRACE_POINTS = ("s5.trace.cas_conflict", "s5.trace.drop")
RETRY_LIMIT = 4
TOOL = ToolDefinition("trace_probe", "fixture effect", '{"type":"object","properties":{}}')
CALL = ToolCallItem(0, "item-call", "call-1", TOOL.name, "{}")
EFFECT_SCRIPT = (
    "import json,os,sys; "
    "f=open(sys.argv[1],'x',encoding='utf-8'); "
    "json.dump({'execution_id':sys.argv[2],'value':'committed-result'},f); "
    "f.flush(); os.fsync(f.fileno()); f.close()"
)


def business_events(store):
    return [event for event in store.read_all(limit=500) if event.stream_id.category != "trace"]


def business_digest(store):
    # Within one run this compares exact payloads, including random claim tokens.
    return hashlib.sha256(canonical_json_bytes_v1([
        {"id": str(event.event_id), "version": event.stream_version,
         "stream": event.stream_id.key, "type": event.event_type,
         "commit": str(event.commit_id), "payload": event.payload}
        for event in business_events(store)
    ])).hexdigest()


def normalized_business(store):
    ids = {}

    def normalize(value):
        if isinstance(value, str):
            try:
                canonical = str(UUID(value)) == value
            except ValueError:
                canonical = False
            if canonical:
                return ids.setdefault(value, f"uuid:{len(ids)}")
            return value
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(value[key]) for key in sorted(value)}
        return value

    # Trace commits can occupy different global positions. Keep every business
    # payload, identity relationship, stream version and transaction boundary;
    # omit only envelope wall-clock time and unrelated trace positions.
    return [normalize(json.loads(canonical_json_bytes_v1({
        "id": str(event.event_id), "category": event.stream_id.category,
        "aggregate": str(event.stream_id.aggregate_id), "version": event.stream_version,
        "type": event.event_type, "schema": event.schema_version,
        "commit": str(event.commit_id), "commit_index": event.commit_index,
        "commit_size": event.commit_size, "payload": event.payload,
        "command": str(event.metadata.command_id),
    }))) for event in business_events(store)]


def context_for(request):
    model_turn_id = identity("trace-model-turn")
    return ToolExecutionContext(
        UUID(request["run_id"]), model_turn_id, 1, ModelCallRef(model_turn_id, CALL.call_id),
        turn_id=UUID(request["turn_id"]), turn_version=request["turn_version"],
    )


class EffectDelegate:
    def __init__(self, root, *, forbid_execute=False):
        self.root, self.forbid_execute = root, forbid_execute

    def definitions(self):
        return (TOOL,)

    def execute(self, call, *, context):
        if self.forbid_execute:
            raise AssertionError("recovery_reexecuted_committed_tool")
        result = subprocess.run(
            [sys.executable, "-c", EFFECT_SCRIPT, str(self.root / "effect.json"), str(context.execution_id)],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise AssertionError("fixture_effect_process_failed")
        return ToolExecutionResult("committed-result")


class CompetingTraceStore:
    def __init__(self, path):
        self.path = path
        self.store = store_at(path)
        self.attempts = 0
        self.baseline = None

    def read_stream(self, *args, **kwargs):
        return self.store.read_stream(*args, **kwargs)

    def append_batch(self, writes, *, idempotency_key):
        event = writes[0].events[0]
        if event.payload["kind"] == "result":
            if self.baseline is None:
                self.baseline = business_digest(store_at(self.path))
            self.attempts += 1
            # A separate store/connection commits after the first reader took
            # its head. The original append below must now fail exact CAS.
            TraceStore(store_at(self.path)).append(
                correlation_id=writes[0].stream_id.aggregate_id,
                stream="tool", kind="competitor", fields={"attempt": self.attempts},
            )
        return self.store.append_batch(writes, idempotency_key=idempotency_key)


class TraceKillPort(NoOpFaultPort):
    def __init__(self, root, request, race):
        self.root, self.request, self.race = root, request, race

    def hit(self, point, facts):
        super().hit(point, facts)
        if point != self.request["point"]:
            return
        store = store_at(self.root / "runtime.db")
        context = context_for(self.request)
        record = ToolLedgerStore(store).load_for_call(context.turn_id, context.model_turn_id, CALL.call_id)
        assert record.state.value == "succeeded" and record.result.content == "committed-result"
        effect = json.loads((self.root / "effect.json").read_text(encoding="utf-8"))
        assert effect["execution_id"] == str(record.execution_id)
        assert business_digest(store) == self.race.baseline, "trace_altered_committed_business"
        expected = 1 if point == TRACE_POINTS[0] else RETRY_LIMIT
        assert self.race.attempts == expected, "trace_retry_not_bounded"
        trace = TraceStore(store).read(UUID(self.request["correlation_id"]))
        assert [item.kind for item in trace] == ["prepared", "claimed"] + ["competitor"] * expected
        atomic_json(self.root / "ready.json", {
            "point": point, "point_class": FAULT_SPECS[point].point_class.value,
            "crash_pid": os.getpid(), "attempts": expected,
            "business_digest": self.race.baseline, "trace_count": len(trace),
            "effect_digest": hashlib.sha256((self.root / "effect.json").read_bytes()).hexdigest(),
        })
        Event().wait()


def crash_trace(root, point):
    if point not in TRACE_POINTS:
        raise ValueError("unsupported trace scenario")
    store = store_at(root / "runtime.db")
    runtime = ThreadRuntime(store)
    thread = runtime.create_thread("trace-fixture", command_id=identity("trace-thread"))
    turn = runtime.create_turn(thread.thread_id, "fixture", expected_thread_version=thread.version,
                               command_id=identity("trace-turn"))
    running = runtime.start_turn(turn.turn_id, turn.version, command_id=identity("trace-start"))
    request = {"point": point, "turn_id": str(turn.turn_id), "run_id": str(running.current_run_id),
               "turn_version": running.version, "correlation_id": str(identity("trace-correlation"))}
    atomic_json(root / "request.json", request)
    race = CompetingTraceStore(root / "runtime.db")
    sink = BestEffortTraceSink(TraceStore(race), cas_retries=RETRY_LIMIT,
                               fault_port=TraceKillPort(root, request, race))
    executor = LedgerExecutor(EffectDelegate(root), ToolLedgerStore(store), {TOOL.name: MANUAL_WRITE_PROFILE},
                              trace_sink=sink, correlation_id=UUID(request["correlation_id"]))
    executor.execute(CALL, context=context_for(request))
    raise AssertionError("trace production path missed kill point")


def recover_trace(root):
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    marker = json.loads((root / "ready.json").read_text(encoding="utf-8"))
    store = store_at(root / "runtime.db")
    assert business_digest(store) == marker["business_digest"]
    assert len(TraceStore(store).read(UUID(request["correlation_id"]))) == marker["trace_count"]
    ledger = ToolLedgerStore(store)
    executor = LedgerExecutor(EffectDelegate(root, forbid_execute=True), ledger, {TOOL.name: MANUAL_WRITE_PROFILE})
    context = context_for(request)
    for _ in range(2):
        result = executor.execute(CALL, context=context)
        assert result.content == "committed-result" and not result.is_error
    assert business_digest(store) == marker["business_digest"], "trace_recovery_duplicated_business"
    assert hashlib.sha256((root / "effect.json").read_bytes()).hexdigest() == marker["effect_digest"]
    record = ledger.load_for_call(context.turn_id, context.model_turn_id, CALL.call_id)
    assert record.state.value == "succeeded" and record.claim_epoch == 1
    runtime = ThreadRuntime(store)
    for _ in range(2):
        final = runtime.complete_turn(context.turn_id, "done", expected_version=context.turn_version,
                                      run_id=context.run_id, command_id=identity("trace-terminal"))
        assert final.status is TurnStatus.COMPLETED
    atomic_json(root / "recovered.json", {
        "point": request["point"], "recovery_pid": os.getpid(), "final_status": final.status.value,
        "normalized_business": normalized_business(store), "claim_epoch": record.claim_epoch,
        "trace_attempts_before_kill": marker["attempts"], "external_effects": 1,
    })
