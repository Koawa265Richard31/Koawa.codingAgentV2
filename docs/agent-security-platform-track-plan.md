# KoawaAgent V2 平台安全工程轨道规划书（PSEC 轨道）

版本：v1.1（T7 应用修订）。日期：2026-09-06。
状态：维护者指令（2026-09-06 会话）：规划先行，实现由多个 Agent 接管推进；维护者只读规划与各切片策略文档，待实现完成后按切片阅读。v0.1 已经外部评审 Agent 审查（判定 APPROVE-WITH-FIXES，7 条修订全部采纳），按 §3.0 升版 v1.0 生效；v1.1：维护者批准 T7 应用，§3.2 冻结面相应修订。
基线：`PSEC_BASE_COMMIT = e213580`（与 origin/main 同步，2026-09-06 核对；接管会话开工时必须 `git log -1` 复核基线未被移动）。
进度账本：`docs/psec-progress.md`（**唯一状态事实源**，本规划书不含进度）。

变更记录：v0.1 初稿（2026-09-06）。

> **维护者阅读指引**：现在只需读 §0（执行摘要）和 §4 中每个切片的"目标问题 / 产出物 / 完成门"三行；实现完成后按 §7 的路径逐切片读策略文档。其余章节是给接管 Agent 的执行约束。

## 0. 执行摘要

RT/J 轨道已工程收口（J1 5/5、RT-1 4/4、校准门通过、findings=[]，e213580），单 agent runtime 的安全闭环（策略→审批→沙箱→账本→恢复）已有真实实现与独立复现。本轨道回答下一个问题：**平台工程型 Agent 安全岗位的四个真空区**——

1. **MCP 授权与身份层**：D25 治理了第三方 MCP 的进程/文件/网络/资源/生命周期，但 server 的凭据展开、身份与授权面未裁决（审计交接文档遗留 S 候选"secret 与出站边界"）；
2. **策略决策外置（PDP/PEP）**：SecurityGate/Policy 是进程内模块，外置决策是平台岗核心架构题；
3. **信息流控制（scoped taint）**：MCP 返回已标记"不可信内容"，但没有任何执行点消费这个标记——CaMeL/dual-LLM 是架构层答案；
4. **多 Agent 委派安全**：D11 已有控制面与权限收窄，但安全信号（canary/escalation）无树级语义，RT/J §1.2 明确把"canary 向子 Agent 树级传播"列为已声明空白。

拆解为六个切片 S0–S5 与一个 backlog（§4、§5）。产出形态三档：**切片策略文档**（维护者唯一必读面，强制）、研究摘要（材料钉定版本，内嵌策略文档）、条件性代码（只做加法性加固，必须先过 S 判定）。

## 1. 背景与定位（接管会话必读）

- **岗位目标**：方向 B（平台工程型）Agent 安全岗位。本轨道产出同时服务：补齐真空区的真实理解、为 Koawa 产出设计研究文档、形成面试可讲的研究级材料。
- **继承纪律**：审计交接文档（`docs/Koawa_Runtime_Security_Audit_Handoff.md`）的 S/C/N 分类与"安全闭环链路"标准（识别受保护对象→权限决策→执行点强制→可信证据→终止/恢复/回滚）是唯一准入判据。论文提过、成熟产品实现过、测试好写，都不构成准入理由。
- **与 RT/J 的关系**：RT/J 已收口，本轨道不得使其回归（冻结面见 §3.2）。

## 2. 现状事实基线（接管会话免读源码）

以下事实来自仓库文档与 git 历史，可直接引用；需要更细粒度时按各切片步骤所指读对应源码文件。

### 2.1 已有能力（不得重复建设）

