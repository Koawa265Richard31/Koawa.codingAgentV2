# 缺口实现规划（整合版，2026-09-13）

来源：`audits/2026-09-11-platform-audit/`（§10 + smoke-readonly-review）与 `docs/open-active/`（psec/rtj 台账、d12-i7 问题台账）。本文件是缺口实现 的唯一审阅面；每项给出现状、方案、验收。状态标记：`已修待收口` / `本轮实施` / `台账逐项` / `设计裁决` / `等外部条件`。

## A. 已修待收口（工作区未提交）

| 项 | 内容 | 收口条件 |
|---|---|---|
| S1 | GitFacade 封闭环境对多账户属主仓库报 not_a_git_repository；已修（对配置根自授权 safe.directory） | 回归门（patch/verification 专项 + 全量）后提交 |
| F19 | 缓存命中遥测（协议+解析+fact 三处加法，7 测试绿） | 随 S1 同批提交 |

## B. 本轮实施（快赢，影响面小）——已完成（待提交）

| 项 | 缺口 | 方案 | 验收 | 状态 |
|---|---|---|---|---|
| S6 | task 主 loop `claim_gate=False`：任务中途 STOP 的"声称改动无写工具"不被拦截（finalize 兜底但错误码晚且模糊）。另 `build_worker(claim_gate=…, task_mode=True)` 参数被静默忽略 | task loop 构造传 `claim_gate=True`；`build_worker` 对 task_mode=True+claim_gate=True 组合校验 task loop 已启用门 | 单测：task 模式 loop `_claim_gate` 为 True；组合路径不再静默 | **完成**（tests/test_s6_s7_gate_and_descriptions.py 5/5；受影响面 30/30） |
| S7 | 工具描述缺陷：update_plan 未写明 statuses 枚举（pending/done）；read_file/list_files/search_text/repo_map 未写明路径相对仓库根 | 仅改 ToolSpec description 文本（repository path_schema、repo_map path、update_plan 顶层） | 现有工具测试不回归；描述含关键词 | **完成**（同上） |
| S4 | run9"completed 空 final_text"**定性为假警报**：turn.completed payload 键是 `summary`，取证文本在 completion-evidence 事件；`<tool_calls>` XML 是模型侧格式毛刺 | 无代码改动；结论登记 | 本节即为登记 | **闭合** |

## C. 台账逐项（按台账内验收标准执行，顺序即优先级）

| # | 来源 | 缺口 | 实施要点 | 验收 |
|---|---|---|---|---|
| C1 | D12-I7-001 | 失败集成回执复用返回 0/success | 复用分支读取并验证持久化 payload，按真实结果构造返回；不改 deliver 门 | 台账"验收条件"全表（复用不重测、失败仍拒交付、异常不入成功） |
| C2 | D13-D23-001 | 压缩替换块丢失结果语义（成功/失败同块，函数级已复现） | 替换块增加有界结果事实：每批次聚合 is_error 计数、代表行（错误码/退出码片段）、changed/test 线索（来源组结果摘要，截断至预算）；不改事件契约 | 重跑台账对照复现：A/B 场景替换块可区分；既有 d23 套件绿；重启等价仍成立 |
| C3 | D13-D23-003 | CLI 连续聊天失败回合未入历史 | cli.py 聊天失败分支追加失败 SessionTurn（复用 `_failed_turn_text`） | 同进程失败后下一轮上下文含失败回显；d16 套件绿 |
| C4 | D13-D23-002 | 结论投影漏 test_evidence_refs | `_conclusion_text` 附加证据引用行（有界） | 投影含引用；d23_session_projection 绿 |
| C5 | D13-D23-004 | 跨回合压缩块无界累积 | context_items 注入压缩块设累计字符预算（超限合并最旧块为单行摘要） | 台账 20-turn 复现场景：块总量有界 |
| C6 | D13-D23-005 | 跨回合模型摘要不持久化 | 摘要结果与来源绑定写入会话流（事件或 marker 文件），重启复用不重生成 | 重启后不再调摘要模型（计数断言） |
| C7 | D12-I7-002 | 首次投递缺完整基线前置检查 | **先裁决**允许的投递前置状态（拒绝全部脏 vs 受控快照绑定），再实现写入前拒绝 | 台账复现方案 + 裁决记录 |
| C8 | D12-I7-003 | worktree reap 缺封存前置 | 设计候选（disposition 状态机）——**先裁决再实现** | 设计记录 + 实现 |

## D. 设计裁决 / 等外部条件（不实施）

- B-3 agents 侧委派图接线（semantics-C 剩余全部）——D12+ owner 域，等 F5 面启动；
- B-1 残余（remote-MCP OAuth、凭据代管）、PDP/PEP 外置——等真实 MCP/OAuth 需求触发；
- T2 J2 on/off 消融、冒烟续跑——等模型 API 余额；
- Mimosa 13+14 findings 逐条分诊——离线可做，列次轮；
- takeover×sticky 交集审计、REVIEW-001、journal_inject_latest 字段去留——审计/裁决项。

## 执行顺序

本轮：B（S6/S7，S4 已结）→ 提交 A+B。次轮起：C1 → C2 → C3 → C4 → C5 → C6（全部离线可验）；C7/C8 待裁决。每项完成即更新本表状态。

## 进度日志

- 2026-09-13：B 组完成（S6 代码 2 处 + S7 描述 3 处 + 回归测试 tests/test_s6_s7_gate_and_descriptions.py）；S4 结案（假警报定性 + 依据）。受影响面 30/30 绿；全量回归 1040 项 OK（skips=32 为 Docker daemon 掉线）。

## E. 检索加固轨道（2026-09-19 新增，来源 hardening-memory-retrieval-2026-09-19 包）

安全加固设计包（context.md 证据清单 E1-E5 + hardening.md 评审 + proposals/bounded-result-retrieval.md 提案 + 3 图）定义了有界历史工具结果检索的实现轨道。与 C 组关系：**C5/C6（会话压缩块/摘要持久化）不在该包范围**（它管工具结果检索，不管会话块）；其"完整请求计量门禁"（E1/E2）与 C 组互补且优先级高——请求容量漏计指令与工具定义、无独立最终门禁。

工作包（按提案依赖序，**未授权不动工**，需维护者对提案逐节确认）：
1. 定义结果身份与可见等级（含"运行时已切断敏感可达性"的测试分类标准）；
2. 补完整请求计量门禁（E1/E2 修正：指令+工具定义+输出预留全计量，独立最终门禁）；
3. 旧记录可重建短元数据索引 + 权限校验（选项 1）；
4. 新回合持久化安全投影 + 分页读取与截断说明（选项 2，首次回执同入口）；
5. 跨边界与长任务验证（敏感样例、跨线程拒绝、命中率/拒绝率/p95 实测）。
验收基线：提案 Validation Plan 节。首版约束：测试/MCP 优先、敏感日志不进模型服务、MCP 白名单、历史文件正文快照不做。

E1/E2 两份原始缺口记录已被本包吸收为证据，移至 docs/closed-archive/；包内引用路径已更新。

## 进度日志（续）

- 2026-09-19：并入 hardening-memory-retrieval 设计包（新增 E 组）；E1/E2 归档。**C1 完成**：integrate() 复用分支回放持久化结果（known_negative 不再报 0/success；非法字段 integration_receipt_result_invalid fail-closed；成功复用与 deliver 拒绝语义不变），tests/test_d12_i7_001_reuse_status.py 3 项 + I7/I9 回归 41/41 绿。下一项：C2。
