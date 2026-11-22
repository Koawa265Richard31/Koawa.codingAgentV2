"""agents 控制面共享的命令写入辅助(单点实现)。

事件构造(_event / _correlation)、请求指纹(_fingerprint)、故障事实(_facts)、
幂等回执读取(_read_receipt)与 CAS 重试上限(MAX_CAS_RETRIES)只在此一处实现;
agents/control.py 与 agents/message_service.py 仅导入使用,不复制逻辑。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from ..control.event_store import (
    EventMetadata,
    IdempotencyConflict,
    NewEvent,
    StreamId,
)


MAX_CAS_RETRIES = 3

# facts passed to fault hooks never carry task text, result bodies or
# credentials; they only hold stable ids/versions/attempts.
FaultInjector = Callable[[str, Mapping[str, object]], None]


def _no_faults(point: str, facts: Mapping[str, object]) -> None:
    return None


NO_FAULTS: FaultInjector = _no_faults


def _correlation(command_id: UUID) -> UUID:
    return uuid5(command_id, "correlation")


def _fingerprint(document: Mapping) -> str:
    return json.dumps(
        dict(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _facts(
    agent_id: UUID,
    attempt: int,
    *,
    message_ids: tuple[UUID, ...] = (),
    delivery_attempt: int | None = None,
    version: int | None = None,
) -> dict[str, object]:
    facts: dict[str, object] = {"agent_id": str(agent_id), "attempt": attempt}
    if message_ids:
        facts["message_ids"] = [str(item) for item in message_ids]
    if delivery_attempt is not None:
        facts["delivery_attempt"] = delivery_attempt
    if version is not None:
        facts["version"] = version
    return facts


def _read_receipt(event_store, command_id: UUID, fingerprint: str):
    """Return the persisted receipt, or the string sentinel "committed"
    when the same command was already committed under a different semantic
    fingerprint (IdempotencyConflict). Callers treat both as idempotent
    success; record_message_result maps the sentinel to
    message_result_identity_conflict.
    """

    read_idempotency = getattr(event_store, "read_idempotency", None)
    if read_idempotency is None:
        return None
    try:
        return read_idempotency(command_id, request_fingerprint=fingerprint)
    except IdempotencyConflict:
        return "committed"


def _event(
    command_id: UUID,
    slot: str,
    event_type: str,
    payload: Mapping,
    *,
    occurred_at: datetime,
    correlation_id: UUID,
    turn_id: UUID | None = None,
    run_id: UUID | None = None,
) -> NewEvent:
    return NewEvent(
        uuid5(command_id, "event:" + slot),
        event_type,
        int(event_type.rsplit(".v", 1)[1]),
        occurred_at,
        dict(payload),
        EventMetadata(
            command_id,
            correlation_id,
            thread_id=None,
            turn_id=turn_id,
            run_id=run_id,
            actor="agent-control",
        ),
    )


def _read_all(event_store, stream: StreamId) -> tuple:
    """按 stream head 分页读全量事件;控制面与消息服务共用此实现。"""

    values = []
    cursor = -1
    while True:
        page = event_store.read_stream(stream, after_version=cursor, limit=500)
        values.extend(page)
        if len(page) < 500:
            return tuple(values)
        cursor = page[-1].stream_version
