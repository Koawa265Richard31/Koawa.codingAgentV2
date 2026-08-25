"""Append-only Event Store 的类型契约。

本文件只定义“Runtime 怎样与事件存储交互”，不包含 SQLite 等具体实现。
核心写入模型是：命令基于某个精确的流版本作出决定，再把一个或多个
``NewEvent`` 原子追加为 ``StoredEvent``。状态不会在这里被覆盖更新，而是由
``models.py`` 重放历史事件得到，这就是 D1 的事件溯源（Event Sourcing）边界。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID


# category 会进入持久化 stream key，稳定的小写格式可避免跨语言命名歧义。
_CATEGORY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
# 事件名本身携带 schema 大版本，例如 ``turn.started.v1``。
_EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9.-]*\.v[1-9][0-9]*$")


class EventStoreError(RuntimeError):
    """事件存储可预期失败的基类，便于 Runtime 统一捕获存储层错误。"""


class InvalidEvent(EventStoreError):
    """事件不满足可持久化 JSON 契约时抛出，而不是等待数据库序列化失败。"""


class WrongExpectedVersion(EventStoreError):
    """命令所依据的聚合版本已过期时抛出，即事件流上的乐观锁冲突。"""

    def __init__(self, stream_id: StreamId, expected: int, actual: int) -> None:
        """保留流、期望版本和实际版本，供上层判断是否需要重新读取再决策。"""

        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"wrong expected version for {stream_id}: "
            f"expected {expected}, actual {actual}"
        )


class IdempotencyConflict(EventStoreError):
    """同一个幂等键被用于不同请求内容时抛出，通常表示调用方错误。"""

    def __init__(self, idempotency_key: UUID) -> None:
        """记录发生语义冲突的命令幂等键。"""

        self.idempotency_key = idempotency_key
        super().__init__(
            f"idempotency key {idempotency_key} was already used by another request"
        )


class DuplicateEventId(EventStoreError):
    """event_id 已存在于全局事件日志时抛出。"""

    def __init__(self, event_id: UUID) -> None:
        """记录重复的事件标识；一个 command 可以产生多个不同 event_id。"""

        self.event_id = event_id
        super().__init__(f"event id {event_id} already exists")


@dataclass(frozen=True, slots=True)
class StreamId:
    """一条聚合事件流的稳定身份，例如 ``turn-<uuid>``。"""

    # 聚合类型，如 thread/turn；它不是模型 token 的“流式输出”。
    category: str
    # 具体聚合实例的 ID；category 与此字段共同确定唯一事件流。
    aggregate_id: UUID

    def __post_init__(self) -> None:
        """构造时阻止不稳定 category 和非 UUID 聚合 ID 进入存储协议。"""

        if not isinstance(self.category, str) or not _CATEGORY_PATTERN.fullmatch(
            self.category
        ):
            raise ValueError(
                "stream category must match ^[a-z][a-z0-9_-]*$"
            )
        if not isinstance(self.aggregate_id, UUID):
            raise TypeError("aggregate_id must be a UUID")

    @property
    def key(self) -> str:
        """返回数据库使用的规范化 stream key。"""

        return f"{self.category}-{self.aggregate_id}"

    def __str__(self) -> str:
        """日志和异常中直接显示可定位的 stream key。"""

        return self.key


@dataclass(frozen=True, slots=True)
class EventMetadata:
    """事件的审计/链路元数据；业务状态本身应放在 payload 中。"""

    # 触发这批状态变化的命令 ID；也通常作为持久化幂等键。
    command_id: UUID
    # 串联一次用户任务下多个命令、工具调用和子 Agent 的业务链路。
    correlation_id: UUID
    # 以下 ID 用于把事件定位到会话、任务和某一次 Worker 执行尝试。
    thread_id: UUID | None = None
    turn_id: UUID | None = None
    run_id: UUID | None = None
    # 审计标签（runtime/user/worker 等），D1 不把它当认证后的安全身份。
    actor: str = "runtime"
    # 直接导致本事件的上游 event_id；与“整条链路”的 correlation_id 不同。
    causation_id: UUID | None = None

    def __post_init__(self) -> None:
        """在事件入库前统一校验追踪 ID 和 actor。"""

        for name in (
            "command_id",
            "correlation_id",
            "thread_id",
            "turn_id",
            "run_id",
            "causation_id",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, UUID):
                raise TypeError(f"{name} must be a UUID or None")
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ValueError("actor must be non-empty text")


@dataclass(frozen=True, slots=True)
class NewEvent:
    """尚未提交的领域事实，由 Runtime 创建、交给 EventStore 追加。"""

    # 每一条事实的全局唯一 ID；不能用 command_id 代替，因为一个命令可产多条事件。
    event_id: UUID
    # 稳定事件类型，名称以 .vN 结尾，例如 turn.started.v1。
    event_type: str
    # payload 的协议版本，必须与 event_type 后缀一致。
    schema_version: int
    # 领域事实发生时间；必须带时区，不等于数据库真正写入的 recorded_at。
    occurred_at: datetime
    # 仅含 JSON 语义数据；构造后会递归复制和冻结，形成可靠快照。
    payload: Mapping[str, Any]
    # 命令、链路和聚合定位信息。
    metadata: EventMetadata

    def __post_init__(self) -> None:
        """校验事件协议，并把调用方传入的可变 payload 固化为不可变快照。"""

        if not isinstance(self.event_id, UUID):
            raise TypeError("event_id must be a UUID")
        if not isinstance(self.event_type, str) or not _EVENT_TYPE_PATTERN.fullmatch(
            self.event_type
        ):
            raise InvalidEvent(
                "event_type must be a lowercase stable name ending in .vN"
            )
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version < 1
        ):
            raise InvalidEvent("schema_version must be a positive integer")
        declared_version = int(self.event_type.rsplit(".v", 1)[1])
        if declared_version != self.schema_version:
            raise InvalidEvent(
                "event_type suffix and schema_version must declare the same version"
            )
        _require_aware(self.occurred_at, "occurred_at")
        if not isinstance(self.payload, Mapping):
            raise InvalidEvent("payload must be a JSON object")
        # frozen dataclass 只禁止重新绑定字段，原始 dict/list 仍可能被外部修改；
        # 因此这里必须深复制并冻结，避免“算指纹时”和“落库时”看到不同内容。
        object.__setattr__(self, "payload", _freeze_json_object(self.payload))
        if not isinstance(self.metadata, EventMetadata):
            raise TypeError("metadata must be EventMetadata")


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """EventStore 已确认提交、可用于聚合重放的历史事实。"""

    event_id: UUID
    stream_id: StreamId
    # 在单个聚合流内从 0 连续递增：用于重放顺序和乐观并发控制。
    stream_version: int
    # 整个 Event Store 的全局顺序：用于投影、审计和全局订阅。
    global_position: int
    # 同一原子 append_batch 共享 commit_id。
    commit_id: UUID
    # 本事件在原子批次中的位置与批次总大小，使分页消费者能识别完整提交边界。
    commit_index: int
    commit_size: int
    event_type: str
    schema_version: int
    occurred_at: datetime
    # 数据库接受该事件的时间；它可能晚于领域发生时间 occurred_at。
    recorded_at: datetime
    payload: Mapping[str, Any]
    metadata: EventMetadata

    def __post_init__(self) -> None:
        """读取事件后同样冻结 payload，避免重放过程意外篡改历史。"""

        object.__setattr__(self, "payload", _freeze_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class StreamWrite:
    """对一条流的条件追加请求：版本精确匹配时才写入全部事件。"""

    stream_id: StreamId
    # 调用者作出决定时看到的流版本；-1 表示要求该流尚不存在。
    expected_version: int
    # 同一流本次连续追加的事件，至少一条。
    events: tuple[NewEvent, ...]

    def __post_init__(self) -> None:
        """规范化事件序列，并保证 expected_version 是精确整数而非布尔值。"""

        if not isinstance(self.stream_id, StreamId):
            raise TypeError("stream_id must be StreamId")
        if (
            not isinstance(self.expected_version, int)
            or isinstance(self.expected_version, bool)
            or self.expected_version < -1
        ):
            raise ValueError("expected_version must be an exact integer >= -1")
        if not isinstance(self.events, tuple):
            object.__setattr__(self, "events", tuple(self.events))
        if not self.events:
            raise ValueError("a stream write must contain at least one event")
        if not all(isinstance(event, NewEvent) for event in self.events):
            raise TypeError("events must contain only NewEvent values")


@dataclass(frozen=True, slots=True)
class StreamPrecondition:
    """只读流栅栏；与 writes 在同一事务中验证但不推进该流。"""

    stream_id: StreamId
    expected_version: int
    required_event_type: str | None = None
    required_payload: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, StreamId):
            raise TypeError("stream_id must be StreamId")
        if not isinstance(self.expected_version, int) or isinstance(self.expected_version, bool) or self.expected_version < -1:
            raise ValueError("expected_version must be an exact integer >= -1")
        if self.required_event_type is not None and not _EVENT_TYPE_PATTERN.fullmatch(self.required_event_type):
            raise ValueError("required_event_type must be a stable event type")
        object.__setattr__(self, "required_payload", _freeze_json_object(self.required_payload))


@dataclass(frozen=True, slots=True)
class StreamAppendReceipt:
    """某一条流在原子批次中的实际写入范围。"""

    stream_id: StreamId
    # 本命令在该流写入的首、尾版本；不是查询时的“最新版本”。
    first_version: int
    last_version: int
    # 与这几条流内版本一一对应的全局日志位置。
    global_positions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AppendReceipt:
    """整个 append_batch 的持久化回执，也是幂等重试需要原样返回的结果。"""

    idempotency_key: UUID
    # 一个命令可以原子修改多个聚合流，因此回执按流列出写入结果。
    streams: tuple[StreamAppendReceipt, ...]


class EventStore(Protocol):
    """Runtime 控制面的持久化端口；SQLite/Postgres 实现都应遵守此契约。"""

    def database_time(self) -> datetime:
        """Return the backend-authoritative UTC clock.

        Lease and expiry decisions must use this clock (contract §2.4);
        the local wall clock is only for non-authoritative display. Tests may
        inject a fake clock to observe paths that depend on it.
        """

        ...

    def append_batch(
        self,
        writes: Sequence[StreamWrite],
        *,
        idempotency_key: UUID,
        request_fingerprint: str | None = None,
        preconditions: Sequence[StreamPrecondition] = (),
    ) -> AppendReceipt:
        """以精确版本原子追加一批流，并按幂等键保存/返回固定回执。

        ``request_fingerprint`` 描述命令的语义内容。同一个 key 与相同指纹是
        安全重试；同一个 key 与不同指纹必须报告 ``IdempotencyConflict``。
        """

        ...

    def read_idempotency(
        self,
        idempotency_key: UUID,
        *,
        request_fingerprint: str,
    ) -> AppendReceipt | None:
        """查询命令是否已经成功提交，并验证本次语义指纹与原请求一致。

        Runtime 应在重新做状态迁移校验前调用它：客户端可能只丢了响应，
        已成功的命令应该返回原回执，而不是因聚合已变化被当成非法重试。
        """

        ...

    def read_stream(
        self,
        stream_id: StreamId,
        *,
        after_version: int = -1,
        limit: int = 500,
    ) -> tuple[StoredEvent, ...]:
        """按 stream_version 读取单个聚合历史，用于 Thread/Turn 状态重建。"""

        ...

    def read_all(
        self,
        *,
        after_position: int = 0,
        limit: int = 500,
    ) -> tuple[StoredEvent, ...]:
        """按 global_position 读取全局事件日志，用于投影、审计和订阅。"""

        ...


def _require_aware(value: datetime, name: str) -> None:
    """要求时间携带可计算的 UTC offset，避免跨进程/容器时间歧义。"""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidEvent(f"{name} must be a timezone-aware datetime")
    if value.utcoffset() is None:
        raise InvalidEvent(f"{name} must have a concrete UTC offset")


def _freeze_json_object(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """冻结事件 payload，并额外保证最外层保持 JSON object 结构。"""

    frozen = _freeze_json(value, path="payload")
    if not isinstance(frozen, Mapping):
        raise InvalidEvent("payload must be a JSON object")
    return frozen


def _freeze_json(value: Any, *, path: str) -> Any:
    """递归复制并冻结 JSON 值，在错误中保留精确字段路径。

    Mapping 转为只读 ``MappingProxyType``，list/tuple 统一转为 tuple；
    NaN/Infinity、非字符串 key 和 Python 运行时对象都被拒绝，以保证事件可被
    其他语言、其他进程和未来版本稳定读取。
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise InvalidEvent(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidEvent(f"{path} contains a non-string key")
            copied[key] = _freeze_json(item, path=f"{path}.{key}")
        return MappingProxyType(copied)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    raise InvalidEvent(f"{path} contains non-JSON value {type(value).__name__}")
