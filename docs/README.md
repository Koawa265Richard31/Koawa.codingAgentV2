# docs/ 目录结构（2026-09-13 整理）

全部设计/治理/操作文档按状态分为两夹；未删除任何文件，git 保留全部历史。

## closed-archive/ —— 已闭合（历史档案，只读参考）

- `day-01…day-25*.md`（30 份）：D1–D25 全部切片的设计文档——**均已实现并通过测试**（全量回归 1040 项）。
- `15-day-coding-agent-roadmap.md`：15 天路线图，25 个切片全部落地，计划闭合。
- `v2-stabilization-and-production-readiness-plan.md` + `v2-stabilization-detailed-implementation.md`：I1–I9 稳定化，已全部完成（b8 首次真实模型全绿交付）。
- `agent-redteam-jailbreak-plan.md`：RT/J v1.2，已执行完毕（J1 5/5、J2 9/9、校准门通过；J2 生产激活见 6367a16）。
- `agent-security-engineering.md`：事故→加固实录（交付物②），记录的事故均已修复或定性为环境态。
- `context-strategy-investigation.md`：上下文策略调查（F12–F16 断点已全部修复，commit 2d42746）。
- `s0…s5`、`b2-d10-normalization-ruling.md`、`t7-amendment-proposal.md`（原 psec/）：PSEC S0–S5 切片已实现闭合；B-2 已裁决（方向 b）；T7 修订已应用（a9d8896）。
- `i8-progress.md`、`i9-progress.md`、`p0-reproducers.md`、`rtj-plan-v1.1-snapshot.md`、`preflight-baseline.v1.json`、`stability-approved-skips.json`、`stability-capacity-baseline.json`（原 stability/ 及根散件）：I8/I9 完成记录与一次性档案。

## open-active/ —— 待办与活文档（有未闭合项或持续使用）

- `agent-security-community-roadmap-2026-09-30.md`：Agent 安全社区贡献与 KoawaAgent 学习的 8 周路线；首题为安全评测空转验证，含每周产物、证据标准和披露边界。仅规划，未启动外部贡献。
- `psec-progress.md`：PSEC 进度账本（**唯一状态事实源**）——未闭合项：B-3 委派图接线、B-1 残余（remote-MCP OAuth/凭据代管）、Mimosa 13+14 findings 逐条分诊、takeover×sticky 交集未审计。
- `rtj-progress.md`：RT/J 账本——sticky escalation×takeover 交集、变形/编码种子（declared 不检测）。
- `agent-security-threat-model.md`：活治理文档（T1–T7，随代码演进更新哈希）。
- `agent-security-platform-track-plan.md`：PSEC 轨道计划（剩余切片入口）。
- `s5-semantics-c-design.md`（原 psec/）：**未实施**的 semantics-C 设计草案——待实施。
- `d12-i7-receipt-reuse-followup.md`：切片阅读问题台账——D12-I7-001/002 待动态复现与修复（D12+ owner）。
- `d25-sandboxed-mcp-operations.md`、`d25-sandboxed-mcp-runbook.md`、`examples/`、`rtj-runbook.md`：持续使用的操作手册与配置样例。
- `Koawa_Runtime_Security_Audit_Handoff.md`：外部安全审计交接文档（待外部审计执行）。
- `hardening-memory-retrieval-2026-09-19/`：有界结果检索加固（2026-09-19 起）——设计（proposals/bounded-result-retrieval.md）、实施契约与工作包（implementation/）、WP-1/WP-D/R2-R4 进度记录。当前状态：WP-1 已提交；R1 完成、R2（修正版 B 交付门 + 按引用读取）、R3（释放契约）、R4（实测计量）、R5/WP-C v1（隔离工作区 + 真实生产证据）已实施待提交（r2r4-progress.md）；开放：读取链当前权限/检索限额、Docker manifest、MCP 适配器、敏感容器实验矩阵、WP-H。
- `implementation-closure-review-2026-09-25.md`：E 轨道实现闭环审查（基线 73270c2）+ R1 修复闭合记录、两轮复验修正与 R2-R4 部分完成裁决（文末追加节）。R5（WP-C 隔离诊断）开放。

## 相关目录

- `audits/2026-09-11-platform-audit/`：**现役**平台审计（§10 开口项：S1 facade 已修待回归、S4 空文本待定位、S6 task 主 loop claim gate、S7 工具描述、S8 设计裁决、T2 消融、真实模型续跑）。
- `audits/closed/2026-09-07-platform-audit/`：首轮审计，已被 09-11 取代，其 F1–F3 已修复（cbc0629/b29e4c6）。

注意：src/tests 的 docstring 中存在指向 `docs/day-23-*.md` 等旧路径的注释引用（仅注释，无运行时加载）；本整理不改源码注释。
