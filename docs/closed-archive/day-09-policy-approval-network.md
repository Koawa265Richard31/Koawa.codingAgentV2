# D9：Policy、Durable Approval 与网络/资源控制

## 1. 完成边界

D9 在 D3 Registry、D7 Tool Ledger 和 D8 Docker Sandbox 之间加入统一授权面。
它解决的是“一个已经通过 schema 的动作是否有权取得副作用 claim”，而不是让模型
直接决定权限。

D9 已实现：

- Policy domain 把 built-in tool、未来 MCP tool 与未来 Agent spawn 统一建模为
  `ALLOW | DENY | ASK`；没有匹配规则时 default deny，冲突规则按
  `DENY > ASK > ALLOW` 收敛；D10/D11 才接入真实 MCP/child lifecycle；
- action digest 绑定规范化参数、解析后的资源、主体、policy version、sandbox、网络、
  凭据范围和资源请求；
- Registry 在 Policy 之前完成 name/schema gate，并在绑定 policy authority 后拒绝
  raw handler bypass；
- `ASK` 产生可跨进程重放的 approval stream，并与 D1 Turn interrupt、D7 claim、
  durable resource budget 形成精确版本事务；
- claim 前再次解析 path/DNS/resource；身份漂移使旧 grant 失效并产生新 `ASK`；
- 网络目标规范化、DNS 全地址检查、rebinding 检查、redirect credential 检查和
  proxy-only policy 都有纯控制面实现；
- 联网必须在 PolicyEngine 构造时配置非空 origin 白名单并强制 proxy，二者缺一
  即构造失败；MCP 凭据按可信 server ID 绑定，不再按不可信 origin 放宽；
- Registry 的 prepared 票据是单次、防篡改的：调用时从原始 ToolCall 重新解码，
  成功/拒绝/等待/异常所有退出路径都会销毁票据；handler 只能在一次持久化 claim
  上进入一次（`tool.execution-attempted.v1` one-shot gate）；
- 终态结果重放会重新鉴权持久化 claim 证据；principal/policy/digest 漂移时拒绝
  返回旧结果，而不是把已批准结果释放给不同授权主体；
- ApprovalService 在生产构造下使用注入时钟，拒绝调用方逐命令覆盖时间。
- resource request 在授权前 preflight，实际 Docker 运行继续由 D8 强制 CPU、memory、
  PID、tmpfs、时间和输出限制。

D9 尚未开放：

- 真实外网 egress、HTTP proxy 进程或联网 MCP transport。D8 容器仍固定
  `--network none`；
- 凭据保管库、企业身份认证或审批者角色目录；
- 已发生副作用的撤销，或任意外部系统的 exactly-once；
- Unicode/IDNA2008 域名。当前没有获批的 IDNA2008 依赖，Unicode host 与
  `xn--` A-label 都 fail closed；
- Docker 资源限制之外的宿主级多租户配额。D9 durable budget 是授权计数器，不是
  OS scheduler。

因此，D9 可以声明“联网动作的身份和授权规则已经定义并可测试”，不能声明“Agent
已经能访问互联网”。

## 2. 威胁模型与信任边界

| 主体 | 信任 | D9 边界 |
|---|---|---|
| 模型输出与仓库文本 | 不可信 | 只能提出 tool name 与 JSON arguments；不能签发 grant |
| MCP server 声明 | 不可信 | capability claim 只进入 digest，不能扩展本地主体 scope |
| child Agent | 受限主体 | scope 只能是 parent scope 与 requested scope 的交集 |
| Tool Registry/schema | 可信结构边界 | 先确认工具存在并把参数解码成 typed value |
| resource/DNS resolver | 可信但会变化的观察边界 | 每次返回都要规范化、验证并绑定 digest |
| Policy Engine | 本地可信授权源 | 只根据管理员规则产生 ALLOW/DENY/ASK |
| ApprovalService | durable 协调者 | 保存 request/answer，执行版本校验和跨流原子提交 |
| D7 Tool Ledger | 副作用事实源 | PREPARED/CLAIMED/terminal/UNKNOWN，不接受 approval 替代账本 |
| D8 Docker controller | 强制执行边界 | 保持 network none，并执行不可由模型降低的硬限制 |
| Event Store | durable control plane | 只保存版本化 JSON，不保存 secret/client/process handle |

主要攻击与失败条件：

