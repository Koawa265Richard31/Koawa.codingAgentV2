# D25：第三方 MCP 沙箱安全治理（Third-party MCP Sandbox Governance）

版本：v1.0（实施定稿，W0 已冻结）。日期：2026-09-02。  
状态：**已批准，W0 完成；W1 起进入实现**。D24 前置门五项已于 2026-09-02 全部核验（见 §1.3）。

变更记录：v0.1 → v1.0：W0 开工门核验完成并冻结基线；reference server 由维护者选定
`@modelcontextprotocol/server-filesystem`（单选，不并做 time）。

## 0. 结论与定位

D25 将把当前“`sandboxed` 配置存在但启动必然报
`mcp_sandbox_unavailable`”的占位合同，落成一个可运行、可审计、可恢复的第三方 stdio MCP
容器执行面。它不是为了做插件市场或生产发布认证，而是用一条真实的第三方代码执行链证明：

> 不信任的 MCP 服务端可以提供真实工具能力，但它获得的进程、文件、网络、资源与生命周期
> 权限均由 Koawa 的可信控制面决定；协议内返回内容不能扩大协议外权限。

本切片目标是**本地工程级可用**，不是一次性 demo，也不追求 SaaS 产品级运营能力：

- 必须能运行至少一个镜像内置、无需宿主机权限的真实 stdio MCP server；
- 必须覆盖启动、握手、列工具、调用、取消、超时、关闭、崩溃恢复和清理；
- 必须有 Docker 真实集成证据及恶意 server 对抗证据；
- 不做 24 小时 reference soak，不做生产发布资格认证，不做插件商店、远程 MCP 或 UI 管理台。

## 1. 开工门与基线冻结

### 1.1 D24 前置门

D25 开工前必须同时满足：

1. D24 W1–W6 全部完成，D24 文档和 README 的完成声明已提交；
2. `git status --porcelain` 中没有归属不明改动；并行会话产物已由其所有者提交或明确保留；
3. 记录 `D25_BASE_COMMIT=<D24 最终提交>`，后续证据均带该基线；
4. 在该提交上从 `v2/` 执行一次全量测试并归档结果；
5. Docker doctor 通过，并确认使用的是本机 Docker Desktop 对应引擎，而不是误用独立 WSL
   发行版中的另一套 daemon。

若任一项不满足，D25 状态保持 `BLOCKED_BY_D24`，不得一边修改 D24 同一文件一边启动 D25。

### 1.3 W0 开工门核验记录（2026-09-02，全部满足）

1. **D24 完成**：W1–W6 已提交，最终提交 `a82d1ccc1446b5ca3af7f977d7419c0d3a94e461`，README/day-24 文档完成声明在案。
2. **工作树归属**：tracked 文件零未提交改动；未跟踪的 `scratch_*.txt`/`app_part*.txt`/`.dsh_tmp/` 为并行会话产物，**保持不动、不提交、不清理**（遵守 §7 归属纪律）。
3. **D25_BASE_COMMIT = `a82d1ccc1446b5ca3af7f977d7419c0d3a94e461`**；后续全部证据带该基线。
4. **全量回归归档**：该提交上 `.dsh_tmp/i9-lanes/pr-fast-d24.json`（933 discovered / 911 passed / 0 failed / 0 errors / 22 env skips，ok:true，894s，ResourceWarning=error 内建）。
5. **Docker 身份确认**：doctor 可达；daemon = `docker-desktop`（Docker Desktop），context = `desktop-linux`，Server 29.6.1——非 WSL 发行版独立 daemon（§1.1 第 5 项的"误用"排除成立）。doctor 对不存在镜像返回 `sandbox_image_unavailable`（预期路径，非 daemon 不可用）。

**Reference server 决策（维护者 2026-09-02）**：单选 `@modelcontextprotocol/server-filesystem`（Node/TS）。W5 必须入档：项目名、固定 commit/版本、来源 URL、许可证、制品 hash、最终 image digest；floating tag / 无法钉来源即候选作废。自制 fixture 仍独立存在用于对抗矩阵，不替代第三方证据。

