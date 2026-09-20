# DS-V4-Flash 只读审查报告（2026-09-19，经生产 CLI 交付）

> 原样提取自 turn.completed 的 summary 字段（7300 字符），未经编辑。

已获得足够证据。关键：test_unified_runtime.py 注释明确承认"Full assemble_runtime() ... cannot currently reach AgentLoop (CompositeToolRegistry is not a CompletionGate)"——这是一个生产装配未完全接线环路的证据。我现在有足够信息输出最终报告。

让我作最后一次补证：确认 tests 走完整 ledger 生产路径（而非绕过 ledger/policy）的连接覆盖，读 test_d10_integration.py 顶层以确认 ledger 绑定测试结构。

我已经收集了足够证据可以输出最终报告。总结发现并分级。

## 最终审查报告

### 审查范围与方法
基于 `repo_map` 定向后，用 `read_file` 深读 `runtime/assembly.py`（1208 行全读：控制面装配、执行面装配、`_bind_ledger_policy`、MCP 接线、命令 runner、关闭链），并用 `search_text` 统计 tests 对生产装配入口与验证注册入口的引用。由于我的只读工具沙盒拒绝访问 `src/koawa_agent_v2/execution/` 子目录（`read_file` 对这些路径返回路径校验失败），执行链 `loop.py`/`worker.py` 的**行级内容**我未能读到——这点必须诚实标注（见文末）。

---

### 总体结论（先给出判断）
生产装配 `runtime/assembly.py` 的真实接线程度**非常高**：`docstring` 声称的「policy/ledger/recovery 单链」（D3+D4+D5 注册 → D9 policy 门 → D7 ledger 执行器 → D2 loop → D6 worker → recovery 协调器）在代码层面**基本全部真实接线**，并非「仅注册未启用」。发现的问题集中在**装配完整链路存在一个已知断裂点**、以及 **tests 覆盖大量绕过 policy/ledger 的注册直连路径**。

---

### P0 —— 阻塞/必须修复

**[P0-1] 生产装配无法把 CompositeToolRegistry 作为 CompletionGate 接通 loop —— 执行链最终一环未闭合**
位置：`src/koawa_agent_v2/runtime/assembly.py:592-604`（`assemble_execution_plane` 中 `AgentLoop(..., completion_gate=registry, ...)`）；并经 `tests/test_unified_runtime.py:237-245` 注释确认。
问题一句话：`assemble_execution_plane` 把 `completion_gate=registry` 传入 `AgentLoop`，但当存在 MCP 会话时 registry 是 `CompositeToolRegistry`，而后者**不是** `CompletionGate`（完成门协议）。
为什么重要：注释原文「Full assemble_runtime() with configured MCP servers cannot currently reach AgentLoop (CompositeToolRegistry is not a CompletionGate)」。也就是说**只要配置了 MCP 服务器（生产常见场景），装配出来的 runtime 在实际跑 turn 时会在进入 AgentLoop 前卡住/无法完成门校验**，所谓「production single chain」在 MCP 场景下实际断链；测试也绕开了这一点，未覆盖到真实缺口。
影响：任务调用在 MCP 部署下会失败（无法通过 completion gate 类型检查或运行期完成判定），等于生产装配只有无 MCP 时可用——与「one real chain」的声称不符。
最小改进建议：为 `CompositeToolRegistry` 实现/委托 `CompletionGate` 协议（或让 `assemble_execution_plane` 在 MCP 存在时构造一个可识别完成状态的 gate），并补一个「assemble_runtime 带 MCP 后真正跑通 AgentLoop turn」的端到端测试。
标注：[确认]（assembly.py 代码 + test_unified_runtime.py 注释双重证实）

---

### P1 —— 重要

