# D13 / D23 上下文与记忆阅读笔记（2026-09-16）

## 1. 文档状态

- 阅读基线：本地 HEAD 与已有 `origin/main` 引用均为 `2d42746`；本次没有执行远端 fetch。
- 阅读范围：D13 Repository Context / Compaction，以及正式 runtime 中承接这些能力的 D19、D23、D24 增强路径。
- 当前阶段：尚未读完全部增强链。先记录实际行为和缺口，读完后再决定压缩、检索、缓存与容量策略。
- 问题明细继续以 `docs/open-active/d12-i7-receipt-reuse-followup.md` 为台账；本文用于解释机制和保留学习上下文。

## 2. 核心对象关系

一次用户请求创建一个 Turn。一个 Turn 可以进行多次模型调用和多次工具调用，也可能在恢复后经历不同 Run。

```text
Thread（连续会话）
└─ Turn（一次用户请求）
   └─ Run（一次执行尝试）
      ├─ ModelTurn 1
      │  ├─ assistant text
      │  ├─ tool call A
      │  └─ tool result A
      ├─ ModelTurn 2
      │  ├─ tool call B
      │  └─ tool result B
      └─ final text + terminal commit
```

模型生成最终回复不等于 Turn 已完成。最终回复会先进入执行记录；运行时还要记录 completion evidence，并以正确的 expected version 提交 Turn 终态。

三种持久化记录承担不同职责：

| 记录 | 写入时机 | 用途 |
|---|---|---|
| 工具账本 | 工具执行过程中 | 判断登记、claim、已有结果、未知结果及能否重试 |
| Run execution 事件 | 模型与工具循环过程中 | 崩溃后重建上下文、阶段和执行进度 |
| TurnConclusion | Turn 终态后 | 为后续 Turn 提供有界的结构化历史结论 |

TurnConclusion 不是整轮原始记录的打包，也不替代账本和执行事件。

## 3. Run 内自动压缩

### 3.1 触发条件

下一次请求模型前，运行时计算当前上下文与预留空间。超过软上限时尝试压缩；无法安全缩减且超过硬上限时，拒绝发送请求并报告 `context_capacity_exhausted`。

压缩还要求：

- 已启用 Run 内压缩；
- 当前 Run 已绑定 durable recorder；
- 没有 recorder 已知的待完成工具调用；
- 存在较老、完整闭合且不属于最近保护组的执行组。

### 3.2 完整执行组

压缩的最小单位是一次 ModelTurn 及其全部配对工具结果。每个 call ref 都有唯一结果时，执行组才闭合。

开放调用可能仍在执行、等待批准、响应丢失或结果未知，因此不能进入压缩范围。组闭合只是成为候选的必要条件，不表示此时一定触发压缩。

### 3.3 两阶段持久化与恢复

压缩依次记录：

```text
run.context-compaction-intended.v1
→ run.context-compacted.v1
```

intended 记录 epoch、来源版本范围、压缩前上下文 digest 和来源身份 digest。compacted 保存实际 replacement item 及替换后上下文 digest。

只有 intended 落盘时，恢复仍采用未替换的原上下文。intended 与 compacted 都落盘时，恢复校验后采用事件中保存的 replacement；崩溃前内存是否完成替换不影响结果。缺少 intended 的 compacted、epoch 跳号、范围错误、开放调用或 digest 不一致都会拒绝重建。

这些校验保证恢复结果与记录一致，不保证摘要内容完整或业务结论正确。

### 3.4 当前压缩内容

正式 Run 内 replacement 当前主要保留：

```text
压缩的闭合组数量
涉及的工具名
累计工具调用数
```

测试退出码、失败原因、文件发现和未完成义务可能没有进入 replacement。函数级对照已证明合成的成功与失败工具结果能够生成完全相同的压缩块。

## 4. 不能随执行组压缩的内容

`InstructionMessage` 和 `UserMessage` 不会被解析为执行组。原始用户目标、运行时生成的 session 压缩块和 Run 压缩块因此受到类型保护。最近 `in_run_keep_groups` 个闭合组也暂时保留完整细节。

最近组保护会随着执行推进而移动。旧测试结果退出最近窗口后，仍可能进入压缩范围。类型保护只表示保留原文，不表示消息内容可信或拥有审批权限。