- 用无效 schema 诱导 Policy 或审批 UI 解释另一组参数；
- 审批后替换参数、cwd、symlink 目标、DNS 地址、origin、sandbox 或 principal；
- 用 D1 legacy `True` 冒充 D9 grant；
- 重复审批、过期审批、回答旧 request/interrupt，或并发消费 single-use grant；
- 在 cancel 与 claim 竞态中，让旧 Run 越过执行权 fence；
- MCP 或 child Agent 自报更大权限；
- URL parser 差异、userinfo、非 ASCII、奇异 IP、混合公网/私网 DNS、rebinding、
  redirect 或 direct socket 绕过 proxy；
- 只做预算预检却不做容器硬限制，或只做容器限制却不做 durable 总量预算。

## 3. 单一工具调用链

~~~text
Model ToolCall
  -> Registry.prepare(name + schema decode)
  -> D7 PREPARED（稳定 execution_id，参数只保存 hash/bytes）
  -> resolve canonical action
  -> PolicyEngine.evaluate
       DENY -> 配对 typed ToolResult；不调用 handler
       ASK  -> approval.requested + Turn WAITING；停止在 handler 之前
       ALLOW -> 继续
  -> re-resolve path/DNS/resource
  -> PolicyEngine.evaluate again
       drift -> 旧 approval 失效并重新 ASK
  -> ApprovalService.claim
       grant consume（ASK 时）
       durable budget reserve
       D7 claim/reclaim
       current Turn/Run precondition
       全部同一 SQLite transaction
  -> ApprovalService.begin_execution
       tool.execution-attempted.v1（claim token 流，one-shot CAS）
  -> process-local AuthorizedToolCall ticket
  -> Registry.execute_prepared(authority ticket)
  -> D8 sandbox / future MCP transport
  -> D7 result or OUTCOME_UNKNOWN
  -> paired ToolResult
~~~

schema 必须先于 policy。无效 arguments 不会创建 approval、ledger claim、
`tool_started` 或调用 handler。Registry 一旦绑定 D9 authority，旧 `execute()` 入口会以
`policy_authorization_required` 拒绝；伪造 authority 会以 `invalid_policy_authority`
拒绝。进程内 ticket 不是 durable 权限事实，崩溃后必须从事件流重新走授权和 claim。

## 4. ALLOW、DENY、ASK

Policy rule 可按 action kind、tool name、principal、required scope、side-effect class、
sandbox profile 和 allowed origin 匹配。

| 决策 | Runtime 行为 | 是否取得 claim | 模型可见结果 |
|---|---|---|---|
| ALLOW | 仍需 re-resolve、budget 和 Turn fence | 是 | 正常 ToolResult |
| DENY | 不进入 handler；生成稳定 policy error | 否 | 配对 error ToolResult |
| ASK | 持久化 request 与 Turn interrupt 后暂停 | 回答前否 | 恢复后重放同一 call |

多条规则同时匹配时，DENY 优先于 ASK，ASK 优先于 ALLOW；没有规则匹配时返回
`denied_by_default`。policy document 本身有版本；action 的 `policy_version` 与 Engine
不一致时直接 `stale_policy_version`，不能沿用旧版本 grant。

DENY 和用户拒绝都必须为原 ToolCall 产生配对的 typed error ToolResult。恢复逻辑不会
把审批布尔值伪装成新的用户消息，也不会静默丢弃原 pending call。

## 5. Action digest

`ResolvedAction` 先转为 canonical JSON，再计算小写 SHA-256。digest 不是 grant；它是
grant 所绑定的完整动作身份。

| 维度 | digest 中的字段 |
|---|---|
| 协议 | policy schema version、action kind |
| 工具 | canonical tool name、canonical arguments JSON |
| 资源 | kind、requested、resolved、stable identity、sorted metadata |
| 副作用 | read-only/idempotent/non-idempotent class |
| 沙箱 | trusted sandbox profile ID |
| 网络 | normalized URL、scheme/host/port origin、全部解析地址、via_proxy |
| 凭据 | credential scope ID、允许 origins/server IDs；不含 credential value |
| 资源预算 | CPU、memory、PID、tmpfs、duration、stdout/stderr、network bytes、tool calls、subagents |
| policy | exact policy version |
| 主体 | principal ID、sorted scopes、parent principal ID |
| MCP | untrusted claimed capabilities，仅用于防篡改和审计 |