**D25 完成门清单与证据目录**：完成门 = §10 全部条目；证据目录命名 `docs/stability/d25-evidence/`（实施期创建），JSON-only、不含宿主环境 dump。

### 1.2 已核实的当前代码事实（2026-09-02）

| 现有能力 | 可复用部分 | D25 缺口 |
|---|---|---|
| `mcp/activation.py` | 激活身份、单次 ticket、intent/claim、精确版本事件、UNKNOWN 与恢复 | 沙箱容器身份尚未绑定进 MCP allocation |
| `mcp/launcher.py` | `McpProcessEndpoint` 与 host-trusted launcher | `SandboxedLauncher.launch()` 消费 ticket 后固定失败 |
| `mcp/transport.py` | 长驻 stdio、帧/队列/stderr 边界、关闭和超时 | 需要一个真正可交互的容器 endpoint |
| `sandbox/runtime.py` | Docker doctor、不可变镜像、资源限制、隔离参数、allocation ledger、reaper | 当前 runner 面向一次性 batch command，stdin 为 `DEVNULL`，不能直接承载 MCP |
| `runtime/config.py` | 已有 `sandboxed`/`host_trusted`、镜像、限制、只读挂载字段 | `cwd`/`command` 的宿主与容器语义未分开；沙箱 profile 校验不完整 |
| `runtime/assembly.py` | grant → intend → claim → ticket → launcher → transport 链 | 沙箱策略在预检阶段仍固定拒绝，默认 launcher 未注入 Docker 控制面 |

这些事实是实现起点，不允许通过复制一个绕过 activation、ledger 或 transport 的独立 Docker
脚本来“完成”D25。

## 2. 信任模型与支持边界

### 2.1 两条执行路径

| 路径 | 适用对象 | 默认策略 | 安全含义 |
|---|---|---|---|
| `sandboxed` | 未审计或普通第三方 stdio MCP | `mcp.use` 能力范围内可启用；仍受每个工具的既有策略/账本约束 | 真实容器边界，默认无网、无宿主挂载、无秘密 |
| `host_trusted` | 维护者明确审计并信任的本机 server | 每次启动保持 `ASK`，要求 `mcp.host_process.execute` | 明示逃生舱；拥有宿主用户权限，不得包装成“安全沙箱” |

生产配置中的 `execution_profile=None` 不再获得隐式 `allow`。它只可保留给明确标记的旧测试夹具；
文件配置或正常运行装配遇到 legacy profile 必须 fail closed，并给出稳定错误码和迁移提示。

### 2.2 D25 核心支持集

D25 完成时承诺支持：

- 本地 Docker 上的、镜像 digest 精确固定的、长驻 stdio MCP server；
- 镜像内置 executable 和依赖，容器内绝对 argv 与工作目录；
- 默认 `network=none`、只读 rootfs、非 root、drop all capabilities、no-new-privileges、
  有界 CPU/内存/PID/tmpfs；
- 无宿主目录挂载、无 secret 注入、最小固定环境；
- Windows + Docker Desktop 真实 lane，以及一个有实际价值的 Linux/WSL Docker lane；
- server 工具的 read-only/idempotent/non-idempotent 分类继续走既有 policy + tool ledger。

### 2.3 明确不声称支持

以下能力在 D25 中**显式拒绝**，而不是静默降级：

- 任意公网访问、域名 allowlist、代理认证或远程 HTTP/SSE MCP；
- 把仓库、用户目录、Docker socket、SSH agent、云凭据目录挂入第三方容器；
- 将 API key、token、密码或宿主完整环境传入不可信 MCP；
- 可写宿主挂载、特权容器、host network、host PID/IPC、额外 Linux capabilities；
- 自动下载 `latest` 镜像或接受 tag 作为已验证身份；
- 24h soak、生产 SLO、签名供应链/镜像透明日志、跨主机调度。