| 能力 | 位置/文档 | 要点 |
|---|---|---|
| 事件存储与账本 | `src/koawa_agent_v2/ledger`、D1 文档 | typed event + exact expected stream version，JSON-only 持久化 |
| 策略/审批/网络门禁 | D9 文档、`security/gate.py`、`security/state.py` | ALLOW/DENY/ASK、fail-closed；J2 canary 精确命中→ASK 升级（五事件原子批）；审批粘性联合所有权 |
| 多 Agent 控制面 | D11 文档、`agents/`（control、graph、messages、resources、scheduler） | parent/child 状态机、durable mailbox、深度/总数/每父并发预算原子 reserve、**子权限只收窄（scopes 来自父配置 + read-only allowlist 硬限）**、run fence、orphan takeover |
| 第三方 MCP 沙箱 | D25 文档（v1.3 COMPLETE）、`mcp/`、`sandbox/` | stdio MCP 容器执行：默认无网、只读挂载、资源限制、非 root；`host_trusted` 逃生舱每次启动 ASK；binding digest 钉定工具面；对抗矩阵 + Windows/Linux 双平台证据 |
| 记忆层 | D23 文档 | 会话记忆增强（其安全面只出现在 backlog B4） |
| 红队与校准 | RT/J（`docs/agent-redteam-jailbreak-plan.md` v1.1 + e213580） | 七类结果分类、外部 oracle、controlled sink、500/500 校准门、findings 登记 |

### 2.2 明确没做过的（本轨道空间）

- MCP 的 OAuth/授权/身份层；`host_trusted` 路径的 secret/env 展开与失败脱敏的最终裁决（审计文档遗留 S 候选）；
- 工具面在**重连/崩溃恢复/server 重启**路径上的重验语义系统验证（启动时 binding 钉定已实现，其余路径未系统审计）；
- 策略决策外置（进程内 gate 是唯一形态）；
- taint 的动作边界消费（不可信标记存在，无消费者）；
- 子 Agent 的安全信号传播（RT/J §1.2 明确排除，属已声明空白）；
- 生产级出站控制、检测工程、框架映射（backlog / S0）。

## 3. 治理与多 Agent 接管协议

### 3.0 版本门

v0.1 → v1.0 必须经外部评审 Agent 审查（复用 RT/J 的评审流程：评审可直接修订，修订记入变更记录）；评审 Agent 直接修订升版 v1.0，维护者知情确认后生效。**v1.0 生效前不得启动任何切片**；S0 属纯文档可在 v1.0 生效后第一时间启动。

### 3.1 会话开工门（每个接管会话按序执行）

1. AGENTS.md 对齐检查（`git status --porcelain`、local ahead、remote head）并报告；
2. 读 `docs/psec-progress.md`：确认目标切片为 `pending` 且无其他会话认领记录；
3. 在账本登记认领（会话标识、时间、切片号）后，立即以 `PSEC/CLAIM:S<n>:` 前缀单独 commit，提交后重读账本确认无同切片他人认领，冲突以先提交者为准；认领会话超过 5 天无对应 commit，其他会话可在账本备注后接管。**单切片单会话**，禁止并行改同一切片；
4. `git log -1` 复核基线；若基线已推进，以账本中最新已完成切片的 commit 为个人基线；
5. 切片完成后：全量测试（若有代码变更）→ 账本更新（状态 + 证据）→ commit。

### 3.2 冻结面（零改动，验收时 `git diff` 必须为空）

- `docs/agent-security-threat-model.md` **全文件**（T2/T4 曾按精确行级增补协议修改；本轨道修改它只能走 S5 的提案文件路径。**v1.1 例外**：维护者 2026-09-06 批准 T7 应用——三锚点：§1 框架依据补 ASI 行、§2 纯插入 T7 节、§3 拦截点图加行，其余 byte-identical（before `021fecb9…` / after `3d131d82…`）；除此之外仍为冻结面）；
- J1/J2/RT-1 行为面：`security/gate.py` 的 canary→ASK 升级语义、ApprovalService 五事件原子批、security-state 事件 schema、J1 冻结用例；
- `docs/agent-redteam-jailbreak-plan.md`、`docs/rtj-*`、已归档证据目录；
- 未跟踪的 `scratch_*.txt` / `app_part*.txt` / `.dsh_tmp` 既有内容（并行会话产物，归属纪律不变：不动、不提交、不清理）。

