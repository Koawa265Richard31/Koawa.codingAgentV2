from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

from koawa_agent_v2.control.event_store import (
    DuplicateEventId,
    EventMetadata,
    EventStoreError,
    IdempotencyConflict,
    InvalidEvent,
    NewEvent,
    StreamId,
    StreamPrecondition,
    StreamWrite,
    WrongExpectedVersion,
)
from koawa_agent_v2.control.sqlite_store import SqliteEventStore


class SqliteEventStoreTest(unittest.TestCase):
    """验证 EventStore 的事务、并发、幂等和序列化契约。"""
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "events.sqlite3"
        self.store = SqliteEventStore(self.database_path)
        self.now = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)

    def event(
        self,
        command_id: UUID,
        payload: dict[str, object] | None = None,
        *,
        event_id: UUID | None = None,
        event_type: str = "test.recorded.v1",
    ) -> NewEvent:
        """创建符合存储协议的测试事件，减少各用例中的无关样板代码。"""
        return NewEvent(
            event_id=event_id or uuid4(),
            event_type=event_type,
            schema_version=1,
            occurred_at=self.now,
            payload=payload or {"message": "你好, agent"},
            metadata=EventMetadata(
                command_id=command_id,
                correlation_id=command_id,
                actor="test",
            ),
        )

    def test_append_replay_pagination_and_process_style_restart(self) -> None:
        """事件分页不重不漏，并能由新的 Store 实例继续读取。"""
        stream = StreamId("turn", uuid4())
        key = uuid4()
        events = tuple(self.event(key, {"index": index}) for index in range(3))
        receipt = self.store.append_batch(
            (StreamWrite(stream, -1, events),),
            idempotency_key=key,
        )

        self.assertEqual(0, receipt.streams[0].first_version)
        self.assertEqual(2, receipt.streams[0].last_version)
        first_page = self.store.read_stream(stream, limit=2)
        second_page = self.store.read_stream(
            stream,
            after_version=first_page[-1].stream_version,
            limit=2,
        )
        replay = first_page + second_page
        self.assertEqual([0, 1, 2], [event.stream_version for event in replay])
        self.assertEqual([0, 1, 2], [event.payload["index"] for event in replay])
        self.assertTrue(all(event.occurred_at.tzinfo is not None for event in replay))

        restarted = SqliteEventStore(self.database_path)
        self.assertEqual(replay, restarted.read_stream(stream))
        global_page = restarted.read_all(limit=2)
        global_tail = restarted.read_all(
            after_position=global_page[-1].global_position,
            limit=2,
        )
        self.assertEqual(replay, global_page + global_tail)

    def test_stale_exact_version_does_not_append(self) -> None:
        """基于旧版本作出的决定必须失败，且不能产生半条事件。"""
        stream = StreamId("thread", uuid4())
        first_key = uuid4()
        self.store.append_batch(
            (StreamWrite(stream, -1, (self.event(first_key),)),),
            idempotency_key=first_key,
        )

        stale_key = uuid4()
        with self.assertRaises(WrongExpectedVersion) as raised:
            self.store.append_batch(
                (StreamWrite(stream, -1, (self.event(stale_key),)),),
                idempotency_key=stale_key,
            )

        self.assertEqual(-1, raised.exception.expected)
        self.assertEqual(0, raised.exception.actual)
        self.assertEqual(1, len(self.store.read_stream(stream)))

    def test_identical_append_retry_returns_original_receipt(self) -> None:
        """完全相同的底层追加重试返回首次回执，不重复落库。"""
        stream = StreamId("thread", uuid4())
        key = uuid4()
        write = StreamWrite(stream, -1, (self.event(key),))

        first = self.store.append_batch((write,), idempotency_key=key)
        retried = self.store.append_batch((write,), idempotency_key=key)

        self.assertEqual(first, retried)
        self.assertEqual(1, len(self.store.read_stream(stream)))

    def test_semantic_fingerprint_supports_regenerated_event_envelopes(self) -> None:
        """事件时间/ID可重新生成；语义相同的命令仍能命中原回执。"""
        stream = StreamId("thread", uuid4())
        key = uuid4()
        first = StreamWrite(stream, -1, (self.event(key, {"value": 1}),))
        receipt = self.store.append_batch(
            (first,),
            idempotency_key=key,
            request_fingerprint='{"action":"create"}',
        )
        regenerated = StreamWrite(
            stream,
            -1,
            (self.event(key, {"value": 1}),),
        )

        retried = self.store.append_batch(
            (regenerated,),
            idempotency_key=key,
            request_fingerprint='{"action":"create"}',
        )
        self.assertEqual(receipt, retried)
        self.assertEqual(
            receipt,
            self.store.read_idempotency(
                key,
                request_fingerprint='{"action":"create"}',
            ),
        )

    def test_idempotency_key_reuse_with_different_request_is_rejected(self) -> None:
        """同一幂等键不能被两个不同语义的请求占用。"""
        key = uuid4()
        first_stream = StreamId("thread", uuid4())
        self.store.append_batch(
            (StreamWrite(first_stream, -1, (self.event(key, {"value": 1}),)),),
            idempotency_key=key,
            request_fingerprint="first",
        )

        conflicting_stream = StreamId("thread", uuid4())
        with self.assertRaises(IdempotencyConflict):
            self.store.append_batch(
                (
                    StreamWrite(
                        conflicting_stream,
                        -1,
                        (self.event(key, {"value": 2}),),
                    ),
                ),
                idempotency_key=key,
                request_fingerprint="different",
            )
        self.assertEqual((), self.store.read_stream(conflicting_stream))

    def test_stale_member_rolls_back_entire_multi_stream_batch(self) -> None:
        """批次中任意流版本过期，其他流也不能写入。"""
        existing = StreamId("thread", uuid4())
        untouched = StreamId("turn", uuid4())
        first_key = uuid4()
        self.store.append_batch(
            (StreamWrite(existing, -1, (self.event(first_key),)),),
            idempotency_key=first_key,
        )

        batch_key = uuid4()
        with self.assertRaises(WrongExpectedVersion):
            self.store.append_batch(
                (
                    StreamWrite(untouched, -1, (self.event(batch_key),)),
                    StreamWrite(existing, -1, (self.event(batch_key),)),
                ),
                idempotency_key=batch_key,
            )

        self.assertEqual((), self.store.read_stream(untouched))
        self.assertEqual(1, len(self.store.read_stream(existing)))

    def test_duplicate_event_id_rolls_back_other_streams(self) -> None:
        """全局重复 event_id 会令整个多流事务回滚。"""
        original_stream = StreamId("thread", uuid4())
        duplicate_id = uuid4()
        original_key = uuid4()
        self.store.append_batch(
            (
                StreamWrite(
                    original_stream,
                    -1,
                    (self.event(original_key, event_id=duplicate_id),),
                ),
            ),
            idempotency_key=original_key,
        )
        fresh_a = StreamId("agent", uuid4())
        fresh_b = StreamId("turn", uuid4())
        batch_key = uuid4()

        with self.assertRaises(DuplicateEventId):
            self.store.append_batch(
                (
                    StreamWrite(fresh_a, -1, (self.event(batch_key),)),
                    StreamWrite(
                        fresh_b,
                        -1,
                        (self.event(batch_key, event_id=duplicate_id),),
                    ),
                ),
                idempotency_key=batch_key,
            )

        self.assertEqual((), self.store.read_stream(fresh_a))
        self.assertEqual((), self.store.read_stream(fresh_b))
        self.assertEqual(1, len(self.store.read_stream(original_stream)))

    def test_injected_mid_batch_database_failure_rolls_back_and_can_retry(self) -> None:
        """在第二条流注入数据库失败，证明第一条流不会残留且可安全重试。"""
        first = StreamId("alpha", uuid4())
        second = StreamId("omega", uuid4())
        key = uuid4()
        writes = (
            StreamWrite(first, -1, (self.event(key, {"step": 1}),)),
            StreamWrite(
                second,
                -1,
                (
                    self.event(
                        key,
                        {"step": 2},
                        event_type="test.injected-failure.v1",
                    ),
                ),
            ),
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """
                CREATE TRIGGER fail_injected_event
                BEFORE INSERT ON events
                WHEN NEW.event_type = 'test.injected-failure.v1'
                BEGIN
                    SELECT RAISE(ABORT, 'injected write failure');
                END
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(EventStoreError):
            self.store.append_batch(writes, idempotency_key=key)
        self.assertEqual((), self.store.read_stream(first))
        self.assertEqual((), self.store.read_stream(second))

        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("DROP TRIGGER fail_injected_event")
            connection.commit()
        finally:
            connection.close()
        receipt = self.store.append_batch(writes, idempotency_key=key)
        self.assertEqual(2, len(receipt.streams))

    def test_commit_boundary_is_visible_even_when_global_page_splits_it(self) -> None:
        """分页切开原子批次时，消费者仍可用 commit 元数据识别完整边界。"""
        key = uuid4()
        writes = (
            StreamWrite(
                StreamId("thread", uuid4()),
                -1,
                (self.event(key, {"aggregate": "thread"}),),
            ),
            StreamWrite(
                StreamId("turn", uuid4()),
                -1,
                (self.event(key, {"aggregate": "turn"}),),
            ),
        )
        self.store.append_batch(writes, idempotency_key=key)

        first_page = self.store.read_all(limit=1)
        second_page = self.store.read_all(
            after_position=first_page[-1].global_position,
            limit=1,
        )
        commit = first_page + second_page
        self.assertEqual({key}, {event.commit_id for event in commit})
        self.assertEqual([0, 1], [event.commit_index for event in commit])
        self.assertEqual([2, 2], [event.commit_size for event in commit])

    def test_payload_is_snapshotted_as_immutable_json(self) -> None:
        """调用者事后修改原字典不会改变事件中的不可变 JSON 快照。"""
        key = uuid4()
        original = {"nested": {"values": [1, 2]}}
        event = self.event(key, original)
        original["nested"]["values"].append(3)
        self.assertIsInstance(event.payload, MappingProxyType)
        with self.assertRaises(TypeError):
            event.payload["new"] = "forbidden"

        stream = StreamId("thread", uuid4())
        self.store.append_batch(
            (StreamWrite(stream, -1, (event,)),),
            idempotency_key=key,
        )
        self.assertEqual((1, 2), self.store.read_stream(stream)[0].payload["nested"]["values"])

    def test_batch_rejects_event_from_a_different_command(self) -> None:
        """一个原子批次内的事件必须属于该批次的 command_id。"""
        with self.assertRaises(InvalidEvent):
            self.store.append_batch(
                (
                    StreamWrite(
                        StreamId("thread", uuid4()),
                        -1,
                        (self.event(uuid4()),),
                    ),
                ),
                idempotency_key=uuid4(),
            )

    def test_twenty_concurrent_creators_have_one_winner(self) -> None:
        """20 个写者争抢空流版本 -1 时只能有一个创建者成功。"""
        stream = StreamId("thread", uuid4())
        stores = [SqliteEventStore(self.database_path) for _ in range(20)]
        barrier = threading.Barrier(20)

        def compete(index: int) -> str:
            key = uuid4()
            barrier.wait(timeout=10)
            try:
                stores[index].append_batch(
                    (
                        StreamWrite(
                            stream,
                            -1,
                            (self.event(key, {"writer": index}),),
                        ),
                    ),
                    idempotency_key=key,
                )
                return "won"
            except WrongExpectedVersion:
                return "stale"

        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(compete, range(20)))

        self.assertEqual(1, results.count("won"))
        self.assertEqual(19, results.count("stale"))
        stored = self.store.read_stream(stream)
        self.assertEqual(1, len(stored))
        self.assertEqual(0, stored[0].stream_version)


    def test_database_time_returns_aware_utc_clock(self) -> None:
        """I2 4.2: database_time() is the backend-authoritative UTC clock."""
        value = self.store.database_time()
        self.assertIsNotNone(value.tzinfo)
        self.assertIsNotNone(value.utcoffset())
        self.assertEqual(0, value.utcoffset().total_seconds())
        self.assertLess(abs((value - datetime.now(timezone.utc)).total_seconds()), 120)

    def test_precondition_failure_rolls_back_writes_and_receipt(self) -> None:
        """I2 4.2: failing precondition writes nothing and leaves no receipt."""
        guard = StreamId("agent", uuid4())
        written = StreamId("mailbox", uuid4())
        guard_key = uuid4()
        self.store.append_batch(
            (StreamWrite(guard, -1, (self.event(guard_key),)),),
            idempotency_key=guard_key,
        )
        key = uuid4()
        with self.assertRaises(WrongExpectedVersion):
            self.store.append_batch(
                (StreamWrite(written, -1, (self.event(key),)),),
                idempotency_key=key,
                preconditions=(
                    StreamPrecondition(guard, 99),
                ),
            )
        self.assertEqual((), self.store.read_stream(written))
        self.assertEqual(1, len(self.store.read_stream(guard)))
        self.assertIsNone(
            self.store.read_idempotency(key, request_fingerprint="fingerprint")
        )

    def test_current_global_position_tracks_high_water(self) -> None:
        """I3 5.3: current_global_position is the committed high-water."""
        self.assertEqual(0, self.store.current_global_position())
        key = uuid4()
        stream = StreamId("agent-capacity", uuid4())
        self.store.append_batch(
            (StreamWrite(stream, -1, (self.event(key, {"n": 1}),)),),
            idempotency_key=key,
        )
        self.assertEqual(1, self.store.current_global_position())
        second_key = uuid4()
        self.store.append_batch(
            (StreamWrite(stream, 0, (self.event(second_key, {"n": 2}),)),),
            idempotency_key=second_key,
        )
        self.assertEqual(2, self.store.current_global_position())

    def test_read_all_through_position_is_an_inclusive_boundary(self) -> None:
        """I3 5.3: after < position <= through keeps the scan snapshot stable."""
        stream = StreamId("agent", uuid4())
        for index in range(3):
            key = uuid4()
            self.store.append_batch(
                (StreamWrite(stream, index - 1, (self.event(key, {"i": index}),)),),
                idempotency_key=key,
            )
        high_water = self.store.current_global_position()
        snapshot = self.store.read_all(
            after_position=0, through_position=high_water
        )
        self.assertEqual([1, 2, 3], [e.global_position for e in snapshot])
        # events committed after the boundary never appear in the snapshot
        key = uuid4()
        self.store.append_batch(
            (StreamWrite(stream, 2, (self.event(key, {"i": 3}),)),),
            idempotency_key=key,
        )
        again = self.store.read_all(after_position=0, through_position=high_water)
        self.assertEqual(snapshot, again)
        self.assertEqual(
            [4],
            [e.global_position for e in self.store.read_all(after_position=high_water)],
        )
        self.assertEqual(4, self.store.current_global_position())

    def test_explicit_fingerprint_keeps_original_receipt_across_regenerated_events(self) -> None:
        """I2 4.2: explicit fingerprint returns original receipt on retry."""
        stream = StreamId("mailbox", uuid4())
        key = uuid4()
        first = self.store.append_batch(
            (StreamWrite(stream, -1, (self.event(key, {"value": 7}),)),),
            idempotency_key=key,
            request_fingerprint='{"operation":"deliver","message":"m1"}',
        )
        saved_now = datetime(2026, 8, 12, 11, 0, tzinfo=timezone.utc)
        self.now = saved_now
        regenerated = self.store.append_batch(
            (StreamWrite(stream, -1, (self.event(key, {"value": 7}),)),),
            idempotency_key=key,
            request_fingerprint='{"operation":"deliver","message":"m1"}',
        )
        self.assertEqual(first, regenerated)
        self.assertEqual(1, len(self.store.read_stream(stream)))


class DurableJsonLimitsTest(unittest.TestCase):
    """I4 6.2: hard durable-JSON boundaries on write and fail-closed reads."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "durable-limits.sqlite3"
        self.store = SqliteEventStore(self.database_path)
        self.now = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)

    def event(
        self,
        command_id: UUID,
        payload: dict[str, object] | None = None,
        *,
        event_id: UUID | None = None,
        event_type: str = "test.recorded.v1",
    ) -> NewEvent:
        return NewEvent(
            event_id=event_id or uuid4(),
            event_type=event_type,
            schema_version=1,
            occurred_at=self.now,
            payload=payload or {"message": "durable limits"},
            metadata=EventMetadata(
                command_id=command_id,
                correlation_id=command_id,
                actor="test",
            ),
        )

    def ingress(self, **overrides: int) -> dict[str, int]:
        from koawa_agent_v2.control.durable_json import INGRESS_DEFAULTS

        values = dict(INGRESS_DEFAULTS)
        values.update(overrides)
        return values

    def test_payload_depth_plus_one_rejected_at_construction(self) -> None:
        """root depth=1; nesting to the limit is accepted, limit+1 is not."""
        from koawa_agent_v2.control.durable_json import (
            DurableJsonLimitExceeded,
            EVENT_PAYLOAD_READ_V1,
        )

        def chain(target_depth: int) -> dict[str, object]:
            """A document whose deepest VALUE sits exactly at target_depth."""
            value: object = 1
            for _ in range(target_depth - 2):
                value = {"next": value}
            return {"root": value}

        at_limit = chain(EVENT_PAYLOAD_READ_V1.max_depth)
        key = uuid4()
        self.store.append_batch(
            (StreamWrite(StreamId("turn", uuid4()), -1, (self.event(key, at_limit),)),),
            idempotency_key=key,
        )
        over_limit = chain(EVENT_PAYLOAD_READ_V1.max_depth + 1)
        with self.assertRaises(DurableJsonLimitExceeded) as raised:
            self.event(uuid4(), over_limit)
        self.assertEqual("depth", raised.exception.code)

    def test_cycle_nan_surrogate_and_non_string_key_never_escape_bare(self) -> None:
        """Malformed payloads fail closed with domain errors, not RecursionError."""
        from koawa_agent_v2.control.durable_json import DurableJsonError
        from koawa_agent_v2.control.event_store import InvalidEvent

        cyclic: dict[str, object] = {}
        cyclic["self"] = cyclic
        with self.assertRaises(InvalidEvent):
            self.event(uuid4(), cyclic)
        with self.assertRaises(InvalidEvent):
            self.event(uuid4(), {"value": float("nan")})
        with self.assertRaises(DurableJsonError):
            self.event(uuid4(), {"value": "\ud800 lone surrogate"})
        with self.assertRaises(InvalidEvent):
            self.event(uuid4(), {1: "non-string key"})

    def test_ingress_node_boundary_accepted_and_plus_one_atomic_rejection(self) -> None:
        """limit succeeds; limit+1 rejects before any stream/head/receipt writes."""
        from koawa_agent_v2.control.durable_json import DurableJsonLimitExceeded

        store = SqliteEventStore(
            self.database_path,
            durable_limits=self.ingress(event_payload_max_nodes=64),
        )
        stream = StreamId("turn", uuid4())
        # root object + values array + 62 ints = 64 nodes -> boundary accepted.
        at_limit = {"values": list(range(62))}
        key = uuid4()
        store.append_batch(
            (StreamWrite(stream, -1, (self.event(key, at_limit),)),),
            idempotency_key=key,
        )
        self.assertEqual(1, len(store.read_stream(stream)))
        # root object + values array + 63 ints = 65 nodes -> limit+1 rejected.
        over_limit = {"values": list(range(63))}
        rejected_key = uuid4()
        with self.assertRaises(DurableJsonLimitExceeded):
            store.append_batch(
                (StreamWrite(stream, 0, (self.event(rejected_key, over_limit),)),),
                idempotency_key=rejected_key,
            )
        self.assertEqual(1, len(store.read_stream(stream)))
        self.assertIsNone(
            store.read_idempotency(rejected_key, request_fingerprint="fp"),
        )

    def test_multi_stream_batch_with_any_over_limit_payload_is_atomic(self) -> None:
        """One over-limit payload rolls back the whole batch: 0 events/heads."""
        from koawa_agent_v2.control.durable_json import DurableJsonLimitExceeded

        store = SqliteEventStore(
            self.database_path,
            durable_limits=self.ingress(event_payload_max_nodes=64),
        )
        fine = StreamId("mailbox", uuid4())
        bad = StreamId("agent", uuid4())
        key = uuid4()
        with self.assertRaises(DurableJsonLimitExceeded):
            store.append_batch(
                (
                    StreamWrite(fine, -1, (self.event(key, {"ok": True}),)),
                    StreamWrite(
                        bad,
                        -1,
                        (self.event(key, {"values": list(range(63))}),),
                    ),
                ),
                idempotency_key=key,
            )
        self.assertEqual((), store.read_stream(fine))
        self.assertEqual((), store.read_stream(bad))
        self.assertIsNone(store.read_idempotency(key, request_fingerprint="fp"))

    def test_member_item_and_key_limits_boundary_and_plus_one(self) -> None:
        """Object members, array items, key bytes and string bytes caps."""
        from koawa_agent_v2.control.durable_json import DurableJsonLimitExceeded

        store = SqliteEventStore(
            self.database_path,
            durable_limits=self.ingress(
                event_payload_max_object_members=16,
                event_payload_max_array_items=16,
                event_payload_max_key_utf8_bytes=32,
                event_payload_max_string_utf8_bytes=256,
            ),
        )
        key = uuid4()
        store.append_batch(
            (StreamWrite(StreamId("turn", uuid4()), -1, (self.event(key, {f"k{i}": i for i in range(16)}),)),),
            idempotency_key=key,
        )
        with self.assertRaises(DurableJsonLimitExceeded):
            over_key = uuid4()
            store.append_batch(
                (
                    StreamWrite(
                        StreamId("turn", uuid4()),
                        -1,
                        (self.event(over_key, {f"k{i}": i for i in range(17)}),),
                    ),
                ),
                idempotency_key=over_key,
            )
        key = uuid4()
        store.append_batch(
            (StreamWrite(StreamId("turn", uuid4()), -1, (self.event(key, {"a": list(range(16))}),)),),
            idempotency_key=key,
        )
        with self.assertRaises(DurableJsonLimitExceeded):
            over_key = uuid4()
            store.append_batch(
                (
                    StreamWrite(
                        StreamId("turn", uuid4()),
                        -1,
                        (self.event(over_key, {"a": list(range(17))}),),
                    ),
                ),
                idempotency_key=over_key,
            )
        with self.assertRaises(DurableJsonLimitExceeded):
            over_key = uuid4()
            store.append_batch(
                (
                    StreamWrite(
                        StreamId("turn", uuid4()),
                        -1,
                        (self.event(over_key, {"a" * 33: 1}),),
                    ),
                ),
                idempotency_key=over_key,
            )
        with self.assertRaises(DurableJsonLimitExceeded):
            self.event(uuid4(), {"text": "x" * 2_097_153})

    def test_event_store_never_rewrites_business_payload(self) -> None:
        """The store validates and rejects; credential-shaped text stays as-is."""
        stream = StreamId("turn", uuid4())
        original = {
            "secret": "sk-abc1234567890xyz",
            "nested": {"key": "Bearer abcdefghijklmnop"},
        }
        key = uuid4()
        self.store.append_batch(
            (StreamWrite(stream, -1, (self.event(key, original),)),),
            idempotency_key=key,
        )
        stored = self.store.read_stream(stream)[0].payload
        self.assertEqual(original, dict(stored))

    def test_corrupt_oversized_payload_fails_whole_page_closed(self) -> None:
        """An oversized stored payload fails the page before decoding."""
        stream = StreamId("turn", uuid4())
        for index in range(2):
            key = uuid4()
            self.store.append_batch(
                (StreamWrite(stream, index - 1, (self.event(key, {"i": index}),)),),
                idempotency_key=key,
            )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE global_position = 1",
                ("x" * 5_000_000,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(EventStoreError):
            self.store.read_stream(stream)
        with self.assertRaises(EventStoreError):
            self.store.read_all()

    def test_corrupt_bad_utf8_and_nested_duplicate_payload_fail_closed(self) -> None:
        """Bad UTF-8 and nested duplicate keys are rejected before decode/parse."""
        stream = StreamId("turn", uuid4())
        key = uuid4()
        self.store.append_batch(
            (StreamWrite(stream, -1, (self.event(key, {"i": 0}),)),),
            idempotency_key=key,
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE global_position = 1",
                (b"\xff\xfe\x00\x01 not utf8",),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(EventStoreError):
            self.store.read_stream(stream)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE global_position = 1",
                ('{"a": {"b": 1, "b": 2}}',),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(EventStoreError):
            self.store.read_stream(stream)

    def test_corrupt_metadata_and_receipt_fail_closed(self) -> None:
        """Oversized metadata/receipt fail before decoding."""
        stream = StreamId("turn", uuid4())
        key = uuid4()
        self.store.append_batch(
            (StreamWrite(stream, -1, (self.event(key, {"i": 0}),)),),
            idempotency_key=key,
            request_fingerprint="fp",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE events SET metadata_json = ? WHERE global_position = 1",
                ("y" * 20_000,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(EventStoreError):
            self.store.read_stream(stream)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE idempotency_keys SET receipt_json = ?",
                ("z" * 2_000_000,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(EventStoreError):
            self.store.read_idempotency(key, request_fingerprint="fp")

    def test_ingress_shrink_old_events_replay_new_writes_rejected(self) -> None:
        """Smaller runtime ingress keeps reading old events, rejects new ones."""
        from koawa_agent_v2.control.durable_json import DurableJsonLimitExceeded

        stream = StreamId("turn", uuid4())
        key = uuid4()
        self.store.append_batch(
            (StreamWrite(stream, -1, (self.event(key, {"values": list(range(100))}),)),),
            idempotency_key=key,
        )
        restarted = SqliteEventStore(
            self.database_path,
            durable_limits=self.ingress(event_payload_max_nodes=64),
        )
        replayed = restarted.read_stream(stream)
        self.assertEqual(1, len(replayed))
        self.assertEqual(tuple(range(100)), replayed[0].payload["values"])
        with self.assertRaises(DurableJsonLimitExceeded):
            over_key = uuid4()
            restarted.append_batch(
                (StreamWrite(stream, 0, (self.event(over_key, {"values": list(range(63))}),)),),
                idempotency_key=over_key,
            )


if __name__ == "__main__":
    unittest.main()

