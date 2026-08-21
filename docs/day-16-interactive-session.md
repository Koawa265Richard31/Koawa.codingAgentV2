# D16：交互式会话（Interactive Session + 会话记忆投影与压缩）

> 状态：PLANNED（NEXT）。本文档是 D16 的完整设计与验收合同，未经用户确认不进入实现。
> 背景结论：D1–D15 把 Agent Loop 收敛到任务粒度，任务内闭环真实（真实模型 smoke 已验证）；
> 但同一线程的新 Turn 不投影历史对话——存储有记录，模型看不见。复杂 coding 任务无法
> 收敛到单任务粒度，必须补"会话记忆"这一层。

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
- 无：新 Turn 启动时，TurnWorker 上下文 = 系统指令 + 本轮输入（execution/worker.py
  现行为），不含同线程历史 Turn 的任何投影。run 因此是独立任务。
- 无：CLI 只有一次性命令（run/resume/status/...），无对话式交互入口。
- 有间接记忆：git 工作树保留文件改动（连续任务可"接着改"），但模型不记得
  为什么改、改了什么、证据在哪。

## 3. 记忆阶梯与 D16 定位

业界上下文管理不是"把历史全塞回去"，而是阶梯：

| 层 | 做法 | D16 |
| --- | --- | --- |
| ① 窗口内全量 | 会话短时全部保留 | 实现 |
| ② 截断 | 超限丢最旧 Turn | 实现 |
| ③ 压缩/摘要 | 被丢旧轮压成"会话摘要"常驻上下文 | 实现 |
| ④ 检索召回 | 从事件存储按需召回"我对 X 做过什么" | 边界（原料已具备，后续日） |
| ⑤ 外部工件 | plan/TODO/证据写入 repo（session journal） | 边界（后续日） |

D16 交付 ①②③：有界历史投影 + 超限截断 + 会话压缩。④⑤ 写入"推迟风险"。

## 4. 核心设计

### 4.1 会话模型

- thread = 会话。交互模式下，一个 thread 对应一次 CLI 会话（持久化在 config.db），
  会话内每个用户输入 = 一个新 Turn。
- 新 Turn 的模型上下文 = 系统指令 + 历史投影 + 本轮输入。
  接口变更：TurnWorker.__init__ 增加 initial_context: Sequence[ModelContextItem]
  参数（fresh-turn 分支插在 instructions 之后、新输入之前；resume 路径不注入，
  仍以 D6 重放为准——实现时落地）。
- 投影只发生于 fresh turn；Turn 内仍由 Agent Loop 自行累积。

### 4.2 历史投影（白名单 + 有界）

- 来源：ThreadRuntime.get_thread 取 thread 内 Turn 列表，对每个已终态 Turn
  投影两条消息：
  - UserMessage(input_id="history:<turn_id>", content=turn.user_input)
  - AssistantMessage：文本取该 Turn 事件流中最后一个 assistant 文本项
    （从 model.turn-completed.v1 的投影重建；确定性、无额外存储）。
- 白名单：只投影 user_input 与最终回答。绝不注入原始工具参数、工具结果全文、
  trace 明细、凭据或推理内容（reasoning 不落库、不回传，延续 D2 契约）。
- 有界（配置项，落 RuntimeConfig）：
  - history_max_turns（默认 16）
  - history_max_chars（默认 32_000 字符）
  - 超限时从最旧 Turn 开始截断；被截断的旧轮进入压缩（见 4.3）。
- waiting_for_approval 的 Turn 不进入历史投影，等待行内审批闭环（4.5）。

### 4.3 会话压缩（阶梯③）

- 触发：被截断的旧 Turn 数量达到阈值（默认 ≥4 个）。
- 机制（复用 D13 Compactor 的权威/不可信二分法）：
  - 权威部分（typed 投影，确定性拼接，绝不由模型总结）：每个被压 Turn 的
    turn_id / 终态 / 错误码 / 改动的文件集合（从 git 证据）；
  - 不可信部分：让当前 Provider 把被压 Turn 的 (user_input, final_text) 对
    摘要成一段 [untrusted-session-summary]（显式标记，与 D13 一致）。
  - 压缩结果以 UserMessage（权威投影）+ 摘要文本常驻会话头部，替换原始消息。
- 压缩是一次额外的模型调用；压缩期间新 Turn 先按"截断但未压缩"继续（旧的仍丢、
  新的照常），保证交互不被卡住。

### 4.4 工作目录自主选择

- interactive --config cfg.json [--repo PATH]：--repo 在 assembly 之前覆盖
  config.repo（AppRuntime.from_config_file(path, repo_override=...)），支持任意
  目录（含当前目录 .），路径须存在（assembly 校验 repo_not_found）。
