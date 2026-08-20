# KoawaAgent V2

KoawaAgent V2 is an independent, production-oriented local Coding Agent runtime. It lives in the same Git repository as the legacy Java project, but it does not import from or modify that implementation.

The project is built as 15 executable slices. A slice is complete only when its production code, failure-path tests, runnable demonstration, and design notes agree.

## Final execution path

```text
durable Thread / Turn
  -> model stream and native tool calls
  -> repository context
  -> policy and approval
  -> built-in or MCP tool
  -> isolated worktree and Docker sandbox
  -> patch, test, diff and deterministic finalization
  -> checkpoint, crash recovery and trace
  -> optional isolated subagents
```

## Fifteen-day route

| Day | Deliverable |
|---|---|
| D1 | Thread, Turn, append-only Event Store |
| D2 | Typed model streaming protocol and Agent Loop |
| D3 | Tool Registry plus bounded Read/Search |
| D4 | Atomic Apply Patch |
| D5 | Test/Git/Diff finalization loop |
| D6 | Checkpoint and state reconstruction |
| D7 | Tool ledger, crash windows and idempotent recovery |
| D8 | Docker Sandbox |
| D9 | Approval plus resource/network restrictions |
| D10 | MCP lifecycle and secure tool calls |
| D11 | Multi-Agent control plane |
| D12 | Worktree and per-agent container isolation |
| D13 | Repository context and compaction |
| D14 | Trace, evaluation and failure injection |
| D15 | End-to-end acceptance and interview package |

## Executable roadmap and new-session handoff

The complete implementation contract is
[`docs/15-day-coding-agent-roadmap.md`](docs/15-day-coding-agent-roadmap.md).
It records the current status, dependency graph, per-day interfaces, failure matrices,
acceptance gates, prohibited shortcuts, and the exact startup procedure for a new
conversation. Read it before starting the next slice; this README is only the index.

## Current state: D1–D15 complete

D1 remains the durable control plane: append-only Thread/Turn streams, exact-version
writes, semantic command receipts, and per-attempt `run_id` fencing. D2 adds the
trusted model-execution boundary on top:

- Provider-neutral typed requests, context items, output items, and stream events.
- A strict assembler that rejects sequence gaps, identity changes, invalid item
  lifecycles, delta/snapshot mismatches, missing terminals, and oversized streams.
- A bounded Agent Loop that validates the whole model response before any tool
  side effect, feeds typed tool results into the next model round, and requires a
  non-empty final answer.
- A real standard-library OpenAI-compatible `/chat/completions` SSE adapter.
- A `TurnWorker` that starts a fenced D1 Run and commits COMPLETED, FAILED, or
  CANCELLED without allowing a stale worker to overwrite newer state.

D3 supplies the production read-only implementation behind that port:

- A sealed `ToolRegistry` whose provider definitions, runtime validation, and frozen
  typed arguments all come from one `ToolSpec`.
- A workspace resolver that rejects cross-platform path escapes and descendant
  symlink/junction/reparse points, and reads through verified OS handles.
- Deterministic, separately bounded `read_file`, `list_files`, and `search_text`
  tools with stable JSON failures and truncation metadata.
- A real temporary-repository walkthrough that performs search → read → final and
  then replays the completed D1 Turn from SQLite.

D4 now adds the first real write path:

- A versioned structured Patch protocol for exact Add/Update/Delete operations.
- Mandatory base SHA-256 for Update/Delete and exact, non-fuzzy line hunks.
- UTF-8 BOM, LF/CRLF and final-newline preservation rules with hard file/line budgets.
- A same-workspace mutation lock, full preflight, same-directory staging, per-target
  hash/identity revalidation, backup/replace and reverse rollback.
- Stable `workspace_outcome_unknown` when rollback cannot be proven, rather than a
  false atomic-success claim.
- One combined sealed Registry and a real read → multi-file patch → reread/list →
  final walkthrough.

D5 now turns those tools into an evidence-backed coding vertical slice:

- The model can only select immutable, pre-registered test profiles; it cannot provide
  argv, cwd, environment variables, or shell syntax.
- The dev-only host runner has a trust gate, minimal environment, bounded stdout/stderr,
  total timeout, cooperative cancellation, and process-tree termination.
- A fixed read-only Git facade separates the user's dirty baseline from Agent changes
  and disables hooks, fsmonitor, pager, external diff, and textconv executors.
- Each successful Patch advances a verification generation. Tests, status, and diff
  must all describe the current generation before `finalize_task` can issue a report.
- `AgentLoop` has a completion gate, so a model final answer cannot bypass the report.

D6 now persists complete canonical ModelTurns, ToolResults, phase transitions, and
context in a fenced run-execution stream. Verified checkpoints support tail replay;
database-clock leases and a recoverable index let a new process discover and requeue
stale RUNNING turns with a fresh run ID. Completed finals and remaining tool calls can
continue without replaying completed work. A tool that may have executed without a
durable result is explicitly handed to D7 recovery rather than blindly replayed.

D7 adds a write-ahead Tool Ledger around every durable tool call:

