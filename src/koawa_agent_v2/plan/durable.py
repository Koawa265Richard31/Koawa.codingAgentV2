"""Durable plan journal: ``memory.plan-updated.v1`` on a thread-keyed stream.

每次计划变更一个事件（全量快照），以精确版本 CAS 追加；回读取最后一条
有效快照。响应丢失后的重试会产生新 command 并在 CAS 上失败——此时按
write-ahead 合同板面保持原状，由调用方决定是否重放。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5

from ..control.event_store import EventMetadata, NewEvent, StreamId, StreamWrite
from .board import PlanError, PlanItem

PLAN_EVENT_TYPE = "memory.plan-updated.v1"
PLAN_SCHEMA_VERSION = 1

_PLAN_STREAM_PAGE = 500


class PlanDurableJournal:
    """把 PlanBoard 的 on_change 钩子接到线程键控的持久事件流。"""

    def __init__(self, event_store: object, thread_id: UUID) -> None:
        if not hasattr(event_store, "append_batch") or not hasattr(
            event_store, "read_stream"
        ):
            raise PlanError("plan_journal_store_invalid")
        if not isinstance(thread_id, UUID):
            raise PlanError("plan_journal_thread_invalid")
        self._store = event_store
        self._thread_id = thread_id

    def stream(self) -> StreamId:
        return StreamId("memory", self._thread_id)

    def _events(self):
        cursor = -1
        while True:
            page = self._store.read_stream(
                self.stream(), after_version=cursor, limit=_PLAN_STREAM_PAGE
            )
            yield from page
            if len(page) < _PLAN_STREAM_PAGE:
                return
            cursor = page[-1].stream_version

    def current_version(self) -> int:
        last = -1
        for event in self._events():
            last = event.stream_version
        return last

    def append(self, items: Sequence[PlanItem]) -> None:
        command = uuid4()
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "items": [
                {"item_id": item.item_id, "text": item.text, "status": item.status}
                for item in items
            ],
        }
        event = NewEvent(
            uuid5(command, "event:" + PLAN_EVENT_TYPE),
            PLAN_EVENT_TYPE,
            PLAN_SCHEMA_VERSION,
            datetime.now(timezone.utc),
            payload,
            EventMetadata(command, command, thread_id=self._thread_id, actor="runtime"),
        )
        write = StreamWrite(
            stream_id=self.stream(),
            expected_version=self.current_version(),
            events=(event,),
        )
        self._store.append_batch((write,), idempotency_key=command)

    def load(self) -> tuple[PlanItem, ...]:
        latest = None
        for event in self._events():
            if event.event_type == PLAN_EVENT_TYPE:
                latest = event
        if latest is None:
            return ()
        try:
            raw = latest.payload["items"]
            # The event store deep-freezes payloads: JSON lists come back as
            # tuples on read, so both shapes are accepted here.
            if not isinstance(raw, (list, tuple)) or not raw:
                raise ValueError("items must be a non-empty sequence")
            items = tuple(
                PlanItem(
                    int(entry["item_id"]),
                    str(entry["text"]),
                    str(entry["status"]),
                )
                for entry in raw
            )
        except (KeyError, TypeError, ValueError, PlanError):
            raise PlanError("plan_stream_corrupt") from None
        return items
