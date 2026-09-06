# S2 切片策略文档：MCP 工具面固化语义验证（rug-pull 防护）

PSEC 轨道。规划：`docs/agent-security-platform-track-plan.md` v1.0。日期：2026-09-06。基线：`PSEC_BASE_COMMIT = e213580`。**本切片无代码变更、无新增测试**——语义矩阵全格已有代码证据或既有测试钉定，未发现缺陷（规划 §4 S2 步骤②的缺陷固化条款未触发）。

## 0. 结论

Koawa 的工具面在**会话生命周期内结构性钉定**：生产装配两处 `McpSession` 构造均未传 `auto_refresh`（默认 False），`tools/list_changed` 通知只置 advisory 旗标、不换目录；显式 `refresh()` 在生产代码中零调用方；即使 refresh 发生，新目录必须整本通过 `bind_catalog` 重验证，且换代即以 `mcp_binding_stale` 作废全部旧 binding——审批过的 handler 不可能跨代使用。对照审计文档 §7 点名的 OpenHands 取舍（接受运行中 `tools/list_changed` 刷新/替换/删除工具），Koawa 的取舍更严格且与自身威胁模型一致（T3 binding 时拒绝毒化声明）。声明面之外的**行为漂移**无协议级检测手段，属 MCP 协议本身的限制，declared。

## 1. 语义矩阵（每格：预期 = 防 rug-pull 所需语义；实际 = 代码证据）

| 路径 | 预期 | 实际 | 证据 | 判定 |
|---|---|---|---|---|
| 初次 binding | 工具面验证后钉定；非法形状 fail-closed | `connect()` → `_list_tools(1)` → `bind_catalog`：严格 schema 子集（子集外形态 `unsupported_mcp_schema`）、registry 名命名空间化 `server__tool`、重名拒绝、**任一工具非法→整目录拒绝**；allowlist 在验证前过滤（子集外工具"downstream 不存在"，调用拒绝 `mcp_binding_required`） | `connection_manager.py:235-276,482-529`；`tool_binding.py:443-470`；测试 `test_d10_connection_binding.py::test_valid_catalog_builds_typed_specs / test_invalid_tools_are_rejected / test_duplicate_and_namespace_conflicts`；`test_d21_agent_security.py::test_t3_mcp_poisoning_rejected_at_binding_time` | 闭合 |
| 运行中 `tools/list_changed` | 不得静默替换已审批工具面 | 通知 → 节流 → `pending_refresh=True`；`auto_refresh` 生产未传（默认 **False**，`connection_manager.py:114`；装配点 `assembly.py:742-760,864-881` 无此参）→ **catalog 不变**；`pending_refresh` 无生产消费者（见 §2 观察 1） | `connection_manager.py:571-577`；测试 `test_auto_refresh_on_list_changed`（钉住 opt-in 路径） | 闭合（更严：默认完全不响应） |
| 显式 refresh | 新目录必须整本重验证；换代作废旧授权 | `refresh()` → `_list_tools(gen+1)` → **同一 `bind_catalog` 全量重验证**（含 allowlist + launch identity digest）→ 成功才单引用切换目录、generation+1；失败保持旧目录；此后旧 binding 一律 `mcp_binding_stale`（`call()` 校验 server_id + session_generation） | `connection_manager.py:288-313,324-328,482-529`；测试 `test_stale_binding_after_refresh` | 闭合 |
| server 崩溃 / transport 死亡 | 不得在旧目录上静默复活 | 通知循环读错误 → fail 全部 pending → session FAILED；**无自动重连**；恢复 = 重新装配（新 session 实例、新 catalog、新 registry）；binding 校验含 session_generation，session 实例另有 `session_instance_id` fence（§8.8） | `connection_manager.py:562-570,593-599`；`:179,201`（instance id）；`:324-328` | 闭合 |
| 沙箱 reconcile（D25） | 崩溃窗口只按持久事实裁决 | 双账本聚合 ID（MCP allocation = sandbox allocation）；恢复只基于持久事实 + Docker inspect 证据；无法证明 → UNKNOWN，绝不猜测修复 | `sandbox_reconcile.py:1-8` 模块合同；D25 W3 | 闭合 |
| 不确定结果 | 不得盲目重试（server 可能已生效） | call 超时 → `uncertain=True` → handler 抛 `McpOutcomeUncertain`（"Runtime must not retry blindly"）；connect 失败 → `record_outcome_unknown` 入档 | `connection_manager.py:64-65,365-366,669-670`；`assembly.py:876-884`；测试 `test_call_timeout_is_uncertain / test_outcome_uncertain_raises` | 闭合 |
| 行为漂移（声明不变、行为变） | 协议级检测 | 不存在：binding digest 钉定的是声明面（名字/schema/描述），MCP 无行为 attest 机制，所有 client 同此限制 | 协议事实；declared 边界 | N-A(声明) |

补充钉定：binding 参与 action digest（审批↔调用参数一致性链），`test_binding_enters_action_digest`；MCP 结果出站前包 `untrusted_mcp_result: True` 信封 + 脱敏，`test_tool_handler_wraps_untrusted_output`（`connection_manager.py:656-687`）——此即 S4 所研究的"标记"的生产产生点。

## 2. 观察（非缺陷，登记备查）

1. **`pending_refresh` 旗标无生产消费者**：旗标被设置后，runtime/policy/executor 均不读取（全仓库 grep 仅 connection_manager 内部）。失败方向安全（不消费 = 目录保持钉定），但"server 自报变更"这一信号目前对策略层不可见。若未来要做"变更后强制重新审批"的显式语义（而非现状的换代失效），此旗标是天然挂点——登记为 C 级设计观察，与 S5 的信号语义设计同源。
2. **`refresh()` 生产不可达**：生产装配零调用方，refresh 语义目前只被测试钉定。这使"换代失效"防线处于纵深位置（即使未来某处误开 auto_refresh，`bind_catalog` 重验证 + `mcp_binding_stale` 仍然生效）。

## 3. 与产品基线对照

审计文档 §7 已核实：OpenHands 收到 `tools/list_changed` 后**刷新、增加、替换和删除运行中工具**（产品取舍，服务渐进式 MCP 发现）。Koawa 取反：目录按代钉定、变更只留旗标、任何换代都要整本重验证并作废旧授权。两者都是合法取舍，但 Koawa 的选择与"配置即信任 + T3 binding 时拒绝毒化"的既有威胁模型自洽，且审批语义（binding 进入 action digest）依赖代际稳定性——OpenHands 式动态替换会破坏该链。

## 4. 无代码变更声明

矩阵 7 格中 6 格闭合、1 格 N-A(声明)，全部有代码证据或既有测试名。规划 §4 S2 步骤②（expectedFailure 固化缺陷）未触发：未发现"运行中可静默替换工具面"的路径。§2 两项观察均为 C 级设计项，不构成本轮代码准入。

## 5. 诚实边界

- 矩阵基于 2026-09-06 基线 e213580 的代码阅读与既有测试名引用；测试未重跑（其所属套件在本轨道收尾时统一回归）；
- "闭合"绑定当前装配方式：若未来生产装配传入 `auto_refresh=True` 或新增 refresh 调用方，Row 2/3 的判定须按"换代重验证 + 旧授权作废"语义重新复核（该语义本身已被测试钉住）；
- 行为漂移（Row 7）是 MCP 协议级限制，本切片不声称对 server 实际行为有任何检测能力；
- 材料钉定：MCP 规范 tools/list_changed 语义经 `mcp/protocol.py:TOOLS_LIST_CHANGED_NOTIFICATION` 与 spec 2025-06-18 版核对（访问 2026-09-06）。
