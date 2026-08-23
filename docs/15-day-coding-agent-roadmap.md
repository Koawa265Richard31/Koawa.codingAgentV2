# KoawaAgent V2：D1–D15 可执行开发与学习规划书

> 本文件是 KoawaAgent V2 后续开发的唯一长期路线事实源。它不是愿望清单，
> 而是给一个完全不了解历史聊天的新会话使用的执行合同。
>
> 快照日期：2026-08-21。当前状态：D1–D15 全部完成。

## 1. 最终目标与可声明边界

D15 结束时，项目应形成一个单机、本地、可恢复的 Coding Agent：它能读取真实
Git 仓库，调用流式模型，选择并执行 Coding Tools，修改文件，在 Docker 容器中
运行验证，检查 diff，处理审批，调用 MCP，并让隔离子 Agent 协作，最后给出带证据
的完成报告。

最终主链必须真实贯通：

```text
CLI / API
  -> durable Thread / Turn / Run
  -> canonical model stream
  -> bounded repository context
  -> Tool Registry
  -> Policy / Approval
  -> Tool Ledger claim
  -> built-in tool or MCP
  -> per-run / per-agent Docker + worktree isolation
  -> patch -> tests -> status/diff -> bounded repair
  -> checkpoint / crash recovery
  -> trace / eval
  -> evidence-backed final
```

D15 可以声称：

- 完整本地 Coding Agent 工作流，而不是只有 ReAct `while` 循环的演示；
- 模型流、工具、持久化、恢复、沙箱、审批、MCP、多 Agent 都进入同一条执行链；
- 对已知故障窗口有明确状态、测试和恢复策略；
- 任意旧 Worker、旧 Run、旧 Agent 不能提交新状态。

D15 仍不能声称：

- 任意外部副作用都 exactly-once；不可查询的外部系统仍可能得到
  `OUTCOME_UNKNOWN`；
- 强一致分布式调度、跨机器高可用或企业多租户权限系统；
- 已发生的进程、网络或外部系统副作用能被“取消”撤回；
- 所有模型 Provider、所有 MCP transport 都天然兼容。

## 2. 新会话必须先执行的接手流程

任何新会话开始开发前，先执行以下命令，不要依赖旧聊天记忆：

```powershell
Set-Location D:\KoawaAgent\v2
Get-Content .\AGENTS.md
Get-Content .\docs\15-day-coding-agent-roadmap.md
Get-Content .\README.md
git -C D:\KoawaAgent branch --show-current
git -C D:\KoawaAgent status --short

$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = "src"
python -W error::ResourceWarning -B -m unittest discover -s tests -v
```

接手者必须先确认：

1. 仓库根目录是 `D:\KoawaAgent`，V2 唯一实现范围是
   `D:\KoawaAgent\v2\**`。
2. 不导入、不修改仓库根部旧 Java 项目；除非用户在新任务中明确改变范围。
3. 先查看 dirty worktree。现有修改属于用户，不覆盖、不清理、不 reset。
4. 当前基线应为 332 个测试通过（Docker 可用时 3 个平台能力 skip；Docker 不可用环境额外 11 个 Docker skip）；测试数后续可以增加，所以“全部通过”比固定数字
   更重要。若基线失败，先报告并定位，不能在坏基线上继续下一日。
5. 当前只实施状态表中第一个 `NEXT` 切片。不提前创建未来日期的空模块。
6. “一天”表示一个可执行交付切片，不表示为了日历时间可以删除验收条件。

给新会话的推荐开场提示词见本文最后一节。

## 3. 当前事实快照

### 3.1 工程约束

- Python `>=3.12`，当前 `pyproject.toml` 的 `dependencies=[]`；优先标准库。
- 所有持久化数据必须是 JSON；禁止 pickle、runtime client、进程句柄、凭据、
  完整环境变量和隐藏思维链。
- 每个状态变更必须有 typed command/event，并受精确 stream version 约束。
- 外部副作用不是天然 exactly-once；不能用 `run_id` fence 冒充副作用账本。
- 每个切片必须同时有生产代码、失败路径测试、可运行示例和中文设计说明。

### 3.2 状态表

| Slice | 状态 | 已有或目标证据 |
|---|---|---|
| D1 | COMPLETE | Event Store、Thread/Turn/Run、25 个 D1 tests，D1 文档/示例 |
| D2 | COMPLETE | typed model stream、Agent Loop、真实 SSE client、D2 文档/示例；D2 完成时全量 79 tests |
| D3 | COMPLETE | ToolSpec/Registry、安全路径边界、bounded Read/List/Search、D3 文档/示例；全量 132 tests |
| D4 | COMPLETE | Atomic Apply Patch；全量 150 tests、D4 文档/示例 |
| D5 | COMPLETE | 固定测试 profile、安全 Git facade、verification generation、Finalizer；全量 157 tests |
| D6 | COMPLETE | typed execution facts、verified Checkpoint + tail replay、lease/heartbeat、stale Run recovery；当前全量 177 tests |
| D7 | COMPLETE | Tool Ledger、stable execution identity、六个 crash windows、query/manual unknown resolution；当前全量 191 tests |
| D8 | COMPLETE | 真 Docker sandbox、allocation intent/exact reaper、26 个 D8 tests；当前全量 217 tests |
| D9 | COMPLETE | Policy、durable approval、resource/network controls；42 个 D9 tests、D9 文档/示例；当前全量 259 tests |
| D10 | COMPLETE | MCP lifecycle + secure calls；38 个 D10 tests、真实 fixture 集成、D10 文档/示例；当前全量 297 tests |
| D11 | COMPLETE | Durable Multi-Agent control plane；12 个 D11 tests、D11 文档/示例；当前全量 309 tests |
| D12 | COMPLETE | Per-Agent worktree + container isolation；真实 Docker runner 已接入 D8；7 个 D12 tests、D12 文档/示例 |
| D13 | COMPLETE | Repository context + compaction；5 个 D13 tests、D13 文档/示例；当前全量 320 tests |
| D14 | COMPLETE | Trace、eval、failure injection；trace 已接入 model/tool/ledger/mcp；4 个 D14 tests + 20 任务 eval、D14 文档/示例 |
| D15 | COMPLETE | End-to-end acceptance + interview package；CLI 含 run/resume/status/cancel/doctor/approvals/approve/deny；真实 OpenAI-compatible provider 装配；builtin+MCP dispatch 契约、统一运行时；当前全量 333 tests |
| D16 | COMPLETE | Interactive session：thread=会话、有界历史投影（白名单+截断）+ 会话压缩（权威投影+可选模型摘要）；行内审批；--repo 自选工作目录；10 个 D16 tests、离线示例；当前全量 362 tests |
| D17 | COMPLETE | 异常修复：预算可配置（budget_action_limits）+ 非 git 仓库清晰报错（not_a_git_repository）+ CLI 失败解释；思考链显示通道（reasoning_sink，只显示不入库）；5 个新测试；文档 day-17-exception-and-thinking.md |
| D18 | COMPLETE | 交互轨迹：interactive 实时打印 → 工具调用、✓/✗ 结果、[ctx] 上下文规模；2 个新测试；文档 day-18-interactive-trace.md |
| D19 | COMPLETE | 会话记忆增强：压缩 v2（权威投影含改动的文件集合）、阶梯④词法检索召回（/recall，位置作用域圈定工具名）、阶梯⑤ session journal（/journal → SESSION.md）；4 个 D19 tests；文档 day-19-session-memory-enhancement.md |
| D20 | COMPLETE | 工具错误增强（patch detail + 参数 expected/example，错误码稳定）：6 个 D20 tests；Part B 全会话真模型验证（35B×5 + 122B 对比×2，11/14 断言），证据 examples/d20_session_verification.md；归因：模型能力 ×2（F1 幻觉完成 / F5 记忆遵循）、运行时 ×3（F2 UPDATE 基线瞬态误判 / F3 错误无指引 / F4 files= 缺口）、端点 ×1（F6 invalid_completed_snapshot），建议 D22 专项 |
| D21 | COMPLETE | Agent 安全真实性包：威胁模型 docs/agent-security-threat-model.md（T1-T6 映射 OWASP + 诚实边界）、评估矩阵 tests/test_d21_agent_security.py（8 确定性用例全绿）、事故加固实录 docs/agent-security-engineering.md、逃逸演示 examples/day21_escape_demo.py。落地文档 day-21-detailed-implementation.md |
| D22 | COMPLETE | 真实会话健壮性加固（参考 codex-cli-code / deepseekharness）：F2 UPDATE 授权改内容锚定（根因：facade 置空配置使 autocrlf 失效 → CRLF 文件被确定性判脏，改为 git diff --ignore-space-at-eol）、F6 空完成归一+零输出单次重试、F6b 收尾摘要（确定性 + 可选 fallback_summary_model，request-scoped 不切换会话模型）、F1 交互完成门（claimed_change_without_tool）、F4 changed_files 权威来源改 apply_patch 结果、F3 baseline_dirty 补 detail。设计 day-22-robustness-hardening.md；30 新用例全绿 |

