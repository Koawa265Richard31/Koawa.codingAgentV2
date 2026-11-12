"""F8 合并账本:``AgentControlPlane.record_merge_effect`` 的幂等与内容。

本文件钉死的契约(供实现对齐;实现落地前这些用例即规格):

- ``AgentControlPlane.record_merge_effect(request, *, parent_agent_id)`` 把一次
  merge 请求记入 workspace-effect 账本。``request`` 是
  ``koawa_agent_v2.workspace.merge`` 的 :class:`MergeRequest`;
  ``parent_agent_id`` 是授权合并的父代理。
- 幂等键只由 request 的 ``(source_agent_id, run_id, patch_digest)`` 决定
  (merge.py 的语义键不含 ``parent_agent_id``):相同输入两次调用返回相同的
  effect id,且账本里只有这一条效果(effect 流唯一,
  ``WorkspaceEffectStore.load`` 可读回)。同一 request 换一个
  ``parent_agent_id`` 重新入账会撞 ``workspace_effect_idempotency_conflict``
  ——一份补丁在一次 run 里只承认一次授权记录,这是有意的围栏,测试钉死。
- 效果的持久载荷含 ``source_agent_id``、``run_id``、``parent_agent_id``、
  ``base_commit``、``patch_digest`` 五个字段,值与请求一致。字段可落在效果
  记录的 ``result`` 映射或效果流事件的 payload 里,测试在两者合并视图上
  断言;storage 形态由实现选,字段名与取值由本文件钉死。
- ``patch_digest`` 变化(补丁内容一个字符之差,规范摘要随之改变)→ 不同
  effect id。用"改内容后重算 ``canonical_patch_digest``"而不是直接篡改摘要
  字节:本用例验证的是幂等键参与摘要,不与请求合法性校验耦合。
- ``patch_digest`` 是 64 位十六进制,可以安全地作为账本摘要字段
  (``effects._digest`` 的形状);``base_commit`` 只是身份文本。

账本载体沿用勘察到的 API:效果事件在 ``StreamId("workspace-effect", effect_id)``
流里(effects.py:603-604),读回用 ``WorkspaceEffectStore.load``;
"账本里只有一条"按 effect 流去重计数(测试自建 :class:`SqliteEventStore`,
直接 ``read_all`` 按流类别过滤)。agent 平面用无 owner 的 legacy 路径
(test_session_root_issuance.py:98-113 先例):根代理 ``start_attempt`` 进入
RUNNING 拿到 run,再以 ``parent_run_id`` 围栏派生子代理,子代理同样
``start_attempt`` 拿到请求的 ``run_id``——这是生产形状,同时对"父代理必须
在运行中"之类实现选择保持通过。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.agents.graph import ContextMode
from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.workspace.effects import WorkspaceEffectStore
from koawa_agent_v2.workspace.merge import (
    MergeRequest,
    canonical_patch_digest,
    export_worktree_patch,
)

BASE_TRACKED = b"line-one\nline-two\n"
CHILD_TRACKED = b"line-one\nline-two changed\n"
CHILD_ADDED = b"added-by-child\n"
_LEASE_SECONDS = 600


def _clock() -> datetime:
    return datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr}"
        )
    return result.stdout


def _write(repo: Path, relative: str, data: bytes) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _effect_id(result: object) -> UUID:
    """从 record_merge_effect 的返回值中解出 effect id。

    契约只要求返回值"暴露所记效果的 id":UUID 本身、带 ``effect_id`` 的
    WorkspaceEffectRecord、或带 ``record`` 的 WorkspaceEffectWrite 都接受;
    其余形状视为实现违约,显式失败而不是静默猜测。
    """
    record = getattr(result, "record", None)
    if record is not None and getattr(record, "effect_id", None) is not None:
        return record.effect_id
    effect_id = getattr(result, "effect_id", None)
    if effect_id is not None:
        return effect_id
    if isinstance(result, UUID):
        return result
    raise AssertionError(
        "record_merge_effect must expose the recorded effect id "
        f"(UUID / WorkspaceEffectRecord / WorkspaceEffectWrite), got {type(result)!r}"
    )


def _effect_stream_ids(store: SqliteEventStore) -> set[UUID]:
    """账本里已出现的效果流聚合 id(workspace-effect 类别去重)。"""
    seen: set[UUID] = set()
    cursor = 0
    while True:
        page = store.read_all(after_position=cursor, limit=500)
        if not page:
            return seen
        for event in page:
            if event.stream_id.category == "workspace-effect":
                seen.add(event.stream_id.aggregate_id)
        cursor = page[-1].global_position


def _durable_value_index(store: SqliteEventStore, effect_id: UUID) -> dict[str, set[str]]:
    """效果持久载荷的"字段 -> 取值集合"索引。

    载荷来源:效果记录的 ``result`` 映射(若已带)+ 效果流全部事件的
    payload。同一字段在多处的取值必须一致,断言按 str 归一后比较。
    """
    values: dict[str, set[str]] = {}
    record = WorkspaceEffectStore(store).load(effect_id)
    payloads: list[dict] = []
    if record is not None and record.result is not None:
        payloads.append(dict(record.result))
    stream = StreamId("workspace-effect", effect_id)
    cursor = -1
    while True:
        page = store.read_stream(stream, after_version=cursor, limit=500)
        if not page:
            break
        payloads.extend(dict(event.payload) for event in page)
        cursor = page[-1].stream_version
    for payload in payloads:
        for key, value in payload.items():
            values.setdefault(key, set()).add(str(value))
    return values


class _MergeEffectCase(unittest.TestCase):
    """无 owner 控制平面 + 真实导出的 MergeRequest(可被任何校验接受)。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SqliteEventStore(Path(temporary.name, "merge-effects.sqlite3"))
        self.control = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(
                max_depth=3, max_total_agents=8, max_concurrent_children=4
            ),
            clock=_clock,
        )
        self.root = self.control.spawn_agent(
            parent_agent_id=None,
            task_id="merge-root",
            principal_id="root",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        self.root_run = self.control.start_attempt(
            self.root.agent_id,
            expected_version=self.root.version,
            lease_seconds=_LEASE_SECONDS,
        )
        self.child = self.control.spawn_agent(
            parent_agent_id=self.root.agent_id,
            parent_run_id=self.root_run.run_id,
            task_id="merge-child",
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        self.child_run = self.control.start_attempt(
            self.child.agent_id,
            expected_version=self.child.version,
            lease_seconds=_LEASE_SECONDS,
        )

        # 子代理工作区:base 提交后,未提交地改 tracked.txt、新增 added.txt。
        repo = Path(temporary.name) / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "test")
        _git(repo, "config", "core.autocrlf", "false")
        _git(repo, "config", "commit.gpgsign", "false")
        _write(repo, "tracked.txt", BASE_TRACKED)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "base")
        self.base = _git(repo, "rev-parse", "HEAD").strip()
        _write(repo, "tracked.txt", CHILD_TRACKED)
        _write(repo, "added.txt", CHILD_ADDED)
        self.repo = repo

        self.request = export_worktree_patch(
            worktree=self.repo,
            base_commit=self.base,
            source_agent_id=self.child.agent_id,
            run_id=self.child_run.run_id,
        )

    def _record(self, request: MergeRequest | None = None):
        return self.control.record_merge_effect(
            request or self.request, parent_agent_id=self.root.agent_id
        )


