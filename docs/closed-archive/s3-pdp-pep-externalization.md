# S3 切片策略文档：策略决策外置（PDP/PEP）设计研究

PSEC 轨道。规划：`docs/agent-security-platform-track-plan.md` v1.0。日期：2026-09-06。基线：`PSEC_BASE_COMMIT = e213580`。**纯设计研究，无代码变更**（遵守审计交接文档 §6"不预先建设通用策略语言"约束）。

## 0. 结论

Koawa 当前的策略决策（Policy Engine + SecurityGate）全部在进程内，但这**不构成现状缺陷**：现有决策语义已经具备 PDP/PEP 分离最关键的两个性质——决策是纯函数（规则输入 → verdict，`DENY > ASK > ALLOW` 收敛、无匹配 default deny），且决策已以 typed event 形式留痕。外置的真实收益只出现在**多 runtime 共享策略并热更新**的场景；外置的真实代价是把"EventStore 不可读写则不进 handler"的 fail-closed 语义复杂化为"决策通道不可达"的分布式问题。定级 **C**，C→S 触发条件见 §4。

## 1. 材料钉定

| 材料 | 版本/日期 | 入口 | 访问 |
|---|---|---|---|
| OPA Decision Logs | 当前文档（示例事件 label `v1.20.2`，页脚 © 2026） | openpolicyagent.org/docs/management-decision-logs | 2026-09-06 |
| Cedar 官方文档 | Version 4.5 | docs.cedarpolicy.com | 2026-09-06 |
| Koawa 基线语义 | day-09 文档 + gate.py/state.py 代码 | `docs/day-09-policy-approval-network.md`、`security/gate.py`、`security/state.py` | 基线 e213580 |

## 2. 语义映射表（现状 → PDP/PEP 职责）

现状权威来源：day-09 §4（ALLOW/DENY/ASK 表）、day-09 §3（执行顺序）、`security/gate.py`（J2 升级）、`security/state.py`（sticky 安全状态）。

| 现状分支 | 现状语义（证据） | PDP 职责 | PEP 职责 | 外置代价 |
|---|---|---|---|---|
| 规则收敛 | 无匹配 default deny；冲突按 DENY>ASK>ALLOW（day-09:12-13,120） | 纯函数：规则集版本 + 输入摘要 → verdict | 接受 verdict 并执行 | 决策通道成为新依赖：PDP 不可达时 PEP 必须 fail-closed（拒而非放），需要本地策略快照 + 版本号做有界降级 |
| DENY | 不进 handler，配对 typed error ToolResult（day-09:83,117,124） | 出 DENY | 拒绝 + 配对错误 | 无 |
| ASK | 持久 approval stream + Turn interrupt，五事件原子批，流版本过期整批回滚（day-09:84,187,235） | 出 ASK | 落五事件原子批、暂停 Turn | **审批资产不外置**：approval stream 是 runtime 事件流资产，PDP 只产 verdict，不持有审批状态 |
| ALLOW 后置门 | 仍需 re-resolve、budget、Turn fence；claim 前再次解析 path/DNS/resource，身份漂移→旧 grant 失效→新 ASK（day-09:20,116,263-270） | 无（感知不到本地文件系统/DNS） | 全部保留在执行点 | 重解析数据在执行侧，**不可外置**——PDP/PEP 分离的天然边界 |
| J2 canary 升级 | ALLOW verdict 上精确 canary 命中 → 五事件原子批升级为 ASK；detector/store 故障退回 base verdict（fail-open）；**已持久化的升级永不可削弱**（gate.py:1-7,42-82；state.py 合同） | 可视为第二 PDP（detector 决策） | 升级落档与 sticky 状态归 PEP | detector fail-open 语义必须留在 PEP：外置 detector 的不可达≠退回 ALLOW，只能退回 base verdict |
| fail-open 边界 | 仅 detector 故障可退回 base verdict；核心门（EventStore 不可读写等）任一失败不进 handler（gate.py docstring；规划书 J2 语义） | — | 决策来源（base/external/detector）必须作为决策事件字段入档 | 决策来源字段是外置后的新增审计义务 |
| 决策留痕 | D1 EventStore：typed event + exact expected stream version，append-only | OPA decision log 的对应物 | — | **无需引入 OPA 自有日志**：Koawa 事件流已是 append-only 决策日志，OPA 的 `labels/decision_id/input/result/erased/masked` 字段模型可作事件 schema 演进参考（含脱敏字段），但存储复用现有账本 |

## 3. 迁移草图（非实现承诺）

```text
管理员策略工件（版本化 + hash 钉定）
        │  版本化发布（改动 = 新版本，非原地修改）
        ▼
┌─────────────────┐    决策请求：输入摘要 + 策略版本    ┌──────────────────────┐
│ PDP（策略引擎） │◄──────────────────────────────────►│ PEP（executor 执行点）│
│ 纯函数：输入→   │    verdict: ALLOW/DENY/ASK          │ claim 前 re-resolve / │
│ ALLOW/DENY/ASK  │    + policy_version                 │ budget / Turn fence / │
└─────────────────┘                                     │ approval 五事件原子批 │
        │                                               │ J2 detector + sticky  │
        │ 决策日志 = typed event（决策来源字段必填）      └──────────────────────┘
        ▼
  D1 EventStore（复用，不引入独立日志系统）
```

信任边界移动：策略文本从进程内配置变为外部版本化工件；决策输入以 digest 入档（不存原始参数，沿用 F2/F5 的摘要纪律）；**审批与后置门不跨边界**。若用 Cedar 类引擎：principal/action/resource 模型可承载现有规则集，但 Koawa 的 ASK（人审中间态）在 Cedar 的 allow/deny 二值模型外，需要 PEP 侧组合（Cedar 出 deny/allow，ASK 由 PEP 规则层映射）——这本身是外置的语义损耗点之一。

## 4. 结论定级与触发条件

**定级：C（条件性需要）。** 现状进程内决策无准入缺口（决策纯函数化 + 事件留痕已具备），外置引入分布式 fail-closed 复杂度而无对应收益。

C→S 触发条件（任一成真则重开本设计为新切片）：

1. 多 runtime 实例（多机/多进程组）需要共享同一策略并保证一致热更新；
2. 策略规则复杂度增长到需要专用表达（资源级授权矩阵、关系规则），进程内规则结构不再可维护；
3. 合规/审计要求提供"决策与执行职责分离"的形式化证明；
4. 出现不可信策略来源（策略本身需要摘要验证与外部裁决），当前"管理员配置即信任"边界被突破。

## 5. 本研究不做什么

不建策略引擎、不写策略 DSL、不引入 OPA/Cedar 依赖、不修改 SecurityGate/Policy 任何行为、不产出迁移代码。本研究只回答"若外置，边界在哪、代价是什么、何时值得"。

## 6. 诚实边界

- 映射基于 day-09 文档与 gate.py/state.py 代码阅读（file:line 见 §2），未对运行时做动态验证；
- OPA/Cedar 部分为机制摘要（钉定见 §1），未在 Koawa 上做任何实验或原型；
- 触发条件表是工程判断，不是承诺；任何条件成真时仍需走版本化切片审批；
- 决策日志字段模型引用 OPA 文档钉定版本，Koawa 事件 schema 演进时须按当时版本复核。