状态变更规则：只有当该日的 Definition of Done 全部成立，才允许把该行改为
`COMPLETE`，并把下一行改为 `NEXT`。

### 3.3 当前源码分包

根包只保留稳定公共入口 `koawa_agent_v2/__init__.py`，生产实现按切片职责组织：

- `control/`：D1 Event Store、Thread/Turn reducer 与 Runtime；
- `model/`：D2 provider-neutral 协议、stream assembler 与 OpenAI-compatible transport；
- `execution/`：D2 Agent Loop 与 Turn Worker；
- `tools/`：D3 schema、Registry、workspace 边界与只读仓库工具；
- `editing/`：D4 Patch 协议、原子事务与写工具；
- `verification/`：D5 trusted runner、Git、verification ledger 与 finalization；
- `recovery/`：D6 execution facts、checkpoint、lease 与恢复协调器；
- `ledger/`：D7 logical tool execution、physical claim、result/unknown resolution。
- `sandbox/`：D8 immutable protocol、allocation Event Store、Docker runner 与 exact reaper。

### 3.4 D1 已完成的真实能力

生产文件：

- `src/koawa_agent_v2/control/event_store.py`
- `src/koawa_agent_v2/control/sqlite_store.py`
- `src/koawa_agent_v2/control/models.py`
- `src/koawa_agent_v2/control/runtime.py`

已形成：SQLite append-only Event Store、跨 stream 原子 commit、commit 分页边界、
semantic command receipt、精确 expected version、Thread/Turn reducer、Run attempt、
`run_id` fencing、wait/approval/pause/resume/complete/fail/cancel/timeout 状态迁移。

D1 尚未形成：模型上下文 checkpoint、snapshot、僵死 `RUNNING` 自动发现、Worker
lease、工具调用和外部副作用去重。

### 3.5 D2 已完成的真实能力

生产文件：

- `src/koawa_agent_v2/model/protocol.py`
- `src/koawa_agent_v2/model/stream.py`
- `src/koawa_agent_v2/execution/loop.py`
- `src/koawa_agent_v2/model/openai_client.py`
- `src/koawa_agent_v2/execution/worker.py`

已形成：Provider-neutral request/context/output 类型、连续 sequence、完整 item
lifecycle、delta/done/terminal/usage 一致性、严格 JSON tool arguments、有界 Agent
Loop、完整 response 校验后才执行工具、真实 OpenAI-compatible Chat Completions SSE、
HTTPS/redirect/secret 防护、协作取消、总 stream deadline，以及 D1 TurnWorker 接缝。

D2 尚未形成：Tool Registry、真实 Coding Tools、持久化 ModelTurn/ToolResult、
checkpoint resume、Tool Ledger、sandbox、MCP、多 Agent。`ToolExecutor` 只是 D3 的端口。

已知边界：ownership guard 到真实工具副作用之间仍有 TOCTOU；正在阻塞的网络读取
最多要等 socket timeout 才观察到取消。前者由 D7 收口，后者是有界但非即时取消。

## 4. 全局 Definition of Done

每个 Dn 必须同时满足：

1. **生产实现**：没有空壳、TODO 伪实现或只为测试存在的主链。
2. **正常闭环**：至少一个示例通过真实组件跑通，不以全 mock 冒充集成。
3. **失败闭环**：关键故障有稳定错误码、明确持久状态和是否可重试结论。
4. **安全边界**：输入、输出、路径、时间、并发和资源均有上限或明确推迟原因。
5. **回归测试**：单元、合同、集成；涉及崩溃的切片还必须有故障注入/重启测试。
6. **中文说明**：`docs/day-XX-*.md` 说明调用链、状态机、失败窗口、面试讲法。
7. **可运行示例**：`examples/dayXX_*.py`，输出能观察到该日核心性质。
8. **全量绿**：从 `v2/` 运行所有测试，并把 ResourceWarning 视为错误。
9. **独立审查**：至少检查 P0/P1；未关闭的高优先级问题不得标记完成。
10. **路线同步**：更新本文状态表、README 当前切片、测试基线和下一目标。

日末必须记录：新增/修改文件、验收命令及结果、已知边界、下一日输入接口。

## 5. 永久安全与一致性不变量

后续 D3–D15 每天都必须回归：

1. Event log 是权威历史；checkpoint/summary 是可验证投影，不能替代事件真源。
2. stale version、stale run、stale agent 不能提交状态或取得新的副作用 claim。
3. 模型完整合法 terminal 之前绝不执行工具；未知语义 fail closed。
4. 工具链固定为：Schema → Policy → Approval → Ledger → Sandbox/Transport。
5. `OUTCOME_UNKNOWN` 不等于 FAILED，也绝不能自动重试成“也许成功两次”。
6. 只持久化受版本控制的 JSON；不保存 secret、完整环境、隐藏推理或 runtime object。
7. 所有路径必须 canonicalize 并证明仍在管理根目录内；防 symlink、junction、
   reparse point 和 TOCTOU 逃逸。
8. 网络默认关闭；MCP 与子 Agent 不能绕过或扩大父级权限。
9. 取消表示不再开始新工作，不表示撤销已经 claim 或已经发生的副作用。
10. 不默认 commit、push、删除、reset 或覆盖用户已有改动。
11. stdout、文件内容、tool result、MCP result、trace 都必须有大小和脱敏边界。
12. 配置错误必须尽量在 durable Turn 启动前预检，避免留下无意义 RUNNING 状态。

## 6. 依赖关系与为何不能乱序

```text
D1 -> D2 -> D3 -> D4 -> D5
D5 -> D6（同时引入跨 stream StreamPrecondition）-> D7
D3 + D7 + D8 + D9 -> D10
D6 + D7 + D9 + D10 -> D11
D8 + D11 + D5 Git facade -> D12
D3 + D4 + D5 + D6 + D7 + D9 + D11 -> D13
D1..D13 -> D14 -> D15
```

四个容易误解的顺序：

- D5 在 D8 前，所以 D5 只能运行配置中固定且可信的 argv；不能开放通用宿主机
  Shell。生产命令执行必须等 D8 容器。
- D6 负责“从已提交安全边界恢复”，不负责证明工具没有重复执行；工具崩溃窗口
  由 D7 Ledger 决定。
- D11 在 D12 前，所以 D11 子 Agent 只能做只读/推理任务；并发写必须等每 Agent
  worktree + container。
- D13 在 D11 后，所以 D11 初期只接受父 Agent 显式传入的有界上下文，不能提前
  宣称完整自动仓库检索。

## 7. D1 — Durable Control Plane（COMPLETE）

### 目标与面试价值

把 Thread、Turn、Run、resume、optimistic concurrency、idempotent command receipt
变成真正持久化语义。面试重点：为什么 conversation transcript 不等于 checkpoint，
为什么 version 与 `run_id` 是两道不同 fence。

### 已验收主链

```text
create Thread -> attach Turn -> start Run -> wait/pause
-> process restart -> rebuild -> resume -> fresh run_id
-> complete/fail/cancel/timeout -> atomic Thread detach
```

### 保留给后续的接口

- D2 `TurnWorker` 使用 `start_turn` 和终态命令。
- D6 扩展 stale Run recovery，但必须迁移 reducer/schema，不能偷偷改内存状态。
- D7 使用 exact Turn stream precondition 验证 active Run。

