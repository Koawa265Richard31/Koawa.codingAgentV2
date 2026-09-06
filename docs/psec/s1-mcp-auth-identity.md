# S1 切片策略文档：MCP 授权与身份层研究 + secret 传播审计

PSEC 轨道。规划：`docs/agent-security-platform-track-plan.md` v1.0。日期：2026-09-06。基线：`PSEC_BASE_COMMIT = e213580`。**本切片无代码变更**（审计结论：无可加法性修复的真实缺陷；残余面均为 C 级设计决策）。

## 0. 结论

审计交接文档遗留的 S 候选"secret 与出站边界"在 **env 层已闭合**：Koawa 的子进程环境合同是"不可信子进程永不继承父环境 + secret 形态变量名整类拒绝 + 事件只存摘要"，比 MCP 规范对 stdio 的预期（server 从环境取凭据）**更严格**。真实残余面有三项，全部定级 **C**：可信 CLI 子进程（git/docker/taskkill）继承操作员环境；远程/HTTP MCP 授权层（当前不支持远程 MCP，无攻击面）；需要凭据的受信 server 的功能边界（答案指向 B1 专用凭据代管通道，而非给 env 契约开洞）。

## 1. 材料钉定

| 材料 | 版本 | 入口 | 访问 |
|---|---|---|---|
| MCP 规范 Authorization 章节 | spec 2025-06-18 | modelcontextprotocol.io/specification/2025-06-18/basic/authorization | 2026-09-06 |
| RFC 8707（Resource Indicators / audience binding） | IETF | rfc-editor.org/rfc/rfc8707 | 2026-09-06（经 spec 页引用核对） |
| RFC 9728（Protected Resource Metadata） | IETF | 经 spec 页引用核对 | 2026-09-06 |

## 2. MCP 授权规范摘要（与 Koawa 相关的部分）

- **传输层差异**（spec "Purpose and Scope"）：授权是 transport 级、可选；HTTP transport **SHOULD** 遵循本规范；**stdio transport SHOULD NOT 遵循，server 改从环境取凭据**。→ Koawa 当前只支持 stdio MCP（D25 §0 明确排除远程 MCP），OAuth 层按规范本身就不在 stdio 攻击面上。
- **HTTP 侧核心要求**（若未来支持远程 MCP 则全部适用）：OAuth 2.1（draft-ietf-oauth-v2-1-13）+ RFC 8414（AS metadata）+ RFC 7591（动态客户端注册）+ RFC 9728（protected resource metadata，401 必带 WWW-Authenticate）；RFC 8707 `resource` 参数在授权与 token 请求中 **MUST**（token 绑定目标 server 的 canonical URI）；server **MUST** 校验 token audience、拒绝不属于自己的 token；**token passthrough 明确禁止**（server 调上游 API 必须用上游 AS 另发的 token）；静态 client ID 的代理对每个动态注册客户端 **MUST** 取得用户同意（confused deputy）；PKCE **MUST**；全链路 HTTPS；短生命周期 token SHOULD。
- **关键洞察**：MCP 规范预期 stdio server"从环境取凭据"，而 Koawa 的环境合同**整类拒绝 secret 形态变量**——比生态默认更严格，代价是需要凭据的受信 server 必须在 runtime 外自行解决（见 §5 Row 6）。

## 3. 事实表（全部 file:line 实证）

| # | 事实 | 证据 |
|---|---|---|
| F1 | 子进程环境合同：永不继承父环境；平台变量从 OS 解析（SystemRoot 走 kernel32，非父环境）；注入向量名硬拒绝（LD_PRELOAD/BASH_ENV/NODE_OPTIONS 等，casefold 比较）；**secret 形态名整类拒绝**（`(?i)secret\|token\|password\|api[-_]?key\|authorization\|credential\|signature\|private[-_]?key` → `secret_variable_forbidden`）；显式 allowlist 强制（`environment_not_allowlisted`）；保留变量不可覆盖；值校验（禁 NUL、≤16KB）；输出不可变 | `runtime/subprocess_env.py:27-56,122-181`（SystemRoot :69-81，secret 拒绝 :171-172，allowlist :160-174） |
| F2 | 配置 env 的审计表示只有 (名字， sha256(值)) 对，值本身永不离开该模块 | `subprocess_env.py:84-105`（注释明示 "values never leave this module"） |
| F3 | stdio transport 合同：child NEVER inherits parent environment；admin 配置的显式 env 映射即 allowlist；SubprocessEnvError 以同名稳定码透传（content-free，不带值） | `mcp/transport.py:5-11` |
| F4 | host_trusted launcher：一次性 ticket 消费 → launch identity digest 重验（不符即 `mcp_launch_identity_mismatch`）→ 源重验（drift → 零 spawn）→ 最小 env 构建 → argv 码承载项重写到 staged 副本 → Windows Job Object 资源限制 | `mcp/launcher.py:206-248`（ticket :245，digest :246-247，源重验 :248，env :257-263，limits :185-196） |
| F5 | activation 事件契约：**永不包含 env 值、stderr、credentials、argv bodies**；environment 以 (名， 值摘要) 元组入档 | `mcp/activation.py:10,270-275,310-312,362-364,412-414` |
| F6 | 全仓库 14 处 spawn 点审计：**零** `env=None`/`env=os.environ`/`shell=True`。模式分两档：不可信/混合内容子进程走最小 env（MCP stdio `transport.py:285` ← `build_minimal_environment`；verification runner `runner.py:313-318` 同）；**可信 CLI 子进程继承操作员环境**（git：`context/index.py:97-104`、`workspace/integration.py:128-146,153-160`；docker：`sandbox/runtime.py:620-631,1415-1424`、`mcp/docker_endpoint.py:260-272`；taskkill：`mcp/transport.py:359-365`、`verification/runner.py:423`） | 各 file:line 如左 |
| F7 | provider 凭据通道：config 只持久化 `api_key_env`（环境变量**名**），运行时 `resolve_api_key` 按名从进程环境读取并校验，值不入配置/不入档；config 有 secret 关键词脱敏表 | `runtime/config.py:5,211-234,58,1186-1192`；`model/openai_client.py:177-205` |
| F8 | worktree 子进程入口接受调用方显式传入的 environment 映射 | `workspace/subprocesses.py:24,35` |