### 3.3 代码变更纪律

1. **加法性优先**：新模块、新测试优先；修改既有行为必须先在切片策略文档通过 S 判定（真实入口 + 现有控制截不住 + 有明确执行强制点），否则只产提案、登记 blocker；
2. 源码一律用 Read+Edit/Write 工具修改（Mimosa 钩子拒绝 Bash 直写，见 rtj-progress 阻塞 #5）；
3. JSON-only 持久化；不引入新依赖（标准库优先，确需依赖先登记 blocker 等批准）；
4. 任何代码切片完成后从 `v2/` 执行 `PYTHONPATH=src python -m unittest discover -s tests -v`，结果 JSON 归档 `.dsh_tmp/psec-lanes/<slice>-<date>.json`，账本记 pass/fail/skip 计数；
5. 每个代码切片单独 commit，信息前缀 `PSEC/S<n>:`。

### 3.4 切片完成门（统一，四件套缺一不可）

1. **切片策略文档** `docs/psec/s<n>-*.md`：目标问题、结论、证据指针（file:line / commit / 测试名）、诚实边界、若适用的提案；正文 ≤ 2 页，证据入附录；
2. 研究摘要内嵌策略文档：材料清单带 URL / 版本 / immutable revision（遵守 RT/J 语料钉定纪律；floating 来源即作废）；
3. 测试证据（纯文档切片写明"无代码变更"）；
4. 账本更新。

冻结面 `git diff` 与测试归档不由完成切片的会话自证：由另一会话或维护者独立复核，结论记入账本。

### 3.5 阻塞与升级

阻塞登记进账本（现象、根因、处理）；**禁止缩小范围自救**（复用 RT/J 规则）。需要动冻结面 → 停该步骤、产提案、登记 blocker 等维护者裁决。

## 4. 切片定义

产出目录：`docs/psec/`。每切片按 §3.4 完成门交付。

### S0 框架词汇映射表（P0，纯文档，最先做）

- **目标问题**：把 Koawa 已有控制映射到行业标准威胁分类，形成面试语言与差距地图；同时是后续切片引用的公共底稿。
- **学什么**：OWASP GenAI Security / Agentic AI Initiative 当前出版物（候选入口：owasp.org 的 GenAI Security 与 Agentic AI Initiative 页面）；NIST AI RMF GenAI Profile（NIST AI 600-1，nist.gov）；CSA Agentic AI 威胁分类（cloudsecurityalliance.org）；（选）MITRE ATLAS。以官方源为准，钉定所读版本（URL + 页面版本标注 + 访问日期；floating 来源即作废）。
- **与 Koawa 映射**：每条框架威胁 → Koawa 已有控制（引用文档/测试/commit）→ 状态（covered / partial / gap）→ S/C/N 预判。
- **执行步骤**：①收集并钉定框架文本版本；②逐条映射；③产出差距清单供 S1–S5 引用。
- **产出物**：`docs/psec/s0-framework-mapping.md`。
- **完成门**：覆盖所选框架的类目全集；每条映射有仓库证据指针；明确声明"映射≠认证"。
- **代码资格**：N（无代码）。

### S1 MCP 授权与身份层研究 + secret 传播审计（P0）

