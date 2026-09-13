# KoawaAgent V2 平台审计续审报告（2026-09-11）

日期：2026-09-11。本报告接续 `../2026-09-07-platform-audit/report.md`（下称"首轮报告"），是其续审而非替代：首轮结论在本轮逐项复核，未推翻任何一条；按首轮第 7 节续审入口推进后，新增 3 项实测确认发现（F6、F7、flaky 测试观察）并大幅强化 F5。

**同日第二阶段（用户指令"一口气审完"+"api你直接用"）**：Docker daemon 拉起后全部容器 lane 通过（含此前一直缺席的 golden 全链 kill/resume 复合测试）；三项静态审查（schema/并发、MCP transport、凭据持久层）由主审完成（三个审查子代理因账号用量上限失败，21:07 重置）；全量回归重跑（首次因与容器 lane 并发产生 d10 竞争假阳性，已单独干净重跑确认，见 §9）；真实模型链被凭据阻塞（环境唯一 key `AAA1_API_KEY` 在 z.ai/bigmodel 全部兼容端点 401 失效，`KOAWA_PROVIDER_KEY` 未设置），基线已冻结于 `baseline-real-model.md`。

**总体判定不变，证据更强：全项目审计仍为部分完成；当前证据不足以认定本项目已具备"产品级任务交付能力的 Agent 安全研究平台"。** 首轮已证实完成门接受与测试不一致的产物（F1，本轮复现成立）；本轮进一步证实文件事务层存在回滚失败即永久丢失原始内容的路径（F6，新），生产装配下 >4 个必测 profile 的合法配置在数学上不可完成（F7，新），验证证据完全不跨中断持久化（F8，新，代码推断）。安全增量方面，J2 的 JSON 配置激活入口仍断（F2，复现成立），多 Agent 委派面被证实整体位于安全周界之外且无真实模型 provider（F5，强化）。同时本轮再次确认：控制面/恢复/账本/策略/压缩等底座机制在专项与 golden 级测试中真实工作，项目仍是有明确范围的机制研究平台。

无同条件 Codex 对照数据，不作领先/落后/等同判断。本判定不等于"所有任务都无法完成"。

---

## 1. 基线、环境与证据纪律

- HEAD：`f3d2664ca3b848e0d73bf1170ba5a132be6c1e83`（与首轮相同，本地无新提交；`origin/main` 仍为缓存的 `e213580`，未 fetch）。
- 工作区改动：README、runtime/assembly.py、runtime/config.py、verification/finalization.py、verification/tools.py、两个测试文件有未提交修改，另有未跟踪的 tests/test_d5_required_profiles.py 等。**时间戳核实：全部 tracked 修改最后写于 2026-09-07 16:31–20:36，之后未再变化**，与首轮报告描述的并行会话一致；归属未变，本轮未覆盖、回滚、暂存或提交它们。本轮结论用 `file-hashes-20260911.json` 的指纹绑定这些字节。
- **首轮与本轮之间存在一次未写报告的续审会话（2026-09-09）**：审计目录内新增 `patch-windows.json`、`repro_patch_windows.py`（D4 kill 窗口注入）与 `full-regression-20260909.txt`（1027 tests / 1 failure / 32 skipped）。本轮复核并复现了其 kill 窗口结果（语义一致），并将其唯一失败测试三角定位为时序竞态（见 §3 O-F）。
- 环境：Windows，解释器 `C:/Users/qaz14/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`（Python 3.12.14，与首轮一致），`PYTHONPATH=src`，`PYTHONDONTWRITEBYTECODE=1`。真实容器/Docker：第一阶段 daemon 未运行（未启动）；第二阶段经用户授权拉起 Docker Desktop（daemon 29.6.1，本地 digest 锁定镜像，未下载任何镜像），容器 lane 全部实跑（§9.1）。
- 本轮无生产代码修改、无原断言修改、无提交推送、无付费模型调用。新增产物仅在本目录：`repro_profile_budget.py`、`profile-budget.json`、`focused-tests-20260911.txt`、`core-tests-20260911.txt`、`patch-windows-rerun-20260911.json`、`snapshot_hashes.py`、`file-hashes-20260911.json`、本报告。
- 本轮实际执行 vs 引用历史：§2 表中全部为本轮实跑；09-09 全量回归与 6367a16 记录的 1016/1016 为引用历史结果（已注明）；两轮源码追踪子审查为本轮完成（首轮该项因用量限制中止，本轮以两个只读子审查关闭）。

### 本轮范围与未覆盖（诚实清单）

已覆盖：首轮 7 个续审入口中的 #1（对齐+复现复核）、#3（三条生产链）、#4（D4 三窗口）、#5（委派/ancestor 接线）、#6（Docker 条件检查——二阶段已实际执行全部容器 lane）、#7 的离线部分（D11/D12/golden/D21/D5/core 专项）。#2（独立静态/架构全量审查）在二阶段以主审定向审查方式覆盖了三个最高价值面（schema 迁移/并发、MCP transport 全量要点、凭据持久层），仍不是全量逐行审查。

仍未覆盖：**任何真实模型端到端任务**——这是唯一剩余的执行链缺口，阻塞在凭据（`AAA1_API_KEY` 失效、`KOAWA_PROVIDER_KEY` 未设置），基线与四分类判定已冻结（`baseline-real-model.md`），key 就绪即可执行；全量回归本轮两跑（§9）；schema 迁移与并发组合的运行级验证、MCP transport 逐行审查仍以锚点级静态审查为限。**因此仍报告"全项目审计部分完成"，但未覆盖项已收敛到"真实模型链"一项执行缺口 + 静态审查深度声明。**

---

## 2. 本轮执行验证汇总

| 验证 | 命令/位置 | 结果 | 等级 |
|---|---|---|---|
| F1 内容漂移反例复核 | `../2026-09-07-platform-audit/repro_content_drift.py` | `completion_accepted_after_drift=true`、`status_digest_equal=true`、独立验收 exit=1 | **实测确认**（复现成立） |
| F2 J2 配置入口复核 | `../2026-09-07-platform-audit/repro_config_and_eval.py` | `canary_key_env` → `config_unknown_field`；eval 20/20 且 `agent_loop_calls=0`（F3 一并复核） | **实测确认**（复现成立） |
| D4 kill 三窗口 + 回滚失败 | `repro_patch_windows.py` 重跑 | 与 09-09 产物语义一致（见 F6） | **实测确认**（双重复现） |
| F7 profile 预算断层 | `repro_profile_budget.py`（新增） | 第 5 个必测 profile `test_run_budget_exceeded`，finalize `required_test_profile_not_run`，门 `verification_required` | **实测确认** |
| D5/J2/T7/golden 专项 | `focused-tests-20260911.txt` | 30/30 OK（53.8s，无 skip） | 实测确认 |
| 控制面/恢复/账本/策略 | `core-tests-20260911.txt` | 124/124 OK（46.4s） | 实测确认 |
| D11 委派面 + D21 安全 | test_d11_{control,scheduler,concurrency,process_kill} + test_d21 | 68/68 OK（120.5s） | 实测确认 |
| D12 集成 + golden 复合 | test_d12_workspace_integration + test_golden_composite_e2e | 6 OK + 1 skip（docker）；2 OK + 2 skip（docker/嵌套前置） | 实测确认（容器 lane 缺席） |
| D25 close-hang 治理测试 | 单测循环 ×3，两解释器 | 3.12：2/3 失败；3.13：1/3 失败 | **实测确认**（flaky，见 O-F） |
| 三条生产链源码追踪 | 子审查（app→assembly→worker→executor→verification） | 见 F7/F8/F9 锚点 | 代码推断（锚点核实） |
| 委派/ancestor 接线追踪 | 子审查（agents/、workspace/、unified.py、loop.py） | 见 F5 锚点 | 代码推断（锚点核实） |
| **D8 真实容器 lane** | `docker-lane-d8.txt`（daemon 29.6.1，本地 pinned 镜像 sha256:2c941e…，未下载） | **14/14 OK**（56.2s） | **实测确认**（真实容器） |
| **D25 W5 真实容器 MCP** | `docker-lane-d25w5.txt`（koawa-d25-filesystem:fixed digest 锁定） | **2/2 OK** | **实测确认**（真实容器） |
| **golden 全链 kill/resume 复合 + D12** | `docker-lane-golden-d12.txt` | **11/11 OK**，含 `test_full_composite_kill_and_resume`（此前两轮均因 daemon 缺席被跳过） | **实测确认**（真实容器+真实进程 kill） |
| d10 竞争假阳性三角定位 | `d10-retest-clean.txt` | 首次全量回归 7 error 全为 d10 `mcp_request_timeout`（与容器 lane 并发）；单独干净重跑 **OK** | **实测确认**（假阳性归因） |
| 静态审查 ×3 | 见 §9（schema/并发、MCP transport、凭据持久层） | 无新缺陷；S4 零消费者结论复验不变 | 代码推断（锚点核实，主审执行） |

