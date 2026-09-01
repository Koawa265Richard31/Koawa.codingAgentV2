from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from koawa_agent_v2.sandbox.runtime import DockerSandboxDoctor


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "tests" / "fixtures" / "golden_worker.py"
IMAGE_ID = "sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"

# I9 §11.3 is a lane contract, not a claim that one assertion proves every
# subsystem.  The golden lane's composite worker supplies the entries marked
# ``golden``; the focused durable tests are the independent evidence for the
# boundary probes.  Keeping the map here makes accidental matrix shrinkage
# visible in review and prevents Docker SKIP from being treated as PASS.
REQUIRED_MATRIX = {
    1: ("golden:single-agent-write-and-test",),
    2: ("golden:first-test-fails-bounded-repair-succeeds",),
    3: (
        "golden:bad-model-zero-tool-executions",
        "tests.test_agent_loop.AgentLoopTest.test_completed_tool_item_is_not_executed_before_stream_terminal_validation",
        "tests.test_agent_loop.AgentLoopTest.test_unknown_tool_blocks_the_entire_batch_before_first_execution",
    ),
    4: ("golden:os-kill-after-checkpoint-and-resume",),
    5: (
        "tests.test_d10_integration.D10McpIntegrationTest.test_timeout_marks_outcome_unknown",
        "tests.test_d7_tool_ledger.D7ToolLedgerTest.test_queryable_non_idempotent_recovery_applied_not_applied_and_unknown",
    ),
    6: (
        "tests.test_d10_integration.D10McpIntegrationTest.test_ask_grant_resume_executes_once_through_fixture",
        "tests.test_d10_integration.D10McpIntegrationTest.test_refresh_creates_new_binding_and_new_approval",
        "tests.test_d9_approval.D9DurableApprovalTest.test_grant_deny_and_expiry_queue_recovery_in_same_commit",
        "tests.test_d9_approval.D9DurableApprovalTest.test_action_drift_invalidates_grant_and_reasks",
        "tests.test_d9_approval.D9DurableApprovalTest.test_budget_exceeded_leaves_second_execution_prepared",
    ),
    7: (
        "tests.test_d8_docker_integration.D8DockerPrerequisiteTest.test_workspace_mount_is_canonical_and_rejects_link_escape",
        "tests.test_d8_docker_integration.D8DockerPrerequisiteTest.test_workspace_mount_rejects_same_volume_hard_link_escape",
        "tests.test_d8_docker_integration.D8DockerRunnerIntegrationTest.test_real_runner_success_and_container_attack_evidence",
    ),
    8: (
        "tests.test_d10_integration.D10McpIntegrationTest.test_allow_mcp_call_roundtrip_and_ledger_binding",
        "tests.test_d10_integration.D10McpIntegrationTest.test_trace_wired_into_tool_ledger_and_mcp",
        "tests.test_d10_fixture_smoke.FixtureSmokeTest.test_initialize_and_tools_list_pagination",
        "tests.test_d10_fixture_smoke.FixtureSmokeTest.test_echo_call_and_concurrent_ids",
    ),
    9: ("golden:two-writing-agents-independent-worktrees-integrate",),
    10: ("golden:same-line-artifact-conflict",),
    11: (
        "tests.test_d21_agent_security.RepoInjectionEgressTest.test_t1_repo_injection_egress_is_fail_closed",
        "tests.test_d21_agent_security.ToolResultInjectionTest.test_t2_tool_result_injection_denied_and_redacted",
        "tests.test_d21_agent_security.McpPoisoningTest.test_t3b_bound_mcp_write_denied_by_default",
        "tests.test_d21_agent_security.WorkspaceEscapeTest.test_t5_workspace_escape_blocked_and_audited",
    ),
    12: ("golden:late-agent-result-fenced",),
    13: ("golden:restart-oracle-eventstore-ledger-workspace-run",),
}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout


class GoldenCompositeE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "golden@example.com")
        _git(self.repo, "config", "user.name", "golden")
        _git(self.repo, "config", "core.autocrlf", "false")
        # Tracked files give the two writing children real, independently
        # applicable patches (and let the third child create an exact overlap).
        (self.repo / "README.md").write_text("golden\n", encoding="utf-8")
        (self.repo / "alpha.txt").write_text("alpha-base\n", encoding="utf-8")
        (self.repo / "beta.txt").write_text("beta-base\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "base")
        self.db = root / "golden.sqlite3"
        self.ready = root / "ready.txt"
        self.state = root / "state.json"
        self.evidence = root / "evidence.json"

    def _env(self, resume: bool) -> dict:
        environment = os.environ.copy()
        configured = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(ROOT / "src"), str(ROOT), configured) if part
        )
        environment["GOLDEN_DB"] = str(self.db)
        environment["GOLDEN_REPO"] = str(self.repo)
        environment["GOLDEN_RESUME"] = "1" if resume else "0"
        environment["GOLDEN_READY_FILE"] = str(self.ready)
        environment["GOLDEN_STATE_FILE"] = str(self.state)
        environment["GOLDEN_EVIDENCE_FILE"] = str(self.evidence)
        if not resume:
            environment["GOLDEN_KILL_POINT"] = "after_claim"
        return environment

    def _spawn(self, resume: bool) -> subprocess.Popen:
        kwargs = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.Popen(
            [sys.executable, "-B", str(WORKER)],
            cwd=ROOT,
            env=self._env(resume),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **kwargs,
        )

    def test_full_composite_kill_and_resume(self) -> None:
        if not DockerSandboxDoctor().check(IMAGE_ID).ready:
            self.skipTest("docker_doctor_not_ready")

        run = self._spawn(resume=False)
        try:
            deadline = time.monotonic() + 45
            while not self.ready.exists() and time.monotonic() < deadline:
                if run.poll() is not None:
                    stdout, stderr = run.communicate()
                    self.fail(f"run phase exited early:\n{stdout}\n{stderr}")
                time.sleep(0.05)
            if not self.ready.exists():
                if run.poll() is None:
                    run.kill()
                stdout, stderr = run.communicate(timeout=10)
                self.fail(
                    "run phase never reached the kill point; child diagnostics:\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}\n"
                    f"returncode: {run.returncode}"
                )
            run.kill()
            run.communicate(timeout=10)
            self.assertNotEqual(0, run.returncode)
        finally:
            if run.poll() is None:
                run.kill()
                run.communicate(timeout=10)

        resume = self._spawn(resume=True)
        try:
            deadline = time.monotonic() + 45
            while not self.evidence.exists() and time.monotonic() < deadline:
                if resume.poll() is not None:
                    stdout, stderr = resume.communicate()
                    self.fail(f"resume phase exited early:\n{stdout}\n{stderr}")
                time.sleep(0.05)
            if not self.evidence.exists():
                if resume.poll() is None:
                    resume.kill()
                stdout, stderr = resume.communicate(timeout=10)
                self.fail(
                    "resume phase never produced evidence; child diagnostics:\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )
            stdout, stderr = resume.communicate(timeout=15)
            if resume.returncode != 0:
                evidence_summary = {}
                if self.evidence.exists():
                    try:
                        partial = json.loads(self.evidence.read_text(encoding="utf-8"))
                        oracle = partial.get("oracle") or {}
                        artifact_facts = partial.get("artifact_facts") or {}
                        evidence_summary = {
                            "status": partial.get("status"),
                            "turn_error": partial.get("turn_error"),
                            "artifact_facts.completion_error": artifact_facts.get("completion_error"),
                            "oracle.turn_status": oracle.get("turn_status"),
                            "oracle.run_replay": oracle.get("run_replay"),
                        }
                    except (OSError, TypeError, ValueError):
                        evidence_summary = {"evidence_summary": "unreadable"}
                self.fail(
                    f"resume phase exited with {resume.returncode}; child diagnostics:\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}\n"
                    f"evidence_summary: {evidence_summary}"
                )
        finally:
            if resume.poll() is None:
                resume.kill()
                resume.communicate(timeout=10)

        evidence = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertEqual("completed", evidence["status"])
        self.assertEqual("v2", evidence["solution"])
        self.assertEqual("completed", evidence["subagent_state"])
        for stream in ("model", "tool", "ledger", "mcp"):
            self.assertIn(stream, evidence["trace_streams"], stream)
        self.assertIn("tool.execution-succeeded.v1", evidence["ledger_states"])
        self.assertIn("tool.execution-failed.v1", evidence["ledger_states"])
        facts = evidence["artifact_facts"]
        self.assertEqual(["completed", "completed", "completed"], facts["writer_states"])
        self.assertEqual(3, len(set(facts["accepted_artifacts"])))
        self.assertEqual("completed", facts["root_agent_state"])
        self.assertEqual("artifact_conflict", facts["artifact_conflict"])
        self.assertEqual(0, facts["integration_test_exit"])
        self.assertEqual(0, facts["bad_model_tool_calls"])
        self.assertEqual(0, facts["deny_handler_calls"])
        self.assertGreaterEqual(len(facts["approval_facts"]), 2)
        self.assertTrue(all(item["decision"] == "granted" for item in facts["approval_facts"]))
        self.assertEqual("stale_agent_run_fenced", facts["late_result_fence"])
        self.assertNotEqual(facts["late_result_run_id"], facts["late_takeover_run_id"])
        self.assertEqual("alpha-from-agent-a", facts["delivered_files"]["alpha.txt"])
        self.assertEqual("beta-from-agent-b", facts["delivered_files"]["beta.txt"])
        self.assertEqual(["alpha.txt", "beta.txt"], facts["delivered_diff_files"])
        self.assertTrue(all(len(value) == 64 for value in facts["artifact_diff_sha256"].values()))
        self.assertEqual(64, len(facts["delivered_diff_sha256"]))
        oracle = evidence["oracle"]
        self.assertEqual("completed", oracle["turn_status"])
        self.assertGreater(oracle["event_count"], 0)
        self.assertEqual([], oracle["workspace_inventory"])
        self.assertGreaterEqual(oracle["workspace_records"], 1)
        self.assertIn("run.context-seeded.v2", oracle["event_types"])
        self.assertIn("workspace.inventory-state.v2", oracle["event_types"])
        self.assertIn("message.result-recorded.v1", oracle["event_types"])
        self.assertGreater(oracle["execution_event_count"], 0)
        for checkpoint_type in (
            "run.context-seeded.v2", "run.phase-advanced.v1", "tool.result-recorded.v1",
        ):
            self.assertIn(checkpoint_type, oracle["checkpoint_event_types"])
        self.assertGreater(oracle["trace_record_count"], 0)
        self.assertEqual(
            {"model", "tool", "ledger", "mcp"}, set(oracle["trace_streams"])
        )
        self.assertEqual("completed", oracle["run_replay"]["status"])
        self.assertEqual(1, oracle["artifact_event_count"])
        approval_types = [item["event_type"] for item in oracle["approval_events"]]
        self.assertGreaterEqual(approval_types.count("approval.requested.v1"), 2)
        self.assertGreaterEqual(approval_types.count("approval.granted.v1"), 2)
        self.assertIn("approval.expired.v1", approval_types)
        self.assertEqual(facts["approval_facts"], oracle["approval_facts_replay"])
        self.assertEqual(
            facts["delivered_diff_sha256"],
            oracle["artifact_fact_replay"]["delivered_diff_sha256"],
        )
        self.assertEqual(
            facts["delivered_diff_files"],
            oracle["artifact_fact_replay"]["delivered_diff_files"],
        )
        self.assertEqual(evidence["ledger_states"], oracle["ledger_states"])

    def test_required_matrix_is_complete_and_explicit(self) -> None:
        """The mandatory lane must enumerate all 13 cases exactly once."""
        self.assertEqual(set(range(1, 14)), set(REQUIRED_MATRIX))
        self.assertTrue(all(isinstance(value, tuple) and value for value in REQUIRED_MATRIX.values()))

    def test_approval_replay_survives_event_store_reopen(self) -> None:
        """The approval oracle must recover both requests after a restart."""
        from datetime import datetime, timezone
        from uuid import uuid4

        from koawa_agent_v2.control.event_store import (
            EventMetadata, NewEvent, StreamId, StreamWrite,
        )
        from koawa_agent_v2.control.sqlite_store import SqliteEventStore

        old_db = os.environ.get("GOLDEN_DB")
        old_repo = os.environ.get("GOLDEN_REPO")
        os.environ["GOLDEN_DB"] = str(self.db)
        os.environ["GOLDEN_REPO"] = str(self.repo)
        try:
            from tests.fixtures.golden_worker import _approval_facts_for_turn

            store = SqliteEventStore(self.db)
            turn_id, subject_id = uuid4(), uuid4()
            stream = StreamId("approval", subject_id)
            run_a, run_b = uuid4(), uuid4()
            request_a, request_b = uuid4(), uuid4()
            version = -1

            def append(event_type, payload, run_id):
                nonlocal version
                command_id = uuid4()
                event = NewEvent(
                    uuid4(), event_type, 1, datetime.now(timezone.utc), payload,
                    EventMetadata(
                        command_id, turn_id, turn_id=turn_id, run_id=run_id,
                        actor="golden-replay-test",
                    ),
                )
                store.append_batch(
                    (StreamWrite(stream, version, (event,)),),
                    idempotency_key=command_id,
                )
                version += 1

            common = {"turn_id": str(turn_id), "subject_id": str(subject_id)}
            append(
                "approval.requested.v1",
                {**common, "request_id": str(request_a), "action_digest": "a" * 64},
                run_a,
            )
            append(
                "approval.granted.v1",
                {"request_id": str(request_a)}, run_a,
            )
            append(
                "approval.expired.v1",
                {"request_id": str(request_a)}, run_b,
            )
            append(
                "approval.requested.v1",
                {**common, "request_id": str(request_b), "action_digest": "b" * 64},
                run_b,
            )
            append(
                "approval.granted.v1",
                {"request_id": str(request_b)}, run_b,
            )
            store = SqliteEventStore(self.db)
            facts = _approval_facts_for_turn(store, turn_id)
            self.assertEqual(2, len(facts))
            self.assertEqual(["granted", "expired"], [
                item["decision"] for item in facts[0]["resolution_events"]
            ])
            self.assertEqual("granted", facts[1]["decision"])
        finally:
            if old_db is None:
                os.environ.pop("GOLDEN_DB", None)
            else:
                os.environ["GOLDEN_DB"] = old_db
            if old_repo is None:
                os.environ.pop("GOLDEN_REPO", None)
            else:
                os.environ["GOLDEN_REPO"] = old_repo

    def test_matrix_focused_boundaries_are_executed(self) -> None:
        """Run the exact non-composite boundary probes in the golden lane.

        The composite process owns the durable 1/2/3/4/9/10/12/13 facts.  The
        remaining boundary contracts are deliberately loaded as concrete
        unittest IDs here, so a label cannot silently replace an executable
        test.  Docker tests retain their own prerequisite skip; that skip is
        visible in the nested result and is never converted to a pass.
        """
        import unittest as _unittest

        focused_cases = tuple(
            case for number in (3, 5, 6, 7, 8, 11)
            for case in REQUIRED_MATRIX[number]
            if not case.startswith("golden:")
        )
        loader = _unittest.TestLoader()
        suite = _unittest.TestSuite()
        for case in focused_cases:
            loaded = loader.loadTestsFromName(case)
            self.assertGreater(loaded.countTestCases(), 0, case)
            suite.addTests(loaded)
        output = io.StringIO()
        result = _unittest.TextTestRunner(stream=output, verbosity=0).run(suite)
        self.assertEqual(len(focused_cases), result.testsRun)
        self.assertTrue(result.wasSuccessful(), output.getvalue())
        if result.skipped:
            skipped_ids = ", ".join(str(case) for case, _reason in result.skipped)
            self.skipTest(f"nested matrix prerequisite skip: {skipped_ids}")


if __name__ == "__main__":
    unittest.main()
