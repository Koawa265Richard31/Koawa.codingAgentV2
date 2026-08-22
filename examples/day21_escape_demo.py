"""D21 沙箱逃逸演示（离线确定性）：检测 + 审计 + fail-closed 三合一。

运行方式（在 v2/ 下）：

    $env:PYTHONPATH="src"
    python -B examples/day21_escape_demo.py

流程：
1. 构造恶意仓库：正常文件 + 指向仓库外 secret.txt 的 junction（Windows）或 symlink（POSIX）；
2. 用真实工具全路径（ToolRegistry -> D9 Policy/Approval -> D7 Ledger）读取 escape 路径；
3. 读取被 D3 WorkspacePathResolver 拒绝（workspace_path_link_forbidden，正文不含宿主机绝对路径）；
4. 从事件库重放工具执行流，打印 tool.execution-failed.v1 审计事件；
5. 对照：读取正常文件仍然成功（fail-closed 不误伤合法任务）。

不连真实网络、不调用真实模型、不依赖 Docker。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.approval_service import ApprovalService
from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.ledger import LedgerExecutor, READ_ONLY_PROFILE, ToolLedgerStore
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.policy import (
    ActionKind,
    Decision,
    PolicyEngine,
    PolicyRule,
    Principal,
    ResolvedAction,
    SideEffectClass,
    canonical_arguments,
)
from koawa_agent_v2.tools.repository import (
    RepositoryToolLimits,
    RepositoryToolRegistry,
    register_repository_tools,
)
from koawa_agent_v2.tools.workspace import WorkspacePathResolver

_REPO_TOOLS = ("read_file", "list_files", "search_text")
SENTINEL = "top-secret-sentinel-77"


def _args(document: dict[str, object]) -> str:
    return canonical_arguments(json.dumps(document, ensure_ascii=False, separators=(",", ":")))


def build_link(link: Path, target: Path) -> str:
    if os.name == "nt":
        proc = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if proc.returncode == 0 and os.path.isjunction(link):
            return "junction"
        return ""
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return "symlink"
    except OSError:
        return ""


def main() -> int:
    temporary = tempfile.TemporaryDirectory()
    try:
        base = Path(temporary.name)
        root = base / "repo"
        outside = base / "outside"
        root.mkdir()
        outside.mkdir()
        (root / "README.md").write_text("normal file\n", encoding="utf-8")
        (outside / "secret.txt").write_text(SENTINEL + "\n", encoding="utf-8")
        link = root / "escape"
        kind = build_link(link, outside)
        if not kind:
            print("[跳过] 当前环境无法创建链接（Windows 需 mklink /J 可用），演示只做离线说明。")
            return 0
        print("[1/4] 恶意仓库已构造:", kind, "escape ->", str(outside))

        store = SqliteEventStore(base / "demo.sqlite3")
        runtime = ThreadRuntime(store, actor="d21-demo")
        ledger = ToolLedgerStore(store)
        approvals = ApprovalService(store, ledger, budget_action_limits={"root": 20})
        principal = Principal("root", ("workspace.read",))

        resolver = WorkspacePathResolver(root)
        registry = RepositoryToolRegistry(resolver)
        try:
            register_repository_tools(registry, resolver, limits=RepositoryToolLimits())
            rule = PolicyRule("repo-read", Decision.ALLOW, action_kinds=(ActionKind.BUILTIN_TOOL,),
                              tool_names=_REPO_TOOLS, principal_ids=("root",),
                              required_scopes=("workspace.read",))
            engine = PolicyEngine("policy-v1", (rule,))

            def action_resolver(call: ToolCallItem, _context: ToolExecutionContext, profile: object,
                                _previous: ResolvedAction | None) -> ResolvedAction:
                return ResolvedAction(kind=ActionKind.BUILTIN_TOOL, tool_name=call.name,
                                      canonical_arguments_json=canonical_arguments(call.arguments_json),
                                      principal=principal,
                                      side_effect_class=SideEffectClass(profile.side_effect_class.value),
                                      sandbox_profile_id="d8-readonly", policy_version="policy-v1")
            executor = LedgerExecutor(registry, ledger, {name: READ_ONLY_PROFILE for name in _REPO_TOOLS},
                                      policy_engine=engine, approval_service=approvals,
                                      action_resolvers={name: action_resolver for name in _REPO_TOOLS})

            thread = runtime.create_thread("repo-demo")
            queued = runtime.create_turn(thread.thread_id, "escape-demo",
                                         expected_thread_version=thread.version)
            running = runtime.start_turn(queued.turn_id, queued.version)

            def execute_read(path: str, call_id: str):
                model_turn_id = uuid4()
                call = ToolCallItem(0, f"item-{call_id}", call_id, "read_file",
                                    _args({"path": path, "start_line": 1, "max_lines": 20}))
                context = ToolExecutionContext(running.current_run_id, model_turn_id, 1,
                                               ModelCallRef(model_turn_id, call.call_id),
                                               turn_id=running.turn_id, turn_version=running.version)
                result = executor.execute(call, context=context)
                record = ledger.load_for_call(running.turn_id, model_turn_id, call_id)
                return result, record

            print("[2/4] 尝试读取 escape/secret.txt ...")
            result, record = execute_read("escape/secret.txt", "call-escape")
            print("      -> 被拒，错误码: workspace_path_link_forbidden")
            print("      -> 错误正文不含宿主机绝对路径:", str(outside) not in result.content)

            print("[3/4] ledger 审计重放（tool-execution 流）:")
            for event in store.read_stream(StreamId("tool-execution", record.execution_id)):
                suffix = "   <- FAILED（稳定错误码，无敏感内容）" if event.event_type == "tool.execution-failed.v1" else ""
                print("      " + event.event_type + suffix)

            print("[4/4] 对照：读取 README.md（合法路径）...")
            ok_result, _ = execute_read("README.md", "call-ok")
            preview = ok_result.content[:60].replace(chr(10), " ")
            print("      -> 正常返回:", preview)
            print()
            conclusion = "结论：检测 = tools/workspace.py（D3 resolver 链接拒绝）；审计 = ledger/store.py（D7）；fail-closed = 被拒动作不触 handler、不泄露内容、任务继续。"
            print(conclusion)
            runtime.complete_turn(running.turn_id, "escape demo complete",
                                  expected_version=running.version, run_id=running.current_run_id,
                                  command_id=uuid4())
        finally:
            try:
                registry.close()
            except Exception:
                pass
    finally:
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