人工介入说明与首轮相同：本轮所有"成功"均在脚本模型/预设桩下取得，不计自主推理证据。

---

## 3. 发现

编号沿用首轮；F6–F9 为本轮新增；O-F 为测试观察。

### F1：完成门未把测试/最终报告绑定当前文件内容（交付底座 P1）——复核成立，实测确认

首轮全部内容维持。本轮复现：漂移后 `completion_accepted_after_drift=true`、独立验收 exit=1。生产链追踪补充了两个新锚点：完成门唯一生产消费者是 `execution/loop.py:653-655`（STOP 时 `assert_complete`，失败转 `fail_turn`），接线在 `runtime/assembly.py:566`（`completion_gate=registry`）；status digest 只含 `(状态码, 路径)`（`verification/git.py:184-190`），同路径同状态码的内容篡改对门不可见（`verification/finalization.py:255-266`）。最小建议不变：证据绑定工作树内容与 HEAD/index 身份；验收含"同路径同状态不同字节"等四场景。

### F2：J2 JSON 配置入口拒绝激活字段（安全增量接线 P1）——复核成立，实测确认

`runtime/config.py:977-1001` 允许字段列表仍缺 `canary_key_env`，未知字段立即拒绝；同文件 1235-1252 行读取它、`runtime/assembly.py:1131-1150` 在配置存在时确实构造 `SecurityGate` 并注入 `LedgerExecutor`——机制在，JSON 入口断。最小修复仍是把字段加入 loader 白名单并用"配置文件→装配→合成 canary→持久 ASK→重启/批准"的集成测试验收（off 对照）。

### F3：eval 成功率不经过 Agent 执行链（实验可信度 P1）——复核成立，实测确认

本轮复现 `agent_loop_calls=0` 下 20/20。补充生产链事实：真实生产入口存在且不是 eval 的对象——console script `koawa-agent-v2`（pyproject.toml:14）→ `runtime/cli.py:344-406` 双模式分发（带 `--config` 走 `_real_main`→`AppRuntime`；无 `--config` 是 D15 FakeProvider 演示路径，cli.py:76-110、231-243）。eval 应改测这条链或明确标注为 fixture 自洽验收。

### F4：J2 校准是检测器级字符串匹配（研究方法 P2）——未变，代码推断

首轮结论维持；本轮无新证据，不重述。

### F5：真实委派与祖先接线未获生产证据（交付/研究缺口 P1）——强化：委派面整体在安全周界外，代码推断（锚点核实）

本轮子审查把首轮"未接线"细化为完整图景，**仓库存在两套互不连通的 Agent 体系**：

1. **生产体系**（`runtime/app.py`→`assembly.py`→`AgentLoop`+`LedgerExecutor`+`SecurityGate`）：单 loop、无委派。两处 AgentLoop 构造（`assembly.py:563` 任务模式带门、`assembly.py:168` 会话模式 `completion_gate=None`）均未传 `ancestor_turn_ids`，恒为默认空 tuple（`execution/loop.py:233`）→ 生产中 `SecurityGate.hit_multi` 的 ancestor 分支永远遍历空集（`security/gate.py:52`）。B-3 未闭合，与 PSEC 账本一致。
2. **委派体系**（`agents/` + `AgentScheduler` + `workspace/integration.py`）：src 内唯一组装点是 `runtime/unified.py:92-118`，仅被测试引用，非生产组合；且该路径在 `_durable_turn`（unified.py:161-180）直接伪造完成证据并 `complete_turn`，完全绕过 D5 门（测试面）。子 agent 执行体是 `AgentScheduler.run_attempt`→`ScriptedAgentProvider.run`（`agents/scheduler.py:37-55,335-357`）——**无真实工具调用、无 policy、无 ledger、无 J2 扫描**，唯一"授权"是脚本声明的工具名前缀 allowlist（scheduler.py:27,50-54）。`OpenAICompatibleChatClient`（model/openai_client.py:167）与 provider.run 协议不兼容且无桥接 adapter → **子 agent 无法接真实模型**。
3. mailbox 中继（TASK/RESULT，`agents/control.py:1026-1223`）不经过任何 canary/J2 扫描（agents/ 零 security 导入）；`body_ref` 允许 2048 字符任意文本，unified.py:110 把任务全文放入。T7-c 边界测试钉住的是"无信任标记"这一声明式放弃。
4. takeover（`agents/control.py:672-814`）不触碰安全状态：无 SecurityStateStore 读写、无恢复后 J2 复查；sticky escalation 仅因事件存储 append-only 而隐式保留，无人消费。**新缺口（代码推断）**：恢复侧 `_authorize_terminal`（`ledger/executor.py:623-679`）复验 policy/claim/approval 但**不重跑 `_j2_check`**（只有 CLAIMED 路径 :340-363 复查）——已执行完成的工具在恢复时不再扫 canary。
5. 产物集成器 `ArtifactIntegrator`/`DurableArtifactIntegrator`（重测+HEAD+digest 门，`workspace/integration.py:109-111,284-285,597-681`）在 src 内**零生产调用**（仅测试引用）。

影响：任何"委派+安全"的研究声明（T7、semantics-C、sticky×takeover）目前都没有可运行的生产对象；不构成已确认越权。最小建议：先做 AgentLoop→provider.run 适配器让子 agent 进真实执行平面（继承 SecurityGate/policy/ledger），ancestor_turn_ids 从 spawn 图派生（`agents/control.py:2760` 已有 turn_id 进事件 metadata 的数据源），再谈委派安全实验。

### F6（新）：D4 回滚失败后清理无条件删除备份，原始内容永久丢失（交付底座 P1）——实测确认

