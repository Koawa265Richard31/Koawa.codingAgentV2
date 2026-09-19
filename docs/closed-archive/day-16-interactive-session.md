# D16：交互式会话（Interactive Session + 会话记忆投影与压缩）

> 状态：COMPLETE（2026-08）。实现按本文档落地，10 个 D16 tests + 离线示例 + 全量回归绿。
> 背景结论：D1–D15 把 Agent Loop 收敛到任务粒度，任务内闭环真实（真实模型 smoke 已验证）；
> 但同一线程的新 Turn 不投影历史对话——存储有记录，模型看不见。复杂 coding 任务无法
> 收敛到单任务粒度，必须补"会话记忆"这一层。

## 0. 实测结果与实现说明（COMPLETE 后追加）

- 离线示例 `examples/day16_interactive_session.py`：4 轮对话，窗口 max_turns=2，
  第 4 轮触发压缩（compacted_blocks=1），权威投影含 turn id/status/request。
- 关键实现决策：
  - `TurnState.outcome` 已持久化 worker 的 final_text（complete_turn 存 summary），
    历史重建直接读 outcome，无需解析 model.turn-completed 事件（文档 4.2 的简化）。
  - 对话回合关闭 D5 completion gate（`build_worker(task_mode=False)`）：纯对话回合
    不应被 verification_required 拒绝；聊天中暂停的审批 Turn resume 也走该模式。
  - 会话连续性：CLI 把 thread id 写入 `<db>.session.json`（JSON 持久化），重开
    interactive 自动接续；`/thread <uuid>` 可切换。
  - 压缩 v1 权威投影 = turn_id/status/error/request 摘要；"改动的文件集合（git 证据）"
    推迟（v2）。模型摘要经 `summarize_via_client` 走真实 client，失败自动降级为
    仅权威投影（[untrusted-session-summary] 缺失可容忍）。
- 顺手修复：`_turn_document_from_state` 引用不存在的 `turn.final_text`（status 命令会崩），
  改为 `turn.outcome`。

## 1. 为什么需要 D16

1. **复杂任务的真实形态是会话，不是单任务**。"给项目加多租户"这类任务包含多轮
   勘察、方案讨论、分步实施、失败修复、范围再谈判。若每轮模型都失忆，等于每轮
   重新谈判：反复询问"我改到哪了"、重复已完成的工作。codex/opencode/Claude Code
   均为会话式交互，原因在此。
2. **面试重点**：上下文管理是 coding-agent 面试的高频追问板块。当前项目只有
   "任务内上下文"（Agent Loop 内累积 + D6 checkpoint 持久化）与"仓库级压缩"
   （D13），缺"会话级记忆"，叙事不完整。
3. **底层已就绪**：事件存储已持久化每个 Turn 的 user_input、模型轮次、工具调用/
   结果全文、终态（append-only、可重放）。D16 只补"投影"层，不改存储底座——
   这正是事件溯源架构的收益，面试可讲。

## 2. 现状与差距（事实）

- 有：ThreadRuntime 线程（= 会话谱系）；每个 Turn 的事件流含 user_input、
  model.turn-completed.v1（带输出投影）、tool.*、trace.*、终态。
- 有：D6 checkpoint 在 Turn 内崩溃后重建上下文；resume 接管暂停/中断的 Turn。
- 无：新 Turn 启动时，TurnWorker 上下文 = 系统指令 + 本轮输入，不含同线程历史
  Turn 的任何投影。run 因此是独立任务。
- 无：CLI 只有一次性命令，无对话式交互入口。
- 有间接记忆：git 工作树保留文件改动，但模型不记得为什么改、改了什么、证据在哪。

## 3. 记忆阶梯与 D16 定位

| 层 | 做法 | D16 |
| --- | --- | --- |
| ① 窗口内全量 | 会话短时全部保留 | 实现 |
| ② 截断 | 超限丢最旧 Turn | 实现 |
| ③ 压缩/摘要 | 被丢旧轮压成"会话摘要"常驻上下文 | 实现（v1：权威投影+可选模型摘要） |
| ④ 检索召回 | 从事件存储按需召回 | 边界（原料已具备，后续日） |
| ⑤ 外部工件 | plan/TODO/证据写入 repo（session journal） | 边界（后续日） |

## 4. 核心设计

### 4.1 会话模型

- thread = 会话。交互模式下，一个 thread 对应一次 CLI 会话（持久化在 config.db），
  会话内每个用户输入 = 一个新 Turn。
- 新 Turn 的模型上下文 = 系统指令 + 历史投影 + 本轮输入。
  实现：`TurnWorker(..., initial_context=())`，fresh-turn 分支插在 instructions 之后、
  新输入之前；resume 路径不注入，仍以 D6 重放为准。
- 投影只发生于 fresh turn；Turn 内仍由 Agent Loop 自行累积。

### 4.2 历史投影（白名单 + 有界）

- 来源：CLI 内存维护的 SessionTurn 列表；重启时 `SessionHistory.from_thread` 从事件
  存储重建（user_input 与 TurnState.outcome 即 final_text）。
- 白名单：只投影 user_input 与最终回答。绝不注入原始工具参数、工具结果全文、
  trace 明细、凭据或推理内容（reasoning 不落库、不回传，延续 D2 契约）。
