"""Deterministic I8 datasets and measurements through production ports.

Dataset construction is outside timed regions. No runtime object is persisted:
the seed manifest and subprocess requests contain JSON identities only.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.agents.messages import MessageKind
from koawa_agent_v2.control.event_store import EventMetadata, NewEvent, StreamId, StreamWrite
from koawa_agent_v2.control.durable_json import canonical_json_bytes_v1
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.model.protocol import UserMessage
from koawa_agent_v2.recovery import CheckpointStore, RecoveryCoordinator, execution_seed
from koawa_agent_v2.recovery.context import projection_digest, reduce_execution
from koawa_agent_v2.recovery.store import RecoverableTurn


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def identity(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"koawa-stability-benchmark-v1:{name}")


def store_at(path: Path) -> SqliteEventStore:
    return SqliteEventStore(path, busy_timeout_ms=5000)


def control_at(path: Path, capacity: int) -> AgentControlPlane:
    return AgentControlPlane(
        store_at(path),
        limits=AgentBudgetLimits(4, capacity + 1, capacity),
        clock=lambda: NOW,
    )


def read_events(store: SqliteEventStore, stream: StreamId) -> tuple:
    result = []
    cursor = -1
    while True:
        page = store.read_stream(stream, after_version=cursor, limit=500)
        result.extend(page)
        if len(page) < 500:
            return tuple(result)
        cursor = page[-1].stream_version


def event_digest(store: SqliteEventStore) -> str:
    """Hash the whole logical dataset, not its shape or SQLite page layout.