### 资料与验收

- 文档：`docs/day-01-durable-control-plane.md`
- 示例：`examples/day01_restart_resume.py`
- 测试：`tests/test_event_store.py`、`tests/test_thread_runtime.py`

## 8. D2 — Typed Model Stream + Agent Loop（COMPLETE）

### 目标与面试价值

证明 Coding Agent 不只是 `while True`：Provider 的不可信流必须先规范化、聚合、
终结验证，完整 ToolCall 才能进入执行面。

### 已验收主链

```text
D1 QUEUED -> fenced RUNNING
-> ModelRequest -> SSE typed events -> complete ModelTurn
-> validate all calls -> ToolExecutor port -> ToolResult context
-> next model round -> non-empty final -> D1 COMPLETED + detach
```

### 后续接口状态

- D3 已实现 `ToolExecutor` 的生产 Registry 与只读仓库工具。
- D6 持久化 canonical context；不持久化 delta。
- D7 让每个 ToolCall 先经过 Ledger claim。

### 资料与验收

- 文档：`docs/day-02-model-stream-agent-loop.md`
- 示例：`examples/day02_streaming_agent_loop.py`
- D2 完成时全量基线：79 tests / OK；当前 D1–D3 基线见状态表。

## 9. D3 — Tool Registry + Bounded Read/List/Search（COMPLETE）

### 核心问题

把 D2 的抽象 `ToolExecutor` 变成真实、可注册、可限制的只读 Coding Tools。D3
结束后，模型必须能通过 Registry 读取一个真实临时仓库并基于结果回答。

### 建议生产文件

- `src/koawa_agent_v2/tools/registry.py`
- `src/koawa_agent_v2/tools/errors.py`
- `src/koawa_agent_v2/tools/workspace.py`
- `src/koawa_agent_v2/tools/read_file.py`
- `src/koawa_agent_v2/tools/list_files.py`
- `src/koawa_agent_v2/tools/search_text.py`

具体文件名可在实现前小幅调整，但不得复制 D2 已有的 `ToolDefinition`、
`ToolCallItem`、`ToolExecutionResult` 类型形成第二套协议。

### 必须设计的接口

- `ToolRegistry.register(spec, handler)`：拒绝重名和不稳定名称。
- `ToolRegistry.definitions()`：确定性排序后提供 D2 `ToolDefinition`。
- `ToolRegistry.execute(call, context)`：唯一分发入口；未知工具使用稳定错误码。
- 每个 handler 用 typed args dataclass 解析严格 JSON object，而不是信任 dict。
- D3 只实现并文档化一个明确的 JSON Schema 子集（至少 object、properties、required、
  additionalProperties=false、受限 string/integer/boolean/array）；启动时拒绝任何不支持的
  keyword。模型看到的 schema、Registry validator 与 typed args decoder 必须由同一份
  `ToolSpec` 生成，不能维护三份会漂移的规则。
- `WorkspacePathResolver` 接收固定 workspace root，返回验证后的 canonical path。
- `ToolLimits` 至少包括单文件字节、最大行数、目录条目、扫描文件数、匹配数、
  单结果字符数和总输出字符数。

### 成功链

```text
Model ToolCall(read_file/search_text)
-> Registry name/schema gate
-> canonical workspace path
-> bounded handler
-> ToolExecutionResult(truncated metadata if needed)
-> next model round -> final
```

### 关键失败矩阵

- 重名注册：启动前失败，不能启动 durable Turn。
- unknown tool：整批调用在第一个副作用前拒绝。
- 缺字段、额外字段、错误类型、非法 Unicode：Registry 返回
  `invalid_tool_arguments`，handler 不执行。
- duplicate JSON key 在 D2 canonical `ToolCallItem` 边界就是 Provider protocol failure，
  不会伪装成可执行调用；`ToolSpec` 对直接 Registry 调用仍做二次拒绝。
- `..`、绝对路径、UNC、盘符切换：拒绝。
- symlink/junction/reparse point 指向 workspace 外：拒绝。
- 二进制、非 UTF-8、超大文件：拒绝或明确 bounded preview，不能乱码吞入模型。
- 目录/搜索超限：返回确定性排序的截断结果和 `truncated=true`，不能悄悄漏掉。
- cancellation/ownership 丢失：进入 handler 前停止；仍不宣称撤销已完成的读取。

### 必须测试与示例

- Registry 合同、重名/未知/错误 result 类型。
- Windows 与 POSIX 风格路径攻击、symlink/reparse escape。
- 真实临时 Git 仓库上的 read/list/search 集成；不能只用 mock handler。
- 固定排序、截断、二进制、超大目录和输出上限。
- `examples/day03_repository_read_loop.py`：模型请求搜索，再读取命中文件，最后回答。
- `docs/day-03-tool-registry-read-search.md`：逐函数调用链和面试回答。

### 禁止越界

不写文件、不 Apply Patch、不 Git、不 Shell、不 MCP、不审批。D3 没有 Ledger，
所以只允许无外部写副作用的工具。

### D3 Definition of Done

真实临时仓库的模型→Registry→read/search→ToolResult→final 闭环通过；所有路径与
大小攻击回归通过；全量测试绿；审查无 P0/P1。

### 实际验收结果

- 生产实现：`tools/errors.py`、`tools/schema.py`、`tools/registry.py`、
  `tools/workspace.py`、`tools/repository.py`。
- 离线闭环：`examples/day03_repository_read_loop.py` 完成真实临时仓库
  search→read→final，并用新 Runtime 从 SQLite 重放 COMPLETED/Thread detach。
- 中文说明：`docs/day-03-tool-registry-read-search.md`。
- 全量验收：132 tests / OK，3 个平台 skip 分别是当前 Windows 无 symlink 创建权限、
  POSIX-only FIFO 与 POSIX-only 非 UTF-8 文件名；真实 Windows junction 攻击测试已通过。

## 10. D4 — Atomic Apply Patch（COMPLETE）

### 核心问题

让模型以结构化 Patch 修改文件，并保证普通失败下不会留下半应用工作区。这里的
“atomic”是进程存活且文件系统正常时的操作原子性；跨断电/进程崩溃恢复由 D6/D7
继续处理，不能提前夸大。

### 建议生产文件

- `src/koawa_agent_v2/editing/protocol.py`
- `src/koawa_agent_v2/editing/transaction.py`
- `src/koawa_agent_v2/editing/tools.py`

### 协议与算法

- 明确支持 Add/Update/Delete；Move 可推迟，但必须写成显式非目标。
- Patch 文档有 schema/version、目标路径、base SHA-256、hunks 和大小上限。
- 两阶段：parse → 全量 preflight → stage temp files → commit replacements。
- 所有 hunk 在任何写入前完成 context 和 base hash 校验。
- 整个事务持有 workspace mutation lock；从 preflight 到每次 `os.replace` 前重新核对
  目标的 current hash 与 file identity。若用户或其他 Agent 在窗口内修改目标，必须零
  覆盖或回滚并返回 `stale_patch_base`，不能用旧 preflight 结果覆盖新内容。
- 多文件任一失败必须回滚已经替换的文件；回滚失败要返回明确
  `workspace_outcome_unknown`，不能假装原子。
- 保留 UTF-8/BOM、LF/CRLF、末尾换行的明确规则。
- 返回 before/after hash、changed files、行数摘要和确定性 diff。

### 关键失败矩阵

- malformed patch、重叠 hunk、context 不匹配：零写入。
- base hash 过期：`stale_patch_base`，拒绝覆盖并发用户修改。
- path/symlink escape、二进制、文件/Patch 超限：拒绝。
- 第二个文件 stage 失败：第一个文件仍保持原样。
- commit 中途故障：执行回滚；若无法证明最终状态，标记 unknown 并阻止继续。
- 新建已存在、删除不存在、编码不可表示：稳定分类。

### 验收产物

- 真实临时仓库的 add/update/delete、多文件成功与全部回滚。
- 每个 stage/replace 点的故障注入。
- `examples/day04_atomic_patch.py`。
- `docs/day-04-atomic-apply-patch.md`。

