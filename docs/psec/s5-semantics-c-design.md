# S5 语义 C 详细设计（评审稿 v0.1）：J2 canary 种子的委派链传播

PSEC 轨道。日期：2026-09-06。基线：a9d8896（威胁模型含 T7，hash 3d131d82）。**评审稿，未实现**；实现需另行版本化切片审批，且须先解决 §5 前置问题。本文取代 `s5-delegation-security.md` §3 中"语义 C（digest 种子集上浮）"的粗粒度构想，并含一处对原构想的**修正**（§2.3）。

> **实现记录（2026-09-06，维护者"一次性实现"指令）**：设计已按本文实现，威胁模型 T7 失效条件行/复现要点/测试锚点/§3 图行已同步翻转，边界测试 `test_t7_delegation_boundaries.py` 已按翻转计划重写。已落地：①`SecurityGate.hit_multi`（own + 祖先种子精确扫描）与 `escalate(matched=...)`（ancestor 命中的 payload 增 `signal_kind=ancestor_seed_exact`/`seed_source`/`ancestor_turn_id`，own-turn 负载与 J2 原形 byte 一致）；②`ToolExecutionContext.ancestor_turn_ids` + `AgentLoop(ancestor_turn_ids=...)` 构造参数（两个上下文构造点全部穿透）；③`LedgerExecutor._j2_check` 改用 hit_multi；④**J2 生产激活**：`RuntimeConfig.canary_key_env`（环境变量名，遵守凭据纪律——key 本体只从环境读取、绝不入档/入码）+ `resolve_canary_key`（缺变量 fail-closed：`canary_key_missing`/`canary_key_invalid`）+ `_bind_ledger_policy` 注入。**剩余唯一接线点**：agents 侧委派图 → 子 loop 的 `ancestor_turn_ids` 传参（子 agent 工具执行路径未在本轮核实，机制与上下文字段已就绪，接线是纯调用方工作）。§5 前置问题状态：#0 生产激活已实现（opt-in via canary_key_env）；#2 key 生命周期 = 运算符负责 env 值跨重启稳定（确定性导出要求同值）；#1 takeover×sticky 与 #4 性能界保持未审计/有界声明。测试：`tests/test_j2_semantics_c.py`（10 用例）+ 翻转后的 `tests/test_t7_delegation_boundaries.py`。

## 0. 结论与目标收敛

语义 C 的目标：覆盖 T7-b（父上下文种下的 canary 种子对子 turn 不可见）。设计核实后有一个重要的**诚实缩小**：

- **单跳路径已经被现有 J2 门覆盖**：父向子传递内容的唯一通道是父的动作（`spawn_agent` 任务文本、`send_message` 消息体），这些动作参数在父侧 authorize 时就经过 J2 门扫描（`ledger/executor.py:737-769`）——种子若被模型放进这些参数，父侧已升级。因此语义 C 的真实增量**不是**"单跳传播"；
- **残余盲区**：(i) 多跳中继（A→B→C：A 的种子经 B 中转后在 C 的动作参数出现，B 的 turn token 与 A 不同，C 扫不到 A 的种子）；(ii) 非 action 通道进入子上下文的内容（工具结果在父子间被复述、共享 workspace 文件内容）再流入子动作。

语义 C = 给子 agent 的门加"祖先种子扫描集"，闭合这两类残余。

## 1. 机制修正（对 s5 §3 原构想的自我纠正）

原构想"对任务文本做**种子摘要**的规范化匹配"在密码学上不可行：摘要匹配要求对候选子串逐一哈希（组合爆炸），精确匹配扫描必须持有**明文模式**（J2 本体 `scan_exact_token` 正是如此）。修正后的机制：

> **扫描集 = 运行时可信内存中的确定性可导出明文 token 集合**，token 永不进入事件 payload、模型上下文或任何持久化内容——digest-only 契约（`security/state.py` 模块合同）保持成立，因为父 token 本来就在父 runtime 内存里（`derive_canary_token(key, turn_id)` 确定性 HMAC），子 runtime 持有同一把 `key` + 委派图即可自行导出，无需传递任何秘密。

- 扫描集定义：`seeds(child) = { derive_canary_token(key, t) | t ∈ ancestors_turns(child) } ∪ { derive_canary_token(key, own_turn) }`；
- 祖先 turn 集从 **D11 已持久化的委派图**（`agent.spawned` 事件链）按需重建，**无需新事件类型**；
- 集合大小 = 委派深度 + 1，受 D11 深度预算天然封顶；
- 命中判定：精确字节命中（沿用 J2 canonicalized/resolved 参数纪律，不引入变形/编码/启发式匹配）。