- **触发条件**：多文件 patch 提交阶段抛异常（如外部进程重建目标文件、杀软/权限导致 `os.replace` OSError、磁盘错误），且回滚该文件同样失败（`_assert_missing` 报 `stale_patch_base` 或 replace OSError）。
- **预期**：D4 契约承诺进程内异常时 stage/backup/rollback 恢复原状；至少 backup 保留以便手工恢复。**实际**：`_rollback`（`editing/transaction.py:400-439`）对该文件恢复失败后，`_discard_uncommitted(staged)`（:514-528）对 staged 中每一项**无条件 `unlink(item.backup)`**——而 `_stage()` 返回的 `staged` 包含已进入 commit 的项（:300-337），于是恢复失败文件唯一的原始内容副本被删除。
- **复现**：`../2026-09-09` 产物 `patch-windows.json` 第 4 案例与本轮 `patch-windows-rerun-20260911.json` 双重复现：注入 commit+rollback 双失败后目录仅剩 `b.txt`（旧内容），a.txt 与其 backup 临时文件均消失，工具结果 `workspace_outcome_unknown`——"unknown"标签诚实，但状态已不可恢复。
- **同轮确认的三个 kill 窗口**（进程被 kill，无清理机会，备份留在盘上）：①移走原文件后 kill → a.txt 缺失、retry `patch_target_missing`、无自动恢复（手工可从 `.koawa-patch-backup-*` 恢复）；②首文件安装后 kill → a.txt 新/b.txt 旧，retry `stale_patch_base` 拒绝，部分状态保留；③handler 完成但结果未记账即 kill → 两文件均已更新，retry 报 `stale_patch_base`、ledger 落 **failed**——已成功的写入被记为失败（保守方向、无危险重复，但结果标签错误，下游会按"需重做"处理）。
- **影响**：用户/Agent 已有内容可能被永久销毁；"不重复已完成的危险副作用"在 ②③ 成立，"失败后能继续"在 ①②③ 均不成立（不自动恢复、需模型从头重建 patch）。
- **最小建议**：`_discard_uncommitted` 只删除 `original_moved=False` 或已成功恢复项的 backup（恢复失败的 backup 必须保留并报 `workspace_outcome_unknown` 带残留物清单）；kill 窗口的 `.koawa-patch-backup-*`/`stage` 残留应被恢复流程识别（重试 `patch_target_missing` 时提示 backup 位置或自动还原）；窗口③应在 retry 时识别"目标已是期望内容"并落 SUCCEEDED。验收 = 本目录 `repro_patch_windows.py` 四场景的期望语义化断言。

### F7（新）：必测 profile 配置上限 64 与生产装配默认预算 4 的断层（交付底座 P1）——实测确认

- **触发条件**：JSON 配置 `required_test_profiles` 含 5 个及以上 profile（config.py:788-811 只拒绝空与 >64，5 个合法），经生产装配运行（`assembly.py:516-523` 传 `required_test_profiles` 但**从不传 `verification_limits`** → `VerificationLimits()` 默认 `max_test_runs=4`，finalization.py:32-48）。
- **预期**：配置合法即可满足。**实际**（`profile-budget.json`）：第 5 次 `run_test_profile` 预约即 `test_run_budget_exceeded`，finalize `required_test_profile_not_run`，完成门 `verification_required`——**任务在合法配置下永不可能完成**。完成方向 fail-closed（诚实），但交付被静默预算卡死。代码推断补充：恰好 4 个 profile 时，任何 patch 后全部证据 stale（finalization.py:104,173-176），重跑需 4 次新预约 > 剩余预算 → 同样不可完成；即生产默认实际只支持"≤4 profile 且测试后零 patch"。
- **影响**：多 profile 修复-重试工作流结构性不可用；与 O1（首轮）同源，本轮升格为确认缺陷。
- **最小建议**：装配时推导 `max_test_runs ≥ len(required_profiles) × 期望重试轮数`（或配置校验时拒绝不可满足组合并显式要求预算）；验收 = 5-profile 修复一轮后可 finalize。

### F8（新）：D5 验证证据不跨 run/中断持久化，resume 后完成条件摩擦（交付底座 P2）——代码推断（锚点核实）

- `VerificationLedger._runs` 是进程内 dict（finalization.py:94），按 run_id 键控；`request_resume`→`start_turn` 派生新 run_id（control/runtime.py:285-288,892-952）→ 暂停/崩溃恢复后全部测试证据、generation 清零，模型必须重跑全部必测（成本与轮数压力，叠加 F7 预算）。
- 跨进程 resume 时 GitFacade baseline 重采样（assembly.py:383-386 + git.py:132-142）：上一轮 Agent 改动并入新 baseline，而 finalize 要求本 run 有 Agent 改动（`no_agent_changes`，finalization.py:189-190）→ 恢复后模型必须再打至少一次 patch 才可能过门。
- 不对称：chat turn 在本进程内走无门 worker（app.py:463-467 `_chat_turn_ids`），**跨进程 resume 同一 chat turn 会落回带门 worker** → `verification_required` 拒绝（设计内旁路只在本进程成立）。
- **影响**：长任务中断恢复"能继续但代价高且条件变严"，与"从失败、超时、压缩和中断中继续工作"的目标有实测落差风险（未做真实模型验证，故 P2 代码推断）。
- **最小建议**：把 VerificationLedger 证据持久化到事件存储并按 (turn_id, content-binding) 复用；resume 时预算独立计算；chat turn 标记持久化或在门处识别 turn 类型。验收 = kill→resume 后不重跑测试即 finalize 通过（内容未变时）。

### F9（新）：finalize 报告的唯一消费者是模型，用户面无确定性验证报告（证据可信度 P2）——代码推断（锚点核实）

- `finalize_task` 报告（含 required/optional 测试结果，finalization.py:199-236）作为工具结果返回给模型（verification/tools.py:154-168）；没有任何 runtime 代码把它写进用户可见输出——`_truth_outcome` 的 `evidence_digest` 只是 `sha256(final_text)`（control/runtime.py:1229-1269、app.py:479-488），turn_summary 只用 ok_tools+changed_files（runtime/turn_summary.py:28-39）。用户能否看到真实测试证据完全依赖模型转述（system prompt 要求，config.py:919-936，无强制）。
- **影响**：与 F1 叠加——证据既不绑定内容又不强制呈现，"最终结果可信"缺少确定性出口。
- **最小建议**：run 结束时把 finalize 报告（或其 digest + required profile 结果矩阵）作为结构化字段写入 truth/turn_summary 并在 CLI 输出。

### O-F（新观察）：D25 close-hang 治理测试时序竞态 flaky——实测确认（产品语义无缺陷）

- `test_d25_g3_g4_governance.G3ProtocolLifecycleTest.test_real_close_hang_is_bounded_and_reported_unknown`（tests:167-188）断言 200ms 截止内"无法证明退出"（`process_exited=False`）。实现（mcp/transport.py:830-899）在共享截止内会**强制 kill 进程树并 reap**；快机器上 kill+reap 在预算内完成 → `process_exited=True`（诚实报告已退出）→ 断言失败。本轮 3.12 下 2/3、3.13 下 1/3 失败；09-09 全量回归的唯一失败即此（非工作区改动引入）。
- **影响**：治理门测试不稳定，回归信号有噪声；产品行为（有界关闭、强制 kill、如实报告 exit/uncertain）本身正确。建议改测试断言（接受"预算内证明退出"与"未证明"两种诚实结果，或固定时钟注入）。

### F10（新，实测确认）：apply_patch 线格式对模型不可自学，写入链第一步断裂（交付底座 P0）

