# R2 回收带读单元（备料完成，2026-10-16）

三产出目标：记忆恢复 + 博客骨架填空 + 5 分钟脱稿面试答案。
单元原则：检查点卡你自己的词，不代填答案。

## 材料清单（读序）

| # | 材料 | 行数 | 角色 |
|---|---|---|---|
| 1 | `docs/open-active/implementation-closure-review-2026-09-25.md` L42-58, L255-258 | 摘读 | 复验裁决原话：R2 为什么当时"不称闭环" |
| 2 | `docs/open-active/hardening-memory-retrieval-2026-09-19/implementation/r2r4-progress.md` | 245 | R2 全部修复记录（主线教材） |
| 3 | `src/koawa_agent_v2/recovery/delivery.py` | 204 | 切片(a) 交付门实现（centerpiece） |
| 4 | `src/koawa_agent_v2/recovery/store.py` | 355 | 投影发布/幂等/状态归并 |
| 5 | （按需）`recovery/execution.py`、`control/sqlite_store.py` | 1208/1376 | 只在问到时翻，不全文读 |

## 块序列（5 块 + 收尾）

**Block 0 定位**：R2 守 ②→③。检查点：你说过"副作用不可逆 → 发布必须
先落盘"——R2 里哪个机制是这句话的字面实现？哪个部分复验裁决说还不是？
（裁决原话："发布失败后原回执仍会进入上下文……完整契约不存在"）

**Block 1 来源身份绑定**：字段标记≠来源证明。回执须同时满足 policy 版本
匹配 + ledger 上存在同身份可信执行记录。检查点：`untrusted` 和
`unverified` 都"绝不发布"，为什么还要区分？（fail-closed 的两种味道：
来源不匹配 vs 读取失败）

**Block 2 发布身份四元组**：(turn_id, model_turn_id, call_id, content_sha256)。
案例：首版身份漏掉 model_turn_id → 两轮复用 c1 同内容被折叠成 1 条投影。
检查点：为什么 call_id 单独不够用？（它只在所属模型轮次内唯一）

**Block 3 发布时序**：`tool_completed` 之后、`context.extend` 之前调用
publisher（主循环+resume 两处接线）；钩子故障隔离（失败登记 unavailable，
绝不 fail turn、绝不重执行工具）。检查点：为什么发布失败宁可让回执裸进
上下文也不重执行工具？（重执行 = 副作用二次发生，违反 R2 的第一性）

**Block 4 交付门切片(a) 修正版 B**（delivery.py 精读）：SPEC-1 幂等键
(turn, model_turn, call) 不含内容维度 + IdempotencyConflict；SPEC-2
`canonical_text` 单一实现点 + 三摘要分立（source_digest /
projection_payload_digest / delivery 指纹）。检查点：为什么幂等键故意
**不含**内容维度，而内容进指纹？

**Block 5 按引用读取**：`read_result_projection` 三态可用性、
`ambiguous_reference` 歧义边界。切片(b) 复验通过。

**收尾产出**：①博客骨架对应节填空（去博客仓库找 R 系列骨架）；
②5 分钟脱稿答案写出来发我审。

## 已知残余（讲答案时必须如实带出，这是加分项不是丢分项）

- 首轮回执来自工具执行路径（metadata-only）而非已提交投影——完整首轮
  缓冲需改 loop 协议，独立切片
- recall 接投影流读取归 R3 残余
- R1 残端（恢复入口补发 + 重启后状态展示）按 closure review 并入 R2 收口

## 备注

课程 = R2 → R3 → R4 → R5（用户 2026-10-16 定；R1 暂缓）。每单元同构：
备料（本文件）→ 带读+检查点 → 博客填空 → 5 分钟答案审。
