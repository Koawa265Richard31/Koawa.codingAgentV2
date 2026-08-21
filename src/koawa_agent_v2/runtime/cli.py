"""D15 minimal durable CLI: run / resume / status / cancel / doctor."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from ..agents.graph import AgentError
from ..approval_service import ApprovalService
from ..control.models import TurnStatus
from ..control.runtime import ThreadRuntime
from ..control.sqlite_store import SqliteEventStore
from ..execution.loop import AgentLoop, ToolExecutionContext, ToolExecutionResult
from ..execution.worker import TurnWorker
from ..ledger import (
    IDEMPOTENT_WRITE_PROFILE,
    LedgerExecutor,
    ToolLedgerStore,
)
from ..model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelCallRef,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    OutputKind,
    StreamHeader,
    ToolCallItem,
    ToolResultMessage,
    TurnCompleted,
    TurnStarted,
)
from ..policy import (
    ActionKind,
    Decision,
    PolicyEngine,
    PolicyRule,
    Principal,
    ResolvedAction,
    SideEffectClass,
    canonical_arguments,
)
from ..recovery import CheckpointStore
from ..tools.registry import ToolRegistry
from ..tools.schema import ToolSpec


@dataclass(frozen=True, slots=True)
class PatchArguments:
    path: str
    content: str


PATCH_SPEC = ToolSpec(
    "write_patch",
    "Write one UTF-8 file inside the task repo.",
    PatchArguments,
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 512},
            "content": {"type": "string", "minLength": 1, "maxLength": 100_000},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
)


class FakeProvider:
    """Deterministic provider: one patch call, then a final answer."""

    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo)
        self.rounds = 0

    def stream(self, request: ModelRequest):
        self.rounds += 1
        if self.rounds > 1:
            item = AssistantTextItem(0, "item-final", "done with evidence")
            yield from _completed_stream(
                request,
                (item,),
                FinishReason.STOP,
                "response-final",
            )
            return
        call = ToolCallItem(
            0,
            "item-patch",
            "call-patch",
            "write_patch",
            json.dumps(
                {"path": "out.txt", "content": "patched\n"},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        yield from _completed_stream(
            request,
            (call,),
            FinishReason.TOOL_CALLS,
            "response-patch",
        )


def _header(request: ModelRequest, response_id: str, sequence: int) -> StreamHeader:
    return StreamHeader(
        request.model_turn_id,
        request.provider,
        response_id,
        sequence,
        sequence,
    )


def _completed_stream(
    request: ModelRequest,
    items: tuple,
    finish_reason: FinishReason,
    response_id: str,
) -> tuple[ModelStreamEvent, ...]:
    events: list[ModelStreamEvent] = [
        TurnStarted(_header(request, response_id, 0), request.model)
    ]
    sequence = 1
    for item in items:
        if isinstance(item, ToolCallItem):
            started = ItemStarted(
                _header(request, response_id, sequence),
                item.canonical_index,
                item.item_id,
                OutputKind.TOOL_CALL,
                item.call_id,
                item.name,
            )
        else:
            started = ItemStarted(
                _header(request, response_id, sequence),
                item.canonical_index,
                item.item_id,
                OutputKind.ASSISTANT_TEXT,
            )
        events.append(started)
        sequence += 1
        events.append(ItemCompleted(_header(request, response_id, sequence), item))
        sequence += 1
    turn = ModelTurn(
        request.model_turn_id,
        request.provider,
        request.model,
        response_id,
        tuple(items),
        finish_reason,
    )
    events.append(TurnCompleted(_header(request, response_id, sequence), turn))
    return tuple(events)


def _executor(repo: Path, store, ledger, approvals) -> LedgerExecutor:
    def handler(arguments: PatchArguments, *, context: ToolExecutionContext) -> ToolExecutionResult:
        target = Path(repo) / arguments.path
        target.write_text(arguments.content, encoding="utf-8")
        return ToolExecutionResult(f"wrote {arguments.path}")

    registry = ToolRegistry()
    registry.register(PATCH_SPEC, handler)
    principal = Principal("root", ("workspace.write",))
    engine = PolicyEngine(
        "policy-v1",
        (
            PolicyRule(
                "patch-allow",
                Decision.ALLOW,
                action_kinds=(ActionKind.BUILTIN_TOOL,),
                tool_names=("write_patch",),
                principal_ids=("root",),
                required_scopes=("workspace.write",),
            ),
        ),
    )

    def resolver(call, context, profile, previous):
        return ResolvedAction(
            kind=ActionKind.BUILTIN_TOOL,
            tool_name=call.name,
            canonical_arguments_json=canonical_arguments(call.arguments_json),
            principal=principal,
            side_effect_class=SideEffectClass.IDEMPOTENT_WRITE,
            sandbox_profile_id="d15-write",
            policy_version="policy-v1",
        )

    return LedgerExecutor(
        registry,
        ledger,
        {"write_patch": IDEMPOTENT_WRITE_PROFILE},
        policy_engine=engine,
        approval_service=approvals,
        action_resolvers={"write_patch": resolver},
    )


def run_command(db: Path, repo: Path) -> dict:
    store = SqliteEventStore(db)
    runtime = ThreadRuntime(store, actor="cli")
    ledger = ToolLedgerStore(store)
    approvals = ApprovalService(store, ledger, budget_action_limits={"root": 10})
    thread = runtime.create_thread(f"task-{repo.name}")
    queued = runtime.create_turn(
        thread.thread_id,
        "apply patch",
        expected_thread_version=thread.version,
    )
    result = _worker(runtime, store, ledger, approvals, repo).execute(
        queued.turn_id, queued.version
    )
    return {
        "turn_id": str(queued.turn_id),
        "status": result.turn.status.value,
        "out.txt": (Path(repo) / "out.txt").read_text(encoding="utf-8"),
    }


def _worker(runtime, store, ledger, approvals, repo: Path) -> TurnWorker:
    worker = TurnWorker(
        runtime,
        AgentLoop(
            FakeProvider(repo),
            tool_executor=_executor(repo, store, ledger, approvals),
        ),
        provider="test-provider",
        model="test-model",
        checkpoint_store=CheckpointStore(store),
        owner_id="cli",
    )
    return worker


def resume_command(db: Path, turn_id: str | UUID, repo: Path) -> dict:
    store = SqliteEventStore(db)
    runtime = ThreadRuntime(store, actor="cli-resume")
    ledger = ToolLedgerStore(store)
    approvals = ApprovalService(store, ledger, budget_action_limits={"root": 10})
    resolved = UUID(turn_id) if isinstance(turn_id, str) else turn_id
    turn = runtime.get_turn(resolved)
    if turn.status in (
        TurnStatus.COMPLETED,
        TurnStatus.FAILED,
        TurnStatus.CANCELLED,
    ):
        return {"turn_id": str(resolved), "status": turn.status.value}
    result = _worker(runtime, store, ledger, approvals, repo).execute(
        turn.turn_id, turn.version
    )
    return {"turn_id": str(resolved), "status": result.turn.status.value}


def cancel_command(db: Path, turn_id: str | UUID) -> dict:
    store = SqliteEventStore(db)
    runtime = ThreadRuntime(store, actor="cli-cancel")
    resolved = UUID(turn_id) if isinstance(turn_id, str) else turn_id
    turn = runtime.get_turn(resolved)
    if turn.status in (
        TurnStatus.COMPLETED,
        TurnStatus.FAILED,
        TurnStatus.CANCELLED,
    ):
        return {"turn_id": str(resolved), "status": turn.status.value}
    runtime.cancel_turn(
        turn.turn_id,
        "operator-cancel",
        expected_version=turn.version,
    )
    updated = runtime.get_turn(resolved)
    return {"turn_id": str(resolved), "status": updated.status.value}


def status_command(db: Path) -> dict:
    store = SqliteEventStore(db)
    runtime = ThreadRuntime(store, actor="cli-status")
    turns = _scan_turns(store)
    return {
        "threads": len(_scan_threads(store)),
        "turns": [
            {
                "turn_id": str(turn_id),
                "status": runtime.get_turn(turn_id).status.value,
            }
            for turn_id in turns
        ],
    }


def doctor_command(db: Path) -> dict:
    try:
        store = SqliteEventStore(db)
        runtime = ThreadRuntime(store, actor="doctor")
        probe = runtime.create_thread("doctor-probe")
        queued = runtime.create_turn(
            probe.thread_id,
            "probe",
            expected_thread_version=probe.version,
        )
        running = runtime.start_turn(queued.turn_id, queued.version)
        runtime.get_turn(running.turn_id)
        return {"ok": True, "probe_turn": str(queued.turn_id)}
    except Exception as error:
        raise AgentError("doctor_failed") from None


def _scan_threads(store) -> list:
    threads = []
    cursor = 0
    while True:
        page = store.read_all(after_position=cursor, limit=500)
        for event in page:
            if event.event_type == "thread.created.v1":
                threads.append(__import__("uuid").UUID(event.payload["thread_id"]))
        if len(page) < 500:
            return threads
        cursor = page[-1].global_position


def _scan_turns(store) -> list:
    turns = []
    cursor = 0
    while True:
        page = store.read_all(after_position=cursor, limit=500)
        for event in page:
            if event.event_type == "turn.created.v1":
                turns.append(__import__("uuid").UUID(event.payload["turn_id"]))
        if len(page) < 500:
            return turns
        cursor = page[-1].global_position


def main(argv: list[str] | None = None) -> int:
    """Deterministic legacy entry plus the P0 configured real entry.

    Real mode::

        python -m koawa_agent_v2.runtime.cli run --config cfg.json --task "..."
        python -m koawa_agent_v2.runtime.cli resume --config cfg.json --turn-id UUID
        python -m koawa_agent_v2.runtime.cli status --config cfg.json
        python -m koawa_agent_v2.runtime.cli cancel --config cfg.json --turn-id UUID
        python -m koawa_agent_v2.runtime.cli doctor --config cfg.json

    Legacy deterministic mode is kept for D15 tests and offline demos::

        python -m koawa_agent_v2.runtime.cli run <db> <repo>
    """
    argv = list(sys.argv if argv is None else argv)
    if len(argv) > 1 and argv[1] in {
        "run",
        "resume",
        "status",
        "cancel",
        "doctor",
        "approvals",
        "approve",
        "deny",
        "interactive",
    }:
        if "--config" in argv or "--help" in argv or "-h" in argv:
            return _real_main(argv)
    if len(argv) > 1 and argv[1] in ("--help", "-h"):
        # Top-level help shows the real argparse surface (subcommands + flags).
        return _real_main(argv)
    command = argv[1] if len(argv) > 1 else "doctor"
    db = Path(argv[2]) if len(argv) > 2 else Path("agent.sqlite3")
    if command == "run":
        if len(argv) < 4:
            print("usage: run <db> <repo>")
            return 2
        result = run_command(db, Path(argv[3]))
    elif command == "resume":
        if len(argv) < 5:
            print("usage: resume <db> <turn_id> <repo>")
            return 2
        result = resume_command(db, argv[3], Path(argv[4]))
    elif command == "cancel":
        if len(argv) < 4:
            print("usage: cancel <db> <turn_id>")
            return 2
        result = cancel_command(db, argv[3])
    elif command == "status":
        result = status_command(db)
    elif command == "doctor":
        result = doctor_command(db)
    else:
        print(
            "commands: run <db> <repo> | resume <db> <turn_id> <repo> | "
            "cancel <db> <turn_id> | status <db> | doctor <db>"
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _real_main(argv: list[str]) -> int:
    import argparse

    from .app import AppRuntime
    from .config import RuntimeConfigError

    parser = argparse.ArgumentParser(prog="koawa-agent-v2")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--task", default=None)
    run_parser.add_argument("--task-file", default=None)
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--config", required=True)
    resume_parser.add_argument("--turn-id", required=True)
    cancel_parser = subparsers.add_parser("cancel")
    cancel_parser.add_argument("--config", required=True)
    cancel_parser.add_argument("--turn-id", required=True)
    approvals_parser = subparsers.add_parser("approvals")
    approvals_parser.add_argument("--config", required=True)
    approve_parser = subparsers.add_parser("approve")
    approve_parser.add_argument("--config", required=True)
    approve_parser.add_argument("--request-id", required=True)
    approve_parser.add_argument("--no-resume", action="store_true")
    deny_parser = subparsers.add_parser("deny")
    deny_parser.add_argument("--config", required=True)
    deny_parser.add_argument("--request-id", required=True)
    for name in ("status", "doctor"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--config", required=True)
    interactive_parser = subparsers.add_parser(
        "interactive", help="conversational session over one repo"
    )
    interactive_parser.add_argument("--config", required=True)
    interactive_parser.add_argument(
        "--repo",
        default=None,
        help="override the configured repo (e.g. '.' for the current directory)",
    )

    arguments = parser.parse_args(argv[1:])
    try:
        app = AppRuntime.from_config_file(
            arguments.config,
            repo_override=getattr(arguments, "repo", None),
        )
        if arguments.command == "interactive":
            return _interactive_main(app)
        if arguments.command == "run":
            task = _read_task(arguments)
            outcome = app.run(task)
        elif arguments.command == "resume":
            outcome = app.resume(arguments.turn_id)
        elif arguments.command == "cancel":
            outcome = app.cancel(arguments.turn_id)
        elif arguments.command == "approvals":
            outcome = app.pending_approvals()
        elif arguments.command == "approve":
            outcome = app.resolve_approval(
                arguments.request_id,
                True,
                resume_after=not arguments.no_resume,
            )
        elif arguments.command == "deny":
            outcome = app.resolve_approval(
                arguments.request_id,
                False,
                resume_after=False,
            )
        elif arguments.command == "status":
            outcome = app.status()
        else:
            outcome = app.doctor()
        print(outcome.to_json())
        return 0 if outcome.ok else 1
    except RuntimeConfigError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "payload": {}}))
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "code": getattr(exc, "code", "runtime_error"), "payload": {}}))
        return 1


_INTERACTIVE_HELP = """commands:
  <message>             run one agent turn (conversation context is kept)
  /status               show threads, turns and pending approvals
  /approvals            list pending durable approvals
  /approve <id>         approve a pending request (and resume)
  /deny <id>            deny a pending request
  /resume <turn-id>     resume a paused/interrupted turn
  /history              show the current in-memory session history
  /thread <uuid>        switch to (or create) a conversation thread
  /help                 this help
  /exit                 save session and quit
