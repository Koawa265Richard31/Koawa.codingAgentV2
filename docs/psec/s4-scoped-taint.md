# S4 切片策略文档：scoped taint / 信息流控制设计研究

PSEC 轨道。规划：`docs/agent-security-platform-track-plan.md` v1.0。日期：2026-09-06。基线：`PSEC_BASE_COMMIT = e213580`。**纯设计研究，无代码变更**（尊重审计交接文档 §6"暂缓：provenance、taint 与 memory quarantine"条款）。

## 0. 结论

Koawa 已经生产"不可信"标记（MCP 结果出站前包 `untrusted_mcp_result: True` 信封，`connection_manager.py:673`），但全仓库**没有任何执行点消费该标记**——标记目前只是给模型上下文和审计读者的注释，不是控制。CaMeL 一类能力系统给出的架构层答案是：把"来源信任"做成随值传播的能力，在工具调用边界强制策略。对 Koawa 的对应物是一个**严格限缩的动作边界 taint 门**（不可信来源文本不得成为网络动作的关键参数），挂在 executor authorize 点、复用 J2 已验证的"信号→升级为 ASK"五事件模式。按审计文档暂缓条款衡量，触发条件**尚未成熟**（实际出站 sink 未开放），定级 **C**；本文是该 C 项的预设计，触发条件成熟时按此开切片。

## 1. 材料钉定

| 材料 | 版本 | 入口 | 访问 | 引用层级 |
|---|---|---|---|---|
| CaMeL: *Defeating Prompt Injections by Design* | arXiv:2503.18813 **v2**（v1 2025-03-24，v2 2025-06-24），Google Research 团队 | arxiv.org/abs/2503.18813 | 2026-09-06 | 摘要级钉定；双 LLM 分工细节为正文级引用 |
| Spotlighting: *Defending Against Indirect Prompt Injection Attacks With Spotlighting* | arXiv:2403.14720（2024-03-20），Microsoft | arxiv.org/abs/2403.14720 | 2026-09-06 | 摘要级钉定；三种技术命名为正文级引用 |
| dual-LLM 模式（Simon Willison，2023） | 站内文章系列 | simonwillison.net（标签入口，未做内容快照） | 2026-09-06 | 仅概念归属，不承担数据声称 |

## 2. 机制摘要

**CaMeL**（摘要钉定）：在可攻击的 LLM 外加一层保护性系统层；从**可信**查询显式抽取控制流与数据流，使不可信检索数据无法改变程序流向；用能力（capability）概念阻止私密数据经未授权数据流外泄，策略在**工具调用时**强制；在 AgentDojo 上 77% 任务可解且具**可证明安全性**（未防护系统 84%）。正文级细节：系统由两个 LLM 组成——privileged LLM 只看可信输入、负责产生程序控制流；quarantined LLM 读不可信内容、只能**提议**值，永不控制控制流；值上随附来源信任能力（数据能力/控制能力两类），工具调用边界按来源/目标/来源链检查策略。

**Spotlighting**（摘要钉定）：一组提示工程变换，给不可信数据"可靠且连续的来源信号"，让模型能区分数据与指令；GPT 系上攻击成功率从 >50% 降到 <2%，任务性能影响小。正文命名三种技术：delimiting（定界）、datamarking（数据标记）、encoding（编码）。定位澄清：spotlighting 是**提示级**缓解（降低模型被骗概率），不提供运行时可证明的强制点——与 Koawa"提示词不是安全边界"的责任线（RT/J §0）正交，只能作为补充而非替代。

**dual-LLM**（概念归属）：有工具权的模型与读不可信内容的模型分离，后者只能经受控通道影响前者。这是 CaMeL 双 LLM 结构的思想前身；在 Koawa 单模型装配下的对应物是"不可信内容只能经结构化通道（工具参数）进入动作"，而非自由文本影响。

## 3. Koawa 映射：标记的产生与消费现状

| 事实 | 证据 |
|---|---|
| 标记产生：MCP 工具结果出站前包信封 `{"untrusted_mcp_result": true, "server_id", "tool", "result"(脱敏后)}` | `mcp/connection_manager.py:671-685`；测试 `test_d10_connection_binding.py::test_tool_handler_wraps_untrusted_output` |
| 标记消费：**零**。全仓库 `untrusted_mcp_result` 仅此一处（产生点），policy/executor/detector 均不读取 | 2026-09-06 基线 grep 实证 |
| 不可信内容进入上下文的现有通道 | MCP 结果（有信封）、仓库文本 T1、工具结果注入 T2（RT/J §2.3 冻结 profile） |
| 动作边界的现有强制点 | policy resolve → J2 canary 门（gate.py，first/final resolve 后咨询）→ 审批五事件原子批 → budget/Turn fence → claim 前 re-resolve（day-09:83-90） |