- **目标问题**：D25 治理了第三方 stdio MCP 的进程/文件/网络/资源/生命周期，未裁决凭据与身份层：secret 是否按 server/任务最小化注入、是否可能进入 MCP 环境/日志/错误信息/工具返回、`host_trusted` 逃生舱的信任边界如何表述；未来若支持远程/HTTP MCP，授权模型是什么。
- **学什么**：MCP 规范授权章节（钉定 spec revision；注意授权面主要在 HTTP transport，stdio 本地 server 无 OAuth 面——这决定本切片的 S/C 边界）；RFC 8707（resource indicators / audience binding）；RFC 9700（OAuth 2.0 安全 BCP，选读）。
- **与 Koawa 映射**：`sandboxed` 路径容器默认无秘密（D25 §2.1）；审计面集中在 `host_trusted` 路径的 env/secret 展开、失败信息脱敏、以及"远程 MCP"这一 D25 明确不做的扩展点。
- **执行步骤**：①写授权规范摘要（含 stdio/HTTP 差异表）；②审计 `mcp/launcher.py`、`mcp/connection_manager.py`、`mcp/transport.py`、`runtime/assembly.py` 的 secret/env 传播，产出 file:line 事实表；③按审计交接文档 **§9** 裁决表格式出结论（每项 S/C/N + 最小修复 + 不做什么）；④条件代码仅限**新模块**（如 env 过滤 helper、失败脱敏 helper）加新测试；任何触碰 `host_trusted` launcher 既有语义的修复只产提案并登记 blocker，且须先过 §3.3 第 1 条的 S 判定；证明真实缺口但需改既有行为 → 登记 C 与触发条件。
- **产出物**：`docs/psec/s1-mcp-auth-identity.md`（+ 条件代码与测试）。
- **完成门**：裁决表完整；事实表带 file:line；若有代码，全量回归绿。
- **代码资格**：条件 S（审计先行，代码加法性）。

### S2 工具面固化语义验证（rug-pull 防护，P0，小切片）

- **目标问题**：OpenHands 接受运行中 `tools/list_changed` 刷新/替换工具（产品取舍，审计文档 §7 已点名）；Koawa 启动时以 binding digest 钉定工具面，但重连、崩溃恢复、server 重启、sandbox reconcile 路径是否一律重验 binding、拒绝运行中替换，未系统验证。若存在任何"重启即换工具面"的路径，等价于审批后 rug-pull。
- **学什么**：MCP 工具发现与变更通知语义（钉定 spec revision）；对照 OpenHands SDK 的动态工具刷新实现（只读其源码，作为基线对照）。
- **与 Koawa 映射**：`mcp/tool_binding.py`、`mcp/activation.py`、`mcp/connection_manager.py`、`mcp/sandbox_reconcile.py`。
- **执行步骤**：①建语义矩阵：初次 binding / 重连 / 崩溃恢复 / server 重启 / sandbox reconcile / 变更通知（若存在处理路径），每格记预期 vs 实际 vs 证据；②发现缺口 → 用 `@unittest.expectedFailure`（或 skip 并引用 blocker）的回归测试**固化缺陷现状**，测试注释注明"固化当前缺陷、随修复提案翻转"，全量回归须保持绿；**不得把缺陷行为写成预期通过的断言**；随后提最小修复提案；③把结论整理为 T3（MCP poisoning）countermeasure 的证据材料（引用不改威胁模型）。
- **产出物**：`docs/psec/s2-tool-surface-pinning.md`（+ 条件测试）。
- **完成门**：矩阵全格覆盖且有证据；无"静默替换"路径或已被测试钉住；诚实边界写明验证范围。
- **代码资格**：条件 S（测试优先）。

### S3 策略决策外置（PDP/PEP）设计研究（P1，纯文档）

- **目标问题**：SecurityGate/Policy 决策在进程内。若策略决策外置为独立决策点（PDP）+ 执行点（PEP），fail-closed 语义如何保持、决策日志如何映射到既有事件流、信任边界移到哪里。
- **学什么**：OPA 架构文档（decision log、sidecar 模式，openpolicyagent.org）；Cedar 官方文档与白皮书（cedar-policy.github.io，钉定版本）；（选）Zanzibar 作为关系授权背景。基线文档：`docs/day-09-policy-approval-network.md`（现有策略/审批/网络语义的权威描述）。
- **与 Koawa 映射**：现有语义逐分支映射——ALLOW/DENY/ASK、fail-open 仅限 detector 故障、fail-closed 其余、审批粘性、预算 CAS。**约束**：审计文档明确"不预先建设通用策略语言"，本研究只回答"若外置，边界在哪、代价是什么"，不建引擎、不写策略 DSL。
- **执行步骤**：①摘要 OPA/Cedar 的决策模型与日志语义；②语义映射表（每个 gate 分支 → PDP/PEP 职责划分 → 事件流表示）；③迁移草图（数据流图，非实现承诺）；④结论定级（预期 C）与 C→S 触发条件（如出现多 runtime 共享策略、策略热更新需求）。
- **产出物**：`docs/psec/s3-pdp-pep-externalization.md`。
- **完成门**：语义映射表覆盖全部分支（含 fail-open/fail-closed 各路径）；草图完整；结论明确。
- **代码资格**：N（无代码）。