EOF (Ctrl+Z) and Ctrl+C also quit cleanly."""


def _interactive_main(app) -> int:
    import json as _json
    from uuid import UUID as _UUID

    from .session import (
        SessionHistory,
        SessionHistoryError,
        SessionHistoryLimits,
        SessionTurn,
        summarize_via_client,
    )

    config = app.config
    limits = SessionHistoryLimits(
        max_turns=config.history_max_turns,
        max_chars=config.history_max_chars,
        compact_min_turns=config.compact_min_turns,
    )
    summarize = None
    if hasattr(app.assembled.client, "_endpoint"):
        summarize = lambda text: summarize_via_client(
            app.assembled.client,
            provider=config.provider.provider,
            model=config.provider.model,
            text=text,
        )
    marker = Path(str(config.db) + ".session.json")
    thread_id: _UUID | None = None
    if marker.exists():
        try:
            thread_id = _UUID(
                _json.loads(marker.read_text(encoding="utf-8"))["thread_id"]
            )
        except Exception:
            thread_id = None

    def save_marker() -> None:
        if thread_id is None:
            return
        marker.write_text(
            _json.dumps({"thread_id": str(thread_id)}, ensure_ascii=False),
            encoding="utf-8",
        )

    history = SessionHistory(
        provider=config.provider.provider,
        limits=limits,
        summarize=summarize,
    )
    if thread_id is not None:
        try:
            history = SessionHistory.from_thread(
                app.assembled.store,
                app.assembled.runtime,
                thread_id,
                provider=config.provider.provider,
                limits=limits,
                summarize=summarize,
            )
        except SessionHistoryError:
            thread_id = None

    def drain_approvals() -> None:
        pending = app.pending_approvals().payload.get("pending_approvals", [])
        for item in pending:
            answer = input(f"approve {item['request_id']}? [y/N] ").strip().lower()
            result = app.resolve_approval(
                item["request_id"], answer in ("y", "yes")
            )
            print(result.to_json())

    print(
        f"KoawaAgent V2 interactive session\n"
        f"  repo : {config.repo}\n"
        f"  db   : {config.db}\n"
        f"  model: {config.provider.model} (reasoning_effort={config.provider.reasoning_effort})\n"
        f"  thread: {thread_id or 'new (created on first message)'}\n"
        f"type /help for commands"
    )
    while True:
        try:
            line = input("you> ")
        except EOFError:
            print("\nbye")
            save_marker()
            return 0
        except KeyboardInterrupt:
            print("\nbye")
            save_marker()
            return 0
        text = line.strip()
        if not text:
            continue
        if text in ("/exit", "/quit"):
            save_marker()
            return 0
        if text == "/help":
            print(_INTERACTIVE_HELP)
            continue
        if text == "/status":
            print(app.status().to_json())
            continue
        if text == "/approvals":
            print(app.pending_approvals().to_json())
            continue
        if text == "/history":
            print(_json.dumps(
                {
                    "turns": history.turn_count,
                    "projected_items": len(history.context_items()),
                    "compacted_blocks": len(history.maybe_compact()),
                },
                ensure_ascii=False,
            ))
            continue
        if text.startswith("/approve "):
            print(app.resolve_approval(text[9:].strip(), True).to_json())
            continue
        if text.startswith("/deny "):
            print(app.resolve_approval(text[6:].strip(), False, resume_after=False).to_json())
            continue
        if text.startswith("/resume "):
            print(app.resume(text[8:].strip()).to_json())
            continue
        if text.startswith("/thread "):
            try:
                thread_id = _UUID(text[8:].strip())
                history = SessionHistory.from_thread(
                    app.assembled.store,
                    app.assembled.runtime,
                    thread_id,
                    provider=config.provider.provider,
                    limits=limits,
                    summarize=summarize,
                )
                print(f"switched to thread {thread_id}")
            except (ValueError, SessionHistoryError) as exc:
                print(f"cannot switch thread: {getattr(exc, 'code', exc)}")
            continue

        outcome = app.chat(text, thread_id=thread_id, history=history)
        payload = outcome.payload
        final_text = payload.get("final_text") or ""
        print(("agent> " + final_text).rstrip() if final_text else f"agent> [{outcome.code}] {payload.get('error')}")
        if thread_id is None and payload.get("thread_id"):
            thread_id = _UUID(payload["thread_id"])
            save_marker()
        if outcome.ok and payload.get("turn_id"):
            history.append(
                SessionTurn(
                    user_input=text,
                    final_text=final_text or None,
                    turn_id=_UUID(payload["turn_id"]),
                    status=payload.get("status"),
                    error=payload.get("error"),
                )
            )
            drain_approvals()
        elif payload.get("status") == "waiting_for_approval":
            drain_approvals()
        else:
            print(f"  [turn failed: {outcome.code}]")


def _read_task(arguments) -> str:
    if arguments.task and arguments.task_file:
        raise SystemExit("use only one of --task or --task-file")
    if arguments.task is not None:
        return arguments.task
    if arguments.task_file:
        return Path(arguments.task_file).read_text(encoding="utf-8")
    return (
        "Read the repository, make the failing tests pass, verify with the "
        "configured test profile, inspect git status and diff, then finalize."
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
