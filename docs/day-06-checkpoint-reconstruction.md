# D6 — Checkpoint, Context Reconstruction, and Stale Run Recovery

D6 makes the model transcript restartable without claiming that arbitrary external side effects are exactly once. The append-only event log remains the source of truth; a checkpoint is only a verified acceleration projection.

## Durable records

Each Turn has a separate `run-execution-<turn_id>` stream. It stores only complete canonical facts:

- `run.context-seeded.v1`: trusted instructions and user context;
- `model.turn-completed.v1`: the complete ModelTurn, projected context items, counters, and next phase;
- `run.phase-advanced.v1`: notably the boundary immediately before a tool executes;
- `tool.result-recorded.v1`: a complete ToolResult associated with its ModelCallRef.

Raw SSE deltas, partial tool arguments, provider transport objects, hidden reasoning, process handles, and environment dumps never enter this stream. Public reasoning summaries are explicit model-visible items and are distinct from hidden reasoning.

Before persistence, D6 recursively redacts credential-shaped fields and common text forms such as Bearer values, `sk-*` keys, and `token`/`password`/`secret` assignments. The live model call can still use the in-memory value; the event stream and checkpoint retain only the redacted projection.

Execution facts are not placed in the D1 Turn stream. Every execution append uses a `StreamPrecondition` that, in the same SQLite transaction, checks the exact Turn version, latest `turn.started.v1`, and current `run_id`. After stale-run requeue, the old Worker cannot append a ModelTurn, ToolResult, phase, or final state.

## Checkpoint validation and tail replay

The checkpoint records thread/turn/run identity, Turn and execution versions, schema version, counters, phase, canonical context, and the covered event's global position, commit ID, and SHA-256. On restart:

1. Parse the checkpoint with an exact schema.
2. Read and hash the exact covered event.
3. Reject mismatched identity, version, global position, commit, or hash.
4. If valid, rebuild from the checkpoint and replay the execution tail.
5. If missing, truncated, unknown, or corrupt, replay all typed execution facts.

Unknown execution fact types, version gaps, or invalid payloads fail closed. Reads paginate beyond 500 events.

## Lease and takeover

The D6 Worker commits `turn.started.v1`, a complete `run.context-seeded.v1`, the recoverable index, and an already-active owner lease in one SQLite transaction. There is therefore no post-start/pre-context window before the first model call. The low-level D1 `start_turn` remains compatible; if a legacy caller dies before creating any execution fact, recovery can only restart from the durable original user input and the new Worker's configured instructions.

A background `LeaseKeeper` renews with SQLite's database clock. Heartbeat is CAS-protected by Turn, Run, owner, generation, version, and unexpired time.

The recovery coordinator lists candidates without knowing a Turn ID. A live lease rejects takeover. An expired lease can be abandoned through an idempotent recovery command which atomically checks the Turn/run/version fences, appends `turn.stale-run-requeued.v1`, invalidates the old lease, and updates the index. A normal Worker then competes to start the QUEUED Turn, producing a fresh attempt and `run_id`.

If the coordinator commits that requeue and dies before dispatching the Worker, claiming the already-QUEUED candidate again is idempotent. Waiting/paused Turns are removed from automatic discovery; an explicit input/approval resume re-adds the queued candidate. The response is injected as one stable `UserMessage`, committed in the next atomic seed, and is not duplicated by a later crash.

## Safe resume boundary before D7

Safe automatic resume includes:

- before a model call;
- after a complete ModelTurn and before a tool starts;
- after a complete ToolResult;
- after a final ModelTurn, where the persisted final is committed without calling the model again;
- between calls in a multi-tool ModelTurn, where only pending calls execute.

Immediately before each tool call, D6 persists `TOOL_IN_PROGRESS`. If the process dies before its result is durable, recovery changes the projection to `BLOCKED_UNCERTAIN_SIDE_EFFECT`. D6 does not retry it automatically. D7 must add a side-effect ledger and reconciliation protocol.

## Four different recovery concepts

| Concept | What it restores | What it cannot prove |
|---|---|---|
| Transcript/context reconstruction | Canonical model-visible messages and pending calls | Filesystem or remote side-effect outcome |
| Checkpoint | A faster verified starting point for replay | Truth independent of the event log |
| File rollback | D4 in-process reversal of a failed multi-file commit | Recovery after the process dies mid-replace |
| Side-effect recovery | D7 ledger claim/result/reconciliation | Exactly-once behavior from D6 alone |

## Failure coverage

The D6 tests cover atomic start/context/lease, legacy pre-model fallback, event-before-checkpoint death, valid checkpoint plus committed tail, truncated/unknown/hash-bad checkpoints, live heartbeat versus takeover, expired and repeated queued takeover, old heartbeat/final rejection, finalization without model replay, remaining-tool-only resume, exactly-once input/approval injection, persisted credential redaction, and tool-in-progress blocking.

In addition to same-process fault tests, a parent test forcibly kills real child Worker processes at six durable boundaries: after atomic start, after a model event but before checkpoint, after checkpoint, ready-for-tool, tool-in-progress, and after a tool result checkpoint.

Run the restart demonstration:

```powershell
$env:PYTHONPATH='src'
python -B examples/day06_checkpoint_restart_resume.py
```
