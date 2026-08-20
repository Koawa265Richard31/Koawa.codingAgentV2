"""D14 walkthrough: trace, deterministic fault injection, eval report."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.telemetry.faults import FaultInjector
from koawa_agent_v2.telemetry.trace import TraceStore


def main() -> dict:
    temporary = tempfile.TemporaryDirectory()
    trace = TraceStore(SqliteEventStore(Path(temporary.name) / "trace.sqlite3"))
    correlation_id = uuid4()
    trace.append(
        correlation_id=correlation_id,
        stream="model",
        kind="round",
        fields={"kind": "round", "usage_tokens": 42, "result_code": "ok"},
    )
    trace.append(
        correlation_id=correlation_id,
        stream="ledger",
        kind="claim",
        fields={"kind": "claim", "result_code": "tool_outcome_unknown"},
    )
    injector = FaultInjector(seed="replay")
    points = [
        point
        for point in (
            "mcp_timeout",
            "db_version_conflict",
            "container_kill",
            "approval_loss",
        )
        if injector.should_fail(point)
    ]
    records = trace.read(correlation_id)
    assert len(records) == 2
    assert all("secret" not in item.fields for item in records)
    temporary.cleanup()
    return {
        "storage": {"temporary_sqlite": True},
        "trace_records": len(records),
        "injected_failures": points,
        "all_assertions_passed": True,
    }


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2, sort_keys=True))
    sys.exit(0)