- 有界（RuntimeConfig）：history_max_turns=16、history_max_chars=32_000、
  compact_min_turns=4；超限从最旧 Turn 截断。
- waiting_for_approval 的 Turn 不进入历史投影，等待行内审批闭环。

### 4.3 会话压缩（阶梯③）

- 触发：被截断的旧 Turn 数量达到 compact_min_turns。
- 权威部分（typed 投影，确定性拼接）：turn_id / 终态 / 错误码 / request 摘要；
  压缩结果以 UserMessage 常驻会话头部。
- 不可信部分：Provider 把被压 Turn 摘要成 [untrusted-session-summary]（D13 约定）；
  摘要失败自动降级为仅权威投影。

### 4.4 工作目录自主选择

- interactive --config cfg.json [--repo PATH]：--repo 在 assembly 之前覆盖
  config.repo（AppRuntime.from_config_file(repo_override=...)），支持任意目录
  （含当前目录 .）。

### 4.5 交互 CLI（纯 CLI，非 TUI）

```text
koawa-agent-v2 interactive --config cfg.json [--repo PATH]
```

- 循环：横幅 + /help；input() 读行；空行跳过。
- 普通输入 → AppRuntime.chat(message, thread_id, history)：同一 thread 上新 Turn，
  打印 agent> final_text。
- 行内审批：waiting_for_approval 时提示 y/n，approve 后自动 resume。
- 命令：/status /approvals /approve /deny /resume /history /thread /help /exit；
  EOF（Ctrl+Z）与 Ctrl+C 干净退出；会话状态持久化于事件存储 + <db>.session.json。
- 无流式打字机渲染（后续日增强，不阻塞）。

### 4.6 新增接口（实现清单）

- execution/worker.py：TurnWorker(..., initial_context=())。
- runtime/session.py（新）：SessionTurn / SessionHistoryLimits / CompactionResult /
  SessionHistory（append/project/maybe_compact/context_items/from_thread）+
  summarize_via_client。
- runtime/app.py：AppRuntime.chat() + from_config_file(repo_override=...)；
  chat Turn 记入 _chat_turn_ids，resume 走无 gate 模式。
- runtime/assembly.py：AssembledRuntime.loop + build_worker(initial_context, task_mode)。
- runtime/cli.py：interactive 子命令 + 交互循环。
- runtime/config.py：history_max_turns / history_max_chars / compact_min_turns。

## 5. 失败路径（全部有测试）

- 历史投影失败（thread 不存在）→ SessionHistoryError(thread_not_found)，CLI 降级为新会话。
- 历史超限 → 截断；压缩后仍超限 → 保持截断态继续。
- 压缩 Provider 失败 → 仅权威投影继续（已测）。
- 审批中断 → 行内 approve/deny 闭环，其余 Turn 照常（已测）。
- stdin EOF / Ctrl+C / 空输入 → 干净退出。
- 同 thread 并发写 → WrongExpectedVersion（已测）。

## 6. 测试清单（10 个，全部落地）

- tests/test_d16_interactive_session.py：
  1. 白名单投影（只 user_input + final_text；未终态 Turn 无 assistant 回声）；
  2. 有界截断（turns 与 chars 两个维度）；
  3. 压缩：权威投影 + [untrusted-session-summary] 标记；
  4. 压缩失败降级为仅权威投影；
  5. from_thread 未知 thread fail-closed；
  6. chat 第二轮能看到第一轮（scripted provider 断言 request 含历史）；
  7. 非法 thread_id 干净失败；
  8. 审批 ASK → approve → resume → completed；
  9. 过期 thread 版本被拒（WrongExpectedVersion）；
  10. --repo 在 assembly 前生效。
- examples/day16_interactive_session.py：4 轮对话 + 压缩触发（离线确定性）。
- 全量回归：362 tests OK（基线 352 + 10）。

## 7. 真实模型验证（人工、opt-in，尚未执行）

- 复用 D15 smoke 的 fixture + Qwen/Qwen3.5-35B-A3B + reasoning_effort=off；
- 手动验证两轮对话：第 2 轮提问"我上一轮修了什么？"模型能答出；
- 压缩验证：把 history_max_turns 调到 2，跑 4 轮，确认摘要生成且行为可解释。

## 8. 边界（不做，写入推迟风险）

- 不做 TUI / 富交互 / 流式打字机渲染；不做 LSP；不做语义检索。
- 不做阶梯④检索召回、阶梯⑤ session journal。
- 压缩 v1 不含"改动的文件集合（git 证据）"（v2）。
- 不做多线程/多会话并发调度；不做跨 repo 会话；子 Agent 不共享会话记忆。

## 9. Definition of Done（全部满足）

- 接口与 10 个测试落地；全量 362 绿；示例可跑。
- 真实模型交互验证：**人工 opt-in，尚未执行**（见 §7）。
- 路线图 D16 → COMPLETE；README 增补 interactive 用法。

## 10. 推迟风险

- 真实模型会话验证未做（需要人工跑 + SF key）。
- 压缩模型摘要未在真实 client 上端到端验证（summarize_via_client 已接，未付费验证）。
- 会话连续性依赖 <db>.session.json 单文件；多用户/多会话并存未处理。
