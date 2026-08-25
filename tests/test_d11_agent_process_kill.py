"""D11 I2 subprocess kill-window tests (P0-01/P0-02 oracles).

Each window uses a named fault point inside a fixture worker: the worker
writes a durable marker, the parent OS-kills the process, and a fresh process
recovers from the same database. No sleeps are used to guess concurrency;
marker existence is polled as an OS durable fact (same pattern as D6/D7).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.agents.control import (
    AgentBudgetLimits,
    AgentControlPlane,
    Principal,
)
from koawa_agent_v2.agents.graph import AgentError, AgentState, ContextMode
from koawa_agent_v2.agents.messages import (
    MessageKind,
    MessageStatus,
)
from koawa_agent_v2.agents.scheduler import AgentScheduler, ScriptedAgentProvider
from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.control.sqlite_store import SqliteEventStore


ROOT = Path(__file__).resolve().parents[1]
CHILD = ROOT / "tests" / "fixtures" / "d11_fault_worker.py"

POINTS = [
    "d11.enqueue.after_commit",
    "d11.deliver.after_commit",
    "d11.provider.entered",
    "d11.result.after_commit",
    "d11.ack.after_commit",
    "d11.unresolved.after_commit",
    "d11.waiting.after_commit",
    "d11.resume.after_commit",
]


class MutableClock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


def _spawn_worker(directory: Path, point: str) -> tuple[subprocess.Popen, Path, Path, Path]:
    database = directory / "d11.sqlite3"
    marker = directory / "ready.json"
    calls = directory / "calls.json"
    environment = os.environ.copy()
    configured = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "src"), str(ROOT), configured) if part
    )
    kwargs = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(CHILD),
            str(database),
            str(marker),
            str(calls),
            point,
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs,
    )
    return process, database, marker, calls


def _wait_for_marker(process: subprocess.Popen, marker: Path, point: str):
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"child exited before {point}:\n{stdout}\n{stderr}"
            )
        time.sleep(0.05)
    if not marker.exists():
        raise AssertionError(f"child did not reach kill point {point}")


class D11ProcessKillTest(unittest.TestCase):
    def _run_point(self, point: str, marker: Path):
        document = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(point, document["point"])
        agent_id = UUID(document["agent_id"])
        self.assertTrue(agent_id)
        killed_at = datetime.fromisoformat(document["clock"])
        control = AgentControlPlane(
            SqliteEventStore(marker.parent / "d11.sqlite3"),
            limits=AgentBudgetLimits(
                max_depth=3, max_total_agents=8, max_concurrent_children=4
            ),
            clock=MutableClock(killed_at + timedelta(minutes=5)),
        )
        control.discover_orphans()
        return control, agent_id

    def _recover_scheduler(self, control):
        return AgentScheduler(
            control,
            provider=ScriptedAgentProvider({"task": "ok"}),
            lease_seconds=30,
        )

    def _provider_calls(self, calls: Path) -> int:
        return json.loads(calls.read_text(encoding="utf-8"))["provider_calls"]

    def test_enqueue_then_kill_recovers_with_single_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process, database, marker, calls = _spawn_worker(
                Path(directory), "d11.enqueue.after_commit"
            )
            try:
                _wait_for_marker(process, marker, "d11.enqueue.after_commit")
                process.kill()
                process.communicate(timeout=5)
                self.assertNotEqual(0, process.returncode)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            control, agent_id = self._run_point(
                "d11.enqueue.after_commit", marker
            )
            messages = control.mailbox.load(agent_id)
            self.assertEqual(MessageStatus.QUEUED, messages[0].status)
            result = self._recover_scheduler(control).run_attempt(agent_id)
            self.assertEqual(AgentState.COMPLETED, result.state)
            messages = control.mailbox.load(agent_id)
            self.assertEqual(MessageStatus.ACKED, messages[0].status)
            self.assertIsNotNone(messages[0].result_digest)
            self.assertEqual(0, self._provider_calls(calls))

    def test_delivered_unacked_after_kill_turns_unresolved_not_completed(self) -> None:
        """P0-01 oracle: no completed agent with an unacked delivery."""
        with tempfile.TemporaryDirectory() as directory:
            process, database, marker, calls = _spawn_worker(
                Path(directory), "d11.deliver.after_commit"
            )
            try:
                _wait_for_marker(process, marker, "d11.deliver.after_commit")
                process.kill()
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            control, agent_id = self._run_point(
                "d11.deliver.after_commit", marker
            )
            agent = control.graph.load(agent_id)
            self.assertEqual(AgentState.ORPHANED, agent.state)
            result = self._recover_scheduler(control).run_attempt(agent_id)
            self.assertEqual(AgentState.WAITING, result.state)
            messages = control.mailbox.load(agent_id)
            self.assertEqual(MessageStatus.UNRESOLVED, messages[0].status)
            self.assertEqual(0, self._provider_calls(calls))
            final = control.graph.load(agent_id)
            self.assertEqual(AgentState.WAITING, final.state)

    def test_provider_kill_turns_unresolved_without_auto_provider(self) -> None:
        """P0-02: killed after provider entered, before result: never recall."""
        with tempfile.TemporaryDirectory() as directory:
            process, database, marker, calls = _spawn_worker(
                Path(directory), "d11.provider.entered"
            )
            try:
                _wait_for_marker(process, marker, "d11.provider.entered")
                process.kill()
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            control, agent_id = self._run_point(
                "d11.provider.entered", marker
            )
            result = self._recover_scheduler(control).run_attempt(agent_id)
            self.assertEqual(AgentState.WAITING, result.state)
            messages = control.mailbox.load(agent_id)
            self.assertEqual(MessageStatus.UNRESOLVED, messages[0].status)
            self.assertEqual(0, self._provider_calls(calls))
            self.assertEqual(
                AgentState.WAITING,
                control.graph.load(agent_id).state,
            )

    def test_result_recorded_then_kill_recovers_ack_only_provider_count_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process, database, marker, calls = _spawn_worker(
                Path(directory), "d11.result.after_commit"
            )
            try:
                _wait_for_marker(process, marker, "d11.result.after_commit")
                process.kill()
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            control, agent_id = self._run_point(
                "d11.result.after_commit", marker
            )
            recorder = ScriptedAgentProvider({"task": "ok"})
            result = AgentScheduler(control, provider=recorder).run_attempt(agent_id)
            self.assertEqual(AgentState.COMPLETED, result.state)
            self.assertEqual([], recorder.calls)
            messages = control.mailbox.load(agent_id)
            self.assertEqual(MessageStatus.ACKED, messages[0].status)
            self.assertIsNotNone(messages[0].result_ref)
            self.assertEqual(1, self._provider_calls(calls))

    def test_ack_committed_then_kill_terminates_without_recall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process, database, marker, calls = _spawn_worker(
                Path(directory), "d11.ack.after_commit"
            )
            try:
                _wait_for_marker(process, marker, "d11.ack.after_commit")
                process.kill()
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            control, agent_id = self._run_point(
                "d11.ack.after_commit", marker
            )
            recorder = ScriptedAgentProvider({"task": "ok"})
            result = AgentScheduler(control, provider=recorder).run_attempt(agent_id)
            self.assertEqual(AgentState.COMPLETED, result.state)
            self.assertEqual([], recorder.calls)
            messages = control.mailbox.load(agent_id)
            self.assertEqual(MessageStatus.ACKED, messages[0].status)
            self.assertEqual(1, self._provider_calls(calls))

    def test_takeover_batch_committed_atomically_no_half_batch(self) -> None:
        """kill right after the takeover batch commit: exactly one batch."""
        with tempfile.TemporaryDirectory() as directory:
            process, database, marker, calls = _spawn_worker(
                Path(directory), "d11.unresolved.after_commit"
            )
            try:
                _wait_for_marker(process, marker, "d11.unresolved.after_commit")
                process.kill()
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            control, agent_id = self._run_point(
                "d11.unresolved.after_commit", marker
            )
            agent_events = control.event_store.read_stream(
                StreamId("agent", agent_id)
            )
            taken_over = [
                event for event in agent_events
                if event.event_type == "agent.taken-over.v2"
            ]
            self.assertEqual(1, len(taken_over))
            mailbox_events = control.event_store.read_stream(
                StreamId("mailbox", agent_id)
            )
            unresolved = [
                event for event in mailbox_events
                if event.event_type == "message.unresolved.v1"
            ]
            self.assertEqual(1, len(unresolved))
            self.assertEqual(taken_over[0].commit_id, unresolved[0].commit_id)
            result = self._recover_scheduler(control).run_attempt(agent_id)
            self.assertEqual(AgentState.WAITING, result.state)
            messages = control.mailbox.load(agent_id)
            self.assertEqual(MessageStatus.UNRESOLVED, messages[0].status)

    def test_waiting_committed_then_kill_requires_operator_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process, database, marker, calls = _spawn_worker(
                Path(directory), "d11.waiting.after_commit"
            )
            try:
                _wait_for_marker(process, marker, "d11.waiting.after_commit")
                process.kill()
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            control, agent_id = self._run_point(
                "d11.waiting.after_commit", marker
            )
            agent = control.graph.load(agent_id)
            self.assertEqual(AgentState.WAITING, agent.state)
            # blockers still UNRESOLVED: nothing auto-resumes, nothing guessed
            scheduler = self._recover_scheduler(control)
            with self.assertRaises(AgentError) as raised:
                scheduler.run_attempt(agent_id)
            self.assertEqual(
                "message_outcome_unresolved", raised.exception.code
            )
            self.assertEqual(
                AgentState.WAITING,
                control.graph.load(agent_id).state,
            )
            blocker = agent.blocking_message_ids[0]
            control.requeue_message(
                agent_id,
                blocker,
                expected_delivery_attempt=1,
                decision_id=uuid4(),
                actor=Principal("worker", ("agents.resolve",)),
                approval_id=None,
                resolution_kind="proven_not_started",
                reason="test",
            )
            resumed = schedule_replay(control, agent_id)
            self.assertEqual(AgentState.COMPLETED, resumed.state)

    def test_resume_committed_then_kill_continues_fresh_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process, database, marker, calls = _spawn_worker(
                Path(directory), "d11.resume.after_commit"
            )
            try:
                _wait_for_marker(process, marker, "d11.resume.after_commit")
                process.kill()
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            control, agent_id = self._run_point(
                "d11.resume.after_commit", marker
            )
            recorder = ScriptedAgentProvider({"task": "ok"})
            result = AgentScheduler(control, provider=recorder).run_attempt(agent_id)
            self.assertEqual(AgentState.COMPLETED, result.state)
            self.assertEqual(1, len(recorder.calls))
            messages = control.mailbox.load(agent_id)
            self.assertEqual(MessageStatus.ACKED, messages[0].status)
            agent_events = control.event_store.read_stream(
                StreamId("agent", agent_id)
            )
            resumes = [
                event for event in agent_events
                if event.event_type == "agent.resumed.v1"
            ]
            self.assertEqual(1, len(resumes))


def schedule_replay(control, agent_id):
    recorder = ScriptedAgentProvider({"task": "ok"})
    return AgentScheduler(control, provider=recorder).run_attempt(agent_id)


if __name__ == "__main__":
    unittest.main()