需要网络或秘密的第三方 MCP 不得借 `sandboxed` 名义运行；D25 后另立切片实现受控 egress/secret
broker，或由维护者明确选择 `host_trusted` 并接受风险。

## 3. 威胁模型与安全闭环

### 3.1 主要攻击者

1. 恶意或被供应链污染的 MCP 镜像；
2. 恶意 server 通过 stdout/stderr、通知风暴、大帧、挂起和子进程逃逸攻击宿主；
3. 被 prompt injection 诱导的模型，试图选择更宽的 profile、挂载、网络或环境；
4. 并发/崩溃造成的孤儿容器、重复启动、错误清理或 UNKNOWN 被误判成功；
5. 宿主上伪造 Koawa 标签的非本项目容器，诱导 reaper 越权删除。

### 3.2 必须闭合的链

每个安全声明都要按以下链条给出代码和测试锚点：

`受保护对象 → 策略决定 → 执行侧强制 → 可信证据 → 终止/隔离/恢复`

| 受保护对象 | 决策 | 强制点 | 可信证据 | 失败/恢复 |
|---|---|---|---|---|
| 宿主进程与系统调用面 | profile 选择 | Docker container，不允许 host fallback | `docker inspect` 的精确字段摘要/digest | inspect 不可用即不启动或 UNKNOWN |
| 宿主文件 | 默认不挂载 | 可信控制面生成 Docker argv | inspect 的 Mounts 必须为空 | 出现额外挂载立即 kill，标记身份不匹配 |
| 宿主网络 | 默认拒绝 | `--network none` | inspect `NetworkMode=none` | 不一致即启动失败并清理 |
| CPU/内存/PID | 固定上限 | Docker create 参数 | inspect HostConfig | 不一致即 fail closed |
| 启动授权 | grant + claim + one-time ticket | `SandboxedLauncher` 消费 ticket 后才允许 create | activation/allocation typed events | 重放 ticket 拒绝 |
| 外部副作用 | tool policy + ledger | `LedgerExecutor` | action/auth/result 一致性事件 | 不确定结果保持 UNKNOWN |
| 容器生命周期 | allocation 身份 | label + container id 双绑定 | MCP 与 sandbox 两条流交叉核对 | 只回收可证明归属的容器 |

## 4. 不可妥协的实现不变量

1. **绝不回退宿主启动**：Docker 不可用、身份不符或校验失败时，`sandboxed` 必须失败；不得调用
   `HostTrustedLauncher`。
2. **先记 intent，后 create**：容器外部副作用之前必须已有 typed event；所有 append 使用精确
   expected stream version。
3. **身份完整**：授权 digest 至少覆盖 execution profile、精确 image digest、container argv、
   container cwd、安全环境摘要、资源限制、挂载策略和 deadline。
4. **镜像不可变**：仅接受本地解析为 `sha256:<64 hex>` 的镜像身份；tag 只能用于 doctor/解析，
   不能进入已授权执行。
5. **模型不生成 Docker argv**：所有 Docker flag、label、网络/权限/资源参数由可信代码固定生成。
6. **容器内命令与宿主命令分义**：sandboxed 的 argv[0] 是容器内绝对 POSIX 路径，不做宿主
   `PATH` 查找或代码 staging；host-trusted 保留现有 no-follow/hash/stage 合同。
7. **零秘密基线**：D25 sandboxed 环境只允许固定无秘密白名单；事件、trace、错误、repr、
   external identity 都不得含环境值、完整 argv、stderr 正文或凭据。
8. **默认零挂载**：D25 核心的 `read_only_mounts` 必须为空；非空配置稳定拒绝，不能“看似 ro”
   却留下宿主 TOCTOU 窗口。
9. **PID 不代表容器**：attach 客户端 PID 只是传输句柄；授权身份必须使用 container id、allocation
   id、owner nonce 与 digest。
