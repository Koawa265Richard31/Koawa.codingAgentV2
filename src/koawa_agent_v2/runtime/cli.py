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
        "export-legacy-store",
    }:
        if argv[1] in {"export-legacy-store"} or "--config" in argv or "--help" in argv or "-h" in argv:
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
    export_parser = subparsers.add_parser(
        "export-legacy-store", help="offline sanitized legacy-store export"
    )
    export_parser.add_argument("--source", required=True)
    export_parser.add_argument("--destination", required=True)
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
    if arguments.command == "export-legacy-store":
        from .store_migration import (
            LegacyExportError,
            export_legacy_store,
        )

        try:
            report = export_legacy_store(
                arguments.source, arguments.destination
            )
        except LegacyExportError as exc:
            print(
                json.dumps(
                    {"ok": False, "code": exc.code, "payload": {"detail": exc.detail}},
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
            )
            return 1
        print(
            json.dumps(
                {"ok": True, "payload": report},
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        return 0
    # app is closed in the `finally` block below on every exit path (normal,
    # exception and KeyboardInterrupt) for every configured subcommand.
    app = None
    try:
        thinking = _ThinkingDisplay()
        app = AppRuntime.from_config_file(
            arguments.config,
            repo_override=getattr(arguments, "repo", None),
            reasoning_sink=(
                thinking if arguments.command == "interactive" else None
            ),
        )
        if arguments.command == "interactive":
            return _interactive_main(app, thinking=thinking)
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
    finally:
        # I1 ownership chain: every configured entry point (run / resume /
        # cancel / approvals / approve / deny / status / doctor / interactive)
        # closes the AppRuntime on normal return, on exception, and on
        # KeyboardInterrupt (which is not caught above but still reaches this
        # finally).  `_interactive_main` returns through this frame for /exit,
        # EOF, Ctrl-C and turn errors, so every interactive exit path closes.
        if app is not None:
            app.close()


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


class _ThinkingDisplay:
    """Streams model reasoning fragments to stdout with a lazy header."""

    def __init__(self) -> None:
        self.started = False

    def __call__(self, fragment: str) -> None:
        if not self.started:
            print("\n  … 思考: ", end="", flush=True)
            self.started = True
        print(fragment, end="", flush=True)

    def finish(self) -> None:
        if self.started:
            print(flush=True)


def _interactive_main(app, *, thinking: _ThinkingDisplay) -> int:
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
        if text.startswith("/recall "):
            from .session import SessionMemory

            if thread_id is None:
                print("  （还没有会话线程）")
                continue
            hits = SessionMemory(
                app.assembled.store, app.assembled.runtime
            ).recall(thread_id, text[8:].strip())
            if not hits:
                print("  （无命中）")
                continue
            for hit in hits:
                print(f"  [{hit.score:.0f}] {hit.user_input[:80]}")
                if hit.final_text:
                    print(f"      {hit.final_text[:120]}")
                if hit.tools:
                    print(f"      tools: {', '.join(hit.tools)}")
                if hit.files:
                    print(f"      files: {', '.join(hit.files)}")
            continue
        if text == "/journal":
            from .session import SessionHistory, SessionJournal

            if thread_id is None:
                print("  （还没有会话线程）")
                continue
            try:
                turns = SessionHistory.from_thread(
                    app.assembled.store,
                    app.assembled.runtime,
                    thread_id,
                    provider=config.provider.provider,
                ).turns
                target = SessionJournal().write(config.repo, turns)
                print(f"  journal written: {target}")
            except Exception as exc:
                print(f"  journal failed: {getattr(exc, 'code', exc)}")
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

        before_position = _store_position(app)
        calls: dict[str, str] = {}

        def event_sink(event: object) -> None:
            if isinstance(event, ItemCompleted):
                item = getattr(event, "item", None)
                if (
                    item is not None
                    and getattr(item, "kind", None) is OutputKind.TOOL_CALL
                ):
                    calls[item.call_id] = item.name
                    print(f"  → {item.name} {item.arguments_json}", flush=True)

        outcome = app.chat(
            text,
            thread_id=thread_id,
            history=history,
            event_sink=event_sink,
        )
        thinking.finish()
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
                    changed_files=_turn_changed_files(app, before_position),
                )
            )
            drain_approvals()
        elif payload.get("status") == "waiting_for_approval":
            drain_approvals()
        else:
            error_text = payload.get("error") or ""
            print(f"  [turn failed: {outcome.code}]")
            if "resource_budget_exceeded" in error_text:
                limits_map = dict(config.budget_action_limits)
                print(
                    f"  本轮工具动作预算已耗尽（{limits_map.get('root', '?')} 次/轮，"
                    f"可在配置 budget_action_limits 中调大）。建议：把请求写得更具体，"
                    f"例如明确要创建的文件名和内容。"
                )
            else:
                # D22 F6b：回合主体已完成但最终回复失败 → 可见的收尾摘要。
                _print_turn_failure_summary(app, before_position, calls, config)
        _print_tool_trace(app, before_position, calls)
        print(
            f"  [ctx] history_turns={history.turn_count} "
            f"projected_items={len(history.context_items())}"
        )


def _store_position(app) -> int:
    """Current last event position, used to scope diagnostics to one turn."""
    store = app.assembled.store
    cursor = 0
    last = 0  # empty store: read_all(after_position=0) is the whole (empty) store
    while True:
        page = store.read_all(after_position=cursor, limit=500)
        if not page:
            return last
        last = page[-1].global_position
        if len(page) < 500:
            return last
        cursor = page[-1].global_position


def _tool_error_code(content: object) -> str:
    """Extract the stable error code from a tool result payload, if present."""
    if not isinstance(content, str):
        return "tool_error"
    try:
        parsed = json.loads(content)
    except Exception:
        return "tool_error"
    if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
        code = parsed["error"].get("code")
        if isinstance(code, str) and code:
            return code
    return "tool_error"


def _print_tool_trace(app, after_position: int, calls: dict[str, str]) -> None:
    """Print per-tool execution results since a store position (ok / failed).

    Best-effort diagnostics: display must never break the interactive session,
    so any unexpected event shape is skipped instead of raised.
    """
    store = app.assembled.store
    exec_to_call: dict[str, str] = {}
    lines: list[str] = []
    cursor = after_position
    try:
        while True:
            page = store.read_all(after_position=cursor, limit=500)
            if not page:
                break
            for event in page:
                try:
                    payload = event.payload
                    if event.event_type == "tool.execution-prepared.v1":
                        exec_to_call[payload.get("execution_id")] = payload.get(
                            "call_id"
                        )
                    elif event.event_type == "tool.execution-succeeded.v1":
                        name = calls.get(
                            exec_to_call.get(payload.get("execution_id")), "?"
                        )
                        lines.append(f"  ✓ {name}")
                    elif event.event_type == "tool.execution-failed.v1":
                        name = calls.get(
                            exec_to_call.get(payload.get("execution_id")), "?"
                        )
                        code = _tool_error_code(
                            payload.get("result", {}).get("content")
                        )
                        lines.append(f"  ✗ {name} [{code}]")
                except Exception:
                    continue  # skip unparseable events; never crash the session
            if len(page) < 500:
                break
            cursor = page[-1].global_position
    except Exception:
        lines.append("  (工具轨迹读取失败：事件库异常)")
    for line in lines:
        print(line)


def _print_turn_failure_summary(app, after_position: int, calls, config) -> None:
    """D22 F6b：确定性收尾摘要；可选 fallback_summary_model 只用于这一次请求。"""
    try:
        ok_tools: list[str] = []
        exec_to_call: dict[str, str] = {}
        cursor = after_position
        while True:
            page = app.assembled.store.read_all(after_position=cursor, limit=500)
            if not page:
                break
            for event in page:
                try:
                    payload = event.payload
                    if event.event_type == "tool.execution-prepared.v1":
                        exec_to_call[payload.get("execution_id")] = payload.get("call_id")
                    elif event.event_type == "tool.execution-succeeded.v1":
                        call_id = exec_to_call.get(payload.get("execution_id"))
                        name = calls.get(call_id)
                        if name:
                            ok_tools.append(name)
                except Exception:
                    continue
            if len(page) < 500:
                break
            cursor = page[-1].global_position
        if not ok_tools:
            return  # 本回合没有完成任何工具动作：纯失败，无需摘要
        changed_files = _turn_changed_files(app, after_position)
        from .turn_summary import build_turn_summary, summarize_with_model

        text = build_turn_summary(ok_tools, changed_files)
        summary_model = getattr(config, "fallback_summary_model", None)
        if summary_model:
            trace_text = (
                "已执行工具：" + ", ".join(sorted(set(ok_tools)))
                + ("；改动文件：" + ", ".join(changed_files) if changed_files else "")
            )
            try:
                fallback_text, ok = summarize_with_model(
                    app.assembled.client,
                    provider=config.provider.provider,
                    model=summary_model,
                    text=trace_text,
                )
            except Exception:
                fallback_text, ok = None, False
            if ok and fallback_text:
                print(f"  【最终回复失败，已用模型 {summary_model} 生成摘要】")
                print("  " + fallback_text)
                print(f"  （本次摘要仅用该模型一次；下一轮仍使用主模型 {config.provider.model}）")
                return
        print(text)
    except Exception:
        return


def _collect_changed_files(parsed: object) -> set[str]:
    """D22 F4：从工具结果 JSON 收集改动文件（apply_patch changes + git_diff 补集）。"""
    files: set[str] = set()
    if not isinstance(parsed, dict):
        return files
    changed_paths = parsed.get("changed_paths")
    if isinstance(changed_paths, list):
        for path in changed_paths:
            if isinstance(path, str) and path:
                files.add(path)
    changes = parsed.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            path = change.get("path")
            if isinstance(path, str) and path:
                files.add(path)
    return files


def _turn_changed_files(app, after_position: int) -> tuple[str, ...]:
    """本回合改动的文件：apply_patch 结果（权威，含 ADD）+ git_diff 补集。

    D22 F4：文件清单的权威来源是 apply_patch 成功结果的 changes[].path；
    git diff 的 changed_paths 只作补集（内容展示/外部变更检测职责）。
    """
    files: set[str] = set()
    cursor = after_position
    try:
        while True:
            page = app.assembled.store.read_all(after_position=cursor, limit=500)
            if not page:
                break
            for event in page:
                if event.event_type != "tool.execution-succeeded.v1":
                    continue
                content = event.payload.get("result", {}).get("content")
                if not isinstance(content, str):
                    continue
                try:
                    parsed = json.loads(content)
                except Exception:
                    continue
                files.update(_collect_changed_files(parsed))
            if len(page) < 500:
                break
            cursor = page[-1].global_position
    except Exception:
        return ()
    return tuple(sorted(files))


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
