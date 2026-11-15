"""F8 委派作用域:spawn 声明 ``change_scope``,authorize_merge 按它圈定补丁路径。

本文件钉死的契约(供实现对齐;实现落地前这些用例即规格,同
``test_merge_flow.py`` 文件 docstring 的约定):

- ``AgentControlPlane.spawn_agent`` 新增 keyword-only 参数 ``change_scope``,
  默认空元组——现签名全关键字(``agents/control.py:305-317``),加带默认的
  新参数对既有调用点零破坏。
- 作用域是【允许清单】:补丁里每条 change 的路径都必须命中至少一个模式,
  任一路径一个模式都不命中即 ``AgentError("merge_path_outside_change_scope")``,
  由 ``authorize_merge`` 抛出(与它现有全部拒绝同型,control.py:2855-2904;
  它本来就 ``graph.load`` 了 source agent,作用域从子代理记录读)。
- 匹配语义与 ``workspace/merge.py`` 的 ``_path_protected``(merge.py:714-735)
  三段式一致,统一 casefold:模式以 ``/`` 结尾按目录前缀(``key.startswith``);
  模式含 ``/`` 按全路径精确匹配;裸词按 basename 前缀(``name.startswith``)。
  匹配作用在折叠后的规范路径上(``_canonical_merge_path``,merge.py:678-711)。
- ``change_scope=()``(默认)不限制任何路径:未声明作用域的子代理保持既有
  行为,authorize_merge 不新增拒绝。
- ``AgentRecord`` 新增带默认字段 ``change_scope: tuple[str, ...] = ()``;旧形状
  事件(13 键的 ``agent.spawned.v2`` 精确键集,graph.py:230-285)回放后
  ``change_scope == ()``——"新字段靠 dataclass 默认值补齐"与本仓库 spawned.v1
  回放先例一致(graph.py:215-228),模块 docstring 明说旧事件保持可回放
  (graph.py:1-7)。
- 作用域闸与保护面是两道独立的闸:路径落在声明作用域内时 authorize_merge
  放行,``validate_merge_request`` 的 ``protected_patterns`` 仍以
  ``merge_path_protected`` 拒绝同一路径(merge.py:331-333)。

环境纪律同兄弟文件:agent 平面走无 owner 的 legacy 路径
(test_session_root_issuance.py 先例),git 操作全部发生在 tempfile 里的临时
仓库(test_merge_flow.py 先例),不依赖网络与 Docker。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.agents.graph import AgentError, AgentGraph, ContextMode
from koawa_agent_v2.control.event_store import (
    EventMetadata,
    NewEvent,
    StreamId,
    StreamWrite,
)
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.workspace.merge import (
    MergeError,
    MergeRequest,
    canonical_patch_digest,
    export_worktree_patch,
    validate_merge_request,
)

_LEASE_SECONDS = 600
_OCCURRED_AT = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

BASE_TRACKED = b"line-one\nline-two\n"
CHILD_TRACKED = b"line-one\nline-two changed\n"
CHILD_ADDED = b"added-by-child\n"


class MutableClock:
    """Manually advanced clock so lease arithmetic stays deterministic."""

    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


def _patch_document(*paths: str) -> dict:
    """一份合法小补丁文档:每条路径一个 add change,形状同 editing/protocol。"""
    return {
        "schema_version": 1,
        "changes": [
            {
                "operation": "add",
                "path": path,
                "content": f"change scoped to {path}\n",
                "newline": "lf",
                "utf8_bom": False,
            }
            for path in paths
        ],
    }


def _merge_request(
    source_agent_id: UUID, run_id: UUID, *paths: str
) -> MergeRequest:
    """手拼 MergeRequest;digest 与 patch_json 同源现算(同 test_merge_authorization)。"""
    document = _patch_document(*paths)
    return MergeRequest(
        source_agent_id=source_agent_id,
        run_id=run_id,
        base_commit="a" * 40,
        patch_digest=canonical_patch_digest(document),
        patch_json=json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


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


class MergeChangeScopeTest(unittest.TestCase):
    """authorize_merge 侧的作用域闸:声明、拒绝、默认放行、叠加、全流程。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.store = SqliteEventStore(Path(self.temporary, "change-scope.sqlite3"))
        self.clock = MutableClock(_OCCURRED_AT)
        self.control = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(
                max_depth=3, max_total_agents=16, max_concurrent_children=8
            ),
            clock=self.clock,
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

    def _spawn_child(self, change_scope=None):
        """spawn 一个子代理并把它拉到 RUNNING;change_scope=None 表示不传该参数。

        显式不传与显式传 () 是两个都要钉住的兼容面,所以这里用 None 作
        "参数缺席" 哨兵,而不是直接转发 None。
        """
        kwargs = {}
        if change_scope is not None:
            kwargs["change_scope"] = change_scope
        child = self.control.spawn_agent(
            parent_agent_id=self.root.agent_id,
            parent_run_id=self.root_run.run_id,
            task_id="merge-child",
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
            **kwargs,
        )
        child_run = self.control.start_attempt(
            child.agent_id,
            expected_version=child.version,
            lease_seconds=_LEASE_SECONDS,
        )
        return child, child_run

    def _authorize(self, request) -> None:
        self.control.authorize_merge(
            request=request,
            parent_agent_id=self.root.agent_id,
            parent_run_id=self.root_run.run_id,
        )

    # -- 用例 1:声明 src/ 作用域,范围内路径放行 --------------------------------

    def test_declared_scope_admits_in_scope_paths(self) -> None:
        child, child_run = self._spawn_child(("src/",))
        # 作用域持久化在子代理记录上(事件 -> rebuild -> graph.load)。
        stored = self.control.graph.load(child.agent_id)
        self.assertIsNotNone(stored)
        self.assertEqual(("src/",), stored.change_scope)

        request = _merge_request(
            child.agent_id, child_run.run_id, "src/a.txt", "src/nested/deep.txt"
        )
        self._authorize(request)

    # -- 用例 2:范围外路径拒绝 --------------------------------------------------

    def test_path_outside_declared_scope_is_refused(self) -> None:
        child, child_run = self._spawn_child(("src/",))
        request = _merge_request(
            child.agent_id, child_run.run_id, "tests/test_x.py"
        )
        with self.assertRaises(AgentError) as raised:
            self._authorize(request)
        self.assertEqual(
            "merge_path_outside_change_scope", raised.exception.code
        )

    # -- 用例 3:默认空作用域不限制任何路径(兼容既有行为) ----------------------

    def test_default_empty_scope_stays_unrestricted(self) -> None:
        # 参数缺席(真正的默认路径):作用域为空元组,任意路径放行。
        child, child_run = self._spawn_child()
        stored = self.control.graph.load(child.agent_id)
        self.assertEqual((), stored.change_scope)
        self._authorize(
            _merge_request(
                child.agent_id, child_run.run_id, "anything/anywhere.txt"
            )
        )
        # 显式传 change_scope=() 同样不受限。
        explicit, explicit_run = self._spawn_child(())
        self._authorize(
            _merge_request(
                explicit.agent_id, explicit_run.run_id, "top-level.txt"
            )
        )

    # -- 用例 4:裸词作用域按 basename 前缀匹配 ----------------------------------

    def test_bare_word_scope_matches_basename_prefix(self) -> None:
        child, child_run = self._spawn_child(("app",))
        # basename 前缀命中:根级文件。
        self._authorize(
            _merge_request(child.agent_id, child_run.run_id, "app_config.py")
        )
        # basename 前缀命中:嵌套路径也按 basename 比对(非目录匹配)。
        self._authorize(
            _merge_request(child.agent_id, child_run.run_id, "lib/app_main.py")
        )
        # 前缀锚定在开头:myapp_config.py 不以 "app" 开头,范围外。
        with self.assertRaises(AgentError) as raised:
            self._authorize(
                _merge_request(
                    child.agent_id, child_run.run_id, "myapp_config.py"
                )
            )
        self.assertEqual(
            "merge_path_outside_change_scope", raised.exception.code
        )

    # -- 用例 5:全流程(真 git 工作区) -----------------------------------------

    def test_full_flow_out_of_scope_change_is_refused(self) -> None:
        child, child_run = self._spawn_child(("src/",))
        stored = self.control.graph.load(child.agent_id)
        self.assertEqual(("src/",), stored.change_scope)

        # 子代理工作区:提交 base(src/keep.txt 两行),之后只改工作区不提交。
        repo = Path(self.temporary, "repo")
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "test")
        _git(repo, "config", "core.autocrlf", "false")
        _git(repo, "config", "commit.gpgsign", "false")
        _write(repo, "src/keep.txt", BASE_TRACKED)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "base")
        base = _git(repo, "rev-parse", "HEAD").strip()

        # 第一步:只改范围内的 src/keep.txt -> export -> authorize 放行。
        _write(repo, "src/keep.txt", CHILD_TRACKED)
        in_scope = export_worktree_patch(
            worktree=repo,
            base_commit=base,
            source_agent_id=child.agent_id,
            run_id=child_run.run_id,
        )
        self._authorize(in_scope)

        # 第二步:同一工作区、同一 run,再新增范围外的 notes/rogue.txt;
        # 导出的补丁一内一外,任一越界即整体拒绝(fail-closed)。
        _write(repo, "notes/rogue.txt", CHILD_ADDED)
        mixed = export_worktree_patch(
            worktree=repo,
            base_commit=base,
            source_agent_id=child.agent_id,
            run_id=child_run.run_id,
        )
        with self.assertRaises(AgentError) as raised:
            self._authorize(mixed)
        self.assertEqual(
            "merge_path_outside_change_scope", raised.exception.code
        )

    # -- 用例 6:作用域闸与保护面两道闸独立 --------------------------------------

    def test_scope_gate_and_protection_gate_stay_independent(self) -> None:
        child, child_run = self._spawn_child(("tests/",))
        request = _merge_request(
            child.agent_id, child_run.run_id, "tests/locked.txt"
        )
        # 闸一(作用域):tests/locked.txt 命中声明作用域,authorize 放行。
        self._authorize(request)
        # 对照:不带保护面时,同一请求在候选侧校验也通过——拒绝只来自保护面。
        candidate = Path(self.temporary, "candidate")
        candidate.mkdir()
        validate_merge_request(request, candidate_root=candidate)
        # 闸二(保护面):同一路径命中 protected_patterns,仍以
        # merge_path_protected 拒绝——作用域放行不豁免保护面。
        with self.assertRaises(MergeError) as raised:
            validate_merge_request(
                request,
                candidate_root=candidate,
                protected_patterns=("tests/",),
            )
        self.assertEqual("merge_path_protected", raised.exception.code)