10. **UNKNOWN 不得美化**：无法证明容器是否启动、停止或工具副作用是否发生时，只能进入
    `OUTCOME_UNKNOWN`，不得自动重试非幂等调用。
11. **精确回收**：reaper 只有在 ledger 身份、全部受管 label 和 inspect 身份一致时才可 stop/rm；
    对未知、缺标签或篡改对象只报告，不删除。
12. **协议内容无授权力**：server name、tool description、result、notification、stderr 均是不可信数据，
    不得更改 profile、policy、approval、completion gate 或恢复结论。

## 5. 目标架构

```text
Runtime config (trusted local file)
        │ validate / canonical identity
        ▼
ActivationService ── grant → intend → claim → one-time ticket
        │                                  │
        │                                  ▼
        │                         SandboxedLauncher
        │                                  │
        │                 sandbox intent → docker create
        │                                  │
        │                   inspect exact contract → bind
        │                                  │
        │                     docker start -a -i
        │                                  ▼
        │                         DockerMcpEndpoint
        │                          stdin/stdout/stderr
        ▼                                  │
MCP allocation events ◄──── lifecycle ─────┘
        │
        ▼
McpSession → verified registry → policy/approval → ToolLedger → result
```

`DockerMcpEndpoint` 管理两种外部对象：Docker attach 客户端进程与容器。关闭成功必须同时证明
attach 句柄已收集、容器已停止并释放；只完成其中一个不算成功。

## 6. 工作分解（严格顺序）

每个工作项完成后独立提交；后项不得通过预建空模块绕过前项。简单机械实现可由实现 Agent 完成，
但配置语义、状态机、恢复判断与安全评审必须由监督 Agent 复核。

### W0：D24 交接与 D25 合同冻结

**实现**

- 执行 §1 开工门，记录 D25_BASE_COMMIT、Python/Docker/OS/daemon identity；
- 将本规划从 v0.1 更新为 v1.0，仅允许基于代码事实修正接口名，不扩大范围；
- 建立 D25 完成门清单和证据目录命名，证据不得含宿主环境 dump。

**完成门**

- D24 工作树归属清楚且最终提交可定位；
- 全量回归绿；Docker doctor 确认目标 daemon；
- 未写任何 D25 生产代码。

### W1：配置与启动身份分型

**实现**

- 修改 `runtime/config.py`：对 `sandboxed` 强制精确 image digest、容器内绝对 POSIX command、
  明确 container working directory、资源限制；禁止 host cwd 语义、非空 mounts、危险环境；
- 配置字段可新增 `container_working_directory`，不得继续让 `cwd` 同时表达宿主路径和容器路径；
- 修改 `mcp/activation.py`：按 execution profile 构造身份。host-trusted 继续哈希宿主 code artifact；
  sandboxed 哈希 image/argv/cwd/环境摘要/limits/zero-mount policy，不对容器 argv 做宿主解析或 staging；
- 删除正常装配中的 legacy implicit allow；稳定错误码必须不携带原始配置内容。

**测试**

- tag、相对 argv、`..` cwd、secret-like env、非空 mount、缺 limits 全部拒绝；
- digest 任一字段变化即变化，字段顺序不影响 canonical digest；
- sandboxed identity 构建期间零宿主 executable lookup/spawn；
- legacy 文件配置 fail closed，host-trusted 旧合同不回归。

**完成门**：纯单元测试双 lane 绿；没有 Docker 调用。

### W2：长驻 Docker MCP endpoint

**实现**

- 在 `sandbox/` 提取可复用的可信 Docker create/inspect/stop/rm 原语，避免复制 D8 安全参数；
- 在 `mcp/` 新增长驻容器 endpoint（建议实现文件
  `mcp/docker_endpoint.py`，实际创建时再落文件）；
- create 使用交互 stdin 且不分配 TTY；attach 使用受控 `docker start --attach --interactive`；
- endpoint 实现 `McpProcessEndpoint`：stdin/stdout/stderr、poll、wait、terminate_tree、kill_tree、
  close_handles 与 redacted `external_identity`；
