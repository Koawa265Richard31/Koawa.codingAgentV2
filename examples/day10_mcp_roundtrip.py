"""D10 walkthrough: real stdio MCP fixture through session, catalog and ledger.

Run from ``v2/`` with ``PYTHONPATH=src``:

    python -B examples/day10_mcp_roundtrip.py

The fixture server is a real subprocess speaking Content-Length framed
JSON-RPC 2.0; nothing here is mocked.  Real network egress stays at zero.
"""

from __future__ import annotations

import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.approval_service import ApprovalService
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.ledger import (
    LedgerExecutor,
    MANUAL_WRITE_PROFILE,
    READ_ONLY_PROFILE,
    ToolExecutionState,
    ToolLedgerStore,
    ToolOutcomeBlocked,
)
from koawa_agent_v2.mcp import (
    McpSession,
    McpSessionError,
    StdioTransport,
    build_mcp_registry,
    spawn_fixture_command,
)
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.policy import (
    ActionKind,
    Decision,
    PolicyEngine,
    PolicyRule,
    Principal,
    ResolvedAction,
    SideEffectClass as PolicySideEffectClass,
    canonical_arguments,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _call(session: McpSession, binding, arguments: str):
    return session.call(binding, arguments)


def main() -> dict:
    report: dict = {}
    temporary = tempfile.TemporaryDirectory()
    database = Path(temporary.name) / "day10.sqlite3"
    store = SqliteEventStore(database)
    runtime = ThreadRuntime(store, actor="day10")
    ledger = ToolLedgerStore(store)
    approvals = ApprovalService(
        store, ledger, budget_action_limits={"root": 100}
    )
    principal = Principal("root", ("mcp.use",))

    env = {
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "KOAWA_MCP_FIXTURE_CALL_DELAY_MS": "2000",
    }
    transport = StdioTransport(
        spawn_fixture_command(),
        env=env,
        cwd=str(REPO_ROOT),
    )
    session = McpSession("server", transport, request_timeout=2.0)
    try:
        catalog = session.connect()
        report["catalog"] = {
            "generation": catalog.generation,
            "tools": list(catalog.bindings),
        }
        echo_binding = catalog.bindings["server__echo"]
        fail_binding = catalog.bindings["server__fail"]
        slow_binding = catalog.bindings["server__slow"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_call, session, echo_binding, '{"value":"alpha"}'),
                pool.submit(_call, session, echo_binding, '{"value":"beta"}'),
            ]
            concurrent = [future.result() for future in futures]
        assert all(not item.is_error for item in concurrent)
        assert "alpha" in concurrent[0].content or "beta" in concurrent[0].content
        report["concurrent_calls"] = len(concurrent)

        failed = session.call(fail_binding, "{}")
        assert failed.is_error
        report["typed_error"] = failed.content

        slow = session.call(slow_binding, "{}", timeout=0.2)
        assert slow.uncertain
        report["slow_timeout"] = {"uncertain": slow.uncertain, "is_error": slow.is_error}

        # One durable chain: slow call through Registry -> Policy -> Ledger.
        thread = runtime.create_thread("day10-mcp")
        queued = runtime.create_turn(
            thread.thread_id,
            "mcp ledger walkthrough",
            expected_thread_version=thread.version,
        )
        running = runtime.start_turn(queued.turn_id, queued.version)
        model_turn_id = uuid4()
        call = ToolCallItem(
            0, "item-mcp-slow", "call-mcp-slow", "server__slow", "{}",
        )
        context = ToolExecutionContext(
            running.current_run_id,
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, call.call_id),
            turn_id=running.turn_id,
            turn_version=running.version,
        )
        delegate = build_mcp_registry(session, catalog)
        rule = PolicyRule(
            "mcp-slow",
            Decision.ALLOW,
            action_kinds=(ActionKind.MCP_TOOL,),
            tool_names=("server__slow",),
            principal_ids=("root",),
            required_scopes=("mcp.use",),
        )
        engine = PolicyEngine("policy-v1", (rule,))
        resolver = lambda item, c, profile, previous: ResolvedAction(
            kind=ActionKind.MCP_TOOL,
            tool_name=item.name,
            canonical_arguments_json=canonical_arguments(item.arguments_json),
            principal=principal,
            side_effect_class=PolicySideEffectClass(
                profile.side_effect_class.value
            ),
            sandbox_profile_id="d10-mcp",
            policy_version="policy-v1",
            mcp_server_id=session.server_id,
            mcp_session_generation=session.generation,
            mcp_schema_hash=slow_binding.schema_hash,
        )
        executor = LedgerExecutor(
            delegate,
            ledger,
            {
                "server__echo": READ_ONLY_PROFILE,
                "server__fail": READ_ONLY_PROFILE,
                "server__slow": MANUAL_WRITE_PROFILE,
            },
            policy_engine=engine,
            approval_service=approvals,
            action_resolvers={
                name: resolver
                for name in ("server__echo", "server__fail", "server__slow")
            },
        )
        ticket = executor.authorize(call, context=context)
        try:
            executor.execute_authorized(ticket)
        except ToolOutcomeBlocked as error:
            assert error.code == "tool_outcome_unknown"
        record = ledger.load(ticket.record.execution_id)
        assert record.state is ToolExecutionState.OUTCOME_UNKNOWN
        report["ledger_outcome_unknown"] = record.state.value

        refreshed = session.refresh()
        assert refreshed.generation == 2
        try:
            session.call(echo_binding, '{"value":"stale"}')
            stale_rejected = False
        except McpSessionError as error:
            stale_rejected = error.code == "mcp_binding_stale"
        assert stale_rejected
        report["refresh"] = {
            "generation": refreshed.generation,
            "old_binding_rejected": True,
        }
        report["all_assertions_passed"] = True
    finally:
        session.close()
        temporary.cleanup()
    return {
        "storage": {
            "temporary_sqlite": True,
            "external_services": [],
        },
        **report,
    }


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2, sort_keys=True))
    sys.exit(0)