- **触发条件**：任何真实模型经生产链执行写操作。工具描述（editing/tools.py:46-50）只说 "schema_version=1 structured patch JSON" + SHA 提示，**未定义 changes[]/hunks 字段格式**；系统提示（config.py:919-936）只讲流程不讲格式；错误响应只回错误码（`{"code":"invalid_patch_change"}`），零格式提示——模型在运行时没有任何渠道学到 patch 文档结构。
- **实测**（§9.4）：DeepSeek-V4-Flash 两轮共 26 次 apply_patch 尝试 0 成功（invalid_patch_change ×25、invalid_patch_document ×1），从未到达 base 哈希比对层；120 次工具预算耗尽，任务未交付。read_file 已返回 sha256（接口可行性具备），缺的是格式描述。
- **影响**：与模型强弱相关但结构性存在——格式只能靠猜。垂直切片测试全绿是因为测试程序化构造 patch JSON，掩盖了该断点。
- **最小建议**：在工具 description 或错误响应中加入 patch 文档 schema（字段+示例）——一行 JSON Schema 即可；验收 = 真实模型（或脚本模型按描述构造）首次 patch 成功率。

### F11（新，实测确认）：宿主进程 kill 后的 CLI 崩溃恢复链不可用，重复恢复永久卡死（交付底座 P0）

- **触发条件**：`koawa-agent-v2 run` 进程被 kill（真实崩溃）→ 等租约过期 → `koawa-agent-v2 resume --turn-id`。
- **实际行为**（b5 干净路径，确定性复现）：resume 经 claim_stale 启动新 run，该 run 在第一个 ownership 心跳点死亡——`control/runtime.py:716` 抛 `InvalidTransition("recovery lease token mismatch")`，`recovery/store.py:349` 包装为 `LeaseConflict("lease heartbeat failed")`（消费链 worker.py:366 → loop.py:776,802），CLI 返回无 payload 的 `runtime_error`。新 run 本身又成 stale。
- **恶化路径**（b4，多次恢复尝试）：反复 abandon/requeue 后命中 `recovery/store.py:175` 的 `checkpoint_projection_mismatch`（发布路径硬失败、无回退），turn 进入**确定性永久卡死**，CLI 无逃生通道（三次重试同码）。
- **覆盖盲区解释**：D6 recovery 专项（core 124 内）测显式 pause/resume；golden 全链 kill/resume（§9.1，实绿）kill 的是容器/子进程边界。"宿主进程死亡→CLI resume"此前无测试覆盖。
- **影响**："从崩溃中继续工作"这一平台核心声明在真实用户路径上不成立；叠加 F8（证据不跨 run）与 F6（kill 窗口残留），中断恢复面目前实测三处断点。
- **最小建议**：修 lease token 传递（claim_stale 的 recovery lease token 与 TurnWorker 心跳 fence 的 token 对齐）；`checkpoint_projection_mismatch` 在发布路径提供回退或运营逃生（如强制丢弃缓存重放）。验收 = 本审计 b5 序列（kill→过期→单次 resume→run 到终态）。

### O1 残留说明

首轮 O1 的正向部分（必测失败不可被其他 profile 成功掩盖、遗漏/stale 拒绝、D7 复用不自动生成新 D5 证据）本轮 30/30 专项再次确认（含并行会话新增的 tests/test_d5_required_profiles.py，未提交状态不变）；其预算担忧已由 F7 实测确认并升格。

---

## 4. 完成条件表（更新判定列，其余沿用首轮）

| 期望保证 | 权威来源 | 生产强制入口 | 本轮判定 |
|---|---|---|---|
| 所有必测通过 | trusted profile 配置+真实 CommandResult | `assert_complete`（loop.py:653-655） | 机制成立（30/30）；但 >4 profile 配置结构性不可完成（F7 实测） |
| 测试对应实际交付内容 | workspace 内容+测试结果 | assert_complete | **F1 实测未拒绝**（复现成立） |
| 用户已有修改保留 | dirty baseline 指纹 | GitFacade+patch guard | baseline 路径成立；**F6 实测：commit+rollback 双失败路径销毁原始内容** |
| UNKNOWN 不伪装成功 | durable tool ledger | LedgerExecutor/recovery | ledger 语义成立（专项）；**窗口③实测：已成功写入被记 failed（标签错误但保守）**；恢复不自动完成（窗口①②） |
| 恢复能继续并重建验证 | checkpoint+产物 | TurnWorker+registry | 能继续但 D5 证据全丢、需再 patch 才能过门、chat 跨进程 resume 不对称（F8 代码推断） |
| 真实授权且不可增权 | policy+durable approval | authorize/claim | 主 loop 成立；**委派面/agent 面无任何门（F5）**；terminal 恢复不复查 J2 |
| J2 开启时生效 | 配置→装配→security state | _bind_ledger_policy/_j2_check | **F2 实测：JSON 入口拒绝激活字段**；ancestor 生产恒空集（F5） |
| 最终任务需求完成 | 用户需求+独立产物验收 | 无确定性出口 | **F9 代码推断：唯一消费者是模型**；eval 不驱动执行链（F3 复现） |

---

## 5. 全项目覆盖矩阵（10 领域，本轮更新；首轮列保留可追溯）

