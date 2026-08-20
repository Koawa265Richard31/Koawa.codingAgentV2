# D10：MCP Lifecycle 与安全工具调用

## 1. ADR 与完成边界

### ADR：MCP 规范与传输选择

- 固定 MCP Core 规范版本 **2025-06-18**（`MCP_PROTOCOL_VERSION`）。
- 固定传输：**stdio + JSON-RPC 2.0 + LSP Content-Length 帧**；只读官方规范行为，
  不依赖网络。
- 固定官方 Python SDK：**本日不引入**。项目保持标准库-only，协议层自行实现并
  用真实 fixture server 验收；引入 SDK 需要未来切片显式批准。
- **Streamable HTTP / SSE transport 是本日显式非目标**，不声称“支持所有 MCP”。

### 完成边界

D10 把真实 MCP server 作为动态工具来源接入既有
`Schema/Registry → Policy/Approval → D7 Ledger → 传输` 链，而不是写一个叫 MCP
的普通函数。

已实现：

- initialize/initialized 握手、协议版本精确匹配、tools/list 分页（cursor）、
  tools/call、list_changed 通知与 catalog refresh；
- 每个 binding 固定 `(server_id, session_generation, tool_name, schema_hash)`，
  该元组进入 D9 action digest 与 D7 logical execution identity；
- schema 进入 Registry 前严格校验（受限 JSON Schema 子集 + 动态 frozen dataclass）；
- 输出视为不可信数据：结果有界、redact 脱敏、JSON 信封标记
  `untrusted_mcp_result`；
- 超时/错误 id/EOF/畸形帧等不确定路径在非幂等 profile 下进入
  `OUTCOME_UNKNOWN`，不做盲重试；
- 所有 MCP 调用必须走 LedgerExecutor 的 authorize→claim→one-shot gate，没有
  直连旁路。

尚未开放：

- 真实外网 MCP transport、HTTP/SSE、OAuth/凭据保管库；
- 任意 JSON Schema（`$ref`、composition、oneOf 等）——bind 阶段 fail closed；
- 容器内 MCP server 运行（D8 容器集成留给 D12）；
- IDNA2008/Unicode host。

## 2. 生命周期

~~~text
CREATED -> CONNECTING -> READY <-> REFRESHING -> CLOSED
                       \-> FAILED（EOF / 协议错 / 版本错配）

spawn/connect -> initialize -> 版本协商 -> initialized 通知
-> tools/list（分页）-> 校验 + 不可变 catalog bind
-> tools/call（并发，request-id 关联）
-> list_changed -> refresh（新 generation catalog）
-> cancel/timeout -> close/shutdown
~~~

`McpSession` 内只有一个后台读取线程拥有 `transport.read()`；所有请求（initialize、
tools/list、tools/call）都注册 pending id 后发送，响应/通知由该线程按 id 分发。
refresh 由独立线程执行，避免单线程内同步等待造成死锁。

## 3. 绑定元组与身份

每个 catalog generation 的 binding 计算：

~~~text
schema_hash    = sha256(canonical(inputSchema))
binding_digest = sha256(canonical({
  server_id, session_generation, tool_name, schema_hash
}))
~~~

- `binding_digest` 经 `McpRegistryAdapter.binding_digest(name)` 进入
  `ToolLedgerStore.prepare(binding_digest=...)`，从而进入
  `logical_execution_id`。同一 model call 在 refresh 后得到**新的 execution_id**，
  旧 grant 不能平移给新 schema/generation。
- resolver 构造 `ResolvedAction(kind=MCP_TOOL, mcp_server_id, mcp_session_generation,
  mcp_schema_hash)`；三个字段都进入 action digest。PolicyEngine 对缺失 binding 的
  MCP 动作直接 `mcp_binding_required` DENY。
- 工具命名空间：`registry_name = f"{server_id}__{tool_name}"`；冲突/超长在 bind
  时拒绝。

## 4. 信任模型

| 主体 | 信任 | D10 边界 |
|---|---|---|
| MCP server 声明 | 不可信 | description/annotations/readOnly 只进 digest，不决定权限 |
| MCP server 输出 | 不可信 | 有界、redact、`untrusted_mcp_result` 标记 |
| 本地管理员配置 | 可信 | side-effect class / network / credential scope 只来自配置 |
| binding tuple | 可信结构 | 进入 D9 digest 与 D7 identity；refresh 后旧 binding 拒绝 |
| fixture/真实 server | 黑盒 | 只通过 stdio 帧协议交互 |

## 5. 失败矩阵

