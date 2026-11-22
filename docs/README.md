# docs/ 目录结构

按状态分夹:**已闭合的进 `closed-archive/`,还在推进的留 `open-active/`,速查和笔记进 `note/`。**
删除或移动文件时同步改这份索引——索引过期比文件多更误导。

## closed-archive/ —— 已闭合(历史,只读)

D1–D25 切片设计、15 天路线图、I1–I9 稳定化、RT/J 红队计划、PSEC S0–S5、
上下文策略调查等。**都已实现并通过测试**,保留是为了追溯"当时为什么这么设计"。

规则:闭合的东西只进不出,不再修改正文;需要补充就在新文档里写。

## open-active/ —— 活文档

### 安全治理(长期有效)

- `agent-security-threat-model.md`:T1–T7 威胁模型,随代码演进更新。
- `psec-progress.md`:PSEC 进度账本。未闭合:B-3 委派图接线、remote-MCP 凭据代管、Mimosa 告警分诊、takeover×sticky 交集。
- `rtj-progress.md`:RT/J 账本。未闭合:sticky escalation×takeover、变形/编码种子(声明不检测)。
- `agent-security-platform-track-plan.md`:PSEC 轨道计划(剩余切片入口)。
- `s5-semantics-c-design.md`:**未实施**的 semantics-C 设计草案。
- `Koawa_Runtime_Security_Audit_Handoff.md`:外部安全审计交接(待执行)。

### 有界检索加固(2026-09-19 起)

- `hardening-memory-retrieval-2026-09-19/`:设计、实施契约、各工作包进度。
  **R1–R5 与 WP-C v1 已合并(PR #6)**;未闭合:WP-E 主体(recall 接投影流)、WP-H(长任务参数报告)、敏感容器实验矩阵。具体状态以该目录内的进度文件为准。
- `implementation-closure-review-2026-09-25.md`:E 轨道闭环审查记录(含两轮复验修正)。
- `d12-i7-receipt-reuse-followup.md`:切片阅读问题台账(D12-I7-001/002 待复现修复)。

### 多 Agent 身份与环境(2026-11)

- `agent-team-security-baseline.md`:产品级基线对照 + F1–F7 安全问题清单。
- `session-root-and-identity.md`:F2 落地说明——root 由会话签发,不再是一个名字。

### 操作手册(持续使用)

- `d25-sandboxed-mcp-operations.md`、`d25-sandboxed-mcp-runbook.md`、`rtj-runbook.md`、`examples/`。

### 外部协作与个人轨道

- `agent-security-community-roadmap-2026-09-30.md`:8 周社区路线。
- `community-contribution-progress.md`、`community-contribution-retrospective.md`:进展与复盘。
- `test-evidence-layer-rule.md`:可迁移规则(判分必须消费执行层证据)。
- `repo-map-and-architecture.md`:架构图快照(基线较早,结构图仍可参考;逐模块清单见 `note/module-map-detailed.md`)。

## note/ —— 速查与笔记

- `module-map-detailed.md`:**自动生成**的模块详细地图(每个公开类的字段、每个函数签名)。大改动后重跑 `python scripts/gen_module_map.py`。
- `d13-d23-context-memory-reading-notes-2026-09-16.md`:上下文/记忆阅读笔记。
- `r2`–`r5` 引导阅读单元、`r5-complete-plain.md`:通俗讲解稿。

## 相关目录

- `audits/2026-09-11-platform-audit/`:现役平台审计。
- `audits/closed/`:已取代的审计。

## 维护约定

1. 新文档先问"它属于哪一档",再决定放哪;不确定就放 `note/`。
2. 一个轨道只保留**一个**状态文档;切片完成后把进度文件移进 `closed-archive/`。
3. 删除文件前先在索引里找引用;删完同步改索引和 README 链接。
4. `module-map-detailed.md` 是生成物,不要手改。
