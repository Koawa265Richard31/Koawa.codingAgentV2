"""D12-I7-001 regression: reusing a persisted receipt must replay the
RECORDED result. The old reuse branch returned exit_code 0 / "success" even
when the receipt recorded a known_negative failure (the caller-facing result
contradicted the durable receipt; deliver() still refused).
"""
from __future__ import annotations

import sys
from uuid import uuid4

from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.workspace.container import InjectedContainerRunner
from koawa_agent_v2.workspace.integration import DurableArtifactIntegrator
from tests.test_i7_durable_integration import I7DurableIntegrationTest


class ReceiptReuseReportsRecordedResultTest(I7DurableIntegrationTest):
    def test_failed_receipt_reuse_reports_failure_not_success(self) -> None:
        argv = [sys.executable, "-c", "raise SystemExit(3)"]
        first = self.integrator.integrate(
            [self.artifact], test_argv=argv, command_id=uuid4()
        )
        self.assertEqual("known_negative", first.test_result_kind)
        self.assertEqual(3, first.test_exit_code)

        # Same instance: the reuse branch must replay the recorded result.
        second = self.integrator.integrate(
            [self.artifact], test_argv=argv, command_id=uuid4()
        )
        self.assertEqual("known_negative", second.test_result_kind)
        self.assertEqual(3, second.test_exit_code)

        # Rebuilt integrator over the same durable store: identical.
        rebuilt = DurableArtifactIntegrator(
            event_store=self.events, package_store=self.packages,
            effect_store=self.effects, repo_root=self.repo,
            integration_root=self.root / "integration",
            runner=InjectedContainerRunner(),
        )
        third = rebuilt.integrate(
            [self.artifact], test_argv=argv, command_id=uuid4()
        )
        self.assertEqual("known_negative", third.test_result_kind)
        self.assertEqual(3, third.test_exit_code)

        # No re-execution: still exactly one persisted receipt.
        page = self.events.read_stream(
            __import__(
                "koawa_agent_v2.control.event_store", fromlist=["StreamId"]
            ).StreamId("workspace-integration", first.receipt.receipt_id),
        )
        self.assertEqual(1, len(page))

        # deliver() keeps refusing the failed result after reuse.
        with self.assertRaises(AgentError) as raised:
            rebuilt.deliver(second.receipt, command_id=uuid4())
        self.assertEqual(
            "integration_known_negative_not_deliverable", raised.exception.code
        )

    def test_success_receipt_reuse_still_reports_success(self) -> None:
        argv = [sys.executable, "-c", "raise SystemExit(0)"]
        first = self.integrator.integrate(
            [self.artifact], test_argv=argv, command_id=uuid4()
        )
        self.assertEqual("success", first.test_result_kind)
        second = self.integrator.integrate(
            [self.artifact], test_argv=argv, command_id=uuid4()
        )
        self.assertEqual(0, second.test_exit_code)
        self.assertEqual("success", second.test_result_kind)
        # Delivery of a reused success receipt succeeds (returns normally).
        self.integrator.deliver(second.receipt, command_id=uuid4())


if __name__ == "__main__":
    unittest.main()