| 注入点 | 持久状态 | 处理 | 测试 |
|---|---|---|---|
| initialize 版本错配 | session FAILED | 拒绝握手并 close | `test_version_mismatch_fails_closed` |
| tools/list 超限 | connect 失败 | `mcp_tool_limit_exceeded` | unit（构造层） |
| 畸形帧/超大帧 | transport malformed | FAILED，稳定 code | `test_malformed_frame`、`test_frame_too_large` |
| request-id 错配 | 计数 | 丢弃并计数，调用侧超时→uncertain | `test_unknown_response_id_is_counted` |
| 慢响应超时 | 调用 uncertain | 非幂等 profile→OUTCOME_UNKNOWN | `test_timeout_marks_outcome_unknown` |
| EOF/断连 | session FAILED | pending 全部失败 | `test_eof_fails_pending_call` |
| list_changed/refresh | 新 generation | 旧 binding `mcp_binding_stale`，旧批准失效 | `test_refresh_creates_new_binding_and_new_approval` |
| 恶意/超长描述 | bind 拒绝 | `invalid_mcp_tool_description` | `test_invalid_tools_are_rejected` |
| 输出过大 | 截断+is_error | `mcp_result_too_large` 语义 | `_extract_result` 有界 |
| stderr 超限 | truncated 标志 | 停止读取防管道阻塞 | transport 层 |

## 6. 稳定错误码

| 类别 | 错误码 |
|---|---|
| 协议 | `malformed_json`、`duplicate_key`、`non_json_number`、`unsupported_jsonrpc_version`、`invalid_request_id`、`invalid_response`、`protocol_size_exceeded`、`invalid_method`、`invalid_params`、`invalid_payload` |
| 传输 | `transport_closed`、`transport_timeout`、`frame_too_large`、`frame_truncated`、`missing_content_length`、`invalid_content_length`、`unexpected_frame_header`、`non_ascii_frame_header`、`frame_header_too_large` |
| 会话 | `mcp_session_state_invalid`、`mcp_protocol_version_mismatch`、`mcp_initialize_failed`、`mcp_tools_list_failed`、`mcp_tool_limit_exceeded`、`mcp_request_timeout`、`mcp_request_failed`、`mcp_transport_closed`、`mcp_binding_stale`、`mcp_session_not_ready`、`mcp_request_id_mismatch`、`invalid_mcp_arguments` |
| 绑定 | `invalid_mcp_server_id`、`invalid_mcp_tool_name`、`invalid_mcp_tool_schema`、`unsupported_mcp_schema`、`invalid_mcp_tool_description`、`duplicate_mcp_tool_name`、`invalid_mcp_registry_name`、`invalid_mcp_generation` |
| Policy | `mcp_binding_required`、`mcp_binding_on_non_mcp_action`、`invalid_mcp_binding` |

错误只暴露稳定 code；原始 server 报文、参数与结果不回显进异常。

## 7. 测试矩阵

| 文件 | 覆盖 |
|---|---|
| `tests/test_d10_transport_protocol.py`（12） | 严格 JSON-RPC 解析/序列化、深度/大小/重复 key/NaN、帧往返、超时、EOF、畸形帧、超大帧、close 幂等 |
| `tests/test_d10_fixture_smoke.py`（5） | 真实 fixture 子进程：initialize、分页、echo、并发 id、错误工具、版本覆盖、畸形首帧 |
| `tests/test_d10_connection_binding.py`（15） | catalog 正反例、动态 typed spec、MCP binding 策略、session 连接/调用/超时/EOF/未知 id/refresh/auto_refresh、不可信输出信封 |
| `tests/test_d10_integration.py`（6） | 真实 fixture 全链路：ALLOW roundtrip+D7 binding、ASK grant 恢复执行一次、refresh 新 binding/新审批、超时 OUTCOME_UNKNOWN、错误 id uncertain、无直连旁路 |

合计 38 个 D10 测试。

聚焦命令：

~~~powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = 'src'
python -W error::ResourceWarning -B -m unittest tests.test_d10_transport_protocol tests.test_d10_fixture_smoke tests.test_d10_connection_binding tests.test_d10_integration -v
~~~

## 8. 示例

`examples/day10_mcp_roundtrip.py` 演示：真实 fixture 连接→catalog→两个并发
echo→typed error→慢调用超时 uncertain→Ledger OUTCOME_UNKNOWN→refresh 后旧
binding 拒绝→close。断言全部通过且真实网络调用为 0。

## 9. 推迟到后续日的风险

- 真实 MCP server 运行在 D8 容器内（D12 做 worktree+container 隔离时一并接线）。
- HTTP/SSE transport 与 OAuth 凭据（需凭据保管库）。
- 任意 JSON Schema 支持（`$ref`/composition）需扩展 ToolSpec 或独立 schema
  validator，未在本日实现。