历史压缩块也使用 UserMessage 承载，因此当前 Run 内的执行组压缩不能继续缩减这些块。

## 5. 仓库导航与读取

正式工具路径目前是：

```text
repo_map → search_text → read_file
```

`repo_map` 返回有界目录树和 Python 符号名称，不返回完整代码，也不分析业务语义或调用关系。`truncated=true` 表示结果因限制不完整；缺少某个路径不能证明仓库中不存在对应模块。

`search_text` 执行有界字面匹配，不解释正则或语义。无命中时还要检查 `truncated`、`skipped_too_large`、二进制及非法 UTF-8 跳过计数。第一条结果只是按路径、行号、列号排序，并非最相关结论。

`read_file` 返回局部 UTF-8 文本和本次读取的完整文件 SHA-256。完整哈希可以检测文件其他位置的变化，但不能证明模型理解了完整文件。补丁基于 H1 构造后，文件变为 H2 时应拒绝原补丁并重新读取、重新判断；不能只替换预期哈希后继续写入。

高耦合模块可能出现局部测试通过、组合运行失败并不断返工。导航与读取工具提供调查能力，但不强制调用方分析或组合验证。此项已登记为 `REVIEW-001`，等待后续规划与验证链阅读后裁定。

## 6. 跨回合历史检索

`SessionMemory.recall()` 只扫描同 Thread 的终态 Turn。它根据用户输入、最终回复、错误和工具名做关键词匹配，并结合关键词稀有度与新近程度排序。

高分只表示相关且可能较新，不证明历史结论正确，也不证明仍适用于当前代码。历史测试要成为当前交付证据，还需绑定当前代码版本、测试版本、候选 generation 和可核验证据。

当前正式入口是 CLI `/recall`：查询结果打印给用户后直接继续循环。该分支不修改 SessionHistory，也不自动把结果注入下一次模型请求。它也不返回当前活动 Run 被压缩的原始工具结果。

因此，目前没有确认一条正式模型工具链可以按引用检索旧工具结果并把片段重新放入上下文。

## 7. TurnConclusion

Turn 终态后，`TurnConclusionStore.build()` 从 Turn、Run、工具执行、workspace effect、completion evidence 和中断状态重建有界结论，包括：

- Turn / Run 状态；
- 稳定错误码；
- 成功工具；
- 修改文件；
- 测试或完成证据引用；
- 未完成义务；
- 不确定结果；
- 权威内容 digest；
- 可选不可信摘要。

模型最终回复也被记录：执行事件保存模型输出，成功 Turn 将最终文本保存为 outcome，后续近期会话历史可使用它。

窗口外旧回合的 TurnConclusion 会被转换成 `[reconstructed-turn-conclusion]` UserMessage。但当前投影没有输出已持久化的 `test_evidence_refs` 或等价取回引用，因此模型不能沿旧结论核验测试对应的版本。

TurnConclusion 的持久化目前是 best effort。生成或保存失败不会阻止 Turn 进入终态，后续历史可能缺少该结论。

## 8. 失败回显

近期 SessionTurn 没有 final text 时，`SessionHistory.context_items()` 可以生成：

```text
[reconstructed-turn-outcome]
status=failed
errors=resource_budget_exceeded
files=src/auth.py
[/reconstructed-turn-outcome]
```

不过连续 CLI 路径当前只在 `outcome.ok` 时将回合 append 到内存 SessionHistory。失败分支只打印错误或收尾摘要，因此用户立即输入“继续”时，下一次请求可能看不到刚失败的 Turn。进程重启后通过 `SessionHistory.from_thread()` 从数据库重建，终态失败回合才会重新进入候选历史。

Turn 失败不表示此前没有副作用。继续执行前需要检查实际仓库状态、Git diff、文件哈希、工具账本和远端回执；结果未知的外部操作不能因 Turn 失败而盲目重试。

## 9. Session journal

CLI `/journal` 从数据库重建终态回合，确定性导出 `SESSION.md`。写入通过 `JOURNAL_EXPORT` effect 管理：登记意图、claim、临时文件、fsync、replace、目标哈希校验、APPLIED。

journal 的内容哈希一致只证明预期文本成功写入。文件内“测试通过”等最终回复仍可能只是模型陈述，不能替代测试证据。

