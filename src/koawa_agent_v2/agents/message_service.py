"""邮箱消息命令的业务实现(从 AgentControlPlane 抽出)。

本组件承载七个消息方法(send_message / deliver_message / record_message_result /
ack_message / mark_message_unresolved / requeue_message / cancel_message)的持久化
写路径:事件类型、载荷、事件身份、幂等键、错误码、expected version、run/claim
围栏与单次 append_batch 原子写集合,与抽离前逐字节一致;故障注入点顺序不变。
依赖由 AgentControlPlane 显式构造注入(事件库、图/邮箱投影、时钟读取、故障
回调),不继承控制面,也不持有控制面对象。

共享辅助(事件构造、指纹、幂等回执读取)单点实现在 agents/events.py,此处仅
导入使用,不做第二份实现。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from .events import (
    MAX_CAS_RETRIES,
    FaultInjector,
    _correlation,
    _event,
    _facts,
    _fingerprint,
    _read_all,
    _read_receipt,
)
from .graph import AgentError, AgentGraph, AgentRecord, AgentState
from .messages import (
    AgentMailbox,
    MailboxSnapshot,
    MessageKind,
    MessageRecord,
    MessageStatus,
    canonicalize_result,
    result_digest_for,
    result_ref_for,
    summarize_outcome,
)
from ..control.event_store import (
    IdempotencyConflict,
    StreamId,
    StreamPrecondition,
    StreamWrite,
    WrongExpectedVersion,
)
from ..telemetry.faults import FaultPoint


@dataclass(frozen=True, slots=True)
class Principal:
    """Operator identity used by requeue/cancel decisions."""

    principal_id: str
    scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.principal_id, str) or not self.principal_id:
            raise ValueError("principal_id must be non-empty text")
        for scope in self.scopes:
            if not isinstance(scope, str) or not scope:
                raise ValueError("scopes must be non-empty lowercase ids")


class MessageService:
    """邮箱消息命令的持久化业务实现;由 AgentControlPlane 组装持有,职责边界见模块 docstring。"""

    def __init__(
        self,
        *,
        event_store,
        graph: AgentGraph,
        mailbox: AgentMailbox,
        now: Callable[[], datetime],
        faults: FaultInjector,
    ) -> None:
        self.event_store = event_store
        self.graph = graph
        self.mailbox = mailbox
        # 时钟读取复用控制面已规范化的访问器(tz-aware UTC),规范逻辑单点保留。
        self.now = now
        self._faults = faults

    def send_message(
        self,
        to_agent_id: UUID,
        *,
        from_agent_id: UUID | None,
        kind: MessageKind,
        body_ref: str | None,
        idempotency_key: str,
    ) -> MessageRecord:
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 256:
            raise AgentError("invalid_message_idempotency")
        if body_ref is not None and (
            not isinstance(body_ref, str) or not body_ref or len(body_ref) > 2048
        ):
            raise AgentError("invalid_message_body_ref")
        target = self.graph.load(to_agent_id)
        if target is None:
            raise AgentError("message_target_missing")
        if target.state in (
            AgentState.COMPLETED,
            AgentState.FAILED,
            AgentState.CANCELLED,
        ):
            raise AgentError("message_target_terminal")
        existing_messages = self.mailbox.load(to_agent_id)
        for message in existing_messages:
            if message.idempotency_key == idempotency_key:
                return message
        message_id = uuid5(
            NAMESPACE_URL,
            f"koawa-d11:message:{to_agent_id}:{idempotency_key}",
        )
        sequence = len(existing_messages)
        observed_at = self.now()
        enqueue_command_id = uuid5(
            NAMESPACE_URL,
            f"koawa-d11:enqueue:{message_id}",
        )
        enqueued = _event(
            enqueue_command_id,
            "message-enqueued",
            "message.enqueued.v1",
            {
                "agent_id": str(to_agent_id),
                "message_id": str(message_id),
                "from_agent_id": (
                    None if from_agent_id is None else str(from_agent_id)
                ),
                "sequence": sequence,
                "kind": kind.value,
                "body_ref": body_ref,
                "idempotency_key": idempotency_key,
                "status": MessageStatus.QUEUED.value,
            },
            occurred_at=observed_at,
            correlation_id=_correlation(enqueue_command_id),
            turn_id=None,
            run_id=None,
        )
        for _ in range(MAX_CAS_RETRIES):
            mailbox_events = self._read_all(StreamId("mailbox", to_agent_id))
            mailbox_version = -1 if not mailbox_events else mailbox_events[-1].stream_version
            self._fault(
                FaultPoint.D11_ENQUEUE_BEFORE_APPEND,
                _facts(to_agent_id, target.attempt, version=mailbox_version),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", to_agent_id),
                            mailbox_version,
                            (enqueued,),
                        ),
                    ),
                    idempotency_key=enqueue_command_id,
                )
            except WrongExpectedVersion:
                rebuilt = self.mailbox.load(to_agent_id)
                if any(item.idempotency_key == idempotency_key for item in rebuilt):
                    return next(
                        item for item in rebuilt
                        if item.idempotency_key == idempotency_key
                    )
                continue
            self._fault(
                FaultPoint.D11_ENQUEUE_AFTER_COMMIT,
                _facts(to_agent_id, target.attempt, message_ids=(message_id,)),
            )
            rebuilt = self.mailbox.load(to_agent_id)
            return next(item for item in rebuilt if item.message_id == message_id)
        raise AgentError("mailbox_stream_conflict")

    def deliver_message(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        run_id: UUID,
        lease_seconds: int = 30,
    ) -> MessageRecord:
        for _ in range(MAX_CAS_RETRIES):
            record = self.graph.load(agent_id)
            if record is None or record.run_id != run_id:
                raise AgentError("stale_agent_run_fenced")
            snapshot = self.mailbox.snapshot(agent_id)
            message = self._message_in(snapshot, message_id)
            if message is None:
                raise AgentError("message_missing")
            if (
                message.status is MessageStatus.DELIVERED
                and message.delivered_run_id == run_id
            ):
                return message
            if message.status is not MessageStatus.QUEUED:
                raise AgentError("message_transition_invalid")
            delivery_attempt = message.delivery_attempt + 1
            command_id = uuid5(
                NAMESPACE_URL,
                "koawa-v2:deliver:"
                + str(agent_id)
                + ":"
                + str(message_id)
                + ":"
                + str(delivery_attempt),
            )
            observed_at = self.now()
            lease = observed_at + timedelta(seconds=lease_seconds)
            delivered = _event(
                command_id,
                "message-delivered",
                "message.delivered.v2",
                {
                    "agent_id": str(agent_id),
                    "message_id": str(message_id),
                    "run_id": str(run_id),
                    "agent_attempt": record.attempt,
                    "delivery_attempt": delivery_attempt,
                    "lease_expires_at": lease.isoformat(),
                    "delivered_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            fingerprint = _fingerprint({
                "operation": "deliver",
                "agent_id": str(agent_id),
                "message_id": str(message_id),
                "run_id": str(run_id),
                "agent_attempt": record.attempt,
                "delivery_attempt": delivery_attempt,
                "lease_seconds": lease_seconds,
            })
            self._fault(
                FaultPoint.D11_DELIVER_BEFORE_APPEND,
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=delivery_attempt,
                    version=snapshot.stream_version,
                ),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                            (delivered,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("agent", agent_id),
                            record.version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return message
            self._fault(
                FaultPoint.D11_DELIVER_AFTER_COMMIT,
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=delivery_attempt,
                ),
            )
            return self._message_in(self.mailbox.snapshot(agent_id), message_id)
        raise AgentError("mailbox_stream_conflict")

    def record_message_result(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        run_id: UUID,
        expected_delivery_attempt: int,
        outcome: str | None = None,
        error_code: str | None = None,
    ) -> MessageRecord:
        canonical_outcome, is_error, error_code = canonicalize_result(
            outcome, error_code
        )
        command_id = uuid5(
            NAMESPACE_URL,
            "koawa-v2:result:"
            + str(agent_id)
            + ":"
            + str(message_id)
            + ":"
            + str(expected_delivery_attempt),
        )
        result_ref = result_ref_for(
            agent_id, message_id, expected_delivery_attempt
        )
        result_digest = result_digest_for(
            agent_id,
            message_id,
            expected_delivery_attempt,
            canonical_outcome,
            is_error,
            error_code,
        )
        fingerprint = _fingerprint({
            "operation": "record_message_result",
            "agent_id": str(agent_id),
            "message_id": str(message_id),
            "run_id": str(run_id),
            "delivery_attempt": expected_delivery_attempt,
            "result_ref": result_ref,
            "result_digest": result_digest,
            "is_error": is_error,
            "error_code": error_code,
        })
        for _ in range(MAX_CAS_RETRIES):
            record = self.graph.load(agent_id)
            if record is None or record.run_id != run_id:
                raise AgentError("stale_agent_run_fenced")
            snapshot = self.mailbox.snapshot(agent_id)
            message = self._message_in(snapshot, message_id)
            if message is None:
                raise AgentError("message_missing")
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                if receipt == "committed":
                    raise AgentError("message_result_identity_conflict")
                return message
            if (
                message.status is MessageStatus.RESULT_RECORDED
                and message.delivery_attempt == expected_delivery_attempt
                and message.result_digest == result_digest
            ):
                return message
            if message.legacy_delivery:
                raise AgentError("legacy_delivery_requires_resolution")
            if message.status is not MessageStatus.DELIVERED:
                raise AgentError("message_transition_invalid")
            if message.delivery_attempt != expected_delivery_attempt:
                raise AgentError("stale_message_delivery_attempt")
            if message.delivered_run_id != run_id:
                raise AgentError("stale_agent_run_fenced")
            summary = summarize_outcome(canonical_outcome)
            observed_at = self.now()
            recorded = _event(
                command_id,
                "message-result-recorded",
                "message.result-recorded.v1",
                {
                    "agent_id": str(agent_id),
                    "message_id": str(message_id),
                    "delivery_run_id": str(message.delivered_run_id),
                    "recording_run_id": str(run_id),
                    "agent_attempt": record.attempt,
                    "delivery_attempt": expected_delivery_attempt,
                    "result_ref": result_ref,
                    "result_digest": result_digest,
                    "result_summary": summary,
                    "is_error": is_error,
                    "error_code": error_code,
                    "recorded_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            self._fault(
                FaultPoint.D11_RESULT_BEFORE_APPEND,
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=expected_delivery_attempt,
                    version=snapshot.stream_version,
                ),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                            (recorded,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("agent", agent_id),
                            record.version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                raise AgentError("message_result_identity_conflict") from None
            self._fault(
                FaultPoint.D11_RESULT_AFTER_COMMIT,
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=expected_delivery_attempt,
                ),
            )
            return self._message_in(self.mailbox.snapshot(agent_id), message_id)
        raise AgentError("mailbox_stream_conflict")

    def ack_message(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        run_id: UUID,
    ) -> MessageRecord:
        for _ in range(MAX_CAS_RETRIES):
            record = self.graph.load(agent_id)
            if record is None or record.run_id != run_id:
                raise AgentError("stale_agent_run_fenced")
            snapshot = self.mailbox.snapshot(agent_id)
            message = self._message_in(snapshot, message_id)
            if message is None:
                raise AgentError("message_missing")
            command_id = uuid5(
                NAMESPACE_URL,
                "koawa-v2:ack:"
                + str(agent_id)
                + ":"
                + str(message_id)
                + ":"
                + str(message.delivery_attempt),
            )
            fingerprint = _fingerprint({
                "operation": "ack_message",
                "agent_id": str(agent_id),
                "message_id": str(message_id),
                "run_id": str(run_id),
                "delivery_attempt": message.delivery_attempt,
                "result_ref": message.result_ref,
                "result_digest": message.result_digest,
            })
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return message
            if message.status is MessageStatus.ACKED:
                return message
            if message.status is not MessageStatus.RESULT_RECORDED:
                if message.legacy_delivery:
                    raise AgentError("legacy_delivery_requires_resolution")
                raise AgentError("message_transition_invalid")
            if message.result_ref is None or message.result_digest is None:
                raise AgentError("message_result_required")
            observed_at = self.now()
            acked = _event(
                command_id,
                "message-acked",
                "message.acked.v2",
                {
                    "agent_id": str(agent_id),
                    "message_id": str(message_id),
                    "ack_run_id": str(run_id),
                    "delivery_attempt": message.delivery_attempt,
                    "result_ref": message.result_ref,
                    "result_digest": message.result_digest,
                    "acked_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            self._fault(
                FaultPoint.D11_ACK_BEFORE_APPEND,
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=message.delivery_attempt,
                    version=snapshot.stream_version,
                ),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                            (acked,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("agent", agent_id),
                            record.version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return message
            self._fault(
                FaultPoint.D11_ACK_AFTER_COMMIT,
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=message.delivery_attempt,
                ),
            )
            return self._message_in(self.mailbox.snapshot(agent_id), message_id)
        raise AgentError("mailbox_stream_conflict")

    def mark_message_unresolved(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        abandoned_run_id: UUID,
        takeover_run_id: UUID,
        expected_delivery_attempt: int,
        reason: str,
    ) -> MessageRecord:
        """Internal primitive; the real takeover uses the atomic batch in the
        private _takeover method. Only usable when the agent is currently owned
        by takeover_run_id.
        """

        for _ in range(MAX_CAS_RETRIES):
            record = self.graph.load(agent_id)
            if record is None or record.run_id != takeover_run_id:
                raise AgentError("stale_agent_run_fenced")
            snapshot = self.mailbox.snapshot(agent_id)
            message = self._message_in(snapshot, message_id)
            if message is None:
                raise AgentError("message_missing")
            command_id = uuid5(
                NAMESPACE_URL,
                "koawa-v2:unresolved:"
                + str(agent_id)
                + ":"
                + str(message_id)
                + ":"
                + str(expected_delivery_attempt),
            )
            fingerprint = _fingerprint({
                "operation": "mark_message_unresolved",
                "agent_id": str(agent_id),
                "message_id": str(message_id),
                "abandoned_run_id": str(abandoned_run_id),
                "takeover_run_id": str(takeover_run_id),
                "delivery_attempt": expected_delivery_attempt,
                "reason": reason,
            })
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return message
            if (
                message.status is MessageStatus.UNRESOLVED
                and message.delivery_attempt == expected_delivery_attempt
            ):
                return message
            if message.legacy_delivery:
                raise AgentError("legacy_delivery_requires_resolution")
            if message.status is not MessageStatus.DELIVERED:
                raise AgentError("message_transition_invalid")
            if message.delivery_attempt != expected_delivery_attempt:
                raise AgentError("stale_message_delivery_attempt")
            if message.delivered_run_id != abandoned_run_id:
                raise AgentError("stale_agent_run_fenced")
            observed_at = self.now()
            unresolved = _event(
                command_id,
                "message-unresolved",
                "message.unresolved.v1",
                {
                    "agent_id": str(agent_id),
                    "message_id": str(message_id),
                    "abandoned_run_id": str(abandoned_run_id),
                    "delivery_attempt": expected_delivery_attempt,
                    "reason": reason,
                    "observed_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                            (unresolved,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("agent", agent_id),
                            record.version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                continue
            except IdempotencyConflict:
                return message
            self._fault(
                FaultPoint.D11_UNRESOLVED_AFTER_COMMIT,
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=expected_delivery_attempt,
                ),
            )
            return self._message_in(self.mailbox.snapshot(agent_id), message_id)
        raise AgentError("mailbox_stream_conflict")

    def requeue_message(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        expected_delivery_attempt: int,
        decision_id: UUID,
        actor: Principal,
        approval_id: UUID | None,
        resolution_kind: Literal[
            "proven_not_started", "idempotent_read", "operator_retry"
        ],
        reason: str,
    ) -> MessageRecord:
        record = self.graph.load(agent_id)
        if record is None:
            raise AgentError("agent_missing")
        self._require_operator_authority(
            record,
            actor,
            approval_id=approval_id,
            resolution_kind=resolution_kind,
            error_code="message_requeue_not_authorized",
        )
        command_id = uuid5(
            NAMESPACE_URL,
            "koawa-v2:requeue:" + str(decision_id),
        )
        fingerprint = _fingerprint({
            "operation": "requeue_message",
            "agent_id": str(agent_id),
            "message_id": str(message_id),
            "decision_id": str(decision_id),
            "actor": actor.principal_id,
            "approval_id": None if approval_id is None else str(approval_id),
            "resolution_kind": resolution_kind,
            "reason": reason,
            "expected_delivery_attempt": expected_delivery_attempt,
        })
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self.mailbox.snapshot(agent_id)
            message = self._message_in(snapshot, message_id)
            if message is None:
                raise AgentError("message_missing")
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return message
            if (
                message.status is MessageStatus.QUEUED
                and message.delivery_attempt == expected_delivery_attempt
            ):
                return message
            if message.status is not MessageStatus.UNRESOLVED:
                raise AgentError("message_transition_invalid")
            if message.delivery_attempt != expected_delivery_attempt:
                raise AgentError("stale_message_delivery_attempt")
            observed_at = self.now()
            requeued = _event(
                command_id,
                "message-requeued",
                "message.requeued.v1",
                {
                    "agent_id": str(agent_id),
                    "message_id": str(message_id),
                    "previous_delivery_attempt": message.delivery_attempt,
                    "reason": reason,
                    "resolution_kind": resolution_kind,
                    "decision_id": str(decision_id),
                    "actor_principal_id": actor.principal_id,
                    "approval_id": None if approval_id is None else str(approval_id),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                            (requeued,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("agent", agent_id),
                            record.version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                record = self.graph.load(agent_id)
                if record is None:
                    raise AgentError("agent_missing") from None
                continue
            except IdempotencyConflict:
                return message
            return self._message_in(self.mailbox.snapshot(agent_id), message_id)
        raise AgentError("mailbox_stream_conflict")

    def cancel_message(
        self,
        agent_id: UUID,
        message_id: UUID,
        *,
        expected_delivery_attempt: int,
        decision_id: UUID,
        actor: Principal,
        approval_id: UUID | None,
        reason: str,
    ) -> MessageRecord:
        record = self.graph.load(agent_id)
        if record is None:
            raise AgentError("agent_missing")
        self._require_operator_authority(
            record,
            actor,
            approval_id=approval_id,
            resolution_kind="operator_retry" if approval_id is not None else "proven_not_started",
            error_code="message_resolution_not_authorized",
        )
        command_id = uuid5(
            NAMESPACE_URL,
            "koawa-v2:cancel:" + str(decision_id),
        )
        fingerprint = _fingerprint({
            "operation": "cancel_message",
            "agent_id": str(agent_id),
            "message_id": str(message_id),
            "decision_id": str(decision_id),
            "actor": actor.principal_id,
            "approval_id": None if approval_id is None else str(approval_id),
            "reason": reason,
            "expected_delivery_attempt": expected_delivery_attempt,
        })
        for _ in range(MAX_CAS_RETRIES):
            snapshot = self.mailbox.snapshot(agent_id)
            message = self._message_in(snapshot, message_id)
            if message is None:
                raise AgentError("message_missing")
            receipt = self._read_receipt(command_id, fingerprint)
            if receipt is not None:
                return message
            if message.status in (MessageStatus.ACKED, MessageStatus.CANCELLED):
                raise AgentError("message_transition_invalid")
            observed_at = self.now()
            if message.status in (
                MessageStatus.DELIVERED,
                MessageStatus.RESULT_RECORDED,
            ):
                if message.legacy_delivery:
                    raise AgentError("legacy_delivery_requires_resolution")
                if message.delivery_attempt != expected_delivery_attempt:
                    raise AgentError("stale_message_delivery_attempt")
                if message.cancel_requested:
                    return message
                requested = _event(
                    command_id,
                    "message-cancel-requested",
                    "message.cancel-requested.v1",
                    {
                        "agent_id": str(agent_id),
                        "message_id": str(message_id),
                        "delivery_run_id": str(message.delivered_run_id),
                        "delivery_attempt": message.delivery_attempt,
                        "decision_id": str(decision_id),
                        "actor_principal_id": actor.principal_id,
                        "approval_id": None if approval_id is None else str(approval_id),
                        "reason": reason,
                        "requested_at": observed_at.isoformat(),
                    },
                    occurred_at=observed_at,
                    correlation_id=_correlation(command_id),
                )
                self._fault(
                    FaultPoint.D11_CANCEL_BEFORE_APPEND,
                    _facts(
                        agent_id,
                        record.attempt,
                        message_ids=(message_id,),
                        delivery_attempt=message.delivery_attempt,
                        version=snapshot.stream_version,
                    ),
                )
                try:
                    self.event_store.append_batch(
                        (
                            StreamWrite(
                                StreamId("mailbox", agent_id),
                                snapshot.stream_version,
                                (requested,),
                            ),
                        ),
                        idempotency_key=command_id,
                        request_fingerprint=fingerprint,
                        preconditions=(
                            StreamPrecondition(
                                StreamId("agent", agent_id),
                                record.version,
                            ),
                        ),
                    )
                except WrongExpectedVersion:
                    record = self.graph.load(agent_id)
                    if record is None:
                        raise AgentError("agent_missing") from None
                    continue
                except IdempotencyConflict:
                    return message
                self._fault(
                    FaultPoint.D11_CANCEL_AFTER_COMMIT,
                    _facts(
                        agent_id,
                        record.attempt,
                        message_ids=(message_id,),
                        delivery_attempt=message.delivery_attempt,
                    ),
                )
                return self._message_in(self.mailbox.snapshot(agent_id), message_id)
            if message.status is MessageStatus.QUEUED:
                if expected_delivery_attempt != 0:
                    raise AgentError("stale_message_delivery_attempt")
                delivery_attempt_wire = 0
                previous_status = "queued"
            elif message.status is MessageStatus.UNRESOLVED:
                if message.delivery_attempt != expected_delivery_attempt:
                    raise AgentError("stale_message_delivery_attempt")
                delivery_attempt_wire = message.delivery_attempt
                previous_status = "unresolved"
            else:
                raise AgentError("message_transition_invalid")
            cancelled = _event(
                command_id,
                "message-cancelled",
                "message.cancelled.v2",
                {
                    "agent_id": str(agent_id),
                    "message_id": str(message_id),
                    "previous_status": previous_status,
                    "delivery_attempt": delivery_attempt_wire,
                    "decision_id": str(decision_id),
                    "actor_principal_id": actor.principal_id,
                    "approval_id": None if approval_id is None else str(approval_id),
                    "reason": reason,
                    "cancelled_at": observed_at.isoformat(),
                },
                occurred_at=observed_at,
                correlation_id=_correlation(command_id),
            )
            self._fault(
                FaultPoint.D11_CANCEL_BEFORE_APPEND,
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=delivery_attempt_wire,
                    version=snapshot.stream_version,
                ),
            )
            try:
                self.event_store.append_batch(
                    (
                        StreamWrite(
                            StreamId("mailbox", agent_id),
                            snapshot.stream_version,
                            (cancelled,),
                        ),
                    ),
                    idempotency_key=command_id,
                    request_fingerprint=fingerprint,
                    preconditions=(
                        StreamPrecondition(
                            StreamId("agent", agent_id),
                            record.version,
                        ),
                    ),
                )
            except WrongExpectedVersion:
                record = self.graph.load(agent_id)
                if record is None:
                    raise AgentError("agent_missing") from None
                continue
            except IdempotencyConflict:
                return message
            self._fault(
                FaultPoint.D11_CANCEL_AFTER_COMMIT,
                _facts(
                    agent_id,
                    record.attempt,
                    message_ids=(message_id,),
                    delivery_attempt=delivery_attempt_wire,
                ),
            )
            return self._message_in(self.mailbox.snapshot(agent_id), message_id)
        raise AgentError("mailbox_stream_conflict")

    # ------------------------------------------------------------------
    # 内部辅助(操作员授权 / 幂等回执 / 消息定位 / 故障钩子)
    # ------------------------------------------------------------------

    def _require_operator_authority(
        self,
        record: AgentRecord,
        actor: Principal,
        *,
        approval_id: UUID | None,
        resolution_kind: str,
        error_code: str,
    ) -> None:
        scopes = set(actor.scopes)
        authorized = "agents.resolve:any" in scopes or (
            actor.principal_id == record.principal_id
            and "agents.resolve" in scopes
        )
        if not authorized:
            raise AgentError(error_code)
        if resolution_kind == "operator_retry" and approval_id is None:
            raise AgentError(error_code)

    def _read_receipt(self, command_id: UUID, fingerprint: str):
        """幂等回执读取:单点实现位于 agents/events.py(_read_receipt),此处薄委托。"""
        return _read_receipt(self.event_store, command_id, fingerprint)

    def _read_all(self, stream: StreamId) -> tuple:
        """流读取分页:单点实现位于 agents/events.py(_read_all),此处薄委托。"""
        return _read_all(self.event_store, stream)

    def _message_in(
        self, snapshot: MailboxSnapshot, message_id: UUID
    ) -> MessageRecord | None:
        for message in snapshot.messages:
            if message.message_id == message_id:
                return message
        return None

    def _fault(self, point: str, facts: Mapping[str, object]) -> None:
        self._faults(point, facts)