### S4 scoped taint / 信息流控制设计研究（P1，纯文档）

- **目标问题**：MCP 返回内容已标记"不可信"，但没有执行点消费该标记。CaMeL/dual-LLM 给出架构层答案：让不可信数据带 taint 流动、在动作边界做可证明的控制流限制。本研究定义 Koawa 的**最小 scoped 版本**：不可信来源文本不得成为网络动作的 URL/路径等关键参数——并明确仍不建通用 taint 框架（尊重审计文档"暂缓 provenance/taint"条款）。
- **学什么**：CaMeL（arXiv:2503.18813，钉定版本）；Spotlighting（arXiv:2403.14720）；dual-LLM 模式（Simon Willison）；（选）Invariant Labs 的 agent 设计模式综述。
- **与 Koawa 映射**：审计交接文档 §6"暂缓：provenance、taint 与 memory quarantine"条款的触发条件逐条对照（长期 memory 写入、富文本加载、跨信任域组合、实际出站 sink——RT/J 的 controlled sink 目前仅存在于测试面）；设计只覆盖动作边界消费点。
- **执行步骤**：①摘要 CaMeL 能力模型（capability / taint / policy 三层）与 dual-LLM 分工；②对照暂缓条款定触发条件表；③最小设计：若未来开放网络工具，taint 门挂在哪个执行点、事件如何表示、与现有 policy 的先后关系；④结论定级（预期 C）。
- **产出物**：`docs/psec/s4-scoped-taint.md`。
- **完成门**：逐条对照暂缓条款；设计严格限于动作边界；触发条件表完整。
- **代码资格**：N（无代码）。

### S5 多 Agent 委派安全预研 + T7 提案 + J2 树级传播设计提案（P1，依赖 S1 摘要）

- **目标问题**：D11 已有控制面（权限收窄、预算、fence、orphan takeover），但：①安全信号无树级语义——子 agent transcript 中的 canary 命中不会升级，父审批对子动作的覆盖关系未定义；②委派链威胁未建模（confused deputy、经 mailbox 的传递性注入、orphan takeover 后新 run 的授权继承）；③威胁模型无 T7。
- **学什么**：A2A 协议认证与委派模型（a2aproject/A2A，钉定 revision）；复用 S1 授权摘要；（选）AutoGen/OpenHands SDK 的 sub-agent 权限实现对照。
- **与 Koawa 映射**：`agents/control.py`、`agents/graph.py`、`agents/messages.py`、`agents/resources.py`、`security/gate.py`（只读）。
- **执行步骤**：①D11 现状事实表（file:line，重点：子 agent 实际可用工具面 vs 声明、mailbox 消息的信任等级、takeover 后授权继承现状）；②T7 草案：委派链威胁清单 + 每条的真实入口/现有控制/缺口；③J2 树级传播设计提案（canary/escalation 在子 agent 的三种语义：继承 / 独立 / 上浮，给出推荐与理由）；④产 T7 增补提案文件（含 before/after 文本与 hash 占位，**不直接修改威胁模型**）。
- **产出物**：`docs/psec/s5-delegation-security.md` + `docs/psec/t7-amendment-proposal.md`。
- **完成门**：威胁模型原文件零改动（`git diff` 证明）；事实表带 file:line；三种传播语义有明确推荐；提案文件须含：当前 threat model 文件 sha256、精确插入锚点（节/表/行）、完整 after 文本、参照 RT/J 规划 §5 的 diff allowlist 自检清单。RT/J §1.3 冻结"不新增 T7"、J2 子 Agent 传播按冻结决策留待版本化设计——因此 T7 与 J2 树级传播**均为提案**，登记 blocker，经维护者按 RT/J 版本化审批后方可实施。
- **代码资格**：N（提案与设计，无代码；实现需维护者批准后另行排期）。

