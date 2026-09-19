# KoawaAgent V2 稳定化与生产就绪详细规划书

| 项目 | 内容 |
| --- | --- |
| 文档状态 | PROPOSED：稳定化执行基线，尚未代表各阶段已经完成 |
| 编写日期 | 2026-08-25 |
| 适用范围 | KoawaAgent V2 的生产代码、测试、迁移、示例和文档，仅限 v2/** |
| 目标版本 | V2 Stability Gate 1 |
| 实施原则 | 暂停新增横向能力，先修复状态真实性、并发原子性、恢复、安全边界和发布证据 |
| 事实来源 | 现有路线图、D1–D22 切片文档、安全文档、当前源码审查与两次全量回归 |
| 代码实施文档 | `docs/v2-stabilization-detailed-implementation.md`，供执行 Agent 按 I1–I9 逐单元实施 |

> 本文是实施规划，不是完成声明。每个稳定化切片只能在代码、故障路径、迁移、
> 测试和文档全部满足本节门禁后改为 COMPLETE。

## 0. 执行结论

KoawaAgent V2 已经具备较强的单 Agent 持久化主链：显式 Thread/Turn/Run、精确
stream version、SQLite 多流事务、策略与审批、工具 ledger、受限文件工具、补丁
事务、检查点、MCP stdio、Docker/worktree、trace/eval 等基础能力均已有真实实现。

但当前不能继续使用“D1–D22 全部完成”推导“已经生产就绪”。原因不是缺少更多功能，
而是若干已完成声明与当前可复现行为冲突：

1. D11 mailbox、spawn、lease、budget 存在任务丢失、容量超卖和错误接管窗口。
2. D6 checkpoint 可以在覆盖事件位置与 hash 合法时接受伪造的 projection 内容。
3. D10 由管理员授权的宿主 MCP 进程继承完整环境，造成不必要的 credential 扩散，
   违反 D8 与安全文档的最小环境原则。
4. Unified runtime 可返回 completed，而持久 Turn 仍为 running；doctor 也可能留下
   active probe。
5. 全量测试当前非绿，且 16 个跳过中有 13 个覆盖 Docker/golden E2E；这些结果不能
   证明最终发布门。
6. worktree、MCP、telemetry 和 durable 文本边界仍有未纳入统一 ledger、资源收束或
   限量脱敏规则的路径。

因此本计划采取以下发布决策：

- 冻结新工具、新 transport、新 UI、新 provider 类型和模型驱动写子 Agent。
- 保留历史切片的 implemented 事实，但新增 Stability Gate 状态。
- D6、D10、D11、D15 进入 REOPENED；D12、D14 进入 REVALIDATION。
- D21 保留“离线安全审计切片已完成”，但不得外推为运行时安全已经完成。
- D22 的局部功能实现可保留 COMPLETE；其全量回归与发布门重新验收。
- S0–S7 全部完成前，不发布 production-ready、release green 或全部完成声明。

## 1. 规划依据

### 1.1 已阅读文档

本规划先对齐现有文档，再结合当前实现制定，不另造一套术语。输入包括：

- docs/15-day-coding-agent-roadmap.md
- docs/day-01-durable-control-plane.md 至 docs/day-22-robustness-hardening.md
- docs/day-15-interview-package.md 与 docs/day-15-real-model-runtime.md
- docs/day-21-agent-security-authenticity.md 与 docs/day-21-detailed-implementation.md
- docs/agent-security-threat-model.md
- docs/agent-security-engineering.md

### 1.2 事实优先级

发生冲突时按以下顺序裁决：

1. 可重放的 Event Store、ledger 和外部资源核验结果。
2. 当前源码与可重复的测试/故障注入证据。
3. 本规划批准后的验收合同。
4. 路线图和历史切片中的状态说明。
5. 示例输出、模型 final、面试话术。

历史文档中的 COMPLETE 是当时的交付记录，不覆盖后来发现的反例。模型 final、函数返回
和 trace 也不能单独证明业务成功。

### 1.3 当前回归基线

在当前工作区以 PYTHONPATH=src 运行全量 unittest，两次独立运行均发现 417 个测试：

| 运行 | 结果 | 观察 |
| --- | --- | --- |
| 基线 A | errors=3，skipped=16，约 236 秒 | D10 MCP 冷启动连接超时，其中一项具有波动性 |
| 基线 B | errors=2，skipped=16，约 237 秒 | 两项 0.5 秒连接超时稳定复现 |

已测得 Windows 冷子进程连接约需 0.572–0.762 秒，而测试把 0.5 秒同时用于启动握手和
tool call。第三项 1.0 秒 refresh 偶发失败。这说明“启动 deadline”和“调用 deadline”
必须拆分，也说明当前全量门为红。

16 个跳过中，13 个属于 Docker/golden E2E，另有 3 个为平台文件系统用例。发布证据
不能把这些跳过解释为通过。

今后的基线不写死测试数量。正确断言是：

    discovered = passed + explicitly_approved_skips
    failures = 0
    errors = 0

核心生产链在对应 mandatory job 中必须为零跳过。

## 2. 必须继承的架构合同

以下原则直接来自既有路线与切片，稳定化不得削弱：

1. Event log 是事实源；checkpoint、summary、cache 和 trace 只能是可验证投影。
2. 每个状态变化都是有版本的 typed JSON event，并使用精确 expected stream version。
3. stale version、run、agent 或 claim token 不能提交状态，也不能获得新的外部副作用。
4. 完整且合法的 model terminal 形成前，任何工具都不能执行。
5. 工具链固定为 Schema/Registry → Policy/Approval → Ledger → Sandbox/Transport。
6. OUTCOME_UNKNOWN 不等于 FAILED；不能以自动重试伪装 exactly-once。
7. 只持久化有界、版本化、可脱敏的 JSON；禁止 pickle、runtime object、完整环境、
   credential value、隐藏推理和完整环境转储。
8. 路径必须 canonicalize、containment 校验并防御 symlink/junction/hardlink/TOCTOU。
9. network 默认关闭；MCP protocol action、容器和 child Agent 只能收窄权限，不能扩大
   父级授予的 action 权限。宿主 MCP executable 本身属于管理员信任边界，不用这条合同
   伪装成 OS 隔离。
10. cancel 只阻止新工作，不撤回已经发生或结果不确定的外部副作用。
11. 默认不 commit、push、reset、删除或覆盖用户改动；无法证明 workspace 终态时显式
    workspace_outcome_unknown。
12. stdout、stderr、文件、模型、工具、MCP、event、checkpoint、trace 全部必须有字节、
    深度、节点、数量、时间、并发和资源边界。
13. 配置错误尽量在 durable RUNNING 之前 fail closed。
14. 自动重试、redelivery、takeover、fallback、compaction 和 reaper 必须可见、可审计，
    禁止静默降级。

## 3. 信任与数据边界

### 3.1 信任模型

| 边界 | 分类 | 规划要求 |
| --- | --- | --- |
| 模型输出、仓库文本、工具/MCP 返回 | 不可信 | 结构校验、有界、脱敏；不能直接授权 |
| MCP tool 声明/输出与 child Agent | 不可信 | binding 固定身份；action 权限只能收窄；run fence |
| sandboxed MCP executable/process | 不可信进程 | D8 等价文件系统/网络/进程/资源边界；spawn 前授权 |
| 宿主 stdio MCP executable/process | 管理员授权的宿主代码 | 当前无 OS containment；生产配置必须显式 host_trusted，记录解析身份与配置 digest |
| 容器内命令 | 不可信 | immutable image、network none、非 root、资源上限 |
| Schema/Policy/Approval/Ledger/host controller | 可信控制面 | 仍须版本、限量、事务和审计 |
| 路径、文件、DNS、资源解析 | 会漂移 | claim 前重新 resolve，并重算 action digest |
| 管理员配置 | 可信输入 | 严格 schema、限量、fail closed；不防恶意管理员 |
| Docker daemon | host-root 级可信基座 | 不声称防御恶意 daemon |

### 3.2 凭据与 durable 文本

- 配置只保存 API key 的环境变量名，不保存值。
- 持久化只允许 credential scope、server/origin ID、digest 和脱敏后的有界副本。
- 运行时托管的 credential value 不进入 approval、ledger、trace、模型上下文、child Agent
  或环境快照。用户主动输入的 credential-like 文本在进入首轮模型前就变成 canonical
  redacted view；需要真实值时只能使用受控 reference/scope。
- 入口先得到有界且凭据形态脱敏的 canonical user input；首轮模型调用、幂等身份、持久化和
  恢复都只使用这一份 canonical 值，不能首轮用原文、恢复时换成脱敏文本。
- 需要真实 credential 的任务只传 credential reference/scope，不把 value 混入 user input。
- 脱敏只承诺覆盖已定义的 Bearer、sk-、敏感键和赋值式凭据，不宣称识别任意业务秘密。
- 防止任意秘密外泄的主要边界仍是默认无 egress，而不是注入关键词正则。

### 3.3 明确不声称

本轮不声称或不实施：

- 模型层 jailbreak 免疫、启发式提示注入识别或任意业务秘密自动脱敏。
- 防御恶意管理员、恶意 Docker daemon 或宿主多租户 OS 级隔离。
- 恶意 host_trusted MCP executable、宿主 MCP 供应链隔离或其文件系统/网络 containment；
  若未来接收不受信任的 MCP binary，必须先新增真正的进程/文件系统/网络沙箱。
- 外部副作用 exactly-once、自动撤销或不确定结果自动重试。
- MCP HTTP/SSE、OAuth、credential vault、真实网络 MCP、任意 JSON Schema。
- 通用 TUI、浏览器/电脑操作、LSP、向量检索和新 provider 家族。
- 模型驱动的写子 Agent；在 D11/D12 稳定化前继续保持显式非目标。

## 4. 当前问题登记册

### 4.1 P0：会造成错误事实、任务丢失或凭据扩散

| ID | 问题 | 违反合同 | 目标切片 |
| --- | --- | --- | --- |
| P0-01 | mailbox 用单条 message 的版本作为整个 mailbox stream expected version；旧消息在后续 enqueue 后无法 deliver | exact stream CAS | S1 |
| P0-02 | DELIVERED 后 ACK 前崩溃，恢复只查询 QUEUED，scheduler 可直接完成 Agent，任务永久丢失 | message 不丢、恢复可重建 | S1 |
| P0-03 | spawn 的父状态/深度/每父并发检查不在事务中；两个并发 spawn 可同时越过最后一个 slot | parent/run fence、容量原子 reserve | S2 |
| P0-04 | provider 调用期间没有 LeaseKeeper；慢但存活的 worker 可被错误 orphan/takeover | lease 与 stale result fence | S1/S2 |
| P0-05 | Agent terminal 和 budget release 分两次提交；中间崩溃永久泄漏预算 | terminal/settlement 原子性 | S2 |
| P0-06 | Unified runtime 返回 completed，但 durable Turn 仍 running；doctor 留下 running probe | durable truth 优先 | S5 |
| P0-07 | checkpoint 只核对覆盖事件位置/hash，不验证 context、phase、counter 等等价于事件归约 | checkpoint 只是可验证投影 | S3 |
| P0-08 | host_trusted MCP StdioTransport 仍复制完整 os.environ，向无需凭据的进程扩散宿主 secret | 最小环境、credential 不扩散 | S4 |
| P0-09 | user_input、resume、prompt、summary 等 durable 文本没有统一字节/深度/节点/脱敏底线 | JSON-only 且有界脱敏 | S3 |
| P0-10 | 旧数据库可能已含原始 user_input；禁止改写事件与“库中无 canary”发布门之间缺少迁移策略 | 事件不可篡改、凭据不持久化 | S3/S6 |

### 4.2 P1：会产生错误结果、无法恢复或发布证据失真

| ID | 问题 | 目标切片 |
| --- | --- | --- |
| P1-01 | scheduler 丢弃 provider outcome，持久化固定 completed/no_more_messages | S2 |
| P1-02 | MCP logical execution identity 含 binding_digest，但恢复 lookup 不带 digest | S4 |
| P1-03 | worktree add/reap/deliver 与 typed event/ledger 的先后顺序可产生孤儿或虚假事实 | S5 |
| P1-04 | artifact 返回的 head 仍是 base HEAD，不能证明未提交 diff 后的内容身份 | S5 |
| P1-05 | StdioTransport.close 在未 open 时不安全；refresh/close 竞态可出现 closed=True 但状态 READY | S4 |
| P1-06 | stderr 到保留上限后停止读取，持续写 stderr 的 server 可能堵塞 | S4 |
| P1-07 | connect/initialize/list/call/read 共用一个 timeout，既造成 flake 又耦合错误语义 | S4 |
| P1-08 | assembly 后段失败、AppRuntime 结束时缺少统一 close，可能遗留 MCP 进程/线程/管道 | S4 |
| P1-09 | Trace append 的并发 CAS 失败可向上冒泡，在 ledger claim 后破坏业务路径 | S5 |
| P1-10 | AppRuntime 的 terminal 集合和 resume/cancel 状态语义未完整包含 TIMED_OUT | S5 |
| P1-11 | EventStore abstraction 被 recovery projection 的具体 SQLite 表与 raw INSERT 泄漏 | S3/S6 |
| P1-12 | 13 个关键 Docker/golden 用例被跳过；当前不能满足路线图发布门 | S7 |
| P1-13 | runtime config 缺少统一的读取字节、duplicate-key、深度、节点、集合和文本上限 | S3 |

### 4.3 P2：维护、容量和长期演进风险

- 多个生产模块超过 500 行，部分超过 1000 行；状态机、I/O 和装配责任耦合。
- AgentGraph.children 与 wait_agents 反复全流扫描，长事件流下可能退化。
- 没有正式 DB schema version、forward migration 与 current-to-next migration 测试。
- 没有固定 lint/type/coverage gate；关键事务分支缺少量化覆盖门槛。
- D14 FaultInjector 尚未接入生产失败点；20/20 未注入 eval 不能证明 crash window。
- D18 已承认每轮全局事件扫描 O(n)；当前缺少容量和 soak 证据。
- 部分文档仍保留历史测试数量或两套实现算法，需要在稳定化后统一。

### 4.4 主要代码影响面

| 问题域 | 当前主要模块 | 计划中的改动边界 |
| --- | --- | --- |
| Agent graph/mailbox/budget | src/koawa_agent_v2/agents/control.py、messages.py、scheduler.py | stream-head CAS、delivery attempt、parent capacity、原子结算、LeaseKeeper |
| Control plane/EventStore | src/koawa_agent_v2/control/event_store.py、sqlite_store.py、runtime.py | durable JSON 底线、多流公共事务、用户文本限量脱敏 |
| Checkpoint/recovery | src/koawa_agent_v2/recovery/store.py、coordinator.py | checkpoint v2、canonical reducer 验证、移除 raw SQLite 写入 |
| MCP | src/koawa_agent_v2/mcp/transport.py、connection_manager.py、tool_binding.py | 最小环境、分段 deadline、close/refresh 竞态、stderr drain、限量 |
| Ledger/identity | src/koawa_agent_v2/ledger/** | binding-aware typed identity、恢复 lookup 一致 |
| Unified runtime | src/koawa_agent_v2/runtime/unified.py、app.py、assembly.py、cli.py | durable receipt、terminal/detach 一致、read-only doctor、统一 close |
| Workspace/artifact | src/koawa_agent_v2/workspace/** | 外部效果 ledger、content identity、crash reconciliation |
| Trace/eval | src/koawa_agent_v2/telemetry/**、src/koawa_agent_v2/evals/** | 业务隔离、命名生产注入点、确定性重放 |
| 配置与迁移 | src/koawa_agent_v2/runtime/config.py、SQLite schema 初始化路径 | timeout/env schema、DB version、forward-only migration |

该表是影响面，不授权大爆炸重构。每个 S 切片只能改它为关闭当前合同缺口所必需的部分。

## 5. 目标架构与依赖顺序

稳定化不重写主架构，而是把 D1/D6/D7/D9 已有的事务和 fencing 语义复用到 D11、
MCP、workspace 与统一运行时。

~~~text
S0 事实基线与发布门
 ├─> S1 mailbox 不丢消息
 │    └─> S2 spawn / lease / result / budget 原子化
 ├─> S3 checkpoint / EventStore / durable JSON 真相边界
 └─> S4 MCP sandbox、环境、deadline、identity、生命周期

S1 + S2 + S3 + S4
 └─> S5 Unified runtime / workspace / trace 端到端一致
      └─> S6 migration / fault injection / performance
           └─> S7 跨平台、Docker、golden E2E 发布验收
~~~

上图表示依赖拓扑，不表示必须按编号串行。S0 必须先完成；之后 ready set 为 S1、S3、S4，
但任何时刻只实施一个切片，不为未来阶段创建空模块。选择规则为：正在启用 MCP 时先 S4；
需要打开遗留库或修 durable input 时先 S3；其余情况先 S1 再 S2。S5 必须等待 S1/S2/S3/S4
全部完成。若某切片发现新的 P0，停止开启新切片，把问题加入登记册并先关闭。

## 6. S0：建立可重复事实基线与治理门

### 6.1 目标

- 把历史 COMPLETE 与当前 Stability Gate 分开，避免文档继续传播错误发布状态。
- 固定可重复命令、环境证据、skip 分类和失败样本。
- 为后续每个修复建立反例测试，先红后绿。

### 6.2 工作项

1. 在路线图增加 Current Stability Gate 小节，标记 D6/D10/D11/D15 REOPENED，
   D12/D14 REVALIDATION；保留历史实现记录。
2. 为测试输出增加机器可读摘要：Python/OS、SQLite、Git、Docker daemon、image digest、
   discovered/passed/skipped/failures/errors、随机 seed 和耗时。
3. 建立 approved-skip 清单。每一项包含平台原因、对应 mandatory job 和到期条件。
4. 把 D10 连接失败固定成独立 regression fixture，记录 startup 和 call 两段耗时。
5. 为 P0-01 至 P0-10 分别记录最小确定性复现和预期事实，不用 sleep 猜时序，使用 barrier、
   fake clock 或命名 fault point。对应 S 切片开始时把复现转成正式回归测试，并在同一切片
   修绿；不把故意失败的测试合入主分支。
6. CI 先分成 PR-fast、PR-integration、Linux-Docker、nightly-soak、provider-opt-in 五层；
   S0 只建立配置和红线，不把尚未修复的红测伪装为允许失败。

### 6.3 交付物

- 更新后的路线图状态段与 issue-to-test 索引。
- 一条统一的本地验证命令和分层 CI 配置。
- P0 反例索引、可单独运行的复现步骤及当前预期失败说明。

### 6.4 完成门

- 同一 commit 连续三次 discovery 数量一致。
- 所有 skip 均能映射到 approved-skip 或 mandatory job；未知 skip 直接失败。
- 每个 P0 至少有一个确定性复现合同，错误不是依赖 0.x 秒 sleep 的概率结果；主测试发现
  范围内不遗留故意失败项。
- 文档不再声称当前 release green。

## 7. S1：D11 mailbox 不丢消息与安全 redelivery

### 7.1 目标状态机

~~~text
QUEUED
  -> DELIVERED(message_id, delivery_attempt, run_id, lease_until)
  -> RESULT_RECORDED(result_ref, result_digest)
  -> ACKED(result_ref)

DELIVERED --lease expired / owner orphaned-->
  UNRESOLVED/OUTCOME_UNKNOWN
    -> REQUEUED（仅有安全重放证据或用户显式决定）
    -> CANCELLED / MANUAL_REVIEW

QUEUED/DELIVERED -> CANCELLED
~~~

消息处理语义是“至少一次 delivery + 幂等提交”，不是外部副作用 exactly-once。
DELIVERED 只是有租约的处理 claim，绝不能被当成已完成。崩溃后必须 redeliver 或进入显式
UNRESOLVED/OUTCOME_UNKNOWN；不能静默把 Agent 标成 completed。

### 7.2 设计修改

1. mailbox stream CAS 一律使用当前 mailbox stream head version。
   MessageRecord.version 只表示该 message projection 最后变化位置，不能作为 stream
   expected version。
2. delivery/ack/requeue 事件写入时同时携带 message_id、delivery_attempt、agent_id、
   run_id 和有界 idempotency key。
3. mailbox/result stream append 与 Agent ownership stream 的 exact-version/current-run
   StreamPrecondition 放在同一事务；不要为了做 precondition 推进 Agent stream。delivery/
   ack 审计留在 mailbox 或独立 audit stream，避免无故使 heartbeat/terminal 持有的 owner
   version 失效。
4. mailbox 查询同时识别 QUEUED 和过期 DELIVERED。过期 delivery 先进入
   UNRESOLVED/OUTCOME_UNKNOWN；只有 ledger/receipt 证明 provider/effect 尚未开始、调用明确
   幂等/纯读，或用户显式选择时才 requeue。排序以 per-agent sequence 为业务顺序，不以
   message-local version 代替 stream version。
5. provider 开始前持久化 delivery；provider 返回后写 RESULT_RECORDED。恢复看到该状态时
   只补 ACK，绝不再次调用 provider。result record、ACK 与必要的 parent/Agent 状态可在
   append_batch 中一次提交时优先一次提交；若因 aggregate 边界分两步，也必须以上述状态
   关闭 result commit 后/ACK 前窗口。若工具以后接入 child Agent，工具仍须使用 D7 logical
   execution id 去重。
6. ACK 响应丢失时，重试相同 message_id + delivery_attempt 必须返回幂等 receipt。
7. 旧 run、旧 delivery_attempt、已取消 Agent 的 late ACK 使用稳定错误码拒绝并留审计。
8. 提供旧 DELIVERED 数据修复：默认转为 UNRESOLVED/OUTCOME_UNKNOWN 并等待核验，不能自动
   requeue，也不得迁移为 ACKED/completed。只有可证明未执行、明确幂等/纯读或用户显式决定
   才产生 typed requeue event。
9. S1 同时引入 delivery/provider 调用期间的最小 DB-clock LeaseKeeper。只有当前 Agent
   attempt 已原子判定 orphan 并完成 takeover 后，才评估 redelivery；慢但持续 heartbeat 的
   provider 不能因 delivery lease 超时被重复调用。S2 再把 keeper 与全 Agent scheduler、
   terminal/budget 事务统一。

### 7.3 故障注入

依次在 enqueue 后、deliver commit 前后、provider start 后、provider return 后、result
commit 后、ACK commit 前后、terminal 前强杀并用新进程恢复。

每个窗口必须满足：

- 消息最终得到一个可重建 result，或显式 unresolved/unknown。
- 不存在永久 DELIVERED 且 scheduler 报 no_more_messages 的组合。
- 重复 delivery 不产生重复状态提交或未经过 ledger 的重复外部副作用。
- stale run 的 late result 被 fence，当前 attempt 不被污染。

### 7.4 必测用例

- test_deliver_old_message_uses_mailbox_head_version
- test_delivered_unacked_message_is_recovered
- test_takeover_does_not_complete_agent_with_unacked_delivery
- test_result_recorded_before_ack_recovers_without_provider_recall
- test_stale_run_cannot_deliver_ack_or_cancel_message
- test_ack_retry_returns_same_receipt
- test_redelivery_increments_attempt_and_preserves_sequence
- test_cancelled_message_cannot_be_redelivered
- test_mailbox_rebuild_matches_live_projection
- test_legacy_delivered_after_effect_before_ack_is_not_auto_requeued
- test_slow_live_provider_is_not_redelivered_during_delivery_lease

### 7.5 完成门

- D11 原有 mailbox/去重/重启测试全绿。
- 上述所有 kill window 在新进程恢复后通过。
- 事件重放结果与 live projection 完全一致。
- 无静默 redelivery；CLI/status 能看到次数、原因和当前 owner。

## 8. S2：D11 spawn、lease、result 与 budget 原子化

### 8.1 spawn 事务

当前“先查父状态与 children，再写 child/budget”的方式不能防并发。目标事务为：

~~~text
read parent + parent-capacity + root-budget
  -> append_batch(
       parent: child-spawn-authorized,
       parent-capacity: slot-reserved,
       root-budget: agent-budget-reserved,
       child-agent: agent-created
     )
~~~

- parent stream 的 expected version 把 active/run/depth 判断封进事务。
- dedicated parent-capacity stream 负责每父并发 slot，不能靠全图扫描计数。
- root total budget 和 parent slot 与 child create 同 commit；任一 CAS 冲突都重新读取全部
  projection，不只刷新 root budget。
- parent terminal 与 spawn 同抢 parent stream version，只能一个成功。
- semantic spawn idempotency key 返回同一 child receipt，不重复 reserve。

### 8.2 terminal 与预算结算

Agent terminal、parent slot release、root budget release、result reference 和必要的 parent
message enqueue 必须使用一个 append_batch。事务响应丢失后按 idempotency receipt 查询，
不得再次 release。

若 result 过大，只持久化有界脱敏摘要与 content digest；完整 artifact 走受控 artifact
store，事件保存 result_ref。provider 的真实 outcome/error 不能被固定 completed 覆盖。

### 8.3 LeaseKeeper

1. provider 调用期间运行后台 LeaseKeeper，heartbeat 间隔不超过 lease/3。
2. heartbeat 使用数据库时钟、agent_id、attempt、run_id 与 stream version。
3. heartbeat CAS 失败或发现已接管时，旧 worker 停止获取新工作；无法取消的 provider 可以
   结束，但结果提交必被 fence。
4. discover_orphans 只接管超过 lease 且没有更新 heartbeat 的 attempt；接管产生新 attempt、
   fresh run_id 和审计事件。
5. close/cancel 必须收束 keeper 线程；ResourceWarning 和线程泄漏视为失败。

### 8.4 必测竞态

- 两线程 barrier 同抢最后一个 per-parent slot，只能一个成功。
- 两线程同抢最后一个 root total slot，只能一个成功。
- parent terminal 与 child spawn 竞态，只能形成“child 已原子创建”或“terminal 成功且无 child”。
- terminal commit 与响应丢失，恢复后 budget/slot 恰好 release 一次。
- 慢 provider 运行超过 lease，持续 heartbeat，不被误接管。
- worker 真正停止 heartbeat 后可 takeover；旧 provider late result 被 fence。
- 双 takeover 竞争只产生一个新 attempt。

### 8.5 完成门

- active child、root budget、parent capacity 都能只从事件重建。
- 任意 crash window 后不存在孤儿 reserve、负预算、超限 child 或双 terminal。
- provider outcome/result/error 与持久化终态一致。
- D11 全套 control/scheduler 测试和新增并发测试连续 100 轮无 flake。

## 9. S3：D6 checkpoint、EventStore、配置与 durable JSON 真相边界

### 9.1 checkpoint v2

checkpoint v2 至少包含：

- checkpoint_schema_version
- reducer_name 与 reducer_version
- source stream identity、covered version、covered event id/hash
- canonical projection digest
- 有界 projection payload
- created_at 与可选 previous checkpoint reference

保存 API 不再接受任意调用者拼出的 context。projection 必须来自 canonical reducer。
恢复时的正确性优先级如下：

1. 校验 JSON 字节、深度、节点、类型与 schema version。
2. 校验 source stream、covered version 和 event hash。
3. 用覆盖 events 重新归约 canonical projection，比较 digest 和结构。
4. 任一不一致则把 checkpoint 当 cache miss，完整 replay；若 event log 本身损坏则 fail closed。
5. 从 replay 得到的结果必须与“完全不使用 checkpoint”逐字段一致。

短期允许为验证而回放至 checkpoint，牺牲启动性能；在正确性稳定后再设计受证明的 hash chain
增量优化。

旧 checkpoint v1 一律按 cache miss 处理并从 event log 重建，不原地信任，也不修改历史 event。

### 9.2 EventStore 边界

1. 字段级 typed DTO 在 digest 和 event 构造前完成限量、canonicalization 与必要脱敏；
   EventStore 最底层只做 UTF-8 总字节、深度、节点、字符串、数组、对象、单字段和 schema
   验证，发现不合规就拒绝，绝不静默改写已经形成的业务事实。
2. canonical user input、resume response、interrupt prompt、assistant/tool/MCP result、summary、
   checkpoint、trace 全部使用相同 primitive limits，可按对象类型选择更小 profile。
3. 所有自由文本入口在业务 DTO 构造前先限量与凭据形态脱敏，得到 canonical 文本；首轮
   执行、hash、action digest、event 与恢复都用 canonical 值。不能保存 raw hash 作为低熵
   secret 的替代；幂等性另绑定 request id。
4. 任何 limit+1 必须在事务写入前稳定失败，不允许部分 stream 已提交。
5. 消除 recovery 对 SQLite 私有表的 raw INSERT/UPDATE；恢复事实也通过 typed event 和
   append_batch 写入。
6. CheckpointStore 依赖 EventStore/projection 接口，不依赖具体 SQLite 类型。
7. 安全审计必须进入业务/audit event；trace 只是辅助，不可作为唯一证据。

### 9.3 runtime config 边界

load_runtime_config 在解析前有文件总字节上限，解析时拒绝 duplicate key、非有限数字、
过深/过多节点和未知字段。解析后至少限制：

- system/developer prompt 字节数；
- provider options 的键数量、名称和值类型，并使用正向 allowlist；
- test profiles、MCP servers、每 server argv/env、policy rules 的数量；
- model_rounds、max_tool_calls、并发、预算和 timeout 的安全上下界；
- 任意自由文本、URL、路径、server/tool 名的长度；
- config error 只返回稳定 code/字段路径，不回显 credential-like 原文。

MCP env 不能充当通用 secret 容器；secret-like 名称和值、provider key 变量均拒绝。config
失败发生在 durable RUNNING 和子进程 spawn 之前。

### 9.4 旧库与原始文本迁移

禁止在原数据库中 UPDATE 历史 event payload，因为这会破坏 event hash 和审计真相。旧库
采用显式分流：

1. schema 检测到可能保存 raw durable text 的旧版本时，production mode 拒绝直接打开。
2. 提供离线 export-to-fresh-store：从旧 event 归约允许迁移的 terminal 元数据，经过当前
   canonical sanitizer 写入全新的事件库，并写 legacy-store-imported.v1 来源 digest。
3. active/nonterminal execution 不带原始上下文续跑，迁移为 requires_manual_restart；不能用
   脱敏后语义不同的历史冒充原执行。
4. 原 DB、WAL、SHM、freelist 和备份视为可能含 secret 的受限遗留介质，给出隔离、访问控制
   与人工保留/销毁说明；Agent 不自动删除。
5. canary 发布扫描针对 fresh sanitized DB 及同目录 WAL/SHM/temp/backup。只扫逻辑列不足以
   证明物理页无原值。

### 9.5 必测用例

- test_checkpoint_fabricated_context_is_rejected_and_replayed
- test_checkpoint_counter_phase_and_pending_calls_match_reducer
- test_checkpoint_v1_is_cache_miss_not_trusted
- test_checkpoint_limit_plus_one_writes_nothing
- test_event_payload_depth_nodes_and_bytes_are_bounded
- test_canonical_user_input_is_identical_on_first_run_and_restart
- test_uninterrupted_and_kill_resume_model_request_context_are_identical
- test_event_store_rejects_noncanonical_payload_without_rewriting_it
- test_user_resume_prompt_and_summary_are_redacted_before_persist
- test_recovery_uses_public_event_store_transaction
- test_rebuild_with_and_without_checkpoint_is_identical
- test_legacy_store_requires_offline_sanitized_export
- test_sanitized_export_scans_db_wal_shm_and_backup_canaries
- test_config_file_bytes_duplicate_keys_depth_and_nodes_are_bounded
- test_config_collections_prompts_rounds_tools_and_options_are_bounded
- test_config_error_does_not_echo_secret_value

SQLite 扫描测试要把 canary credential 分别放入 user input、resume、prompt、assistant、
tool/MCP result；所有持久文本列不得出现原值。

### 9.6 完成门

- 伪造 context/phase/counter/pending calls 即使 coverage hash 合法也不能被接受。
- full replay 与 checkpoint replay 对所有状态逐字段相同。
- 所有 durable JSON 在边界值成功、limit+1 稳定拒绝且无部分事务。
- 首轮、kill/restart 与 full replay 使用完全相同的 canonical user input。
- config 的 limit/limit+1、duplicate-key 与错误脱敏测试全绿，失败不创建 durable RUNNING
  或 MCP 子进程。
- fresh/sanitized store 的 DB/WAL/SHM/临时备份均不含 canary；旧介质被明确隔离而非伪称已清除。
- D1、D6、D7、D13、D16、D21 相关回归全绿。

## 10. S4：D10 MCP 安全、deadline、identity 与生命周期

### 10.1 execution profile 与最小子进程环境

删除 dict(os.environ) 继承方式，复用一个受审计的 subprocess environment builder：

- 默认 profile 是 sandboxed：使用 D8 等价的进程/容器边界，immutable image、network none、
  非 root、read-only root、capability drop、CPU/内存/PID/time 限制；不挂用户工作区，确需
  文件时只挂显式批准的最小只读路径。stdio 仍由 host controller 有界转发。
- host_trusted 是显式例外：管理员信任 executable/process 具有宿主用户权限；Schema/Policy/
  Ledger 只约束 protocol action，不能阻止进程绕过协议直接读文件或联网。启用前必须有
  durable 管理员批准，绑定 resolved executable/argv 本地文件身份、execution profile、
  配置 digest 和资源范围。
- 每次 host_trusted spawn 前重新 canonicalize/no-follow 解析 executable 与 code-bearing
  argv 路径，核对文件 identity、content digest、argv、working directory、profile 和 config
  digest。任一漂移立即使旧批准失效并重新 ASK，且 spawn 调用数为零。
- TOCTOU 优先通过 controller 管理目录中的 content-addressed staged copy、受限 ACL/权限和
  spawn 前最终 digest 复验收束；平台能提供稳定文件句柄身份时可使用等价方案。不能证明
  approval identity 与将执行内容一致时 fail closed。运行时动态库/依赖仍属于 host_trusted
  管理员信任范围，不伪称完整供应链隔离。
- spawn/connect 本身先经过 config preflight、execution-profile policy、approval 和
  allocation ledger；未批准时进程调用数必须为零。不能先启动 server、拿到 catalog 后才问
  是否信任该 process。
- 默认只含平台启动必需项；Windows 可含显式解析后的 SystemRoot、ComSpec、临时目录等，
  POSIX 只含固定 locale/临时目录等。
- executable 在装配时解析为绝对可信路径，尽量不依赖把完整 PATH 传给 child。
- server 配置只能声明显式非敏感 env allowlist/value；secret-like 名称和值、重复键、
  provider key 变量默认拒绝。
- 当前不向 MCP child 注入 credential value，也不允许用配置明文绕过。
- 未来 credential reference 只能交给受控 broker/connector，或在可信进程并具有强制 egress
  边界后另立切片；不能仅把 reference 解引用后直接塞进 child env。
- 同步审计 Git、worktree、Docker subprocess，删除不必要的完整环境继承。

本轮 production-ready 只允许默认 sandboxed fixture，或有明确 durable 批准的
host_trusted server。最小环境减少凭据暴露，但不被当成 host_trusted 进程的 OS containment。

### 10.2 deadline 配置迁移

拆分为：

- process_start_timeout_seconds
- initialize_timeout_seconds
- tools_list_timeout_seconds
- tool_call_timeout_seconds
- io_poll_timeout_seconds
- shutdown_timeout_seconds

旧 request_timeout_seconds 在一个兼容版本中只映射到 tool_call_timeout_seconds，并给出
deprecation event/doctor 提示；startup/initialize 使用新的安全默认值。不得继续让短调用
deadline 意外杀死冷启动。

非幂等 tool call 超时/EOF 仍进入 OUTCOME_UNKNOWN，不盲重试。启动或 list 在确认没有
tool side effect 前可按有界策略重连，但必须审计。

### 10.3 identity 与恢复

Ledger 的 prepare、claim、load_for_call、recovery lookup 使用同一个 typed
LogicalExecutionIdentity，其中显式包含 binding_digest。禁止一处计算 digest、另一处用
缺字段 tuple 查找。refresh 后旧 generation/schema 的 approval、claim 和 recovery receipt
都不能平移到新 binding。

### 10.4 生命周期与限量

1. StdioTransport.close 支持未 open、open 失败、重复 close，且进程/管道/线程全部收束。
2. close 是终局；refresh 线程不能在 close 后把状态写回 READY。
3. stderr 达到保留上限后继续排空但丢弃超限内容，设置 stderr_truncated；不能停止读取导致
   child 堵塞。
4. AssembledRuntime/AppRuntime 实现 close 和 context manager；装配后段失败立即回收已经
   连接的 MCP session/client。
5. pending request 数、tools/list 页数、重复 cursor、通知速率、refresh 线程数有硬上限。
6. list_changed 使用 single-flight 合并刷新，不为每条通知创建无界线程。
7. inbound/outbound JSON 都执行帧字节、深度和节点限制。

### 10.5 必测用例

- test_mcp_child_receives_only_allowlisted_environment
- test_sandboxed_mcp_cannot_read_host_file_credential_directory_or_network
- test_host_trusted_mcp_requires_identity_bound_durable_approval_before_spawn
- test_replaced_host_trusted_executable_invalidates_approval_before_spawn
- test_restart_with_mcp_config_or_argv_drift_requires_new_approval
- test_host_trusted_staged_copy_closes_path_replacement_window
- test_denied_mcp_start_has_zero_process_calls
- test_provider_and_cloud_secret_canaries_never_reach_child_or_sqlite
- test_startup_and_tool_call_timeouts_are_independent
- test_slow_startup_can_succeed_with_short_call_timeout
- test_process_start_deadline_has_phase_specific_error_and_cleanup
- test_initialize_deadline_is_independent_and_cleans_started_process
- test_tools_list_deadline_is_independent_and_closes_pending_requests
- test_io_poll_deadline_does_not_replace_request_deadline
- test_shutdown_deadline_forces_bounded_cleanup
- test_each_mcp_deadline_accepts_boundary_and_rejects_limit_plus_one
- test_non_idempotent_call_timeout_is_outcome_unknown
- test_binding_digest_is_used_by_prepare_claim_and_recovery
- test_close_before_open_after_failed_open_and_twice
- test_close_wins_refresh_race
- test_large_stderr_is_drained_and_truncated_without_deadlock
- test_assembly_failure_closes_connected_mcp_sessions
- test_refresh_storm_is_single_flight_and_bounded
- test_duplicate_cursor_and_pending_limit_fail_closed

### 10.6 完成门

- 父进程设置 provider key、云凭据和随机 canary 后，fixture server 只能看到精确 allowlist。
- sandboxed fixture 在 initialize 之前也无法读取宿主 canary 文件、credential 目录或建立
  网络连接；host_trusted 例外必须有 identity-bound durable approval 和可见风险提示。
- host_trusted executable/argv/config 在批准后或重启后发生漂移时，旧批准失效、进程调用数
  为零；只有 staged/复验后的已批准内容可以 spawn。
- D10 专项在 Windows/Linux 各连续十次无 timeout flake、无遗留进程/线程/管道。
- process start、initialize、tools/list、tool call、I/O poll、shutdown 六个 phase 都有独立
  deadline、phase-specific 稳定错误、边界/超界和资源收束证据；缩短一个 phase 不改变其他
  phase 的行为。
- request identity 在正常、ASK resume、refresh、crash recovery 中一致。
- D8/D9/D10/D15/D21 聚焦回归全绿，ResourceWarning 视为错误。
- 同步修正文档中“stderr 超限后停止读取防阻塞”的反向描述。

## 11. S5：统一运行时、workspace 与 telemetry 端到端一致

### 11.1 Unified runtime

- UnifiedAgentRuntime 的响应必须来自重建后的 durable receipt/state，禁止 hard-code completed。
- 成功返回前，Turn terminal、Thread detach、Run terminal 和必要 evidence 必须原子或可验证
  收敛；否则返回真实 waiting/running/failed/unknown。
- status/resume/cancel 在新进程重开同一 DB 后必须得到相同状态。
- resume 只允许文档定义的 WAITING/PAUSED/ORPHANED 等状态；TIMED_OUT 进入完整 terminal
  集合，不被误当 active。
- doctor 默认只读。若必须执行写探针，使用隔离临时数据库、完整 close，并断言不污染用户
  Thread/Turn/Run。

### 11.2 workspace 外部效果

worktree add/remove、artifact apply/retest/deliver 都是外部效果，不能用“先做效果再随便记
event”表示成功。统一采用：

~~~text
INTENDED -> CLAIMED -> APPLIED | FAILED | OUTCOME_UNKNOWN
~~~

- allocation intent 先于 git worktree add；崩溃恢复按 exact path、repo identity、nonce 核验。
- reap 先 claim exact inventory，再删除；删除后验证资源缺失再记 APPLIED。无法证明则 UNKNOWN，
  不写虚假 reaped。
- artifact deliver 前验证 user HEAD、dirty 状态、base、diff digest、evidence、image digest 和
  run fence；投递后重新核对 content identity。
- 未提交修改不能称为新的 HEAD。artifact contract 改为 base_commit +
  working_tree_content_digest/diff_digest；需要兼容读取旧 head 字段，但不再误用它证明内容。
- 部分 apply 或中断后不能自动覆盖用户文件；记录 workspace_outcome_unknown 并要求人工检查。

### 11.3 telemetry 隔离

- security/business audit event 必须跟业务事务写入，不依赖 trace。
- TraceStore 使用专用串行 append 或有界 CAS retry。
- trace 写失败不能在 ledger claim 后推翻业务结果；失败记 counter/diagnostic，并在 status 中可见。
- trace payload 仍在落盘前 allowlist、限量、脱敏；不能把 business raw payload 作为重试缓存。

### 11.4 必测用例

- test_unified_response_matches_rebuilt_turn_thread_and_run
- test_doctor_leaves_no_active_probe_or_resource
- test_status_resume_cancel_are_restart_consistent
- test_timed_out_is_terminal_everywhere
- test_worktree_add_crash_is_reconciled_from_intent
- test_reap_event_never_precedes_verified_deletion
- test_artifact_delivery_uses_content_digest_not_base_head
- test_delivery_partial_failure_is_workspace_outcome_unknown
- test_trace_cas_failure_cannot_break_claimed_business_operation

### 11.5 完成门

- 对外成功、durable terminal、evidence 和外部 workspace 事实四者一致。
- 每个 workspace crash window 都能恢复、fail closed 或显式 unknown，没有孤儿被静默忽略。
- doctor、装配失败、cancel 和正常退出均无遗留 active state、进程、worktree 或线程。
- D1/D4/D5/D6/D7/D12/D14/D15/D16/D22 聚焦回归全绿。

## 12. S6：schema migration、生产故障注入与容量

### 12.1 数据库与事件迁移

1. 引入数据库 schema version，例如 SQLite PRAGMA user_version 或等价 metadata 表。
2. migration 只能 forward-only、事务化、可重复检测，不改写历史 event payload。
3. 每个 migration 包含：from/to version、前置检查、备份建议、事务、post-check 和稳定错误码。
4. event schema 保持 append-only；新增 v2 event 时 reducer 同时支持旧 v1，直到明确兼容窗口结束。
5. 测试从当前真实 schema 副本升级，而不是只测空库。
6. 针对旧 checkpoint v1、旧 DELIVERED、旧 MCP timeout 配置和旧 artifact head 提供明确兼容策略。
7. 新二进制打开更高未知 schema 必须 fail closed，不自动降级。
8. 含 raw durable text 的旧库不做原地 scrub；只允许 S3 定义的 fresh sanitized export，
   并把原 DB/WAL/SHM/备份留在受限遗留介质流程中。

### 12.2 FaultInjector 接入生产点

把 D14 的命名注入点接入：

- mailbox enqueue/deliver/provider/ack/terminal
- spawn reserve/create、terminal/release
- lease heartbeat/takeover/late result
- checkpoint save/load/replay
- ledger prepare/claim/effect/result
- MCP process/initialize/list/call/reconnect/close
- worktree allocate/add/apply/retest/deliver/reap
- Unified terminal/detach/response

同一 seed/script 必须产生同一稳定失败分类和等价事件序列。注入逻辑只在明确测试配置启用，
生产默认关闭，不能改变正常路径时序。

### 12.3 容量与性能目标

先以可测工作负载建立基线，再在不改变语义的前提下优化：

- 10,000 条单 stream event 的 rebuild/checkpoint。
- 1,000 个 Agent、每 Agent 100 条 message 的 list/wait/delivery。
- 100 个并发 spawn 竞争固定 parent/root budget。
- 1,000 次 lease heartbeat/takeover 循环。
- 100 个 MCP pending request 与 refresh storm 的上限行为。

初始发布阈值建议在 CI 标准机上采用：

| 操作 | 建议阈值 |
| --- | --- |
| 已有 checkpoint 的 10k event runtime rebuild | p95 < 1 秒 |
| 单 Agent 下一条 mailbox 查询 | p95 < 100 毫秒 |
| 无竞争 spawn transaction | p95 < 100 毫秒 |
| wait_agents 100 个活跃 Agent 轮询周期 | p95 < 250 毫秒 |
| 24 小时 soak | 无预算漂移、无 stuck DELIVERED、无线程/句柄持续增长 |

阈值应在 S6 基线测量后校准并记录硬件，不为过门而放宽到无约束。

优先增加按 parent/status/sequence 的可验证 projection/index，消除全流扫描；projection 可重建，
仍不能成为事实源。

### 12.4 完成门

- current schema → next schema、重复启动、迁移中断、未知高版本全部有测试。
- 所有命名 crash point 可在新进程恢复并满足 no loss/no silent duplicate/unknown explicit。
- 性能阈值写入自动测试或可机读 benchmark，soak 无资源单调增长。
- 大模块只在当前切片需要时按状态机/存储/I/O 边界拆分，不做无验证的大爆炸重构。

## 13. S7：跨平台发布验证

### 13.1 CI 分层

| Lane | 环境 | 必跑内容 | Skip 规则 |
| --- | --- | --- | --- |
| PR-fast | Windows + Linux，Python 3.12 | 离线 unit/contract/security/eval，ResourceWarning=error | 仅明确平台不适用项 |
| PR-integration | Windows + Linux | SQLite 多线程/多进程 race、kill/restart、Git/worktree、checkpoint | 核心持久化不得 skip |
| MCP mandatory | Windows + Linux，真实 fixture process | initialize/list/call/refresh/timeout/unknown/env/lifecycle/ledger | MCP 核心 0 skip |
| Linux-Docker mandatory | Linux + 真实 daemon + immutable image digest | network none、只读 mount、非 root、symlink/hardlink、OOM、timeout、cancel、reaper | 0 个意外 skip |
| Golden mandatory | Linux，fresh repo/DB + Docker + MCP fixture | 完整主链、approval resume、child takeover、workspace delivery、kill/restart | golden 场景 0 skip |
| Nightly-soak | 固定标准机 | 长流、并发 mailbox/spawn、lease、反复恢复、资源监测 | 不允许静默超时 |
| Provider opt-in | secrets-enabled 手工或定时环境 | 真实 provider smoke、多轮会话、empty completion 兼容 | PR 可不跑，发布前必须有日期证据 |

建议支持 Python 3.12、3.13、3.14；至少 3.12 是每个 PR 的强制版本，其余版本按 CI 容量设置
强制或 nightly。

### 13.2 最终 golden E2E

先完整保留路线图的强制 E2E 矩阵，不能用若干模块测试替代：

1. 单 Agent 成功修改并测试。
2. 首次测试失败，模型有界修复后成功。
3. 坏模型流或缺 terminal 时工具执行次数为零。
4. 安全 checkpoint 后 kill，新进程发现并恢复。
5. 工具结果不确定时进入 OUTCOME_UNKNOWN，不盲重试。
6. 审批 allow、deny，以及批准后参数/资源漂移重新 ASK。
7. Docker 路径、网络、资源和 secret 隔离。
8. 真实 MCP fixture 完成 initialize/list/call/close。
9. 两个写 Agent 的独立 worktree 修改不同文件并成功集成。
10. 两个写 Agent 修改同行时进入显式 artifact conflict。
11. 恶意仓库文本/MCP 输出不能扩大 action 权限、绕过 system/developer 控制或产生未授权
    transport 调用；本断言属于 action layer，不宣称模型 jailbreak 免疫。
12. 旧 Worker/旧 Agent 迟到结果被 fence。
13. 全进程重启后 state、checkpoint、ledger、trace、artifact/diff 都可查询并与事件重建一致。

在 fresh repo 和新数据库中执行：

1. run → model → read/search → red test。
2. apply_patch → green test → git status/diff → evidence finalize。
3. ASK approval 中断并由新进程 approve/resume。
4. child Agent enqueue/deliver/provider/result/ack，期间至少一次 kill/takeover。
5. MCP read-only call，验证 binding/ledger；非幂等 timeout 进入 OUTCOME_UNKNOWN。
6. Docker immutable image 中重测，验证 network none 和资源限制。
7. integration worktree retest，user HEAD gate 后投递。
8. 最终从 Event Store/ledger/workspace 重建，不使用模型 final 自证。

另有一条不可拆散的 golden composite：在同一个 durable Turn 中贯通 Policy/Approval →
atomic Ledger claim → 真实且 sandboxed 的 MCP fixture → 只读与写子 Agent → 两个独立
worktree/Docker → 一次测试失败与 repair → OS 级 subprocess kill → 新进程 resume →
evidence final。kill 必须真正终止进程，不能只销毁 Python object。

再单独执行 dispatch-contract E2E，证明 built-in、MCP、subagent 三类入口都不能绕过
Schema/Registry → Policy/Approval → Ledger → Sandbox/Transport；分别通过的模块测试不能
替代这条接线断言。

golden 要有正常链和至少以下失败链：

- provider empty completion：零输出时最多重试一次并审计；partial output 后不重试。
- approval drift：执行前 re-resolve，digest 变化再次 ASK。
- stale Agent late result：稳定 fence。
- kill after effect before result：显式 UNKNOWN，不盲重试。
- user workspace drift：投递 fail closed，不覆盖。
- Docker OOM/timeout/cancel：稳定状态、exact reaper、无宿主逃逸。

### 13.3 发布门

只有全部满足才能恢复 production-ready：

1. Windows/Linux 全量测试连续三次 failures=errors=0。
2. Docker、MCP、golden 每一类都必须在指定 mandatory lane 完整 discovery、核心测试零
   skip 且全部通过。平台不适用项只有在逐测试 approved-skip 映射到另一个实际执行并通过的
   lane 时才允许。
3. P0/P1 登记册为零；独立审查没有开放 P0/P1。
4. ResourceWarning、遗留进程、线程、句柄、worktree 为零；active Run/Turn/Agent、
   stuck DELIVERED/REQUEUED claim、未结 ledger claim、pending approval、过期 lease 和
   workspace inventory 均为零或逐项匹配发布场景明确允许的 durable 状态。
5. current-to-next migration 在真实 schema 副本上通过。
6. 所有 crash window 满足 no loss、no silent duplicate、unknown explicit。
7. fresh/sanitized SQLite、WAL、SHM 与临时备份 canary 扫描无 credential 原值或完整环境；
   遗留库按隔离策略明确标识，不能混入生产 release artifact。
8. 长流/并发/soak 达到 S6 阈值。
9. fresh machine 按文档可确定性完成离线 demo；真实 provider 证据必须由最后一次相关代码/
   配置变化后的精确 release commit 与 build artifact 产生，绑定 commit、artifact、provider、
   model、config digest、日期和明确 PASS 断言。任何相关变化都会使旧证据失效并要求重跑。
10. 路线图、切片文档、错误码表、配置示例和实际代码一致。

## 14. 全局测试矩阵

| 维度 | 必须验证 |
| --- | --- |
| 正常路径 | 每个状态机从创建到 terminal，可由事件重建 |
| 版本竞争 | stale version/run/attempt/claim/delivery 全部稳定拒绝 |
| 原子事务 | 多 stream 全成或全不成；响应丢失可用 receipt 幂等查询 |
| 崩溃窗口 | 每个外部效果和每个双提交边界前后 kill/restart |
| 不确定结果 | 明确 UNKNOWN/UNRESOLVED，不自动重复非幂等效果 |
| 安全 | DENY + transport=0 + 无 succeeded + canary 不落库 |
| 资源 | 字节/深度/节点/数量/时间/并发/内存/CPU/进程/线程全有 limit+1 测试 |
| 恢复 | 新进程重开 DB 后 Thread/Turn/Run/Agent/Message/Budget/Ledger 一致 |
| 平台 | Windows 路径/junction；Linux symlink/hardlink/Docker/mount |
| 可观测性 | 自动动作有 typed audit；trace 丢失不能改变业务事实 |
| 用户工作区 | dirty/drift/partial failure fail closed，不覆盖用户改动 |
| 文档 | 行为合同、稳定错误码、配置 schema、示例与测试一致 |

全量命令以项目 AGENTS.md 为准，在 v2/ 运行：

    $env:PYTHONPATH = 'src'
    python -W error::ResourceWarning -m unittest discover -s tests -v

聚焦测试不能代替全量门；InjectedContainerRunner 不能代替 Linux-Docker mandatory 证据；
scripted provider 不能代替发布前的 opt-in 真实 provider 兼容证据。

## 15. 实施与提交纪律

每个 S 切片遵守相同顺序：

1. 更新该切片 issue 清单与行为合同。
2. 写确定性反例，使测试以预期原因失败。
3. 只实现当前切片，不创建未来空模块。
4. 增加 migration/兼容读取，不能改写历史 event。
5. 跑聚焦 unit/contract/concurrency/fault tests。
6. 跑受影响的跨切片 integration/security/eval。
7. 跑全量 unittest，ResourceWarning 作为错误。
8. 检查进程、线程、临时 worktree、active state 与 SQLite canary。
9. 更新路线图、切片文档、错误码和配置示例。
10. 独立审查 P0/P1 后才把该 Stability Gate 标为 COMPLETE。

一次提交尽量只关闭一个问题族。事件 schema、数据库 migration、运行时行为和文档更新应在同一
可回滚提交序列中出现，避免代码已经写 v2 event 而 reducer/文档仍只认识 v1。

## 16. 回滚与停止条件

以下任一情况出现时停止进入下一切片：

- 修复需要削弱 exact-version、run fence、policy/ledger 或 UNKNOWN 语义。
- migration 需要删除或改写历史 event 才能工作。
- 无法证明外部 workspace/container/MCP 效果终态，却准备记为成功或失败。
- 为让 CI 变绿而扩大 timeout、删除断言、增加无到期条件的 skip。
- 引入完整宿主环境、credential value、隐藏推理或无界正文持久化。
- 新增 P0/P1 没有 owner、反例测试和目标切片。
- 聚焦测试通过但全量回归出现未解释的新失败、skip 或资源泄漏。

回滚优先关闭新入口或回到旧读取兼容模式；不得 git reset 用户工作区、删除 event log 或用
checkpoint 覆盖事实。

## 17. 完成定义

本规划本身完成只表示“计划已对齐现有文档与当前证据”。项目稳定化完成必须同时满足：

- S0–S7 全部按各自门禁关闭。
- D6/D10/D11/D15 的 REOPENED 问题已关闭，D12/D14 完成强制环境复验。
- Event Store、ledger、workspace 实际事实与所有公开 API 返回一致。
- 无开放 P0/P1，无未分类 skip，无静默 redelivery/takeover/fallback/retry。
- 正常、失败、并发、崩溃、迁移、安全、容量和跨平台证据均可重复。
- 路线图再根据新证据恢复 COMPLETE/production-ready，而不是根据历史状态自动恢复。

在此之前，项目的准确定位是：

> 已实现较完整的 coding-agent 架构与强单 Agent 核心，正在进行多 Agent 并发、恢复真实性、
> MCP 安全隔离和端到端发布证据的稳定化。