**[P1-1] 验证注册器的 tests 大量绕过生产 ledger/policy 直接构造验证注册器 —— 覆盖路径与生产不一致**
位置：`tests/test_d5_required_profiles.py:126,272,302,313`、`test_d5_vertical_slice.py:318,360,388,465,514`、`test_d8_verification_integration.py:169,193,213`、`test_d24_repo_map.py:128`、`test_d24_plan_wiring.py:153`。
问题一句话：这些测试都直接 `from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry(...)` 构造注册器，未经过 `assemble_execution_plane`/`_bind_ledger_policy` 的 D9 policy 门与 D7 ledger 封装。
为什么重要：`build_verified_coding_tool_registry` 虽是生产模块（在 assembly 中也被调用），但**以裸构造方式绕过 ledger/policy 层**，只验证注册/验证预算逻辑，测不到「policy 是否真的拦下拒绝路径、ledger 是否真的按精确版本限制写」这两条生产关键链。
影响：verification 层的单测通过不代表生产装配里的 policy/ledger 门真实生效；存在测试全绿但生产被绕过门放行的风险盲区。
最小改进建议：对这些测试，要么走 `assemble_execution_plane`/`_bind_ledger_policy` 注入验证注册器，要么新增至少一个端到端测试覆盖「通过完整生产装配 → 验证注册器处于 ledger/policy 之后」的路径，并把现有裸构造测试标注为「组件级、非装配级」。
标注：[确认]（search_text 命中直接 import 与直接构造）

**[P1-2] 生产装配关闭链中的 sandbox 分配释放存在"静默吞异常"分支，可能遗留孤儿分配**
位置：`src/koawa_agent_v2/runtime/assembly.py:886-899`（`connect_timeout` 块 `_allocation_reporter` 的 `except Exception: pass`）及 `_allocation_reporter` 内 `stop` 分支的 `except Exception: release_failed`。
问题一句话：`_allocation_reporter` 顶层 `except Exception: pass` 会把任何 release 期间异常静默吞掉，导致 sandbox 分配可能停留在 STARTED 而"看起来关闭了"。
为什么重要：代码本身注释说明设计意图是「uncertain close 故意留给 DualLedgerReconciler」，但外层 `pass` 会丢弃该语义（无法区分 expected-uncertain 与真正的 bug）；一旦 release 抛非目标异常，清理证据链断裂，崩溃恢复链无法感知残留分配。
影响：崩溃恢复 `recovery/coordinator.py` 依赖 ledger 显示 sandbox 分配状态以决定是否 `reconcile_pending`；静默吞掉 release 失败后协调器会误判环境已清理。
最小改进建议：把 `_allocation_reporter` 中真正的异常（区别于 `sandbox_release_failed` 预期分支）至少 `logger` 记录并保留 `outcome_unknown` 传播，而不是裸 `pass`；为「release 抛非预期异常 → 分配仍标记 STARTED」补一条测试。
标注：[确认]（`_allocation_reporter` 顶层 `except Exception: pass` / `_connect_activated_server` 的 `except Exception: pass` 代码）

**[P1-3] READ_ONLY 分类将 `run_test_profile` / MCP 只读服务器也归入 READ_ONLY profile —— 恢复 profile 与 policy 的 side-effect 分类各自独立，可能不一致**
位置：`src/koawa_agent_v2/runtime/assembly.py:1070-1082`（`resolve_tool`）与 `1170-1184`（`profiles` 映射）。
问题一句话：`resolve_tool` 对非 `apply_patch`/`run_test_profile` 的所有 builtin tool 统一给 READ_ONLY，而 ledger profile 对 `run_test_profile` 归 `READ_ONLY_PROFILE`；两套分类由配置侧 `side_effect_class` 与代码硬编码分别决定，未强制一致。
为什么重要：recovery 的「不确定副作用需要 D7 ledger 或 operator」判断（`recovery/coordinator.py` `claim_stale` 的 `uncertain_phase`）依赖 ledger profile 的 RecoveryMode/SideEffectClass；若 policy 侧与 ledger 侧分类语义漂移，崩溃恢复会误判某工具是否"可能已产生副作用"。
影响：`run_test_profile` 被 policy 归 READ_ONLY（sandbox-test），但 test 实际可能写文件；若 ledger 侧也按 READ_ONLY 处理，崩溃恢复链无法把测试残留标记为不确定。
最小改进建议：让 `profiles` 与 `resolve_tool` 共用**同一份** side-effect 来源（如统一由 `config.test_profiles`/`side_effect_class` 派生），消除双份硬编码映射；并加断言测试保证 policy 分类与 ledger 分类一致。
标注：[推测]（读到两处映射，但未读到 ledger.protocol 与 policy 的完整对照，未完全证实语义漂移）

---

