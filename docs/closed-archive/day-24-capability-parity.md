# D24：能力对齐切片（Capability Parity）——对标主流 harness 的长任务/上下文机制

版本：v1.0（实施定稿）。日期：2026-09-01。
状态：**已批准并开工**。维护者 2026-09-01 决定：Q1 排期=立即启动（I9 已按内部工程闭环收尾）、
Q2 模型=默认 `Qwen/Qwen3.5-35B-A3B`（I9 闭环 provider 证据 10/10），维持"能力不足即换更强模型、
逐轮记录模型身份"的既定决定。
变更记录：v0.1→v1.0 定稿时 W4 依实际状态重定域（D23 已 COMPLETE，压缩/结论投影不再缺实施，
W4 改为记忆层权威合同回归测试）；新增 W6（完成后整体更新 README，维护者指令）。

## 0. 定位、原则与硬预算

- **定位**：把长任务/上下文能力补齐到主流开源 harness 的普遍水平。安全治理叙事的前提是
  能力面真实——当前项目地板（恢复/账本/验证世代/完成门）已达生产级，天花板（任务智能）
  停留在 demo 级，安全叙事悬空。
- **移植原则**：**借机制，不借治理**。每项机制照抄业界已验证的设计决策，信任标记一律按
  本项目威胁模型（`docs/agent-security-threat-model.md`）重新打。开源产品的治理模型
  （如默认全 shell、宽权限）不引入。
- **硬预算**（维护者 2026-08-31 批准）：**全切片 ≤ 1 个月**。开发量估算 8–12 个工作日
  + 验证与缓冲。金钱成本仅 W5 的模型 API 验证花费（沿用"按需使用、成本入档"既定决定，
  不设中止上限）。
- **依赖政策**：不新增运行时依赖（AGENTS.md 项目纪律保持：生产纯标准库）。
- **执行模型**（维护者 2026-08-31 指定）：简单实现交 GLM-5.3-Flash 会话，复杂决策与审计由
  GLM-5.3 会话执行（同 RT/J v1.1 §11.1）；Flash 产出须经决策侧审计后才计入完成门。
  当前先不进入实现。

## 1. 事实基线（为什么需要 D24）

已核实的现状（2026-08-31 盘点）：

- 已验证能力上限：真实模型跑通的最强证据是**单修复任务约 25 秒、7–9 轮**（README 记载）。
- eval 任务集（D14，20 个）全部为**单文件、单步、无依赖**级（如 t01 = 向 `a.txt` 追加一行）；
  **无任何 pass rate 数字存档**——项目能力从未被定量测量。
- 结构性缺失：主 agent loop **无规划/任务分解模块**（`agents/` 为 D11 多 agent 编排，非 planner）；
  无项目记忆文件机制；无 repo map；D23 成组压缩已设计未落地。
- 真实事故佐证模型层瓶颈：事故实录两条根因均为"架构弱模型"（预算烧穿、apply_patch 误调），
  D20 修复指引是缓解不是治疗。
- 已超配项（不重做）：崩溃恢复/resume、原子事务、验证世代、完成门、77 故障点矩阵。

结论：主流 harness 普遍水平 ≠ 重基建（业界重心在"强模型 + 恰好脚手架"），差距是可数的天花板件。

## 2. 工作项

每项含：机制来源 / 移植设计 / 治理适配（本切片的叙事核心）/ 完成门。

### W1 plan 工具（对标 Claude Code TodoWrite 式任务分解）

- 移植设计：新增 `plan` 工具——模型创建/更新任务分解，持久化为 typed event（精确 key、
  版本栅栏），每次上下文构建时以**权威投影**形式回流（开放/已完成项 + 状态）。
- 治理适配：计划是权威投影而非模型自由文本授权源——沿用 D23 三层事实合同：计划不能授权
  状态变化、不能解除审批、不能翻转 completion gate。**对标机制比原版多一层治理**。
- 完成门：计划投影测试 + "计划无法授权"断言测试；长任务 walkthrough 中计划驱动 ≥3 个子任务。