approval event 只保存 digest 和必要身份，不保存 raw arguments 或 secret。D7 PREPARED
只保存 argument SHA-256 与 byte count；运行时凭据也不能进入 approval、ledger、日志、
模型上下文或 child Agent。

工具参数在同一个 D7 logical execution 中不可漂移；变化会得到
`policy_arguments_identity_mismatch`/D7 identity conflict，而不是让审批覆盖一个不同
调用。资源解析结果可以随现实变化，但必须生成新 digest 和新 request。

## 6. Durable approval 状态机

~~~text
                         approval.denied.v1
                       /                       -> DENIED
approval.requested.v1 -> PENDING
                       \                       -> EXPIRED
                         approval.expired.v1
                       \
                         approval.granted.v1   -> GRANTED
                                                    |
                                                    | approval.consumed.v1
                                                    v
                                                 CONSUMED
~~~

DENIED、EXPIRED、CONSUMED 之后可以针对新 digest 创建新 request。PENDING 或
GRANTED 发生 path/DNS/resource/policy drift 时，先追加带
`reason=action_or_policy_drift` 的 `approval.expired.v1`，再追加新的
`approval.requested.v1`。

request 绑定：

- subject/execution/request/interrupt/thread/turn/model-turn/call identity；
- action digest、principal、capability scope、policy version；
- expiry 与 `single_use=true`。

所有回答必须同时匹配 request ID、interrupt ID、approval expected version、Turn
expected version、当前 WAITING 状态和 expiry。错误 ID、错误类型、重复回答、过期
回答或旧版本都 fail closed。

### 6.1 Request 与 WAITING 原子

一个 `ASK` 在同一 commit 写入：

~~~text
approval.requested.v1
turn.waiting-for-approval.v1(approval_request_id, interrupt_id)
~~~

所以不会出现“UI 看见 request 但 Worker 仍能执行”，也不会出现 Turn 已等待却找不到
审批事实。写入前还要求 Turn 是 exact RUNNING version 且 run_id 匹配。

### 6.2 Answer 与 recovery 原子

批准、拒绝或到期在同一 commit 写入：

~~~text
approval.granted|denied|expired.v1
turn.recovery-queued.v1(request_id, interrupt_id, granted|denied)
~~~

Turn 只回到 QUEUED；之后 `start_turn` 产生新的 run_id/attempt。grant 不等于
执行权，恢复后的同一 pending call 仍要 re-resolve、重新 evaluate、消费 grant 并
取得 D7 claim。denied 则产生配对 error ToolResult，handler 调用次数保持零。

### 6.3 D1 legacy bool 的兼容边界

D1 旧 `turn.waiting-for-approval.v1` 没有 `approval_request_id`，其 bool recovery 仍可
重放，保证旧事件历史可读。但它只形成 `last_resume_response`，绝不形成 D9
`ApprovalRecord` 或 grant。

带 `approval_request_id` 的 durable pending 明确拒绝旧
`request_resume(..., response=True)`。因此 legacy `True` 既不能授权当前 durable ASK，
也不能在新 call 中复用。

## 7. Single-use、budget、D7 claim 与取消竞态

ASK 动作最终执行前，一个 append batch 同时包含：

~~~text
tool.execution-claimed|reclaimed.v1
approval.consumed.v1
resource.budget-reserved.v1
precondition:
  Turn exact version
  latest event == turn.started.v1
  payload.run_id == current run_id
~~~

三条写事件共享 commit ID；`StreamPrecondition` 对 Turn 在同一 SQLite transaction
内验证但不推进 Turn stream。任何一个流版本过期，整批回滚。ALLOW 不写 approval consume，
但仍把首次 D7 claim 和 budget reserve 原子提交。

claim 提交后、handler 进入前还有一个独立的持久化 one-shot gate：executor 以
`claim_token` 为流写 `tool.execution-attempted.v1`。两个进程/线程对同一 claim
并发进入时，事件流 CAS 只允许一个写入，另一个得到
`tool_claim_already_executed`。这样“同一次授权只进一次 handler”不依赖进程内
ticket 表，进程重启后依然成立。

并发消费同一 grant 最多形成一个逻辑 consumer、一个 claim token 和一条 budget
reservation；重复同语义命令只重放原 commit，竞争语义不同则由 stream CAS 拒绝。

