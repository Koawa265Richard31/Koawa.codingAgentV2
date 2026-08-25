"""Deterministic legacy (pre-I5 unversioned) database builder for tests.

Replicates the historical baseline DDL exactly (event store plus the D6
CheckpointStore tables) so the schema manager classifies the file as
database_legacy_export_required, matching the registered fingerprints.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.control.schema import _LEGACY_D6_DDL, _V1_DDL, _statements


def build_legacy_database(
    path: Path | str,
    *,
    canary: str = "sk-legacy-RAW-SECRET-3f9a",
    with_d6_tables: bool = True,
    terminal_summary: str | None = None,
    include_active_turn: bool = False,
    include_unknown_table: bool = False,
) -> dict:
    """Create a legacy unversioned DB; returns its thread/turn identifiers."""
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        for statement in _statements(_V1_DDL):
            if "schema_migrations" in statement:
                continue
            connection.execute(statement)
        if with_d6_tables:
            for statement in _statements(_LEGACY_D6_DDL):
                connection.execute(statement)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        thread_id = uuid4()
        turn_id = uuid4()
        active_turn_id = uuid4() if include_active_turn else None
        events = [
            ("thread", thread_id, "thread.created.v1", {"thread_id": str(thread_id), "workspace_ref": "repo"}, None),
            ("turn", turn_id, "turn.created.v1", {"turn_id": str(turn_id), "thread_id": str(thread_id), "user_input": canary}, None),
            ("turn", turn_id, "turn.started.v1", {"run_id": str(uuid4()), "attempt": 1}, None),
        ]
        if terminal_summary is not None:
            events.append(("turn", turn_id, "turn.completed.v1", {"summary": terminal_summary + " " + canary}, None))
        else:
            events.append(("turn", turn_id, "turn.completed.v1", {"summary": "done " + canary}, None))
        if include_active_turn:
            events.append(("turn", active_turn_id, "turn.created.v1", {"turn_id": str(active_turn_id), "thread_id": str(thread_id), "user_input": canary}, None))
            events.append(("turn", active_turn_id, "turn.started.v1", {"run_id": str(uuid4()), "attempt": 1}, None))
        _append_events(connection, events, now)
        if include_unknown_table:
            connection.execute("CREATE TABLE strange_foreign_table(value TEXT)")
        connection.commit()
    finally:
        connection.close()
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "active_turn_id": active_turn_id,
    }


def build_versioned_v1_database(path: Path | str) -> None:
    """Registered v1 database (0001 ledger + user_version=1)."""
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        for statement in _statements(_V1_DDL):
            connection.execute(statement)
        checksum = _checksum_of(_V1_DDL)
        connection.execute(
            "INSERT INTO schema_migrations(migration_id,from_version,to_version,checksum,applied_at) VALUES(?,?,?,?,?)",
            ("0001_event_store_v1", 0, 1, checksum, "2026-01-01T00:00:00.000000Z"),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()


def _checksum_of(ddl: str) -> str:
    import hashlib

    return hashlib.sha256(" ".join(ddl.split()).strip().encode("utf-8")).hexdigest()


def _append_events(connection, events, now) -> None:
    for category, aggregate, event_type, payload, _metadata in events:
        stream_id = f"{category}-{aggregate}"
        connection.execute(
            "INSERT OR IGNORE INTO streams(stream_id,category,aggregate_id,current_version) VALUES(?,?,?,?)",
            (stream_id, category, str(aggregate), -1),
        )
        head = connection.execute("SELECT current_version FROM streams WHERE stream_id=?", (stream_id,)).fetchone()[0]
        version = int(head) + 1
        connection.execute(
            "INSERT INTO events(event_id,stream_id,stream_version,commit_id,commit_index,commit_size,event_type,schema_version,occurred_at,recorded_at,payload_json,metadata_json) VALUES(?,?,?,?,0,1,?,1,?,?,?,?)",
            (
                str(uuid4()),
                stream_id,
                version,
                str(uuid4()),
                event_type,
                now,
                now,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                json.dumps({"actor": "legacy", "command_id": str(uuid4()), "correlation_id": str(uuid4())}, sort_keys=True, separators=(",", ":")),
            ),
        )
        connection.execute("UPDATE streams SET current_version=? WHERE stream_id=?", (version, stream_id))