- 启动前 inspect 并逐项比对 image、argv/cwd、user、network、readonly rootfs、capabilities、
  security options、resources、mounts 和 labels；
- stderr 和帧大小继续由现有 `StdioTransport` 限界，不在新 endpoint 复制协议解析。

**测试**

- fake Docker client 精确 argv 测试；任何缺失/额外危险 flag 都失败；
- attach 进程先死但容器仍活、容器先死、stdin broken pipe、stderr flood、wait 超时；
- inspect 字段被篡改时不返回可用 endpoint，并执行有界清理；
- `external_identity` 只含 id/digest/state，不含命令正文、env 值、stderr。

**完成门**：endpoint 合同单测全绿；还不接 runtime assembly。

### W3：MCP allocation 与 Docker allocation 双账本绑定

**实现**

- 复用同一个 `ticket.allocation_id` 作为两条 stream 的关联键，不再生成第二个无关联随机 id；
- Docker create 前写 `sandbox.allocation-intended.v1`，其 owner 使用 activation request id，身份 digest
  与 MCP launch identity 一致；
- create 后将精确 container id 绑定到 sandbox allocation；MCP allocation 继续使用既有
  `mcp.process-started/ready/stopped/outcome-unknown.v1`；
- 若需要增加“容器已绑定”事实，只新增一个内容最小、JSON-only 的 typed event，并在同一提交加入
  reconstruct/corruption/CAS 测试；不得把 Docker client、句柄、完整 inspect 或 nonce 明文写入 MCP
  事件；
- 定义跨账本校验器：allocation id、request/owner、image、profile/command/mount digest、deadline
  必须一致，否则进入人工审计状态而不是猜测修复。

**崩溃窗口**

| 窗口 | 可观察事实 | 恢复结论 |
|---|---|---|
| claim 后、sandbox intent 前 | MCP=claimed，无受管容器 | 可信扫描证明不存在后 `failed_before_start` |
| sandbox intent 后、create 前 | 两账本有 intent，无 container id | release sandbox allocation，MCP `failed_before_start` |
| create 后、bind 前 | 可能存在带完整 labels 的容器 | 精确 label+inspect 找回并 bind；证据不足则 UNKNOWN |
| bind 后、start 前 | container 已知且未运行 | 可安全删除并记 `failed_before_start` |
| start 后、MCP started event 前 | container 可能运行 | 找回并停止；无法证明结果则 UNKNOWN |
| ready 后宿主崩溃 | container 可运行且会话丢失 | 只按精确归属停止/删除；工具副作用独立按 ToolLedger 恢复 |
| stop 后、stopped event 前 | container 状态可能已退出 | inspect 证据可补记 stopped；inspect 不可用则 UNKNOWN |

**完成门**：每个窗口至少一个 fault-injection 测试；并发 reconcile 使用 exact version，只有一个胜者。

### W4：启动器与 runtime 装配接线

**实现**

- `SandboxedLauncher` 注入 Docker 控制面、allocation store、clock 和安全 limits；
- 消费 one-time ticket 后完成身份复核、sandbox intent、create/inspect/bind/attach；任一步失败均走
  W3 状态机；
- `runtime/assembly.py::_process_start_decision` 对有效 sandboxed profile 返回受控允许，不再固定
  `mcp_sandbox_unavailable`；host-trusted 继续 ASK；
- `_default_launcher` 只从可信装配对象取得 Docker executable/daemon 和 limits，模型/MCP 配置不得
  替换 Docker binary；
- 保持现有 `StdioTransport`、`McpSession`、verified registry、policy 与 ledger 路径，不新增旁路调用。

**测试**

- sandboxed 成功链：grant → intent → claim → create → inspect → start → initialize → tools/list →
  registry bind → tool call → shutdown → release；
