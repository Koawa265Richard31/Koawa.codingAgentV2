from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.control.models import TurnStatus
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.telemetry.faults import (
    FAULT_REGISTRY,
    FAULT_SPECS,
    FaultPointClass,
    InjectedFault,
    NoOpFaultPort,
    RecordingFaultPort,
)
from koawa_agent_v2.workspace.effects import (
    WorkspaceEffectKind,
    WorkspaceEffectResultKind,
    WorkspaceEffectStore,
)
from tests.fixtures.stability_fault_worker import TERMINAL_POINTS
from tests.fixtures.stability_activation_faults import ACTIVATION_POINTS
from tests.fixtures.stability_artifact_faults import ARTIFACT_POINTS
from tests.fixtures.stability_allocation_faults import ALLOCATION_POINTS
from tests.fixtures.stability_s3_faults import (
    CANARY, CHECKPOINT_POINTS, EXPORT_POINTS, MIGRATION_POINTS, S3_POINTS,
)
from tests.fixtures.legacy_builder import build_legacy_database
from tests.fixtures.stability_trace_faults import TRACE_POINTS, RETRY_LIMIT
from tests.fixtures.stability_worktree_faults import WORKTREE_POINTS, manager_at


class I8FaultRegistryTest(unittest.TestCase):
    def test_commit_windows_have_explicit_commit_class(self) -> None:
        for name in ("s3.checkpoint.after_cache_commit", "s5.run.after_terminal_commit"):
            with self.subTest(name=name):
                self.assertEqual(FaultPointClass.AFTER_COMMIT, FAULT_SPECS[name].point_class)

    def test_registry_is_unique_machine_readable_and_has_production_sites(self) -> None:
        names = tuple(spec.name for spec in FAULT_REGISTRY)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), set(FAULT_SPECS))
        for spec in FAULT_REGISTRY:
            self.assertEqual(spec.point_class.value, spec.marker_timing)
            self.assertIn("durable_events", spec.expected_event_delta.counters)
            self.assertGreaterEqual(
                spec.expected_event_delta.counters["durable_events"], 0,
            )
            self.assertEqual(
                len(spec.expected_event_delta.stream_categories),
                len(set(spec.expected_event_delta.stream_categories)),
            )
        takeover = FAULT_SPECS["d11.takeover.after_commit"].expected_event_delta
        self.assertEqual(1, takeover.counters["durable_events"])
        self.assertEqual(1, takeover.per_fact["message_count"])
        terminal = FAULT_SPECS["d11.terminal.after_commit"].expected_event_delta
        self.assertEqual(4, terminal.variants["child_parent_active_durable_events"]["durable_events"])
        catalog = FAULT_SPECS["s4.mcp.list.after_catalog_commit"].expected_event_delta
        self.assertEqual(0, catalog.counters["durable_events"])
        self.assertEqual(1, catalog.counters["catalog_snapshots"])
        root = Path(__file__).parents[1] / "src" / "koawa_agent_v2"
        from scripts.stability_fault_audit import audit_sites
        audit = audit_sites(root)
        self.assertEqual([], audit["missing"])
        self.assertEqual([], audit["unknown"])
        self.assertEqual([], audit["unknown_fact_keys"])
        self.assertEqual([], audit["legacy_literals"])
        self.assertEqual(set(names), set(audit["declared"]))

    def test_unknown_point_and_untrusted_facts_fail_closed(self) -> None:
        port = NoOpFaultPort()
        with self.assertRaises(AgentError) as unknown:
            port.hit("unknown.point", {})
        self.assertEqual("unknown_failure_point", unknown.exception.code)
        with self.assertRaises(AgentError) as invalid:
            port.hit("s5.trace.drop", {"raw": ["not", "primitive"]})
        self.assertEqual("invalid_fault_facts", invalid.exception.code)
        with self.assertRaises(AgentError):
            port.hit("s5.trace.drop", {"user_text": "x" * 513})