取消竞态由事务顺序决定：

- cancel 先提交：Turn version/precondition 已变化，claim batch 整体失败；grant 保持
  GRANTED，ledger 保持 PREPARED，budget 不扣；
- claim 先提交：grant 已 CONSUMED、ledger 已 CLAIMED、budget 已扣。之后 cancel 只
  阻止新工作，不能假装撤销已经取得的副作用执行权；claim 仍留作恢复/审计事实。

这不是 exactly-once。claim 与外部系统结果之间仍存在 D7 的
`OUTCOME_UNKNOWN` 窗口。

## 8. Re-resolve 与 drift

授权前至少观察两次资源身份：第一次用于 policy/approval，第二次紧邻 claim。

| drift | 处理 |
|---|---|
| symlink/junction/canonical path/文件身份变化 | 新 digest，旧 grant 失效，重新 ASK |
| cwd 或 resource metadata 变化 | 新 digest，重新 ASK |
| DNS 地址集合变化 | `dns_rebinding_detected` 或新 request，不沿用旧 grant |
| origin/port/proxy flag 变化 | 新 digest并重新 policy；不携带旧 credential scope |
| policy version/principal/scope 变化 | 旧 grant 不匹配 |
| raw tool arguments 变化 | 同一 D7 execution 直接 identity mismatch |

重新 ASK 的 commit 会把旧 PENDING/GRANTED 标记 EXPIRED、写新 REQUEST，并让 Turn
再次 WAITING。不存在“用户批准路径 A，Runtime 最后执行路径 B”的隐式转换。

## 9. MCP 与 child Agent 不得扩权

`Principal.narrow()` 生成 child principal 时：

~~~text
child.scopes = intersection(parent.scopes, requested_scopes)
child.parent_principal_id = parent.principal_id
~~~

请求父级没有的 scope 只会被丢弃，不能通过 child ID 获得。child 后续 action digest
绑定自己的 principal、收窄后的 scopes 和 parent ID。

MCP server 的 capability declaration 是不可信输入。它进入 action digest 供篡改审计，
但 policy rule 的 `required_scopes` 只检查本地 trusted principal scopes。MCP 不能用
`mcp_claimed_capabilities` 扩大 allowlist，也不能绕过 Registry/schema、Policy、
Approval、Ledger 或未来 transport proxy。

## 10. 网络策略

### 10.1 URL 与 origin

Python 官方文档明确说明 `urlsplit()`/`urlparse()` 不执行输入验证，所以 D9 只把
`urlsplit()` 当分解器，并在其后施加自己的 fail-closed 规则：

- 输入必须是有界 ASCII；拒绝 NUL、CR/LF、反斜杠和控制/空白字符；
- 只接受 HTTPS；拒绝 userinfo、fragment、空 host、非法/模糊 IP；
- scheme/host 小写，去掉 host 末尾点，HTTPS 缺省端口规范化为 443；
- path 移除 dot segment，percent escape 统一；origin 固定为 scheme+host+port；
- Unicode 和 `xn--` 暂不接受，避免把标准库 IDNA2003 行为误称为 IDNA2008 验证。

RFC 3986 规定 scheme/host 不区分大小写、默认端口可省略，并提示 userinfo 中携带
认证信息具有安全风险。D9 的规则比通用 URI grammar 更窄，这是安全边界而不是
通用浏览器兼容目标。

### 10.2 DNS 全地址与 rebinding

DNS resolver 返回的全部 IPv4/IPv6 地址都进入 canonical set。任何一个地址不是
`ipaddress.is_global`，或属于 private、loopback、link-local、multicast、reserved、
unspecified，整个目标都拒绝；IPv4-mapped IPv6 按内嵌 IPv4 再检查。这样可以拒绝
“一个公网答案夹一个 127.0.0.1”的混合集合。

Python `socket.getaddrinfo()` 的结果是地址序列且平台行为可能不同；D9 因而不只验证
第一个答案。Python `ipaddress` 还说明共享地址空间可能同时满足
`is_private == False` 与 `is_global == False`，所以实现采用“必须明确 global”，而不是
仅使用 `not is_private`。

同一授权窗口再次解析时，地址集合变化以 `dns_rebinding_detected` 拒绝。未来网络
transport 还必须把已批准的地址集合传给受控 proxy/connector，不能验证 host 后又让
底层 client 自主解析到另一地址。