- Docker unavailable/doctor mismatch/image mismatch 时零 host spawn；
- 重放 ticket、过期 grant、错误 principal、配置 digest 漂移全部拒绝；
- host-trusted ASK 与风险提示回归。

**完成门**：fake 集成链全绿，且 `mcp_sandbox_unavailable` 只在 Docker 能力确实不可用时出现。

### W5：真实第三方 MCP 与对抗验证

**测试镜像**

- 建立仓库内最小恶意测试 fixture 与固定 Dockerfile；它只负责故障/攻击注入，不能冒充
  “第三方 MCP 已验证”的证据；
- 另选一个真实第三方 stdio MCP server 作为 reference integration。W0/W5 必须记录其项目名、
  固定版本或 commit、来源、许可证、制品 hash 与最终 image digest；选择标准是可离线运行、无需
  secret/宿主挂载、覆盖 initialize/tools/list/tools/call，而不是知名度；
- 第三方依赖可以在受控构建阶段获取，但运行阶段保持 `network=none`。若无法固定来源与制品，
  该候选作废，不能用 floating install/tag 顶替；
- fixture 同时提供正常边界模式和恶意模式，不使用网络、不读取真实宿主目录；
- 测试时先本地 build，再解析并记录 image digest。规划/配置示例不得写 floating tag。

**必须通过的行为矩阵**

1. 正常：initialize、分页 tools/list、至少两个工具调用、graceful shutdown；
2. 协议：超大 frame、非法 JSON、错误 id、重复响应、通知风暴、无限分页；
3. 资源：fork/PID 压力、内存超限、CPU 忙循环、stderr flood；
4. 隔离：读取预置“宿主诱饵路径”、访问 Docker socket、探测默认网关/公网全部失败；
5. 生命周期：启动挂起、关闭挂起、取消中调用、attach client 异常退出、daemon 短暂不可达；
6. 恢复：W3 每个崩溃窗口在真实 Docker 上至少覆盖关键窗口 create-before-bind、
   start-before-event、stop-before-event；
7. 治理：恶意 tool description/result 要求“切到 host_trusted/外发秘密”不能改变策略结果。

**完成门**：Windows Docker lane 全绿；Linux/WSL lane至少覆盖 create/inspect/stdio/cancel/reap，
用于证明不是 Windows 偶然实现，而非为了凑平台数量。

### W6：审计证据、使用文档与收口

**实现**

- 按 `docs/Koawa_Runtime_Security_Audit_Handoff.md` 的闭环格式，写 D25 证据矩阵：受保护对象、
  决策、强制、证据、终止/恢复；
- 更新 README：只声明已经跑通的 sandboxed stdio MCP 子集，列出 no-network/no-mount/no-secret
  限制及 host-trusted 风险；
- 提供一份可复制的安全配置示例和 doctor/运行/清理说明；
- 归档测试命令、commit、Python/OS/Docker/daemon identity、镜像 digest、通过/跳过/失败数字；
- 执行 full suite 和 ResourceWarning=error lane，审查事件/trace/日志无 secret/argv/stderr 泄漏；
- 逐项勾选 §10，规划状态改为 COMPLETE 后单独提交。

**完成门**：代码、测试、README、配置示例、审计证据零漂移；没有依赖 24h soak 或上线认证。

## 7. 文件级实施地图

