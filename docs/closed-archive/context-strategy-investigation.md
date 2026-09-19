# 上下文策略现状调查（2026-09-12）

基线：HEAD `b29e4c6` 之后的工作区。只描述现状，不做研究对齐、不给改进方案。所有结论带 file:line 锚点；关键断言经主审直接复核（§7 标注）。

## 0. 一句话画像

当前项目的上下文策略是：**固定窗口 + 确定性权威投影 + 不可逆截断**。仓库里存在三套互相独立的"压缩"实现，只有会话级一套接在生产上；两条跨 turn 记忆通道（turn conclusion、/recall）实现了机制但生产空转；模型没有任何主动记忆工具；恢复 = 事件流全量重放（含压缩事实，逐字节等价）。

## 1. 模型每次看到的上下文怎么拼

- 新 turn：`instructions（=1 条 SYSTEM 系统提示）→ initial_context（交互会话 = SessionHistory.context_items()）→ 本 turn 用户输入`（`execution/worker.py:199-209`；instructions 来源 `runtime/assembly.py:568-597`，默认提示词 `runtime/config.py:919-936`）。
- AgentLoop 每个模型轮次把**整个 context 原样全量**发出去（`execution/loop.py:604-611`）——turn 内没有逐轮裁剪。
- resume turn：上下文完全来自 `run-execution` 事实流的重放投影（`worker.py:170-187`），不重新注入会话历史。
- 会话历史限窗：先按 `history_max_turns`（默认 16）截尾、再按 `history_max_chars`（默认 32k 字符）从最旧丢（`runtime/session.py:272-284`；消费点 `runtime/cli.py:613-617`）。

会话注入顺序（`session.py:321-397`，生产）：AGENTS.md 项目注记（untrusted）→ `session:plan` 计划投影 → journal 提醒（确定性触发：距上次 journal ≥ N turn / 最新 turn 失败 / 改动文件超阈值）→ 压缩块 → 窗外结论块（实际恒空，见 §3）→ 窗内 turn → 失败 turn echo（最多最近 3 条，`failed_echo_max_turns`）。

## 2. 三套"压缩"（关键事实：只有 (a) 在生产）

**(a) 会话级压缩（生产接线，`runtime/session.py:286-352`）**
- 触发：窗口挤出 ≥ `compact_min_turns`（默认 4）个新 turn 时。
- 算法：被丢 turn → **确定性权威投影**（纯函数、代码生成、永不模型书写，`session.py:572-589`）；若配置摘要回调则**附加**一段模型摘要，显式标记 `[untrusted-session-summary]`（`session.py:53,303-307`），摘要失败回落纯投影。生产摘要器 = 用主模型发一次独立请求（`session.py:637-662`，接线 `cli.py:621-628`）。
- 不可逆：被丢原文只留在内存 `CompactionResult.dropped`（`session.py:154`），无任何 re-inflate 路径。

**(b) 回合内 in-run 压缩（机制完备、生产未接线）**
- 机制：预算 soft(48k)/hard(64k)+reserve(8k) 字符；超 soft 且无 pending 工具调用时，把上下文切成"模型轮+配对结果"的闭合组、压掉较旧的组（保留最近 4 组锚），替换物是**确定性文本**（epoch 计数+工具名清单），非模型摘要（`execution/compaction.py:164-252`、`execution/loop.py:312-392`）；超 hard 无可压组 fail-closed `context_capacity_exhausted`。
- 持久化：intended+compacted 两事件原子落事实流，恢复重放逐字节等价（`recovery/execution.py:666-842`、`recovery/context.py:550-738`）。
- **生产断点（已直接核实）**：`assemble_execution_plane` 与 chat 模式 `build_worker` 构造 AgentLoop 时均不传 `memory=`/`compaction_sink=`（`runtime/assembly.py:575-585、169-181`；loop 的参数定义 `execution/loop.py:232,295`）→ 生产 loop 内 `_maybe_compact` 直接 return。soft/hard 预算在生产不生效。仅测试手工接线（`tests/test_d23_loop_compaction.py` 等）。

