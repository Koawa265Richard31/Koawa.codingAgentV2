# WP-1 实施记录：通用结果身份与可见等级（tool/MCP 面）

日期：2026-09-20。基线：`bc75026` 之后的 E 组工作分支。

## 已实现

### 1. 策略单一权威扩展（verification/output_policy.py）

- 新增 `MCP_POLICY_VERSION = "mcp-output-policy-v1"` 与
  `MCP_BODY_VISIBILITY = "metadata_only"`：白名单成员资格只证明**来源**，
  不证明**正文**——未知正文在已验证的安全投影适配器（WP-C/D）落地前，
  一律只给元数据。与测试面（`POLICY_VERSION`/`BODY_VISIBILITY`）并列，
  构成按来源分类的可见等级策略族的头两块。

### 2. MCP 回执元数据-only 门禁（mcp/connection_manager.py bind_tool_handler）

- envelope 不再携带正文：`result: None`，代以
  `result_visibility: "metadata_only"`、`output_policy` 版本、
  `body_bytes`（脱敏后字节数，运维面可见）。
- `redact_text` 仍先于字节计数执行（模式脱敏为运维侧最小防线）。
- `untrusted_mcp_result: true` 保留——下游所有 MCP 派生字节的不可信标记。
- 注入姿态升级：prompt-injection 文本（"ignore previous"）从
  "带 untrusted 标记进模型" 变为 "根本不达模型"。

### 3. 断言契约更新

- tests/test_d10_integration.py、test_d10_connection_binding.py、
  test_d25_g3_g4_governance.py、test_d25_g4_e2e.py：
  正文断言改为元数据-only 断言（含注入文本"不达模型"的更强断言）。

## 验证

- d10 x2 + d25 g3/g4 + output gate：48 项 OK（含 2 个 Docker skip）。
- 全量回归：full-regression-wp1.txt（后台记录）。

## 显式未完成（后续包）

- WP-D：result.projection-published.v1 事件族与故障注入矩阵（投影持久化
  之后检索才有正文来源；当前 MCP/测试回执的正文在 durable fact 中，检索
  只暴露元数据）。
- WP-C：安全诊断环境（隔离测试工作区）——落地后测试正文可见性才可升级。
- WP-G：固定产物外发。
