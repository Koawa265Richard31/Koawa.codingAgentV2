"""Read-only durable runtime truth audit for the I9 release lane."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from collections import Counter
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote


class AuditError(RuntimeError):
    """Stable, content-free failure for an unreadable durable store."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _git_head() -> str | None:
    """Return the audited source identity without exposing command details."""
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return value if len(value) == 40 and all(char in "0123456789abcdef" for char in value) else None


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8", "strict")


def _read_only(path: Path) -> sqlite3.Connection:
    # Quote the path as a URI component.  In particular, a filename containing
    # ``#`` or ``?`` must not accidentally change the SQLite URI semantics.
    uri = f"file:{quote(str(path.resolve()), safe='/\\:')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise AuditError("database_open_failed") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        connection.close()
        raise AuditError("database_read_only_failed") from exc
    return connection


def audit_database(path: Path) -> dict:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError("database_not_found")
    generated_at = datetime.now(timezone.utc)
    connection = _read_only(path)
    try:
        tables = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        schema = connection.execute("PRAGMA user_version").fetchone()[0]
        clock_row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()
        database_now = datetime.fromisoformat(clock_row[0][:-1] + "+00:00") if clock_row and clock_row[0] else datetime.now(timezone.utc)
        counts: dict[str, int] = {}
        for table in sorted(tables):
            if table.startswith("sqlite_"): continue
            safe = table.replace('"', '""')
            counts[table] = int(connection.execute(f'SELECT COUNT(*) FROM "{safe}"').fetchone()[0])
        event_counts: Counter[str] = Counter()
        streams: dict[tuple[str, str], list[tuple[str, dict, str | None]]] = defaultdict(list)
        if "events" in tables:
            event_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(events)")
            }
            if "event_type" not in event_columns:
                raise AuditError("events_schema_invalid")
            selected = ["e.event_type" if "streams" in tables else "event_type"]
            for name in ("category", "aggregate_id", "payload_json"):
                if name in event_columns:
                    selected.append(("e." if "streams" in tables else "") + name)
            # The production schema normalizes stream identity into ``streams``;
            # join it for category/aggregate truth instead of guessing from the
            # human-readable ``category-uuid`` stream key.
            if "streams" in tables and "stream_id" in event_columns:
                selected = ["e.event_type", "s.category", "s.aggregate_id"]
                if "payload_json" in event_columns:
                    selected.append("e.payload_json")
                query = "SELECT " + ", ".join(selected) + " FROM events e JOIN streams s ON s.stream_id = e.stream_id ORDER BY e.global_position"
            else:
                query = "SELECT " + ", ".join(selected) + " FROM events"
                if "global_position" in event_columns:
                    query += " ORDER BY global_position"
            for row in connection.execute(query):
                event_type = row["event_type"]
                if not isinstance(event_type, str):
                    raise AuditError("event_type_invalid")
                event_counts[event_type] += 1
                payload: dict = {}
                if "payload_json" in event_columns:
                    try:
                        parsed = json.loads(row["payload_json"])
                    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
                        raise AuditError("event_payload_invalid") from exc
                    if not isinstance(parsed, dict):
                        raise AuditError("event_payload_invalid")
                    payload = parsed
                row_keys = set(row.keys())
                category = str(row["category"]) if "category" in row_keys else ""
                aggregate = str(row["aggregate_id"]) if "aggregate_id" in row_keys else ""
                streams[(category, aggregate)].append((event_type, payload, payload.get("expires_at")))

        # A report is built from event streams, not from mutable projections.
        # This keeps an incomplete/stale projection visible instead of allowing
        # it to certify an otherwise active run as clean.
        terminal_turn = {"turn.completed.v1", "turn.failed.v1", "turn.cancelled.v1", "turn.timed-out.v1"}
        terminal_run = {"run.interrupted.v1", "run.abandoned.v1", "run.completed.v1", "run.failed.v1", "run.cancelled.v1", "run.timed-out.v1", "run.outcome-unknown.v1"}
        terminal_agent = {"agent.completed.v1", "agent.completed.v2", "agent.failed.v1", "agent.failed.v2", "agent.cancelled.v1", "agent.cancelled.v2", "agent.orphaned.v1"}
        active_turns = sum(bool(items) and items[-1][0] not in terminal_turn for (category, _), items in streams.items() if category == "turn")
        active_runs = sum(bool(items) and items[-1][0] not in terminal_run for (category, _), items in streams.items() if category == "run")
        active_agents = sum(bool(items) and items[-1][0] not in terminal_agent for (category, _), items in streams.items() if category == "agent")

        def _active_ids(categories: tuple[str, ...], starts: set[str], ends: set[str]) -> set[str]:
            values: set[str] = set()
            for (category, aggregate), items in streams.items():
                if category in categories and items and items[0][0] in starts and items[-1][0] not in ends:
                    reservation = items[-1][1].get("reservation_id") or items[0][1].get("reservation_id")
                    values.add(str(reservation) if reservation is not None else f"{category}:{aggregate}")
            return values

        active_capacity = _active_ids(("agent-capacity",), {"agent.capacity-reserved.v1"}, {"agent.capacity-released.v1"})
        active_budget = _active_ids(("agent-budget",), {"budget.reserved.v1", "budget.reserved.v2"}, {"budget.released.v1", "budget.released.v2", "budget.legacy-reconciled.v1"})
        active_ledger = sum(bool(items) and items[-1][0] in {"tool.execution-claimed.v1", "tool.execution-reclaimed.v1", "tool.execution-outcome-unknown.v1"} for (category, _), items in streams.items() if category in {"tool-execution", "tool_ledger"})
        active_approvals = sum(bool(items) and items[-1][0] in {"approval.requested.v1", "approval.granted.v1"} for (category, _), items in streams.items() if category in {"approval", "approval_request"})
        active_mcp = sum(bool(items) and items[-1][0] in {"mcp.process-intended.v1", "mcp.process-claimed.v1", "mcp.process-started.v1", "mcp.process-ready.v1", "mcp.process-outcome-unknown.v1"} for (category, _), items in streams.items() if category in {"mcp-allocation", "mcp_allocations"})
        lease_expiries: list[datetime] = []
        for (category, _), items in streams.items():
            if not items or (
                category not in {"recovery-lease", "run_lease", "agent"}
                or items[-1][0] in terminal_agent
                or items[-1][0] in {"turn.recovery-lease-released.v1", "agent.orphaned.v1"}
            ):
                continue
            raw_expiry = next(
                (item[1].get("lease_expires_at") for item in reversed(items)
                 if isinstance(item[1].get("lease_expires_at"), str)),
                None,
            )
            if not isinstance(raw_expiry, str):
                continue
            try:
                expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
            except ValueError as exc:
                raise AuditError("lease_expiry_invalid") from exc
            if expiry.tzinfo is None:
                raise AuditError("lease_expiry_invalid")
            lease_expiries.append(expiry.astimezone(timezone.utc))
        live_leases = sum(expiry > database_now for expiry in lease_expiries)
        expired_leases = len(lease_expiries) - live_leases
        active = {
            "recoverable_turns": counts.get("recoverable_turns", active_turns),
            "runs": active_runs,
            "turns": active_turns,
            "agents": active_agents,
            "capacity_reservations": len(active_capacity),
            "budget_reservations": len(active_budget),
            "ledger_claims": active_ledger,
            "approval_requests": active_approvals,
            "mcp_allocations": active_mcp,
            "recovery_leases": live_leases,
        }
        # Release truth must come from durable streams.  Projections and
        # process-local caches are intentionally not consulted here.
        inventory: dict[str, str] = {}
        effect_states: dict[str, str] = {}
        durable_trace_events = 0
        durable_trace_drop_events = 0
        for (category, aggregate), items in streams.items():
            if category == "workspace":
                for event_type, payload, _ in items:
                    allocation = payload.get("allocation_id")
                    if not isinstance(allocation, str) or not allocation:
                        continue
                    if event_type == "workspace.inventory-active.v2":
                        inventory[allocation] = "active"
                    elif event_type == "workspace.inventory-state.v2":
                        inventory[allocation] = str(payload.get("state", "unknown"))
            elif category == "workspace-effect":
                for event_type, payload, _ in items:
                    effect_id = str(payload.get("effect_id") or aggregate)
                    state = {
                        "workspace.effect-intended.v2": "intended",
                        "workspace.effect-claimed.v1": "claimed",
                        "workspace.effect-applied.v2": "applied",
                        "workspace.effect-failed-before-effect.v1": "failed_before_effect",
                        "workspace.effect-outcome-unknown.v1": "outcome_unknown",
                        "workspace.effect-outcome-resolved.v1": "resolved",
                    }.get(event_type)
                    if state is not None:
                        effect_states[effect_id] = state
            elif category == "trace":
                durable_trace_events += len(items)
                durable_trace_drop_events += sum(
                    1 for event_type, payload, _ in items
                    if event_type.startswith("trace.")
                    and isinstance(payload.get("kind"), str)
                    and "drop" in payload["kind"]
                )
        inventory_states = Counter(inventory.values())
        effect_state_counts = Counter(effect_states.values())
        open_effect_states = {"intended", "claimed", "outcome_unknown"}
        workspace_effect_truth = {
            "inventory_available": bool(inventory),
            "inventory_source": "durable_workspace_streams",
            "inventory_unavailable_reason": None if inventory else "no_workspace_inventory_events",
            "inventory_records": len(inventory),
            "active_inventory": sum(state == "active" for state in inventory.values()),
            "inventory_states": dict(sorted(inventory_states.items())),
            "effect_states": dict(sorted(effect_state_counts.items())),
            "open_effects": sum(state in open_effect_states for state in effect_states.values()),
        }
        trace_truth = {
            "dropped_diagnostics": {
                "durable_drop_events": durable_trace_drop_events,
                "status": "unknown",
                "reason": (
                    "best_effort_trace_diagnostics_are_process_local"
                    if durable_trace_events
                    else "no_durable_trace_events_and_process_local_diagnostics"
                ),
            },
            "durable_trace_events": durable_trace_events,
        }
    finally:
        connection.close()
    raw = path.read_bytes()
    finished_at = datetime.now(timezone.utc)
    # Presence-only canary result: never copy or print matching database text.
    canary_patterns = (b"OPENAI_API_KEY=", b"sk-", b"Authorization: Bearer ")
    return {
        "report_schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        # The report is portable release evidence.  Keep only a non-sensitive
        # label; the full host path is neither needed for durable truth nor
        # safe to expose through the CLI.
        "database": path.name,
        "commit": _git_head(),
        "database_digest": hashlib.sha256(raw).hexdigest(),
        "schema": {"user_version": schema, "tables": counts},
        "active_run_turn_agent": {
            "recoverable_turns": active["recoverable_turns"],
            "runs": active["runs"], "turns": active["turns"], "agents": active["agents"],
        },
        "messages": {
            "delivered": event_counts["message.delivered.v1"] + event_counts["message.delivered.v2"],
            "result_recorded": event_counts["message.result-recorded.v1"],
            "unresolved": event_counts["message.unresolved.v1"],
            "requeued": event_counts["message.requeued.v1"],
        },
        "active_resources": active,
        "unclosed_ledger_claims": active["ledger_claims"],
        "pending_approvals": active["approval_requests"],
        "leases": {"live_or_expired": len(lease_expiries), "live": live_leases, "expired": expired_leases},
        "mcp_allocations": active["mcp_allocations"],
        "workspace_effects": workspace_effect_truth,
        "trace": trace_truth,
        "canary_scan": {"credential_literals_present": any(marker in raw for marker in canary_patterns)},
        "read_only": True,
    }


def write_report(path: Path, document: dict) -> str:
    body = dict(document); body.pop("report_digest", None)
    document = {**body, "report_digest": hashlib.sha256(canonical_bytes(body)).hexdigest()}
    path = path.resolve(); path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(document)); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return document["report_digest"]