### 禁止越界

不运行测试、不自动 commit、不清理用户既有修改、不提供任意 shell。

### 实际验收结果

- 生产实现：`editing/protocol.py`、`editing/transaction.py`、`editing/tools.py`；并为 D3
  resolver 增加 handle-bound file/directory identity，为组合 Registry 增加复用入口。
- 协议：versioned Add/Update/Delete、base SHA-256、exact hunks、BOM/LF/CRLF/末尾
  换行规则和全链资源上限。
- 事务：同/跨进程 workspace lock、全量 plan、同目录 stage、每次提交前 hash/identity
  复核、backup/replace、反向回滚和 `workspace_outcome_unknown`。
- 离线闭环：`examples/day04_atomic_patch.py` 真实运行 D1+D2+D3+D4，多文件
  read bases → update/add/delete → reread/list verification → final → SQLite replay。
- 中文说明：`docs/day-04-atomic-apply-patch.md`。
- 全量验收：150 tests / OK，3 个既有平台能力 skip；D4 新增 18 tests。
- 明确边界：D4 不承诺进程 kill 后恢复或 ToolCall exactly-once；D6/D7 分别补 durable
  reconstruction 与 ledger/unknown outcome。

## 11. D5 — Test/Git/Diff Finalization Vertical Slice（COMPLETE）

### 核心问题

形成第一个能真正做简单编码任务的纵切面：读、搜、改、验证、看 diff、失败后
有界修复、最终报告。

### 已完成生产文件

- `src/koawa_agent_v2/verification/git.py`
- `src/koawa_agent_v2/verification/runner.py`
- `src/koawa_agent_v2/verification/finalization.py`
- `src/koawa_agent_v2/verification/tools.py`
- `src/koawa_agent_v2/execution/loop.py`（CompletionGate / progress guard 接缝）
- `src/koawa_agent_v2/editing/tools.py`（dirty baseline 保护 / Patch observer）

### 安全前提

D8 尚未存在，而 pytest `conftest.py`、npm/Gradle scripts 等即使 argv 固定，也会
执行仓库代码。因此 D5 的 HostRunner 只能标记为 **dev-only**，仅允许项目内置
fixture 或用户明确确认可信的仓库；面对任意/不可信仓库必须跳过测试，等 D8 容器。
它仍必须使用 `shell=False`、固定 cwd、环境白名单、超时、stdout/stderr 上限和
完整进程树终止。模型只能选择预注册 profile_id，不能提供 argv/env/cwd。

宿主 Git facade 只允许经过审计的只读子命令，并显式禁用 hooks、external diff、
textconv、pager 及 system/global/repository 可注入执行器（例如 diff 使用
`--no-ext-diff --no-textconv`，受控 Git config/env）。D8 后连 Git/测试也优先进入
隔离 backend。不得把 D5 描述为可安全处理恶意仓库的通用宿主命令执行器。

### 必须形成的闭环

```text
user task -> read/search -> apply_patch
-> configured trusted test argv
-> git status/diff
-> bounded repair if failed
-> deterministic finalization report
```

Git facade 必须区分任务开始前的 dirty baseline 与 Agent 新增 diff；任何 final 都要
列出 changed files、测试 argv/exit/timeout、未解决失败和风险。默认不 commit/push。

### 关键失败矩阵

- 未配置/模型篡改 argv：拒绝。
- timeout/cancel：杀完整进程树，结果类型化。
- stdout/stderr 超限：截断且保留 exit/摘要。
- 测试失败：结果回填模型，但受 model/tool/repair budget 限制。
- 用户已有 dirty file：不得覆盖、还原或计入 Agent 自己的成功证据。
- 恶意 `.git/config`、`.gitattributes`、diff driver、textconv、hook/pager：不得触发
  宿主进程；对应 fixture 必须证明 Git facade 的禁用参数/config 生效。
- Git 不存在、非仓库、diff 过大：稳定错误并 fail closed。
- 模型声称完成但没有 verification report：Finalizer 拒绝 COMPLETED。

### 验收产物

- 一个带失败测试的小型 fixture repo，Agent 修复后测试通过、diff 正确。
- 一个无法修复用例，预算耗尽后明确失败而非伪成功。
- `examples/day05_coding_vertical_slice.py`。
- `docs/day-05-test-git-finalization.md`。

### 完成证据（2026-08-19）

- 八个工具进入同一 sealed Registry：Read/List/Search/Patch/Test/Status/Diff/Finalize。
- 真实 fixture 首轮测试失败，模型精确 Patch，第二轮测试通过，再取得 status/diff 与
  deterministic finalization report；D1 Turn 最终 COMPLETED。
- 模型提前返回 final 会被 AgentLoop CompletionGate 以 `verification_required` 拒绝。
- dirty baseline 路径不可写、不可计入 Agent diff，并在最终化时重新验证指纹。
- 恶意 `diff.external`、`.gitattributes` 和 textconv fixture 未触发宿主 marker。
- profile-only/trust gate、输出截断、timeout、失败测试和测试预算均有回归用例。
- `examples/day05_coding_vertical_slice.py` 在真实临时 Git 仓库和 SQLite D1 runtime 上通过。
- 全量验收：157 tests / OK，3 个平台能力 skip；D5 新增 7 个纵向/安全测试。

## 12. D6 — Checkpoint、Context Reconstruction 与 Stale Run Recovery（DONE）

### 核心问题

Checkpoint 不只是“把一个 dict 写入数据库”。D6 必须让新进程发现可恢复 Turn，
验证 checkpoint，重建 canonical context，并将僵死旧 Run 转移给新 attempt。

### 建议生产文件/存储

- `src/koawa_agent_v2/recovery/protocol.py`
- `src/koawa_agent_v2/recovery/store.py`
- `src/koawa_agent_v2/recovery/context.py`
- `src/koawa_agent_v2/recovery/coordinator.py`
- `src/koawa_agent_v2/recovery/execution.py`
- `src/koawa_agent_v2/recovery/redaction.py`
- SQLite：版本化 `checkpoints`、`recoverable_turns` 和独立 `run-execution` event stream。
- D6 就扩展 EventStore 的跨 stream `StreamPrecondition`：写 execution stream 时，在同一
  SQLite 事务校验 Turn exact version、status=RUNNING 与 current run_id。不能推迟到 D7。

### Checkpoint 必须包含

- thread/turn/run、turn stream version、checkpoint schema version；
- 已完成 ModelTurn、ToolCallEcho/ToolResult、用户/指令 context；
- model round、tool count、字符/token budget；
- 下一阶段枚举，例如 `READY_FOR_MODEL`、`READY_FOR_TOOL`、`READY_TO_FINALIZE`；
- 覆盖的 event global position/commit/hash，用于验证与 tail replay；
- 不包含 raw SSE delta、半截 arguments、secret 或隐藏推理。

Event log 仍是真源，因此完整 canonical ModelTurn、ToolCall/ToolResult 和 phase advance
必须先作为版本化 typed facts 写入专用 rollout/context streams，例如
`model.turn-completed.v1`、`tool.result-recorded.v1`、`run.phase-advanced.v1`；不能只存在
checkpoint blob 中，也不能塞进 D1 reducer 无法识别的 Turn event。Checkpoint 只是由
这些事实派生的加速投影。它损坏、版本未知或 hash 不匹配时，必须从 typed facts + tail
重建或 fail closed，不能默默猜测。

这些 execution facts 必须进入按 turn/run 标识的独立 `run-execution` stream，而不是写入
D1 Turn stream：当前 D2 Worker 持有启动时的 Turn version，若模型事件推进 Turn version，
会让 Worker 自己触发 ownership guard。每次 append execution fact 都必须使用上面的
`StreamPrecondition` 原子验证 active Turn；旧 run 在接管后无法继续写 ModelTurn、
ToolResult 或 phase。D7 直接复用同一 precondition 做 Ledger claim。

### 真正恢复协调器

- 为 RUNNING attempt 建立有界 lease/heartbeat，记录 owner_id、lease_generation、
  expires_at 和 stream version；过期判断使用数据库时钟，不能信任不同 Worker 的本机钟。
