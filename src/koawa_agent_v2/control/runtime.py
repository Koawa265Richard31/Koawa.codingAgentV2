"""Thread/Turn 的同步应用服务：校验命令，并把状态变化持久化为事件。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .event_store import (
    AppendReceipt,
    EventMetadata,
    EventStore,
    NewEvent,
    StreamId,
    StreamWrite,
    WrongExpectedVersion,
)
from .models import reduce_execution_seed
from .durable_json import (
    CanonicalText,
    CanonicalTextError,
    CanonicalTextPolicy,
    canonicalize_text,
)
from .models import (
    RESUMABLE_TURN_STATUSES,
    TURN_RECOVERY_LEASE_CLAIMED,
    TURN_RECOVERY_LEASE_HEARTBEATED,
    TURN_RECOVERY_LEASE_RELEASED,
    TURN_STALE_RUN_REQUEUED,
    THREAD_ARCHIVED,
    THREAD_CREATED,
    THREAD_TURN_ATTACHED,
    THREAD_TURN_DETACHED,
    TURN_CANCELLED,
    TURN_COMPLETED,
    TURN_CREATED,
    TURN_FAILED,
    TURN_PAUSED,
    TURN_RECOVERY_QUEUED,
    TURN_STARTED,
    TURN_TIMED_OUT,
    TURN_WAITING_FOR_APPROVAL,
    TURN_WAITING_FOR_INPUT,
    InvalidTransition,
    ThreadState,
    ThreadStatus,
    TurnState,
    TurnStatus,
    rebuild_thread,
    rebuild_turn,
)


class ThreadRuntime:
    """持久化控制面的应用服务，负责命令校验、状态栅栏与原子事件提交。

    Runtime 本身不保存可变状态；每次命令都从事件流重建聚合，再根据当前状态
    生成新事件。这样进程退出后，只要 EventStore 仍在，就能恢复 Thread/Turn。
    """

    def __init__(
        self,
        store: EventStore,
        *,
        actor: str = "runtime",
        text_policy: CanonicalTextPolicy | None = None,
    ) -> None:
        """注入事件存储，并设置写入事件元数据的审计主体。

        text_policy (I4) carries the per-kind byte ceilings used to build the
        canonical user text.  Every free-text entry point (user input, wait
        prompt, pause/cancel/timeout reason, resume response, summary/error)
        is canonicalized BEFORE the request fingerprint and event payload are
        formed, so the first model request, idempotency identity, persisted
        event and any later resume all share the same canonical value.
        """
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be non-empty")
        if text_policy is not None and not isinstance(text_policy, CanonicalTextPolicy):
            raise TypeError("text_policy must be CanonicalTextPolicy or None")
        self._store = store
        self._actor = actor
        self._text_policy = text_policy or CanonicalTextPolicy()

    def create_thread(
        self,
        workspace_ref: str,
        *,
        thread_id: UUID | str | None = None,
        command_id: UUID | str | None = None,
    ) -> ThreadState:
        """创建 Thread；相同 command_id 的重试返回首次提交的同一结果。

        未显式提供 thread_id 时，会用 command_id 确定性派生 ID。因而请求在
        “数据库已提交、响应却丢失”后重试，不会意外创建第二个 Thread。
        新事件流以 expected_version=-1 写入，含义是该流必须尚不存在。
        """
        workspace_ref = _non_empty(workspace_ref, "workspace_ref")
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        resolved_thread_id = _optional_uuid(thread_id) or _derived_id(
            resolved_command_id,
            "thread",
        )
        fingerprint = self._fingerprint(
            "create_thread",
            {
                "workspace_ref": workspace_ref,
                "thread_id": resolved_thread_id,
            },
        )
        # 先查持久化命令回执，再读取/校验当前状态。若首次请求其实已经成功，
        # 后续状态即使变化，也应重放首次提交的结果，而不是把重试误判成冲突。
        if receipt := self._committed_receipt(
            resolved_command_id,
            fingerprint,
            _thread_stream(resolved_thread_id),
        ):
            return self._thread_from_receipt(resolved_thread_id, receipt)

        event = self._event(
            event_type=THREAD_CREATED,
            payload={
                "thread_id": str(resolved_thread_id),
                "workspace_ref": workspace_ref,
            },
            occurred_at=_now(),
            command_id=resolved_command_id,
            event_slot="thread-created",
            thread_id=resolved_thread_id,
        )
        receipt = self._append(
            resolved_command_id,
            fingerprint,
            StreamWrite(
                stream_id=_thread_stream(resolved_thread_id),
                expected_version=-1,
                events=(event,),
            ),
        )
        return self._thread_from_receipt(resolved_thread_id, receipt)

    def create_turn(
        self,
        thread_id: UUID | str,
        user_input: str,
        *,
        expected_thread_version: int,
        turn_id: UUID | str | None = None,
        command_id: UUID | str | None = None,
    ) -> TurnState:
        """在 Thread 中创建唯一活跃 Turn，并原子更新 Thread/Turn 两条流。

        expected_thread_version 是调用者读到 Thread 后携带回来的乐观锁版本，
        防止两个并发请求都认为 Thread 没有活跃 Turn。成功提交必须同时写入
        ``thread.turn-attached`` 与 ``turn.created``，避免只完成一半的悬空状态。
        """
        resolved_thread_id = _as_uuid(thread_id, "thread_id")
        canonical_input = self._canonical_required(
            user_input,
            self._text_policy.user_input_max_utf8_bytes,
            "user_input",
        )
        expected_thread_version = _expected_version(
            expected_thread_version,
            "expected_thread_version",
        )
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        resolved_turn_id = _optional_uuid(turn_id) or _derived_id(
            resolved_command_id,
            f"turn:{resolved_thread_id}",
        )
        fingerprint = self._fingerprint(
            "create_turn",
            {
                "thread_id": resolved_thread_id,
                "turn_id": resolved_turn_id,
                "user_input": canonical_input.value,
                "expected_thread_version": expected_thread_version,
                **_canonical_args(canonical_input),
            },
        )
        # command_id 是语义命令的幂等键；同键同参数返回原 receipt，同键异参
        # 会由 EventStore 拒绝，防止一个命令标识被复用于另一项业务操作。
        if receipt := self._committed_receipt(
            resolved_command_id,
            fingerprint,
            _turn_stream(resolved_turn_id),
        ):
            return self._turn_from_receipt(resolved_turn_id, receipt)

        thread = self.get_thread(resolved_thread_id)
        _check_version(
            _thread_stream(resolved_thread_id),
            expected_thread_version,
            thread.version,
        )
        if thread.status is not ThreadStatus.OPEN:
            raise InvalidTransition("cannot create a turn in an archived thread")
        if thread.active_turn_id is not None:
            raise InvalidTransition(
                f"thread already has active turn {thread.active_turn_id}"
            )

        occurred_at = _now()
        metadata_args = {
            "occurred_at": occurred_at,
            "command_id": resolved_command_id,
            "thread_id": resolved_thread_id,
            "turn_id": resolved_turn_id,
        }
        attached = self._event(
            event_type=THREAD_TURN_ATTACHED,
            payload={"turn_id": str(resolved_turn_id)},
            event_slot="thread-turn-attached",
            **metadata_args,
        )
        created = self._event(
            event_type=TURN_CREATED,
            payload={
                "turn_id": str(resolved_turn_id),
                "thread_id": str(resolved_thread_id),
                "user_input": canonical_input.value,
            },
            event_slot="turn-created",
            **metadata_args,
        )
        # 两个 StreamWrite 由 append_batch 放入同一数据库事务：要么 Thread
        # 绑定 Turn 且 Turn 创建成功，要么两条事件都不可见。
        receipt = self._append(
            resolved_command_id,
            fingerprint,
            StreamWrite(
                stream_id=_thread_stream(resolved_thread_id),
                expected_version=expected_thread_version,
                events=(attached,),
            ),
            StreamWrite(
                stream_id=_turn_stream(resolved_turn_id),
                expected_version=-1,
                events=(created,),
            ),
        )
        return self._turn_from_receipt(resolved_turn_id, receipt)

    def start_turn(
        self,
        turn_id: UUID | str,
        expected_version: int,
        *,
        command_id: UUID | str | None = None,
        execution_seed: ExecutionSeedDTO | None = None,
        execution_expected_version: int | None = None,
        lease_owner_id: str | None = None,
        lease_seconds: int | None = None,
    ) -> TurnState:
        """把 QUEUED Turn 启动为一次新的 Run，并增加 attempt。

        run_id 由本次 command_id 确定性派生：命令重试得到同一 Run，而一次
        真正的新启动命令得到新 Run。后续 Worker 写操作必须携带它作为执行者栅栏。
        可恢复启动携带 ``ExecutionSeedDTO``，且只有本 Runtime 能填充其中的
        thread/turn/run/attempt/turn_stream_version 身份字段。
        """
        # Lazy import avoids the recovery-package init cycle (recovery imports
        # ThreadRuntime for its coordinator).
        from ..recovery.execution import ExecutionSeedDTO

        resolved_turn_id = _as_uuid(turn_id, "turn_id")
        expected_version = _expected_version(expected_version)
        if execution_seed is not None and not isinstance(execution_seed, ExecutionSeedDTO):
            raise TypeError("execution_seed must be ExecutionSeedDTO or None")
        durable_start = execution_seed is not None
        if durable_start:
            if (
                not isinstance(execution_expected_version, int)
                or isinstance(execution_expected_version, bool)
                or execution_expected_version < -1
            ):
                raise ValueError("execution_expected_version must be an integer >= -1")
            if not isinstance(lease_owner_id, str) or not lease_owner_id.strip():
                raise ValueError("lease_owner_id must be non-empty for a durable start")
            if (
                not isinstance(lease_seconds, int)
                or isinstance(lease_seconds, bool)
                or lease_seconds < 1
            ):
                raise ValueError("lease_seconds must be positive for a durable start")
        elif any(
            value is not None
            for value in (execution_expected_version, lease_owner_id, lease_seconds)
        ):
            raise ValueError("durable start arguments require execution_seed")
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        resolved_run_id = _derived_id(
            resolved_command_id,
            f"run:{resolved_turn_id}",
        )
        fingerprint = self._fingerprint(
            "start_turn",
            {
                "turn_id": resolved_turn_id,
                "expected_version": expected_version,
                "run_id": resolved_run_id,
                "execution_seed": (
                    None
                    if execution_seed is None
                    else execution_seed.to_document_partial()
                ),
                "execution_expected_version": execution_expected_version,
                "lease_owner_id": lease_owner_id,
                "lease_seconds": lease_seconds,
            },
        )
        if receipt := self._committed_receipt(
            resolved_command_id,
            fingerprint,
            _turn_stream(resolved_turn_id),
        ):
            return self._turn_from_receipt(resolved_turn_id, receipt)

        turn = self._get_turn_at_version(resolved_turn_id, expected_version)
        if turn.status is not TurnStatus.QUEUED:
            raise InvalidTransition(f"cannot start a turn in {turn.status.value}")
        if not durable_start:
            return self._append_turn_event(
                turn,
                resolved_command_id,
                fingerprint,
                TURN_STARTED,
                {"run_id": str(resolved_run_id), "attempt": turn.attempt + 1},
                event_slot="turn-started",
                run_id=resolved_run_id,
            )

        occurred_at = _now()
        started = self._event(
            event_type=TURN_STARTED,
            payload={
                "run_id": str(resolved_run_id),
                "attempt": turn.attempt + 1,
                "lease_owner_id": lease_owner_id,
                "lease_seconds": lease_seconds,
            },
            occurred_at=occurred_at,
            command_id=resolved_command_id,
            event_slot="turn-started",
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            run_id=resolved_run_id,
        )
        assert execution_seed is not None
        seed_document = execution_seed.with_identity(
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            run_id=resolved_run_id,
            attempt=turn.attempt + 1,
            turn_stream_version=turn.version + 1,
        ).to_document()
        # models.py full seed reducer: the run-execution seed must agree with
        # the authoritative Turn identity before it is appended.
        reduce_execution_seed(
            seed_document, turn=turn, turn_stream_version=turn.version + 1,
        )
        seeded = self._event(
            event_type="run.context-seeded.v2",
            payload=seed_document,
            occurred_at=occurred_at,
            command_id=resolved_command_id,
            event_slot="run-context-seeded",
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            run_id=resolved_run_id,
        )
        receipt = self._append(
            resolved_command_id,
            fingerprint,
            StreamWrite(
                stream_id=_turn_stream(turn.turn_id),
                expected_version=turn.version,
                events=(started,),
            ),
            StreamWrite(
                stream_id=_execution_stream(turn.turn_id),
                expected_version=execution_expected_version,
                events=(seeded,),
            ),
        )
        return self._turn_from_receipt(turn.turn_id, receipt)

    def claim_recovery_run(
        self,
        turn_id: UUID | str,
        *,
        expected_version: int,
        owner_id: str,
        lease_seconds: int,
        command_id: UUID | str | None = None,
    ) -> TurnState:
        """Claim a recovery lease overlay on the active run (typed event).

        Writes turn.recovery-lease-claimed.v1 with an exact payload and a fresh
        claim token; heartbeat/release must reuse the same run and token.
        """
        resolved_turn_id = _as_uuid(turn_id, "turn_id")
        expected_version = _expected_version(expected_version)
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must be non-empty")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        claimed_run = _derived_id(resolved_command_id, "recovery-claim-run")
        claim_token = _derived_id(resolved_command_id, "recovery-claim-token")
        fingerprint = self._fingerprint(
            "claim_recovery_run",
            {
                "turn_id": resolved_turn_id,
                "expected_version": expected_version,
                "owner_id": owner_id,
                "lease_seconds": lease_seconds,
                "claim_token": claim_token,
            },
        )
        if receipt := self._committed_receipt(
            resolved_command_id, fingerprint, _turn_stream(resolved_turn_id)
        ):
            return self._turn_from_receipt(resolved_turn_id, receipt)
        turn = self._get_turn_at_version(resolved_turn_id, expected_version)
        if turn.status is not TurnStatus.RUNNING:
            raise InvalidTransition(f"cannot claim a lease in {turn.status.value}")
        if turn.current_run_id is None:
            raise InvalidTransition("running turn is missing a run id")
        occurred_at = _now()
        claim_run_id = turn.current_run_id
        expires_at = self._store.database_time() + timedelta(seconds=lease_seconds)
        event = self._event(
            event_type=TURN_RECOVERY_LEASE_CLAIMED,
            payload={
                "thread_id": str(turn.thread_id),
                "turn_id": str(turn.turn_id),
                "run_id": str(claim_run_id),
                "owner": owner_id,
                "claim_token": str(claim_token),
                "attempt": turn.attempt,
                "lease_expires_at": _now_text(expires_at),
            },
            occurred_at=occurred_at,
            command_id=resolved_command_id,
            event_slot="recovery-lease-claimed",
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            run_id=claim_run_id,
        )
        receipt = self._append(
            resolved_command_id,
            fingerprint,
            StreamWrite(
                stream_id=_turn_stream(turn.turn_id),
                expected_version=turn.version,
                events=(event,),
            ),
        )
        return self._turn_from_receipt(turn.turn_id, receipt)

    def heartbeat_recovery_run(
        self,
        turn_id: UUID | str,
        *,
        expected_version: int,
        run_id: UUID | str,
        claim_token: UUID | str | None,
        lease_seconds: int,
        command_id: UUID | str | None = None,
        owner_id: str | None = None,
    ) -> TurnState:
        """Renew the recovery lease overlay through a typed event."""
        resolved_turn_id = _as_uuid(turn_id, "turn_id")
        expected_version = _expected_version(expected_version)
        resolved_run_id = _as_uuid(run_id, "run_id")
        resolved_token = (
            None if claim_token is None else _as_uuid(claim_token, "claim_token")
        )
        owner = owner_id if owner_id is not None else self._actor
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner_id must be non-empty")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        fingerprint = self._fingerprint(
            "heartbeat_recovery_run",
            {
                "turn_id": resolved_turn_id,
                "expected_version": expected_version,
                "run_id": resolved_run_id,
                "claim_token": resolved_token,
                "lease_seconds": lease_seconds,
                "owner_id": owner,
            },
        )
        if receipt := self._committed_receipt(
            resolved_command_id, fingerprint, _turn_stream(resolved_turn_id)
        ):
            return self._turn_from_receipt(resolved_turn_id, receipt)
        turn = self._get_turn_at_version(resolved_turn_id, expected_version)
        if turn.status is not TurnStatus.RUNNING:
            raise InvalidTransition(f"cannot heartbeat a lease in {turn.status.value}")
        if turn.recovery_claim_token != resolved_token:
            raise InvalidTransition("recovery lease heartbeat token mismatch")
            raise InvalidTransition("recovery lease heartbeat token mismatch")
        expires_at = self._store.database_time() + timedelta(seconds=lease_seconds)
        event = self._event(
            event_type=TURN_RECOVERY_LEASE_HEARTBEATED,
            payload={
                "thread_id": str(turn.thread_id),
                "turn_id": str(turn.turn_id),
                "run_id": str(resolved_run_id),
                "owner": owner,
                "claim_token": (
                    None if resolved_token is None else str(resolved_token)
                ),
                "attempt": turn.attempt,
                "lease_expires_at": _now_text(expires_at),
            },
            occurred_at=_now(),
            command_id=resolved_command_id,
            event_slot="recovery-lease-heartbeated",
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            run_id=resolved_run_id,
        )
        receipt = self._append(
            resolved_command_id,
            fingerprint,
            StreamWrite(
                stream_id=_turn_stream(turn.turn_id),
                expected_version=turn.version,
                events=(event,),
            ),
        )
        return self._turn_from_receipt(turn.turn_id, receipt)

    def release_recovery_run(
        self,
        turn_id: UUID | str,
        *,
        expected_version: int,
        run_id: UUID | str,
        claim_token: UUID | str | None,
        command_id: UUID | str | None = None,
        owner_id: str | None = None,
    ) -> TurnState:
        """Release the recovery lease overlay (typed event, no expiry field)."""
        resolved_turn_id = _as_uuid(turn_id, "turn_id")
        expected_version = _expected_version(expected_version)
        resolved_run_id = _as_uuid(run_id, "run_id")
        resolved_token = (
            None if claim_token is None else _as_uuid(claim_token, "claim_token")
        )
        owner = owner_id if owner_id is not None else self._actor
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        fingerprint = self._fingerprint(
            "release_recovery_run",
            {
                "turn_id": resolved_turn_id,
                "expected_version": expected_version,
                "run_id": resolved_run_id,
                "claim_token": resolved_token,
                "owner_id": owner,
            },
        )
        if receipt := self._committed_receipt(
            resolved_command_id, fingerprint, _turn_stream(resolved_turn_id)
        ):
            return self._turn_from_receipt(resolved_turn_id, receipt)
        turn = self._get_turn_at_version(resolved_turn_id, expected_version)
        if turn.status is not TurnStatus.RUNNING:
            raise InvalidTransition(f"cannot release a lease in {turn.status.value}")
        if turn.recovery_claim_token != resolved_token:
            raise InvalidTransition("recovery lease release token mismatch")
            raise InvalidTransition("recovery lease release token mismatch")
        event = self._event(
            event_type=TURN_RECOVERY_LEASE_RELEASED,
            payload={
                "thread_id": str(turn.thread_id),
                "turn_id": str(turn.turn_id),
                "run_id": str(resolved_run_id),
                "owner": owner,
                "claim_token": (
                    None if resolved_token is None else str(resolved_token)
                ),
                "attempt": turn.attempt,
                "released_at": _now_text(self._store.database_time()),
            },
            occurred_at=_now(),
            command_id=resolved_command_id,
            event_slot="recovery-lease-released",
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            run_id=resolved_run_id,
        )
        receipt = self._append(
            resolved_command_id,
            fingerprint,
            StreamWrite(
                stream_id=_turn_stream(turn.turn_id),
                expected_version=turn.version,
                events=(event,),
            ),
        )
        return self._turn_from_receipt(turn.turn_id, receipt)

    def requeue_stale_run(
        self,
        turn_id: UUID | str,
        *,
        expected_version: int,
        abandoned_run_id: UUID | str,
        command_id: UUID | str | None = None,
    ) -> TurnState:
        """Typed stale-run requeue: appends turn.stale-run-requeued.v1.

        The projection adapter updates the recoverable/lease index in the same
        append transaction; the coordinator never writes SQL.
        """
        resolved_turn_id = _as_uuid(turn_id, "turn_id")
        expected_version = _expected_version(expected_version)
        resolved_run_id = _as_uuid(abandoned_run_id, "abandoned_run_id")
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        fingerprint = self._fingerprint(
            "requeue_stale_run",
            {
                "turn_id": resolved_turn_id,
                "expected_version": expected_version,
                "abandoned_run_id": resolved_run_id,
            },
        )
        if receipt := self._committed_receipt(
            resolved_command_id, fingerprint, _turn_stream(resolved_turn_id)
        ):
            return self._turn_from_receipt(resolved_turn_id, receipt)
        turn = self._get_turn_at_version(resolved_turn_id, expected_version)
        if turn.status is not TurnStatus.RUNNING:
            raise InvalidTransition(f"cannot requeue a turn in {turn.status.value}")
        if turn.current_run_id != resolved_run_id:
            raise InvalidTransition("stale requeue run mismatch")
        return self._append_turn_event(
            turn,
            resolved_command_id,
            fingerprint,
            TURN_STALE_RUN_REQUEUED,
            {
                "abandoned_run_id": str(resolved_run_id),
                "reason": "lease_expired",
            },
            event_slot="turn-stale-run-requeued",
            run_id=resolved_run_id,
        )
    def wait_for_input(
        self,
        turn_id: UUID | str,
        prompt: str,
        *,
        expected_version: int,
        run_id: UUID | str,
        interrupt_id: UUID | str | None = None,
        command_id: UUID | str | None = None,
    ) -> TurnState:
        """让当前 Run 持久化进入“等待用户输入”，并记录可恢复的 interrupt。"""
        return self._wait(
            turn_id,
            prompt,
            TURN_WAITING_FOR_INPUT,
            expected_version=expected_version,
            run_id=run_id,
            interrupt_id=interrupt_id,
            approval_request_id=None,
            command_id=command_id,
        )

    def wait_for_approval(
        self,
        turn_id: UUID | str,
        prompt: str,
        *,
        expected_version: int,
        run_id: UUID | str,
        interrupt_id: UUID | str | None = None,
        approval_request_id: UUID | str | None = None,
        command_id: UUID | str | None = None,
    ) -> TurnState:
        """持久化审批等待；带 request ID 的新请求只能由 ApprovalService 结算。"""
        return self._wait(
            turn_id,
            prompt,
            TURN_WAITING_FOR_APPROVAL,
            expected_version=expected_version,
            run_id=run_id,
            interrupt_id=interrupt_id,
            approval_request_id=approval_request_id,
            command_id=command_id,
        )

    def pause_turn(
        self,
        turn_id: UUID | str,
        expected_version: int,
        reason: str = "operator requested pause",
        *,
        run_id: UUID | str | None = None,
        command_id: UUID | str | None = None,
    ) -> TurnState:
        """暂停 QUEUED/RUNNING Turn；暂停运行中 Turn 时必须通过 run fencing。"""
        resolved_turn_id = _as_uuid(turn_id, "turn_id")
        expected_version = _expected_version(expected_version)
        canonical_reason = self._canonical_required(
            reason,
            self._text_policy.terminal_text_max_utf8_bytes,
            "reason",
        )
        resolved_run_id = _optional_uuid(run_id)
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        fingerprint = self._fingerprint(
            "pause_turn",
            {
                "turn_id": resolved_turn_id,
                "expected_version": expected_version,
                "run_id": resolved_run_id,
                "reason": canonical_reason.value,
                **_canonical_args(canonical_reason),
            },
        )
        if receipt := self._committed_receipt(
            resolved_command_id,
            fingerprint,
            _turn_stream(resolved_turn_id),
        ):
            return self._turn_from_receipt(resolved_turn_id, receipt)

        turn = self._get_turn_at_version(resolved_turn_id, expected_version)
        if turn.status not in (TurnStatus.QUEUED, TurnStatus.RUNNING):
            raise InvalidTransition(f"cannot pause a turn in {turn.status.value}")
        # expected_version 防“旧决定”，run_id 再防“旧执行者”：旧 Worker 即使
        # 重新读取了最新版本，也不能冒充恢复后产生的新 Run。
        if turn.status is TurnStatus.RUNNING:
            _check_run(turn, resolved_run_id)
        elif resolved_run_id is not None:
            raise InvalidTransition("a queued turn has no active run")
        return self._append_turn_event(
            turn,
            resolved_command_id,
            fingerprint,
            TURN_PAUSED,
            {"reason": canonical_reason.value},
            event_slot="turn-paused",
            run_id=resolved_run_id,
        )

    def request_resume(
        self,
        turn_id: UUID | str,
        expected_version: int,
        *,
        interrupt_id: UUID | str | None = None,
        response: str | bool | None = None,
        command_id: UUID | str | None = None,
    ) -> TurnState:
        """确认挂起原因并把 Turn 重新排队，而不是直接伪造 RUNNING 状态。

        等待输入/审批的 Turn 必须用匹配的 interrupt_id 应答；普通 PAUSED Turn
        不接收 interrupt 响应。成功后追加 recovery-queued，后续 ``start_turn``
        会创建新的 run_id 与 attempt，这就是可审计的 Resume 边界。
        """
        resolved_turn_id = _as_uuid(turn_id, "turn_id")
        expected_version = _expected_version(expected_version)
        resolved_interrupt_id = _optional_uuid(interrupt_id)
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        canonical_response: CanonicalText | None = None
        if isinstance(response, str):
            canonical_response = self._canonical_required(
                response,
                self._text_policy.resume_interrupt_max_utf8_bytes,
                "response",
            )
        fingerprint = self._fingerprint(
            "request_resume",
            {
                "turn_id": resolved_turn_id,
                "expected_version": expected_version,
                "interrupt_id": resolved_interrupt_id,
                "response": (
                    canonical_response.value
                    if canonical_response is not None
                    else response
                ),
                **(  # type: ignore[misc]
                    _canonical_args(canonical_response)
                    if canonical_response is not None
                    else {}
                ),
            },
        )
        if receipt := self._committed_receipt(
            resolved_command_id,
            fingerprint,
            _turn_stream(resolved_turn_id),
        ):
            return self._turn_from_receipt(resolved_turn_id, receipt)

        turn = self._get_turn_at_version(resolved_turn_id, expected_version)
        if turn.status not in RESUMABLE_TURN_STATUSES:
            raise InvalidTransition(f"cannot resume a turn in {turn.status.value}")
        if (
            turn.pending_interrupt is not None
            and turn.pending_interrupt.approval_request_id is not None
        ):
            raise InvalidTransition(
                "durable approval interrupts must be resolved by ApprovalService"
            )
        # interrupt_id 把响应绑定到“当前正在等的那个问题”，避免迟到的旧响应
        # 在下一次等待中被误消费。
        if turn.pending_interrupt is not None:
            if resolved_interrupt_id != turn.pending_interrupt.interrupt_id:
                raise InvalidTransition("resume interrupt does not match pending interrupt")
            if turn.status is TurnStatus.WAITING_FOR_INPUT:
                if not isinstance(response, str) or not response.strip():
                    raise InvalidTransition(
                        "an input interrupt requires a non-empty text response"
                    )
            elif not isinstance(response, bool):
                raise InvalidTransition(
                    "an approval interrupt requires a boolean response"
                )
        elif resolved_interrupt_id is not None:
            raise InvalidTransition("a paused turn has no interrupt to acknowledge")
        elif response is not None:
            raise InvalidTransition("a paused turn has no interrupt response")

        return self._append_turn_event(
            turn,
            resolved_command_id,
            fingerprint,
            TURN_RECOVERY_QUEUED,
            {
                "interrupt_id": (
                    str(resolved_interrupt_id)
                    if resolved_interrupt_id is not None
                    else None
                ),
                "response": (
                    canonical_response.value
                    if canonical_response is not None
                    else response
                ),
            },
            event_slot="turn-recovery-queued",
            run_id=turn.current_run_id,
        )

    def complete_turn(
        self,
        turn_id: UUID | str,
        summary: str,
        *,
        expected_version: int,
        run_id: UUID | str,
        command_id: UUID | str | None = None,
    ) -> TurnState:
        """由当前合法 Worker 完成 Turn，并在同一事务中释放 Thread。"""
        return self._worker_terminate(
            turn_id,
            summary,
            value_name="summary",
            expected_version=expected_version,
            run_id=run_id,
            command_id=command_id,
            terminal_status=TurnStatus.COMPLETED,
            event_type=TURN_COMPLETED,
        )

    def fail_turn(
        self,
        turn_id: UUID | str,
        error: str,
        *,
        expected_version: int,
        run_id: UUID | str,
        command_id: UUID | str | None = None,
    ) -> TurnState:
        """由当前合法 Worker 标记执行失败，并在同一事务中释放 Thread。"""
        return self._worker_terminate(
            turn_id,
            error,
            value_name="error",
            expected_version=expected_version,
            run_id=run_id,
            command_id=command_id,
            terminal_status=TurnStatus.FAILED,
            event_type=TURN_FAILED,
        )

    def cancel_turn(
        self,
        turn_id: UUID | str,
        reason: str,
        *,
        expected_version: int,
        command_id: UUID | str | None = None,
    ) -> TurnState:
        """由操作方取消非终态 Turn；不要求 Worker run_id。"""
        return self._operator_terminate(
            turn_id,
            reason,
            expected_version=expected_version,
            command_id=command_id,
            terminal_status=TurnStatus.CANCELLED,
            event_type=TURN_CANCELLED,
        )

    def timeout_turn(
        self,
        turn_id: UUID | str,
        reason: str,
        *,
        expected_version: int,
        command_id: UUID | str | None = None,
    ) -> TurnState:
        """由调度/操作方把非终态 Turn 标记为超时。"""
        return self._operator_terminate(
            turn_id,
            reason,
            expected_version=expected_version,
            command_id=command_id,
            terminal_status=TurnStatus.TIMED_OUT,
            event_type=TURN_TIMED_OUT,
        )

    def archive_thread(
        self,
        thread_id: UUID | str,
        *,
        expected_version: int,
        command_id: UUID | str | None = None,
    ) -> ThreadState:
        """归档没有活跃 Turn 的开放 Thread，并使用精确版本防并发写入。"""
        resolved_thread_id = _as_uuid(thread_id, "thread_id")
        expected_version = _expected_version(expected_version)
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        fingerprint = self._fingerprint(
            "archive_thread",
            {
                "thread_id": resolved_thread_id,
                "expected_version": expected_version,
            },
        )
        if receipt := self._committed_receipt(
            resolved_command_id,
            fingerprint,
            _thread_stream(resolved_thread_id),
        ):
            return self._thread_from_receipt(resolved_thread_id, receipt)

        thread = self.get_thread(resolved_thread_id)
        _check_version(
            _thread_stream(resolved_thread_id),
            expected_version,
            thread.version,
        )
        if thread.status is not ThreadStatus.OPEN:
            raise InvalidTransition("thread is already archived")
        if thread.active_turn_id is not None:
            raise InvalidTransition("cannot archive a thread with an active turn")
        event = self._event(
            event_type=THREAD_ARCHIVED,
            payload={},
            occurred_at=_now(),
            command_id=resolved_command_id,
            event_slot="thread-archived",
            thread_id=thread.thread_id,
        )
        receipt = self._append(
            resolved_command_id,
            fingerprint,
            StreamWrite(
                stream_id=_thread_stream(thread.thread_id),
                expected_version=expected_version,
                events=(event,),
            ),
        )
        return self._thread_from_receipt(thread.thread_id, receipt)

    def get_thread(self, thread_id: UUID | str) -> ThreadState:
        """读取完整 Thread 事件流并重放，得到当前领域状态。"""
        resolved_thread_id = _as_uuid(thread_id, "thread_id")
        return rebuild_thread(
            resolved_thread_id,
            self._read_stream(_thread_stream(resolved_thread_id)),
        )

    def get_turn(self, turn_id: UUID | str) -> TurnState:
        """读取完整 Turn 事件流并重放，得到当前领域状态。"""
        resolved_turn_id = _as_uuid(turn_id, "turn_id")
        return rebuild_turn(
            resolved_turn_id,
            self._read_stream(_turn_stream(resolved_turn_id)),
        )

    def _read_stream(
        self,
        stream_id: StreamId,
        *,
        through_version: int | None = None,
    ) -> tuple[Any, ...]:
        """分页读完事件流；可在指定版本处截断以重建某次命令的结果。

        ``through_version`` 不是普通查询优化，而是 command receipt 语义的一部分：
        命令提交后若别的命令立刻又写入，新命令的事件不能混入旧命令的返回值。
        """
        events: list[Any] = []
        after_version = -1
        while True:
            page = self._store.read_stream(
                stream_id,
                after_version=after_version,
                limit=500,
            )
            if not page:
                return tuple(events)
            for event in page:
                if through_version is not None and event.stream_version > through_version:
                    return tuple(events)
                events.append(event)
            after_version = page[-1].stream_version
            if len(page) < 500:
                return tuple(events)

    def _wait(
        self,
        turn_id: UUID | str,
        prompt: str,
        event_type: str,
        *,
        expected_version: int,
        run_id: UUID | str,
        interrupt_id: UUID | str | None,
        approval_request_id: UUID | str | None,
        command_id: UUID | str | None,
    ) -> TurnState:
        """实现两种等待命令的共享流程，并持久化 interrupt 身份与提示。"""
        resolved_turn_id = _as_uuid(turn_id, "turn_id")
        expected_version = _expected_version(expected_version)
        canonical_prompt = self._canonical_required(
            prompt,
            self._text_policy.terminal_text_max_utf8_bytes,
            "prompt",
        )
        resolved_run_id = _as_uuid(run_id, "run_id")
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        resolved_interrupt_id = _optional_uuid(interrupt_id) or _derived_id(
            resolved_command_id,
            f"interrupt:{resolved_turn_id}",
        )
        resolved_approval_request_id = _optional_uuid(approval_request_id)
        if event_type != TURN_WAITING_FOR_APPROVAL and resolved_approval_request_id:
            raise ValueError(
                "approval_request_id is only valid for an approval interrupt"
            )
        action = (
            "wait_for_input"
            if event_type == TURN_WAITING_FOR_INPUT
            else "wait_for_approval"
        )
        fingerprint_arguments = {
            "turn_id": resolved_turn_id,
            "expected_version": expected_version,
            "run_id": resolved_run_id,
            "interrupt_id": resolved_interrupt_id,
            "prompt": canonical_prompt.value,
            **_canonical_args(canonical_prompt),
        }
        event_payload = {
            "interrupt_id": str(resolved_interrupt_id),
            "prompt": canonical_prompt.value,
        }
        # None 时保持 D1 的原始指纹与 payload，升级后重试 legacy command_id
        # 仍会命中原有幂等回执；只有 durable approval 才增加新字段。
        if resolved_approval_request_id is not None:
            fingerprint_arguments["approval_request_id"] = resolved_approval_request_id
            event_payload["approval_request_id"] = str(resolved_approval_request_id)
        fingerprint = self._fingerprint(action, fingerprint_arguments)
        if receipt := self._committed_receipt(
            resolved_command_id,
            fingerprint,
            _turn_stream(resolved_turn_id),
        ):
            return self._turn_from_receipt(resolved_turn_id, receipt)

        # 先用精确版本确认决策所依据的状态，再校验执行者 run_id；两道栅栏
        # 分别解决并发状态变更与恢复后的旧 Worker 写入。
        turn = self._get_turn_at_version(resolved_turn_id, expected_version)
        if turn.status is not TurnStatus.RUNNING:
            raise InvalidTransition(f"cannot interrupt a turn in {turn.status.value}")
        _check_run(turn, resolved_run_id)
        return self._append_turn_event(
            turn,
            resolved_command_id,
            fingerprint,
            event_type,
            event_payload,
            event_slot=action.replace("_", "-"),
            run_id=resolved_run_id,
        )

    def _worker_terminate(
        self,
        turn_id: UUID | str,
        value: str,
        *,
        value_name: str,
        expected_version: int,
        run_id: UUID | str,
        command_id: UUID | str | None,
        terminal_status: TurnStatus,
        event_type: str,
    ) -> TurnState:
        """Worker 终止路径：只允许当前 RUNNING 且 run_id 匹配的执行者。"""
        resolved_turn_id = _as_uuid(turn_id, "turn_id")
        expected_version = _expected_version(expected_version)
        resolved_run_id = _as_uuid(run_id, "run_id")
        canonical_value = self._canonical_required(
            value,
            self._text_policy.terminal_text_max_utf8_bytes,
            value_name,
        )
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        action = f"{terminal_status.value}_turn"
        fingerprint = self._fingerprint(
            action,
            {
                "turn_id": resolved_turn_id,
                "expected_version": expected_version,
                "run_id": resolved_run_id,
                value_name: canonical_value.value,
                **_canonical_args(canonical_value),
            },
        )
        if receipt := self._committed_receipt(
            resolved_command_id,
            fingerprint,
            _turn_stream(resolved_turn_id),
        ):
            return self._turn_from_receipt(resolved_turn_id, receipt)

        turn = self._get_turn_at_version(resolved_turn_id, expected_version)
        if turn.status is not TurnStatus.RUNNING:
            raise InvalidTransition(
                f"cannot {terminal_status.value} a turn in {turn.status.value}"
            )
        _check_run(turn, resolved_run_id)
        return self._terminate_turn(
            turn,
            resolved_command_id,
            fingerprint,
            terminal_status,
            event_type,
            {value_name: canonical_value.value},
            event_run_id=resolved_run_id,
        )

    def _operator_terminate(
        self,
        turn_id: UUID | str,
        reason: str,
        *,
        expected_version: int,
        command_id: UUID | str | None,
        terminal_status: TurnStatus,
        event_type: str,
    ) -> TurnState:
        """操作方终止路径：可结束任意非终态 Turn，不冒充某个 Worker Run。"""
        resolved_turn_id = _as_uuid(turn_id, "turn_id")
        expected_version = _expected_version(expected_version)
        canonical_reason = self._canonical_required(
            reason,
            self._text_policy.terminal_text_max_utf8_bytes,
            "reason",
        )
        resolved_command_id = _optional_uuid(command_id) or uuid4()
        fingerprint = self._fingerprint(
            f"{terminal_status.value}_turn",
            {
                "turn_id": resolved_turn_id,
                "expected_version": expected_version,
                "reason": canonical_reason.value,
                **_canonical_args(canonical_reason),
            },
        )
        if receipt := self._committed_receipt(
            resolved_command_id,
            fingerprint,
            _turn_stream(resolved_turn_id),
        ):
            return self._turn_from_receipt(resolved_turn_id, receipt)

        turn = self._get_turn_at_version(resolved_turn_id, expected_version)
        if turn.is_terminal:
            raise InvalidTransition(
                f"cannot {terminal_status.value} a turn in {turn.status.value}"
            )
        return self._terminate_turn(
            turn,
            resolved_command_id,
            fingerprint,
            terminal_status,
            event_type,
            {"reason": canonical_reason.value},
            event_run_id=None,
        )

    def _terminate_turn(
        self,
        turn: TurnState,
        command_id: UUID,
        fingerprint: str,
        terminal_status: TurnStatus,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        event_run_id: UUID | None,
    ) -> TurnState:
        """原子写入 Turn 终态事件与 Thread 脱离事件。

        终态若只写 Turn，Thread 会永远残留 active_turn_id；若只释放 Thread，
        同一 Thread 又可能启动新任务而旧 Turn 仍在运行。因此两条流必须共享事务。
        """
        thread = self.get_thread(turn.thread_id)
        if thread.active_turn_id != turn.turn_id:
            raise InvalidTransition(
                f"thread active turn is {thread.active_turn_id}, not {turn.turn_id}"
            )
        occurred_at = _now()
        terminal_event = self._event(
            event_type=event_type,
            payload=payload,
            occurred_at=occurred_at,
            command_id=command_id,
            event_slot=f"turn-{terminal_status.value}",
            thread_id=thread.thread_id,
            turn_id=turn.turn_id,
            run_id=event_run_id,
        )
        detached_event = self._event(
            event_type=THREAD_TURN_DETACHED,
            payload={
                "turn_id": str(turn.turn_id),
                "terminal_status": terminal_status.value,
            },
            occurred_at=occurred_at,
            command_id=command_id,
            event_slot="thread-turn-detached",
            thread_id=thread.thread_id,
            turn_id=turn.turn_id,
            run_id=event_run_id,
        )
        # EventStore 会先同时核对两条流的 expected_version；任意一条过期，整个
        # batch 回滚，从而维持“Turn 终止 ⇔ Thread 已释放”的跨聚合不变量。
        receipt = self._append(
            command_id,
            fingerprint,
            StreamWrite(
                stream_id=_turn_stream(turn.turn_id),
                expected_version=turn.version,
                events=(terminal_event,),
            ),
            StreamWrite(
                stream_id=_thread_stream(thread.thread_id),
                expected_version=thread.version,
                events=(detached_event,),
            ),
        )
        return self._turn_from_receipt(turn.turn_id, receipt)

    def _append_turn_event(
        self,
        turn: TurnState,
        command_id: UUID,
        fingerprint: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        event_slot: str,
        run_id: UUID | None,
    ) -> TurnState:
        """追加一个仅影响 Turn 的事件，并按回执版本重建该命令的结果。"""
        event = self._event(
            event_type=event_type,
            payload=payload,
            occurred_at=_now(),
            command_id=command_id,
            event_slot=event_slot,
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            run_id=run_id,
        )
        receipt = self._append(
            command_id,
            fingerprint,
            StreamWrite(
                stream_id=_turn_stream(turn.turn_id),
                expected_version=turn.version,
                events=(event,),
            ),
        )
        return self._turn_from_receipt(turn.turn_id, receipt)

    def _get_turn_at_version(self, turn_id: UUID, expected_version: int) -> TurnState:
        """重建 Turn 并执行精确版本校验，拒绝基于旧快照作出的命令。"""
        turn = self.get_turn(turn_id)
        _check_version(_turn_stream(turn_id), expected_version, turn.version)
        return turn

    def _canonical_required(
        self,
        value: Any,
        max_utf8_bytes: int,
        name: str,
    ) -> CanonicalText:
        """Canonicalize one free-text entry before fingerprint/event formation.

        Emptiness is decided by strip() only; the canonical value itself keeps
        its surrounding whitespace (contract §6.3).
        """
        try:
            canon = canonicalize_text(value, max_utf8_bytes, name=name)
        except CanonicalTextError:
            raise
        if not canon.value.strip():
            raise ValueError(f"{name} must be non-empty text")
        return canon
    def _committed_receipt(
        self,
        command_id: UUID,
        fingerprint: str,
        result_stream: StreamId,
    ) -> AppendReceipt | None:
        """查询已提交命令回执，并确认它确实包含本命令应返回的流。

        receipt 是持久化幂等事实，不是内存缓存。相同 command_id 与语义指纹
        可以跨进程重启返回首次结果；指纹不一致则由存储层报幂等冲突。
        """
        receipt = self._store.read_idempotency(
            command_id,
            request_fingerprint=fingerprint,
        )
        if receipt is None:
            return None
        _require_result_stream(receipt, result_stream)
        return receipt

    def _thread_from_receipt(
        self,
        thread_id: UUID,
        receipt: AppendReceipt,
    ) -> ThreadState:
        """只重放到 receipt 记录的 Thread 版本，返回本命令提交时的状态。"""
        stream = _thread_stream(thread_id)
        version = _receipt_version(receipt, stream)
        return rebuild_thread(
            thread_id,
            self._read_stream(stream, through_version=version),
        )

    def _turn_from_receipt(
        self,
        turn_id: UUID,
        receipt: AppendReceipt,
    ) -> TurnState:
        """只重放到 receipt 记录的 Turn 版本，隔离随后并发写入的“未来状态”。"""
        stream = _turn_stream(turn_id)
        version = _receipt_version(receipt, stream)
        return rebuild_turn(
            turn_id,
            self._read_stream(stream, through_version=version),
        )

    def _fingerprint(self, action: str, arguments: Mapping[str, Any]) -> str:
        """把命令语义规范化为稳定 JSON，供幂等键检测“同键是否同命令”。"""
        document = {
            "action": action,
            "arguments": _semantic_json(arguments),
        }
        return json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def _event(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        occurred_at: datetime,
        command_id: UUID,
        event_slot: str,
        thread_id: UUID | None = None,
        turn_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> NewEvent:
        """构造领域事件，并从 command_id + event_slot 确定性派生 event_id。

        一条命令可能生成多个事件，所以每个事件使用不同 slot；同一命令重试时
        slot 不变，event_id 也不变，既便于去重，也不会把多个事件混为一个 ID。
        """
        return NewEvent(
            event_id=_derived_id(command_id, f"event:{event_slot}"),
            event_type=event_type,
            schema_version=int(event_type.rsplit(".v", 1)[1]),
            occurred_at=occurred_at,
            payload=dict(payload),
            metadata=EventMetadata(
                command_id=command_id,
                correlation_id=command_id,
                thread_id=thread_id,
                turn_id=turn_id,
                run_id=run_id,
                actor=self._actor,
            ),
        )

    def _append(
        self,
        command_id: UUID,
        fingerprint: str,
        *writes: StreamWrite,
    ) -> AppendReceipt:
        """把一个命令产生的一个或多个流写入作为原子批次交给 EventStore。"""
        return self._store.append_batch(
            writes,
            idempotency_key=command_id,
            request_fingerprint=fingerprint,
        )


def _thread_stream(thread_id: UUID) -> StreamId:
    """把 Thread 聚合 ID 映射成稳定的事件流 ID。"""
    return StreamId("thread", thread_id)


def _turn_stream(turn_id: UUID) -> StreamId:
    """把 Turn 聚合 ID 映射成稳定的事件流 ID。"""
    return StreamId("turn", turn_id)


def _execution_stream(turn_id: UUID) -> StreamId:
    """Map a Turn to its append-only D6 execution-fact stream."""

    return StreamId("run-execution", turn_id)


def _now() -> datetime:
    """生成带 UTC 时区的领域事件时间，避免跨进程/容器的本地时区歧义。"""
    return datetime.now(timezone.utc)


def _as_uuid(value: UUID | str, name: str) -> UUID:
    """把外部字符串边界统一收敛为 UUID，并提供面向参数名的错误信息。"""
    try:
        return value if isinstance(value, UUID) else UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def _optional_uuid(value: UUID | str | None) -> UUID | None:
    """解析可选 UUID；None 表示由 Runtime 生成或该字段不适用。"""
    return None if value is None else _as_uuid(value, "identifier")


def _expected_version(value: int, name: str = "expected_version") -> int:
    """校验更新命令携带的精确流版本；创建流使用的 -1 不从此入口传入。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be an exact integer >= 0")
    return value


