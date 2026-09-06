# S0 框架词汇映射表：行业标准威胁分类 × Koawa 已有控制

PSEC 轨道切片策略文档。规划：`docs/agent-security-platform-track-plan.md` v1.0。日期：2026-09-06。基线：`PSEC_BASE_COMMIT = e213580`。

## 0. 结论

Koawa 在 OWASP Agentic Security Initiative 的十条里，**ASI02/05/08/10 已有工程级控制**（工具面钉定、容器执行、故障隔离、孤儿接管），**ASI01/04/09 部分覆盖**，**ASI03/06/07 是已声明空白**（分别由 S1、B4、S5 承接）。OWASP LLM Top 10 侧，LLM06/LLM10 最强，LLM01 是"动作后果层 covered、注入本身 declared 不防"（RT/J 责任线），LLM07/LLM08 declared 不适用。本表是 S1–S5 与 backlog 的公共底稿。

## 1. 材料清单（钉定）

| 材料 | 版本/日期 | 入口 | 访问 |
|---|---|---|---|
| OWASP Top 10 for LLM Applications | 2025 版 | genai.owasp.org/llm-top-10/ | 2026-09-06 |
| OWASP Top 10 for Agentic Applications（ASI01–ASI10） | 2025-12-09 发布；配套 Threats and Mitigations taxonomy v1.1 | genai.owasp.org/2025/12/09/owasp-top-10-for-agentic-applications-the-benchmark-for-agentic-security-in-the-age-of-autonomous-ai/ | 2026-09-06 |
| OWASP Agentic AI – Threats and Mitigations | v1.1（2026-09-06 时点当前） | genai.owasp.org/resource/agentic-ai-threats-and-mitigations/ | 2026-09-06 |
| NIST AI RMF Generative AI Profile（NIST AI 600-1） | 2024-07-26 | nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf | 2026-09-06 |
| CSA MAESTRO 威胁建模框架（多 Agent） | 2025-02 | cloudsecurityalliance.org | 2026-09-06 |
| CSA Agentic AI Red Teaming Guide（12 类威胁分类） | 2025-08 | cloudsecurityalliance.org | 2026-09-06 |
| CSA Agentic AI Identity and Access Management | 当前版 | cloudsecurityalliance.org/artifacts/agentic-ai-identity-and-access-management-a-new-approach | 2026-09-06 |

## 2. 方法

状态词汇：**covered（动作层）**＝有真实执行强制点+测试证据；**partial**＝有控制但边界内未闭合；**gap(已声明)**＝威胁模型或规划书明确列为空白；**N-A(声明)**＝超出 runtime 责任线（引用 RT/J §1.1）。证据指针格式：文档 § / commit / 测试名。**映射≠认证**：本表只做词汇对齐与差距定位，不构成任何合规声称。

## 3. OWASP Top 10 for LLM Applications（2025）× Koawa

| 条目 | Koawa 控制 | 证据 | 状态 |
|---|---|---|---|
| LLM01 Prompt Injection | 动作后果层：T1/T2 场景回归（J1 5/5）、canary 精确命中→ASK 升级（J2）、MCP 返回标记不可信 | RT/J v1.1；e213580；D25 §2.1 | partial——注入检测本身 declared 不做（启发式不引入），后果遏制 covered |
| LLM02 Sensitive Information Disclosure | secret 注入/日志脱敏/出站边界（审计范围第 6 条）；工具错误正文脱敏 | 审计交接 §2；D20（932a218）；AGENTS.md 脱敏政策 | partial——`host_trusted` 路径 secret 展开未终裁，**S1 审计中** |
| LLM03 Supply Chain | 第三方 MCP 钉定（版本/commit/许可证/制品 hash/image digest）；T6 配置边界（J1 独立 lane，fail-closed）；PyRIT hash lock+SBOM | D25 §1.3/§2；e213580；RT/J §2.3 | covered（工程级，无插件市场场景） |
| LLM04 Data & Model Poisoning | T3 MCP 毒化对抗矩阵；T6 配置投毒边界 | D25 对抗矩阵；RT/J §2.3 | partial——长期 memory 投毒未做，**backlog B4** |
| LLM05 Improper Output Handling | 工具结果标记不可信 + schema 校验（含严格解码）+ action digest 链（参数↔授权一致性） | D25 §1.2；审计 §2 第 3 条 | partial——标记无执行点消费者，**S4** |
| LLM06 Excessive Agency | ALLOW/DENY/ASK + fail-closed；root 预算=20 fail-closed；可修复错误指引；子 Agent 权限只收窄 | D9；budget_action_limits（74d9278）；test_t4_budget_stops_runaway_loop；D11 §1 | covered（动作层） |
| LLM07 System Prompt Leakage | 配置即信任边界 declared（审计边界第 4 条） | RT/J §1.3 | N-A(声明) |
| LLM08 Vector/Embedding Weaknesses | 无向量库/无 RAG 组件 | — | N-A(声明) |
| LLM09 Misinformation | 模型层 declared；runtime 侧诚实报告语义（unknown 分类、insufficient_exposure、零分母 null） | RT/J §2.1 | N-A(声明)+报告诚实性 partial |
| LLM10 Unbounded Consumption | T4 预算闸门；D11 深度/总数/每父并发预算原子 reserve；RT/J 花费逐轮入档 | test_t4_budget_stops_runaway_loop；D11 §3；RT/J v1.1 Q2 | covered（动作层） |