对照结论：Koawa 有"来源不可信"的**标注**、有**动作边界强制点**、有"信号→升级 ASK"的**升级通道**（J2 五事件模式），三者齐备但未接线——缺的正是 CaMeL 意义上的那一环：让标注成为边界检查的输入。

## 4. 与审计暂缓条款的逐条对照（§6 原文四触发条件）

| 暂缓条款触发条件 | Koawa 2026-09-06 现状 | 成熟度 |
|---|---|---|
| 自动长期 memory 写入 | D23 记忆层存在（会话记忆增强）；是否构成"自动长期写入通道"未在本切片审计 | 部分——**B4 联动** |
| 富文本自动加载 | 无（CLI runtime，纯文本上下文） | 未成熟 |
| 跨信任域数据组合 | 已存在单一形态：MCP 不可信结果与仓库文本同上下文（T1/T2 已被 RT/J 治理其动作后果） | 部分成熟 |
| 实际出站 sink | 网络工具默认关闭（network_enabled=False）；D9 网络门禁机制存在（https+全局 IP，反 SSRF）；RT/J controlled sink 仅测试面 | **未成熟——本切片主触发条件不成立** |

## 5. 最小 scoped 设计（提案，非实现承诺）

**范围声明**：只覆盖动作边界消费，不建全链路 taint、不改造消息类型、不引入双 LLM 装配（审计暂缓条款原文："不得为论文覆盖率改造所有消息类型"）。

1. **挂点**：executor authorize 点，J2 gate 同一位置（first/final resolve 之后、handler 之前）——不新增强制点，复用既有执行顺序。
2. **传播规则（最小化）**：不追踪全链路 taint；只做**来源直标**——工具结果进入上下文时已带结构化来源（MCP 结果有 server_id 信封），动作参数校验时按"该参数值与不可信来源内容的最小包含关系"检查（复用 J2 `scan_exact_token` 的规范化/精确匹配范式，来源指纹 = 不可信内容的规范化摘要集合）。
3. **检查语义**：网络类动作（未来开放后）的 URL/主机/路径关键参数，若与不可信来源指纹精确匹配 → 升级 ASK；失败方向与 J2 一致：检测器故障退回 base verdict（fail-open 仅限 detector），核心门失败不进 handler；升级走 J2 同款五事件原子批（signal → escalated → approval.requested → turn.waiting → run.interrupted），sticky 语义复用。
4. **事件表示**：复用 security-state 流（`signal_kind: "untrusted_source_match"`），payload 摘要纪律不变（F2/F5：只存 digest，不存原文）。
5. **明确排除**：非网络动作暂不适用（文件写已有 workspace 边界 + digest 链）；不做跨动作的数据流追踪；不做概率/模糊匹配（沿 J2"只承认精确命中"纪律，不引入启发式）。

## 6. 触发条件表（C→S）

| 条件（对应暂缓条款措辞） | 升级动作 |
|---|---|
| 实际出站 sink 上线（网络工具对策略开放） | 本设计按 §5 开实现切片：taint 门 + 正控/负控测试（复用 RT/J oracle 方法论） |
| 自动长期 memory 写入上线 | 与 B4 合并设计：memory 写入通道视为不可信来源 + 持久化注入面 |
| 富文本自动加载上线 | 重评来源直标规则的覆盖面（富文本解析器成为新注入面） |
| 出现跨信任域数据组合驱动的新 sink 类型 | 按新 sink 类型扩展 §5 第 3 条的关键参数清单 |

## 7. 诚实边界

- 本研究**不声称已实现任何防御**；`untrusted_mcp_result` 标记今天对执行行为无影响（§3 grep 实证）；
- 不建通用 taint 框架、不做全链路数据流追踪、不改造消息类型（暂缓条款约束仍在）；
- §5 是预设计，其正确性未经实现或测试验证；触发条件成熟时须按版本化切片流程重审；
- CaMeL/Spotlighting 的数字声称（77%/84%、50%→<2%）为钉定摘要原文转述，不构成本仓库的任何效果声称；
- 正文级引用（双 LLM 分工、三种技术命名）未做全文快照钉定，只作机制理解用途。