- A logical execution ID survives process, attempt, and run changes; physical claims
  use separate run ID, epoch, and token fences.
- PREPARED and CLAIMED are appended under the active Turn's exact-version/current-run
  precondition before a handler may run.
- Read-only, idempotent, queryable non-idempotent, and manual recovery profiles make
  replay policy a trusted Runtime declaration rather than a model choice.
- Definite results, terminal negative queries, and OUTCOME_UNKNOWN can converge under
  exact ledger-version and claim-token checks.
- Durable Workers reject a raw Registry/executor bypass, and recovered redacted
  credentials are never executed as literal placeholders.
- Real child-process kills cover the five physical ledger boundaries; cancel-versus-
  claim is decided by the shared Turn CAS.

D8 moves untrusted test execution into a real Docker container:

- Runtime accepts only a complete immutable image ID and uses pull=never.
- Every allocation persists an intent before create, then binds a complete container
  ID; restart recovery uses exact labels, a 256-bit nonce, and a deterministic name.
- Containers run non-root with a read-only rootfs/workspace, network none, no added
  capabilities or privilege escalation, bounded PID/CPU/memory/time/output, and a
  bounded tmpfs.
- Create-before-bind process death is recovered from SQLite without a leaked
  container; unknown, cloned, or label-tampered containers are refused.
- D5 final evidence records Docker backend, image, profile, allocation, and container
  identities. The host runner remains bootstrap/test only.

D9 adds one policy and durable-approval boundary in front of every durable handler:

- Built-in, future MCP, and future Agent-spawn actions share local
  ALLOW / DENY / ASK decisions with deny-by-default and scope intersection.
- A canonical action digest binds arguments, resolved resource identities, side-effect
  class, sandbox profile, network/credential scope, budget, policy version, and
  authenticated principal.
- Registry schema validation happens before policy. ASK persists an approval request
  and D1 wait in one commit, while legacy boolean resume never creates authority.
- Grant/deny recovery is durable. Before execution the action is resolved again; drift
  asks again instead of carrying an old grant to a new resource.
- Single-use grant consumption, durable budget reservation, D7 claim, and active-Run
  fencing share one SQLite transaction.
- HTTPS/DNS/redirect/proxy rules are fail-closed pure policy in D9. D8 containers remain
  network=none; no real egress is opened by this slice.
- Network enablement requires a configured origin allowlist and a mandatory proxy;
  MCP credentials bind to trusted server identity.
- Process-local authorized tickets are single-use and tamper-evident; every exit
  path discards the prepared invocation, and terminal results are re-authorized
  against durable claim evidence before being released.

D10 connects real MCP servers as dynamic tool sources on the same chain:

- stdio JSON-RPC 2.0 transport with Content-Length framing, initialize/initialized,
  tools/list pagination, tools/call concurrency, and list_changed refresh.
- Every binding fixes `(server_id, session_generation, tool_name, schema_hash)`;
  the tuple enters the D9 action digest and the D7 logical execution identity,
  so refresh invalidates old approvals and old in-flight bindings.
- Server schemas are strictly validated before Registry binding; descriptions,
  annotations, and results are untrusted and fail closed / redacted.
- Timeouts, wrong response ids, EOF, and malformed frames map to
  OUTCOME_UNKNOWN for non-idempotent profiles instead of blind retries.
- A real fixture MCP server runs as a subprocess for tests and the walkthrough;
  no direct bypass around Registry/Policy/Ledger exists.

D11 adds the durable multi-agent control plane:

- Event-sourced parent/child graph with CREATED/RUNNING/WAITING/COMPLETED/FAILED/
  CANCELLED/ORPHANED states, per-attempt run ids, leases, and exact-version
  fences; stale runs can never commit messages or terminal results.
- Durable mailboxes with per-agent sequences and semantic idempotency keys;
  depth/concurrency/total-agent budgets are reserved and released atomically.
- Orphan discovery and takeover restart from a new process; cancel propagates
  through typed messages; failure of one worker never blocks its siblings.
- D11 workers are hard-limited to the read-only tool allowlist and model
  inference; writes, patch, tests, host commands, network, and MCP are refused.

D12 isolates every writing agent in its own Git worktree and gates delivery:

- Durable per-agent workspace inventory under a managed root with a safe reaper;
  write agents refuse a dirty user baseline by default.
- Artifacts carry run fence, base/head hashes, diff, test evidence, and image
  digest; integration applies them serially, retests in a container runner,
  and delivers to the user workspace only when HEAD still matches the base.
- Same-line edits raise an explicit conflict; no partial or per-artifact writes
  to the main workspace ever happen.
- `DockerContainerRunner` is wired to the D8 `DockerCommandRunner`, running each
  worktree's tests in a read-only-mounted Linux container (0-skip with Docker up).

D13 adds bounded repository context and lossless compaction:

- Git-ignored, binary, oversized, and over-budget files are excluded before
  retrieval; every result carries path, SHA-256, line range, source, and score.
- Stale hashes are re-validated so changed files never reach the model.
- Compaction keeps system/developer instructions verbatim, marks summaries as
  untrusted, and rebuilds goals/constraints/approval/unknown outcomes/children/
  budgets from typed projections; open tool calls block compaction.

