# S5 切片策略文档：多 Agent 委派安全预研 + J2 树级传播设计提案

PSEC 轨道。规划：`docs/agent-security-platform-track-plan.md` v1.0。日期：2026-09-06。基线：`PSEC_BASE_COMMIT = e213580`。**纯设计与提案，无代码变更**；T7 增补为独立提案文件（`t7-amendment-proposal.md`），威胁模型原文件零改动。

> **应用更新（2026-09-06，维护者批准）**：T7 已按提案应用（三锚点，格式按 T1–T6 表格模板重排；before `021fecb9…` / after `3d131d82…`），RT/J 规划 v1.2 解除"不新增 T7"冻结，边界钉定测试 `tests/test_t7_delegation_boundaries.py` 已交付。本文正文保留为设计依据；§4 触发条件 1 部分成立（T7 落档），语义 C 仍待设计评审后方可实现。

## 0. 结论

D11 控制面已给子 agent 提供动作级安全基座：子动作经**同一个共享 executor**（policy → 审批 → J2 门 → budget → fence），scopes 在事件契约里收窄。真正的空白有四个，全部已定位到机制级：

1. **canary 令牌 turn 域不传播**（父上下文种下的 canary 种子对子 turn 结构性不可见——RT/J §1.2 声明 non-scope 的机制本质）；
2. **mailbox 消息无信任等级标注**（含不可信内容的父消息对子 agent 呈"可信指令"外观）；
3. **orphan takeover 与 sticky escalation 的交集未定义**；
4. **威胁模型无 T7**（RT/J §1.3 冻结，须版本化审批增补）。

J2 树级传播的三种语义中，推荐以**语义 C（上浮 + digest 种子集）**作为版本化设计的预研方向，理由与前提见 §4。

## 1. 事实表（file:line 实证，基线 e213580）

| # | 事实 | 证据 |
|---|---|---|
| F1 | 子 agent scopes 是事件契约字段：非空小写 id、排序去重、逐 spawn 入档；D11 文档明文"子权限只收窄（scopes 来自父配置，read-only allowlist 硬限制）" | `agents/control.py:98-105,297-389`；D11 §1 |
| F2 | 深度/总数/每父并发预算以 `budget.reserved/released` 事件原子 reserve/release，超限稳定错误码 | `agents/resources.py:28-102`；D11 §3 |
| F3 | run fence：所有 terminal/message 提交要求 `record.run_id == 提交者 run_id`，否则 `stale_agent_run_fenced`；takeover 是原子事件（`taken-over.v2` + 逐消息 unresolved），新 attempt = 旧 attempt+1 | `agents/control.py:594-618,672-683`；D11 §2-§3 |
| F4 | durable mailbox：MessageKind/Status/Record、幂等键、canonical result digest；**payload 无信任等级字段** | `agents/messages.py:36-84,421-508`（全文件 grep 无 trust/untrusted 标注） |
| F5 | J2 门注入共享 executor：`security_gate` 参数注入 `LedgerExecutor`；ALLOW verdict 上精确 canary 命中 → 五事件原子批升级 ASK；PENDING re-pause（`ApprovalWaiting("security_escalation_pending")`）、DENIED 拒绝（`security_escalation_denied`）——**子 agent 动作与父动作过同一 authorize 链** | `ledger/executor.py:91,100,737-769`；`security/gate.py:1-7` |
| F6 | canary 令牌绑定单 turn：`derive_canary_token(key, turn_id)`，HMAC-SHA256；token 值永不入档（digest-only 契约） | `security/gate.py:30-35`；`security/state.py:55` 及模块合同 |
| F7 | takeover 路径不触碰 approval/security 流（`_takeover` 区间零 approval/grant/escalation 引用）；execution-scoped sticky 状态跨 takeover 的行为**未审计** | `agents/control.py:672-720` grep 实证（2026-09-06） |

## 2. 委派链威胁草案（T7 预研内容）