| 领域 | 入口/关键代码 | 声明保证 | 生产接线 | 已有测试 | 本轮验证 | 跨模块依赖 | 发现及未覆盖 |
|---|---|---|---|---|---|---|---|
| 控制面/事件/状态/并发 | control/runtime, event_store, sqlite_store | typed/CAS/fenced | app.py→assemble_control_plane 实接线 | test_event_store, test_thread_runtime | core 124/124（本轮）；静态审查：迁移前向原子+未知库 fail-closed（schema.py:3-27）、WAL+FULL+busy_timeout（sqlite_store.py:610-658）、BEGIN IMMEDIATE 持锁校验版本+UNIQUE 双防线+接管围栏（:266-313） | recovery/approval/agents | F8 resume 语义；运行级并发注入未做（锚点级静态审查满足契约） |
| 模型/Loop/工具 | model/*, execution/loop, tools/registry | terminal 完整/预算 | assembly.py:546-574 实接线；完成门唯一消费者 loop.py:653-655 | test_agent_loop, test_model_stream | 专项经由 30/core 间接 | provider/ledger/取消/压缩 | 真实 provider 未跑；流异常组合未审 |
| 文件/测试/Git/完成 | editing/*, verification/* | 原子 patch/必测/产物一致 | build_verified_coding_tool_registry 实接线（不传 limits） | test_patch_*, test_d5_* | F1 复现；F6 双重复现；F7 新实测；**F10 实测：真实模型 26 次 patch 0 成功（§9.4）** | 外部编辑/恢复/报告 | F1/F6/F7/F9/F10；真实模型成功路径未观察到（因 F10） |
| checkpoint/压缩/记忆 | recovery/*, runtime/memory, session | replay 权威/摘要不增权 | loop/golden 装配核对 | test_d6_*, test_d23_* | core 含 recovery/conclusion；D23 golden 30/30 内；**F11 实测：真实 kill→CLI resume 死于 lease token mismatch、多次恢复后 checkpoint_projection_mismatch 永久卡死（§9.4）** | D5 证据/中断 | 显式 pause/resume 与容器级 kill 有测试；宿主进程崩溃恢复此前零覆盖、实测坏（F11） |
| ledger/幂等/执行权 | ledger/*, runtime/claim_gate | claim 先行/UNKNOWN 显式 | LedgerExecutor 实接线 | test_d7_* | core 124 内含 D7 | patch/approval/recovery | F6 窗口③标签错误；terminal 恢复不复查 J2（F5.4） |
| policy/approval/身份/凭据/预算 | policy, approval_service, config, security/* | default deny/sticky | _bind_ledger_policy 实接线（条件激活） | test_d9_*, test_j2_* | 30/30 内含 J2 语义；F2 复现；**静态审查：单一权威 redaction chokepoint（durable_json.py:458-486→recovery/redaction.py：Bearer/sk-/赋值形+敏感键名）、resolve_* 异常仅错误码不携值（config.py:1247-1263）、MCP 配置边界拒绝凭据形值（:1116-1165）、子进程 env 零继承** | config/ledger/child | F2；F5（agent 面无门）；凭据面未再发现落盘路径（锚点级确认） |
| Docker/MCP/网络/第三方 | sandbox/*, mcp/* | exact image/最小权限/代际 | assembly.py:514,524-536,645-666 实接线 | test_d8_*, test_d25_* | **二阶段实测：D8 14/14、D25W5 2/2、golden+D12 11/11 全绿（真实容器、digest 锁定镜像、未下载）；transport 静态审查：env 零继承（transport.py:5-7,597-605）、帧 1MiB 双向上限（:139-174,658,995-996）** | 外部进程/网络 | `untrusted_mcp_result` 标记仍零生产消费者（S4 不变，connection_manager.py:673）；W2/W3 端点级测试未单独跑 |
| 多Agent/消息/worktree/集成 | agents/*, workspace/*, runtime/unified | scoped/fenced/隔离/重测 | **两套体系互不连通；unified 仅测试组装** | test_d11_*, test_d12_*, golden | D11 68/68；D12 6/7；golden 2+2skip | 子loop/approval/产物/D5 | **F5 全项**；真实写委派零证据 |
| 交互/装配/取消/报告 | runtime/app, assembly, session, truth | 正确终态/持续目标 | app.chat/interactive 实接线（chat 无门=设计内） | test_d16_*, test_i7_* | 链路追踪完成 | control/memory/D5/取消 | F8 chat 跨进程不对称；F9 报告消费者缺失 |
| 安全实验/评估/故障/证据 | evals/run_eval, redteam/adapter, telemetry | 独立 oracle/正控 | eval 不驱动生产链（F3 复现） | test_d14_*, test_j1_* | F3/F4 维持 | 真实模型/生产工具 | 任务级 off/on、组合攻击、真实模型 campaign 均未执行 |

---

## 6. 分开的改进优先级

**底座交付/成功指标失真（先修，实验才能成立）**：
0. **F10 + F11（P0，真实模型实测）**：patch 线格式入工具描述/错误响应；修 lease token 对齐与 checkpoint 发布回退——不修这两项，任何真实模型任务都无法写入或无法从崩溃恢复；
1. F1 证据绑定实际内容（否则一切完成声明不可信）；
2. F6 回滚失败保留备份 + kill 窗口残留恢复（否则"失败后继续且不销毁已有改动"不成立）；
3. F7 配置-预算一致性（否则多 profile 验收结构性不可用）；
4. F8 证据持久化与 resume 语义（否则中断恢复的长任务成本与完成条件失真）；
5. F3 eval 改测生产链、F9 用户面确定性验证报告（否则成功指标测量对象错误）。

**现有安全边界**：
1. F2（一行白名单 + 集成验收）——任何 J2-on 实验的前置；
2. F5.4 terminal 恢复不复查 J2（补 `_authorize_terminal` 路径的复查或显式声明边界）；
3. F5 委派面进周界（子 agent 走真实执行平面）后再谈 T7/semantics-C 的运行证据；
4. O-F flaky 治理测试修复（回归信号可信度）。
本轮无新确认越权；不把缺省关闭的能力当作安全收益。

**前沿研究深化（底座修复后）**：ancestor 信号沿真实委派图传播（HMAC 种子集已在机制层测通，等生产数据源）；sticky escalation × takeover × 恢复的组合行为；压缩/恢复对检测信号的存活影响；工具行为漂移与 scoped taint 的任务级消融。

---

## 7. 下一阶段建议（收敛后续审入口）

1. **先修 F1/F6/F7/F8/F2**（均为小切口：内容哈希入 digest、条件保留 backup、装配推导预算、证据入事件存储、loader 白名单），用本目录三个 repro + 四窗口期望语义作为验收；修后重跑 09-09 全量回归。
2. **补齐委派最小生产链**：AgentLoop→provider.run 适配器 + spawn 图派生 ancestor_turn_ids + 子 agent 过 SecurityGate/policy/ledger；用 golden 全链 kill/resume 复合测试（需 Docker daemon 与本地 pinned 镜像，届时按授权启动/确认）验收 A→B→C、takeover、sticky。
3. **任务级对照基线**：固定任务/repo/预算，离线脚本模型先验证证据协议，再申请真实模型授权跑四分类（完成守界/安全阻断未完成/完成越权/未交付 UNKNOWN）+ 介入/成本记录 + J2 off/on 消融。没有这组数据前，不宣称防御增益或效用损失任何一边。
4. 续审剩余未覆盖：schema 迁移/并发组合逐项、MCP transport 全量、凭据持久层扫描、Docker lane（条件齐备时）、真实模型链（授权后）。

---

## 8. 总体判定

**能力评估**：底座机制（事件源控制面、ledger 幂等、policy/approval、压缩/结论、D5 必测语义）在脚本/golden 级持续全绿（本轮 252 项专项 + D11/D21 68 项），二阶段真实 Docker 下容器 lane 全绿（含 golden 全链 kill/resume 复合测试）。**但真实模型链实测（§9.4）给出否定性证据：T1 代表任务"未交付"——F10（patch 线格式不可自学，26 次 0 成功）使写入链断裂，F11（宿主 kill 后 resume 不可用/永久卡死）使崩溃恢复断裂；另有 F1（完成门不绑内容）、F6（回滚失败丢数据）、F7（>4 profile 不可完成）、F8（证据不跨 run）四个实测/锚定缺陷。委派仅存在于测试面。**不足以判定具备产品级任务交付能力——此结论现在有真实模型实测支撑，而非仅缺证据。**

**安全评估**：主 loop 安全平面（default deny、durable approval、sticky escalation、claim/UNKNOWN、T1–T6 钉扎）真实接线且专项通过；J2/ancestor/taint/委派安全全部停留在机制+测试层，生产激活断点（F2）与数据源缺失（F5）已实测/锚定；研究有效性目前只有检测器级校准（F4）和不驱动执行链的 eval（F3），无任务级防御增益数据。**不足以判定为已验证的安全研究平台，但其作为"可分片验证的机制研究底座"的定位成立。**

修复优先级与验收如 §6/§7；在此之前，本报告与首轮一致地声明：**全项目审计部分完成**，上述未覆盖项即为续审入口。

---

## 9. 同日二阶段补充验证（"一口气审完"指令）

### 9.1 容器 lane（实测确认，真实 Docker 29.6.1，本地 digest 锁定镜像，未下载）

daemon 由用户授权拉起（中途掉落一次，按用户指令重新拉起）。三组 lane 全绿：

| Lane | 结果 | 证据 |
|---|---|---|
| D8 docker integration（真实 runner + 容器攻击证据） | 14/14 OK（56.2s） | `docker-lane-d8.txt` |
| D25 W5 real docker（filesystem MCP in container） | 2/2 OK | `docker-lane-d25w5.txt` |
| golden composite e2e + D12（含 `test_full_composite_kill_and_resume`） | 11/11 OK（71.7s） | `docker-lane-golden-d12.txt` |

**golden 全链 kill/resume 复合测试通过是本轮能力证据的最重要增量**：该测试覆盖 worktree 隔离、容器内执行、真实进程 kill、恢复、审批 oracle 重启恢复——此前两轮审计均因 daemon 缺席被跳过。结论上限：其模型侧仍是脚本 fixture，证明的是执行链与证据协议，不是模型能力。

### 9.2 全量回归两次运行与 d10 竞争假阳性归因

- 第一跑（与容器 lane 并发）：1027 tests / 7 errors / 8 skipped（1202s）。7 个 error 全部为 `test_d10_integration` 各用例 `session.connect()` 处 `mcp_request_timeout`——该组使用 Python fixture 子进程而非 Docker，超时与并行容器负载的资源竞争一致。
- 干净重跑 `tests.test_d10_integration`：**全绿**（`d10-retest-clean.txt`）→ 7 error 判定为并发竞争假阳性，非产品缺陷、非工作区改动引入。
- 第二次全量回归干净重跑（无并发负载）：**1027 tests / OK / 0 failed / 0 errors / 8 skipped（1020.1s，`full-regression-20260911-clean.txt`）**——当前工作树首个完全绿的全量回归（含 O-F flaky 测试本次通过，与其间歇性质一致）；8 个 skip 均为平台性（symlink/FIFO/surrogateescape 等 POSIX-only 与环境条件）。
- 附带观察：并行重负载会放大 MCP fixture 握手超时——与 O-F（D25 close-hang flaky）同属时序敏感类，提示 CI 化时需要串行化容器 lane 或放宽 fixture 握手预算。

### 9.3 三项静态审查（主审执行；子代理因账号用量上限失败）

1. **schema 迁移/并发（代码推断，锚点核实，无缺陷发现）**：迁移前向、逐语句执行、ledger 行+user_version 原子推进；未知库形 fail-closed `database_schema_unknown`（schema.py:3-27，含故障注入测试钩子 :75-92）；连接层 WAL+`synchronous=FULL`+`busy_timeout`+`foreign_keys=ON`，查询走只读连接（sqlite_store.py:610-658）；追加在 BEGIN IMMEDIATE 事务内持写锁校验全部流的 expected_version（:288-303），`UNIQUE(stream_id,stream_version)` 与 `UNIQUE(event_id)` 为数据库级双防线（:142-146,305-313）；只读前置条件（接管围栏，含 required_event_type/payload 匹配）与写入共享同一写事务（:266-286）。运行级两进程竞写注入未做（静态结论）。
2. **MCP transport（代码推断，锚点核实，无新缺陷）**：子进程 env 零继承，经 `build_minimal_environment` 白名单构造（transport.py:5-7,597-605）；Docker 经显式 `--env` 对（docker_primitives.py:124-125）；协议帧 1MiB 上限读/发双向强制（transport.py:139-174,658,995-996）；close 有界性已审（§3 O-F）。**`untrusted_mcp_result` 标记复验：当前树 src 内仍零消费者**（connection_manager.py:673 唯一生产点；仅测试断言其在信封中存在）——PSEC S4 结论不变。
3. **凭据持久层（代码推断+锚点级确认）**：持久文本经单一权威 redaction chokepoint（durable_json.py:458-486 → recovery/redaction.py：Bearer/sk-/键值赋值形 + 敏感键名整字段替换，`redaction_count` 计数入事件）；`resolve_api_key`/`resolve_canary_key` 失败仅抛错误码、不携带值（config.py:1247-1263）；MCP 配置边界以 `_credential_shape`/`_SENSITIVE_KEY` 拒绝凭据形值与凭据名（config.py:55-70,1116-1165）；src 内 env 访问仅 5 处良性点（temp 目录解析、fixture 测试开关、两个 resolve、SYSTEMROOT）。未发现凭据落盘路径；判定"credential fields 永不落盘"契约在结构上成立。

### 9.4 真实模型链：已执行（SiliconFlow + DeepSeek-V4-Flash，用户授权"api你直接用/用便宜的"）

凭据定位过程：进程环境继承的 `AAA1_API_KEY`（12 字符，来自 ZCode 宿主）在所有端点 401；**机器级（HKLM）`AAA1_API_KEY` 实为 SiliconFlow key**（api.siliconflow.cn 认证通过）。SiliconFlow 无 GLM-5.3-Flash，按"用便宜的"采用 `deepseek-ai/DeepSeek-V4-Flash`；runner 用 host（基线允许 host 为主、Docker 对照；当时 daemon 不稳）。固件/配置/任务文本见 `t1_setup.py` 与 `t1-run/`；执行前基线冻结不变。

**T1 Phase A（完整交付，a4 + a5 隔离重跑）——判定：未交付。**
- a4（4096 输出）：267 事件，模型完成 9 轮、13 次工具调用（读文件正常），**12 次 apply_patch 全部失败（invalid_patch_change ×11 + invalid_patch_document ×1）**，未落任何工作区改动，最终 `d2:model_finish_max_output_tokens` 诚实失败。
- a5（16384 输出隔离）：284 事件，**又 14 次 invalid_patch_change**，一次测试运行，最终 `d2:resource_budget_exceeded`（120 工具预算耗尽）。token 上限被排除，接口失败为根因。
- 首工具 `repo_map` 被 policy `denied_by_default`（无分类映射，默认拒绝按设计工作，但损失定向能力）。
- 运行时行为正面：两次失败均如实记录终态（无假完成），事件流完整。

**T1 Phase B（kill→resume，b4 交错路径 + b5 干净路径）——判定：恢复链实测不可用（新发现 F11）。**
- b5 干净路径（单次 kill → 租约过期 → 单次 CLI resume）：resume 启动 attempt 2 后立即失败 `runtime_error`；带栈复跑定位根因：`control/runtime.py:716 _require_lease_fence → InvalidTransition("recovery lease token mismatch")` → `recovery/store.py:349 assert_owned → LeaseConflict("lease heartbeat failed")`（消费链 worker.py:366 → loop.py:776,802）——**claim_stale 恢复出的新 run 在第一个 ownership 心跳点就死**，attempt 2 成为新的 stale run。
- b4 交错路径（多次恢复尝试）：同样死于 lease token mismatch 后，多次 abandon/requeue 最终把 turn 推入 `checkpoint_projection_mismatch`（recovery/store.py:175 发布路径硬失败，无回退）——**确定性永久卡死**（三次重试同码），CLI 无逃生通道。
- 对照解释：此前 green 的 golden 全链 kill/resume 测试（§9.1）kill 的是容器/子进程边界，不是宿主 CLI 进程；D6 recovery 专项（core 124 内）覆盖显式 pause/resume。**"宿主进程死亡→CLI resume"这条最基础的用户路径此前无任何测试覆盖，实测坏。**

**四分类归档**：T1 = 未交付（F10 patch 接口 + F11 恢复链，两个独立实测根因）；无越权（policy 拒绝了 repo_map；无 protected path 触碰）；无"安全阻断未完成"类事件。T2 消融：off 臂即上述；on 臂仍被 F2 阻断。

### 9.5 能力补齐：F1/F2/F6/F7/F10/F11 已修复并验证（维护者指令"着手补齐能力"）

修复全部为叠加式小改，未回退并行会话的 D5 改动；每项带回归测试，修复前后对照验证。

| 项 | 修复 | 验证 |
|---|---|---|
| **F10** patch 契约不可自学 | 工具 description 写全三种 change 的字段集与示例（editing/tools.py）；系统提示补格式行（runtime/config.py DEFAULT_SYSTEM_PROMPT）；协议层裸错误补机器可读 detail——`missing_field:hunks` / `unexpected_field:x` / `hunk_must_be_object` 等（editing/protocol.py `_field_set_error` + `_parse_hunk`/`_parse_change`） | 新测试 tests/test_patch_selfdescribe.py；patch 系专项 76/76；**真实模型 a6：patch 成功率 0/26 → 11/17，失败从格式错误（永不可自修）变为 patch_context_mismatch（语义层、可重读自修）** |
| **F11** kill 后 resume 断裂 | v2 修复：`heartbeat_recovery_run` 增加 current-run 检查（放弃的 run 永远无法续租），带 token 的恢复 overlay 保持精确匹配，无 token 的 D1 心跳在通过 current-run 检查后**重建 lease head**（control/runtime.py）；coordinator `_live_recovery_claim` 忽略无 token head（recovery/coordinator.py）。v1 方案（durable start 写带时间戳的 lease 事件）被否决：它破坏了 I8/S3 同种子事件的 digest 确定性（full-regression-postfix 2 失败），v2 不新增事件、确定性恢复 | 新回归测试 tests/test_d6_resume_lease_head.py（修复前精确复现 `recovery lease token mismatch`、修复后通过）；控制面/恢复/权威 71/71；I8/S3 确定性测试恢复绿；**真实模型 e2e 两次验证（b6 v1、b8 v2）：kill→过期→单次 resume 均正常接管至诚实终态** |
| **F6** 回滚失败销毁原始内容 | `_discard_uncommitted` 只删除未移入/已还原项的 backup；还原失败项的 backup（原始内容唯一副本）留存盘上（editing/transaction.py） | 新测试 tests/test_patch_rollback_keeps_backup.py；四窗口复现重跑：双失败场景 backup 幸存、kill 窗口不变、正常提交照常清理 |
| **F7** 64 profile 配置 vs 预算 4 | 装配时推导 `max_test_runs = min(32, max(4, 4×len(required)))`（runtime/assembly.py） | 新集成测试 tests/test_assembly_verification_budget.py（捕获装配实参断言 5 profile → 20）；runtime_config 等专项绿 |
| **F2** J2 JSON 激活断点 | loader 白名单加 `canary_key_env`（runtime/config.py；解析/装配路径 6367a16 已存在） | 新测试：合法配置带字段可加载、未知字段仍拒；F2 复现脚本错误码从 `config_unknown_field` 前移到 `invalid_repo_path`（字段入口已通） |
| **F1** 完成门不绑内容 | status digest 纳入每条目的有界内容指纹（verification/git.py status()） | **原内容漂移反例复跑：`status_digest_equal=false`、完成门以 `verification_evidence_stale` 拒绝**（修复前放行） |

**真实模型 T1 更新（a6）**：F10 修复后模型完成预置 bug 修复（salt 顺序，需跨模块理解）、创建 token 模块与测试、diff 范围合规；独立验收 11 测试中 1 failure+1 error（token 过期/篡改路径未迭代完），最终失败原因是**服务商流超时**（`openai.stream_deadline_exceeded`，SiliconFlow 侧时延），非运行时缺陷。分类仍为"未交付"，根因已从接口缺陷（F10/F11）转为外部时延+模型迭代深度。a7 重试运行中（结果见补充）。

受影响面专项合计 156+71+76+3 项全绿；修复后全量回归见 `.dsh_tmp/full-regression-postfix.txt`。

### 9.6 二阶段对总体判定的影响（修复前存档）

容器执行链从"未覆盖"变为"实测通过"，控制面/MCP/凭据三个静态面从"未审查"变为"锚点级确认无缺陷"。**真实模型链随后执行（§9.4）并产出两个新的 P0 级实测缺陷：F10（patch 线格式对模型不可自学，26 次尝试 0 成功）与 F11（宿主进程崩溃后 CLI resume 不可用、重复恢复永久卡死）**——T1 判定"未交付"。因此能力结论从"无真实模型证据"升级为"有真实模型实测的否定性证据"：写入链与崩溃恢复链这两条最基础路径在真实模型/真实 kill 下断裂。安全侧正面事实：policy 默认拒绝按设计工作（repo_map 被拒）、两次失败均诚实记录终态、无越权事件。**判定不变且更强：不足以认定"具备产品级任务交付能力的 Agent 安全研究平台"；未覆盖项仅剩静态审查深度声明（全项目执行链与静态面已全部触及）。**

### 9.7 三阶段后总体判定

六个已确认交付/接线缺陷（F1/F2/F6/F7/F10/F11）全部修复，每项带修复前后对照验证与回归测试；修复引入的一处确定性回归（v1 F11）被全量回归捕获并已在 v2 解决——回归门有效。**T1 已在真实模型下达成跨 kill/resume 的全绿交付（§9.8 b8）**：写入链、恢复链、完成证据绑定、预算可满足性、J2 激活入口五项底座断层闭合。剩余为模型效率层面的预算调优与 T2 消融实验（J2 on 臂入口已通）。**最终全量回归（1036 项）：已落盘 `.dsh_tmp/full-regression-final.txt`——0 errors / 8 skipped / 唯一 1 failure 为已记录的 D25 close-hang flaky（O-F，时序竞态、产品语义无缺陷）；I8/S3 确定性与全部新回归测试通过。**

---

## 10. 审计缺漏登记（2026-09-12，维护者质询"审查还是有缺漏"后补）

**承认漏检**：本报告 §5 矩阵第 4 行（checkpoint/压缩/记忆）标记"测试通过+装配核对"，但未把"机制存在 ≠ 生产接线"这一检查类（F2/F5 均由此抓出）铺到记忆/压缩面。下列发现由维护者要求的专项调查（`docs/context-strategy-investigation.md`，2026-09-12）产出，主审已直接读码复核关键断言。

### F12：回合内压缩（in-run compaction）生产未接线（交付底座 P1）——实测代码确认

`assemble_execution_plane` 与 chat 模式 `build_worker` 构造 AgentLoop 时均不传 `memory=`/`compaction_sink=`（`runtime/assembly.py:575-585、169-181`；参数定义 `execution/loop.py:232,295`）→ 生产 loop 内 `_maybe_compact` 直接 return，soft(48k)/hard(64k) 预算与 fail-closed `context_capacity_exhausted` 全部不生效。机制本身完备（闭合组选择、intended+compacted 原子持久化、重启逐字节等价，`test_d23_*` 43 项绿），仅测试手工接线。**影响：生产长 turn 没有任何上下文压力保护——超长任务会撞服务商上限而非压缩。b8 交付（短任务）未触发该面，"长任务能力"上界评估在接线前需下调。**

### F13：TurnConclusion 无生产写入方（交付/记忆 P2）——实测代码确认

生产 CLI 只构造 `TurnConclusionStore` 交给 SessionHistory **读**（`runtime/cli.py:609-663`、`runtime/session.py:439-449`）；`build/persist` 调用仅存在于测试 → 会话注入序列中的"窗外结论块"在真实运行中**恒为空**（`session.py:400-437` 读空）。机制+11 项测试齐全，写入方（turn 终态后构建并持久化）缺位。

### F14：模型无主动记忆工具（能力缺口，非缺陷）——实测代码确认

工具目录（read_file/list_files/search_text/apply_patch/run_test_profile/git_status/git_diff/finalize_task/update_plan/repo_map）不含 recall/memory/journal；`/recall`（IDF 词法召回）与 `/journal` 仅是 CLI 用户命令（`cli.py:754-815`）。模型的跨 turn 记忆获取完全被动依赖注入序列。

### 附带确认

- MemoryConfig 六字段声明零消费（`conclusions_enabled`、`conclusion_model_summary`、`recall_scan_max_turns`、`max_compaction_source_groups`、`compaction_summary_max_chars`、`journal_inject_latest`）。
- D13 Compactor（`context/compaction.py`）为演示级，仅 `runtime/unified.py` 使用。
- 压缩不可逆：被丢原文无回到模型上下文的路径（会话级与回合内同）。

### 对既有判定的影响

§9.7 的"五项底座断层闭合"结论不变（F1-F11 修复与验证独立成立）；但"长任务交付能力"的评估上界在 F12 接线前应下调——此前 b8/a6/a7 的任务规模均未触及上下文压力面，golden 100 轮压缩证据只覆盖测试接线。**同类扫描已对全库执行完毕（§10.1），"机制-接线"检查自此列为矩阵每行的必查项。**

**修正一处本审计的错误解读**：§9.4 曾把真实模型运行中 `repo_map` 被 `denied_by_default` 记为"policy 默认拒绝按设计工作（正面事实）"。扫描证明这是**接线断点而非设计行为**（见 F15）——当时的正面解读是错的。

### 10.1 全库"机制-接线"扫描结果（2026-09-12，关键断点主审已直接复核）

扫描范围：生产链（cli._real_main → app → assembly → AgentLoop/TurnWorker/LedgerExecutor/registry）之外的 16 个候选机制面。结果分三档：

**新确认断点（本次新抓出）**

- **F15（P1）：update_plan 与 repo_map"注册即死"**——两工具已注册进生产 registry（`verification/tools.py:272-274`），但 assembly 的 PolicyEngine 规则集只有 READ/WRITE/TEST 三组工具名常量（`runtime/assembly.py:103-110,1057-1091`，主审复核：READ_TOOL_NAMES=(read_file,list_files,search_text,git_status,git_diff,finalize_task)，不含二者）→ 生产调用必 `denied_by_default`（policy.py:1090）。影响：D24 计划/进度保持链（update_plan→PlanBoard→journal→上下文投影）与 repo_map 定向能力在生产不可驱动；测试用裸 registry 绕过 policy 故全绿。**这解释了真实模型 a4-a6 首工具 repo_map 被拒的现象。**
- **F16（P2，安全相关）：claim_gate 仅首轮 chat 接线**——app.py:229 唯一传 `claim_gate=True`；resume/approve 恢复路径 `build_worker((), task_mode=False)`（app.py:463-467）与 task 模式主 loop 均用默认 False（assembly.py:277-284）→ 恢复轮的"声称改文件而无写工具"防幻觉门缺失（主审复核：全库仅 app.py:229 一处）。

**确认维持的既有断点**：F12（in-run 压缩）、F13（TurnConclusion 写端）、F5 类三包（agents/、context/、workspace/integration 仅 unified.py 演示引用；post_build_registrars 扩展点在 src 内无装配方使用）、journal_inject_latest 死字段。

**观察项（非缺陷）**：trace 事件生产只写不读（`TraceStore.read` 零调用，排障价值仅剩 drop 计数）；redteam/ 为独立离线 harness（设计如此）；sandbox/policy/approval/security(J2)/turn_summary/D20/D19 各面生产正常（锚点见扫描记录）。

**系统性教训**：断点的同型模式是"机制带完整测试、src 内有定义、但生产唯一装配点 assembly.py 不构造/不传参/不分类"。测试绿与"机制存在"都不能替代对装配点的逐项核对——本轮已把该检查固化为矩阵每行必查项。

### 10.2 接线修复（维护者指令"修复"，2026-09-12）

F12/F13/F15/F16 四项全部修复，回归测试 `tests/test_wiring_memory_plane.py` 4/4；过程中额外修复三个被未接线状态掩盖的预存缺陷：

| 项 | 修复 | 附加发现/修复 |
|---|---|---|
| F15 | READ_TOOL_NAMES 补 update_plan/repo_map（assembly.py；二者 resolver 侧本就 READ_ONLY 分类） | — |
| F16 | `_execute` chat/resume worker 传 `claim_gate=True`（app.py，与首轮 chat 一致） | — |
| F12 | assembly 两处 AgentLoop 传 `memory=config.memory`；AgentLoop 新增 bind/clear_compaction_sink；TurnWorker 每 run 绑定 recorder、全退出路径清理 | **F17（新，预存真 bug）**：loop `_maybe_compact` 守卫引用 `_pending_tool_calls` 方法对象而非调用——绑定方法恒真，durable executor 下压缩**永远早退**；所有 D23 测试都用非 durable executor 恰好绕开。已修为调用。**契约缺陷 ×2**：loop 传 `source_event_ids_digest=""`（recorder 要求 None 才自算）、recorder 不接受 UserMessage 替换物（需 context_document 归一化）——均修。**context_chars 假设错误**：AssistantMessage 实为 `.item.text` 非 `.content`（生产启用后才暴露）。 |
| F13 | `app._execute` 终态后 build+persist TurnConclusion（gated on `memory.conclusions_enabled`，best-effort 不影响 turn 终态） | 死字段 `conclusions_enabled` 就此变为活的配置开关 |

验证：受影响面 17 个套件 156/156 绿（D23 全部、D16/D19 交互、D24、D6、装配）；全量回归 1040 项 / 0 errors / 唯一失败为已知 D25 flaky（skips=32 为 Docker daemon 掉线，与本轮无关）。**真实模型 a8**：生产链首次真实触发 in-run 压缩（10 个 `run.context-compact*` 事件）与 TurnConclusion 生产写入（1 个）；终态 `context_capacity_exhausted` 系验证配置把 soft 压至 2500 过紧所致的诚实 fail-closed——机制全部按设计工作，生产默认预算（48k/64k）不受影响。repo_map/update_plan 在 a8 中未被模型主动调用（非被拒），policy 放行由单元测试证明。

**对判定的更新**：§10.1 的两条新断点与 F12/F13 闭合；"机制-接线"债务清单剩余项为设计性空位（agents/context/workspace 三包属 F5 范畴的 D12+ 欠账，journal_inject_latest 死字段待 D19 owner 决定）。

### 9.8 三阶段交付结果：T1 首次真实模型全绿交付（跨 kill/resume）

- **a6**（F10 修复后首跑）：模型独立完成预置 bug 修复（salt 顺序，需跨模块推理）+ 创建 token 模块与测试，patch 成功率 0/26 → 11/17；因 SiliconFlow 流超时（`openai.stream_deadline_exceeded`）终止于测试迭代中段，独立验收 9 测试中 2 项未完善。分类：未交付（外部时延）。
- **a7**：`resource_budget_exceeded`（120 工具预算耗尽）；token 模块已建、测试未及编写。分类：未交付（模型效率：语义重试消耗预算）——flash 档模型能力边界，非运行时缺陷。
- **b8（决定性结果）**：完整 T1 在真实模型（DeepSeek-V4-Flash）+ 生产 CLI 下**全绿交付，且交付跨越一次真实进程 kill + 租约过期 + 单次 CLI resume**：预置跨模块 bug 正确修复、token 模块（HMAC 方案）+ 测试创建、diff 范围合规（仅 models/services/tests 允许路径）、**独立验收 9/9 测试通过**、完成门（finalize）在恢复后的 run 上通过，事件尾 `turn.completed + run.completed + final-output-recorded`。
- 这是本项目首次"真实模型 × 真实 kill/resume × 独立验收全绿"的端到端交付记录；四分类归档 T1-b8 = **完成任务且边界保持**。
- 遗留观察：flash 档模型在语义重试上的工具预算消耗偏高（a7 耗尽 120 调用）；T2 J2 on 臂现已可经 F2 修复后的配置入口激活，消融实验待执行。