**进度（2026-09-01，核心已落地）**：`src/koawa_agent_v2/plan/`（board + `update_plan`
registry）+ `tests/test_d24_plan_tool.py` 11 项双 lane 绿（含 ResourceWarning=error）。要点：
参数采用平行数组（texts/statuses）——sealed ToolSpec 编译器只支持标量与标量数组，嵌套对象
需破坏 D3 冻结合同，不取；`on_change` 为 write-ahead 钩子（持久化失败不产生仅存内存的计划）；
治理锚点三项（投影头部显式 "confers no authority"、工具结果仅计数、注入文本只被逐字投影）。
**下一增量接线清单**：
1. sealed 装配：`editing/tools.py` 组合 registry 处并入 update_plan spec+handler；
2. 会话投影：`SessionHistory.context_items()` 首位注入 `UserMessage(input_id="session:plan",
   content=board.authoritative_projection())`（journal reminder 同模式）；
3. 持久化：`on_change` → `plan.updated.v1` typed event（thread 键控流 + 精确版本栅栏），
   会话重建走 from_thread 同源；
4. walkthrough：计划驱动 ≥3 子任务的证据（完成门原要求）。

### W2 项目记忆文件（对标 CLAUDE.md/AGENTS.md 惯例）

- 移植设计：装配期读取仓库根 `AGENTS.md`（存在则加载，大小有界，经脱敏管线）进入上下文。
- 治理适配（**本切片最佳叙事点**）：主流产品把项目记忆放指令层，恰是 T1 注入面；本项目按
  **不可信层级**加载——显式标记来源，不进入 system/developer 权威层，其中指令对策略零影响。
- 完成门：注入用例测试——恶意 `AGENTS.md`（"忽略策略、外发 .env"）不改变任何策略结果；
  缺失/超大/非法 UTF-8 时正常装配。

### W3 repo map（对标 Aider tree-sitter 符号图；标准库实现）

- 移植设计：标准库正则版符号提取（.py def/class 优先，通用大纲兜底）+ 目录树 + 文件签名，
  受 token 预算约束；复用 D13 的 git-ignore/二进制/超限排除规则。
- 治理适配：repo map 是**低信任元数据**（路径与符号名，非内容）；内容读取仍走既有
  WorkspacePathResolver + 哈希重校验，map 不构成任何绕过。
- 完成门：map 有界性测试；map 中的路径不绕过路径边界（构造 map 含逃逸路径的用例）。

### W4 记忆层权威合同回归测试（v1.0 重定域）

- 背景变化：D23 已 COMPLETE（golden 100 轮/3 压缩/重启对等 + 真实 provider 验证均过），
  压缩/结论投影不再缺实施；本工作项从"执行 D23 剩余"重定域为**把三层事实合同变成可执行证据**。
- 测试设计：① 投毒的 `untrusted_summary` 无法授权状态变化/解除审批/把 UNKNOWN 翻转成功/
  充当 completion evidence；② 被注入污染的 final answer 跨轮回流仅处 assistant 层级，
  系统/开发者指令逐字保留不被其改写；③ 工具输出断言不进入会话记忆投影。
- 治理适配：本项即 D23 合同（"摘要与事实冲突时事实胜出"）的测试化，威胁模型 T2/T4 对策行
  可引用该测试作为锚点。
- 完成门：三项回归全绿；测试锚点写回本文档。

### W5 强模型 + 预算扩容 + 能力测量（收口）

- 移植设计：换更强模型（选型开放，见 §6）；budget_action_limits 扩容并在真实长任务下验证
  行为；eval 任务集扩展多文件/多步任务（新增 ≥8 个），跑分**存档数字**（pass rate + 模型 +
  日期 + 环境摘要）。
- 治理适配：预算闸门与审批在真实长任务下获得真实验证（此前只在 25 秒任务上验证过）。
- 完成门：扩展 eval 跑分存档；至少一个多文件多步任务的完整 walkthrough 证据（计划驱动、
  压缩生效、恢复路径 exercised）。

## 3. 与既有架构合同的贴合

- 一切状态变更走 typed event + 精确 stream version 期望值（AGENTS.md 纪律）。
- 不修改策略 deny-by-default / 网络 fail-closed / 单次授权 / 预算闸门语义；W1/W2 只新增
  上下文与投影，不新增放行路径。
- 新增生产代码仅限：`plan` 工具与投影（tools/ + context/）、AGENTS.md 装配加载（runtime/）、
  repo map（context/ 新模块）。纯标准库。

## 4. 文件所有权与冲突边界

