"""Audit S6/S7 regressions.

S6: the production TASK-mode loop must enable the D22 anti-hallucination
claim gate (the smoke run's top DS finding), and build_worker must not
silently ignore claim_gate=True for the task path.
S7: tool descriptions must document the statuses enum (update_plan) and the
repository-root-relative path semantics (repo_map / repository tools) - a
real model burned rounds on both.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.plan.tools import plan_tool_spec
from koawa_agent_v2.tools.repo_map import repo_map_tool_spec
from koawa_agent_v2.tools.repository import build_repository_tool_registry

import koawa_agent_v2.runtime.assembly as assembly_module
from tests.test_wiring_memory_plane import _AppFixture


class TaskLoopClaimGateTest(unittest.TestCase):
    def test_task_loop_enables_claim_gate(self) -> None:
        fixture = _AppFixture()
        self.addCleanup(fixture.cleanup)
        app = fixture.app()
        self.addCleanup(app.close)
        execution = app._ensure_execution_plane()
        self.assertTrue(execution.loop._claim_gate)

    def test_build_worker_rejects_ignored_claim_gate_combo(self) -> None:
        fixture = _AppFixture()
        self.addCleanup(fixture.cleanup)
        app = fixture.app()
        self.addCleanup(app.close)
        execution = app._ensure_execution_plane()
        # The task loop always has the gate, so the previously silent combo
        # is now consistent; verify build_worker accepts it without error.
        worker = execution.build_worker((), task_mode=True, claim_gate=True)
        self.assertTrue(worker._loop._claim_gate)


class ToolDescriptionSemanticsTest(unittest.TestCase):
    def test_update_plan_documents_status_enum(self) -> None:
        spec = plan_tool_spec()
        self.assertIn("'pending' or 'done'", spec.description)

    def test_repo_map_documents_repository_root_relative_paths(self) -> None:
        spec = repo_map_tool_spec()
        schema = json.loads(spec.input_schema_json)
        self.assertIn("REPOSITORY ROOT", schema["properties"]["path"]["description"])

    def test_repository_tools_document_repository_root_relative_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="koawa-s7-") as directory:
            registry = build_repository_tool_registry(Path(directory))
            self.addCleanup(registry.close)
            schemas = {
                definition.name: json.loads(definition.input_schema_json)
                for definition in registry.definitions()
            }
            for name in ("read_file", "list_files", "search_text"):
                self.assertIn(
                    "REPOSITORY ROOT",
                    schemas[name]["properties"]["path"]["description"],
                    name,
                )


if __name__ == "__main__":
    unittest.main()