| 路径 | 计划动作 | 所属工作项 | 主要风险 |
|---|---|---|---|
| `src/koawa_agent_v2/runtime/config.py` | 修改 profile 分型与 fail-closed 校验 | W1 | 兼容旧测试配置 |
| `src/koawa_agent_v2/mcp/activation.py` | 修改 sandbox launch identity 与恢复关联 | W1/W3 | 事件重建与 digest 漂移 |
| `src/koawa_agent_v2/sandbox/protocol.py` | 最小扩展 identity/label（确有需要才改） | W2/W3 | 破坏 D8 既有 runner |
| `src/koawa_agent_v2/sandbox/runtime.py` | 提取共享 Docker 安全原语，不改变 batch 语义 | W2/W3 | command runner 回归 |
| `src/koawa_agent_v2/mcp/docker_endpoint.py` | 新增长驻交互 endpoint | W2 | 双对象生命周期 |
| `src/koawa_agent_v2/mcp/launcher.py` | 实装 `SandboxedLauncher` | W4 | 错误 fallback/重复 ticket |
| `src/koawa_agent_v2/runtime/assembly.py` | 注入 Docker 控制面并开放沙箱路径 | W4 | 绕过 policy/ledger |
| `src/koawa_agent_v2/runtime/app.py` | 最小 operator 提示/doctor 接线（若现有入口需要） | W4/W6 | 与并行 UI/CLI 修改冲突 |
| `tests/test_d25_mcp_sandbox_*.py` | 单元、状态机、装配、泄漏测试 | W1–W4 | fake 与真实行为偏差 |
| `tests/fixtures/d25_mcp_server/` | 正常/恶意测试 server 与 Dockerfile | W5 | 夹具误含宿主依赖 |
| `scripts/` 或既有 lane runner | 新增 D25 Docker lane，优先扩展现有 runner | W5/W6 | 重复 lane 基建 |
| `docs/day-25-third-party-mcp-security-governance.md` | 证据与完成门回写 | 全程 | 声称先于证据 |
| `README.md` | 完成后更新真实支持面 | W6 | 与 D24 W6 竞态 |

开始修改任何共享文件前必须重新检查 `git status`、时间戳和 diff。发现并行修改时暂停该文件，先确认
所有者；禁止 reset、stash、覆盖或顺手提交他人改动。

## 8. 测试与证据策略

### 8.1 测试层级

1. **纯单元层**：配置、canonical identity、Docker argv、inspect validator、redaction；
2. **fake 控制面层**：全状态机、故障注入、CAS 冲突、双账本一致性；
3. **真实 Docker 层**：真实 stdio、隔离、资源限制、取消/超时/清理；
4. **runtime walkthrough**：从配置装配到模型可见 tool result 的完整链；
5. **全量回归层**：D1–D24 全套，确认 D8 sandbox 和 host-trusted MCP 未被破坏。

### 8.2 必跑命令口径

