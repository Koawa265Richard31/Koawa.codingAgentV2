"""基于 SQLite 的 append-only Event Store 实现。

这个模块只负责可靠保存和读取事件，不负责解释 Thread/Turn 的业务状态。
写路径最重要的保证是：精确版本校验、多事件流原子提交和命令幂等回执。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import UUID

from .event_store import (
    AppendReceipt,
    DuplicateEventId,
    EventMetadata,
    EventStoreError,
    IdempotencyConflict,
    InvalidEvent,
    NewEvent,
    StoredEvent,
    StreamAppendReceipt,
    StreamId,
    StreamPrecondition,
    StreamWrite,
    WrongExpectedVersion,
)
from .durable_json import (
    DurableJsonError,
    EVENT_METADATA_READ_V1,
    EVENT_PAYLOAD_READ_V1,
    IDEMPOTENCY_RECEIPT_READ_V1,
    effective_payload_limits,
    strict_json_loads_bytes,
    validate_json_value,
    validate_runtime_ingress,
)


class SqliteEventStore:
    """文件型 Event Store：支持精确版本控制和多流原子追加。

    每次公开操作都新建一个连接，因此对象可被多个线程共享；真正的写并发
    由 SQLite 事务协调。SQLite 适合本地单机 Agent，后续若换成 PostgreSQL，
    上层仍可依赖 ``EventStore`` 协议而不改领域逻辑。
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 10_000,
        durable_limits: Mapping[str, int] | None = None,
    ) -> None:
        """绑定数据库文件并初始化表结构。

        durable_limits is the I4 exact-key runtime ingress policy: absent means
        section 6.2 defaults, an object must contain all eleven keys.  Writes
        are rejected at min(EVENT_PAYLOAD_READ_V1, ingress); historical reads
        always use the immutable protocol profile.

        这里禁止 ``:memory:``，因为本实现每个操作使用独立连接；SQLite 的普通
        内存库属于单个连接，换连接后数据就不再是同一个库，也无法验证重启恢复。
        ``busy_timeout_ms`` 表示遇到其他写事务时最多等待多久，而不是网络超时。
        """
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        path = Path(database_path)
        if str(path) == ":memory:":
            raise ValueError("use a file path; per-operation connections cannot use :memory:")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._database_path = str(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._durable_limits = validate_runtime_ingress(durable_limits)
        self._payload_write_limits = effective_payload_limits(self._durable_limits)
        self._initialize()

    @property
    def durable_limits(self) -> dict[str, int]:
        """Return the normalized exact-key runtime ingress policy."""
        return dict(self._durable_limits)

    @property
    def database_path(self) -> Path:
        """返回当前 Event Store 使用的数据库文件路径。"""

        return Path(self._database_path)

    def database_time(self) -> datetime:
        """Return the SQLite database-authoritative UTC clock.

        The value comes from strftime('%Y-%m-%dT%H:%M:%fZ','now') evaluated
        inside SQLite itself, so all processes sharing one database observe a
        single clock even if host wall clocks drift.
        """

        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            ).fetchone()
            if row is None or not row[0]:
                raise EventStoreError("database clock unavailable")
            value = row[0]
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith("Z") else value
            )
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except sqlite3.Error as exc:
            raise EventStoreError(f"SQLite clock failure: {exc}") from exc
        finally:
            connection.close()

    def append_batch(
        self,
        writes: Sequence[StreamWrite],
        *,
        idempotency_key: UUID,
        request_fingerprint: str | None = None,
        preconditions: Sequence[StreamPrecondition] = (),
    ) -> AppendReceipt:
        """把一个命令产生的多个流写入作为一个事务原子追加。

        ``expected_version`` 必须与每条流的当前版本完全相等；因此调用者基于旧
        状态作出的决定会被拒绝。``idempotency_key`` 标识业务命令，同一个键和
        同一个语义指纹重试时返回首次提交的回执，换参数复用该键则报冲突。

        返回的 ``AppendReceipt`` 精确记录本命令写出的版本和全局位置。上层可以
        重放到这些位置，避免提交之后的并发事件混进当前命令的返回结果。
        """

        # 阶段 1：在开启事务前完成纯内存校验和规范化，减少持有写锁的时间。
        resolved_key = _require_uuid(idempotency_key, "idempotency_key")
        supplied_writes = tuple(writes)
        if not supplied_writes:
            raise ValueError("append_batch requires at least one stream write")
        if not all(isinstance(write, StreamWrite) for write in supplied_writes):
            raise TypeError("writes must contain only StreamWrite values")
        normalized_writes = tuple(
            sorted(supplied_writes, key=lambda write: write.stream_id.key)
        )
        resolved_preconditions = tuple(preconditions)
        if not all(isinstance(item, StreamPrecondition) for item in resolved_preconditions):
            raise TypeError("preconditions must contain only StreamPrecondition values")

        # 排序让同一批流始终按稳定顺序处理。SQLite 当前是数据库级单写者，
        # 但稳定顺序也让指纹、回执和未来迁移到行锁数据库后的锁顺序可预测。
        stream_keys = [write.stream_id.key for write in normalized_writes]
        if len(stream_keys) != len(set(stream_keys)):
            raise ValueError("a batch may contain each stream at most once")

        event_ids = [event.event_id for write in normalized_writes for event in write.events]
        duplicate_in_batch = _first_duplicate(event_ids)
        if duplicate_in_batch is not None:
            raise DuplicateEventId(duplicate_in_batch)
        for write in normalized_writes:
            for event in write.events:
                if event.metadata.command_id != resolved_key:
                    raise InvalidEvent(
                        "every event command_id must equal the batch idempotency_key"
                    )

                

        # 阶段 2：把调用者拥有的 Mapping/Sequence 快照一次。默认幂等指纹和实际
        # 入库都使用同一份规范文档，避免调用者在两者之间修改可变 payload。
        write_documents = [_write_document(write) for write in normalized_writes]
        # I4: EventStore validates and rejects; it never rewrites business data.
        # Payload limits are min(EVENT_PAYLOAD_READ_V1, runtime ingress); metadata
        # uses the fixed EVENT_METADATA_READ_V1 profile.  Any limit+1 must fail
        # BEFORE the write transaction opens, so no stream, head or receipt can
        # be partially committed.
        for write_document in write_documents:
            for event_document in write_document["events"]:
                validate_json_value(
                    event_document["payload"],
                    self._payload_write_limits,
                    path="payload",
                )
                validate_json_value(
                    event_document["metadata"],
                    EVENT_METADATA_READ_V1,
                    path="metadata",
                )
        request_document = {
            "writes": write_documents,
            "preconditions": [
                {"stream": item.stream_id.key, "expected_version": item.expected_version,
                 "required_event_type": item.required_event_type,
                 "required_payload": _json_object(item.required_payload)}
                for item in resolved_preconditions
            ],
        }
        fingerprint = (
            _canonical_json(request_document)
            if request_fingerprint is None
            else _require_fingerprint(request_fingerprint)
        )
        request_hash = _fingerprint_hash(fingerprint)

        connection = self._connect()
        try:
            # 阶段 3：BEGIN IMMEDIATE 在事务一开始就申请写保留锁。WAL 允许既有
            # 读者继续读，但 SQLite 仍只有一个写者；其他写者在 busy_timeout 内
            # 等待。这样“读取当前版本→插入事件→更新流头”全部处于同一写事务，
            # 不会出现两个调用者同时通过版本检查的 TOCTOU 竞态。
            connection.execute("BEGIN IMMEDIATE")

            # 阶段 4：幂等检查必须先于版本检查。若首次提交已经成功、响应却丢失，
            # 重试时流版本早已改变；先返回持久化回执才能把它识别为成功重试。
            existing = connection.execute(
                "SELECT request_hash, receipt_json, "
                "length(CAST(receipt_json AS BLOB)) AS receipt_blob_bytes, "
                "CAST(receipt_json AS BLOB) AS receipt_blob "
                "FROM idempotency_keys WHERE idempotency_key = ?",
                (str(resolved_key),),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict(resolved_key)
                if existing["receipt_blob_bytes"] > IDEMPOTENCY_RECEIPT_READ_V1.max_utf8_bytes:
                    raise EventStoreError(
                        "durable read rejected: idempotency receipt exceeds profile"
                    )
                try:
                    receipt_document = strict_json_loads_bytes(
                        existing["receipt_blob"],
                        IDEMPOTENCY_RECEIPT_READ_V1,
                        path="receipt",
                    )
                except DurableJsonError as exc:
                    raise EventStoreError(
                        "durable read rejected: " + exc.code
                    ) from exc
                receipt = _receipt_from_document(receipt_document)
                connection.commit()
                return receipt

            # 只读 stream fence 与写入共享 BEGIN IMMEDIATE；execution/ledger 事实
            # 因而不能在 Turn 已被接管后迟到提交。
            for condition in resolved_preconditions:
                row = connection.execute(
                    "SELECT current_version FROM streams WHERE stream_id = ?",
                    (condition.stream_id.key,),
                ).fetchone()
                actual = -1 if row is None else int(row["current_version"])
                if actual != condition.expected_version:
                    raise WrongExpectedVersion(condition.stream_id, condition.expected_version, actual)
                if condition.required_event_type is not None:
                    latest = connection.execute(
                        "SELECT event_type, payload_json FROM events WHERE stream_id = ? AND stream_version = ?",
                        (condition.stream_id.key, actual),
                    ).fetchone()
                    if latest is None or latest["event_type"] != condition.required_event_type:
                        raise EventStoreError("stream precondition event type mismatch")
                    payload = json.loads(latest["payload_json"])
                    for key, value in _json_object(condition.required_payload).items():
                        if payload.get(key) != value:
                            raise EventStoreError(f"stream precondition payload mismatch: {key}")

            # 阶段 5：在持有写锁时校验所有流的精确版本。不存在的流视为 -1。
            # 任意一条不匹配都会抛错并回滚，所以不会只写成功批次的一部分。
            actual_versions: dict[str, int] = {}
            d6_projection_tables = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('run_leases','recoverable_turns')"
            ).fetchone()[0] == 2
            for write in normalized_writes:
                row = connection.execute(
                    "SELECT current_version FROM streams WHERE stream_id = ?",
                    (write.stream_id.key,),
                ).fetchone()
                actual = -1 if row is None else int(row["current_version"])
                actual_versions[write.stream_id.key] = actual
                if actual != write.expected_version:
                    raise WrongExpectedVersion(
                        write.stream_id,
                        write.expected_version,
                        actual,
                    )

            # 这里的显式查询用于给调用者一个领域化 DuplicateEventId；events 表的
            # UNIQUE(event_id) 仍是最终防线，防止任何遗漏或并发造成重复事实。
            for event_id in event_ids:
                exists = connection.execute(
                    "SELECT 1 FROM events WHERE event_id = ?",
                    (str(event_id),),
                ).fetchone()
                if exists is not None:
                    raise DuplicateEventId(event_id)

            # 阶段 6：同一原子提交共用 recorded_at。commit_id/index/size 标记事务
            # 边界；全局订阅者即便分页只读到一部分，也能知道该 commit 尚未读全。
            recorded_at = datetime.now(timezone.utc)
            receipts: list[StreamAppendReceipt] = []
            commit_size = sum(len(write.events) for write in normalized_writes)
            commit_index = 0
            for write, write_document in zip(
                normalized_writes,
                write_documents,
                strict=True,
            ):
                # 新流先建立 head=-1，再从版本 0 写第一条事件。外键确保事件不会
                # 指向不存在的流，随后在同一事务中把 head 推进到最终版本。
                if actual_versions[write.stream_id.key] == -1:
                    connection.execute(
                        "INSERT INTO streams "
                        "(stream_id, category, aggregate_id, current_version) "
                        "VALUES (?, ?, ?, -1)",
                        (
                            write.stream_id.key,
                            write.stream_id.category,
                            str(write.stream_id.aggregate_id),
                        ),
                    )

                first_version = write.expected_version + 1
                positions: list[int] = []
                for offset, event_document in enumerate(write_document["events"]):
                    stream_version = first_version + offset
                    # AUTOINCREMENT 生成全库单调递增的 global_position；它与单条流
                    # 内连续的 stream_version 分工：前者服务全局消费，后者服务聚合重放。
                    cursor = connection.execute(
                        "INSERT INTO events "
                        "(event_id, stream_id, stream_version, commit_id, "
                        "commit_index, commit_size, event_type, "
                        "schema_version, occurred_at, recorded_at, payload_json, "
                        "metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            event_document["event_id"],
                            write.stream_id.key,
                            stream_version,
                            str(resolved_key),
                            commit_index,
                            commit_size,
                            event_document["event_type"],
                            event_document["schema_version"],
                            event_document["occurred_at"],
                            _datetime_text(recorded_at),
                            _canonical_json(event_document["payload"]),
                            _canonical_json(event_document["metadata"]),
                        ),
                    )
                    positions.append(int(cursor.lastrowid))
                    if d6_projection_tables and write.stream_id.category == "turn":
                        turn_id = str(write.stream_id.aggregate_id)
                        run_id = event_document["metadata"].get("run_id")
                        thread_id = event_document["metadata"].get("thread_id")
                        if event_document["event_type"] == "turn.started.v1" and run_id and thread_id:
                            db_now = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
                            payload = event_document["payload"]
                            owner_id = payload.get("lease_owner_id") or "__bootstrap__"
                            ttl_seconds = payload.get("lease_seconds")
                            if isinstance(ttl_seconds, int) and not isinstance(ttl_seconds, bool) and ttl_seconds > 0:
                                expires_at = connection.execute(
                                    "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now',?)",
                                    (f"+{ttl_seconds} seconds",),
                                ).fetchone()[0]
                                initial_generation = 1
                            else:
                                expires_at = db_now
                                initial_generation = 0
                            connection.execute(
                                "INSERT INTO run_leases(turn_id,run_id,owner_id,generation,version,expires_at) VALUES(?,?,?,?,0,?) "
                                "ON CONFLICT(turn_id) DO UPDATE SET run_id=excluded.run_id,owner_id=excluded.owner_id,generation=run_leases.generation+1,version=0,expires_at=excluded.expires_at",
                                (turn_id, run_id, owner_id, initial_generation, expires_at),
                            )
                            connection.execute(
                                "INSERT INTO recoverable_turns(turn_id,thread_id,run_id,turn_version,phase,automatic,updated_at) VALUES(?,?,?,?, 'ready_for_model',1,?) "
                                "ON CONFLICT(turn_id) DO UPDATE SET thread_id=excluded.thread_id,run_id=excluded.run_id,turn_version=excluded.turn_version,phase=excluded.phase,automatic=1,updated_at=excluded.updated_at",
                                (turn_id, thread_id, run_id, stream_version, db_now),
                            )
                        elif event_document["event_type"] in (
                            "turn.waiting-for-input.v1",
                            "turn.waiting-for-approval.v1",
                            "turn.paused.v1",
                        ):
                            connection.execute("DELETE FROM run_leases WHERE turn_id=?", (turn_id,))
                            connection.execute("DELETE FROM recoverable_turns WHERE turn_id=?", (turn_id,))
                        elif event_document["event_type"] == "turn.recovery-queued.v1" and run_id and thread_id:
                            db_now = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
                            connection.execute("DELETE FROM run_leases WHERE turn_id=?", (turn_id,))
                            connection.execute(
                                "INSERT INTO recoverable_turns(turn_id,thread_id,run_id,turn_version,phase,automatic,updated_at) VALUES(?,?,?,?, 'ready_for_model',1,?) "
                                "ON CONFLICT(turn_id) DO UPDATE SET thread_id=excluded.thread_id,run_id=excluded.run_id,turn_version=excluded.turn_version,phase=excluded.phase,automatic=1,updated_at=excluded.updated_at",
                                (turn_id, thread_id, run_id, stream_version, db_now),
                            )
                        elif event_document["event_type"] in ("turn.completed.v1", "turn.failed.v1", "turn.cancelled.v1", "turn.timed-out.v1"):
                            connection.execute("DELETE FROM run_leases WHERE turn_id=?", (turn_id,))
                            connection.execute("DELETE FROM recoverable_turns WHERE turn_id=?", (turn_id,))
                    commit_index += 1

                last_version = first_version + len(write.events) - 1
                # 流头是并发校验的快速索引；历史真相仍在 append-only events 表中。
                connection.execute(
                    "UPDATE streams SET current_version = ? WHERE stream_id = ?",
                    (last_version, write.stream_id.key),
                )
                receipts.append(
                    StreamAppendReceipt(
                        stream_id=write.stream_id,
                        first_version=first_version,
                        last_version=last_version,
                        global_positions=tuple(positions),
                    )
                )

            # 阶段 7：把结果回执与业务事件放进同一事务。只有事件和 receipt 都落库
            # 后才 COMMIT；因此重试不会遇到“事件成功但没有幂等证明”的中间状态。
            receipt = AppendReceipt(resolved_key, tuple(receipts))
            connection.execute(
                "INSERT INTO idempotency_keys "
                "(idempotency_key, request_hash, receipt_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    str(resolved_key),
                    request_hash,
                    _canonical_json(_receipt_document(receipt)),
                    _datetime_text(recorded_at),
                ),
            )
            connection.commit()
            return receipt
        except EventStoreError:
            # 领域化错误保持原类型，上层可区分版本冲突、幂等冲突和重复事件。
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            # 数据库约束是最后一道一致性防线；统一包装，避免泄漏 sqlite3 异常类型。
            connection.rollback()
            raise EventStoreError(f"SQLite integrity failure: {exc}") from exc
        except sqlite3.Error as exc:
            # BEGIN 后的任意 SQLite 故障都必须显式回滚整个多流批次。
            connection.rollback()
            raise EventStoreError(f"SQLite write failure: {exc}") from exc
        except Exception:
            # 编程错误也不能留下半开事务；回滚后保留原异常，便于定位代码问题。
            connection.rollback()
            raise
        finally:
            # 每个操作独占一个短连接，关闭连接同时释放相关数据库资源和锁。
            connection.close()

    def read_idempotency(
        self,
        idempotency_key: UUID,
        *,
        request_fingerprint: str,
    ) -> AppendReceipt | None:
        """按命令键和语义指纹读取已提交回执。

        返回 ``None`` 表示该命令从未成功提交；键存在但指纹不同表示调用者错误地
        复用了命令 ID。Runtime 可在做状态迁移校验前调用它，实现跨重启幂等。
        """

        resolved_key = _require_uuid(idempotency_key, "idempotency_key")
        request_hash = _fingerprint_hash(_require_fingerprint(request_fingerprint))
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT request_hash, receipt_json, "
                "length(CAST(receipt_json AS BLOB)) AS receipt_blob_bytes, "
                "CAST(receipt_json AS BLOB) AS receipt_blob "
                "FROM idempotency_keys WHERE idempotency_key = ?",
                (str(resolved_key),),
            ).fetchone()
            if row is None:
                return None
            if row["request_hash"] != request_hash:
                raise IdempotencyConflict(resolved_key)
            if row["receipt_blob_bytes"] > IDEMPOTENCY_RECEIPT_READ_V1.max_utf8_bytes:
                raise EventStoreError(
                    "durable read rejected: idempotency receipt exceeds profile"
                )
            try:
                document = strict_json_loads_bytes(
                    row["receipt_blob"],
                    IDEMPOTENCY_RECEIPT_READ_V1,
                    path="receipt",
                )
            except DurableJsonError as exc:
                raise EventStoreError(
                    "durable read rejected: " + exc.code
                ) from exc
            return _receipt_from_document(document)
        except EventStoreError:
            raise
        except sqlite3.Error as exc:
            raise EventStoreError(f"SQLite read failure: {exc}") from exc
        finally:
            connection.close()

    def read_stream(
        self,
        stream_id: StreamId,
        *,
        after_version: int = -1,
        limit: int = 500,
    ) -> tuple[StoredEvent, ...]:
        """按流内版本升序读取某个聚合的事件页。

        ``after_version`` 是排他游标；传 -1 会从 v0 开始。Thread/Turn 的状态重建
        使用这个接口，而不是读取一行可变的当前状态。
        """

        if not isinstance(stream_id, StreamId):
            raise TypeError("stream_id must be StreamId")
        _validate_page(after_version, limit, cursor_name="after_version", minimum=-1)
        connection = self._connect()
        try:
            rows = connection.execute(
                _EVENT_SELECT
                + " WHERE events.stream_id = ? AND events.stream_version > ?"
                + " ORDER BY events.stream_version ASC LIMIT ?",
                (stream_id.key, after_version, limit),
            ).fetchall()
            return tuple(_stored_event(row) for row in rows)
        except sqlite3.Error as exc:
            raise EventStoreError(f"SQLite read failure: {exc}") from exc
        finally:
            connection.close()

    def current_global_position(self) -> int:
        """Return the highest committed global position (0 for an empty log).

        One read statement computes the high-water so projections can pin a
        scan boundary; the append-only log never changes underneath it.
        """

        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COALESCE(MAX(global_position), 0) AS high_water FROM events"
            ).fetchone()
            if row is None:
                raise EventStoreError("global position unavailable")
            return int(row["high_water"])
        except sqlite3.Error as exc:
            raise EventStoreError(f"SQLite read failure: {exc}") from exc
        finally:
            connection.close()

    def read_all(
        self,
        *,
        after_position: int = 0,
        through_position: int | None = None,
        limit: int = 500,
    ) -> tuple[StoredEvent, ...]:
        """按全局位置升序读取整个事件库的一页。

        ``after_position`` 同样是排他游标，主要服务投影、审计、Trace 和监控；
        单个聚合的恢复应优先使用 ``read_stream``。
        ``through_position`` 是包含上界：只返回
        ``after < global_position <= through`` 的事件，与合同 §5.3 的
        high-water 扫描边界一致。
        """

        _validate_page(after_position, limit, cursor_name="after_position", minimum=0)
        if through_position is not None:
            if (
                not isinstance(through_position, int)
                or isinstance(through_position, bool)
                or through_position < 0
            ):
                raise ValueError("through_position must be an integer >= 0")
        connection = self._connect()
        try:
            statement = (
                _EVENT_SELECT
                + " WHERE events.global_position > ?"
                + (" AND events.global_position <= ?" if through_position is not None else "")
                + " ORDER BY events.global_position ASC LIMIT ?"
            )
            parameters: list[object] = [after_position]
            if through_position is not None:
                parameters.append(through_position)
            parameters.append(limit)
            rows = connection.execute(statement, parameters).fetchall()
            return tuple(_stored_event(row) for row in rows)
        except sqlite3.Error as exc:
            raise EventStoreError(f"SQLite read failure: {exc}") from exc
        finally:
            connection.close()

    def _initialize(self) -> None:
        """创建 Event Store 所需表、约束和索引，并设置持久化策略。

        WAL（预写日志）让读者与写者更好地并存，但不代表允许多个并行写者；
        ``synchronous=FULL`` 要求 SQLite 更谨慎地刷盘，优先保证崩溃后的完整性。
        表定义中的 UNIQUE/CHECK/FOREIGN KEY 是应用校验之外的最后一致性防线。
        """

        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS streams (
                    stream_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    current_version INTEGER NOT NULL CHECK (current_version >= -1)
                );

                CREATE TABLE IF NOT EXISTS events (
                    global_position INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    stream_id TEXT NOT NULL REFERENCES streams(stream_id),
                    stream_version INTEGER NOT NULL CHECK (stream_version >= 0),
                    commit_id TEXT NOT NULL,
                    commit_index INTEGER NOT NULL CHECK (commit_index >= 0),
                    commit_size INTEGER NOT NULL CHECK (commit_size > 0),
                    event_type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
                    occurred_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    UNIQUE (stream_id, stream_version)
                );

                CREATE INDEX IF NOT EXISTS ix_events_stream
                    ON events(stream_id, stream_version);

                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
        except sqlite3.Error as exc:
            raise EventStoreError(f"could not initialize SQLite event store: {exc}") from exc
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        """创建一个配置一致的短生命周期 SQLite 连接。

        ``isolation_level=None`` 开启自动提交模式，使写路径可以显式控制
        ``BEGIN IMMEDIATE/COMMIT/ROLLBACK``；外键、FULL 同步和 busy timeout 必须
        对每个新连接重新启用。``sqlite3.Row`` 让读取代码可按列名访问结果。
        """

        connection = sqlite3.connect(
            self._database_path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection


_EVENT_SELECT = """
SELECT events.global_position, events.event_id, events.stream_version,
       events.commit_id, events.commit_index, events.commit_size,
       events.event_type, events.schema_version, events.occurred_at,
       events.recorded_at, events.payload_json, events.metadata_json,
       length(CAST(events.payload_json AS BLOB)) AS payload_blob_bytes,
       CAST(events.payload_json AS BLOB) AS payload_blob,
       length(CAST(events.metadata_json AS BLOB)) AS metadata_blob_bytes,
       CAST(events.metadata_json AS BLOB) AS metadata_blob,
       streams.category, streams.aggregate_id
FROM events
JOIN streams ON streams.stream_id = events.stream_id
"""


def _stored_event(row: sqlite3.Row) -> StoredEvent:
    """把数据库联表查询的一行反序列化为强类型 ``StoredEvent``。

    UUID、时间、metadata 和 payload 在这里恢复成领域协议使用的类型；构造
    ``StoredEvent`` 时还会再次执行其自身不变量校验，避免静默接受坏数据。
    """

    if row["payload_blob_bytes"] > EVENT_PAYLOAD_READ_V1.max_utf8_bytes:
        raise EventStoreError(
            "durable read rejected: event payload exceeds the read profile"
        )
    if row["metadata_blob_bytes"] > EVENT_METADATA_READ_V1.max_utf8_bytes:
        raise EventStoreError(
            "durable read rejected: event metadata exceeds the read profile"
        )
    try:
        payload = strict_json_loads_bytes(
            row["payload_blob"], EVENT_PAYLOAD_READ_V1, path="payload"
        )
        metadata_document = strict_json_loads_bytes(
            row["metadata_blob"], EVENT_METADATA_READ_V1, path="metadata"
        )
    except DurableJsonError as exc:
        # The whole page fails closed with the domain error type; partial,
        # partially-trusted projection is never returned.
        raise EventStoreError(
            "durable read rejected: " + exc.code
        ) from exc
    _require_read_invariants(row)
    return StoredEvent(
        event_id=UUID(row["event_id"]),
        stream_id=StreamId(row["category"], UUID(row["aggregate_id"])),
        stream_version=int(row["stream_version"]),
        global_position=int(row["global_position"]),
        commit_id=UUID(row["commit_id"]),
        commit_index=int(row["commit_index"]),
        commit_size=int(row["commit_size"]),
        event_type=row["event_type"],
        schema_version=int(row["schema_version"]),
        occurred_at=_parse_datetime(row["occurred_at"]),
        recorded_at=_parse_datetime(row["recorded_at"]),
        payload=payload,
        metadata=_metadata_from_document(metadata_document),
    )


def _require_read_invariants(row: sqlite3.Row) -> None:
    """Verify the stored-event invariants (section 6.2) before trusting the row.

    Checks the event_type schema suffix against schema_version and the commit
    boundary fields; any mismatch means the event log itself is corrupt.
    """
    event_type = row["event_type"]
    suffix = event_type.rsplit(".v", 1)
    if (
        len(suffix) != 2
        or not suffix[1].isdigit()
        or int(suffix[1]) != int(row["schema_version"])
    ):
        raise EventStoreError(
            "durable read rejected: event_type schema suffix mismatch"
        )
    commit_index = int(row["commit_index"])
    commit_size = int(row["commit_size"])
    if commit_size < 1 or not 0 <= commit_index < commit_size:
        raise EventStoreError(
            "durable read rejected: event commit boundary is corrupt"
        )


def _write_document(write: StreamWrite) -> dict[str, Any]:
    """把一条 ``StreamWrite`` 快照成纯 JSON 文档。

    该文档既参与幂等指纹计算，也直接提供入库字段，确保“比较的请求”和
    “真正保存的请求”是同一份值快照，而不是两次读取调用者的可变对象。
    """

    return {
        "stream": {
            "category": write.stream_id.category,
            "aggregate_id": str(write.stream_id.aggregate_id),
        },
        "expected_version": write.expected_version,
        "events": [
            {
                "event_id": str(event.event_id),
                "event_type": event.event_type,
                "schema_version": event.schema_version,
                "occurred_at": _datetime_text(event.occurred_at),
                "payload": _json_object(event.payload),
                "metadata": _metadata_document(event.metadata),
            }
            for event in write.events
        ],
    }


def _metadata_document(metadata: EventMetadata) -> dict[str, Any]:
    """把事件 metadata 序列化为跨进程、跨语言可读的 JSON 对象。"""

    return {
        "command_id": str(metadata.command_id),
        "correlation_id": str(metadata.correlation_id),
        "causation_id": _uuid_text(metadata.causation_id),
        "thread_id": _uuid_text(metadata.thread_id),
        "turn_id": _uuid_text(metadata.turn_id),
        "run_id": _uuid_text(metadata.run_id),
        "actor": metadata.actor,
    }


def _metadata_from_document(document: Mapping[str, Any]) -> EventMetadata:
    """从 JSON 对象恢复 ``EventMetadata``，包括可空 UUID 字段。"""

    return EventMetadata(
        command_id=UUID(document["command_id"]),
        correlation_id=UUID(document["correlation_id"]),
        causation_id=_optional_uuid(document.get("causation_id")),
        thread_id=_optional_uuid(document.get("thread_id")),
        turn_id=_optional_uuid(document.get("turn_id")),
        run_id=_optional_uuid(document.get("run_id")),
        actor=document["actor"],
    )


def _receipt_document(receipt: AppendReceipt) -> dict[str, Any]:
    """把追加回执序列化成可持久化 JSON 文档。

    回执保存每条流本命令写出的版本范围和全局位置，是幂等重试时必须原样
    返回的结果，而不只是一个简单的 success 标志。
    """

    return {
        "idempotency_key": str(receipt.idempotency_key),
        "streams": [
            {
                "category": item.stream_id.category,
                "aggregate_id": str(item.stream_id.aggregate_id),
                "first_version": item.first_version,
                "last_version": item.last_version,
                "global_positions": list(item.global_positions),
            }
            for item in receipt.streams
        ],
    }


def _receipt_from_document(document: Mapping[str, Any]) -> AppendReceipt:
    """把幂等表中的 JSON 文档恢复成强类型追加回执。"""

    return AppendReceipt(
        idempotency_key=UUID(document["idempotency_key"]),
        streams=tuple(
            StreamAppendReceipt(
                stream_id=StreamId(
                    item["category"],
                    UUID(item["aggregate_id"]),
                ),
                first_version=int(item["first_version"]),
                last_version=int(item["last_version"]),
                global_positions=tuple(int(value) for value in item["global_positions"]),
            )
            for item in document["streams"]
        ),
    )


def _json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    """把 payload 复制成普通 JSON 对象，并拒绝非对象顶层值。"""

    normalized = _json_value(value, path="payload")
    if not isinstance(normalized, dict):
        raise InvalidEvent("payload must be a JSON object")
    return normalized


def _json_value(value: Any, *, path: str) -> Any:
    """递归复制并验证一个值是否属于严格 JSON 数据模型。

    ``path`` 只用于生成精确错误位置。这里拒绝非字符串键、NaN/Infinity 和
    Python 专有对象，保证事件可稳定序列化，并能被未来其他语言的实现读取。
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidEvent(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidEvent(f"{path} contains a non-string object key")
            result[key] = _json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise InvalidEvent(f"{path} contains non-JSON value {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    """生成确定性的紧凑 JSON 字符串。

    键排序和固定分隔符让相同语义值产生相同字节表示，供指纹、payload 和
    receipt 共用；``allow_nan=False`` 防止写出标准 JSON 不支持的特殊浮点数。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _require_fingerprint(value: str) -> str:
    """校验 Runtime 提供的语义指纹必须是非空文本。"""

    if not isinstance(value, str) or not value:
        raise ValueError("request_fingerprint must be non-empty text")
    return value


def _fingerprint_hash(value: str) -> str:
    """把语义指纹压缩成固定长度 SHA-256 文本用于索引和比较。

    这里的哈希用于稳定等值比较，不承担签名、鉴权或密码学身份认证。
    """

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _datetime_text(value: datetime) -> str:
    """把带时区时间统一转换成 UTC、微秒精度的 ISO-8601 文本。"""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidEvent("event datetimes must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_datetime(value: str) -> datetime:
    """解析库存时间并统一成 UTC；拒绝缺少时区的历史数据。"""

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EventStoreError("stored datetime is not timezone-aware")
    return parsed.astimezone(timezone.utc)

 
def _uuid_text(value: UUID | None) -> str | None:
    """把可空 UUID 转成 JSON/SQLite 可保存的文本。"""

    return None if value is None else str(value)


def _optional_uuid(value: Any) -> UUID | None:
    """把可空数据库值恢复成 UUID。"""

    return None if value is None else UUID(str(value))


def _require_uuid(value: UUID, name: str) -> UUID:
    """要求公开 API 的指定参数已经是 UUID，而不是自动猜测字符串。"""

    if not isinstance(value, UUID):
        raise TypeError(f"{name} must be a UUID")
    return value


def _first_duplicate(values: Sequence[UUID]) -> UUID | None:
    """返回序列中首个重复 UUID；没有重复时返回 ``None``。"""

    seen: set[UUID] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


def _validate_page(value: int, limit: int, *, cursor_name: str, minimum: int) -> None:
    """统一校验排他分页游标和页大小，避免无界查询及 bool 冒充 int。"""

    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{cursor_name} must be an integer >= {minimum}")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