## 4. 与生态基线对照

OpenHands OSS 与 MCP 生态默认：MCP server 子进程通常继承（或弱过滤）父环境，spec 对 stdio 的凭据预期就是"从环境取"。Koawa 的 `secret_variable_forbidden` 是**结构性**排除而非过滤——需要凭据的 server 在装配时就失败（fail-closed，稳定错误码），不存在"忘了过滤"的态。审计交接文档 §6 的 S 候选所担心的"secret 是否可能进入 MCP 环境"，在本仓库的答案是不可能经由配置 env 通道。

## 5. 裁决表（审计交接文档 §9 格式）

| 安全链路 | Koawa 当前实现 | 产品基线做法 | 可到达缺口 | 分类 | 最小修复 | 不做什么 |
|---|---|---|---|---|---|---|
| 不可信子进程环境继承 | 强制最小 env + 注入名/secret 名结构拒绝（F1/F3） | 继承或弱过滤；spec 预期 stdio 从 env 取凭据 | 无 | 闭合（S 已达成） | — | 不为兼容性放开 secret 名拒绝 |
| secret 入事件/日志/错误 | 事件只存摘要（F2/F5）；错误码 content-free（F1/F3） | 常见泄漏源 | 无 | 闭合 | — | — |
| host_trusted 信任边界 | 每次启动 ASK + 一次性 ticket + digest 重验 + 源重验（F4）；D25 §2.1 明文"不得包装成安全沙箱" | 静默信任或无逃生舱 | 无（边界是声明式逃生舱） | 闭合 | — | 不静默放行、不降低 ASK 频率 |
| 可信 CLI 子进程（git/docker/taskkill）继承操作员 env | 继承（F6） | 同（这是操作员自己的工具与其环境） | 操作员 shell 中的 secret 对这些子进程及其子进程可见；恶意 repo 经 git filter/credential helper 的执行向量理论上存在，但 repo 指向属配置即信任边界内的 admin 决定（T6），且 MCP server 等不可信子进程被 F1 隔离在外 | **C** | 无需本轮修复；可选未来硬化 = git/docker 子进程专用最小 env helper（加法性新模块），登记 backlog | 不在本轮给 git/docker 建 env 白名单（收益低、易破坏工具功能） |
| 远程/HTTP MCP 授权层 | 不支持远程 MCP（D25 §0 明确排除）→ 攻击面不存在 | MCP spec OAuth 2.1 全集（§2） | 当前无；一旦立项远程 MCP 即成为必答题 | **C(条件)** | 触发条件 = 远程 MCP 支持立项；届时按 §2 全集新开切片（与 B1 身份/凭据代管合并设计）；token passthrough 禁止与 audience binding 是不可协商项 | 不预先实现 OAuth client |
| 需要凭据的受信 stdio server | env 通道契约性拒绝 → 该类 server 须由操作员在 runtime 外自行持有凭据 | spec：stdio server 从 env 取凭据 | 功能边界（非安全缺口）：生态常见 server（如需要 token 的 GitHub server）无法从 Koawa env 拿到凭据 | **C** | 未来答案 = B1 专用凭据代管通道（新模块、审计入档、按工具/任务最小化），**绝不回退 env 契约** | 不给 env 契约加豁免名单 |

## 6. 无代码变更声明

按规划 §3.3 第 1 条（加法性优先 + S 判定），本切片不写代码：Row 1-3 已闭合无需改；Row 4-6 为 C 级设计决策，其"最小修复"要么是未来 backlog（B1、git env helper），要么依赖尚未立项的前提（远程 MCP）。提前实现任何一项都违反"不预先建设"纪律。

## 7. 诚实边界

- 审计范围 = env/secret 传播 + 授权/身份层 + spawn 点完整性；**不含**：model provider 侧的传输安全（TLS/证书校验属模型客户端实现，F7 只审计了凭据通道）、D25 容器内 env（D25 已审计）、secret 在工具参数中经模型上下文传播的语义（属 S4/内容边界主题）；
- F6 的 spawn 清单以 2026-09-06 基线 e213580 的 grep + 精确行读为准，新增 spawn 点需按同法复核；
- "闭合"指当前证据下未发现可到达缺口，不构成对未来输入组合的无条件保证；任何反证按账本阻塞流程登记；
- 材料只对 §1 钉定版本负责；MCP spec 演进后本表需按新版本复核。