- heartbeat 必须用 owner + generation + exact version 做 CAS；旧 owner/generation 不能
  在 recovery 后续租。
- 增加显式 `abandon_stale_run` / `turn.stale-run-requeued.v1` 一类迁移。lease 过期检查、
  旧 run_id/Turn exact-version fence、lease generation fence、requeue event、旧 lease
  失效和 recoverable index 更新必须在同一个 SQLite 事务完成，避免 heartbeat/reaper
  同时制造两个 Worker。管理员显式 abandon 也走同一命令协议。
- 新 start 必须生成新 run_id；旧 Worker 的 heartbeat、模型结果和 final 全部被拒绝。
- 提供 `list_recoverable_turns()`，新 Runtime 不需要事先知道 turn_id 才能接管。
- 旧 Worker 迟到的模型/final 仍被 version/run fence 拒绝。

### D7 前允许恢复的安全边界

- 可以恢复：模型调用前；完整 ModelTurn 已持久化但任何工具尚未开始；完整只读
  ToolResult 已持久化；测试/patch 已有确定性完成事实。
- 不可自动恢复：写工具可能已执行但结果未落库。此时 D6 必须标记需要 D7/人工，
  不能盲目再次执行。

### 故障矩阵

- checkpoint 写前/后 kill；event commit 后 checkpoint 前 kill；tail replay。
- checkpoint 截断、未知 schema、hash 不符。
- lease 未过期抢占：拒绝；过期恢复：新 attempt/run_id。
- 旧 Worker 在接管后 final：拒绝且不覆盖。
- `READY_FOR_TOOL` 前 kill：安全执行一次；工具执行中 kill：D7 前阻断自动恢复。

### 验收产物

- 真文件 SQLite，销毁旧 Runtime/Worker 后由新 Runtime 自动发现并继续。
- 至少五个 kill point 的故障注入。
- `examples/day06_checkpoint_restart_resume.py`。
- `docs/day-06-checkpoint-reconstruction.md`，必须解释 transcript、checkpoint、
  file rollback、side-effect recovery 的区别。

### 完成证据（2026-08-20）

- D6 模块已集中到 `koawa_agent_v2.recovery` 子包，公共根目录不再散落五个恢复模块。
- D6 Worker 在同一 SQLite commit 中写 `turn.started`、完整 execution seed、recoverable
  projection 和活跃 owner lease；首轮模型调用前不存在半启动窗口。
- stale requeue 已提交但 Worker 尚未启动时可重复 claim；等待/暂停会退出自动恢复索引，
  显式 resume 后重新入列。
- input/approval 响应以稳定 `UserMessage` 身份恰好注入一次，并跨再次崩溃保留。
- execution facts 与 checkpoint 对常见 credential 字段、Bearer、`sk-*` 和 secret 赋值做
  递归脱敏；回归测试直接检查 SQLite 原文不存在测试密钥。
- 父进程真实强杀子 Worker 的 6 个持久边界全部通过；工具执行中恢复保持
  `BLOCKED_UNCERTAIN_SIDE_EFFECT`，不自动重放。
- 全量验收：177 tests / OK，3 个平台能力 skip。

## 13. D7 — Tool Ledger、Crash Windows 与 Idempotent Recovery（DONE）

### 核心问题

关闭 D2 ownership guard 与副作用之间的 TOCTOU，并把“可能已成功但没记录”的情况
变成一等状态，而不是自动重试。

### 生产文件/存储

- `src/koawa_agent_v2/ledger/protocol.py`
- `src/koawa_agent_v2/ledger/store.py`
- `src/koawa_agent_v2/ledger/executor.py`
- `src/koawa_agent_v2/ledger/recovery.py`
- `src/koawa_agent_v2/ledger/__init__.py`
- 复用 D6 的 `StreamPrecondition`：在事务中校验 Turn exact version/current run_id，
  不向 Turn stream 写无意义事件。
- Ledger stream/table 保存版本化 JSON，不保存 handler/runtime object。

### 身份与状态机

逻辑 `execution_id` 必须在 PREPARED 时持久生成，并跨 process、attempt 和新 run_id 保持
稳定；推荐绑定 turn_id + 持久化 logical invocation id（或原始 ModelCallRef）+ tool
name + 规范化 arguments hash + side-effect class。**不能把 claimant run_id 放进稳定
execution key**，否则 D6 恢复生成新 run_id 时会产生第二个 key 并重复副作用。

claimant_run_id、claim_epoch、claim_token 是某次物理认领者的独立字段，只用于 fencing，
不改变逻辑 execution identity。

```text
PREPARED -> CLAIMED -> SUCCEEDED
                    -> FAILED
                    -> OUTCOME_UNKNOWN
```

- PREPARED：完整 ToolCall 已持久化，尚无执行权。
- CLAIMED：active-run exact-version precondition 与 ledger claim 在同一个 SQLite
  事务成功；只有 CLAIMED 才能调用 handler/MCP。
- SUCCEEDED/FAILED：有明确 result，带 digest、大小和脱敏信息。
- OUTCOME_UNKNOWN：副作用可能完成，但无法证明；必须查询、补偿或人工决定。

相同 key + 相同语义重试返回原 ledger 事实；相同 key + 不同参数必须 conflict。

### 恢复策略

- read-only：可由确定性重复读取或查询收敛，但仍记录。
- idempotent write：向支持方传逻辑 execution_id，或查询结果后决定。
- queryable non-idempotent：先查询外部结果，再标记成功/失败/unknown。
- unqueryable non-idempotent：绝不盲重试，保持 OUTCOME_UNKNOWN 等人工。
- orphaned CLAIMED 在持久状态上无法证明 handler 是否已经开始；恢复器必须按工具的
  recovery class 保守查询/幂等收敛，否则转 OUTCOME_UNKNOWN，绝不能假设“还没执行”。
- SUCCEEDED/FAILED/OUTCOME_UNKNOWN 提交必须携带 ledger exact version + claim_token；旧
  claimant 即使迟到也不能覆盖新 recovery 决策。

### 必测崩溃窗口

1. claim 前 kill：没有副作用，可重新 claim。
2. claim commit 后、handler 前 kill：恢复器知道 CLAIMED；按工具恢复策略处理。
3. handler 执行中 kill：OUTCOME_UNKNOWN。
4. handler 成功、result commit 前 kill：OUTCOME_UNKNOWN/查询，不能重复写。
5. result commit 后、checkpoint 前 kill：复用 ledger result，不再执行。
6. cancel 与 claim 并发：cancel 先提交则新 claim 失败；claim 先提交则允许该已认领
   操作收敛，但不再开始下一个。

### 验收产物

- 所有工具调用统一穿过 `LedgerExecutor`，没有旁路 Registry/MCP executor。
- 上述六个窗口使用可控 handler + kill/fault injection 验证。
- `examples/day07_tool_ledger_recovery.py`。
- `docs/day-07-tool-ledger-crash-windows.md`。

### 明确边界

D7 只能保证不盲目重复和原子 claim，不能让任意外部系统凭空获得 exactly-once。

### 完成证据（2026-08-20）

- 持久 Tool Worker 拒绝 raw Registry/Executor 旁路；handler 接收稳定
  `ToolExecutionContext.execution_id` 作为外部幂等键。
- 真实子进程强杀覆盖 claim 前、claim 后、handler 中、handler 后/result 前、
  result 后/checkpoint 前；cancel/claim 由 Turn CAS 双顺序验证。
- 并发 prepare 幂等、claim epoch/token fencing、五态回放、四种 recovery profile、
  UNKNOWN 后续查询/人工解析和脱敏参数 fail-closed 均有回归测试。
- `examples/day07_tool_ledger_recovery.py` 真实运行通过；write-ahead trace 为
  PREPARED→CLAIMED→SUCCEEDED，重启复用结果且 manual unknown 不重复调用。
- D7 新增 14 个测试；全量 `unittest discover` 为 191 tests / OK，3 个既有
  平台能力 skip。

## 14. D8 — 完整 Docker Sandbox（COMPLETE）

### 核心问题

