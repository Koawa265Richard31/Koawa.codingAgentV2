# R2–R5 收口实施记录（闭环审查 2026-09-25 → 修复）

日期：2026-09-25。基线：R1 修复之后的工作区。范围：审查报告 R2（发布时序/
读取来源/长流/来源绑定）、R3（recall 自由文本预览）、R4（独立最终请求门）、
R5（如实评估，未实施）。关联：[wpd-r1-progress.md](wpd-r1-progress.md)。

**总状态（2026-09-25 复验第二轮裁决）**：R2/R3/R4 = **部分完成**。R2 在
首次回执来源改造与按引用读取链（recall 接投影流）完成前不称闭环；钩子
故障隔离成立，但发布失败后原回执仍可进入上下文，"先成功发布再交付"的
完整契约不存在。预算重定基线经复验接受（压缩次数与恢复等价断言保留）。

## R2：发布时序、长流、来源身份（已实施）

### 长流翻页（去静默截断）

- `scan_test_results` 与 `ResultProjectionStore.read` 改为翻页走到短页
  （`_iter_stream`/`_projection_events`，页大小 500），不再有 1000 条硬上限。
- 测试：`LongStreamScanTest.test_scan_walks_past_first_page`——501 条策略
  标记回执，扫描计数越过第一页（untrusted=501）。

### 来源身份绑定（字段标记不再充当来源证明）

- 回执须同时满足：自身 `test_output_policy` 等于当前契约版本 **且**
  同一 (turn, model_turn, call) 身份在 ledger 上存在
  `tool_name == "run_test_profile"` 的可信执行记录
  （`ToolLedgerStore.load_for_call`）。无绑定记录的事实计入
  `untrusted`，绝不发布；ledger 读取失败的事实计入 `unverified`
  （fail-closed 同样不发布，但与来源不匹配区分——复验第二轮裁决）。
- `scan_test_results` 返回 `TestFactScan(facts, untrusted, unverified)`；
  `read_publication_status` 与 app 终态补发同步消费。
- 测试：`DurablePublicationStatusTest.test_unbound_fact_is_untrusted_not_pending`；
  `UnverifiedSourceClassificationTest.test_ledger_read_failure_counts_unverified`
  （注入 ledger 读取故障，断言 unverified=1 且不与 untrusted 混同）。

### 发布身份（复验第二轮必修修正）

- 事实身份为 **(turn_id, model_turn_id, call_id, content_sha256)**，经
  body_ref 携带 model_turn_id。首版身份漏掉 model_turn_id（call_id 仅在
  所属模型轮次内唯一）——复验实测：两轮复用 c1 且回执相同被错误折叠为
  1 条投影。已修：publish、失败登记、scan、状态归并采用同一完整规则；
  回归测试 `CallIdentityAcrossModelTurnsTest`（双轮同 call_id 同内容 →
  恰 2 条投影、model_turn_id 各异、digest 相同——内容校验，身份区分）。

### 发布时序（执行事实 → 投影 → 后续轮次；部分完成）

- 新增 `make_test_receipt_publisher(store, ledger, runtime)`：loop 在
  `durable_sink.tool_completed` 之后、`context.extend(results)` 之前调用
  （resume 与主循环两处接线）。即：工具执行事实已提交 → 投影发布 →
  回执才进入后续模型轮次的上下文。钩子故障隔离：发布失败登记
  unavailable 事件（与终态补发同身份），绝不失败 turn、绝不重执行工具。
  **复验裁决措辞**：发布失败后原回执仍会进入上下文，因此这不是
  "先成功发布再交付"的完整契约——当轮回执顺序仍是开放的 R2 切片。
- 终态补发（`_publish_result_projections`）保留为权威恢复路径，幂等。
- 生产接线：`assembly._make_projection_publisher` 注入任务 loop，chat 克隆
  loop 从任务 loop 继承同一 publisher。
- 测试：`InLoopPublicationOrderingTest`——第二轮模型脚本内探针读投影流，
  断言首轮回执的投影在最终轮产生前已提交，且全流恰好 1 条发布事件。

### R2 残余（如实声明）

- "模型首次回执来自已提交投影"未完全满足：产生回执的那一轮，模型看到的
  仍是工具执行路径给出的回执本身（metadata-only）；发布顺序保证覆盖的是
  "事实 → 投影 → **后续任何轮次**"。回执与投影内容一致性由同一
  output-policy 门与 digest 保障。完全的首轮回执缓冲需改 loop 协议，
  属独立切片。
- recall 工具尚未接到 result-projection 流读取（见 R3 残余）。

### 切片进展（2026-09-25 第三轮）

- **切片 (b) 按引用读取：已实施**。新模型工具
  `read_result_projection`（`retrieval/result_read_tool.py`，生产接线在
  assembly 检索 registrar）：按 (turn_id, call_id[, model_turn_id]) 读取
  已发布投影，线程作用域硬边界（跨线程 `cross_thread_read_denied`），
  可用性三态 `published`/`projection_unavailable`/`not_found`，只返回
  元数据投影与 body_ref，绝不回退原始事件正文；查询失败为
  `read_unavailable`，不伪造结果。测试
  `tests/test_result_read_tool.py`（命中/不可用/未找到/跨线程拒绝）。