### Backlog（登记不做，防遗忘）

- **B1** 身份与凭据代管深化：短生命周期 scoped token、RFC 8693 token exchange、"agent 用自己身份还是用户身份"的委托授权（依赖 S1 结论）。
- **B2** 生产级出站控制（egress/DLP）：URL/域名 allowlist、跨通道外泄（文件内容→网络工具）阻断点（依赖 S4 触发条件成熟）。
- **B3** 检测工程：OTel GenAI 语义约定、事件流上的检测规则、工具调用序列异常检测（D1 事件存储是现成基座）。
- **B4** 记忆投毒与跨会话持久化攻击：D23 记忆层的写入信任等级、隔离与投毒防御。

## 5. 顺序与并行

```text
v1.0 定稿
  └─ S0（词汇底稿）
       └─ S1（MCP 授权摘要 + secret 审计）
            ├─ S2（工具面固化验证）   ┐
            ├─ S3（PDP/PEP 研究）     ├─ 三者可由三个会话并行
            └─ S4（scoped taint 研究）┘
                 └─ S5（委派安全；依赖 S1 摘要 + S2 事实表）
```

并行纪律：单切片单会话、认领登记先行（§3.1）；S2/S3/S4 互不依赖，可安全并行。

## 6. 轨道级完成定义与诚实边界

**完成定义**：S0–S5 策略文档全部在 `docs/psec/`；账本无未解 blocker；若有代码切片，全量回归绿且逐切片 commit；`git diff` 证明冻结面零改动。

**诚实边界（不得越线声称）**：

- 不声称实现 MCP OAuth/远程 MCP 授权（本研究 + 审计；实现需另立项）；
- 不声称建成策略引擎或通用 taint 框架（S3/S4 明确为设计研究，预期 C 级）；
- 不声称子 Agent 安全信号传播已实现（S5 是提案；实现需维护者批准）；
- 框架映射（S0）≠ 任何合规认证；
- 全部结论绑定基线 commit 与所读材料版本。

## 7. 维护者阅读路径

实现完成后：本规划 §0 → `docs/psec/s0-framework-mapping.md` → `s1` → `s2` → `s3` → `s4` → `s5`，每篇 ≤ 2 页正文 + 证据附录；提案类内容（T7、条件代码清单）单独成节，批准与否逐项勾选即可。

## 8. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-09-06 | 初稿：四真空区 → S0–S5 + backlog；治理协议复用 RT/J 机制（基线冻结、开工门、认领、诚实边界）；待外部评审 |
| v1.0 | 2026-09-06 | 外部评审 APPROVE-WITH-FIXES，7 条修订全部采纳：S2 测试先行改 expectedFailure 缺陷固化（P0）；S1 裁决表引用改 §9、条件代码限新模块、host_trusted 语义修复只产提案；S5 提案文件格式（sha256/锚点/after 文本/diff allowlist 自检）+ 双提案登记 blocker 走版本化审批；认领 commit 协议（`PSEC/CLAIM:S<n>:`）+ 5 天 stale 接管；完成门加独立复核；S0/S3/S4 补精确入口；版本门生效方式明确。评审 Agent 直接修订升版，维护者知情生效 |
| v1.1 | 2026-09-06 | 维护者批准 T7 应用并要求旧文档/旧测试/旧治理同步：威胁模型三锚点应用（§1 框架 ASI 行——超出原提案单锚点，理由：T7 的 OWASP 行引用 ASI 需 §1 依据；§2 T7 节按 T1–T6 表格模板重排——原提案段落体与模板矛盾，已修正；§3 拦截点图加行）；RT/J 规划升 v1.2 解除"不新增 T7"；新增边界钉定测试 tests/test_t7_delegation_boundaries.py（3 用例，钉 declared 失效条件而非新对策）；S0/S5/提案/账本同步 |