将命令、测试和不可信仓库进程从宿主机搬进真实容器。这里的“Sandbox”必须真的
创建 Docker 容器，不能只是给 `subprocess` 换类名。

### 建议生产文件

- `src/koawa_agent_v2/sandbox_protocol.py`
- `src/koawa_agent_v2/docker_sandbox.py`
- `src/koawa_agent_v2/container_reaper.py`
- `docker/agent-runtime.Dockerfile`

### 强制容器配置

- 运行时只接受 immutable image ID/内容 digest；tag 只用于人类显示，绝不作为安全身份。
  目标仓库的 Dockerfile/构建脚本不能成为 runtime image 的 build input。
- 非 root 用户；read-only rootfs；
- 只挂目标 workspace 和必要 tmpfs，不挂 Docker socket、用户目录、凭据目录；
- `cap-drop=ALL`、no-new-privileges、PID/CPU/memory/time/output 限制；
- 默认 `network=none`；环境变量白名单，绝不继承完整 host env；
- argv/cwd 结构化，禁止 shell 拼接；stdout/stderr/exit/timeout/cancel 类型化；
- 容器创建前先持久化 allocation intent（owner、不可猜 nonce、期望 labels、image digest、
  mount digest）；create 成功后再回写 container ID。managed labels 同时带 owner token。
- reaper 以 intent + labels + owner nonce 对账，只清理 V2 管理域容器，且覆盖“Docker
  create 已成功、DB 回写 container ID 前进程被 kill”的窗口；禁止按模糊名字批量删除。
- timeout/cancel 杀完整容器/进程树；container identity 持久化，崩溃后可 reaper；
- Windows 路径先 resolve 并验证 mount source 在指定 workspace/管理目录内。
- 容器不得写共享 `.git`、父仓库 Git config/attributes 或 host controller 元数据；否则
  恶意任务可修改 diff driver/textconv 后借 D5 的宿主 Git facade 执行代码。

### 攻击/失败验收

- 读取宿主用户目录、Docker socket、workspace 外路径失败。
- 网络默认不可达；fork bomb/大量进程被 pids limit 限制。
- CPU/memory/time/output 超限得到稳定状态并清理容器。
- symlink mount escape、环境 secret、子进程残留、orphan container。
- 在 create 后 DB 回写前 kill：新进程依据 allocation intent/labels 找回或安全清理，
  且不能误删用户容器；篡改 label/owner nonce 必须拒绝清理。
- Docker daemon 不可用时 `doctor` 明确失败；D8 的 Docker 集成测试不能全部 skip
  后仍标记 COMPLETE。

### 接线与产物

D5 的 test runner 改为 Docker backend；宿主固定 argv runner 仅保留测试/bootstrapping。

- `examples/day08_docker_sandbox.py`
- `docs/day-08-docker-sandbox-threat-model.md`

### 完成证据（2026-08-20）

- Docker Desktop Linux Engine 29.6.1 上使用 immutable Python 3.12 image ID
  完成真实执行；Dockerfile 的 context 只有 docker/，无目标仓库 COPY/ADD。
- inspect 证明 non-root、read-only rootfs/workspace、network none、cap-drop ALL、
  no-new-privileges、PID/CPU/memory/swap、tmpfs 和 log none 均真实生效。
- 容器内实际验证 host secret/home、Docker socket、workspace 外路径和网络不可达，
  /tmp 可写，workspace、父 Git 元数据与 rootfs 不可写。
- timeout 子进程树、output overflow、pids limit、OOM 和 cancellation 均类型化，
  每条路径结束后 managed container 清单回到基线。
- 子进程在 create 成功、DB bind 前以 os._exit 强杀；重启后由 INTENDED、确定性
  name、exact labels 和 nonce 找回并按完整 ID 删除。clone、unknown、owner/
  allocation/nonce 篡改均 refused 且不错误 release。
- create/inspect 结果不确定保持 open allocation；取消期间 Event Store 失败仍原样
  重抛取消；NTFS hard-link、junction/reparse/symlink mount 逃逸 fail closed。
- D8 新增 26 个测试，其中 14 个真实 Docker tests 为 0 skip；全量
  unittest discover 为 217 tests / OK，3 个既有平台能力 skip。
- examples/day08_docker_sandbox.py 真实运行通过，security probe 与
  create-before-bind 重启回收结束后 managed containers 为 0。

## 15. D9 — Policy、Durable Approval 与 Resource/Network Controls（COMPLETE）

### 核心问题

Sandbox 负责“即使模型想越界也做不到”；Policy/Approval 负责“什么动作被允许”。
两者不能互相替代。

### 建议生产文件

- `src/koawa_agent_v2/policy.py`
- `src/koawa_agent_v2/action_digest.py`
- `src/koawa_agent_v2/approval_service.py`
- `src/koawa_agent_v2/network_policy.py`
- `src/koawa_agent_v2/resource_budget.py`

### 决策与审批

- 所有 built-in tool、MCP tool、Agent spawn 统一返回 `ALLOW | DENY | ASK`。
- action digest 绑定 tool、规范化参数、已 resolve 资源身份/路径、side-effect class、
  sandbox profile、network target、credential scope、policy version 和 principal。
- 现有 D1 `PendingInterrupt(prompt, kind)` 与 bool resume 不足以承载安全批准；D9 必须
  新增/迁移版本化 durable approval request/grant/deny/expire/consume schema/events，
  保存 digest、scope、principal、policy version、expiry、single-use 和关联 interrupt。
- ASK 仍通过 D1 `WAITING_FOR_APPROVAL` 生命周期等待，但 resume 必须匹配 interrupt_id、
  expected version 和 approval request；不能只凭一个 bool 推断批准了什么。
- 批准后、执行前重新 resolve 路径/DNS/资源身份并重算 digest；任何变化都重新审批。
- 一次性 grant 的 consume、active-run exact-version fence 和 D7 ledger claim 必须在同一
  SQLite 事务完成。事务谁先提交决定 cancel/claim 竞态，grant 不能被第二次复用。
- 子 Agent 权限只能收窄；MCP server 声称的能力不能扩大父 Policy。

### 网络与资源

- D8 仍默认 network=none；允许网络时通过受控 proxy/allowlist，不直接开全网。
- 规范化 scheme/host/port；HTTPS、redirect、DNS/IP/private-range/rebinding 策略明确。
- credential 只给批准的 origin/server，不进日志、模型 context 或子 Agent。
- preflight budget 与运行时 Docker limit 同时执行，不能只在 UI 显示预算。

### 失败验收

- deny/ask/allow；旧审批、重复回答、错误类型、参数篡改。
- symlink 让批准路径指向新目标；审批后 cwd/network target 改变。
- redirect 跨域带 credential、DNS 指向 loopback/private IP、直连绕 proxy。
- MCP/子 Agent 尝试绕过 Policy；budget 超限。

### 产物

- `examples/day09_durable_approval.py`
- `docs/day-09-policy-approval-network.md`

## 16. D10 — MCP Lifecycle 与安全工具调用（COMPLETE）

### 核心问题

把真实 MCP server 作为动态工具来源接入同一条 Registry→Policy→Ledger→Sandbox 链，
而不是写一个名字叫 MCP 的普通函数。

### 实现前 ADR

先核对并固定 MCP 规范/官方 Python SDK 版本，记录选择。核心验收至少完整支持 stdio；
Streamable HTTP 若本日不做，必须写成显式非目标，不能模糊声称“支持所有 MCP”。

### 建议生产文件

- `src/koawa_agent_v2/mcp/protocol.py`
- `src/koawa_agent_v2/mcp/transport.py`
- `src/koawa_agent_v2/mcp/connection_manager.py`
- `src/koawa_agent_v2/mcp/tool_binding.py`
- `src/koawa_agent_v2/mcp/fixture_server.py`

### 必须覆盖的生命周期

```text
spawn/connect -> initialize -> version/capability negotiation
-> initialized notification -> tools/list (including pagination)
-> validate + immutable catalog bind -> tools/call
-> refresh/catalog replacement -> cancel/timeout -> close/shutdown
```