class LegacySpawnedEventReplayTest(unittest.TestCase):
    """旧形状 spawned.v2(13 键,无 change_scope 键)回放 -> change_scope == ()。"""

    def test_spawned_v2_without_change_scope_replays_to_empty_scope(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SqliteEventStore(Path(temporary.name, "legacy-spawn.sqlite3"))

        root_id = uuid4()
        child_id = uuid4()
        parent_id = uuid4()
        reservation = uuid4()

        def payload(agent_id, *, parent, root, depth, task, principal, res):
            # 恰为 graph.py:230-285 钉死的 13 键;无 change_scope 键。
            return {
                "agent_id": str(agent_id),
                "parent_agent_id": None if parent is None else str(parent),
                "root_agent_id": str(root),
                "task_id": task,
                "attempt": 1,
                "run_id": None,
                "principal_id": principal,
                "scopes": ["read"],
                "context_mode": "fresh",
                "created_at": _OCCURRED_AT.isoformat(),
                "depth": depth,
                "capacity_reservation_id": (
                    None if res is None else str(res)
                ),
                "budget_reservation_id": (
                    None if res is None else str(res)
                ),
            }

        def append(agent_id, document, *, command):
            event = NewEvent(
                uuid5(command, "event:spawned"),
                "agent.spawned.v2",
                2,
                _OCCURRED_AT,
                document,
                EventMetadata(
                    command, uuid5(command, "correlation"), actor="test"
                ),
            )
            store.append_batch(
                (StreamWrite(StreamId("agent", agent_id), -1, (event,)),),
                idempotency_key=command,
            )

        # 根形状(parent=None、depth=0、两个 reservation 为 None)。
        append(
            root_id,
            payload(
                root_id,
                parent=None,
                root=root_id,
                depth=0,
                task="legacy-root",
                principal="root",
                res=None,
            ),
            command=uuid4(),
        )
        # 子形状(parent 非 None、depth=1、两个 reservation 同值)。
        append(
            child_id,
            payload(
                child_id,
                parent=parent_id,
                root=parent_id,
                depth=1,
                task="legacy-child",
                principal="worker",
                res=reservation,
            ),
            command=uuid4(),
        )

        graph = AgentGraph(store)
        root_record = graph.load(root_id)
        child_record = graph.load(child_id)
        self.assertIsNotNone(root_record)
        self.assertIsNotNone(child_record)
        # 旧流回放给新字段默认值:change_scope == ()。
        self.assertEqual((), root_record.change_scope)
        self.assertEqual((), child_record.change_scope)
        # 回放本身不受影响:形状其余部分照旧成立。
        self.assertEqual(("read",), root_record.scopes)
        self.assertEqual(("read",), child_record.scopes)
        self.assertEqual("legacy-root", root_record.task_id)
        self.assertEqual("legacy-child", child_record.task_id)


if __name__ == "__main__":
    unittest.main()
