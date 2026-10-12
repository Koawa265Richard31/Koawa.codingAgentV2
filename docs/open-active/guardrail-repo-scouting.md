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