class I8FaultBehaviorTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = SqliteEventStore(self.root / "faults.sqlite3")

    def test_after_terminal_commit_is_response_loss_and_retry_is_exact(self) -> None:
        port = RecordingFaultPort(
            raise_at=frozenset({"s5.run.after_terminal_commit"})
        )
        runtime = ThreadRuntime(self.events, fault_port=port)
        thread = runtime.create_thread("D:/repo")
        turn = runtime.create_turn(
            thread.thread_id, "finish", expected_thread_version=thread.version
        )
        running = runtime.start_turn(turn.turn_id, expected_version=turn.version)
        command_id = uuid4()
        with self.assertRaises(InjectedFault):
            runtime.complete_turn(
                running.turn_id,
                "done",
                expected_version=running.version,
                run_id=running.current_run_id,
                command_id=command_id,
            )
        self.assertEqual(TurnStatus.COMPLETED, runtime.get_turn(turn.turn_id).status)
        count = len(self.events.read_all())
        replay = runtime.complete_turn(
            running.turn_id,
            "done",
            expected_version=running.version,
            run_id=running.current_run_id,
            command_id=command_id,
        )
        self.assertEqual(TurnStatus.COMPLETED, replay.status)
        self.assertEqual(count, len(self.events.read_all()))

    def test_workspace_effect_points_are_on_real_transitions(self) -> None:
        port = RecordingFaultPort()
        effects = WorkspaceEffectStore(self.events, fault_port=port)
        expected = set()
        operation = {
            WorkspaceEffectKind.WORKTREE_ADD: "add",
            WorkspaceEffectKind.WORKTREE_REMOVE: "remove",
            WorkspaceEffectKind.ARTIFACT_APPLY: "apply",
            WorkspaceEffectKind.ARTIFACT_RETEST: "retest",
            WorkspaceEffectKind.ARTIFACT_DELIVER: "deliver",
        }
        for kind, short in operation.items():
            intended = effects.intend(
                semantic_command_id=uuid4(),
                kind=kind,
                repository_identity_digest="a" * 64,
                agent_id=None,
                run_id=uuid4(),
                resource_ref=f"resource/{short}",
                base_digest="b" * 64,
                input_digest="c" * 64,
                precondition_digest="d" * 64,
                expected_postcondition_digest="e" * 64,
            ).record
            claimed = effects.claim(
                intended.effect_id,
                expected_version=intended.version,
                owner_id="fault-test",
            ).record
            effects.record_applied(
                claimed.effect_id,
                expected_version=claimed.version,
                claim_epoch=claimed.claim_epoch,
                claim_token=claimed.claim_token,
                result_kind=WorkspaceEffectResultKind.SUCCESS,
                result_code="ok",
                exit_code=0,
                postcondition_digest="e" * 64,
                evidence_digest="f" * 64,
            )
            expected.update({
                f"s5.workspace.{short}.after_intent_commit",
                f"s5.workspace.{short}.after_claim_commit",
                f"s5.workspace.{short}.after_effect_before_ack",
            })
        self.assertEqual(expected, {name for name, _ in port.hits})


