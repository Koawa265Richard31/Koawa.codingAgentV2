"""Merge 来源闸:``authorize_merge`` 对 source agent 状态与 run 绑定的三道闸。

F8 已有 ``merge_source_agent_missing``;本文件钉死其上新增的三道来源闸契约
(供实现对齐;实现落地前这些用例即规格,``test_merge_flow.py`` 同一先例):

- 来源 CANCELLED -> ``merge_source_cancelled``;
- 来源 FAILED -> ``merge_source_failed``;
- 来源的 run 身份与 ``request.run_id`` 不一致 -> ``merge_source_run_mismatch``:
  RUNNING / CREATED 等非终态来源以记录上的 ``record.run_id`` 为准(从未
  start_attempt 即 ``None``,任何非空请求 run 都算不一致);COMPLETED 例外——
  terminal 结算按 reducer 语义清空 ``record.run_id``(graph.py:429,482,517),
  补丁的 run 绑定只由 ``request.run_id`` 携带,故 COMPLETED 来源以其原始 run
  出示请求即放行(正常流:子代理跑完 -> 导出补丁 -> 父级授权)。

顺序契约:状态闸先于 run 闸。CANCELLED / FAILED 来源即使按原始 run 出示请求
(此时 ``record.run_id`` 已为 ``None``),也报状态码而非 ``merge_source_run_mismatch``。

放行契约:RUNNING 来源 run 一致 -> 放行;COMPLETED 来源按原始 run -> 放行。

环境纪律沿 ``test_merge_authorization.py``:legacy(无 owner)控制平面 +
MutableClock + root -> worker -> child 三层树,``MergeRequest`` 手拼合法小文档
(authorize_merge 不解析补丁内容)。FAILED / COMPLETED 结算各需至少一条 ACKED
结果(``terminal_result_identity``:只有 CANCELLED 允许空 results),故先给
child 投递一条可 ACK 的 TASK 消息再结算;每个拒绝用例精确断言稳定错误码。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.agents.control import (
    AgentBudgetLimits,
    AgentControlPlane,
    terminal_result_identity,
)
from koawa_agent_v2.agents.graph import AgentError, AgentState, ContextMode
from koawa_agent_v2.agents.messages import MessageKind, MessageStatus
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
# utf8_bom)。来源闸只看身份,不解析补丁;patch_digest 用 canonical_patch_digest
# 现算、与 patch_json 同源。
_PATCH_DOCUMENT = {
    "schema_version": 1,
    "changes": [
        {
            "operation": "add",
            "path": "notes/merge-source-gate.txt",
            "content": "patch payload bound to the source run\n",
            "newline": "lf",
            "utf8_bom": False,
        }
    ],
}


def _merge_request(source_agent_id: UUID, run_id: UUID | None) -> MergeRequest:
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


class MergeSourceGateTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SqliteEventStore(Path(temporary.name, "merge-source-gate.sqlite3"))
        self.clock = MutableClock(datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc))
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
        # child 的 spawn 向 worker 的 agent 流追加了 child-spawn-authorized;
        # 刷新投影后拿最新 version 去 start_attempt(worker 保持 RUNNING 以便
        # 出示 parent_run_id;child 即被测的"来源")。
        self.worker = self.control.graph.load(self.worker.agent_id)
        self.worker_run = self.control.start_attempt(
            self.worker.agent_id,
            expected_version=self.worker.version,
            lease_seconds=10,
        )
        self.child = self.control.graph.load(self.child.agent_id)
        self.child_run = self.control.start_attempt(
            self.child.agent_id,
            expected_version=self.child.version,
            lease_seconds=10,
        )
        self.request = _merge_request(self.child.agent_id, self.child_run.run_id)

    # ------------------------------------------------------------------
    # 结算辅助:FAILED / COMPLETED 各需至少一条 ACKED 结果,先投一条可 ACK
    # 的 TASK 消息;terminal 内部会用 mailbox 快照逐字节复核 identity。
    # ------------------------------------------------------------------
    def _ack_one_task_on_child(self) -> UUID:
        message = self.control.send_message(
            self.child.agent_id,
            from_agent_id=self.worker.agent_id,
            kind=MessageKind.TASK,
            body_ref="source-gate-work",
            idempotency_key="source-gate-work-1",
        )
        delivered = self.control.deliver_message(
            self.child.agent_id,
            message.message_id,
            run_id=self.child_run.run_id,
        )
        self.control.record_message_result(
            self.child.agent_id,
            message.message_id,
            run_id=self.child_run.run_id,
            expected_delivery_attempt=delivered.delivery_attempt,
            outcome="ok",
        )
        self.control.ack_message(
            self.child.agent_id,
            message.message_id,
            run_id=self.child_run.run_id,
        )
        return message.message_id

    def _settle_child_terminal(
        self, state: AgentState, reason: str | None
    ) -> AgentRecord:
        current = self.control.graph.load(self.child.agent_id)
        messages = self.control.mailbox.load(self.child.agent_id)
        ref, digest = terminal_result_identity(
            self.child.agent_id,
            self.child_run.run_id,
            state,
            reason,
            messages,
        )
        acked = tuple(
            message.message_id
            for message in messages
            if message.status is MessageStatus.ACKED
        )
        settled = self.control.terminal(
            self.child.agent_id,
            run_id=self.child_run.run_id,
            expected_attempt=current.attempt,
            state=state,
            reason=reason,
            result_ref=ref,
            result_digest=digest,
            source_message_ids=acked,
        )
        return settled

    def _authorize(self, request: MergeRequest) -> None:
        """授权方固定为直接父级 worker(RUNNING 且 run 精确匹配)。"""
        self.control.authorize_merge(
            request,
            parent_agent_id=self.worker.agent_id,
            parent_run_id=self.worker_run.run_id,
        )

    # ------------------------------------------------------------------
    # 闸一:来源 CANCELLED -> merge_source_cancelled
    # ------------------------------------------------------------------
    def test_cancelled_source_is_refused(self) -> None:
        # child 的 mailbox 为空,CANCELLED 是唯一允许零 results 的终态
        # (terminal_result_identity 的空列表出口)。
        settled = self._settle_child_terminal(
            AgentState.CANCELLED, "cancelled_before_dispatch"
        )
        self.assertEqual(AgentState.CANCELLED, settled.state)
        # 结算把 record.run_id 清成 None;仍按原始 run 出示请求,状态闸必须
        # 先于 run 闸生效,报状态码而非 merge_source_run_mismatch。
        with self.assertRaises(AgentError) as raised:
            self._authorize(self.request)
        self.assertEqual("merge_source_cancelled", raised.exception.code)

    # ------------------------------------------------------------------
    # 闸二:来源 FAILED -> merge_source_failed
    # ------------------------------------------------------------------
    def test_failed_source_is_refused(self) -> None:
        # FAILED 与 COMPLETED 一样要求至少一条 ACKED 结果,先投递并 ACK 一条
        # TASK 消息,再以稳定 reason 结算。
        self._ack_one_task_on_child()
        settled = self._settle_child_terminal(AgentState.FAILED, "work_failed")
        self.assertEqual(AgentState.FAILED, settled.state)
        with self.assertRaises(AgentError) as raised:
            self._authorize(self.request)
        self.assertEqual("merge_source_failed", raised.exception.code)

    # ------------------------------------------------------------------
    # 闸三放行面:来源 COMPLETED -> 正常流放行
    # ------------------------------------------------------------------
    def test_completed_source_with_original_run_is_accepted(self) -> None:
        self._ack_one_task_on_child()
        settled = self._settle_child_terminal(AgentState.COMPLETED, None)
        self.assertEqual(AgentState.COMPLETED, settled.state)
        # COMPLETED 的 record.run_id 已清空;request.run_id 绑定的是产出补丁
        # 的原始 run,这是"子代理跑完 -> 导出 -> 父级授权"的正常流,必须放行。
        self._authorize(self.request)

    # ------------------------------------------------------------------
    # 闸三拒绝面:run 绑定不一致 -> merge_source_run_mismatch
    # ------------------------------------------------------------------
    def test_foreign_request_run_is_refused(self) -> None:
        """RUNNING 来源 + request.run_id 换成无关 UUID -> run 闸拒绝。"""
        request = _merge_request(self.child.agent_id, uuid4())
        self.assertNotEqual(self.child_run.run_id, request.run_id)
        with self.assertRaises(AgentError) as raised:
            self._authorize(request)
        self.assertEqual("merge_source_run_mismatch", raised.exception.code)

    def test_source_without_started_run_is_refused(self) -> None:
        """从未 start_attempt 的来源(record.run_id=None)-> run 闸拒绝。"""
        second = self.control.spawn_agent(
            parent_agent_id=self.worker.agent_id,
            parent_run_id=self.worker_run.run_id,
            task_id="never-started",
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        never_started = self.control.graph.load(second.agent_id)
        self.assertEqual(AgentState.CREATED, never_started.state)
        self.assertIsNone(never_started.run_id)
        request = _merge_request(second.agent_id, uuid4())
        with self.assertRaises(AgentError) as raised:
            self._authorize(request)
        self.assertEqual("merge_source_run_mismatch", raised.exception.code)

    def test_request_with_null_run_id_is_refused_even_against_null_record(
        self,
    ) -> None:
        """手拼 MergeRequest(run_id=None) 撞 record.run_id=None:无条件拒绝。

        MergeRequest 无 __post_init__,None != None 为假会放行——补丁必须
        绑定具体 run,run 闸对 None 请求先行拒绝(纵深防御,#23 复核 medium)。
        """
        second = self.control.spawn_agent(
            parent_agent_id=self.worker.agent_id,
            parent_run_id=self.worker_run.run_id,
            task_id="null-run-request",
            principal_id="worker",
            scopes=("read",),
            context_mode=ContextMode.FRESH,
        )
        request = _merge_request(second.agent_id, None)
        with self.assertRaises(AgentError) as raised:
            self._authorize(request)
        self.assertEqual("merge_source_run_mismatch", raised.exception.code)

    # ------------------------------------------------------------------
    # 闸三放行面:RUNNING 来源 + run 一致 -> 放行(现策略允许)
    # ------------------------------------------------------------------
    def test_running_source_with_matching_run_is_accepted(self) -> None:
        """RUNNING 来源按当前 run 出示请求即放行,不要求来源先落终态。"""
        self._authorize(self.request)


if __name__ == "__main__":
    unittest.main()
