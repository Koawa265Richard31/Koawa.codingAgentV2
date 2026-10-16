# Agent 安全围栏仓库调研报告（2026-10-12/13 实测）

调研对象：职业会话候选池五仓。核验方法：GitHub API 基础面 + merged PR 管道抽样（squash 陷阱已识别并绕过——`merged:false` 字段不可信，须用 `search/issues+is:merged` 或 git merge-base 交叉验证）+ 领域入口（attack/guardrail issue）探测。

## 排序结论（按"想法激进+实验激进"复合分 + 管道通畅度）

| 排名 | 仓库 | 规模/活跃 | 合并管道 | 激进实验接纳先例 | 领域入口 |
|---|---|---|---|---|---|
| **1** | **IBM mcp-context-forge（ContextForge）** | 4605★，今日推送 | **9月以来 151 个 PR 合并**（单人连发 followup 也收） | 大量外部 feat 合并 | **#7101 [FEATURE] IsMalicious content/URL guardrail plugin ——开放的功能槽，直接对口的入口**；security 标签 open 258 个 |
| **2** | **NVIDIA NeMo-Guardrails**（现 NVIDIA-NeMo/Guardrails） | 7281★，今日推送 | 10月 12 个合并（外部 Pouyanpi 5连、tanushriya910 2个） | **#2028 multi-turn jailbreak detection rail**（3人抢认领=领域竞争激烈但维护者未裁决）；仓内已有 jailbreak_detection rail 可扩展；attack/red-team open issue 23 个 | 自定义 rail 是官方扩展点 |
| 3 | **Meta LlamaFirewall**（在 meta-llama/PurpleLlama 内 LlamaFirewall/ 目录） | 4427★，8月还有目录提交 | **全历史仅 4 个 merged PR（2个是机器人、2个琐碎）**——外部 PR 管道几乎不存在 | 无先例 | 目录活跃但合并管道冰封 |
| 4 | Lasso mcp-gateway | 395★ | 最后推送 2026-01-22（**9个月沉寂**） | —— | 沉寂仓不投 |
| 5 | Trail of Bits mcp-context-protector | 226★ | 最后推送 2026-04-14（**6个月沉寂**） | —— | 沉寂仓不投 |

## 各仓深挖细节

### 1. ContextForge（IBM）——管道之王
- 9月以来 151 merged，其中 ja8zyjits 一人 4 连 followup 全收——**对高频小步贡献者极其友好**
- security 标签 open 258 个：#7101 IsMalicious guardrail plugin（直接的功能槽入口）、#6408 Security Policy Engine and Generalization（通用策略引擎讨论）
- 架构 = gateway + 插件槽——你的 runtime-native defense 经验（claim-gate/证据层规则）映射路径最短
- 风险：名"ContextForge"实为 mcp-context-forge 改名系（IBM 收购），确认它是你研究的"那个"项目

### 2. NeMo-Guardrails（NVIDIA）——最对口你的红队线
- 10月 12 merged 含外部（Pouyanpi 5连大系列、tanushriya910 测试覆盖）
- **#2028 multi-turn jailbreak detection rail**：与你的红队线（Crescendo 类多轮越狱）精确对口，但已有 3 人表态认领（kenlacroix/immut4989 提议自己写 spike/lavkeshdwivedi）——拥挤
- attack/red-team open issue 23 个可选
- 声明式 rail 配置框架——贡献形状 = 自定义 rail，与中间件代码不同

### 3. LlamaFirewall（PurpleLlama 目录）
- 目录 8 月还有提交（活跃）但**全历史外部 merged 仅 2 个且均琐碎**（README 改动、chmod）——外部 PR 合并管道近乎不存在
- Python 库形态（AlignmentCheck 与你 claim-gate 同物种），**适合"用"和"对标"（博客素材），不适合当贡献主阵地**

### 4/5. 沉寂双仓
- Lasso mcp-gateway（395★，9 个月无推送）、Trail of Bits mcp-context-protector（226★，6 个月无推送）——沉寂仓不投，仅作为对照

## 与你主线的组合建议

- **workspace 深耕（AgentScope）不变**——已锁定的 PR 队列继续
- **围栏第二落点推荐顺序：ContextForge > NeMo**——前者管道最快+有开放功能槽（#7101），后者与红队线（jailbreak detection）协同最强
- **LlamaFirewall 转"对标+博客"用途**：AlignmentCheck 与 claim-gate 的物种对照是现成博客题目（"我的 claim-gate 和 Meta 的 AlignmentCheck"），不需要给 PurpleLlama 提 PR
- **探测数据修正**：正确位置——NeMo= `NVIDIA-NeMo/Guardrails`（重定向）、LlamaFirewall= `meta-llama/PurpleLlama` 子目录、ContextForge= `IBM/mcp-context-forge`

## NeMo 本地初扫（2026-10-16，clone 到 D:\A_Dev_Projects\Guardrails）

- 基本信息：NVIDIA-NeMo/Guardrails，默认分支 **develop**（不是 main！），HEAD af242f0b4（clone 时前一天），v0.24.1 已发布；包名 `nemoguardrails`，~75k 行（colang DSL 12.3k / library 14.6k / guardrails 核心 8.9k / server 8.8k / llm 5.5k）
- jailbreak rail 本体：`nemoguardrails/library/jailbreak_detection/` 共 **756 行**（flows.co + heuristics/ + model_based/ + rail.py）——#2028 要扩展的就是它
- 近 30 commit 作者：Pouyan 13 / tanushriya910 5 / Tim Gasser 4（与远程侦察一致，NVIDIA 内部主导）
- **对 AI 贡献者的成文规则（AGENTS.md + AI_POLICY.md + CONTRIBUTING.md，三份都读了）**：
  1. **PR 前置门**：related issue 必须 triaged（label）+ assigned 给你——read-only API 可自查（labels + assignees 对比 `gh api user`），否则只停在草稿。比 AgentScope 硬（那边是"快认领"，这边是"没分配不能开 PR"）
  2. **issue 必须人类手动开**（AI 可代拟文本）；AGENTS.md 例外：操作者明确指示时代理可用 gh 开——每次都要显式授权
  3. **DCO 签署**（git commit -s）硬要求；**禁止 AI 工具进 co-author 行**（AgentScope 那边 HsbcJone 用 cursor co-author 是常态，这边明令禁止）
  4. PR 描述必须披露 AI 使用（工具 + 程度）；人类对内容负全责；"能解释每一处改动"是明文要求
  5. 单元测试禁止调真 LLM/服务（录制的 cassettes 模式，tests/recorded/）；CHANGELOG 禁止手改；license 头由 pre-commit 管；反对机械/低价值贡献（可能直接关）
  6. 独立 refactor PR = 维护者专属，外部只能做已分配变更范围内的机会性小重构
- 与 AgentScope 工作流的差异清单：开 issue（人开 vs 代发）、PR 前置（分配门 vs 快认领）、署名（DCO 无 co-author vs co-author 常态）、默认分支（develop vs main）、测试（无真 LLM vs 无特殊限制）
- 轨道 1 判定影响：NeMo 的贡献形状 = "先 issue 讨论 → 拿到 assignment → 才动手"，节奏比 AgentScope 慢但预期明确；jailbreak rail 756 行 = #2028 竞争场的实际大小，一个下午可全文读
