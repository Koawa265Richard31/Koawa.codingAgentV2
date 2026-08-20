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


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "doctor"
    db = Path(argv[2]) if len(argv) > 2 else Path("agent.sqlite3")
    if command == "run":
        result = run_command(db, Path(argv[3]))
    elif command == "resume":
        result = resume_command(db, argv[3], Path(argv[4]))
    elif command == "cancel":
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
