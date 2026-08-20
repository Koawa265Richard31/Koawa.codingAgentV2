# D15：End-to-End Acceptance 与面试成果包

## 1. 产品入口

`python -m koawa_agent_v2.cli run <db> <repo>` / `resume <db> <turn_id> <repo>` /
`cancel <db> <turn_id>` / `status <db>` / `doctor <db>`。
真实凭据只来自受控 provider/env，不写入配置或 trace。

## 2. 最终链路与矩阵

本日 CLI 走通：选 Git repo → Thread/Turn → 模型回合 → Registry→Policy→Ledger
写 patch → 证据 final → 重启后 `status`/`resume` 查询或续跑同一 durable task；
`cancel` 终止非终态 turn。dispatch-contract 覆盖 built-in 与 MCP 两个入口
（subagent 入口由 D11 的写工具拒绝 `d11_write_forbidden` 覆盖）。真实子进程
kill 窗口由 D6/D7/D8 的 kill fixtures 覆盖，D15 不重复实现 kill 基建。

## 3. 架构与状态图

~~~text
Model -> AgentLoop -> Registry(schema) -> Policy(ALLOW/ASK/DENY)
      -> ApprovalService(durable grant) -> D7 Ledger(claim/one-shot)
      -> built-in/MCP(stdio fixture)/subagent(agent control)
      -> D8 sandbox / D12 worktree / D11 graph / D13 context / D14 trace
~~~

D1–D15 状态：D1 Event Store、D2 Model/Loop、D3 Registry、D4 Patch、D5
Test/Git/Finalize、D6 Recovery、D7 Ledger、D8 Docker、D9 Policy/Approval、
D10 MCP、D11 Multi-Agent、D12 Worktree、D13 Context/Compaction、D14
Trace/Eval、D15 CLI/E2E —— 全部 COMPLETE。

## 4. 讲法

- 5 分钟：问题（agent 可靠执行）→ 事件溯源控制面 → schema→policy→ledger→
  sandbox 一条链 → 崩溃恢复与 exactly-once 边界 → 全量 323 tests。
- 15 分钟：在上述基础上展开 D6 checkpoint、D7 crash matrix、D9 授权事务、
  D10 MCP binding、D11 orphan 接管、D12 集成投递、D14 eval/故障注入。

## 5. 深挖材料

Checkpoint/Resume（day-06）、Ledger crash matrix（day-07）、Sandbox threat model
（day-08）、MCP lifecycle（day-10）、Multi-Agent isolation（day-11/12）。

## 6. ADR 与追问

关键 ADR：事件存储、模型协议、Patch 原子性、恢复、Ledger、Docker、MCP（分别见
各 day 文档）。常见追问要点分散记录在各 day 文档的“完成边界/推迟风险”章节。

## 7. 已知边界（README 公开）

- 本环境 Docker daemon 不可用，D8 真容器集成与 D12 真容器 runner 以 skip/注入
  runner 呈现；Docker 可用环境按 D8 原验收运行。
- Streamable HTTP MCP、IDNA、任意 JSON Schema、dirty 基线快照、真实模型 eval
  为显式非目标。
