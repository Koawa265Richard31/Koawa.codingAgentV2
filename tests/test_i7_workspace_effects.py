from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from koawa_agent_v2.control.event_store import (
    EventMetadata,
    NewEvent,
    StreamId,
    StreamWrite,
)
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.workspace.effects import (
    WorkspaceEffectConflict,
    WorkspaceEffectError,
    WorkspaceEffectKind,
    WorkspaceEffectResolvedState,
    WorkspaceEffectResultKind,
    WorkspaceEffectState,
    WorkspaceEffectStore,
    workspace_effect_id,
    workspace_resource_nonce,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


class MovingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 27, 1, 2, 3, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


class WorkspaceEffectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = SqliteEventStore(self.root / "effects.sqlite3")
        self.clock = MovingClock()
        self.store = WorkspaceEffectStore(self.events, clock=self.clock)
        self.semantic_id = UUID("11111111-1111-4111-8111-111111111111")
        self.run_id = UUID("22222222-2222-4222-8222-222222222222")
        self.agent_id = UUID("33333333-3333-4333-8333-333333333333")

    def intend(self, **changes):
        values = {
            "semantic_command_id": self.semantic_id,
            "kind": WorkspaceEffectKind.ARTIFACT_RETEST,
            "repository_identity_digest": SHA_A,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "resource_ref": "integration/run-1",
            "base_digest": SHA_B,
            "input_digest": SHA_C,
            "precondition_digest": SHA_D,
            "expected_postcondition_digest": SHA_E,
        }
        values.update(changes)
        return self.store.intend(**values)

    def claimed(self):
        intended = self.intend().record
        claimed = self.store.claim(
            intended.effect_id,
            expected_version=intended.version,
            owner_id="worker-1",
        ).record
        return intended, claimed

    def unknown(self):
        _, claimed = self.claimed()
        unknown = self.store.record_outcome_unknown(
            claimed.effect_id,
            expected_version=claimed.version,
            claim_epoch=claimed.claim_epoch,
            claim_token=claimed.claim_token,
            uncertainty_code="response_lost",
            evidence_digest=None,
        ).record
        return claimed, unknown

    def assert_code(self, code: str, callback) -> WorkspaceEffectError:
        with self.assertRaises(WorkspaceEffectError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def test_ids_are_exact_uuid5_and_intent_payload_is_digest_only(self) -> None:
        write = self.intend()
        expected = uuid5(
            NAMESPACE_URL,
            "koawa-v2:workspace-effect:artifact_retest:" + str(self.semantic_id),
        )
        self.assertEqual(expected, write.record.effect_id)
        self.assertEqual(uuid5(expected, "resource"), write.record.resource_nonce)
        self.assertEqual(WorkspaceEffectState.INTENDED, write.record.state)
        event = self.events.read_stream(StreamId("workspace-effect", expected))[0]
        self.assertEqual(
            {
                "effect_id", "semantic_command_id", "kind",
                "repository_identity_digest", "agent_id", "run_id",
                "resource_nonce", "resource_ref", "base_digest", "input_digest",
                "precondition_digest", "expected_postcondition_digest", "intended_at",
            },
            set(event.payload),
        )
        encoded = repr(event.payload)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("diff", encoded)

    def test_intent_response_loss_returns_original_receipt_despite_clock_move(self) -> None:
        first = self.intend()
        second = self.intend()
        self.assertEqual(first.receipt, second.receipt)
        self.assertEqual(first.record.last_event_id, second.record.last_event_id)
        self.assertEqual(0, second.record.version)

    def test_same_semantic_command_with_different_identity_conflicts(self) -> None:
        self.intend()
        with self.assertRaises(WorkspaceEffectConflict) as caught:
            self.intend(input_digest=SHA_D)
        self.assertEqual("workspace_effect_idempotency_conflict", caught.exception.code)

    def test_resource_ref_rejects_absolute_escape_and_windows_drive(self) -> None:
        for value in ("/tmp/x", "../x", "a/../../x", r"C:\temp\x", ""):
            with self.subTest(value=value):
                self.assert_code(
                    "workspace_effect_invalid_resource_ref",
                    lambda value=value: self.intend(resource_ref=value),
                )

    def test_claim_has_exact_epoch_token_and_expected_version(self) -> None:
        intended = self.intend().record
        self.assert_code(
            "workspace_effect_version_conflict",
            lambda: self.store.claim(
                intended.effect_id, expected_version=9, owner_id="worker-1"
            ),
        )
        claimed = self.store.claim(
            intended.effect_id, expected_version=0, owner_id="worker-1"
        ).record
        command = uuid5(intended.effect_id, "claim:1")
        self.assertEqual(1, claimed.claim_epoch)
        self.assertEqual(uuid5(command, "claim-token"), claimed.claim_token)
        self.assertEqual(WorkspaceEffectState.CLAIMED, claimed.state)

    def test_claim_response_loss_is_idempotent(self) -> None:
        intended = self.intend().record
        first = self.store.claim(
            intended.effect_id, expected_version=0, owner_id="worker-1"
        )
        second = self.store.claim(
            intended.effect_id, expected_version=0, owner_id="worker-1"
        )
        self.assertEqual(first.receipt, second.receipt)
        self.assertEqual(1, second.record.version)

    def test_applied_success_is_terminal_and_claim_fenced(self) -> None:
        _, claimed = self.claimed()
        self.assert_code(
            "workspace_effect_claim_token_mismatch",
            lambda: self.store.record_applied(
                claimed.effect_id,
                expected_version=claimed.version,
                claim_epoch=claimed.claim_epoch,
                claim_token=uuid4(),
                result_kind=WorkspaceEffectResultKind.SUCCESS,
                result_code="ok",
                exit_code=0,
                postcondition_digest=SHA_E,
                evidence_digest=SHA_A,
            ),
        )
        applied = self.store.record_applied(
            claimed.effect_id,
            expected_version=claimed.version,
            claim_epoch=claimed.claim_epoch,
            claim_token=claimed.claim_token,
            result_kind=WorkspaceEffectResultKind.SUCCESS,
            result_code="ok",
            exit_code=0,
            postcondition_digest=SHA_E,
            evidence_digest=SHA_A,
            result={"kind": "test", "passed": True},
        ).record
        self.assertEqual(WorkspaceEffectState.APPLIED, applied.state)
        self.assertEqual(WorkspaceEffectResultKind.SUCCESS, applied.result_kind)
        self.assertEqual({"kind": "test", "passed": True}, dict(applied.result))
        with self.assertRaises(WorkspaceEffectConflict):
            self.store.claim(
                claimed.effect_id, expected_version=applied.version, owner_id="worker-2"
            )

    def test_known_negative_retest_is_applied_not_failed(self) -> None:
        _, claimed = self.claimed()
        applied = self.store.record_applied(
            claimed.effect_id,
            expected_version=claimed.version,
            claim_epoch=claimed.claim_epoch,
            claim_token=claimed.claim_token,
            result_kind=WorkspaceEffectResultKind.KNOWN_NEGATIVE,
            result_code="tests_failed",
            exit_code=1,
            postcondition_digest=SHA_E,
            evidence_digest=SHA_A,
            result={"test_evidence_ref": "test-evidence:1"},
        ).record
        self.assertEqual(WorkspaceEffectState.APPLIED, applied.state)
        self.assertEqual(WorkspaceEffectResultKind.KNOWN_NEGATIVE, applied.result_kind)
        self.assertEqual(1, applied.exit_code)

    def test_failed_before_effect_requires_matching_epoch(self) -> None:
        _, claimed = self.claimed()
        self.assert_code(
            "workspace_effect_claim_epoch_mismatch",
            lambda: self.store.record_failed_before_effect(
                claimed.effect_id,
                expected_version=claimed.version,
                claim_epoch=2,
                claim_token=claimed.claim_token,
                error_code="precheck_failed",
                evidence_digest=SHA_A,
            ),
        )
        failed = self.store.record_failed_before_effect(
            claimed.effect_id,
            expected_version=claimed.version,
            claim_epoch=claimed.claim_epoch,
            claim_token=claimed.claim_token,
            error_code="precheck_failed",
            evidence_digest=SHA_A,
        ).record
        self.assertEqual(WorkspaceEffectState.FAILED_BEFORE_EFFECT, failed.state)

    def test_unknown_is_not_retriable_but_resolves_with_authoritative_evidence(self) -> None:
        claimed, unknown = self.unknown()
        self.assertEqual(WorkspaceEffectState.OUTCOME_UNKNOWN, unknown.state)
        self.assert_code(
            "workspace_effect_not_claimed",
            lambda: self.store.record_applied(
                unknown.effect_id,
                expected_version=unknown.version,
                claim_epoch=claimed.claim_epoch,
                claim_token=claimed.claim_token,
                result_kind=WorkspaceEffectResultKind.SUCCESS,
                result_code="ok",
                exit_code=0,
                postcondition_digest=SHA_E,
                evidence_digest=SHA_A,
            ),
        )
        resolved = self.store.resolve_unknown(
            unknown.effect_id,
            expected_version=unknown.version,
            claim_epoch=unknown.claim_epoch,
            claim_token=unknown.claim_token,
            unknown_event_id=unknown.unknown_event_id,
            resolved_state=WorkspaceEffectResolvedState.APPLIED,
            result_kind=WorkspaceEffectResultKind.SUCCESS,
            reconciler_principal="recovery-worker",
            evidence_kind="exact_postcondition",
            evidence_digest=SHA_B,
        ).record
        self.assertEqual(WorkspaceEffectState.APPLIED, resolved.state)
        self.assertEqual(unknown.unknown_event_id, resolved.unknown_event_id)
        self.assertIsNotNone(resolved.resolved_by_event_id)

    def test_unknown_can_resolve_failed_before_effect(self) -> None:
        _, unknown = self.unknown()
        resolved = self.store.resolve_unknown(
            unknown.effect_id,
            expected_version=unknown.version,
            claim_epoch=unknown.claim_epoch,
            claim_token=unknown.claim_token,
            unknown_event_id=unknown.unknown_event_id,
            resolved_state=WorkspaceEffectResolvedState.FAILED_BEFORE_EFFECT,
            result_kind=None,
            reconciler_principal="recovery-worker",
            evidence_kind="authoritative_absence",
            evidence_digest=SHA_C,
        ).record
        self.assertEqual(WorkspaceEffectState.FAILED_BEFORE_EFFECT, resolved.state)
        self.assertIsNone(resolved.result_kind)

    def test_resolution_same_evidence_replays_receipt_different_evidence_conflicts(self) -> None:
        _, unknown = self.unknown()
        arguments = dict(
            effect_id=unknown.effect_id,
            expected_version=unknown.version,
            claim_epoch=unknown.claim_epoch,
            claim_token=unknown.claim_token,
            unknown_event_id=unknown.unknown_event_id,
            resolved_state=WorkspaceEffectResolvedState.APPLIED,
            result_kind=WorkspaceEffectResultKind.KNOWN_NEGATIVE,
            reconciler_principal="recovery-worker",
            evidence_kind="exact_postcondition",
            evidence_digest=SHA_A,
        )
        first = self.store.resolve_unknown(**arguments)
        second = self.store.resolve_unknown(**arguments)
        self.assertEqual(first.receipt, second.receipt)
        changed = dict(arguments)
        changed["evidence_digest"] = SHA_B
        with self.assertRaises(WorkspaceEffectConflict):
            self.store.resolve_unknown(**changed)

    def test_resolution_rejects_free_text_and_wrong_unknown_event(self) -> None:
        _, unknown = self.unknown()
        base = dict(
            effect_id=unknown.effect_id,
            expected_version=unknown.version,
            claim_epoch=unknown.claim_epoch,
            claim_token=unknown.claim_token,
            unknown_event_id=unknown.unknown_event_id,
            resolved_state=WorkspaceEffectResolvedState.APPLIED,
            result_kind=WorkspaceEffectResultKind.SUCCESS,
            reconciler_principal="operator",
            evidence_digest=SHA_A,
        )
        self.assert_code(
            "workspace_effect_evidence_not_authoritative",
            lambda: self.store.resolve_unknown(evidence_kind="free_text", **base),
        )
        wrong = dict(base)
        wrong["unknown_event_id"] = uuid4()
        self.assert_code(
            "workspace_effect_unknown_event_mismatch",
            lambda: self.store.resolve_unknown(
                evidence_kind="exact_postcondition", **wrong
            ),
        )

    def test_reducer_rejects_unknown_payload_keys(self) -> None:
        valid = self.intend()
        source = self.events.read_stream(
            StreamId("workspace-effect", valid.record.effect_id)
        )[0]
        second_events = SqliteEventStore(self.root / "forged.sqlite3")
        payload = dict(source.payload)
        payload["absolute_host_path"] = str(self.root)
        command = uuid4()
        forged = NewEvent(
            uuid4(),
            source.event_type,
            source.schema_version,
            source.occurred_at,
            payload,
            EventMetadata(command, valid.record.effect_id, actor="test"),
        )
        second_events.append_batch(
            (
                StreamWrite(
                    StreamId("workspace-effect", valid.record.effect_id),
                    -1,
                    (forged,),
                ),
            ),
            idempotency_key=command,
        )
        forged_store = WorkspaceEffectStore(second_events)
        self.assert_code(
            "workspace_effect_invalid_payload",
            lambda: forged_store.load(valid.record.effect_id),
        )

    def test_bounded_result_rejects_non_json_and_oversize(self) -> None:
        _, claimed = self.claimed()
        common = dict(
            effect_id=claimed.effect_id,
            expected_version=claimed.version,
            claim_epoch=claimed.claim_epoch,
            claim_token=claimed.claim_token,
            result_kind=WorkspaceEffectResultKind.SUCCESS,
            result_code="ok",
            exit_code=0,
            postcondition_digest=SHA_E,
            evidence_digest=SHA_A,
        )
        self.assert_code(
            "workspace_effect_invalid_result",
            lambda: self.store.record_applied(result={"bad": object()}, **common),
        )
        self.assert_code(
            "workspace_effect_result_too_large",
            lambda: self.store.record_applied(result={"large": "x" * 9000}, **common),
        )


class WorkspaceEffectIdentityTests(unittest.TestCase):
    def test_identity_helpers_reject_untyped_values(self) -> None:
        with self.assertRaises(TypeError):
            workspace_effect_id("worktree_add", uuid4())
        with self.assertRaises(TypeError):
            workspace_resource_nonce("not-a-uuid")


if __name__ == "__main__":
    unittest.main()