- JSON-RPC request ID 与并发响应正确关联；EOF、断连、server crash、重启明确。
- server/tool 命名空间和冲突规则确定；schema 进入 Registry 前严格校验。
- 每个 binding 固定 `(server_id, session_generation, tool_name, schema_hash)`；该 tuple
  必须进入 D9 action digest 与 D7 logical execution identity。`list_changed`/refresh 产生
  新 catalog generation，不能悄悄改变已审批或在途调用；schema 漂移使旧批准失效。
- tool 描述、annotations 与 readOnly/idempotent 声明都视为不可信提示。side-effect class、
  network 和 credential scope 只能来自本地管理员配置；未知工具默认高风险 ASK/DENY。
- 本地 server 进程使用 sandbox/profile 和环境白名单；host-trusted 运行需显式审批。
- MCP stdout frame、stderr、单结果和累计输出都有限额；输出是不可信数据，需脱敏和
  prompt-injection 标记。
- 所有 MCP 调用（包括声称只读者）都经过 D9 Policy 和 D7 Ledger，没有直连旁路。
- timeout 后 server 可能已成功时进入 OUTCOME_UNKNOWN，不能普通 retry。

### 真实验收

启动真实本地 fixture MCP server，测试 initialize/initialized/list 分页/call/close、两个
并发 request、list_changed/schema refresh、request-id 错配、慢响应、EOF、断连、超大/
畸形 frame/result/stderr 和恶意工具描述。不得只 mock client。

- `examples/day10_mcp_roundtrip.py`
- `docs/day-10-mcp-lifecycle-security.md`

## 17. D11 — Durable Multi-Agent Control Plane（COMPLETE）

### 核心问题

实现可恢复的 parent/child Agent 图和消息/取消/预算控制。D11 证明的不是“并行调用
两次模型”，而是有身份、状态、失败传播和持久化的控制面。

### 建议生产文件/接口

- `src/koawa_agent_v2/agent_graph.py`
- `src/koawa_agent_v2/agent_control.py`
- `src/koawa_agent_v2/agent_messages.py`
- `src/koawa_agent_v2/agent_scheduler.py`
- API：`spawn_agent`、`send_message`、`followup_task`、`wait_agents`、
  `interrupt_agent`、`list_agents`。

### 状态与约束

- durable parent/child/task identity，状态至少 CREATED/RUNNING/WAITING/
  COMPLETED/FAILED/CANCELLED/ORPHANED。
- 每个 Agent 有 attempt、run_id、lease owner/generation 与 exact-version fence；orphan 接管
  必须产生新 run，旧 run 不能再提交 message/result/artifact。
- durable mailbox 的每条消息有 message_id、per-agent sequence、semantic idempotency key
  和 delivery/ack 状态；重启或重复投递不能制造两份任务/结果。
- depth、并发、总 Agent、token、time、tool budget；创建 child 前必须从父级总预算在
  SQLite 中原子 reserve/debit，结束后按规则结算，不能只给每个 child 本地限额导致超卖。
  子权限 ≤ 父权限。
- fresh context 与显式 fork context 区分；不默认复制全部 tool chatter。
- typed message/result/artifact；子结果由父选择进入 context，不能直接污染父 context。
- cancel propagation、失败隔离、join timeout、孤儿发现/接管、无循环依赖/死锁。

### D11 的写限制

D12 尚未隔离工作区，因此用 capability allowlist 硬限制为 D3 本地 read/list/search +
模型推理。write、patch、test、宿主命令、network 和未由本地配置审定的 MCP 全部禁用；
不能根据工具名称或 server 自报 annotation 猜“只读”。D11 只用两个并行只读任务
（例如一个定位代码、一个审查设计）验收；任何共享工作区并发写都算越界。

### 失败验收与产物

- 两个并行 worker + 一个失败/取消，父任务仍收敛。
- 深度/并发/父预算并发 reserve 超限，循环依赖，重复 message，父取消，进程重启后
  orphan 恢复；旧 Agent 的迟到消息/result/artifact 必须被 fence。
- `examples/day11_multi_agent_readonly.py`
- `docs/day-11-multi-agent-control-plane.md`

## 18. D12 — Per-Agent Worktree + Container Isolation（COMPLETE）

### 核心问题

为每个写 Agent 提供独立 Git worktree 和 Docker 容器，消除共享工作区竞态，并让
父 Agent 以可审查 artifact 集成结果。

### 建议生产文件

- `src/koawa_agent_v2/worktree_manager.py`
- `src/koawa_agent_v2/agent_workspace_store.py`
- `src/koawa_agent_v2/agent_container.py`
- `src/koawa_agent_v2/artifact_integration.py`

### 必须实现

- 每 Agent 独立 worktree、branch/base commit、durable workspace identity。
- 每个 Agent 容器只挂自己的 worktree；不能写父、兄弟或主工作区。
- Git 操作由 host coordinator 执行；不把共享 `.git` 任意暴露给容器。
- 子 Agent 交付 patch/commit/diff artifact，带 base/head/hash/test evidence；artifact acceptance
  绑定 active Agent run fence、artifact digest、exact head 与测试使用的 image digest。
- D12 选择明确的 dirty baseline 策略：**多 Agent 写任务默认拒绝 dirty 用户工作区**，
  read-only 仍可用；不得只从 base commit 建 worktree 后假装看见了用户未提交上下文。
  若未来支持 dirty snapshot，必须按 path/hash/bytes 建独立版本化快照后再开放，不在本日
  暗中复制或忽略。
- 父级先把候选 artifacts 集成进独立 integration worktree，完成 base/hash preflight、
  冲突处理和容器重测；全部通过后，才用 D4 workspace lock + current-hash gate 原子投递
  到用户工作区。禁止逐个 artifact 直接写主工作区形成半集成状态。
- 默认不自动 merge/commit 用户分支。
- cancel/crash 后有 durable inventory 和安全 reaper；删除前 resolve 到 V2 管理目录，
  禁止广泛递归删除。

### 验收

- 两 Agent 改不同文件：成功集成并重测。
- 改同一行：明确冲突，父级决定，不丢任一 artifact。
- 兄弟目录不可见/不可写；取消后旧 Agent 不能继续提交。
- orphan worktree/container 恢复与安全清理。

- `examples/day12_isolated_writing_agents.py`
- `docs/day-12-worktree-container-isolation.md`

## 19. D13 — Repository Context 与 Compaction（COMPLETE）

### 核心问题

让大型仓库的相关上下文在预算内进入模型，并在长任务中压缩历史而不丢任务义务、
状态和安全事实。

### 建议生产文件

- `src/koawa_agent_v2/repository_index.py`
- `src/koawa_agent_v2/context_retrieval.py`
- `src/koawa_agent_v2/context_budget.py`
- `src/koawa_agent_v2/compaction.py`

### Repository Context

- 遵守 `.gitignore` 和显式 include/exclude；二进制/大小/文件数限制。
- 结果有 path、SHA-256、line range、source、score；确定性排序/去重。
- 文件变更后旧 hash 片段失效；不能向模型发送 stale context。
- 仓库文本始终标记为不可信数据，不能覆盖 system/developer instructions。
- 不要求先上 vector DB；可从 tree、recent diff、symbol/text search 和启发式排序开始。

### Compaction

summary 只压缩已经闭合的公开对话前缀，作为明确标记的 model-generated/untrusted data；
必须保持 D2 ToolCall/ToolResult 配对合法，不能留下 unresolved call，也不能改写系统/
开发者指令。原始权威事件不删除；不保存隐藏思维链。

用户目标、硬约束、已改文件/hash、测试证据、pending approval、ledger OUTCOME_UNKNOWN、
active child agents、预算与下一阶段等安全/执行状态，必须每次从 typed authoritative
projections 确定性拼接，不能靠有损 summary “记住”。coverage 使用各权威 stream version、
global commit position 与 canonical hash；重启由 checkpoint + authoritative projections +
summary + tail events 重建，任务义务不改变。

### 验收

- 大 fixture repo 下的预算、排序、ignore、binary、stale invalidation。
- compaction 前后同一任务约束、pending approval/unknown outcome 不丢。
- 多轮压缩与重启重建。

- `examples/day13_context_compaction.py`
- `docs/day-13-repository-context-compaction.md`