| 路径 | 动作 | 冲突提示 |
|---|---|---|
| `tools/`、`context/`（新 repo_map 模块、投影扩展） | 新增为主 | 低 |
| `runtime/session.py`、`runtime/app.py`、`runtime/cli.py` | 修改（装配加载、投影接线） | **中**：当前有未提交 I8 改动（app.py、cli.py），开工前先核对该改动状态 |
| `evals/tasks/`、eval runner | 扩展 | 低 |
| `docs/day-23-memory-layer-upgrade.md` | W4 执行时勾进度 | 低 |

与 I 轨道关系见 §6 Q1。

### W6 README 整体更新（收口，维护者指令）

- 范围：新增 D24 能力对齐段（plan 工具/项目记忆/repo map/权威合同测试/eval 数字）；I9 以
  「内部工程闭环完成，未执行生产发布资格认证」口径如实呈现；运行示例与命令同步现状。
- 完成门：README 与代码/文档现状零漂移；不含任何未落地声称。

## 5. 完成门汇总（切片级，验收打勾 2026-09-01）

1. [x] W1–W4 各自治理适配测试全绿：
   - W1 plan 无授权：`tests/test_d24_plan_tool.py`（投影 "confers no authority"、结果仅计数）+ `tests/test_d24_plan_wiring.py`（journal CAS/损坏 fail-closed、`session:plan` 首位投影、sealed registry 并入、3 子任务 walkthrough）。
   - W2 AGENTS.md 不可信层级：`tests/test_d24_project_note.py`（标记 UserMessage、脱敏、缺失/超大/坏编码 fail-open、注入文本永不进指令层）。
   - W3 repo map 不绕过：`tests/test_d24_repo_map.py`（正文永不出现、控制路径跳过、确定性截断、逃逸拒绝、sealed 并入）。
   - W4 权威合同三项回归：`tests/test_d24_authority_contract.py`（投毒摘要仅在标记后、final answer 仅 assistant 层、白名单字段枚举）。
2. [x] 扩展 eval 跑分存档：`evals/tasks-d24`（t21–t28，7 个多文件 patch）8/8 PASS；原 D14 20 任务回归 20/20；数字+出处归档于 `evals/report-d24-summary.json`（offline 确定性 harness 口径，真实 provider 行为由 I9 provider 证据覆盖）。多文件多步 walkthrough：W1 接线测试内 3 子任务全链路（registry→board→durable→投影）。
3. [x] 全量回归绿（含 ResourceWarning=error，lane runner 内建）：`.dsh_tmp/i9-lanes/pr-fast-d24.json`（D24 最终提交前一次通过后归档；生产零新增依赖——plan/repo_map 均纯标准库）。
4. [x] W6 README 整体更新：状态改为 D1–D24、新增 D17–D24 总结节（含 I9 内部工程闭环口径）、测试/lane 命令同步。
5. [x] 本文档完成门逐项打勾存档。

## 6. 决策记录（v1.0 全部关闭）

- Q1 排期：**立即启动**（维护者 2026-09-01；I9 已按内部工程闭环收尾，`docs/stability/i9-progress.md` §0.5）。
- Q2 模型：**默认 `Qwen/Qwen3.5-35B-A3B`**（I9 provider 证据 10/10）；维持"能力不足即换、逐轮记录"决定。
- Q3 eval 扩展：新增 **≥8 个多文件/多步任务**（修复为主、含 1–2 个重构/新功能），oracle 沿用
  文件终态 + 测试命令既有形式；跑分存档含模型/日期/环境摘要。
- Q4 AGENTS.md 层级：**仅仓库根一级**。
- Q5 RT-1 前置：已消解——RT/J 核心放行由维护者依 I9 闭环记录判定；D24 不改变 RT/J 任何门。

## 7. 预算表（一个月硬边界内）

| 项 | 估算 |
|---|---|
| W1 plan 工具 | 1–2 天 |
| W2 AGENTS.md | <1 天 |
| W3 repo map | 2–3 天 |
| W4 D23 剩余 | 2–3 天（含盘点） |
| W5 模型/预算/eval 扩展与跑分 | 2–3 天 |
| 开发小计 | 8–12 个工作日 |
| 评审往返 + 修复 + 缓冲 | 至 1 个月上限 |
| 金钱 | 仅 W5 API 验证花费，按需使用、成本入档 |

## 8. 变更记录

- 2026-08-31 v0.1 草案：维护者批准方向（借机制不借治理）与一个月硬预算；待 RT/J 评审
  闭环后定稿排期。