| 威胁 | 机制 | 现有控制 | 残余 |
|---|---|---|---|
| T7-a 委派链 confused deputy | 父把 T1/T2/T3 入口的不可信内容转述进子任务文本；子工具面（D12 起逐步开放写/命令/MCP）可能执行父上下文未见其后果的动作 | 子动作过全链门（F5）；scopes 收窄（F1） | 转述本身无信号；子的升级只停子的 turn |
| T7-b 信号盲区 | canary 种子绑定父 turn（F6），流入子上下文后对子的 turn token 不匹配 | 子有自己的 canary（覆盖子上下文内新种子的 canary） | **父种子跨代不可见**——结构性，declared |
| T7-c mailbox 注入 | 父消息体无信任标注（F4），子把含不可信工具结果的父消息当可信指令消费 | mailbox 幂等/fence 只保证投递语义 | 无内容信任语义 |
| T7-d 接管语义 | takeover 新 attempt 与 sticky escalation 的交集未定义（F7） | 旧 run 提交被 fence 拒绝（F3） | escalation 归属（execution 域）与 attempt 归属（agent 域）是否对齐未验证 |
| T7-e 收窄逃逸 | scopes 契约正确性依赖装配层；无独立验证点复查"子实际工具面 ⊆ 父声明" | D11 测试套件 | 运行时无自证机制 |

## 3. J2 树级传播：三种语义的设计评审

| 语义 | 机制 | 优点 | 缺点 |
|---|---|---|---|
| A 继承 | 子 turn 复用父 turn 的 canary token（或父种子集明文下发） | 语义最简单，覆盖 T7-b | **违反 digest-only 契约**：token 值进入子上下文 = 安全秘密泄漏给可能被注入的子 agent（F6 契约"never the token value"被破坏） |
| B 独立（现状） | 子有自己的 turn token，互不感知 | 无泄漏；实现为零 | T7-b 空白保持（RT/J declared non-scope） |
| C 上浮 + digest 种子集 | 父在 spawn 时以 **HMAC 摘要**（非明文 token）声明 canary 种子集随任务下发；子端检测除自身 turn token 外，对任务文本/mirror 参数做种子摘要的规范化匹配；命中 → 子动作升级 ASK，同时向上游 root 发 advisory 信号（不阻断父） | 覆盖 T7-b 且 token 值不离开产生域；advisory 上浮复用 J2"信号与 policy effect 分离"纪律 | 需要：种子集的持久化位置与版本化、摘要匹配的密码学验证规范、升级事件的所有权字段（agent 域 × execution 域）——三项均为实现前置设计题 |

**推荐**：以 C 为版本化设计评审的输入方向；A 因违反 digest-only 契约不建议；B 是合规现状，在 T7 增补获批前保持。实现前提：本 §3 先作为设计评审材料获批，再按版本化切片落地；审批语义须先回答 T7-d（sticky 状态 × takeover）。

## 4. C→S 触发条件

1. RT/J 维护者撤销"树级传播 non-scope"决策，或 T7 增补按提案获批（触发 T7 落档 + §3 语义 C 切片立项）；
2. 子 agent 工具面扩展到写/命令/MCP/网络（D12+ 全开）且产品场景出现真实多 Agent 委派链——届时 T7-a/c 的残余从理论面变为现实面；
3. 出现跨父-子的共享资源写（同 workspace 多子并发 patch）——fence 语义需扩展到资源级。

## 5. 本研究不做什么

不实现任何传播语义（§3 是评审输入，非代码）；不修改 `agents/`、`security/`、`ledger/` 任何行为；不直接修改威胁模型（提案文件路径见 `t7-amendment-proposal.md`）；不新增 T7 到运行时任何判定（T7 是威胁建模条目，非 detector）。

## 6. 诚实边界

- F7 的"未审计"是如实声明：takeover × sticky escalation 交集需专项验证（读 recovery 路径 + 事件重建），本切片未做；
- 三语义评审是设计推演，未做原型或测试；语义 C 的三项实现前置问题不解决不应开工；
- 委派威胁草案覆盖 D11 现状工具面（read-only 起步）；D12+ 工具面逐日开放后本表须按实际工具面复核；
- 材料钉定：本切片未引用外部材料（A2A/MCP 委派模型留待 §3 语义 C 立项时钉定，避免浮动引用）。