class I8ProcessKillTest(unittest.TestCase):
    def test_allocation_and_external_process_windows_reconcile_exact_identity(self):
        repo = Path(__file__).resolve().parents[1]
        worker = repo / "tests/fixtures/stability_fault_worker.py"
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "src")))}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for point in ALLOCATION_POINTS:
            sequences = []
            for repetition in range(2):
                with self.subTest(point=point, repetition=repetition), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    process = subprocess.Popen(
                        [sys.executable, str(worker), "crash", str(root), point],
                        cwd=repo, env=environment, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, creationflags=flags,
                    )
                    child_pid = None
                    try:
                        deadline = time.monotonic() + 30
                        while not (root / "ready.json").exists():
                            if process.poll() is not None:
                                output, error = process.communicate()
                                self.fail(f"allocation worker exited before {point}: {output}\n{error}")
                            if time.monotonic() >= deadline:
                                self.fail(f"allocation worker failed to reach {point}")
                            time.sleep(.02)
                        marker = json.loads((root / "ready.json").read_text())
                        if marker.get("child"):
                            child_pid = marker["child"]["pid"]
                        self.assertEqual(FAULT_SPECS[point].point_class.value, marker["point_class"])
                        process.kill()
                        process.communicate(timeout=10)
                        recovered = subprocess.run(
                            [sys.executable, str(worker), "recover", str(root)],
                            cwd=repo, env=environment, capture_output=True, text=True,
                            timeout=30, creationflags=flags,
                        )
                        self.assertEqual(0, recovered.returncode, recovered.stdout + recovered.stderr)
                        result = json.loads((root / "recovered.json").read_text())
                        self.assertFalse(result["child_alive"])
                        self.assertIn(result["status"], ("stopped", "failed_before_start"))
                        sequences.append(result["normalized_events"])
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate(timeout=10)
                        if child_pid is not None:
                            try:
                                os.kill(child_pid, 15)
                            except OSError:
                                pass
            self.assertEqual(sequences[0], sequences[1])

    def test_artifact_apply_retest_deliver_kill_windows_fail_closed(self):
        repo = Path(__file__).resolve().parents[1]
        worker = repo / "tests/fixtures/stability_fault_worker.py"
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "src")))}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for point in ARTIFACT_POINTS:
            sequences = []
            for repetition in range(2):
                with self.subTest(point=point, repetition=repetition), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    process = subprocess.Popen(
                        [sys.executable, str(worker), "crash", str(root), point],
                        cwd=repo, env=environment, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, creationflags=flags,
                    )
                    try:
                        deadline = time.monotonic() + 40
                        while not (root / "ready.json").exists():
                            if process.poll() is not None:
                                output, error = process.communicate()
                                self.fail(f"artifact worker exited before {point}: {output}\n{error}")
                            if time.monotonic() >= deadline:
                                self.fail(f"artifact worker failed to reach {point}")
                            time.sleep(.02)
                        marker = json.loads((root / "ready.json").read_text())
                        self.assertEqual(point, marker["point"])
                        self.assertEqual(FAULT_SPECS[point].point_class.value, marker["point_class"])
                        if point.endswith("after_effect_before_ack"):
                            self.assertTrue(marker["external_marker"])
                        process.kill()
                        process.communicate(timeout=10)
                        self.assertNotEqual(0, process.returncode)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate(timeout=10)
                    recovered = subprocess.run(
                        [sys.executable, str(worker), "recover", str(root)],
                        cwd=repo, env=environment, capture_output=True, text=True,
                        timeout=40, creationflags=flags,
                    )
                    self.assertEqual(0, recovered.returncode, recovered.stdout + recovered.stderr)
                    result = json.loads((root / "recovered.json").read_text())
                    expected = "applied" if point.endswith("after_intent_commit") else "outcome_unknown"
                    self.assertEqual(expected, result["state"])
                    sequences.append(result["normalized_events"])
            self.assertEqual(sequences[0], sequences[1])

    def test_activation_request_and_grant_kill_replay_without_launch_or_renewal(self):
        repo = Path(__file__).resolve().parents[1]
        worker = repo / "tests/fixtures/stability_fault_worker.py"
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "src")))}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for point in ACTIVATION_POINTS:
            for variant in (("operator",) if point == ACTIVATION_POINTS[0] else ("operator", "policy")):
                sequences = []
                for repetition in range(2):
                    with self.subTest(point=point, variant=variant, repetition=repetition), tempfile.TemporaryDirectory() as raw:
                        root = Path(raw)
                        process = subprocess.Popen(
                            [sys.executable, str(worker), "crash", str(root), point, variant],
                            cwd=repo, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, creationflags=flags,
                        )
                        try:
                            deadline = time.monotonic() + 30
                            while not (root / "ready.json").exists():
                                if process.poll() is not None:
                                    output, error = process.communicate()
                                    self.fail(f"activation worker exited before {point}: {output}\n{error}")
                                if time.monotonic() >= deadline:
                                    self.fail(f"activation worker missed {point}")
                                time.sleep(.02)  # Wait for durable marker, not elapsed crash timing.
                            marker = json.loads((root / "ready.json").read_text())
                            self.assertEqual(point, marker["point"])
                            self.assertEqual(variant, marker["variant"])
                            self.assertEqual(FAULT_SPECS[point].point_class.value, marker["point_class"])
                            self.assertEqual(process.pid, marker["crash_pid"])
                            process.kill()
                            process.communicate(timeout=10)
                            self.assertNotEqual(0, process.returncode)
                        finally:
                            if process.poll() is None:
                                process.kill()
                                process.communicate(timeout=10)
                        recovered = subprocess.run(
                            [sys.executable, str(worker), "recover", str(root)], cwd=repo, env=environment,
                            capture_output=True, text=True, timeout=30, creationflags=flags,
                        )
                        self.assertEqual(0, recovered.returncode, recovered.stdout + recovered.stderr)
                        result = json.loads((root / "recovered.json").read_text())
                        self.assertNotIn(result["recovery_pid"], (os.getpid(), marker["crash_pid"]))
                        sequences.append(result["normalized_events"])
                self.assertEqual(2, len(sequences))
                self.assertEqual(sequences[0], sequences[1], "same activation seed recovered to different facts")

    def test_real_git_worktree_windows_recover_without_replaying_uncertain_effects(self):
        repo = Path(__file__).resolve().parents[1]
        worker = repo / "tests/fixtures/stability_fault_worker.py"
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "src")))}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for point in WORKTREE_POINTS:
            sequences = []
            for repetition in range(2):
                with self.subTest(point=point, repetition=repetition), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    process = subprocess.Popen(
                        [sys.executable, str(worker), "crash", str(root), point], cwd=repo, env=environment,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags,
                    )
                    try:
                        deadline = time.monotonic() + 40
                        while not (root / "ready.json").exists():
                            if process.poll() is not None:
                                output, error = process.communicate()
                                self.fail(f"worker exited before {point}: {output}\n{error}")
                            if time.monotonic() >= deadline:
                                self.fail(f"worker failed to reach {point}")
                            time.sleep(.02)
                        marker = json.loads((root / "ready.json").read_text())
                        request = json.loads((root / "request.json").read_text())
                        self.assertEqual(point, marker["point"])
                        self.assertEqual(process.pid, marker["crash_pid"])
                        manager = manager_at(root)
                        from uuid import UUID
                        from scripts.stability_scenarios import event_digest
                        before = event_digest(manager.store.event_store)
                        with self.assertRaises(AgentError) as blocked:
                            manager.reconcile(UUID(request["effect_id"]), expected_version=marker["effect_version"],
                                              base_commit=request["base_commit"])
                        self.assertEqual("workspace_operation_locked", blocked.exception.code)
                        self.assertEqual(before, event_digest(manager.store.event_store))
                        process.kill()
                        process.communicate(timeout=10)
                        self.assertNotEqual(0, process.returncode)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate(timeout=10)
                    completed = subprocess.run(
                        [sys.executable, str(worker), "recover", str(root)], cwd=repo, env=environment,
                        capture_output=True, text=True, timeout=40, creationflags=flags,
                    )
                    self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                    result = json.loads((root / "recovered.json").read_text())
                    self.assertNotIn(result["recovery_pid"], (os.getpid(), marker["crash_pid"]))
                    sequences.append(result["normalized_events"])
            self.assertEqual(2, len(sequences))
            self.assertEqual(sequences[0], sequences[1], "same worktree window recovered to different facts")

    def test_trace_conflict_and_drop_kill_preserve_committed_tool_result(self):
        repo = Path(__file__).resolve().parents[1]
        worker = repo / "tests/fixtures/stability_fault_worker.py"
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "src")))}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        sequences = []
        for point in TRACE_POINTS:
            for repetition in range(2):
                with self.subTest(point=point, repetition=repetition), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    process = subprocess.Popen(
                        [sys.executable, str(worker), "crash", str(root), point],
                        cwd=repo, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, creationflags=flags,
                    )
                    try:
                        deadline = time.monotonic() + 30
                        while not (root / "ready.json").exists():
                            if process.poll() is not None:
                                output, error = process.communicate()
                                self.fail(f"worker exited before {point}: {output}\n{error}")
                            if time.monotonic() >= deadline:
                                self.fail(f"worker failed to reach {point}")
                            time.sleep(.02)
                        marker = json.loads((root / "ready.json").read_text())
                        self.assertEqual(point, marker["point"])
                        self.assertEqual(process.pid, marker["crash_pid"])
                        expected = 1 if point == TRACE_POINTS[0] else RETRY_LIMIT
                        self.assertEqual(expected, marker["attempts"])
                        process.kill()
                        process.communicate(timeout=10)
                        self.assertNotEqual(0, process.returncode)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate(timeout=10)
                    completed = subprocess.run(
                        [sys.executable, str(worker), "recover", str(root)],
                        cwd=repo, env=environment, capture_output=True, text=True,
                        timeout=30, creationflags=flags,
                    )
                    self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                    result = json.loads((root / "recovered.json").read_text())
                    self.assertNotIn(result["recovery_pid"], (os.getpid(), marker["crash_pid"]))
                    self.assertEqual("completed", result["final_status"])
                    self.assertEqual(1, result["external_effects"])
                    self.assertEqual(1, result["claim_epoch"])
                    sequences.append(result["normalized_business"])
        self.assertTrue(all(sequence == sequences[0] for sequence in sequences),
                        "trace faults changed normalized business facts")

    def test_s3_migration_export_and_checkpoint_kill_restart_twice(self):
        repo = Path(__file__).resolve().parents[1]
        worker = repo / "tests/fixtures/stability_fault_worker.py"
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "src")))}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            legacy_seed = base / "legacy-seed.db"
            build_legacy_database(legacy_seed, include_active_turn=True)
            keeper = sqlite3.connect(legacy_seed, isolation_level=None)
            self.assertEqual("wal", keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0])
            keeper.execute("PRAGMA wal_autocheckpoint=0")
            keeper.execute(
                "UPDATE events SET payload_json=? WHERE event_type='turn.completed.v1'",
                (json.dumps({"summary": "wal-export-result " + CANARY}),),
            )
            legacy_media = {
                suffix: Path(str(legacy_seed) + suffix).read_bytes()
                for suffix in ("", "-wal", "-shm")
            }
            keeper.close()
            normalized = {}
            for point in S3_POINTS:
                for repetition in range(2):
                    with self.subTest(point=point, repetition=repetition):
                        root = base / f"{point}-{repetition}"
                        root.mkdir()
                        if point in EXPORT_POINTS:
                            for suffix, contents in legacy_media.items():
                                Path(str(root / "legacy.db") + suffix).write_bytes(contents)
                        process = subprocess.Popen(
                            [sys.executable, str(worker), "crash", str(root), point],
                            cwd=repo, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, creationflags=flags,
                        )
                        try:
                            deadline = time.monotonic() + 30
                            while not (root / "ready.json").exists():
                                if process.poll() is not None:
                                    output, error = process.communicate()
                                    self.fail(f"worker exited before {point}: {output}\n{error}")
                                if time.monotonic() >= deadline:
                                    self.fail(f"worker failed to reach {point}")
                                # Poll a durable marker, never guess the crash timing.
                                time.sleep(.02)
                            marker = json.loads((root / "ready.json").read_text())
                            self.assertEqual(point, marker["point"])
                            self.assertEqual(process.pid, marker["crash_pid"])
                            self.assertEqual(FAULT_SPECS[point].point_class.value, marker["point_class"])
                            process.kill()
                            process.communicate(timeout=10)
                            self.assertNotEqual(0, process.returncode)
                        finally:
                            if process.poll() is None:
                                process.kill()
                                process.communicate(timeout=10)
                        recovered = subprocess.run(
                            [sys.executable, str(worker), "recover", str(root)],
                            cwd=repo, env=environment, capture_output=True, text=True, timeout=30,
                            creationflags=flags,
                        )
                        self.assertEqual(0, recovered.returncode, recovered.stdout + recovered.stderr)
                        result = json.loads((root / "recovered.json").read_text())
                        self.assertNotIn(result["recovery_pid"], (os.getpid(), marker["crash_pid"]))
                        state = json.dumps(result["normalized_state"], sort_keys=True)
                        normalized.setdefault(point, []).append(state)
            self.assertEqual(set(S3_POINTS), set(normalized))
            for group in (MIGRATION_POINTS, EXPORT_POINTS, CHECKPOINT_POINTS):
                self.assertEqual(1, len({state for point in group for state in normalized[point]}),
                                 "same seed has divergent recovery facts")

    def test_terminal_atomicity_and_response_loss_survive_os_kill_and_fresh_process(self):
        repo = Path(__file__).resolve().parents[1]
        worker = repo / "tests/fixtures/stability_fault_worker.py"
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "src")))}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        normalized = []
        for point in TERMINAL_POINTS:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                process = subprocess.Popen(
                    [sys.executable, str(worker), "crash", str(root), point],
                    cwd=repo, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, creationflags=flags,
                )
                try:
                    deadline = time.monotonic() + 30
                    while not (root / "ready.json").exists():
                        if process.poll() is not None:
                            output, error = process.communicate()
                            self.fail(f"worker exited before {point}: {output}\n{error}")
                        if time.monotonic() >= deadline:
                            self.fail(f"worker failed to reach {point}")
                        time.sleep(.02)
                    marker = json.loads((root / "ready.json").read_text())
                    self.assertEqual(point, marker["point"])
                    process.kill()
                    process.communicate(timeout=10)
                    self.assertNotEqual(0, process.returncode)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=10)
                recovered = subprocess.run(
                    [sys.executable, str(worker), "recover", str(root)],
                    cwd=repo, env=environment, capture_output=True, text=True, timeout=30,
                    creationflags=flags,
                )
                self.assertEqual(0, recovered.returncode, recovered.stdout + recovered.stderr)
                result = json.loads((root / "recovered.json").read_text())
                self.assertEqual("completed", result["final_status"])
                self.assertNotEqual(os.getpid(), result["recovery_pid"])
                self.assertEqual(marker["baseline_count"] + 3, result["final_count"])
                normalized.append(result["normalized_event_digest"])
        self.assertEqual(1, len(set(normalized)), "same script recovered to different facts")


if __name__ == "__main__":
    unittest.main()
