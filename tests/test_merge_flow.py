"""F8 合并全流程:子代理工作区 -> 授权 -> 账本 -> 校验 -> 应用。

一条用例走完 merge 边界的整条链路,一条用例守住授权的祖先链边界。
本文件钉死的契约(供实现对齐;实现落地前这些用例即规格):

- ``AgentControlPlane.authorize_merge(request, *, parent_agent_id,
  parent_run_id)``:``request`` 是 ``koawa_agent_v2.workspace.merge`` 的
  :class:`MergeRequest`。授权方必须落在 ``request.source_agent_id`` 的祖先链
  上(沿 parent_agent_id 上溯,同 ``root_for`` 的走法),否则抛
  ``AgentError("merge_parent_not_in_chain")``;实现先围栏后链查——两种顺序
  都是 fail-closed,但拒绝码以"围栏赢"为准(链外 parent + COMPLETED 态报
  ``parent_agent_not_active``)。授权方须处于 RUNNING 且 ``parent_run_id``
  等于其当前 run;缺 run 与错 run 统一 ``parent_run_fenced``,
  ``parent_run_required`` 从不被 authorize_merge 抛出(沿用 spawn 的围栏码
  ``parent_agent_not_active`` / ``parent_agent_orphaned`` /
  ``parent_run_fenced``)。CREATED 态(从未 start_attempt)不在围栏内。
  授权本身是纯检查,
  不写效果账本——账本写入只发生在 ``record_merge_effect``。
- ``AgentControlPlane.record_merge_effect(request, *, parent_agent_id)``
  的返回值暴露所记效果的 effect id(解包规则见 ``_effect_id``)。

环境纪律:agent 平面用无 owner 的 legacy 路径(test_session_root_issuance.py:
98-113 先例);git 操作全部发生在 tempfile 里的临时仓库,候选目录不是 git
仓库(test_merge_export_rejections.py:122-127 先例);不依赖网络与 Docker。
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from koawa_agent_v2.agents.control import AgentBudgetLimits, AgentControlPlane
from koawa_agent_v2.agents.graph import AgentError, ContextMode
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.editing.protocol import PatchLimits, parse_patch_document
from koawa_agent_v2.editing.transaction import AtomicPatchWorkspace
from koawa_agent_v2.tools.workspace import WorkspacePathResolver
from koawa_agent_v2.workspace.effects import WorkspaceEffectStore
from koawa_agent_v2.workspace.merge import export_worktree_patch, validate_merge_request

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
    """record_merge_effect 返回值 -> effect id(与 test_merge_effects 一致)。"""
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


class MergeFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SqliteEventStore(Path(temporary.name, "merge-flow.sqlite3"))
        # 无 owner:root 权威不走 session 签发(legacy 嵌入路径)。
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

        # 子代理工作区:提交 base(tracked.txt 两行),再未提交地改
        # tracked.txt、新增 added.txt;子代理只改工作区、不提交。
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

        # 父级侧候选目录:只放 base 版本的 tracked.txt(不是 git 仓库,
        # added.txt 尚不存在)。
        candidate = Path(temporary.name) / "candidate"
        candidate.mkdir()
        _write(candidate, "tracked.txt", BASE_TRACKED)
        self.candidate = candidate
        self.effects = WorkspaceEffectStore(self.store)

    def test_child_patch_flows_into_parent_candidate(self) -> None:
        self.control.authorize_merge(
            self.request,
            parent_agent_id=self.root.agent_id,
            parent_run_id=self.root_run.run_id,
        )
        effect_id = _effect_id(
            self.control.record_merge_effect(
                self.request, parent_agent_id=self.root.agent_id
            )
        )
        self.assertIsNotNone(self.effects.load(effect_id))

        validate_merge_request(self.request, candidate_root=self.candidate)

        resolver = WorkspacePathResolver(self.candidate)
        self.addCleanup(resolver.close)
        workspace = AtomicPatchWorkspace(self.candidate, resolver)
        workspace.apply(parse_patch_document(self.request.patch_json, PatchLimits()))

        self.assertEqual(CHILD_TRACKED, (self.candidate / "tracked.txt").read_bytes())
        self.assertTrue((self.candidate / "added.txt").is_file())
        self.assertEqual(CHILD_ADDED, (self.candidate / "added.txt").read_bytes())

    def test_unrelated_parent_cannot_authorize_and_writes_no_effect(self) -> None:
        stranger = self.control.spawn_agent(
            parent_agent_id=None,
            task_id="stranger",
            principal_id="stranger",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        stranger_run = self.control.start_attempt(
            stranger.agent_id,
            expected_version=stranger.version,
            lease_seconds=_LEASE_SECONDS,
        )

        with self.assertRaises(AgentError) as raised:
            self.control.authorize_merge(
                self.request,
                parent_agent_id=stranger.agent_id,
                parent_run_id=stranger_run.run_id,
            )
        self.assertEqual("merge_parent_not_in_chain", raised.exception.code)

        # 被拒的授权不得在效果账本留下任何东西。
        self.assertEqual(set(), _effect_stream_ids(self.store))


if __name__ == "__main__":
    unittest.main()
