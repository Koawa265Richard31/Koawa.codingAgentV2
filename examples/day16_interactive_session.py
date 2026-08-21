"""D16 interactive session walkthrough (offline, deterministic).

Shows the conversational loop over one thread: each user message becomes a
turn, prior turns are projected into the model context under a bounded
whitelist, and compaction kicks in once enough old turns fall out of the
window. Uses a scripted provider, so this example never touches the network.

Run:

    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONPATH = 'src'
    py -3.14 -B examples/day16_interactive_session.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, "src")

from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    OutputKind,
    StreamHeader,
    TurnCompleted,
    TurnStarted,
)
from koawa_agent_v2.runtime.app import AppRuntime
from koawa_agent_v2.runtime.config import (
    PolicyConfig,
    ProviderConfig,
    RepositoryTrustMode,
    RuntimeConfig,
    SandboxConfig,
    SandboxRunner,
    TestProfileConfig,
)
from koawa_agent_v2.runtime.session import (
    SessionHistory,
    SessionHistoryLimits,
    SessionTurn,
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo() -> Path:
    temporary = tempfile.mkdtemp(prefix="koawa-d16-example-")
    repo = Path(temporary) / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "d16@example.com")
    _git(repo, "config", "user.name", "D16 Example")
    _git(repo, "config", "core.fsmonitor", "false")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "core.filemode", "false")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _header(request: ModelRequest, response_id: str, sequence: int) -> StreamHeader:
    return StreamHeader(
        request.model_turn_id, request.provider, response_id, sequence, sequence
    )


def _text_stream(
    request: ModelRequest,
    text: str,
    response_id: str,
) -> tuple[ModelStreamEvent, ...]:
    item = AssistantTextItem(0, f"chat-text:{response_id}", text)
    started = ItemStarted(_header(request, response_id, 1), 0, item.item_id, OutputKind.ASSISTANT_TEXT)
    turn = ModelTurn(request.model_turn_id, request.provider, request.model, response_id, (item,), FinishReason.STOP)
    return (
        TurnStarted(_header(request, response_id, 0), request.model),
        started,
        ItemCompleted(_header(request, response_id, 2), item),
        TurnCompleted(_header(request, response_id, 3), turn),
    )


class _ScriptedProvider:
    """Answers every turn with a fixed text; records how many times called."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, request: ModelRequest) -> tuple[ModelStreamEvent, ...]:
        self.calls += 1
        return _text_stream(request, f"answer #{self.calls}", f"r{self.calls}")


def main() -> int:
    repo = _make_repo()
    config = RuntimeConfig(
        repo=repo,
        db=Path(repo.parent) / "agent.sqlite3",
        provider=ProviderConfig(
            base_url="http://127.0.0.1:1/v1",
            api_key_env="D16_EXAMPLE_KEY",
            model="scripted",
        ),
        sandbox=SandboxConfig(
            runner=SandboxRunner.HOST,
            host_trust=RepositoryTrustMode.BUILTIN_FIXTURE,
        ),
        test_profiles=(
            TestProfileConfig(
                "unit",
                (str(Path(sys.executable).resolve()), "-B", "-m", "unittest"),
                timeout_seconds=30,
            ),
        ),
        policy=PolicyConfig(),
        system_prompt="You are KoawaAgent V2 in an interactive session.",
        # Small window so the example visibly triggers compaction.
        history_max_turns=2,
        history_max_chars=10_000,
        compact_min_turns=2,
    )
    app = AppRuntime(config, model_client=_ScriptedProvider())
    limits = SessionHistoryLimits(
        max_turns=config.history_max_turns,
        max_chars=config.history_max_chars,
        compact_min_turns=config.compact_min_turns,
    )
    history = SessionHistory(provider=config.provider.provider, limits=limits)
    print(f"repo: {repo}")
    print(f"history window: max_turns={limits.max_turns} "
          f"compact_min_turns={limits.compact_min_turns}")
    thread_id: UUID | None = None
    prompts = ("inspect calc.py", "what does add() do?", "explain the bug", "summarize")
    for message in prompts:
        outcome = app.chat(message, thread_id=thread_id, history=history)
        payload = outcome.payload
        thread_id = UUID(payload["thread_id"])
        history.append(
            SessionTurn(
                user_input=message,
                final_text=payload.get("final_text"),
                turn_id=UUID(payload["turn_id"]),
                status=payload.get("status"),
                error=payload.get("error"),
            )
        )
        blocks = history.maybe_compact()
        print(f"\n[user]  {message}")
        print(f"[agent] {payload.get('final_text')}")
        print(f"[ctx]   projected items={len(history.context_items())} "
              f"compacted_blocks={len(blocks)} turns={history.turn_count}")
    print(f"\nprovider calls (model rounds): {app.assembled.client.calls}")
    print("compaction block preview:")
    for block in history.maybe_compact():
        print(block.authoritative[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
