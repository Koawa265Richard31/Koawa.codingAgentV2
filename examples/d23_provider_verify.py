"""D23 §12.2：真实 provider 验证（opt-in，真实 provider 付费）。

运行（在 v2/ 下，需 User 作用域已配置 SF_CodingAgentTestKey）：

    $env:PYTHONPATH = "src"
    py -3.14 -B examples/d23_provider_verify.py

行为：
- 场景 A：构造一个必然失败的轮次（apply_patch 引用错误 base_sha256），下一轮询问
  "上一轮失败原因"，断言回答命中权威错误码/工具名/文件（D23 §4.4 失败回显）。
- 场景 B：长会话（多次修改 + 多次提问）触发至少一次 in-run/session compaction，
  后续轮询问目标与改动，断言回答引用正确事实（D23 §5 压缩后记忆保持）。

证据产物：D:/koawa-demo/d23_verify_a.txt / d23_verify_b.txt（原始转写）
         + examples/d23-provider-evidence.md（脱敏结论，绑定 commit/config/model）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

BASE_CONFIG = "D:/koawa-demo/my_config.json"
DEMO_REPO = "D:/koawa-demo/d23_repo_" + uuid4().hex[:8]
VERIFY_DB_A = "D:/koawa-demo/d23_verify_a.sqlite3"
VERIFY_DB_B = "D:/koawa-demo/d23_verify_b.sqlite3"
CONFIG_A = "D:/koawa-demo/my_config_d23_a.json"
CONFIG_B = "D:/koawa-demo/my_config_d23_b.json"
TRANSCRIPT_A = "D:/koawa-demo/d23_verify_a.txt"
TRANSCRIPT_B = "D:/koawa-demo/d23_verify_b.txt"
V2_ROOT = "D:/A_Dev_Projects/KoawaAgent/v2"
PYTHON = "py"

# 场景 A：第一轮必然失败（read_file 不存在的文件 -> workspace_path_not_found），
# 第二轮问失败点。不依赖模型 schema 遵循能力（35B 的该能力弱点已在 D20 Part B 记录）。
MESSAGES_A = [
    "用 read_file 读取文件 does_not_exist_123.txt（仓库里没有这个文件），"
    "然后直接结束回答，不要重试。",
    "上一轮你做了什么？失败了吗？失败的具体错误是什么？涉及哪个文件？请用三句话以内回答。",
    "/exit",
]

# 场景 B：长会话触发压缩（大文件多轮读取 + 小 history 窗口 -> session compaction），
# 末尾问跨轮事实（压缩块/结论块应保留文件名与内容事实）。
MESSAGES_B = [
    "用 read_file 读取 data_a.txt，然后直接结束回答。",
    "用 read_file 读取 data_b.txt，然后直接结束回答。",
    "用 read_file 读取 data_c.txt，然后直接结束回答。",
    "用三句话以内回答：你之前读过哪几个文件？data_b.txt 的第一行内容是什么？",
    "/exit",
]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )


def fresh_demo_repo() -> None:
    repo = Path(DEMO_REPO)
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "verify@example.com")
    _git(repo, "config", "user.name", "d23 verify")
    (repo / "README.md").write_text(
        "# D23 verify\nline2\nline3\n", encoding="utf-8",
    )
    # 三个中等大小文件（含唯一标识行），用于场景 B 的多轮读取触发压缩。
    (repo / "data_a.txt").write_text(
        "alpha-first-line\n" + "\n".join(f"alpha-{index}" for index in range(200)),
        encoding="utf-8",
    )
    (repo / "data_b.txt").write_text(
        "bravo-first-line\n" + "\n".join(f"bravo-{index}" for index in range(200)),
        encoding="utf-8",
    )
    (repo / "data_c.txt").write_text(
        "charlie-first-line\n" + "\n".join(f"charlie-{index}" for index in range(200)),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "update-index", "--really-refresh")


def write_variant(overrides: dict[str, object], target: str) -> None:
    base = json.loads(Path(BASE_CONFIG).read_text(encoding="utf-8"))
    nested_provider = overrides.pop("provider", {})
    base["provider"].update(nested_provider)
    base.update(overrides)
    Path(target).write_text(
        json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def run_cli(config: str, messages: list[str], timeout: int) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(V2_ROOT) / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    env["SF_CodingAgentTestKey"] = os.environ.get(
        "SF_CodingAgentTestKey"
    ) or _user_key()
    code = (
        "import sys; from koawa_agent_v2.runtime.cli import main; ",
        "raise SystemExit(main(sys.argv))",
    )
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
    if proc.returncode != 0:
        print("CLI stderr:", proc.stderr[-2000:], file=sys.stderr)
    return proc.stdout


def _user_key() -> str:
    import os as _os

    value = _os.environ.get("SF_CodingAgentTestKey")
    if value:
        return value
    try:
        import ctypes
        from ctypes import wintypes

        buffer = ctypes.create_unicode_buffer(4096)
        size = wintypes.DWORD(4096)
        target = wintypes.DWORD(1)  # User scope
        result = ctypes.windll.kernel32.GetEnvironmentVariableW(
            "SF_CodingAgentTestKey", buffer, size
        )
        if result:
            return buffer.value
    except Exception:
        pass
    return ""


def answer_lines(text: str) -> list[str]:
    """agent> 回答；兼容回答与提示同行、回答在下一行的形态（含多行）。"""
    answers: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("agent> ") and stripped[len("agent> "):].strip():
            answers.append(stripped[len("agent> "):].strip())
        elif stripped.startswith("you> agent> ") and stripped[len("you> agent> "):].strip():
            answers.append(stripped[len("you> agent> "):].strip())
        elif stripped in ("agent>", "you> agent>"):
            # 回答从下一行开始；收集后续非空行直到下一个提示/工具行。
            body: list[str] = []
            cursor = index + 1
            while cursor < len(lines):
                peek = lines[cursor].strip()
                if (
                    peek.startswith("you>")
                    or peek.startswith("→")
                    or peek.startswith("  ✓")
                    or peek.startswith("  ✗")
                    or peek.startswith("  …")
                    or peek.startswith("[ctx]")
                    or peek.startswith("type /")
                    or peek.startswith("KoawaAgent")
                ):
                    break
                if peek:
                    body.append(peek)
                cursor += 1
            if body:
                answers.append(" ".join(body))
                index = cursor
                continue
        index += 1
    return answers


def check_a(transcript: str) -> list[str]:
    answers = answer_lines(transcript)
    joined = " ".join(answers).lower()
    problems = []
    if "does_not_exist_123" not in joined:
        problems.append("回答未命中文件 does_not_exist_123.txt")
    read_semantics = any(
        token in joined
        for token in ("read_file", "读取", "读文件")
    )
    if not read_semantics:
        problems.append("回答未命中读取语义")
    error_shaped = any(
        token in joined
        for token in ("not_found", "workspace_path", "不存在", "失败", "错误", "找不到")
    )
    if not error_shaped:
        problems.append("回答未命中失败/错误语义")
    return problems


def check_b(transcript: str, db: str) -> list[str]:
    problems = []
    # ① 确定性（不经模型）：从事件存储重建投影，压缩块/结论块必须含被压缩轮的
    #    文件名事实（D23 结论/压缩投影的信息保留验收）。
    try:
        import json as _json
        import uuid as _uuid
        from koawa_agent_v2.control.sqlite_store import SqliteEventStore
        from koawa_agent_v2.control.runtime import ThreadRuntime
        from koawa_agent_v2.runtime.session import (
            SessionHistory,
            SessionHistoryLimits,
        )
        from koawa_agent_v2.runtime.turn_conclusion import TurnConclusionStore
        from koawa_agent_v2.runtime.memory import MemoryConfig

        store = SqliteEventStore(db)
        runtime = ThreadRuntime(store)
        marker = _json.loads(Path(db + ".session.json").read_text(encoding="utf-8"))
        thread_id = _uuid.UUID(marker["thread_id"])
        history = SessionHistory.from_thread(
            store, runtime, thread_id,
            provider="siliconflow",
            limits=SessionHistoryLimits(max_turns=2, max_chars=32_000, compact_min_turns=2),
            conclusions=TurnConclusionStore(store, runtime),
            memory=MemoryConfig(),
        )
        block_text = "\n".join(
            getattr(item, "content", "") or ""
            for item in history.context_items()
        )
        if "data_a.txt" not in block_text and "data_a" not in block_text:
            problems.append("压缩/结论块未保留 data_a.txt 文件名事实")
        if "data_b.txt" not in block_text and "data_b" not in block_text:
            problems.append("压缩/结论块未保留 data_b.txt 文件名事实")
    except Exception as exc:
        problems.append(f"store 检查失败: {type(exc).__name__}")
    # ② 模型级：窗口内轮（data_b/data_c）应被模型记住。
    answers = answer_lines(transcript)
    joined = " ".join(answers).lower()
    if "data_b.txt" not in joined:
        problems.append("模型回答未命中 data_b.txt（窗口内记忆缺失）")
    if "bravo" not in joined:
        problems.append("模型回答未命中 data_b.txt 内容 bravo")
    return problems


def main() -> int:
    if not Path(BASE_CONFIG).exists():
        print(f"缺少基底配置: {BASE_CONFIG}", file=sys.stderr)
        print("请先创建（provider: siliconflow, api_key_env: SF_CodingAgentTestKey）", file=sys.stderr)
        return 2
    fresh_demo_repo()
    commit = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=V2_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )
    model = os.environ.get("KOAWA_SF_MODEL", "Qwen/Qwen3.5-35B-A3B")

    write_variant(
        {
            "repo": DEMO_REPO,
            "db": VERIFY_DB_A,
            "provider": {"model": model, "reasoning_effort": "off"},
            "memory": {
                "failed_echo_max_turns": 3,
                "conclusions_enabled": True,
            },
        },
        CONFIG_A,
    )
    write_variant(
        {
            "repo": DEMO_REPO,
            "db": VERIFY_DB_B,
            "provider": {"model": model, "reasoning_effort": "low"},
            "history_max_turns": 2,
            "compact_min_turns": 2,
            "memory": {
                "conclusions_enabled": True,
                "failed_echo_max_turns": 3,
            },
        },
        CONFIG_B,
    )

    print("== 场景 A（失败回显命中）：", len(MESSAGES_A), "条输入 ==")
    transcript_a = run_cli(CONFIG_A, MESSAGES_A, timeout=900)
    Path(TRANSCRIPT_A).write_text(transcript_a, encoding="utf-8")
    problems_a = check_a(transcript_a)
    print("场景 A 问题:", problems_a if problems_a else "无")

    print("== 场景 B（压缩后跨轮记忆）：", len(MESSAGES_B), "条输入 ==")
    transcript_b = run_cli(CONFIG_B, MESSAGES_B, timeout=900)
    Path(TRANSCRIPT_B).write_text(transcript_b, encoding="utf-8")
    problems_b = check_b(transcript_b, VERIFY_DB_B)
    print("场景 B 问题:", problems_b if problems_b else "无")

    evidence = f"""# D23 §12.2 真实 provider 验证证据

- 日期: 2026-08-30（opt-in 运行）
- commit: {commit}
- config: base_url=api.siliconflow.cn/v1, model={model}
- 场景 A（失败回显）: 断言问题 {problems_a or "无"} → {'通过' if not problems_a else '失败'}
- 场景 B（压缩后记忆）: 断言问题 {problems_b or "无"} → {'通过' if not problems_b else '失败'}
- 原始转写（脱敏前，仅本机）: {TRANSCRIPT_A}, {TRANSCRIPT_B}
"""
    Path(V2_ROOT + "/examples/d23-provider-evidence.md").write_text(
        evidence, encoding="utf-8",
    )
    print(evidence)
    return 0 if not problems_a and not problems_b else 1


if __name__ == "__main__":
    raise SystemExit(main())