### 10.3 Redirect 与 credential

每一跳 Location 都相对当前 URL 解析，然后完整重做 HTTPS/origin/DNS/policy。
credential 只能发送到其 scope 明确允许且与当前请求相同的 origin；跨 origin redirect
返回 `credential_redirect_forbidden`，不会复制 Authorization/Cookie。

RFC 9110 指出自动 redirect 对 unsafe method 必须谨慎，并建议重建或移除
origin/resource-specific headers，包括 Authorization 与 Cookie。D9 选择更严格规则：
有 credential 时禁止跨 origin 携带，而不是依赖通用 client 的默认 redirect 行为。
未来 transport 还必须设置有界 hop count 并检测 redirect loop。

### 10.4 Proxy-only 与 D8 network none

network action 的 digest 绑定 `via_proxy`。Policy 默认 `proxy_required=True`；目标未标记
经受控 proxy 时返回 `proxy_required`。`network_enabled=False` 时无条件
`network_disabled`，origin allowlist 与 credential scope 也必须同时通过。

当前没有真实 proxy 或 egress adapter。D8 Docker 命令继续使用 `--network none`；
Docker 官方说明 none driver 只创建隔离网络栈中的 loopback。未来若开放网络，只能
接入 D9 控制的 proxy/allowlist transport，不得把容器直接改成 unrestricted bridge。

## 11. 资源双层控制

`ResourceRequest`/`ResourceBudgetLimits` 在 Policy 阶段比较：

- CPU、memory、PID、tmpfs；
- duration；
- stdout/stderr bytes；
- network bytes；
- tool call count 与 subagent count。

超过任一 ceiling 返回 `resource_budget_exceeded`，在 approval、claim 和 handler 前
失败。缺少管理员要求的 resource request 返回 `resource_request_required`。

ApprovalService 另有按 principal 的 durable action-count budget。首次 claim 与
`resource.budget-reserved.v1` 同 commit，崩溃/重试不会重复扣；达到 limit 时第二个
execution 保持 PREPARED。该计数当前单调累计，不表达费用退款或时间窗口。

preflight 不是强制执行。Docker 官方指出容器缺省没有资源约束，因此 D8 仍必须使用
`--cpus`、`--memory`/`--memory-swap`、`--pids-limit`、tmpfs size、deadline 和 output
cap。Policy ceiling 防止未授权请求，Docker/cgroup 防止运行时超用，两层缺一不可。

## 12. 事件协议

| event | 状态/作用 | 精确版本边界 |
|---|---|---|
| `approval.requested.v1` | 新 PENDING，保存 request/digest/principal/policy/expiry | approval stream expected version |
| `approval.granted.v1` | PENDING -> GRANTED | request + approval version |
| `approval.denied.v1` | PENDING -> DENIED | request + approval version |
| `approval.expired.v1` | PENDING/GRANTED -> EXPIRED | expiry 或 drift invalidation |
| `approval.consumed.v1` | GRANTED -> CONSUMED | 与 D7 claim/budget 同 commit |
| `turn.waiting-for-approval.v1` | RUNNING -> WAITING | 与 request 同 commit；run fence |
| `turn.recovery-queued.v1` | WAITING -> QUEUED | 与 resolve 同 commit；request/interrupt exact match |
| `resource.budget-reserved.v1` | durable principal action count +1 | 与首次 claim 同 commit |
| `tool.execution-claimed.v1` | D7 PREPARED -> CLAIMED | Turn started/run precondition |
| `tool.execution-reclaimed.v1` | retry-safe stale claim -> new claimant | 不重复消费 grant/budget |
| `tool.execution-attempted.v1` | one-shot handler entry gate | claim token 流版本 CAS |

事件 payload 都是有界 JSON。approval 不保存 raw arguments、credential、完整环境、
runtime client 或隐藏 reasoning。

## 13. 稳定错误码