**(c) D13 Compactor（演示级）**：唯一 src 使用者是 `runtime/unified.py:68-71,132-141`（演示组合，摘要硬编码），不在生产 run/chat 链上。

## 3. 跨 turn 记忆：两条通道、生产都空转

- **TurnConclusion**（`runtime/turn_conclusion.py`，注意路径在 runtime/ 下）：终态 turn 的有界权威摘要（状态/错误码/成功工具/改动文件/测试证据引用/未了结事项/不确定码 + untrusted 摘要位），只从 durable 事实构建、幂等持久化到 `turn-memory` 流、带过期校验（`turn_conclusion.py:377-471,475-525,544-563`）。**生产只读不写**：cli.py:618-620 构造 store 交给 SessionHistory 读（`session.py:439-449`），`build/persist` 的调用只存在于测试 → 会话里的"窗外结论块"在真实运行中恒为空（已核实）。
- **SessionMemory.recall**：按 thread 的 IDF×字段权重×recency 词法召回（`session.py:676-737,968-1038`），**只挂在 CLI 用户命令 `/recall`**（`cli.py:754-774`）——模型没有对应工具（已核实：工具目录 read_file/list_files/search_text/apply_patch/run_test_profile/git_status/git_diff/finalize_task/update_plan/repo_map，无 recall/memory/journal）。
- **SessionJournal**：会话写仓库内 SESSION.md，`/journal` 用户命令触发 + 确定性提醒注入（§1）。

## 4. 恢复与压缩的交互

kill/resume 后模型看到 = 事实流**全量重放投影**（非摘要），重放会应用压缩事实 → 恢复后即压缩后视图，与崩溃前逐字节等价（`recovery/context.py:299-612`、`test_d23_in_run_compaction.py:133`）。被压掉的原文只存在于事件流中供审计/重放验证，投影层永不恢复。checkpoint 缓存（`recovery/execution.py:1040-1060`）只是加速，必须与全量重放逐字段相等才被采用（`recovery/coordinator.py:86-152`）。

## 5. 配置面：生效 vs 声明未消费

生效（生产消费点存在）：`history_max_turns/history_max_chars/compact_min_turns`（cli.py:613-617）、`fallback_summary_model`（仅交互收尾失败时的一次性提示摘要，cli.py:1015-1034，不改变上下文）、MemoryConfig 的 `conclusion_max_chars/conclusion_recent_limit/failed_echo_max_turns/in_run_keep_groups` 等经 SessionHistory 的部分。

**声明但零消费**（grep 仅 memory.py 定义与配置测试）：`conclusions_enabled`、`conclusion_model_summary`（→ TurnConclusion 的 untrusted_summary 恒 None，`turn_conclusion.py:463,467`）、`recall_scan_max_turns`、`max_compaction_source_groups`、`compaction_summary_max_chars`、`journal_inject_latest`（`runtime/memory.py:104-148`）。

## 6. 确认不存在的机制（grep 证据 + 主审复核）

1. 生产回路内没有回合内压缩生效（§2b，直接核实）。
2. TurnConclusion 无生产写入方（§3，直接核实）。
3. 模型无主动记忆/召回工具（§3，直接核实）。
4. 无语义/向量检索（全库无 embedding/vector/bm25；recall 是纯词法 IDF）。
5. 无遗忘/衰减机制（memory 核心模块无 forget/decay/ttl）。
6. 无跨会话/跨 thread 记忆（recall 与 from_thread 均单 thread 过滤；根目录 `rt1-memory.sqlite3` 无任何代码引用，属遗留产物）。
7. 无工作/长期记忆分层；设计注释里的 "MemoryEnvelope" 无实现（仅 `turn_conclusion.py:14`、`config.py:724` 两处注释）。
8. 压缩不可逆（§2a、§4）。

## 7. 核实标记

"生产未接线"三项（in-run 压缩、TurnConclusion 只读、无 recall 工具）由主审以直接读码/grep 复核（assembly.py:575-585 AgentLoop 实参、cli.py:609-663 conclusion 流向、工具目录清单）；其余锚点来自只读盘点代理，抽样未发现偏差（一处路径修正：turn_conclusion.py 位于 `runtime/` 下）。