class RecordMergeEffectTest(_MergeEffectCase):
    def test_same_request_and_parent_records_exactly_one_effect(self) -> None:
        first = _effect_id(self._record())
        second = _effect_id(self._record())

        self.assertEqual(first, second)

        streams = _effect_stream_ids(self.store)
        self.assertEqual({first}, streams)
        self.assertIsNotNone(WorkspaceEffectStore(self.store).load(first))

    def test_payload_carries_merge_identity(self) -> None:
        effect_id = _effect_id(self._record())

        values = _durable_value_index(self.store, effect_id)
        expected = {
            "source_agent_id": self.child.agent_id,
            "run_id": self.request.run_id,
            "parent_agent_id": self.root.agent_id,
            "base_commit": self.request.base_commit,
            "patch_digest": self.request.patch_digest,
        }
        for key, value in expected.items():
            self.assertIn(
                key, values, f"effect payload must carry {key!r}: {sorted(values)}"
            )
            self.assertEqual(
                {str(value)},
                values[key],
                f"effect payload {key!r} must match the request exactly",
            )

    def test_changed_patch_digest_gets_a_different_effect_id(self) -> None:
        document = json.loads(self.request.patch_json)
        additions = [
            change
            for change in document["changes"]
            if change.get("operation") == "add"
        ]
        self.assertTrue(additions, "fixture must export at least one add change")
        # 内容一个字符之差 -> 规范摘要随之改变;文档保持自洽。
        additions[0]["content"] = additions[0]["content"] + "x"
        patch_json = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        altered = MergeRequest(
            source_agent_id=self.request.source_agent_id,
            run_id=self.request.run_id,
            base_commit=self.request.base_commit,
            patch_digest=canonical_patch_digest(document),
            patch_json=patch_json,
        )
        self.assertNotEqual(altered.patch_digest, self.request.patch_digest)

        first = _effect_id(self._record())
        second = _effect_id(self._record(altered))

        self.assertNotEqual(first, second)

    def test_same_request_with_a_different_parent_conflicts(self) -> None:
        """语义键不含 parent_agent_id:同一 request 换授权方撞幂等冲突。

        一份补丁在一次 run 里只承认一次授权记录——首条不被改写,重放
        fail-closed 为 ``workspace_effect_idempotency_conflict``。
        """

        self._record()
        outsider = self.control.spawn_agent(
            parent_agent_id=None,
            task_id="outsider-root",
            principal_id="root",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        with self.assertRaises(Exception) as raised:
            self.control.record_merge_effect(
                self.request, parent_agent_id=outsider.agent_id
            )
        self.assertEqual(
            "workspace_effect_idempotency_conflict", raised.exception.code
        )


if __name__ == "__main__":
    unittest.main()