## 20. D14 — Trace、Evaluation 与 Failure Injection（COMPLETE）

### 核心问题

用可重复数据回答“Agent 是否可靠”，而不是凭一次演示感觉良好。

### 建议生产文件

- `src/koawa_agent_v2/trace.py`
- `src/koawa_agent_v2/redaction.py`
- `src/koawa_agent_v2/fault_injection.py`
- `evals/tasks/*.json`
- `evals/run_eval.py`

### Trace

- thread/turn/run/model/tool/ledger/MCP/sandbox/subagent 全链 correlation IDs。
- trace schema 使用字段 allowlist；时长、usage、预算、结果、失败分类在进入持久化管道前
  就完成裁剪/脱敏。raw secret/正文绝不能先落盘再事后清洗。
- API key、完整环境、隐藏推理、无界文件/tool/MCP 正文不得入 trace。
- 能从一次 E2E trace 定位决策、工具、测试、恢复和 final，不只是 print log。

### Failure Injection

使用 fake clock/seed/明确 failure point，至少覆盖：坏 SSE、terminal 后事件、
checkpoint 各窗口、Ledger 各窗口、DB version conflict、MCP EOF/timeout、container
kill、审批丢失/篡改、subagent orphan、worktree 冲突。

关键 crash window 不能全部用同进程异常模拟：必须由 subprocess 真正 kill Worker，
启动新进程重建，并断言 durable 状态、外部副作用计数、旧 run fencing 与资源回收。

### Eval

建立至少 20 个固定小任务；每项固定 base commit/fixture digest，并运行在 fresh isolated
worktree + container。grader 使用独立隐藏 oracle/测试，不能让模型 final 自证完成；
统一 budgets、model/config、policy 与 image digest。指标包含任务成功、Patch 正确/可应用、测试通过、
工具次数、token/时间、sandbox/policy violation、resume 成功、unknown outcome 数。
mandatory reliability 使用 deterministic scripted/fake provider；真实模型只做 opt-in 多次
运行并报告均值、方差和失败分类。保存 baseline 与一次真实改动的 A/B；安全不变量违规必须为 0。

### 产物

- `examples/day14_trace_failure_replay.py`
- `docs/day-14-trace-eval-failure-injection.md`
- 一份失败分类报告，而不是只给平均成功率。

## 21. D15 — End-to-End Acceptance 与面试成果包（COMPLETE）

真实 Provider 装配、approvals/approve/deny 命令、MCP 配置化与真模型 smoke 的
补充说明见 `docs/day-15-real-model-runtime.md`；本节仍记录原始验收矩阵。

### 产品入口

提供最小稳定 CLI/API，例如：`run`、`resume`、`status`、`cancel`、`doctor`，能选择
仓库、模型配置、sandbox profile、MCP 配置和是否启用 multi-agent。真实凭据只从
受控 credential provider/env 获取，不写配置/trace。

### 必须观察到的最终链路

```text
select real Git repo -> create Thread/Turn
-> model read/search -> apply patch
-> Docker test -> status/diff
-> bounded repair -> evidence final
-> process restart -> query/resume same durable task
```

### 强制 E2E 矩阵

1. 单 Agent 成功修改并测试。
2. 首次测试失败，模型有界修复后成功。
3. 坏模型流/缺 terminal，工具执行次数为 0。
4. 安全 checkpoint 后 kill，新进程发现并恢复。
5. 工具结果不确定时进入 OUTCOME_UNKNOWN，不盲重试。
6. 审批 allow、deny、以及审批后参数篡改重新审批。
7. Docker 路径、网络、资源和 secret 隔离。
8. 真实 MCP fixture 完成 initialize/list/call/close。
9. 两个写 Agent 独立 worktree：不同文件集成成功。
10. 同行冲突进入显式冲突分支。
11. 恶意仓库文本/MCP 输出不能越权或覆盖系统指令。
12. 旧 Worker/旧 Agent 迟到结果被 fence。
13. 全进程重启后状态、checkpoint、ledger、trace、diff 可查询。

CI 的 mandatory E2E 使用 deterministic fake model；真实 Provider 是需要凭据的
opt-in smoke，CI 不依赖付费 API 或网络波动。

此外必须有一条 **golden composite E2E**，在同一个 durable Turn 中真实贯通：Policy/
Approval → atomic Ledger claim → 真实 MCP fixture → 只读与写子 Agent → 各自 worktree/
Docker → 一次测试失败和 repair → OS 进程 kill → 新进程 resume → evidence final。它用
subprocess 真正终止进程，而不是只销毁 Python 对象。

再增加 dispatch-contract 测试，证明 built-in、MCP 和 subagent 三类入口都不能绕过
Schema/Registry → Policy/Approval → Ledger → Sandbox/Transport；分别通过的模块测试不能
替代这条接线断言。

### 面试与简历成果包

- 一张总架构图和 D1–D15 状态/调用链图；
- 5 分钟项目讲法、15 分钟深入讲法；
- Checkpoint/Resume、Ledger crash matrix、Sandbox threat model、MCP lifecycle、
  Multi-Agent isolation 五份深挖材料；
- 关键 ADR：事件存储、模型协议、Patch 原子性、恢复、Ledger、Docker、MCP；
- 20 道常见追问的 30 秒/2 分钟/深挖答案；
- 可重复演示脚本和 eval 报告；
- 简历只写实际通过 D15 验收的能力，不把 PENDING 项提前写成已完成。

### 最终发布门槛

- D1–D15 全部状态 COMPLETE；
- 全量单元/合同/集成/故障/E2E 通过；
- Docker 与 MCP mandatory 集成没有全 skip；
- 无未关闭 P0/P1；已知 P2/边界在 README/文档公开；
- 新机器按 README/doctor 能完成安装与 deterministic demo。

## 22. 每日统一执行模板

新会话实施任何 Dn 时，先在回复中明确以下内容，再改代码：

1. 本日唯一目标与上一日输入接口。
2. 将新增/修改的具体文件；说明为何不改未来模块。
3. 正常时序图或调用链。
4. 状态/事件/schema/稳定错误码。
5. 失败矩阵：注入点、持久状态、是否重试、副作用结论、测试名。
6. 单元、合同、真实集成、故障注入的分工。
7. 示例与中文文档路径。
8. 完成后全量验收命令和不能声称的边界。

推荐日内顺序：

```text
读当前切片相关生产链与 1 个成熟源码参照
-> 写协议/状态与失败矩阵
-> 测试先锁高风险边界
-> 实现最短真实主链
-> 故障注入/安全审查
-> 示例、中文文档、全量回归
-> 更新状态表与下一目标
```

## 23. 每日收尾更新清单

完成 Dn 后必须：

- 将本文状态表的 Dn 改为 COMPLETE，Dn+1 改为 NEXT；
- 更新 README 的 current/next slice；
- 增加 `docs/day-XX-*.md` 与 `examples/dayXX_*.py`；
- 在当日文档记录实际测试数量、命令和输出摘要；
- 列出新增 public API、schema/event、稳定错误码；
- 列出明确推迟到后续日的风险，不能藏在 TODO；
- 执行 `git status --short`，确认没有修改 legacy Java 或无关用户文件；
- 不自动 commit/push，除非用户明确要求。

## 24. 新会话接手提示模板

复制下面这段给新会话即可：

```text
继续开发 D:\KoawaAgent\v2。先完整读取：
1) D:\KoawaAgent\v2\AGENTS.md
2) D:\KoawaAgent\v2\docs\15-day-coding-agent-roadmap.md
3) D:\KoawaAgent\v2\README.md
4) 所有已 COMPLETE 日的 docs/day-XX 文档

先检查 D:\KoawaAgent 的 branch/status，保留用户现有修改；只允许修改 v2/**，
不导入或修改旧 Java。按规划书状态表找到唯一 NEXT 切片，先运行全量 unittest
基线，再复述该日目标、具体文件、状态/接口、失败矩阵、验收和禁止越界项。
只实现这一个切片；不要创建未来空模块。生产代码、失败测试、真实示例、中文文档、
全量回归和 P0/P1 审查全部完成后，才能更新状态表并进入下一日。
```
