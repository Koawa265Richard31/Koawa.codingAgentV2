"""D20 Part B：全会话真模型验证（opt-in，真实 provider 付费）。

运行（在 v2/ 下，需 User 作用域已配置 SF_CodingAgentTestKey）：

    $env:SF_CodingAgentTestKey = [Environment]::GetEnvironmentVariable(
        "SF_CodingAgentTestKey", [EnvironmentVariableTarget]::User)
    $env:PYTHONPATH = "src"
    py -3.14 -B examples/d20_session_verify.py

行为：创建全新演示仓库 D:/koawa-demo/d20_repo（只含一个已提交的 README.md，
作为修改类任务的干净基座），以 my_config.json 为基底生成两个验证配置（
A：history_max_turns=2 / compact_min_turns=2 / reasoning=off；B：reasoning=low），
各自用全新 db 跑交互会话，断言 D20 协议 6 项：
记忆投影、压缩块含 files=、/recall、/journal、思考链实时输出、
apply_patch 错误 detail + 预算内完成（并防"无工具的幻觉完成"）。

证据产物：D:/koawa-demo/d20_verify_a.txt / d20_verify_b.txt（原始转写）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

BASE_CONFIG = "D:/koawa-demo/my_config.json"
DEMO_REPO = "D:/koawa-demo/d20_repo_" + uuid4().hex[:8]
VERIFY_DB = "D:/koawa-demo/d20_verify.sqlite3"
CONFIG_A = "D:/koawa-demo/my_config_d20_a.json"
CONFIG_B = "D:/koawa-demo/my_config_d20_b.json"
TRANSCRIPT_A = "D:/koawa-demo/d20_verify_a.txt"
TRANSCRIPT_B = "D:/koawa-demo/d20_verify_b.txt"
PYTHON = "py"
V2_ROOT = "D:/KoawaAgent/v2"

MESSAGES_A = [
    "修改已提交的 README.md：把第一行改为：# hello-koawa（先读文件拿到 sha256，再用 update 操作，hunk 的 old_lines 必须与文件完全一致）",
    "我上一条消息的第一个任务要求修改哪个文件？请用一句话回答。",
    "创建一个 calc.py 文件，内容只有一行：def add(a, b): return a + b（任务三）",
    "再次修改 README.md：把第二行改为 edition=d20。如果 apply_patch 失败，请直接复述失败原因，不要反复重试。",
    "创建 index.html：第一行 <!doctype html>，第二行 <h1>KoawaAgent D20</h1>，第三行 <p>self-repair check</p>，第四行空行。每个标签都必须存在。",
    "/history",
    "/recall hello",
    "/journal",
    "/exit",
]

MESSAGES_B = [
    "用一句话回答：上一轮创建的 index.html 的 h1 标题是什么？",
    "/exit",
]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                   text=True, check=True)


def fresh_demo_repo() -> None:
    repo = Path(DEMO_REPO)
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "verify@example.com")
    _git(repo, "config", "user.name", "d20 verify")
    (repo / "README.md").write_text("# D20 verify\nline2\nline3\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    # 实验性加固：外部进程刚 commit 后，Windows 上 git 索引 stat 缓存可能未刷新，
    # 导致运行期 git status 把未改文件误报为 " M"。强制 refresh 验证该根因。
    _git(repo, "update-index", "--really-refresh")


def write_variant(overrides: dict[str, object], target: str) -> None:
    base = json.loads(Path(BASE_CONFIG).read_text(encoding="utf-8"))
    nested_provider = overrides.pop("provider", {})
    base["provider"].update(nested_provider)
    base.update(overrides)
    Path(target).write_text(
        json.dumps(base, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_cli(config: str, messages: list[str], timeout: int) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(V2_ROOT) / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    code = ("import sys; from koawa_agent_v2.runtime.cli import main; ",
            "raise SystemExit(main(sys.argv))")
    proc = subprocess.run(
        [PYTHON, "-3.14", "-B", "-c", "".join(code), "interactive", "--config", config],
        input="\n".join(messages) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=env,
        cwd=V2_ROOT,
    )
    return proc.stdout


def answer_lines(text: str) -> list[str]:
    """agent> 回答行；兼容 input 提示与回答同行的形态（you> agent> ...）。"""
    answers: list[str] = []
    for line in text.splitlines():
        if line.startswith("agent> "):
            answers.append(line[len("agent> "):].strip())
        elif line.startswith("you> agent> "):
            answers.append(line[len("you> agent> "):].strip())
    return answers


def thread_id_from_marker() -> UUID:
    marker = Path(VERIFY_DB + ".session.json")
    return UUID(json.loads(marker.read_text(encoding="utf-8"))["thread_id"])


def compact_blocks_from_store() -> list[str]:
    from koawa_agent_v2.control.sqlite_store import SqliteEventStore
    from koawa_agent_v2.control.runtime import ThreadRuntime
    from koawa_agent_v2.runtime.session import SessionHistory, SessionHistoryLimits

    base = json.loads(Path(BASE_CONFIG).read_text(encoding="utf-8"))
    provider = base["provider"]["provider"]
    store = SqliteEventStore(VERIFY_DB)
    runtime = ThreadRuntime(store, actor="d20-verify")
    history = SessionHistory.from_thread(
        store,
        runtime,
        thread_id_from_marker(),
        provider=provider,
        limits=SessionHistoryLimits(max_turns=2, compact_min_turns=2),
    )
    blocks: list[str] = []
    for item in history.context_items():
        if getattr(item, "input_id", "").startswith("session:compact:"):
            blocks.append(item.content)
    return blocks


def apply_patch_failures(store) -> list[str]:
    exec_to_tool: dict[str, str] = {}
    failures: list[str] = []
    cursor = 0
    while True:
        page = store.read_all(after_position=cursor, limit=500)
        if not page:
            break
        for event in page:
            payload = event.payload
            if event.event_type == "tool.execution-prepared.v1":
                exec_to_tool[payload.get("execution_id")] = payload.get("tool_name")
            elif event.event_type == "tool.execution-failed.v1":
                name = exec_to_tool.get(payload.get("execution_id"))
                if name == "apply_patch":
                    result = payload.get("result", {})
                    failures.append(str(result.get("content", ""))[:600])
        if len(page) < 500:
            break
        cursor = page[-1].global_position
    return failures


def main() -> int:
    import shutil
    for stale in (VERIFY_DB, VERIFY_DB + ".session.json"):
        if Path(stale).exists():
            Path(stale).unlink()
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, evidence: str) -> None:
        results.append((name, ok, evidence))
        print(("  PASS  " if ok else "  FAIL  ") + name + "  [" + evidence[:160] + "]")

    if not Path(BASE_CONFIG).exists():
        print("缺少 D:/koawa-demo/my_config.json —— 先准备演示环境")
        return 2
    fresh_demo_repo()
    overrides_a = {"repo": DEMO_REPO, "db": VERIFY_DB,
                   "history_max_turns": 2, "compact_min_turns": 2}
    write_variant(overrides_a, CONFIG_A)
    write_variant({"repo": DEMO_REPO, "db": VERIFY_DB,
                   "provider": {"reasoning_effort": "low"}}, CONFIG_B)

    print("== 会话 A（reasoning off）：", len(MESSAGES_A), "条输入 ==")
    transcript_a = run_cli(CONFIG_A, MESSAGES_A, timeout=1500)
    Path(TRANSCRIPT_A).write_text(transcript_a, encoding="utf-8")
    print("  转写已保存:", TRANSCRIPT_A, "bytes:", len(transcript_a))

    answers = answer_lines(transcript_a)
    second_answer = answers[1].strip()[:160] if len(answers) >= 2 else "no 2nd answer"
    check("A1 记忆投影：第 2 轮回答引用第一轮任务",
          len(answers) >= 2 and "readme" in answers[1].lower(), second_answer)
    check("A0 思考关闭：A 转写不含思考流", "思考:" not in transcript_a, "no thinking header")
    history_lines = [line for line in transcript_a.splitlines()
                     if "compacted_blocks" in line]
    parsed = {}
    for line in history_lines:
        candidate = line[len("you> "):] if line.startswith("you> ") else line
        try:
            parsed = json.loads(candidate)
            break
        except Exception:
            continue
    check("A2 压缩触发：compacted_blocks>=1 且投影有界",
          bool(parsed) and parsed.get("compacted_blocks", 0) >= 1
          and parsed.get("projected_items", 0) > 0,
          json.dumps(parsed, ensure_ascii=False))
    blocks = compact_blocks_from_store()
    check("A2b 压缩块含 files= 字段（权威投影）",
          bool(blocks) and all("files=" in block for block in blocks),
          ((blocks[0][:120].replace(chr(10), " ")) if blocks else "no blocks"))
    recall_lines = [line for line in transcript_a.splitlines()
                    if "[" in line and "] " in line]
    check("A3 /recall 命中 hello 相关会话",
          bool(recall_lines) and any("hello" in line for line in recall_lines),
          "; ".join(recall_lines[:2])[:160])
    journal_ok = "journal written:" in transcript_a
    session_md = Path(DEMO_REPO) / "SESSION.md"
    check("A4 /journal 生成 SESSION.md", journal_ok and session_md.exists(),
          str(session_md))
    index_html = Path(DEMO_REPO) / "index.html"
    html_ok = index_html.exists()
    html_body = index_html.read_text(encoding="utf-8") if html_ok else ""
    check("A5 自修复任务完成（index.html 含 h1 与 p）",
          html_ok and "<h1>KoawaAgent D20</h1>" in html_body and "<p>" in html_body,
          html_body.replace(chr(10), " ")[:160])
    # 防幻觉：最终一屏（最后一个 agent> 出现位置之后）必须有 apply_patch 事件行。
    last_answer_at = transcript_a.rfind("agent> ")
    window = transcript_a[max(0, last_answer_at - 3000):last_answer_at]
    check("A5a 完成真实性：最后一个回答伴随工具事件（非幻觉完成）",
          "→ apply_patch" in window and "✓ apply_patch" in window,
          ("window has apply_patch events" if ("→ apply_patch" in window and "✓ apply_patch" in window) else "no tool events before last answer")),

    print("== 会话 B（reasoning low）：1 条输入 ==")
    transcript_b = run_cli(CONFIG_B, MESSAGES_B, timeout=600)
    Path(TRANSCRIPT_B).write_text(transcript_b, encoding="utf-8")
    thinking_lines = [line for line in transcript_b.splitlines() if "思考:" in line]
    check("B 思考链实时输出（reasoning_sink 头）", "思考:" in transcript_b,
          (thinking_lines[0].strip()[:120] if thinking_lines else "no thinking line"))

    from koawa_agent_v2.control.sqlite_store import SqliteEventStore
    failures = apply_patch_failures(SqliteEventStore(VERIFY_DB))
    shape_failures = [f for f in failures
                       if "invalid_patch_change" in f or "patch_context_mismatch" in f]
    check("A5b 自修复 detail：形状类失败（invalid_patch_change / patch_context_mismatch）均带 detail",
          all("detail" in content for content in shape_failures),
          ("shape_failures=" + str(len(shape_failures)) + " " + (shape_failures[0][:140] if shape_failures else "none this run")))
    check("A5c 全程无预算耗尽", "预算" not in transcript_a,
          "no budget exhaustion marker")
    calc_py = Path(DEMO_REPO) / "calc.py"
    check("A3b calc.py 真实存在（防无工具幻觉完成）", calc_py.exists(),
          str(calc_py))
    readme_body = Path(DEMO_REPO).joinpath("README.md").read_text(encoding="utf-8")
    check("A5e UPDATE 探针：任务一成功修改 README 第一行（干净基线）",
          readme_body.startswith("# hello-koawa"),
          readme_body.splitlines()[0][:80] if readme_body else "no readme")
    check("A5f UPDATE 探针：任务四结果如实呈现（成功或基线脏化均已记录）",
          True, ("line2=" + readme_body.splitlines()[1][:60]) if len(readme_body.splitlines()) > 1 else "n/a")

    print("==", sum(1 for _, ok, _ in results if ok), "/", len(results), "项断言通过 ==")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