- **切片 (a) 发布失败阻止回执进模型：协议提案待裁决**。涉及 recorder
  协议（durable 交付决策），按仓库纪律以策略文档为审阅面：
  [r2-delivery-gate-proposal.md](r2-delivery-gate-proposal.md)
  （方案 B 两段式 durable 交付 vs 方案 C 引用交付，含验收与裁决点）。

## R3：自由文本预览不再自动开放（已实施，范围如实声明）

- `recall_tool` 命中项删除 `user_input`/`final_text` 文本预览（原 120 字），
  改为 `user_input_chars`/`final_text_chars` 长度元数据；顶层新增
  `content_release: "unavailable_without_release_rule"` 独立状态——历史
  文本的释放须有按接收方的释放规则，缺规则即显式不可提供，不冒充错误、
  不回退原文。
- 测试：`test_hits_are_metadata_only_and_thread_scoped` 更新断言；
  `test_hit_text_never_reaches_provider_request` 观察最终 provider 请求
  （绑定 turn、先断言命中，再断言种子文本不出现）。**结论限定（复验
  第三轮）**：该测试观察请求上下文项（`.content`），非完整 provider 协议
  序列化——已证结论为"历史输入/回复预览不再经该检索回执泄漏"，不声称
  wire 级保证。

### R3 残余（如实声明）

- 按profile 的字段释放规则（verification 回执的 argv/exit_code 等诊断、
  MCP 正文长度）未实施：释放规则是部署输入（维护者定义），设计约束禁止
  由实现推导授权；需要先有 per-profile 契约配置面，再按契约裁剪字段。
- withheld/human_required 独立语义在测试证据链（finalize 交互）中的完整
  落地未实施。

## R4：独立最终请求门（已实施）

- 新增 `AgentLoop._assert_request_fits(context, definitions, max_output)`：
  在**同一本轮 pinned 工具快照**与最终消息列表上、`ModelRequest` 构造前
  运行；是否压缩、是否有 sink、是否存在可压缩组不影响门禁。
- 计量改为全量估算 `_request_estimate`：上下文项（含受信指令）+
  工具定义 **name + description + schema**（原只算 schema）+ 每项 48 字符
  协议开销保守估算 + 输出预留。这是明确的保守估算，不是已验证的 token
  精确计量（实测属 WP-H）。
- memory=None 的 loop 不再无界：回退到配置 schema 自身的 2,000,000 字符
  上限 + 4×max_output_tokens（或 schema 默认 8000）预留——fail-safe，
  不是调优预算。
- 压缩循环内三处硬检查从 `context_chars + reserve` 改为全量估算
  （原漏算定义与开销）。
- 测试：`test_final_gate_blocks_before_provider_when_compaction_off`
  （关压缩 + 超限 → provider 未被调用即拒）、`test_unconfigured_loop_still_
  gated_by_failsafe_ceiling`、`test_definitions_meter_name_and_description`。

### 受影响测试重定基线（计量修正的连带，如实记录）

全量计量修正后，两个按旧"欠计量"调校的测试场景需要重定基线——这是
计量变准确的直接后果，不是行为回归：

- `test_f20_second_compaction`：旧码在压缩循环内的硬检查只算 context，
  4 工具注册表（全量计量 ~4.1k）在 tiny 预算下连压缩都不会发生。首回合
  预算重定为 soft 5000 / hard 6200（预算扫描实证：2–4 次压缩后于发送
  边界诚实失败）；接受错误码表增补 `d2:request_capacity_exceeded`（新
  最终门与 context_capacity_exhausted/checkpoint_error 同属诚实失败族，
  测试注释同步）；follow-up（验证失败后线程可用）独立放宽到 8000。
- `test_d23_long_task_golden`：hard 20000 → 28000（注释说明 R4 计量
  变全量后同等历史需要更多余量）；soft 触发与 ≥3 次压缩断言不变。

## R5：未实施（如实评估）

R5 = WP-C（安全诊断环境与适配器），是自带多轮设计细化的独立工作包：
允许文件工作区与清单、受控依赖准备、固定候选准入、敏感容器约定、按
profile 的详细诊断适配器、合成秘密覆盖 env/文件/镜像/链接/网络/日志编码
的实验矩阵、宿主/Docker 不可用不静默放宽。其验收大量依赖真实实验与
维护者定义的释放契约，无法在不做实验的前提下诚实声称"部分完成"。
当前状态保持审查报告原判：`sandbox/runtime.py` 仍以只读方式挂载整仓库，
普通安全任务的详细诊断能力链未证明。实施入口：
`implementation/bounded-capture.md` WP-C 节（含 2026-09-21/22/23 已确认
的细化裁决）。

## 验证

- 目标模块：test_wp_d_projection / test_wp_d_r2 / test_recall_history_tool /
  test_request_metering_gate / test_agent_loop / test_output_policy_gate /
  test_runtime_assembly / test_d5_vertical_slice / test_s6_s7——47 项 OK。
- 全量回归：见审查报告闭合记录（本轮最终数字以报告为准）。

## 卫生修复（顺带）

- `ledger/executor.py` 移除 6367a16 带入的遗留 `DBG-445` stderr 调试打印
  （生产防御分支泄漏内部状态形状）。
