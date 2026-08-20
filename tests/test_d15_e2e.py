from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.runtime.cli import doctor_command, run_command, status_command


class D15CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.db = root / "agent.sqlite3"
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "in.txt").write_text("input\n", encoding="utf-8")

    def test_run_status_doctor_and_resume_durability(self) -> None:
        result = run_command(self.db, self.repo)
        self.assertEqual("completed", result["status"])
        self.assertEqual("patched\n", result["out.txt"])
        status = status_command(self.db)
        self.assertEqual(1, status["threads"])
        self.assertEqual("completed", status["turns"][0]["status"])
        doctor = doctor_command(self.db)
        self.assertTrue(doctor["ok"])

    def test_cli_entry_points_are_dispatch_contract_safe(self) -> None:
        # The CLI worker routes the patch through Registry -> Policy -> Ledger;
        # the raw registry would reject the call once policy-bound (D9 contract).
        from koawa_agent_v2.ledger import LedgerExecutor
        from koawa_agent_v2.tools.registry import ToolRegistry
        from koawa_agent_v2.tools.errors import ToolRegistryError
        from koawa_agent_v2.execution.loop import ToolExecutionContext
        from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
        from uuid import uuid4

        registry = ToolRegistry()
        authority = object()
        registry.bind_policy_authority(authority)
        model_turn_id = uuid4()
        call = ToolCallItem(0, "item", "call", "write_patch", "{}")
        context = ToolExecutionContext(
            uuid4(),
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, call.call_id),
        )
        with self.assertRaises(ToolRegistryError) as raised:
            registry.execute(call, context=context)
        self.assertEqual("policy_authorization_required", raised.exception.code)

    def test_dispatch_contract_mcp_entry(self) -> None:
        from koawa_agent_v2.mcp.tool_binding import bind_catalog, build_mcp_registry
        from koawa_agent_v2.tools.errors import ToolRegistryError

        catalog = bind_catalog(
            "server",
            1,
            [
                {
                    "name": "echo",
                    "description": "echo",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                }
            ],
        )

        class DummySession:
            def handler(self, binding):
                return lambda arguments, *, context: None

        adapter = build_mcp_registry(DummySession(), catalog)
        adapter.bind_policy_authority(object())
        model_turn_id = __import__("uuid").uuid4()
        from koawa_agent_v2.execution.loop import ToolExecutionContext
        from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem

        call = ToolCallItem(0, "item", "call", "server__echo", "{}")
        context = ToolExecutionContext(
            __import__("uuid").uuid4(),
            model_turn_id,
            1,
            ModelCallRef(model_turn_id, call.call_id),
        )
        with self.assertRaises(ToolRegistryError) as raised:
            adapter.execute(call, context=context)
        self.assertEqual("policy_authorization_required", raised.exception.code)

    def test_restart_rebuilds_state_from_same_sqlite(self) -> None:
        run_command(self.db, self.repo)
        reloaded = status_command(self.db)
        self.assertEqual(1, reloaded["threads"])
        self.assertEqual("completed", reloaded["turns"][0]["status"])

    def test_resume_and_cancel_commands(self) -> None:
        from koawa_agent_v2.runtime.cli import cancel_command, resume_command
        from koawa_agent_v2.control.runtime import ThreadRuntime
        from koawa_agent_v2.control.sqlite_store import SqliteEventStore

        store = SqliteEventStore(self.db)
        runtime = ThreadRuntime(store, actor="resume")
        thread = runtime.create_thread("resume-repo")
        queued = runtime.create_turn(
            thread.thread_id,
            "resume me",
            expected_thread_version=thread.version,
        )
        resumed = resume_command(self.db, str(queued.turn_id), self.repo)
        self.assertEqual("completed", resumed["status"])
        self.assertEqual("patched\n", (self.repo / "out.txt").read_text(encoding="utf-8"))

        thread2 = runtime.create_thread("cancel-repo")
        queued2 = runtime.create_turn(
            thread2.thread_id,
            "cancel me",
            expected_thread_version=thread2.version,
        )
        cancelled = cancel_command(self.db, str(queued2.turn_id))
        self.assertEqual("cancelled", cancelled["status"])


if __name__ == "__main__":
    unittest.main()