## 2. 升级与上浮语义

| 项 | 设计 |
|---|---|
| 命中处理 | 子动作按 J2 同款五事件原子批升级 ASK（`ApprovalWaiting("security_escalation_pending")`），sticky 归属 execution 域不变 |
| signal_payload | `signal_kind: "ancestor_seed_exact"`（区别于现有 `"session_canary_exact"`）；新增 `ancestor_agent_id` 字段；`canary_id` 沿用现有 `token[:16]` 前缀约定（J2 已如此入档，`gate.py:60`） |
| 向上通知 | root 的 security-state 流追加 advisory 事件（**report-only，不阻断父**）：`advisory_kind: "descendant_seed_hit"`、`descendant_agent_id`、`canary_id`——沿用 J2"信号与 policy effect 分离"纪律 |
| 阻断边界 | 祖先种子命中只停子动作，父/兄弟不受影响（失败隔离沿用 D11） |

## 3. 与既有声明的衔接

- **T7 行翻转计划**（已执行，2026-09-06）：T7 失效条件第一条已改为"祖先 seed 扫描集已实现，变形/编码仍不检测"；`test_t7_delegation_boundaries.py` 的 `test_parent_turn_canary_does_not_match_child_turn_scan` 已翻转拆分为 `test_t7b_own_turn_scan_ignores_parent_seed`（own-turn 边界保持）+ `test_t7b_ancestor_seed_scan_hits_parent_canary`（新行为命中）——测试与威胁模型行同步翻转，未静默。
- **RT/J 评测范围不变**：本设计是 runtime 传播语义，不把树级传播变成 RT/J 评测项（RT/J v1.2 已声明）。
- **S4 的关系**：本设计的扫描集机制（可信内存持有模式、精确匹配、升级事件复用）与 S4 scoped taint 门同构——若两者都立项，建议共用"多模式扫描集"基础设施，避免两套扫描器。

## 4. 覆盖矩阵（实现后）

| 盲区 | 语义 C 前 | 语义 C 后 |
|---|---|---|
| 单跳：种子进 spawn/send 参数 | 已覆盖（父侧 J2 门） | 不变 |
| 多跳中继（A→B→C） | 盲区 | 覆盖（C 的扫描集含 A、B 的种子） |
| 工具结果复述/共享文件内容流入子动作 | 盲区 | 覆盖（子动作参数被多模式扫描） |
| 变形/编码/改写的种子 | 盲区（declared） | 仍盲区（精确匹配纪律，declared） |

## 5. 实现前置问题（按优先级）

1. **生产装配的 gate/key 激活确权（最高优先，本次设计评审中的新发现）**：全 src 搜索证实 `SecurityGate` 无任何生产构造点、`LedgerExecutor.security_gate` 无生产注入（构造默认 `None`）、config 无 key 字段——**J2 门当前仅测试/lane 激活**。这不是本设计引入的缺陷，但它意味着：任何树级传播实现之前，须先确权 J2 本体的生产激活路径（key 来源、注入点、是否入 config、key 生命周期）。建议维护者将结论记入 rtj-progress。
2. **key 生命周期**：确定性导出要求 `key` 跨重启稳定；若 key 为进程启动随机值，重启后祖先种子不可重导出——须改为持久化 key 或事件内安全记录种子集（前者涉密钥管理，后者涉 digest-only 契约边界，需裁决）。
3. **takeover × sticky escalation 交集**（S5 事实表 F7 未审计项）：祖先种子命中产生的 sticky 状态在 orphan takeover 后的归属，须先裁决（否则升级语义在接管场景未定义）。
4. **性能界**：扫描集随深度线性增长；D11 深度预算（默认上限）即上界，单次扫描成本 = 集合大小 × 一次子串扫描。

## 6. 诚实边界

- 评审稿非实现承诺；§5 四项前置问题未解决前不得开工；
- 对 s5 §3 原构想的修正（纯 digest 扫描不可行 → 可信内存导出扫描集）是设计推演结论，未做原型；
- 覆盖矩阵第 4 行（变形种子）永远 declared——本设计不引入任何变形检测，与 J2/§4 诚实边界一致；
- 生产 gate 激活确权（§5.1）的结论可能反过来影响本设计的范围（例如若裁决为"J2 仅 lane 激活是产品定位"，则语义 C 顺延为 lane 级语义）。