从 `v2/` 运行，环境设置方式按当前 shell 调整：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -W error::ResourceWarning -m unittest discover -s tests -v
```

真实 Docker lane 必须走项目已有 lane runner/manifest 机制并生成 JSON 报告；不得只把终端截图或
“我本机跑过”写成证据。环境原因跳过必须是显式 `ENV_BLOCKED`，且 D25 最终完成时 Windows
Docker 核心 lane 不允许跳过。

### 8.3 证据最小字段

- `schema_version`、`slice=D25`、commit、dirty flag；
- Python/OS/Docker client/daemon 的有界版本字段；
- image digest、配置/launch identity digest；
- suite 名、passed/failed/skipped、duration、稳定错误码；
- 各安全闭环测试锚点；
- 不记录 env value、credential、完整 argv、stderr/stdout 正文、宿主目录清单。

## 9. Agent 执行与评审协议

每个实现 Agent 接手一个 W 项时，输入必须包含：D25_BASE_COMMIT、本 W 的允许文件、前置提交、
完成门和不得触碰的并行文件。交付格式固定为：

1. 改动文件与合同变化；
2. 新增/修改的 typed events 及 expected-version 规则；
3. 测试命令和精确数字；
4. 未通过、跳过、环境阻断和残余风险；
5. `git status --porcelain` 与建议提交范围。

监督评审逐 W 检查：

- 是否存在 host fallback、Docker argv 注入、身份字段遗漏；
- 是否把外部副作用先做后记；
- 是否把 UNKNOWN 当失败后自动重试；
- 是否只测 fake、没有真实 Docker；
- 是否泄漏环境值/命令/协议正文；
- 是否修改或提交了不属于本 W 的并行改动。

评审不通过则原 W 保持未完成，后续 W 不得用兼容 shim 掩盖缺口。

## 10. D25 切片完成门

- [ ] D24 最终提交已冻结为 D25_BASE_COMMIT，开工时工作树归属清楚；
- [ ] sandboxed profile 能运行镜像 digest 固定的真实第三方 stdio MCP；
- [ ] 第三方 reference server 的名称、固定版本/commit、来源、许可证、制品 hash 与 image digest
  已入档；自制 fixture 仅作为对抗测试，不替代第三方证据；
- [ ] Docker 不可用或校验失败时零 host spawn、零静默降级；
- [ ] network none、零 mount、零 secret、readonly rootfs、non-root、cap-drop、NNP、资源上限均有
  inspect 证据与负向测试；
- [ ] activation、MCP allocation、sandbox allocation 使用同一关联身份并可精确重建；
- [ ] create/start/stop 关键崩溃窗口均有 fault-injection，无法证明的结果保持 UNKNOWN；
- [ ] cancel/timeout/crash 能有界终止并只回收精确归属容器；
- [ ] 恶意 MCP 协议内容不能改变 profile、policy、approval、ledger 或 completion gate；
- [ ] host-trusted 仍为显式 ASK 风险路径，legacy production config fail closed；
- [ ] Windows Docker 真实 lane 与有意义的 Linux/WSL lane 通过；
- [ ] 全量测试与 ResourceWarning=error lane 通过，无 D1–D24 回归；
- [ ] 证据报告无 credential/完整环境/完整 argv/协议正文泄漏；
- [ ] README、配置示例、审计矩阵与实际能力一致；
- [ ] 规划书回写测试锚点、commit 与精确数字，状态改为 COMPLETE。

## 11. 预算与停止条件

| 工作项 | 估算 |
|---|---:|
| W0 基线冻结 | 0.5 天 |
| W1 配置/身份 | 1–2 天 |
| W2 长驻 endpoint | 2–3 天 |
| W3 双账本/恢复 | 2–3 天 |
| W4 装配接线 | 1–2 天 |
| W5 真实 Docker/对抗矩阵 | 2–3 天 |
| W6 文档/全量收口 | 1 天 |
| 合计 | 9.5–14.5 个工作日 |

无需模型 API 预算即可完成大多数验证；真实 runtime walkthrough 若需要 provider 调用，沿用项目现有
“模型可灵活更换、预算不足立即说明、身份与花费入档”规则。

出现以下任一情形应停止扩范围并回到规划评审：

- 要让 sandboxed server 访问公网、秘密或可写宿主目录；
- Docker Desktop/WSL daemon 身份无法稳定确认；
- 需要用 host fallback 才能通过测试；
- D8 allocation/reaper 合同必须被破坏而无法通过适配层复用；
- 发现 D24 或其他并行会话仍在修改同一核心文件。

## 12. 后续但不属于 D25

- 受控网络：独立 egress proxy、域名/IP/DNS 重绑定治理、请求审计；
- 秘密代理：按工具/目标/时限签发，避免把长期 secret 交给 server；
- 安全只读数据输入：不可变 snapshot，而不是活宿主 bind mount；
- 远程 HTTP/SSE MCP、签名镜像供应链、插件分发与撤销；
- 生产 release gate、长周期 soak、SLO 与多主机调度。

这些条目是明确边界，不是 D25 的“未完成尾巴”。D25 的闭环对象就是：**无网、无挂载、无秘密、
镜像内置的第三方 stdio MCP，在真实容器隔离下完成全生命周期，并可审计、可恢复、无宿主降级。**

## 13. 变更记录

- 2026-09-02 v0.1：维护者决定在 D24 完成后以 D25 落地第三方 MCP 安全治理；规划限定为本地
  工程级 stdio 容器执行面，明确不做 24h soak/生产发布认证，并补齐配置、身份、执行强制、
  双账本、恢复、真实 Docker 对抗验证和文档收口。
