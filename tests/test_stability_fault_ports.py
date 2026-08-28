from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.agents.control import AgentControlPlane
from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.agents.messages import MessageKind
from koawa_agent_v2.agents.scheduler import AgentScheduler, ScriptedAgentProvider
from koawa_agent_v2.control.schema import inject_fault, register_fault_hook
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.telemetry.faults import (
    FaultPoint, InjectedFault, NoOpFaultPort, RecordingFaultPort, adapt_fault_callback,
    using_fault_port, validate_fault_facts,
)


class FaultMetadataTest(unittest.TestCase):
    def test_mcp_page_and_generation_metadata_are_bounded_counters(self):
        facts = {"server_id": "fixture", "page": 1, "tool_count": 100, "generation": 2}
        self.assertEqual(facts, dict(validate_fault_facts(facts)))
        for key in ("page", "tool_count", "generation"):
            for value in (-1, True, 2**63, "1"):
                with self.subTest(key=key, value=value), self.assertRaises(AgentError):
                    validate_fault_facts({**facts, key: value})

    def test_untrusted_fields_unicode_controls_and_number_edges_fail_content_free(self):
        bad = [{"user_text": "short secret"}, {"credential": "sk-private"}, {"env": {}},
               {"attempt": True}, {"attempt": -1}, {"version": -2}, {"count": 2**63},
               {"count": float("nan")}, {"count": float("inf")}, {"count": 1.5},
               {"kind": "\ud800"}, {"kind": "a\x00b"}, {"code": "a\nb"},
               {"agent_id": "user text"}, {"server_id": "x" * 65}, {"message_ids": [str(uuid4())]},
               {"kind": {"nested": ["object"]}}, {"is_error": 1}]
        for facts in bad:
            with self.subTest(fields=list(facts)), self.assertRaises(AgentError) as raised:
                validate_fault_facts(facts)
            self.assertEqual("invalid_fault_facts", str(raised.exception))

    def test_primitive_metadata_snapshot_is_immutable(self):
        original = {"agent_id": str(uuid4()), "version": -1, "attempt": 2**63 - 1,
                    "request_id": 5, "is_error": False}
        frozen = validate_fault_facts(original)
        original["version"] = 9
        self.assertEqual(-1, frozen["version"])
        with self.assertRaises(TypeError):
            frozen["version"] = 10

    def test_legacy_callback_keeps_ids_but_typed_port_only_sees_count_and_digest(self):
        port, legacy = RecordingFaultPort(), []
        callback = adapt_fault_callback(lambda name, facts: legacy.append(facts), port)
        ids = [str(uuid4()), str(uuid4())]
        callback(FaultPoint.D11_WAITING_AFTER_COMMIT, {"agent_id": str(uuid4()), "message_ids": ids})
        self.assertEqual(ids, legacy[0]["message_ids"])
        facts = port.hits[0][1]
        self.assertNotIn("message_ids", facts)
        self.assertEqual(2, facts["message_count"])
        self.assertEqual(64, len(facts["message_ids_digest"]))
        self.assertTrue(all(value is None or type(value) in (str, int, bool) for value in facts.values()))

    def test_unknown_configuration_fails_at_registration(self):
        with self.assertRaises(AgentError):
            RecordingFaultPort(raise_at=frozenset({"s3.unknown"}))
        with self.assertRaises(AgentError):
            register_fault_hook("s3.unknown", lambda: None)
        with self.assertRaises(TypeError):
            register_fault_hook(FaultPoint.S3_EVENT_AFTER_VALIDATE_BEFORE_BEGIN, 1)


class ProductionPortIntegrationTest(unittest.TestCase):
    def test_s3_context_injection_is_scoped_and_legacy_hook_remains_compatible(self):
        point = FaultPoint.S3_EVENT_AFTER_VALIDATE_BEFORE_BEGIN
        calls = []
        register_fault_hook(point, lambda: calls.append("legacy"))
        try:
            outer, inner = RecordingFaultPort(), RecordingFaultPort(raise_at=frozenset({point}))
            with using_fault_port(outer):
                inject_fault(point)
                with self.assertRaises(InjectedFault), using_fault_port(inner):
                    inject_fault(point)
                inject_fault(point)
            inject_fault(point)
            self.assertEqual(2, len(outer.hits))
            self.assertEqual(1, len(inner.hits))
            self.assertEqual(["legacy"] * 3, calls)
        finally:
            register_fault_hook(point, None)

    def test_d11_control_and_scheduler_emit_to_typed_port(self):
        with tempfile.TemporaryDirectory() as directory:
            port = RecordingFaultPort()
            control = AgentControlPlane(SqliteEventStore(Path(directory) / "runtime.db"), fault_port=port)
            root = control.spawn_agent(parent_agent_id=None, task_id="root", principal_id="test", scopes=("read",))
            child = control.spawn_agent(parent_agent_id=root.agent_id, task_id="task", principal_id="test", scopes=("read",))
            control.send_message(child.agent_id, from_agent_id=root.agent_id, kind=MessageKind.TASK,
                                 body_ref="task", idempotency_key="message")
            AgentScheduler(control, provider=ScriptedAgentProvider({"task": "ok"}), fault_port=port).run_attempt(child.agent_id)
            names = {name for name, _ in port.hits}
            for expected in (FaultPoint.D11_ENQUEUE_AFTER_COMMIT, FaultPoint.D11_PROVIDER_ENTERED,
                             FaultPoint.D11_PROVIDER_RETURNED, FaultPoint.D11_TERMINAL_AFTER_COMMIT):
                self.assertIn(expected, names)
            self.assertTrue(all(not isinstance(value, (list, tuple, dict)) for _, facts in port.hits for value in facts.values()))

    def test_fault_validation_rejects_before_legacy_callback(self):
        calls = []
        callback = adapt_fault_callback(lambda *args: calls.append(args))
        with self.assertRaises(AgentError):
            callback(FaultPoint.D11_ACK_BEFORE_APPEND, {"message_ids": ["not-an-id"]})
        self.assertEqual([], calls)

    def test_independent_cold_import_orders_have_no_runtime_cycle(self):
        root = Path(__file__).resolve().parents[1]
        for module in ("telemetry.faults", "control.schema", "agents.control", "telemetry.trace"):
            result = subprocess.run([sys.executable, "-c", f"import koawa_agent_v2.{module}"],
                                    cwd=root, env={**os.environ, "PYTHONPATH": str(root / "src")},
                                    capture_output=True, text=True, timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
