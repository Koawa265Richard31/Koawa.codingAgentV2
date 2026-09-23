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

安全加固设计包已于 2026-09-20 更新为[已选完整设计](hardening-memory-retrieval-2026-09-19/proposals/bounded-result-retrieval.md)，附 E1-E9 证据清单、3 张边界图、结构化状态及[实施交接](hardening-memory-retrieval-2026-09-19/implementation/bounded-capture.md)。用户已接受方案并授权文档交付，**架构不再逐节待确认；运行时代码本次未实施**。C5/C6 的既有修复不在此重复实施；新方案连接其摘要与可信结果引用，另补完整请求计量、首返/恢复入口、安全诊断、检索和内容外发边界。F20/F21 需按最新取证独立核验，不能由本设计宣称关闭。

工作包（具体内容与验收以实施交接为准；此处记录后续顺序）：

1. 核验恢复/压缩基线，定义可信数据契约，接入首次回执、恢复、摘要和完整请求门；
2. 建立允许文件/合成输入的安全诊断环境及测试/MCP 适配器；
3. 发布不可变安全 JSON 投影，旧记录仅建安全元数据索引；
4. 实现当前作用域的搜索、分页读取、摘要引用与累计预算；
5. 接入固定产物外发，复用 D9/D7，并核验旁路与 uncertain；
6. 真实长任务、故障与成本实验决定页大小、累计限额、窗口及留存参数，不预设数字。
验收基线：提案 Validation Plan 节。首版约束：测试/MCP 优先、敏感日志不进模型服务、MCP 白名单、历史文件正文快照不做。

E1/E2 两份原始缺口记录已被本包吸收为证据，移至 docs/closed-archive/；包内引用路径已更新。

## 进度日志（续）

- 2026-09-19：并入 hardening-memory-retrieval 设计包（新增 E 组）；E1/E2 归档。**C1 完成**：integrate() 复用分支回放持久化结果（known_negative 不再报 0/success；非法字段 integration_receipt_result_invalid fail-closed；成功复用与 deliver 拒绝语义不变），tests/test_d12_i7_001_reuse_status.py 3 项 + I7/I9 回归 41/41 绿。下一项：C2。

- 2026-09-19（续）：**C3-C6 全部完成**。C3：CLI 聊天失败分支把失败回合写入同进程历史（恢复后与重启视图一致）。C4：结论投影增加有界 test_evidence_refs 行（计数+前 4 个 digest 前缀）。C5：跨回合压缩块总量受 history_max_chars/4（下限 1000）预算约束，超限最旧块折叠为确定性 merged 摘要行。C6：跨回合模型摘要经会话 marker 持久化（export/preload_summaries），重启按块位次重放、不再重调摘要模型。回归 tests/test_c3456_session_gaps.py 4 项 + 会话全套（d16/d19/d23 projection/recall/config）58/58 绿。**C 组（C1-C6）全部完成**；剩余 C7/C8 待裁决，D 组等外部条件。cli.py/session.py 的 CI 全量回归与 d16 交互端到端建议下窗口补跑。

- 2026-09-19（E 组开工）：**工作包 2「完整请求计量门禁」完成**（E1/E2 修正）。loop `context_chars` 计入 InstructionMessage（指令漏计修复）；`_maybe_compact` 计量先于 sink 判定，full request = context + 固定项（工具定义 schema，新增 `_definitions_chars`）；无 sink 路径超 hard 同样 fail-closed（`context_capacity_exhausted`），不再静默旁路。回归 tests/test_request_metering_gate.py 4/4（指令计量、定义计量、小请求通过、和值超硬）+ d23/wiring/d16 套件 62/62。E 组剩余：工作包 1（结果身份与可见等级）→ 3（元数据索引）→ 4（安全投影+分页）→ 5（验证），按提案依赖序推进。

- 2026-09-19（WP-A 基线核验①）：新增 tests/test_f20_second_compaction.py——同线程"失败回合 + 历史前缀 + 多工具轮 + 双次压缩预算"受控场景，钉住 WP-A 第一契约：**压缩门失败必须落诚实终态（d2:context_capacity_exhausted / d2:checkpoint_error）且线程保持可用**（F21 修复回归）。当前该场景实际终态为 context_capacity_exhausted（预算与 keep 组形状未调优），F20 映射根因（同 run 第二次压缩对齐，见 f20-correction.md）仍开放，下窗口以 `_source_versions` 全表打印定位。

- 2026-09-19（WP-A 深挖②）：recorder 级探针新增两条事实——① seed 各项共享版本 0，版本表非逐项递增；② 无 assistant 边界的连续工具轮被 parse_closed_groups 聚成单巨组。run11 intended 范围 0..18 因此极可能吞掉了 seed 前缀（instruction/user 本应是边界）——首次压缩"成功"但吞前缀，第二次压缩在替换后索引-版本错位上爆炸。WP-A 修正清单（assistant 边界多组复现、替换后对齐不变量、巨组契约）已写入 f20-correction.md；根因修复与 WP-1 实现待新窗口（上下文已尽）。

- 2026-09-20（WP-E 切片①）：**recall_history 模型工具上线**（src/koawa_agent_v2/retrieval/recall_tool.py + retrieval 包）。元数据-only（turn id/score/双预览≤120 字符/工具/文件），线程作用域自 execution context 解析，失败显式 recall_unavailable；经 assembly post_build_registrars 钩子注册进生产 registry（任务+chat 路径同享）。回归 tests/test_recall_history_tool.py 2/2 + assembly/wiring/d24/d10 39/39。E 组剩余：工作包 1（可见等级策略正式化）、3（旧记录回填索引）、4（安全投影发布事件族）、5（跨边界验证矩阵）。