Only wall-clock storage fields are omitted. Event IDs, commands, stream
versions, payloads and ordering remain part of the fingerprint.
"""
    digest = hashlib.sha256()
    cursor = 0
    while True:
        page = store.read_all(after_position=cursor, limit=500)
        for event in page:
            payload = json.loads(canonical_json_bytes_v1(event.payload))
            document = {
                "id": str(event.event_id), "stream": event.stream_id.key,
                "version": event.stream_version, "type": event.event_type,
                "schema": event.schema_version, "payload": payload,
                "command": str(event.metadata.command_id),
            }
            digest.update(json.dumps(document, sort_keys=True, separators=(",", ":")).encode())
            digest.update(b"\n")
        if len(page) < 500:
            return digest.hexdigest()
        cursor = page[-1].global_position


def clone_database(source: Path, target: Path) -> None:
    """SQLite online backup includes committed WAL; never copy only main DB."""
    with closing(sqlite3.connect(source)) as reader:
        with closing(sqlite3.connect(target)) as writer:
            reader.backup(writer)


def pragmas(store: SqliteEventStore) -> dict:
    # Instrument the production connection, including connection-local flags.
    with closing(store._connect()) as connection:
        return {
            name: connection.execute(f"PRAGMA {name}").fetchone()[0]
            for name in ("journal_mode", "synchronous", "foreign_keys", "busy_timeout",
                         "page_size", "cache_size", "temp_store")
        }


def seed_execution(path: Path, event_count: int) -> dict:
    if event_count < 4:
        raise ValueError("execution dataset requires at least four events")
    store = store_at(path)
    runtime = ThreadRuntime(store)
    thread = runtime.create_thread("benchmark-workspace", command_id=identity("thread"))
    turn = runtime.create_turn(
        thread.thread_id, "benchmark", expected_thread_version=thread.version,
        command_id=identity("turn"),
    )
    running = runtime.start_turn(
        turn.turn_id, turn.version, command_id=identity("start"),
        execution_seed=execution_seed(
            (UserMessage("benchmark-input", "benchmark"),),
            provider="fixture", model="fixture", max_output_tokens=256,
        ),
        execution_expected_version=-1, lease_owner_id="benchmark", lease_seconds=1,
    )
    stream = StreamId("run-execution", turn.turn_id)
    head = read_events(store, stream)[-1].stream_version
    # A real model -> tool-start -> tool-result transcript. Prefixes may end
    # with a pending call, which must survive reconstruction, not be discarded.
    for first in range(head + 1, event_count, 100):
        command = identity(f"execution-batch:{first}")
        events = []
        for index in range(first, min(first + 100, event_count)):
            round_number = (index - 1) // 3 + 1
            call = f"call-{round_number}"
            model_turn = str(identity(f"model-turn:{round_number}"))
            payload = {
                "thread_id": str(thread.thread_id), "turn_id": str(turn.turn_id),
                "run_id": str(running.current_run_id),
            }
            phase = (index - 1) % 3
            if phase == 0:
                event_type = "model.turn-completed.v1"
                payload.update({
                    "context_items": [{
                        "kind": "tool_call", "provider": "fixture",
                        "model_turn_id": model_turn, "call_id": call,
                        "item": {"item_id": call, "index": 0, "name": "read_file",
                                 "arguments_json": '{"path":"fixture.txt"}'},
                    }],
                    "model_turn": {"final_text": ""}, "model_round": round_number,
                    "output_chars": 0, "input_tokens": round_number * 10,
                    "output_tokens": round_number * 5, "next_phase": "ready_for_tool",
                })
            elif phase == 1:
                event_type = "run.phase-advanced.v1"
                payload.update({"phase": "tool_in_progress", "call_id": call, "tool_name": "read_file"})
            else:
                event_type = "tool.result-recorded.v1"
                payload.update({"tool_count": round_number, "context_item": {
                    "kind": "tool_result", "model_turn_id": model_turn,
                    "call_id": call, "content": "fixture content", "is_error": False,
                }})
            events.append(NewEvent(
                identity(f"execution-event:{index}"), event_type, 1, NOW, payload,
                EventMetadata(command, turn.turn_id, thread_id=thread.thread_id,
                              turn_id=turn.turn_id, run_id=running.current_run_id,
                              actor="benchmark"),
            ))
        store.append_batch((StreamWrite(stream, first - 1, tuple(events)),),
                           idempotency_key=command)
    events = read_events(store, stream)
    projection = reduce_execution(events)
    CheckpointStore(store).publish_from_source(
        thread_id=thread.thread_id, turn_id=turn.turn_id, run_id=running.current_run_id,
        turn_version=running.version, source_event=events[-1], projection=projection,
    )
    return {
        "thread_id": str(thread.thread_id), "turn_id": str(turn.turn_id),
        "run_id": str(running.current_run_id), "turn_version": running.version,
        "event_count": len(events), "projection_digest": projection_digest(projection),
        "dataset_digest": event_digest(store),
    }


def seed_agents(path: Path, agents: int, messages: int) -> dict:
    control = control_at(path, agents)
    root = control.spawn_agent(
        parent_agent_id=None, task_id="root", principal_id="benchmark", scopes=("read",),
        semantic_idempotency_key="benchmark-root",
    )
    children = []
    for index in range(agents):
        child = control.spawn_agent(
            parent_agent_id=root.agent_id, task_id=f"task-{index}",
            principal_id="benchmark", scopes=("read",),
            semantic_idempotency_key=f"benchmark-child-{index}",
        )
        children.append(str(child.agent_id))
        for message in range(messages):
            control.send_message(
                child.agent_id, from_agent_id=root.agent_id, kind=MessageKind.TASK,
                body_ref=f"task-{message}", idempotency_key=f"message-{message}",
            )
    return {"root_id": str(root.agent_id), "children": children, "capacity": agents,
            "messages_per_agent": messages, "dataset_digest": event_digest(control.event_store)}


def seed_spawn(path: Path) -> dict:
    control = control_at(path, 100)
    root = control.spawn_agent(
        parent_agent_id=None, task_id="root", principal_id="benchmark", scopes=("read",),
        semantic_idempotency_key="spawn-root",
    )
    return {"root_id": str(root.agent_id), "capacity": 100,
            "dataset_digest": event_digest(control.event_store)}


def operation(name: str, path: Path, manifest: dict):
    """Construct clients before timing; every store operation opens a new connection."""
    if name == "verified_event_rebuild_10k":
        store = store_at(path)
        coordinator = RecoveryCoordinator(ThreadRuntime(store), CheckpointStore(store), owner_id="benchmark")
        item = RecoverableTurn(
            turn_id=UUID(manifest["turn_id"]), turn_version=manifest["turn_version"],
            run_id=UUID(manifest["run_id"]), lease_expires_at=NOW,
        )

        def rebuild():
            projection = coordinator.reconstruct(item)
            if projection.execution_version != manifest["event_count"] - 1:
                raise RuntimeError("benchmark_execution_count_mismatch")
            if projection_digest(projection) != manifest["projection_digest"]:
                raise RuntimeError("benchmark_projection_mismatch")
        return rebuild
    control = control_at(path, manifest["capacity"])
    root_id = UUID(manifest["root_id"])
    if name == "uncontended_spawn_transaction":
        def spawn():
            child = control.spawn_agent(
                parent_agent_id=root_id, task_id="sample-child", principal_id="benchmark",
                scopes=("read",), semantic_idempotency_key="sample-child",
            )
            if child.parent_agent_id != root_id:
                raise RuntimeError("benchmark_spawn_identity_mismatch")
        return spawn
    if name in ("mailbox_next", "mailbox_list"):
        child_id = UUID(manifest["children"][0])
        def mailbox():
            messages = control.mailbox.queued(child_id) if name == "mailbox_next" else control.mailbox.load(child_id)
            if len(messages) != manifest["messages_per_agent"]:
                raise RuntimeError("benchmark_mailbox_count_mismatch")
        return mailbox
    if name in ("wait_agents_100", "agent_list"):
        def children():
            rows = control.wait_agents(root_id, timeout_seconds=0) if name == "wait_agents_100" else control.list_agents(root_id)
            if len(rows) != len(manifest["children"]):
                raise RuntimeError("benchmark_agent_count_mismatch")
        return children
    raise ValueError("unknown benchmark operation")