def _check_version(stream_id: StreamId, expected: int, actual: int) -> None:
    """执行乐观并发检查，拒绝基于旧聚合快照作出的决定。"""
    if expected != actual:
        raise WrongExpectedVersion(stream_id, expected, actual)


def _check_run(turn: TurnState, supplied_run_id: UUID | None) -> None:
    """执行 Run fencing，阻止暂停/恢复前的旧 Worker 继续提交结果。"""
    if supplied_run_id is None or turn.current_run_id != supplied_run_id:
        raise InvalidTransition(
            f"run fence rejected {supplied_run_id}; current run is {turn.current_run_id}"
        )


def _require_result_stream(receipt: AppendReceipt, expected: StreamId) -> None:
    """防御损坏回执：幂等结果必须包含调用方约定的返回聚合流。"""
    if not any(item.stream_id == expected for item in receipt.streams):
        raise RuntimeError(
            f"corrupt idempotency receipt {receipt.idempotency_key}: "
            f"missing result stream {expected}"
        )


def _receipt_version(receipt: AppendReceipt, stream_id: StreamId) -> int:
    """取得本命令在目标流提交到的末版本，供精确截断重放。"""
    for item in receipt.streams:
        if item.stream_id == stream_id:
            return item.last_version
    raise RuntimeError(
        f"corrupt idempotency receipt {receipt.idempotency_key}: "
        f"missing result stream {stream_id}"
    )


def _derived_id(command_id: UUID, slot: str) -> UUID:
    """用 UUIDv5 从命令与用途槽位派生可重复、互不混淆的稳定 ID。"""
    return uuid5(NAMESPACE_URL, f"koawa-agent-v2:{command_id}:{slot}")


def _semantic_json(value: Any) -> Any:
    """递归规范化命令参数，使语义相同的输入产生相同幂等指纹。"""
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _semantic_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_semantic_json(item) for item in value]
    raise TypeError(f"unsupported command argument type: {type(value).__name__}")


def _canonical_args(canon: CanonicalText) -> dict[str, object]:
    """Canonical text metadata for the request fingerprint.

    The fingerprint carries the canonical (redacted) value plus digest/byte/
    policy/count metadata — never the pre-canonical raw text (contract §2.5,
    §6.3).
    """
    return {
        "utf8_bytes": canon.utf8_bytes,
        "digest": canon.digest,
        "redaction_policy_version": canon.redaction_policy_version,
        "redaction_count": canon.redaction_count,
    }


def _non_empty(value: str, name: str) -> str:
    """校验领域命令中必须存在的文本，同时保留原始文本内容。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _now_text(value: datetime) -> str:
    # Six-microsecond UTC ISO text for typed lease payloads.
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