| 类别 | 代表错误码 | 含义 |
|---|---|---|
| policy 默认拒绝 | `denied_by_default`, `rule_denied`, `stale_policy_version` | 无权限或策略已变 |
| Registry 绕过 | `policy_authorization_required`, `invalid_policy_authority`, `invalid_prepared_invocation`, `prepared_invocation_schema_drift` | handler 未经过 D9 authority 或票据被替换/重放 |
| action 身份 | `policy_action_digest_mismatch`, `policy_arguments_identity_mismatch`, `policy_tool_identity_mismatch`, `approval_action_mismatch` | verdict/grant 与动作不一致 |
| approval 等待/回答 | `approval_waiting`, `approval_request_stale`, `approval_interrupt_mismatch`, `approval_version_stale`, `approval_turn_version_stale`, `approval_already_resolved` | durable request 未答或回答错误对象 |
| approval 消费 | `approval_grant_required`, `approval_expired`, `unexpected_approval_grant` | 缺 grant、过期或不应携带 grant |
| Turn fence | `approval_turn_fence_rejected`, `durable_turn_identity_required` | stale/non-durable Run 不能申请或 claim |
| 网络 | `https_required`, `invalid_network_url`, `network_userinfo_forbidden`, `non_global_network_address`, `dns_rebinding_detected`, `credential_redirect_forbidden`, `network_disabled`, `proxy_required`, `network_allowlist_required`, `network_proxy_required` | 网络身份或路由不安全；联网必须配白名单与强制 proxy |
| 终态重放 | `policy_authorization_evidence_missing`, `policy_authorization_evidence_mismatch`, `policy_revalidation_unavailable`, `credential_server_missing` | 终态结果必须与持久化 claim 证据、当前 policy/principal 一致才释放 |
| 资源 | `resource_request_required`, `resource_budget_exceeded`, `resource_budget_principal_missing` | preflight 或 durable 计数失败 |
| 执行门/时钟 | `tool_claim_already_executed`, `untrusted_approval_time_override` | claim 已被执行一次，或调用方在生产模式覆盖审批时钟 |

错误只暴露稳定 code，不回显 secret、raw credential 或不可信异常正文。

## 14. 测试矩阵

当前 D9 聚焦测试由三组组成：

| 文件 | 覆盖 |
|---|---|
| `tests/test_d9_policy.py`（17） | DENY/ASK/ALLOW 优先级与 default deny、canonical JSON/digest、path/cwd drift、HTTPS、DNS 私网/混合答案/rebinding、redirect credential、proxy bypass、联网白名单/强制 proxy、resource preflight、MCP credential server binding、MCP/child 收窄 |
| `tests/test_d9_approval.py`（14） | request/WAITING 与 answer/recovery 原子、无 raw secret、生产时钟拒绝逐命令覆盖、legacy bool 拒绝、错误 ID/type/version/principal/policy/digest/expiry、重复回答、single-use consume+budget+D7 claim、并发消费、cancel-first/claim-first、budget exhausted、drift re-ASK、restart load |
| `tests/test_d9_integration.py`（11） | schema-before-policy、Registry bypass、ASK 在 handler/tool_started 前持久化、grant 重启恢复一次执行、denial 配对结果、resolver drift 新 ASK、ALLOW 无审批、legacy True 不授权、prepared/authorized 票据防篡改与单次消费、同 Run 并发只进一次 handler、终态结果在 principal/policy 漂移后拒绝释放 |

2026-08-21 聚焦共 42 个 D9 tests 全部通过；从 `v2/` 全量
`unittest discover` 为 259 tests / OK（当前环境 Docker daemon 不可用时
11 个 Docker skip + 3 个平台能力 skip）。

聚焦命令：

~~~powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = 'src'
python -W error::ResourceWarning -B -m unittest tests.test_d9_policy tests.test_d9_approval tests.test_d9_integration -v
~~~

D9 完成时必须从 `v2/` 执行全量：

~~~powershell
python -W error::ResourceWarning -B -m unittest discover -s tests -v
~~~

## 15. 参考

- [RFC 3986：URI Generic Syntax](https://www.rfc-editor.org/rfc/rfc3986)
- [RFC 9110 §15.4：HTTP Redirection](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.4)
- [Python urllib.parse：URL parsing security](https://docs.python.org/3/library/urllib.parse.html#url-parsing-security)
- [Python socket.getaddrinfo](https://docs.python.org/3/library/socket.html#socket.getaddrinfo)
- [Python ipaddress：is_global](https://docs.python.org/3/library/ipaddress.html#ipaddress.IPv4Address.is_global)
- [Docker none network driver](https://docs.docker.com/engine/network/drivers/none/)
- [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)
- [Docker Engine security](https://docs.docker.com/engine/security/)