- 不加 --repo 时使用配置里的 repo。会话归属（thread）按 repo 区分。

### 4.5 交互 CLI（纯 CLI，非 TUI）

新子命令：

```text
koawa-agent-v2 interactive --config cfg.json [--repo PATH]
```

- 循环：打印横幅与 /help；input() 读行；空行跳过。
- 普通输入 → AppRuntime.chat(message, thread_id, history)：同一 thread 上新 Turn，
  打印 agent> final_text 与紧凑工具摘要（工具名 + 结果码，不刷原始参数）。
- 行内审批：Turn 进入 waiting_for_approval 时，终端提示
  approve <request-id>? [y/n]，y 走 resolve_approval(..., resume_after=True)
  并打印恢复结果；n 拒绝。
- 命令：/status、/approvals、/approve <id>、/deny <id>、/resume <turn-id>、
  /history（打印会话记录摘要）、/exit；EOF（Ctrl+Z）与 Ctrl+C 干净退出，
  会话状态已持久化，重开 interactive 可继续（thread 复用）。
- 无流式打字机渲染：Turn 完成后一次性打印（可后续日增强，不阻塞 D16）。

### 4.6 新增接口（实现清单）

- execution/worker.py：TurnWorker(..., initial_context=())（fresh-turn 注入）。
- runtime/app.py：AppRuntime.chat(message, *, thread_id=None, history=(), event_sink=None)
  与 from_config_file(..., repo_override=None)；历史投影构建函数（白名单 + 有界）。
- runtime/session.py（新）：SessionHistory 投影/截断/压缩（职责单一）。
- runtime/cli.py：interactive 子命令 + 交互循环。
- runtime/config.py：history_max_turns / history_max_chars / compact_min_turns。

## 5. 失败路径（全部要有测试）

- 历史投影失败（事件损坏/读取异常）→ 降级为单轮并打印 session_history_unavailable，
  不阻断新 Turn。
- 历史超限 → 截断；截断后再压缩；压缩后仍超限 → 拒绝本轮并提示缩短会话。
- 压缩 Provider 失败 → 保留截断态继续，[untrusted-session-summary] 缺失时权威投影仍可用。
- 审批中断（waiting Turn 存在）→ 会话内其余 Turn 照常；被等 Turn 只能 approve/deny
  闭环，不并入历史。
- stdin EOF / Ctrl+C / 空输入 → 干净退出，不产生脏 Turn。
- 同 repo 两个并发 interactive → thread 版本冲突报 wrong_expected_version，
  提示换 thread 或接续（不自动合并）。

## 6. 测试清单

- tests/test_d16_interactive_session.py：
  1. fresh Turn 注入 initial_context（scripted provider 断言第二轮请求含第一轮文本）；
  2. 白名单：工具参数/结果不进入历史投影；
  3. 有界截断：超 turns/chars 时最旧被丢；
  4. 压缩：权威投影（终态/文件/错误码）保留 + [untrusted-session-summary] 标记，
     未终态 Turn 拒绝压缩；
  5. 投影失败降级 + 错误码；
  6. 行内审批闭环（ASK → y → resume → completed）；
  7. thread 版本冲突错误码；
  8. --repo 覆盖在 assembly 前生效。
- 全量回归保持绿；基线预期从 352 增长。
- examples/day16_interactive_session.py：scripted provider 跑一段 3 轮会话，
  打印每轮投影的上下文规模与压缩触发点（离线、确定性）。

## 7. 真实模型验证（人工、opt-in）

- 复用 D15 smoke 的 fixture + Qwen/Qwen3.5-35B-A3B + reasoning_effort=off；
- 手动验证两轮对话：第 2 轮提问"我上一轮修了什么？"模型能答出（证明投影生效）；
- 压缩验证：把 history_max_turns 调到 2，跑 4 轮，确认摘要生成且行为可解释。

## 8. 边界（不做，写入推迟风险）

- 不做 TUI / 富交互 / 流式打字机渲染；不做 LSP；不做语义检索。
- 不做阶梯④检索召回（事件存储原料已具备，属后续日）。
- 不做阶梯⑤ session journal 工件写盘。
- 不做多线程/多会话并发调度；不做跨 repo 会话。
- 子 Agent（D11/D12）不共享本会话记忆。

## 9. Definition of Done

- 上述接口与测试全部落地；全量测试绿；示例可跑。
- 真实模型交互验证通过（见 §7），并把结果记入本文档。
- 更新 docs/15-day-coding-agent-roadmap.md 状态表：D16 → COMPLETE，D17 → NEXT
  （若存在）；README 增补 interactive 用法。
- 本文档顶部状态改为 COMPLETE，并追加"实测结果"节。