### P2 —— 次要

**[P2-1] 完成门/claim_gate 在 chat 路径（task_mode=False）被显式关闭，存在双路径行为分叉**
位置：`src/koawa_agent_v2/runtime/assembly.py:41-52`（`AssembledRuntime.build_worker` 中 `task_mode=False` 时 `completion_gate=None`）。
问题一句话：非任务模式（对话）移除 completion gate 与 claim gate，与任务模式的 `claim_gate=True` 形成双路径行为分叉。
为什么重要：`task_mode=False` 的注释说明是「conversational turns may stop without finalize evidence」，意图合理，但该 `build_worker` 分支在 `elif claim_gate:` 的审计注释（S6/DS）只校验不启用，逻辑略绕。
影响：若某个上层入口误传 `task_mode=False` 给真实任务，会静默去掉完成门与反幻觉门，绕过 D5/D22 证据链。
最小改进建议：把 `build_worker` 的 `if not task_mode:` 与 `elif claim_gate:` 两条路径注释/分支整理清晰，并在文档中强制「任务执行必须 task_mode=True」。
标注：[确认]（代码显式分支）

**[P2-2] MCP legacy（None profile）服务器在测试被标记 legacy_fixture 才能继续 —— 生产路径对 legacy 强迁移，但测试仍在用 legacy**
位置：`tests/test_unified_runtime.py:203-224`（`_runtime_config` 用 `McpServerConfig` 无 profile → legacy fixture + `spawn_fixture_command`）；对应 assembly `_process_start_decision` legacy 分支 `legacy_fixture` 检查（`assembly.py:441-450`）。
问题一句话：生产对 legacy 服务器要求显式迁移标记（`mcp_legacy_profile_requires_migration`），但大量 e2e 测试仍以 legacy fixture 模式连接。
为什么重要：测试走的 legacy 决策分支（无条件 allow）在真实生产被禁止，说明 e2e 测试并未证明「生产会拒绝 legacy」，反而证明的是「测试夹具允许 legacy」。
影响：对 legacy 策略「应拒绝」的断言缺少一个走生产 `_process_start_decision` 且不带 `legacy_fixture` 的负向测试。
最小改进建议：加一个断言「legacy_fixture=False 的 legacy server 触发 `mcp_legacy_profile_requires_migration`」的装配级测试。
标注：[确认]

---

### 未能检查到的方面（必须诚实声明）
1. **`execution/loop.py` 与 `execution/worker.py` 的行级内容**：我的只读工具沙盒对 `src/koawa_agent_v2/execution/*` 路径返回路径校验失败（`read_file` 拒绝），因此执行链的**内部细节**（AgentLoop turn 循环、checkpoint 持久化时机、claim_gate/completion_gate 的运行时判定、worker 如何绑定 loop）**未能逐行核实**。我只通过 assembly.py 的 import 与调用点间接确认这些模块被生产接线，以及它们与 `completion_gate`/`claim_gate`/`memory`/`trace_sink` 的传入关系。
2. **`verification/finalization.py`（VerificationLedger）、`verification/tools.py`（D5ToolLimits/run_test_profile）、`recovery/store.py`（CheckpointStore）、`recovery/execution.py` 的内部实现细节**：未逐行读取，故完成证据链（evidence digest、测试预留/记录、状态转移）与崩溃恢复链（checkpoint 覆盖、lease 心跳）的**正确性**仅能推断，未完全证实。
3. **`ledger/protocol.py` 的 RecoveryMode/SideEffectClass 定义与 policy 分类的完整对照**：仅读到片段，P1-3 的「语义漂移」为推测。
4. **tests 中是否还有走 `_bind_ledger_policy`/`LedgerExecutor` 生产链路的直接覆盖：** 我确认 `tests/test_d10_integration.py`、`test_d11_*`（scheduler/process_kill/agent_control）存在且主题涉及 ledger，但未逐文件核实它们用的是「生产 `_bind_ledger_policy` 组装」还是「自定义 executor 模拟」，故 tests 对 ledger 生产装配路径的覆盖程度未完全证实。

工具环境注：git_status 显示工作区 clean，无 parallel 会话的未提交改动与我从仓库外写入（无 diff）。本次审查全程只读，未调用 finalize_task，未运行测试。