## 4. OWASP ASI Top 10 for Agentic Applications（2025-12）× Koawa

| 条目 | Koawa 控制 | 证据 | 状态 |
|---|---|---|---|
| ASI01 Agent Goal Hijack | 同 LLM01 后果层；漂移状态故障注入测试（drift subjects） | e213580 | partial——目标漂移检测无信号层，gap 部分 declared |
| ASI02 Tool Misuse | 工具面启动钉定（binding digest）；命名空间+schema 校验；每工具策略/账本约束 | D25 §1.2；mcp/tool_binding.py | partial——重连/恢复路径重验未系统验证，**S2** |
| ASI03 Identity & Privilege Abuse | `sandboxed`/`host_trusted` 双路径身份区分；host_trusted 每次启动 ASK；legacy profile fail-closed | D25 §2.1 | gap——server 凭据展开/身份层未终裁，**S1 + backlog B1** |
| ASI04 Agentic Supply Chain | 同 LLM03（MCP server 钉定是本条核心控制） | D25 §1.3 | covered（工程级） |
| ASI05 Unexpected Code Execution | 第三方 stdio server 容器执行（无网/只读挂载/资源限制/非 root）；workspace patch 走策略+apply_patch 协议 | D25 v1.3；D4 | covered（动作层） |
| ASI06 Memory & Context Poisoning | 会话记忆层已有（D23）但无投毒防御 | D23 文档 | gap(已声明)——**backlog B4** |
| ASI07 Insecure Inter-Agent Communication | D11 mailbox：message_id/幂等键/fence（可靠性层 covered）；安全语义（信任等级/信号传播）无 | D11 §1-§3；**威胁模型 T7 条目已登记（2026-09-06）** | gap(已声明)——T7 已入威胁模型，语义 C 设计待评审 |
| ASI08 Cascading Failures | 失败隔离（单 worker 失败不阻塞兄弟）；崩溃窗口/响应丢失幂等/重启粘性故障注入；outcome_unknown 语义 | D11 §1；D7；e213580 | covered（可靠性+安全面） |
| ASI09 Human-Agent Trust Exploitation | 审批流语义（sticky 联合所有权、DENY 拒绝、PENDING 重挂起）；审批 UI 展示 declared 排除 | e213580；审计 §2 | partial——语义层 covered，展示层 N-A(声明) |
| ASI10 Rogue Agents | run fence 拒绝陈旧提交；orphan 发现与接管；旧 run 终态提交被拒 | D11 §1-§3（stale_agent_run_fenced） | covered（动作层） |

## 5. NIST AI 600-1（2024-07）相关类目 × Koawa

只映射与 runtime 安全相关的类目；CBRN、环境、偏见、版权、内容类 declared N-A（模型/产品责任线）。

| 类目 | Koawa 对应 | 状态 |
|---|---|---|
| Information Security（提示注入、供应链） | 同 §3 LLM01/LLM03 | partial / covered |
| Data Privacy | secret 注入/脱敏（审计第 6 条） | partial——**S1** |
| Value Chain and Component Risks | 第三方 MCP 钉定、依赖 hash lock | covered（工程级） |
| Human–AI Configuration（human oversight） | ASK/审批五事件原子批、fail-closed 语义 | covered（动作层） |
| Confabulation / Information Integrity | 模型层 declared；诚实报告语义同 LLM09 | N-A(声明) |

## 6. CSA 材料定位（不逐条映射）

- **MAESTRO**（2025-02，多 Agent 威胁建模）：S5 的参考框架——委派链/跨 Agent 通信威胁按其建模维度整理。
- **Agentic AI Red Teaming Guide**（2025-08，12 类分类+四阶段方法）：RT/J 已按更严格的 oracle 纪律运行；此分类仅用于核对 RT/J 覆盖是否漏类目，不改变 RT/J 冻结流程。
- **Agentic AI IAM**（当前版）：S1/backlog B1 的参考材料（agent 身份与凭据代管）。

## 7. 差距汇总 → PSEC 承接

| 差距 | 框架条目 | 承接 |
|---|---|---|
| MCP server 身份/凭据层未终裁 | LLM02、ASI03 | S1（+B1） |
| 工具面重连/恢复重验未系统验证 | ASI02、ASI04 | S2 |
| 决策在进程内（架构演进题） | —（非框架条目） | S3 |
| 不可信标记无执行点消费者 | LLM05、ASI01/02 | S4 |
| 委派链/跨 Agent 安全语义空白 | ASI03/07/10 交叉 | S5；T7 条目已登记（RT/J v1.2），语义 C 待设计评审 |
| 长期 memory 投毒 | LLM04、ASI06 | B4 |
| 生产级 egress/DLP | LLM02 | B2 |
| 检测工程（事件流上的检测规则） | —（超出 Top 10，属运营面） | B3 |

## 8. 诚实边界

- 本表是词汇对齐与差距定位，**不构成任何合规、认证或审计结论**；
- "covered（动作层）"全部绑定基线 e213580 与对应文档/测试；材料随官网演进，本表只对 §1 钉定版本负责；
- 模型层类目（LLM07/08/09、NIST 内容类）按 RT/J §1.1 责任线 declared N-A，不因本表产生新义务；
- 状态判定基于文档与 commit 证据，未重跑验证；任何"covered"在复核发现反证时应降级并在账本登记。
