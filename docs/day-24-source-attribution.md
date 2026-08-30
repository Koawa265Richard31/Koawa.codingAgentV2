# D24：不可信内容来源归因（untrusted provenance attribution）

> 状态：PLANNED（2026-08-30，设计草案，未动代码）。
> 北极星：当注入/不可信内容进入模型上下文时，能回答"是哪一轮交互、哪个工具调用、
> 哪个文件把它带进来的"——即 AgentSentry 论文（arXiv:2602.22724）的
> Temporal Causal Diagnostics 在本项目事件溯源架构上的结构性落法。
> 参考会话：session-4a5bd80d（AgentSentry/OWASP 学习会话，2026-08-30）。

---

## 1. 为什么这是缺漏（证据）

- 威胁模型（docs/agent-security-threat-model.md）只承诺"拒绝 + 审计"：8 个 D21 用例
  全部是"被拒 + 传输层零调用"，**没有任何路径记录"哪个 context item 的来源"**。
- 事件溯源（D6 facts / tool ledger / I7 RunEffectIndex）逐轮记录了 model/tool 事实，
  具备回答"哪一轮带入"的全部原材料，但**没有投影层把 provenance 挂到 context item 上**。
- AgentSentry 的核心能力（Temporal Causal Diagnostics）正是这种归因；
  它属于**结构性归因**，不是威胁模型 §4.2 明确不做的启发式注入检测——两者不冲突。

## 2. 目标与不目标

### 2.1 目标

1. 每个进入模型请求的 context item 可携带 provenance 元数据：
   来源类型（repo_file / tool_result / mcp_result / user_text / compaction /
   conclusion / echo / journal）+ 事件流引用（stream_id + version）。
2. 发生疑似注入时，`/attribution <关键词>` 能返回"该内容首次进入上下文的轮次、
   工具调用、文件"的可审计答案（确定性，从事件流重建）。
3. trace 记录归因查询结果，支持面试叙事与事故复盘。

### 2.2 不目标

- 不做启发式注入检测（威胁模型 §4.2 边界不动）。
- 不改变 D16 白名单投影语义（tool_calls 仍不进交互投影）。
- 不引入净化/删除机制（D23 已用 untrusted 标记闭环）。
- 不承诺识别"任意恶意意图"——只回答"内容从哪来"。

## 3. 设计方向

### 3.1 provenance 附着点（建议）

| 投影层 | 附着方式 |
|---|---|
| D6 run-execution context | context_document 增加可选 `provenance` 字段（来源类型 + stream ref） |
| 交互投影（session.py） | `_conclusion_text` / `_failed_turn_text` 已带 turn_id，补来源标签 |
| compaction replacement | 已有 `[run-history-compaction]` 标记，补 source range 引用 |
| recall 命中 | RecallHit 增加 source refs |

### 3.2 归因查询（建议）

`/attribution <token>`：
1. 在 recall 索引（D23 §6）命中的 turn 上，沿 D6 facts 回溯该 token 首次出现的
   model.turn-completed / tool.result-recorded 事件；
2. 输出：turn_id、model_round、tool_name、call_id、changed_files（如适用）；
3. 全部确定性重建，无模型调用。

### 3.3 接口与文件（草案）

- `runtime/attribution.py`（新增）：provenance DTO、`attribute(store, thread_id, token)`。
- `runtime/session.py`：`/attribution` 命令接线。
- `recovery/execution.py`：context_document provenance 字段（向后兼容，缺省 None）。
- `tests/test_d24_attribution.py`：注入场景 → 归因到具体轮次/工具/文件。

## 4. 决策点（留待实现文档拍板）

- D1：provenance 落 context_document 是否会造成 I4 durable JSON 兼容问题
  （建议：新 schema_version 或可选字段）。
- D2：是否对全部 context item 打标，还是只对"不可信来源"打标。
- D3：归因查询的扫描上限（沿用 recall_scan_max_turns 或独立上限）。
- D4：trace 记录归因查询是否必须（建议：是，成本极低）。

## 5. 验收（草案）

1. 注入场景测试：恶意指令经 tool result 进入 → `/attribution` 命中该轮+工具+调用。
2. 全量回归全绿；D21 8 用例不回退。
3. trace 有归因记录；DB canary 无新增敏感内容。