D14 adds trace, deterministic failure injection, and a 20-task eval harness:

- Traces use stream/field allowlists and pre-persist redaction; raw secrets or
  full bodies never touch SQLite.
- Twelve named failure points replay deterministically and classify failures
  into timeout/concurrency/policy/uncertainty/contract buckets.
- `evals/run_eval.py` grades fresh-repo tasks with an oracle plus test command
  and emits a failure-classification report.
- Trace is wired into the model (AgentLoop), tool/ledger (LedgerExecutor), and
  MCP (McpSession) production paths via optional `trace_store`/`correlation_id`.

D15 ships the minimal durable CLI and the interview package:

- `run / resume / status / cancel / doctor` commands drive one durable task
  through Registry → Policy → Ledger → patch → evidence final, and rebuild or
  resume state from SQLite.
- The dispatch-contract tests prove the built-in and MCP entries refuse raw
  execution once policy-bound (subagent entry refused via D11 write allowlist);
  D6/D7/D8 kill fixtures cover real subprocess crash windows.

## Run D1 through D15 tests

PowerShell:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B -m unittest discover -s tests -v
```

Run the D5 offline failure → repair → test → diff → finalization walkthrough:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B examples/day05_coding_vertical_slice.py
```

Run the D6 destroy → discover → resume walkthrough:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B examples/day06_checkpoint_restart_resume.py
```

Run the D7 claim → crash/restart → result reuse/unknown walkthrough:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B examples/day07_tool_ledger_recovery.py
```

Run the D8 real-container security probe and create-before-bind recovery walkthrough:

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B examples/day08_docker_sandbox.py
~~~

Run the D9 ASK → restart → grant/consume walkthrough and offline network-policy probes:

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B examples/day09_durable_approval.py
~~~

Run the D10 real MCP fixture roundtrip and ledger uncertainty walkthrough:

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B examples/day10_mcp_roundtrip.py
~~~

Run the D11 durable multi-agent read-only walkthrough:

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B examples/day11_multi_agent_readonly.py
~~~

Run the D12 isolated writing-agents walkthrough:

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B examples/day12_isolated_writing_agents.py
~~~

Run the D13 indexed-context and compaction walkthrough:

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B examples/day13_context_compaction.py
~~~

Run the D14 trace/failure-replay walkthrough and the 20-task eval:

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B examples/day14_trace_failure_replay.py
python -B evals/run_eval.py evals/tasks evals/report.json
~~~

Run the D15 CLI and E2E contract tests:

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -W error::ResourceWarning -B -m unittest tests.test_d15_e2e -v
~~~

The D5 trust boundary, verification generations, Git hardening, failure matrix, and
interview explanation are in
[`docs/day-05-test-git-finalization.md`](docs/day-05-test-git-finalization.md).
The D6 checkpoint, lease, tail-replay, and side-effect boundaries are in
[`docs/day-06-checkpoint-reconstruction.md`](docs/day-06-checkpoint-reconstruction.md).
The D7 logical identity, claim fencing, recovery classes, and six crash windows are in
[`docs/day-07-tool-ledger-crash-windows.md`](docs/day-07-tool-ledger-crash-windows.md).
The D8 container boundary, allocation ledger, exact reaper, and attack matrix are in
[docs/day-08-docker-sandbox-threat-model.md](docs/day-08-docker-sandbox-threat-model.md).
The D9 action identity, durable approval transaction, network policy, and budget
boundaries are in
[docs/day-09-policy-approval-network.md](docs/day-09-policy-approval-network.md).
The D10 MCP lifecycle, binding identity, failure matrix, and error codes are in
[docs/day-10-mcp-lifecycle-security.md](docs/day-10-mcp-lifecycle-security.md).
The D11 agent graph, mailbox, budget, fences, and orphan recovery are in
[docs/day-11-multi-agent-control-plane.md](docs/day-11-multi-agent-control-plane.md).
The D12 worktree/container isolation and gated delivery are in
[docs/day-12-worktree-container-isolation.md](docs/day-12-worktree-container-isolation.md).
The D13 context budget, staleness, and compaction are in
[docs/day-13-repository-context-compaction.md](docs/day-13-repository-context-compaction.md).
The D14 trace, eval, and failure injection are in
[docs/day-14-trace-eval-failure-injection.md](docs/day-14-trace-eval-failure-injection.md).
The D15 CLI, E2E matrix, and interview package are in
[docs/day-15-interview-package.md](docs/day-15-interview-package.md).
The D4 transaction phases and crash boundaries remain in
[`docs/day-04-atomic-apply-patch.md`](docs/day-04-atomic-apply-patch.md).
The D3 read-only implementation rationale remains in
[`docs/day-03-tool-registry-read-search.md`](docs/day-03-tool-registry-read-search.md).
The D2 model boundary remains documented in
[`docs/day-02-model-stream-agent-loop.md`](docs/day-02-model-stream-agent-loop.md).
The D1 persistence notes remain in
[`docs/day-01-durable-control-plane.md`](docs/day-01-durable-control-plane.md).