当前存在 journal reminder，但它只建议用户执行 `/journal`。没有确认 `journal_inject_latest` 已接入，也没有发现 SESSION.md 正文自动进入后续模型上下文的正式路径。

## 10. 跨回合上下文组装

新聊天 Turn 的首次上下文大致依次包含：

```text
系统指令
→ 不可信项目说明
→ 当前计划投影
→ journal reminder
→ 旧 session 压缩块 S1、S2……
→ 窗口外 TurnConclusion 文本
→ 近期用户消息与最终回复
→ 可用的失败回显
→ 当前用户请求
```

进入 Run 后，模型文本、工具调用和工具结果继续追加。工具定义也属于模型请求的一部分并占用容量。

数据库中的全部账本、执行事件、测试日志和仓库代码不会自动整体进入上下文。记录存在与模型可见是两件事。

## 11. Session 压缩与模型摘要

近期历史默认最多 16 个 Turn、32,000 字符。旧回合累计达到阈值后生成一个 CompactionResult，其中包含程序生成的确定性投影，以及可选的 `[untrusted-session-summary]`。

确定性投影保存有限状态、错误、文件和用户请求片段。可选模型摘要的输入只有旧回合 user input 和 final text，不含完整工具结果、账本、测试日志或 TurnConclusion 证据引用。摘要失败时系统保留确定性投影。

交互 CLI 对具备 `_endpoint` 的模型客户端提供 `summarize_via_client()`。该摘要请求使用同一 provider/model、无工具、最多 512 output tokens。

CompactionResult 只保存在当前 SessionHistory 内存中。重启后系统从终态 Turn 重建历史，再次达到阈值时重新调用摘要模型，所以摘要文本可能变化，也会再次产生调用成本。

## 12. 已确认的长期容量问题

SessionHistory 的近期窗口有界，但 `_compacted` 块持续追加；`context_items()` 每次注入全部已有压缩块，没有为其设置累计预算或滚动合并。

函数级复现使用：

```text
max_turns=2
max_chars=200
compact_min_turns=2
连续加入 20 个短成功回合
```

最终仍保留 9 个 session compaction 块，共 1,853 字符。近期窗口有界不代表整份历史投影有界。

这些块是 UserMessage，当前 Run 内 closed-group 压缩不能缩减它们。最终硬上限可以拒绝超界请求，但不能使长期会话持续前进。

## 13. 今日确认并登记的事项

| 编号 | 内容 | 状态 |
|---|---|---|
| D13-D23-001 | Run 内压缩丢失工具结果语义 | 静态确认并完成成功/失败对照复现 |
| D13-D23-002 | 旧回合结论未投影测试证据引用 | 静态确认 |
| D13-D23-003 | CLI 连续聊天失败回合未加入历史 | 静态接线确认 |
| D13-D23-004 | 跨回合压缩块持续累积 | 静态确认并完成 SessionHistory 级复现 |
| D13-D23-005 | 跨回合模型摘要未持久化绑定 | 静态恢复路径确认 |
| REVIEW-001 | 高耦合模块依赖分析与组合验证 | 待后续链路核查，尚未认定缺陷 |

具体证据、复现范围和影响边界见问题台账。上述问题不等于已证实的测试门禁绕过或错误交付；当前交付检查仍可独立核验当前 Run 的证据。

## 14. 暂缓确定的设计方向

当前已讨论但尚未选定的组合包括：

- 关键状态与未完成义务的有界投影；
- 长工具结果外置保存及剪枝；
- 按稳定引用分页取回旧工具结果；
- 跨回合摘要滚动合并；
- 稳定前缀与阶段性压缩以控制缓存成本；
- 用任务成功率、信息遗漏、缓存费用、检索与重试成本共同选择窗口大小；
- 容量仍不足时的受控续接。

读完 D13 与增强链后，再根据现有运行时可复用能力、已确认断点和实测成本统一确定方向。

## 15. 下次续读位置

继续核对剩余记忆与请求容量链，重点包括：

1. Session 压缩、TurnConclusion、近期历史与 Run 内预算之间是否存在遗漏的统一 envelope 检查；
2. 可选模型 TurnConclusion summary 的实际接线；
3. journal 配置字段与注入路径；
4. 历史工具结果是否存在尚未发现的引用读取接口；
5. 完成 D13 与增强链后，对压缩、检索、缓存和窗口策略作统一裁决。
