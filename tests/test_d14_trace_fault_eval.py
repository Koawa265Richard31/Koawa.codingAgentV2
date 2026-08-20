from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.telemetry.faults import (
    FAILURE_POINTS,
    FaultInjector,
    classify_failure,
)
from koawa_agent_v2.telemetry.trace import ALLOWED_FIELDS, TraceStore


class D14TraceTest(unittest.TestCase):
    def test_trace_redacts_and_allowlists_fields(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = TraceStore(SqliteEventStore(Path(temporary.name) / "t.sqlite3"))
        correlation_id = uuid4()
        store.append(
            correlation_id=correlation_id,
            stream="tool",
            kind="read_file",
            fields={
                "tool_name": "read_file",
                "result_code": "ok",
                "secret": "sk-raw-secret",
                "body": "full body must not persist",
            },
        )
        records = store.read(correlation_id)
        self.assertEqual(1, len(records))
        fields = records[0].fields
        self.assertEqual("read_file", fields["tool_name"])
        self.assertNotIn("secret", fields)
        self.assertNotIn("body", fields)
        with self.assertRaises(AgentError) as raised:
            store.append(
                correlation_id=correlation_id,
                stream="not-allowed",
                kind="x",
                fields={},
            )
        self.assertEqual("trace_stream_not_allowed", raised.exception.code)

    def test_fault_injector_deterministic_and_classify(self) -> None:
        first = FaultInjector(seed="s")
        second = FaultInjector(seed="s")
        points = sorted(FAILURE_POINTS)
        self.assertEqual(
            [first.should_fail(point) for point in points],
            [second.should_fail(point) for point in points],
        )
        self.assertEqual("timeout", classify_failure("mcp_timeout"))
        self.assertEqual("concurrency", classify_failure("db_version_conflict"))
        self.assertEqual("policy", classify_failure("approval_denied"))
        self.assertEqual("uncertainty", classify_failure("tool_outcome_unknown"))
        self.assertEqual("contract", classify_failure("malformed_json"))

    def test_eval_runner_writes_report(self) -> None:
        from evals.run_eval import main

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tasks_dir = Path(temporary.name) / "tasks"
        tasks_dir.mkdir()
        (tasks_dir / "a.json").write_text(
            json.dumps(
                {
                    "id": "a",
                    "files": {"x.txt": "x\n"},
                    "patch": {"x.txt": "x\npatched\n"},
                    "test": ["python", "-c", "assert 'patched' in open('x.txt').read()"],
                    "oracle": {"x.txt": "x\npatched\n"},
                }
            ),
            encoding="utf-8",
        )
        report_path = Path(temporary.name) / "report.json"
        report = main(tasks_dir, report_path, seed="eval")
        self.assertEqual(1, report["total"])
        self.assertEqual(1, report["success"])
        self.assertTrue(report_path.exists())

    def test_model_round_trace_through_agent_loop(self) -> None:
        from koawa_agent_v2.runtime.cli import _completed_stream
        from koawa_agent_v2.execution.loop import AgentLoop
        from koawa_agent_v2.model.protocol import AssistantTextItem, FinishReason

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        trace = TraceStore(SqliteEventStore(Path(temporary.name) / "t.sqlite3"))

        class FinalProvider:
            def stream(self, request):
                item = AssistantTextItem(0, "item-final", "done")
                yield from _completed_stream(
                    request, (item,), FinishReason.STOP, "response-final"
                )

        correlation_id = uuid4()
        loop = AgentLoop(
            FinalProvider(),
            tool_executor=None,
            trace_store=trace,
            correlation_id=correlation_id,
        )
        result = loop.run(
            run_id=uuid4(),
            input_items=(),
            provider="test-provider",
            model="test-model",
        )
        self.assertIsNotNone(result.final_text)
        records = trace.read(correlation_id)
        self.assertTrue(any(item.stream == "model" for item in records))


if __name__ == "__main__":
    unittest.main()
