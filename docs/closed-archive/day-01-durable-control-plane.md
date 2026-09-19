# Day 01: Durable control plane

## What this slice proves

D1 turns the words **thread**, **turn**, **run**, and **resume** into explicit persistence semantics before an LLM or a tool is introduced. Durable checkpoints and snapshots are a later slice.

- A **Thread** is the long-lived coding conversation attached to a workspace reference.
- A **Turn** is one user request. It can execute more than once because recovery starts a new run attempt.
- A **Run** is one worker attempt to execute a turn. Every `QUEUED -> RUNNING` transition receives a fresh `run_id`.
- An **Event Stream** is the append-only history of one aggregate. Its exact version is the concurrency token.

The database is the source of truth. There is no in-memory session object that must survive a process restart.

## Write path

```text
command
  -> look up semantic command receipt (safe retry or conflicting reuse)
  -> rebuild aggregate from its stream
  -> validate expected version, state transition and run fence
  -> create immutable JSON events
  -> append with exact expected stream version
  -> commit all affected streams in one SQLite transaction
  -> rebuild and return the new state
```

Creating and finishing a turn affect two aggregates, so both operations use an atomic multi-stream batch:

```text
create turn:   thread.turn-attached.v1 + turn.created.v1
finish turn:   turn.completed.v1       + thread.turn-detached.v1
```

If either expected version is stale, neither stream changes.

## Durable invariants

1. An empty stream has version `-1`; its first event has version `0`.
2. Callers cannot append with an unspecified or wildcard version.
3. A thread has at most one active root turn.
4. A terminal turn cannot transition or resume.
5. Resuming a wait requires the current interrupt identifier and persists the
   supplied input or boolean approval decision.
6. Resume queues work; it does not pretend that an old worker process continued.
7. Every new run attempt has a new `run_id` and increments `attempt`.
8. Every worker-owned transition supplies both the caller's `expected_version` and
   `run_id`; an old worker cannot finish a replacement run.
9. Reusing a command ID with identical semantic arguments returns the original
   command result, even after the aggregate advances. Reusing it with different
   arguments fails.
10. Every event in an atomic batch records `commit_id`, `commit_index`, and
    `commit_size`, so a paged projection can detect and buffer an incomplete commit.
11. Persisted payloads and metadata are immutable JSON snapshots. Runtime objects,
    process handles, credential fields, and hidden reasoning are outside the event
    contract. User-controlled text may itself contain sensitive data; enforcement
    and redaction are not claimed by D1.

## Crash and concurrency semantics

SQLite uses WAL mode and `BEGIN IMMEDIATE` for a write batch. Version checks, event insertion, stream-head updates, and the semantic command receipt commit together. A crash before commit leaves no visible part of the batch; a retry after commit reconstructs the result at the versions recorded in the original receipt.

Optimistic concurrency decides competing commands. If twenty writers all expect version `-1`, one creates version `0` and the other nineteen receive `WrongExpectedVersion`. After recovery, both a stale stream version and a stale `run_id` are rejected; this prevents an old worker from committing a replacement worker's result.

This does **not** make arbitrary tools exactly-once. A shell command may perform an external side effect and then lose its result before the runtime records it. D7 introduces a tool-execution ledger, fencing/idempotency identifiers, and an explicit `OUTCOME_UNKNOWN` state for that failure window.

## Why an event log instead of only mutable rows?

A mutable `turn.status` row answers what the latest state is, but discards how it got there. An append-only log gives deterministic reconstruction, auditability, failure injection points, and a natural basis for later checkpoints and projections. Global readers receive an ordered event sequence plus explicit commit boundaries; they must not publish a projection until all events in a commit have arrived. A snapshot may optimize replay later, but it must never replace the authoritative history.

## Interview answers

**What is resume?**

Resume is a validated state transition. The runtime replays durable events, verifies the stream version and pending interrupt, and durably records the input or approval decision in `turn.recovery-queued.v1`. A later `start_turn` creates a fresh fenced run attempt. It is not continuation of a Python stack frame.

**Why exact expected versions?**

They prevent lost updates without holding a database lock while business logic runs. The decision was made against state version `n`; if another command advances it, the stale decision must be retried from the new history.

**What does the idempotency key protect?**

The Runtime fingerprints the command name, identifiers, arguments, and caller versions. The receipt records result stream versions, so an identical retry returns the original result rather than today's newer state; conflicting reuse fails. Callers that need retry safety must retain and resend their `command_id`. The worker/process label is not part of the fingerprint, so a replacement process can perform the retry. This does not deduplicate side effects outside the database transaction.

**Why update the thread and turn in one batch?**

Otherwise a crash could leave a thread pointing at a missing turn, or a completed turn still attached as active. The multi-stream transaction preserves the cross-aggregate invariant.

**Why are both version and run ID required?**

The version rejects any decision based on stale aggregate state. The run ID is a fencing token: even if an obsolete worker somehow learns the latest version, it cannot publish output for the replacement worker's run.

## Deliberate D1 limits

- There is no model loop, tool execution, scheduler, worker lease, or automatic
  discovery of unfinished turns yet.
- The walkthrough resumes a known `turn_id`; persistent indexes and operational
  discovery arrive with later control-plane slices.
- There is no checkpoint or snapshot yet. D1 replays the authoritative event log;
  D6 adds checkpoint/compaction policy.
- Database command retries are idempotent. External tool side effects are not;
  D7 adds the tool ledger and uncertain-outcome recovery.
- User text is stored as requested. Credential classification/redaction belongs to
  the later security slice.

## Acceptance command

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
```

The test suite covers stale versions, semantic Runtime retries, conflicting command reuse, old-worker fencing, duplicate event identifiers, injected mid-batch rollback, immutable JSON snapshots, commit boundaries, twenty concurrent writers, process-style store restart, fail-closed schema replay, interrupt validation, repeated run attempts, terminal-state guards, and atomic thread/turn updates.
