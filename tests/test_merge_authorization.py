"""Merge authorization boundary: ``AgentControlPlane.authorize_merge``.

F8 exported the patch-level merge boundary (``workspace/merge.py``);
``authorize_merge`` is the parent-side gate that decides whether a child's
:class:`MergeRequest` may enter the candidate.  These tests pin the contract on
the legacy (no-owner) control plane, over a three-layer root -> worker -> child
tree plus one independent second tree:

- the named parent must sit on the source agent's ancestor chain
  (``merge_parent_not_in_chain`` - the source itself does not count);
- both the source agent and the parent must exist
  (``merge_source_agent_missing`` / ``merge_parent_agent_missing``);
- the parent must be alive (``parent_agent_not_active`` /
  ``parent_agent_orphaned``) and present its current run
  (``parent_run_fenced``);
- the request must be a real :class:`MergeRequest`
  (``invalid_merge_request``).

Every refusal asserts the exact stable error code via ``AgentError.code``.
Parent state is reached with the established techniques from
``test_d11_agent_concurrency``: lease expiry driven by a mutable clock plus
``discover_orphans`` for ORPHANED, and the message -> attempt -> result ->
ack -> ``terminal_result_identity`` -> ``terminal`` sequence for COMPLETED.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.agents.control import (
    AgentBudgetLimits,
    AgentControlPlane,
    terminal_result_identity,
)
from koawa_agent_v2.agents.graph import AgentError, AgentState, ContextMode
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.workspace.merge import MergeRequest, canonical_patch_digest


class MutableClock:
    """Manually advanced clock so lease arithmetic stays deterministic."""

    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


# 同 merge._export_document 的文档形状(editing/protocol 要求键集恰为
# {"schema_version", "changes"},add 条目带 operation/path/content/newline/
# utf8_bom)。authorize_merge 不解析补丁内容;这里只保证 request 携带一份
# 合法小文档,patch_digest 用 canonical_patch_digest 现算、与 patch_json 同源。
_PATCH_DOCUMENT = {
    "schema_version": 1,
    "changes": [
        {
            "operation": "add",
            "path": "notes/merge-authorization.txt",
            "content": "patch payload bound to the merge request\n",
            "newline": "lf",
            "utf8_bom": False,
        }
    ],
}


def _merge_request(source_agent_id: UUID, run_id: UUID) -> MergeRequest:
    return MergeRequest(
        source_agent_id=source_agent_id,
        run_id=run_id,
        base_commit="a" * 40,
        patch_digest=canonical_patch_digest(_PATCH_DOCUMENT),
        patch_json=json.dumps(
            _PATCH_DOCUMENT,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


class MergeAuthorizationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SqliteEventStore(Path(temporary.name, "merge-auth.sqlite3"))
        self.clock = MutableClock(datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc))
        self.control = AgentControlPlane(
            self.store,
            limits=AgentBudgetLimits(
                max_depth=3, max_total_agents=16, max_concurrent_children=8
            ),
            clock=self.clock,
        )
        self.root = self.control.spawn_agent(
            parent_agent_id=None,
            task_id="root",
            principal_id="root",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        self.worker = self.control.spawn_agent(
            parent_agent_id=self.root.agent_id,
            task_id="worker",
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        self.child = self.control.spawn_agent(
            parent_agent_id=self.worker.agent_id,
            task_id="child",
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        # 第二棵独立 root 树,提供"不在 source 链上"的 parent 候选。
        self.root2 = self.control.spawn_agent(
            parent_agent_id=None,
            task_id="root2",
            principal_id="root",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        self.outsider = self.control.spawn_agent(
            parent_agent_id=self.root2.agent_id,
            task_id="outsider",
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        # child 的 spawn 向 worker 的 agent 流追加了 child-spawn-authorized;
        # 刷新投影后拿最新 version 去 start_attempt。
        self.worker = self.control.graph.load(self.worker.agent_id)
        self.worker_run = self.control.start_attempt(
            self.worker.agent_id,
            expected_version=self.worker.version,
            lease_seconds=10,
        )
        self.child_run = self.control.start_attempt(
            self.child.agent_id,
            expected_version=self.child.version,
            lease_seconds=10,
        )
        self.outsider_run = self.control.start_attempt(
            self.outsider.agent_id,
            expected_version=self.outsider.version,
            lease_seconds=10,
        )
        self.request = _merge_request(self.child.agent_id, self.child_run.run_id)

    def test_accepts_direct_parent_with_matching_run(self) -> None:
        """parent=worker 在 child 链上、worker RUNNING 且 run 精确匹配 -> 不抛。"""
        self.control.authorize_merge(
            request=self.request,
            parent_agent_id=self.worker.agent_id,
            parent_run_id=self.worker_run.run_id,
        )

    def test_parent_outside_source_chain_is_refused(self) -> None:
        with self.assertRaises(AgentError) as raised:
            self.control.authorize_merge(
                request=self.request,
                parent_agent_id=self.outsider.agent_id,
                parent_run_id=self.outsider_run.run_id,
            )
        self.assertEqual("merge_parent_not_in_chain", raised.exception.code)

    def test_parent_equal_to_source_is_refused(self) -> None:
        """parent_agent_id 传 child 自己:自己不在自己的祖先链上。"""
        with self.assertRaises(AgentError) as raised:
            self.control.authorize_merge(
                request=self.request,
                parent_agent_id=self.child.agent_id,
                parent_run_id=self.child_run.run_id,
            )
        self.assertEqual("merge_parent_not_in_chain", raised.exception.code)

    def test_missing_source_agent_is_refused(self) -> None:
        request = _merge_request(uuid4(), self.child_run.run_id)
        with self.assertRaises(AgentError) as raised:
            self.control.authorize_merge(
                request=request,
                parent_agent_id=self.worker.agent_id,
                parent_run_id=self.worker_run.run_id,
            )
        self.assertEqual("merge_source_agent_missing", raised.exception.code)

    def test_missing_parent_agent_is_refused(self) -> None:
        with self.assertRaises(AgentError) as raised:
            self.control.authorize_merge(
                request=self.request,
                parent_agent_id=uuid4(),
                parent_run_id=uuid4(),
            )
        self.assertEqual("merge_parent_agent_missing", raised.exception.code)

    def test_running_parent_with_no_run_id_is_fenced(self) -> None:
        """RUNNING 父级 + parent_run_id=None:缺 run 与错 run 同码拒绝。"""
        with self.assertRaises(AgentError) as raised:
            self.control.authorize_merge(
                request=self.request,
                parent_agent_id=self.worker.agent_id,
                parent_run_id=None,
            )
        self.assertEqual("parent_run_fenced", raised.exception.code)

    def test_grandparent_ancestor_is_accepted(self) -> None:
        """深祖先(祖父级)在链上即有权授权,不要求直接父级。"""
        # root 流被 worker/child 的 spawn 事件追加过,刷新后再 start_attempt。
        root = self.control.graph.load(self.root.agent_id)
        root_run = self.control.start_attempt(
            root.agent_id,
            expected_version=root.version,
            lease_seconds=10,
        )
        self.control.authorize_merge(
            request=self.request,
            parent_agent_id=self.root.agent_id,
            parent_run_id=root_run.run_id,
        )

    def test_completed_parent_is_refused(self) -> None:
        # child 先落终态,否则 worker 的 terminal 会被自己的 active child 拦下
        # (agent_children_active)。child 的 mailbox 为空,CANCELLED 允许零
        # results(terminal_result_identity 的唯一空列表出口)。
        child_ref, child_digest = terminal_result_identity(
            self.child.agent_id,
            self.child_run.run_id,
            AgentState.CANCELLED,
            "cancelled_before_dispatch",
            self.control.mailbox.load(self.child.agent_id),
        )
        self.control.terminal(
            self.child.agent_id,
            run_id=self.child_run.run_id,
            expected_attempt=self.child_run.attempt,
            state=AgentState.CANCELLED,
            reason="cancelled_before_dispatch",
            result_ref=child_ref,
            result_digest=child_digest,
            source_message_ids=(),
        )
        # child 的结算向仍 non-terminal 的 worker 投递一条 RESULT 消息,先按
        # deliver -> result -> ack 处理掉,worker 才能 COMPLETED。
        result_message = self.control.mailbox.load(self.worker.agent_id)[0]
        delivered = self.control.deliver_message(
            self.worker.agent_id,
            result_message.message_id,
            run_id=self.worker_run.run_id,
        )
        self.control.record_message_result(
            self.worker.agent_id,
            result_message.message_id,
            run_id=self.worker_run.run_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            outcome="ok",
        )
        self.control.ack_message(
            self.worker.agent_id,
            result_message.message_id,
            run_id=self.worker_run.run_id,
        )
        current = self.control.graph.load(self.worker.agent_id)
        ref, digest = terminal_result_identity(
            self.worker.agent_id,
            self.worker_run.run_id,
            AgentState.COMPLETED,
            None,
            self.control.mailbox.load(self.worker.agent_id),
        )
        self.control.terminal(
            self.worker.agent_id,
            run_id=self.worker_run.run_id,
            expected_attempt=current.attempt,
            state=AgentState.COMPLETED,
            reason=None,
            result_ref=ref,
            result_digest=digest,
            source_message_ids=(result_message.message_id,),
        )
        with self.assertRaises(AgentError) as raised:
            self.control.authorize_merge(
                request=self.request,
                parent_agent_id=self.worker.agent_id,
                parent_run_id=self.worker_run.run_id,
            )
        self.assertEqual("parent_agent_not_active", raised.exception.code)

    def test_orphaned_parent_is_refused(self) -> None:
        # 租约过期 + discover_orphans(RUNNING 且 lease_expires_at <= now 即
        # 落 ORPHANED),不直接写事件。
        self.clock.value += timedelta(seconds=11)
        self.control.discover_orphans()
        orphaned = self.control.graph.load(self.worker.agent_id)
        self.assertEqual(AgentState.ORPHANED, orphaned.state)
        with self.assertRaises(AgentError) as raised:
            self.control.authorize_merge(
                request=self.request,
                parent_agent_id=self.worker.agent_id,
                parent_run_id=self.worker_run.run_id,
            )
        self.assertEqual("parent_agent_orphaned", raised.exception.code)

    def test_stale_parent_run_is_refused(self) -> None:
        """worker RUNNING 但 parent_run_id 与其当前 run 不符。"""
        with self.assertRaises(AgentError) as raised:
            self.control.authorize_merge(
                request=self.request,
                parent_agent_id=self.worker.agent_id,
                parent_run_id=uuid4(),
            )
        self.assertEqual("parent_run_fenced", raised.exception.code)

    def test_non_merge_request_payload_is_refused(self) -> None:
        with self.assertRaises(AgentError) as raised:
            self.control.authorize_merge(
                request=object(),
                parent_agent_id=self.worker.agent_id,
                parent_run_id=self.worker_run.run_id,
            )
        self.assertEqual("invalid_merge_request", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
