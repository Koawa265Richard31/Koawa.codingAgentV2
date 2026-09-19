# KoawaAgent V2 稳定化代码实施文档（Agent 执行版）

| 项目 | 内容 |
| --- | --- |
| 文档状态 | READY_FOR_IMPLEMENTATION；本文完成不代表代码已实现 |
| 上游规划 | docs/v2-stabilization-and-production-readiness-plan.md |
| 源码基线 | f24c15407e7c48860bdad51c38d504d60c992831 |
| 编写日期 | 2026-08-25 |
| 适用范围 | v2/**；禁止修改或导入仓库根部 legacy Java 项目 |
| 运行时 | Python 3.12+，标准库优先 |
| 目标读者 | 接手单个实施单元的编码 Agent、复核 Agent、验证 Agent |

> 本文回答“具体改哪些文件、添加什么事件、事务怎样组成、旧数据怎样读、测试怎样写”。
> 上游规划书回答“为什么做、优先级和最终发布门”。两者冲突时先停下，由维护者校准文档，
> 不允许编码 Agent 自行降低不变量。

## 0. 文档使用方式

### 0.1 规范性措辞

- “必须”：实现和测试都不可省略。
- “禁止”：出现即停止当前实施单元，不能用文档说明代替修复。
- “建议”：默认选择；若当前源码证明不可行，可在 handoff 中给出证据并请求改规格。
- 事件名、stream category、稳定错误码和 wire 字段一旦合入即视为兼容合同。

### 0.2 执行角色

每个实施单元只允许一个写入者：

1. Implementer：只修改该单元允许的文件，完成代码、迁移、测试和文档。
2. Reviewer：只读复核事务、状态机、安全边界和兼容性。
3. Verifier：运行聚焦测试、跨切片测试和全量回归，核对进程/线程/数据库事实。

可并行做只读审查，但不能让两个 Agent 同时修改同一代码域。当前单元没有完成前，不提前创建
未来单元的空模块、空事件或占位测试。

### 0.3 每个 Agent 开工前必须读取

1. AGENTS.md。
2. 上游规划书的第 2–5 节和当前 S 节。
3. 本文第 0–3 节和当前 I 单元。
4. 当前单元列出的所有生产文件与现有测试，不只看文档摘录。
5. git status，确认并保留用户已有改动。

禁止：

- 修改 v2/ 外文件。
- 添加 pickle、隐藏 reasoning、credential value、完整环境或无界正文持久化。
- 用 sleep 猜并发顺序；使用 barrier、fake/DB clock、命名 fault point 或子进程 marker。
- 通过增大 timeout、删除断言、expectedFailure 或新增长期 skip 让测试变绿。
- 在用户未要求时 commit、push、reset、清理工作树或删除数据库/worktree。

### 0.4 单元完成命令

在 v2/ 目录运行：

    $env:PYTHONPATH = 'src'
    python -W error::ResourceWarning -m unittest <当前单元聚焦模块> -v
    python -W error::ResourceWarning -m unittest <跨切片模块> -v
    python -W error::ResourceWarning -m unittest discover -s tests -v

每个实现提交必须同时包含对应的绿测。不得把“预期先红”的测试留在主 discovery 中等待未来
单元修复。

## 1. 当前事实与实施顺序

### 1.1 当前基线

- 生产代码约 79 个 Python 文件，当前测试 discovery 为 417。
- 两次全量基线分别为 errors=3/skipped=16 和 errors=2/skipped=16。
- 当前稳定红项是 D10 Windows 冷 MCP 子进程在 0.5 秒共享 timeout 下握手失败。
- 16 个 skip 中 13 个覆盖 Docker/golden E2E；发布时不能把它们计为通过。
- 当前工作区已有上游规划书，属于用户改动；实施 Agent 必须保留。

### 1.2 可独立提交的实施单元

S0–S7 是规划 workstream；实际编码拆成以下更小的 I 单元：

| 单元 | 映射 | 内容 | 前置 |
| --- | --- | --- | --- |
| PREFLIGHT | S0 | 记录基线、reproducer 与状态，不新增长期红测 | 无 |
| I1 | S4-A | MCP 分段 deadline、最小环境、transport/session 生命周期；先恢复全量绿 | PREFLIGHT |
| I2 | S1 | mailbox stream-head CAS、RESULT_RECORDED、UNKNOWN/requeue、最小 LeaseKeeper | I1 |
| I3 | S2 | spawn/capacity/budget/terminal 原子化、完整 LeaseKeeper 与旧资源 reconcile | I2 |
| I4 | S3-A | durable JSON 硬边界、canonical text、config strict loader | I3 |
| I5 | S3-B/S6-A | DB schema manager、checkpoint v2、recovery port、legacy fresh export | I4 |
| I6 | S4-B | MCP activation、sandbox/host trust、prepared binding identity、惰性 execution plane | I5 |
| I7 | S5 | Unified durable truth、workspace effect ledger、trace 隔离 | I6 |
| I8 | S6 | 全生产故障点、migration 完整链、projection/performance/soak | I7 |
| I9 | S7 | mandatory lanes、完整 E2E、golden composite、dispatch contract | I8 |

推荐按表串行执行。若维护者依据上游拓扑选择 I4/I5 早于 I2/I3，仍必须保证只有一个写单元
活跃，并在 handoff 中记录依赖差异。I1 必须优先，因为它先清除当前全量红项和完整环境继承。

### 1.3 PREFLIGHT 不是生产代码切片

PREFLIGHT 只允许新增或修改：

- `docs/stability/preflight-baseline.v1.json`；
- `docs/stability/p0-reproducers.md`；
- 上游规划书中 Stability Gate 状态（仅当当前状态与实测事实不一致）。

禁止修改 `src/**`、`tests/**`、`scripts/**`、fixture、依赖或运行配置；禁止提交原始环境、完整
测试输出、diff 内容、绝对用户路径或 credential。`preflight-baseline.v1.json` 使用 UTF-8、
LF、exact keys，并具有以下 wire：

~~~json
{
  "schema_version": 1,
  "captured_at": "UTC",
  "document_base_commit": "40-lower-hex",
  "observed_head": "40-lower-hex",
  "branch": "bounded-relative-name-or-DETACHED",
  "dirty_relative_paths": ["sorted/v2/relative/path"],
  "environment": {
    "python": "major.minor.patch",
    "implementation": "CPython",
    "os": "Windows|Linux|Darwin",
    "architecture": "bounded-token",
    "sqlite": "major.minor.patch",
    "git": "major.minor.patch",
    "docker_available": false,
    "docker_client": null,
    "docker_server": null
  },
  "test_runs": [
    {
      "ordinal": 1,
      "command_id": "full-unittest-resourcewarning-v1",
      "exit_code": 1,
      "tests_run": 417,
      "failures": 0,
      "errors": 3,
      "skipped": 16,
      "duration_ms": 12345,
      "error_test_ids": ["sorted.test.id"],
      "failure_test_ids": []
    }
  ],
  "skips": [
    {
      "test_id": "fully.qualified.test.id",
      "reason_code": "docker_unavailable",
      "replacement_lane": "docker-required"
    }
  ]
}
~~~

约束：`test_runs` 必须恰有 ordinal 1、2；`skips` 是两次运行 skip 的排序并集，若同一测试两次
reason 不同则停止；reason 只映射到 `docker_unavailable`、`provider_opt_in`、
`platform_not_applicable` 或 `unexpected_skip`，不保存原始异常正文。版本字段只保存版本号，
不保存安装路径。`dirty_relative_paths` 相对 `v2/`；位于 `v2/` 外只记录计数并停止，不在文档
展开宿主路径。

在 `v2/` PowerShell 依次运行并把聚合数字通过 `apply_patch` 写入上述 JSON：

~~~powershell
git rev-parse HEAD
git branch --show-current
git status --porcelain=v1 --untracked-files=all
python --version
python -c "import platform, sqlite3; print(platform.python_implementation(), platform.system(), platform.machine(), sqlite3.sqlite_version)"
git --version
docker version --format '{{.Client.Version}} {{.Server.Version}}'
$env:PYTHONPATH = 'src'
python -W error::ResourceWarning -m unittest discover -s tests -v
python -W error::ResourceWarning -m unittest discover -s tests -v
~~~

Docker 命令失败只记 `docker_available=false`，不能因此跳过两次 unittest。不得用 shell 重定向
把原始输出落盘；从工具结果提取 test ID 和计数后只持久化上面的有界字段。

`p0-reproducers.md` 必须为 P0-01 至 P0-10 各建一节，并固定包含：`Owner I-unit`、
`现有聚焦命令`、`确定性 setup/barrier/fault point`、`当前可观察反例`、`修复后唯一 oracle`、
`允许的临时资源与有界清理`。没有现有测试时只写未来测试文件/方法名，不把预期红测加入
discovery。reproducer 禁止依赖 sleep、真实 credential、外网或不可恢复的用户目录。

若 `observed_head` 不等于本文 `document_base_commit`，先列出相对变更路径；只要触及当前 I1
允许文件，PREFLIGHT 可以记录事实但必须停止 I1，等待维护者重新核对规格。dirty tree 不清理、
不 stash；与 I1 不重叠时记录后继续。PREFLIGHT 完成门：

- 生产代码、测试 discovery 和脚本零变化；
- 两次全量结果及全部 skip 可机读，JSON 能被 strict parser 读取且无 unknown key；
- P0-01 至 P0-10 都有可运行命令与唯一稳定 oracle；
- 上游 REOPENED/REVALIDATION 状态与实测一致；
- canary 扫描确认两个产物不含环境值、绝对宿主路径、测试正文或 credential。

PREFLIGHT 不声称当前回归绿。I1 开始时只添加 I1 对应反例并在同一单元修绿。

## 2. 跨单元代码合同

### 2.1 typed event 与 schema

- 现有 v1 event 永远可读；不得 UPDATE 历史 payload。
- wire shape 变化时创建 .v2；只改变内部实现而 wire 不变时不要滥增版本。
- reducer 按 event_type 分派，不再使用“所有事件 schema_version 必须等于 1”的全局判断。
- unknown event/schema fail closed，不跳过。
- event payload 使用 exact key validator；未知字段不是“向前兼容”，而是损坏或未知 schema。

### 2.2 command、event 和 correlation identity

所有可重试命令使用稳定语义 ID：

~~~text
command_id = UUIDv5(namespace, operation + semantic identity)
event_id   = UUIDv5(command_id, event slot)
correlation_id = UUIDv5(command_id, "correlation")
~~~

禁止在可重试 event helper 中使用 datetime.now、uuid4 correlation 或随机 command id 形成
request fingerprint。occurred_at 由第一次命令输入或权威 DB clock确定；响应丢失后的重试使用
同一语义 fingerprint。

request_fingerprint 必须包含：

- operation；
- canonical业务参数；
- principal/run/attempt/delivery/binding identity；
- result/config/content digest。

它必须排除：

- observed stream version；
- retry count；
- 本地 wall clock；
- 随机 event/correlation ID；
- credential value 与完整正文。

CAS 重试的标准结构：

~~~python
receipt = store.read_idempotency(command_id, request_fingerprint=fingerprint)
if receipt is not None:
    return rebuild_from_receipt(receipt)

for retry in range(MAX_CAS_RETRIES):
    state = rebuild_current_state()
    validate_business_rules(state)
    try:
        return store.append_batch(
            writes_for(state),
            idempotency_key=command_id,
            request_fingerprint=fingerprint,
            preconditions=preconditions_for(state),
        )
    except WrongExpectedVersion:
        continue
raise StableDomainError("..._retry_exhausted")
~~~

同一 command 已提交但响应丢失时，EventStore 必须先返回 receipt，再做 version 检查。不同语义
复用同一 key 必须 IdempotencyConflict。

### 2.3 StreamPrecondition

- mailbox/result 写入使用 mailbox 当前流头作为 expected_version。
- Agent ownership 只作 StreamPrecondition，不为 mailbox 审计推进 Agent stream。
- parent spawn 是有意义的 parent 状态变化，可以推进 parent stream。
- precondition 与所有 StreamWrite 必须处于同一个 append_batch。
- required_payload 只有配合 required_event_type 才生效；Agent 最新事件可能是 started、
  heartbeat 或 child-spawn-authorized，因此一般用 exact version + reducer 先验，不硬编码单一
  latest event type。

### 2.4 时间与 lease

- 生产 lease 使用 Event Store/SQLite 数据库 UTC clock；本地 datetime.now 只用于非授权展示。
- 测试可注入 aware fake clock。
- heartbeat interval 默认 lease/3，停止和 join 必须有界。
- provider、MCP、Git、Docker 等外部调用使用 monotonic deadline。
- deadline 是绝对截止时间；分页/循环不能每次重置获得无限总时长。

### 2.5 JSON 与文本

- domain producer 在 fingerprint/event/model context 形成前完成 typed canonicalization。
- EventStore 只验证和拒绝，绝不静默脱敏或改写业务 payload。
- 首轮模型、同进程后续轮、持久事件、kill/resume 使用同一 canonical 文本。
- 运行时托管 credential 只通过 reference/scope；value 不进入 model、event、trace、child env。
- executable arguments若含 literal credential，不可在授权后偷偷改写；必须拒绝或要求 reference。

### 2.6 外部效果

统一状态：

    INTENDED -> CLAIMED -> APPLIED | FAILED_BEFORE_EFFECT | OUTCOME_UNKNOWN

FAILED_BEFORE_EFFECT只用于可以证明没有产生效果的情况。已执行且结果为负仍是APPLIED并携带
typed negative result。只要可能已经写文件、发送请求、创建进程/容器或修改worktree，就必须
核验后再决定；无法证明时OUTCOME_UNKNOWN，禁止普通自动重试；UNKNOWN只能凭exact evidence
typed resolution。

### 2.7 稳定错误

- domain 层不向 CLI 泄漏 WrongExpectedVersion、sqlite3.Error、路径正文、server payload。
- 原错误码能准确表达相同合同则保留；不要只为改名破坏兼容。
- 新错误只使用小写稳定 code，正文不含 user text、credential、绝对宿主路径或 MCP 返回。

## 3. I1：MCP deadline、最小环境与生命周期

### 3.1 目标与边界

I1 先修复当前全量红测，并关闭完整 os.environ、stderr 堵塞和 close race。I1 尚不完成
sandboxed activation；在 I6 之前不能声称不可信 MCP binary 已隔离。

### 3.2 允许修改的文件

- src/koawa_agent_v2/runtime/config.py
- src/koawa_agent_v2/runtime/assembly.py
- src/koawa_agent_v2/runtime/app.py
- src/koawa_agent_v2/runtime/cli.py
- src/koawa_agent_v2/runtime/subprocess_env.py（新增且立即被使用）
- src/koawa_agent_v2/verification/runner.py
- src/koawa_agent_v2/mcp/transport.py
- src/koawa_agent_v2/mcp/protocol.py
- src/koawa_agent_v2/mcp/connection_manager.py
- src/koawa_agent_v2/mcp/fixture_server.py
- tests/test_runtime_config.py
- tests/test_runtime_assembly.py
- tests/test_unified_runtime.py
- tests/test_d15_e2e.py
- tests/test_d10_transport_protocol.py
- tests/test_d10_connection_binding.py
- tests/test_d10_fixture_smoke.py
- tests/test_d10_integration.py

禁止在 I1 新建 activation/launcher 空壳；它们属于 I6。

### 3.3 配置模型

McpServerConfig 用以下独立字段替代一个共享 request timeout：

| 字段 | 默认 | 上限 | 对外错误 |
| --- | --- | --- | --- |
| process_start_timeout_seconds | 30.0 | 600 | mcp_process_start_timeout |
| initialize_timeout_seconds | 30.0 | 600 | mcp_initialize_timeout |
| tools_list_timeout_seconds | 30.0 | 600 | mcp_tools_list_timeout |
| tool_call_timeout_seconds | 15.0 | 600 | mcp_tool_call_timeout |
| io_poll_timeout_seconds | 0.25 | 5 | 内部 poll，不替代 phase deadline |
| shutdown_timeout_seconds | 5.0 | 60 | mcp_shutdown_timeout |

规则：

- 值必须为有限 float、非 bool、达到安全最小值。
- 旧 request_timeout_seconds 只兼容一个配置版本，并只映射 tool_call_timeout_seconds；旧 API
  捕获到新 `mcp_tool_call_timeout` 时可在兼容边界映射成 `mcp_request_timeout`并发deprecation，
  新 API/event/trace永远使用新码。
- 旧字段和任一新 timeout 同时出现时拒绝 ambiguous_mcp_timeout_config。
- startup/initialize/list 使用新默认，不继承旧 0.5 秒调用 deadline。
- doctor 输出 deprecation，但不能在 config 或 event 中回显原配置。

同时加入：

- max_pending_requests；
- max_list_pages；
- max_tools；
- max_inbound_messages；
- max_notifications_per_window；
- max_cursor_bytes；
- max_frame/result/stderr bytes。

I1 只加入 transport/session 立即使用的字段，不为 I6 resource profile 建空字段。

### 3.4 共享最小环境

新增 runtime/subprocess_env.py：

~~~python
def build_minimal_environment(
    explicit: Mapping[str, str],
    *,
    allowed_names: frozenset[str],
    private_temp: Path,
) -> Mapping[str, str]
~~~

要求：

- Windows 不信任父环境中的平台变量：用 `GetSystemWindowsDirectoryW`/`GetSystemDirectoryW`
  解析并复验绝对目录，SystemRoot/WINDIR来自该结果，ComSpec固定为受信System32下cmd.exe；
  不需要PATHEXT。TEMP/TMP只指向controller创建的私有目录。
- POSIX 只放固定 `LANG=C`、`LC_ALL=C` 与私有 TMPDIR。
- 固定增加 PYTHONHASHSEED=0、PYTHONUTF8=1、PYTHONIOENCODING=utf-8、
  PYTHONDONTWRITEBYTECODE=1；这些不是从父环境复制。
- 不复制 HOME、USERPROFILE、APPDATA、PATH、SSH、Git credential、云凭据和 provider key。
- explicit key 在 Windows 用 casefold 去重。
- 拒绝 secret/token/password/api-key/authorization 形态和 loader/code-injection 变量。
- 输出 immutable mapping；digest 只用于审计，不保存 credential value。
- executable 必须绝对解析，不依赖 child PATH。
- private temp在spawn前创建：POSIX mode 0700，Windows ACL只给当前controller identity；close
  后在确认process tree退出且handle关闭后清理，失败作为稳定cleanup错误而非静默遗留。

把 verification/runner.py 现有 _minimal_environment 提取并改用共享函数；不得出现两套安全
名单逐渐漂移。

D10 fixture必须改成绝对 `sys.executable` + 绝对 `fixture_server.py`，不能再依赖
`python -m`、PATH或PYTHONPATH；测试不得为了fixture重新放宽环境名单。

### 3.5 StdioTransport

实现锁保护状态：

    CREATED -> OPENING -> OPEN -> CLOSING -> CLOSED
                    \-> FAILED

具体修改：

1. 删除 dict(os.environ)。
2. open 接收/start 使用 process_start deadline；Popen 失败映射稳定 transport code。
3. close 支持未 open、open 失败、重复 close 和 open/close 竞态。
4. close 是终局；任何后台线程都不能再把状态改为 OPEN。
5. stderr 线程始终读到 EOF；超过保留上限后丢弃内容，仅累计 total_bytes 并标 truncated。
6. inbound queue 有界；overflow fail closed 并启动 cleanup。
7. send 在写 pipe 前验证 outbound frame UTF-8 bytes；记录 NOT_SENT/SENT/UNKNOWN。
8. 部分写或 pipe error 无法证明未发送时，调用层得到 uncertain。
9. close 先关闭 stdin/有界等待，再终止 exact process tree，关闭 pipes，join 非 daemon 线程。
10. 实现幂等 context manager。

当前 close-before-open 的 process=None 解引用必须由单测固定。

进程创建不是裸 `Popen` 细节，先定义可注入端口：

~~~python
class ProcessSpawner(Protocol):
    def spawn(self, spec: SpawnSpec, *, deadline: float) -> OwnedProcess: ...

class OwnedProcess(Protocol):
    pid: int
    stdin: BinaryIO
    stdout: BinaryIO
    stderr: BinaryIO
    def terminate_tree(self, *, deadline: float) -> None: ...
    def kill_tree(self, *, deadline: float) -> None: ...
    def wait(self, *, deadline: float) -> int: ...
    def close_handles(self) -> None: ...
~~~

`deadline` 是注入 monotonic clock的绝对值，从身份/环境/temp校验完成、发起OS spawn之前开始，
直到进程已绑定controller-owned tree且stdio handle可用才算成功。handle一取得就先绑定所有权，
未绑定前不得向session暴露 endpoint或开始initialize。

生产 adapter必须提供可取消的受监管spawn：Windows使用受控helper + kill-on-close Job Object
（子进程在对外ACK前已加入Job）；POSIX helper在exec前 `setsid`，controller持有pgid。helper ACK
携带pid/tree identity/pipe identity；deadline前无ACK就终止helper及Job/pgid、wait并关pipe。禁止用
一个可能永久阻塞在Popen/CreateProcess的daemon thread伪造deadline。平台若无法保证“先拥有、
后暴露”和有界kill/wait，直接 `mcp_process_start_unsupported`，不能降级为裸spawn。

spawn已成功但ACK丢失视为可能存在process：按tree identity强制收束；只有wait确认不存在后才报
`mcp_process_start_timeout`。close顺序固定为graceful stdin close → terminate tree → kill tree →
wait → close handles → join reader；每阶段共享一个shutdown绝对deadline，不能各自重置。

### 3.6 protocol 与 McpSession

- inbound/outbound 都验证 byte、depth、node、member；拒绝 duplicate key、NaN/Infinity。
- initialize、整个 tools/list 分页和单次 call 使用各自绝对 deadline。
- poll timeout 只让 reader 检查 closed/phase deadline，不能直接结束 request。
- pending 在发送前占有界 slot；满时发送字节数为零。
- list 共享一个总 deadline，限制 page/tools/cursor，并拒绝重复 cursor。
- refresh 只允许一个固定 worker/single-flight；通知 storm 合并，不创建无界线程。
- session state/catalog/generation/closed 由同一锁和 close epoch保护。
- refresh commit 前重新核对 epoch；CLOSED 不能回 READY。
- FAILED transport 不复用。
- result 按 bytes 增量累积，不先 join 无界正文。
- trace 使用 best-effort adapter；I7 完成正式 sink，I1 至少保证 trace failure不改 session结果。

McpSession 构造器可暂时保留 request_timeout 兼容参数，但内部立即转换为 typed deadlines；新生产
assembly 不再传旧参数。

I1 同时完成最小资源所有权链：`AssembledRuntime`实现幂等 `close/__enter__/__exit__`，只按
逆装配顺序关闭已成功创建的session/transport/temp；`AppRuntime.close()`委托它；CLI每个正常、
异常、Ctrl-C入口都使用 `with` 或 `finally`。I6只把该owner改成惰性execution plane，不再补救
I1遗漏的正常退出。

### 3.7 I1 测试

必须添加：

- test_startup_and_tool_call_timeouts_are_independent
- test_initialize_list_call_and_shutdown_deadlines_are_independent
- test_process_start_deadline_has_phase_specific_error_and_cleanup
- test_hung_spawner_is_cancelled_without_daemon_thread
- test_spawn_ack_loss_kills_owned_process_tree
- test_child_process_tree_is_gone_after_close
- test_io_poll_does_not_replace_request_deadline
- test_each_deadline_accepts_boundary_and_rejects_over_boundary
- test_parent_secret_canaries_are_absent_from_mcp_child
- test_close_before_open_after_failed_open_and_twice
- test_close_wins_refresh
- test_large_stderr_is_drained_then_normal_response_succeeds
- test_pending_limit_sends_zero_bytes
- test_duplicate_cursor_fails_closed
- test_notification_storm_is_single_flight
- test_assembly_failure_closes_every_started_session
- test_app_and_cli_normal_error_and_interrupt_paths_close_runtime

修改 D10 现有 0.5 秒集成测试：短值只能约束 tool call，不再约束冷启动。禁止直接把它改成一个
更大的共享 timeout。

I1 完成门：

- D10 和 runtime config/assembly 聚焦测试连续十次无 flake。
- 全量 unittest failures=errors=0；本地 Docker skip可保留为已知环境证据。
- 父进程 provider/cloud/random canary 不出现在 child env 或 SQLite。
- 退出后无 mcp-* 线程、fixture 子进程、未关闭 pipe 或 ResourceWarning。

## 4. I2：mailbox、result 与安全恢复

### 4.1 文件

- src/koawa_agent_v2/control/event_store.py
- src/koawa_agent_v2/control/sqlite_store.py
- src/koawa_agent_v2/agents/messages.py
- src/koawa_agent_v2/agents/control.py
- src/koawa_agent_v2/agents/graph.py
- src/koawa_agent_v2/agents/scheduler.py
- tests/test_event_store.py
- tests/test_d11_agent_control.py
- tests/test_d11_agent_scheduler.py
- tests/test_d11_agent_process_kill.py（新增）
- tests/fixtures/d11_fault_worker.py（新增）

### 4.2 数据库时钟与事件 helper

EventStore 增加 backend-neutral database_time()；SqliteEventStore 用：

    SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')

返回 aware UTC。AgentControlPlane 默认要求该能力；测试可注入 clock。agents/control.py::_event
改为显式接受 occurred_at/correlation_id，correlation 用 UUIDv5，不再每次 uuid4。

EventStore 测试：

- database_time aware；
- precondition失败时多流写入和 receipt 都为零；
- 显式 fingerprint 即使重建 event 时间仍返回原 receipt。

### 4.3 MessageStatus 与 projection

MessageStatus：

    QUEUED
      -> DELIVERED
      -> RESULT_RECORDED
      -> ACKED

    DELIVERED -> UNRESOLVED
    QUEUED/UNRESOLVED -> CANCELLED
    UNRESOLVED -> QUEUED
    DELIVERED/RESULT_RECORDED -> cancel_requested=true

MessageRecord 保留 version 字段兼容，但文档和变量名解释为 last_event_version，不再当流头。
增加：

- delivery_attempt；
- delivered_agent_attempt；
- delivered_run_id；
- delivery_lease_expires_at；
- result_ref/result_digest/result_summary；
- result_is_error/result_error_code；
- unresolved_reason；
- cancel_requested；
- legacy_delivery。

`cancel_requested` 不改变主 status。它不能把可能已产生外部效果的 DELIVERED 伪装成
CANCELLED；同一 delivery 的 late result 仍必须记录、ACK，随后 Agent 按取消意图终止。

新增 MailboxSnapshot(agent_id, stream_version, messages)，AgentMailbox.snapshot() 一次读取流并返回
真实头版本；load()/queued() 委托它，另加 unfinished()/result_recorded()。

### 4.4 新事件 wire

message.delivered.v2：

~~~json
{
  "agent_id": "uuid",
  "message_id": "uuid",
  "run_id": "uuid",
  "agent_attempt": 2,
  "delivery_attempt": 1,
  "lease_expires_at": "UTC",
  "delivered_at": "UTC"
}
~~~

message.result-recorded.v1：

~~~json
{
  "agent_id": "uuid",
  "message_id": "uuid",
  "delivery_run_id": "uuid",
  "recording_run_id": "uuid",
  "agent_attempt": 2,
  "delivery_attempt": 1,
  "result_ref": "agent-result:uuid",
  "result_digest": "sha256",
  "result_summary": "bounded canonical text",
  "is_error": false,
  "error_code": null,
  "recorded_at": "UTC"
}
~~~

message.acked.v2：

~~~json
{
  "agent_id": "uuid",
  "message_id": "uuid",
  "ack_run_id": "uuid",
  "delivery_attempt": 1,
  "result_ref": "agent-result:uuid",
  "result_digest": "sha256",
  "acked_at": "UTC"
}
~~~

message.unresolved.v1：

~~~json
{
  "agent_id": "uuid",
  "message_id": "uuid",
  "abandoned_run_id": "uuid",
  "delivery_attempt": 1,
  "reason": "provider_outcome_not_recorded",
  "observed_at": "UTC"
}
~~~

message.requeued.v1：

~~~json
{
  "agent_id": "uuid",
  "message_id": "uuid",
  "previous_delivery_attempt": 1,
  "reason": "stable-code",
  "resolution_kind": "proven_not_started|idempotent_read|operator_retry",
  "decision_id": "uuid"
}
~~~

message.cancel-requested.v1：

~~~json
{
  "agent_id": "uuid",
  "message_id": "uuid",
  "delivery_run_id": "uuid",
  "delivery_attempt": 1,
  "decision_id": "uuid",
  "actor_principal_id": "bounded-id",
  "reason": "stable-code",
  "requested_at": "UTC"
}
~~~

只允许 DELIVERED 或 RESULT_RECORDED；重复同一 decision 是 receipt 重放，不同 decision 在
`cancel_requested=true` 后返回现状，不再追加第二个请求。DELIVERED late result 正常进入
RESULT_RECORDED并保留标志；RESULT_RECORDED 只 ACK、不再调 provider。两者最后都让 scheduler
以 CANCELLED terminal 结束 Agent，result digest仍是真实 provider事实。

message.cancelled.v2：

~~~json
{
  "agent_id": "uuid",
  "message_id": "uuid",
  "previous_status": "queued|unresolved",
  "delivery_attempt": 0,
  "decision_id": "uuid",
  "actor_principal_id": "bounded-id",
  "reason": "stable-code",
  "cancelled_at": "UTC"
}
~~~

QUEUED 的 `delivery_attempt=0`；UNRESOLVED 使用当前 attempt。对 ACKED/CANCELLED 请求 cancel
返回 `message_transition_invalid`。CANCELLED 不接受 late result；只有 DELIVERED 的同一
delivery 可以写 late result。

I2 立即固定 result 编码，I4 只能抽取共用实现，不能改变已合入 wire：

~~~text
MESSAGE_RESULT_MAX_INPUT_UTF8_BYTES = 1_048_576
MESSAGE_RESULT_SUMMARY_MAX_UTF8_BYTES = 4_096
MESSAGE_RESULT_REF_MAX_ASCII_CHARS = 49
MESSAGE_ERROR_CODE_MAX_ASCII_CHARS = 128
TRUNCATION_MARKER = "\n[truncated]"
canonical_json = sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                 allow_nan=False, UTF-8 strict
~~~

输入先拒绝 surrogate/NUL，统一 CRLF/CR 为 LF并做 Unicode NFC；超过 input limit 整体拒绝
`message_result_too_large`。summary 为完整文本或“最长 UTF-8 前缀 + marker”，总字节不超过
4096且不切断 code point。`error_code` 为 null 或匹配 `[a-z][a-z0-9_]{0,127}`；
`is_error = error_code is not None`。

digest 覆盖未截断但已 canonical 的有界输入：

~~~text
result_document = {"error_code": error_code, "is_error": is_error, "outcome": canonical_outcome}
result_digest = lower_hex(SHA256(canonical_json_utf8(result_document)))
result_id = UUIDv5(NAMESPACE_URL,
    "koawa-v2:agent-result:" + agent_id + ":" + message_id + ":" + delivery_attempt)
result_ref = "agent-result:" + canonical_uuid(result_id)
~~~

事件只保存 bounded summary、digest与ref，不保存完整 outcome。相同 result_ref 配不同 digest
必须报 `message_result_identity_conflict`。

旧事件兼容：

- delivered.v1 重放为 delivery_attempt=1、legacy_delivery=true、无 lease。
- acked.v1 继续得到 ACKED(result_ref=None)。
- 新代码不再生成 delivered/acked v1。
- 旧 DELIVERED 不能直接 ACK 或自动 requeue；takeover 时转 UNRESOLVED。

### 4.5 control API

保持现有 send_message/deliver_message/ack_message 对外签名兼容，新增：

~~~python
def record_message_result(
    agent_id: UUID,
    message_id: UUID,
    *,
    run_id: UUID,
    expected_delivery_attempt: int,
    outcome: str | None = None,
    error_code: str | None = None,
) -> MessageRecord

def mark_message_unresolved(
    agent_id: UUID,
    message_id: UUID,
    *,
    abandoned_run_id: UUID,
    takeover_run_id: UUID,
    expected_delivery_attempt: int,
    reason: str,
) -> MessageRecord

def requeue_message(
    agent_id: UUID,
    message_id: UUID,
    *,
    expected_delivery_attempt: int,
    decision_id: UUID,
    actor: Principal,
    approval_id: UUID | None,
    resolution_kind: Literal[
        "proven_not_started", "idempotent_read", "operator_retry"
    ],
    reason: str,
) -> MessageRecord

def cancel_message(
    agent_id: UUID,
    message_id: UUID,
    *,
    expected_delivery_attempt: int,
    decision_id: UUID,
    actor: Principal,
    approval_id: UUID | None,
    reason: str,
) -> MessageRecord
~~~

`mark_message_unresolved` 是 takeover 内部 primitive，不从 public package export；只有当前 Agent
已由 `abandoned_run_id` takeover 到 `takeover_run_id` 才可用。实际 takeover 必须使用 4.6 的
多消息原子批次，不逐条调用它。

`requeue_message`/`cancel_message` 是 operator control plane：actor 必须与目标 Agent 的
`principal_id` 相同且含 `agents.resolve` scope，或含 `agents.resolve:any`；
`operator_retry` 必须提供已 GRANTED、action/agent/message/delivery 全匹配的 `approval_id`。
其他 resolution kind只有内部可验证 oracle 为真时可免 approval；不能只信调用方字符串。
事件持久 actor principal ID、decision/approval ID（approval ID放 metadata），不持久 scopes。
reason使用稳定码而非自由文本。

每个 transition：

1. 读取 AgentRecord 和 MailboxSnapshot。
2. reducer 校验 state/run/attempt/message transition。
3. mailbox 用 snapshot.stream_version 写入。
4. Agent stream exact version作为 StreamPrecondition。
5. WEV 后若仍是同 run则重读重试；run改变映射 stale_agent_run_fenced。
6. 使用稳定 fingerprint；响应丢失重试只返回 receipt。

`record_message_result` 的 command ID按 agent/message/delivery attempt 固定；fingerprint包含
完整结果的 digest但不含 outcome。`requeue/cancel` command ID由 decision ID和目标固定。
所有方法先验证 `expected_delivery_attempt`，避免 operator按旧画面解决新的 delivery。

ack 只接受 RESULT_RECORDED。takeover 后的新 run 可以补旧 RESULT_RECORDED 的 ACK，但 ref、
digest、attempt 必须完全一致，且不得再次调用 provider。

### 4.6 takeover 与最小 LeaseKeeper

`AgentRecord` 增加 `waiting_run_id: UUID | None`、
`blocking_message_ids: tuple[UUID, ...]`。I2 新 Agent wire：

agent.taken-over.v2：

~~~json
{
  "agent_id": "uuid",
  "abandoned_run_id": "uuid",
  "run_id": "uuid-v5",
  "attempt": 2,
  "lease_expires_at": "UTC",
  "taken_over_at": "UTC"
}
~~~

agent.waiting-for-message-resolution.v1：

~~~json
{
  "agent_id": "uuid",
  "run_id": "uuid",
  "attempt": 2,
  "blocking_message_ids": ["sequence-ordered-uuid"],
  "mailbox_stream_version": 7,
  "waiting_at": "UTC"
}
~~~

只允许 RUNNING且 run/attempt一致、列表非空且在该 mailbox snapshot均为 UNRESOLVED。reducer
进入 WAITING，保存 `waiting_run_id=run_id` 和 blockers，将 `run_id`、lease清空。进入 WAITING
的 append写 Agent expected version，并把 mailbox exact head作为 `StreamPrecondition`。

agent.resumed.v1：

~~~json
{
  "agent_id": "uuid",
  "previous_run_id": "uuid",
  "run_id": "uuid-v5",
  "attempt": 3,
  "resolved_message_ids": ["sequence-ordered-uuid"],
  "mailbox_stream_version": 9,
  "lease_expires_at": "UTC",
  "resumed_at": "UTC"
}
~~~

`start_attempt` 扩展为接受 WAITING。它读取同一 mailbox head并要求原 blockers已全部解决：要么
全部重新 QUEUED，要么全部 CANCELLED，禁止混合；仍为 UNRESOLVED/DELIVERED则
`message_outcome_unresolved`。append写 Agent expected version + mailbox `StreamPrecondition`，
fresh run ID由 `agent_id + previous_run_id + attempt + mailbox_stream_version` 的稳定 command
UUIDv5派生。全部 QUEUED时正常调度；全部 CANCELLED时新 run 不调用 provider，直接按真实
取消结果 terminal CANCELLED。这样 WAITING 没有悬空状态，也不把 resolution本身伪装成执行。

从 ORPHANED takeover 的 append_batch 固定为：

1. 读取 Agent exact version和一个 `MailboxSnapshot`；筛选 `DELIVERED`、
   `delivered_run_id=abandoned_run_id` 且尚无 result 的消息，按 sequence升序。
2. `command_id = UUIDv5(NAMESPACE_URL, "koawa-v2:takeover:" + agent_id + ":" +
   abandoned_run_id + ":" + new_attempt)`；`new_run_id=UUIDv5(command_id,"run")`。
3. 第一条 `StreamWrite(agent, orphan.version)` 写 taken-over.v2；若筛选非空，第二条
   `StreamWrite(mailbox, snapshot.stream_version)` 一次写所有 unresolved.v1，event slot为
   `unresolved:{message_id}:{delivery_attempt}`。
4. fingerprint包含 agent/abandoned/new attempt/lease seconds和排序
   `(message_id, delivery_attempt)`，不含 observed versions或时间。所有事件共享一次 DB clock。
5. RESULT_RECORDED 不写 unresolved；QUEUED、ACKED、CANCELLED也不变。
6. append前先查 receipt。任一 WEV 后全量重读两个流；若相同 command已提交返回 receipt，
   若已被其他 run takeover报 `agent_takeover_conflict`，否则以新 snapshot重算，最多3次。

不允许先写 taken-over 再逐条 mark unresolved；两流写入必须同一 commit。

I2 在 scheduler.py 内新增立即使用的 AgentLeaseKeeper：

~~~python
class AgentLeaseKeeper:
    def __init__(
        self,
        control: AgentControlPlane,
        *,
        agent_id: UUID,
        run_id: UUID,
        attempt: int,
        lease_seconds: int = 30,
        heartbeat_interval_seconds: float | None = None,
        max_cas_retries: int = 3,
        join_timeout_seconds: float = 5.0,
        wait_strategy: WaitStrategy | None = None,
        faults: FaultInjector = NO_FAULTS,
    ) -> None: ...
    def start(self) -> None: ...
    def assert_owned(self) -> None: ...
    def stop(self, *, assert_owned: bool = True) -> None: ...
~~~

- `lease_seconds` 为3..3600；interval默认 lease/3，显式值必须为0.1..lease/2；
  join timeout为0.1..30秒。
- 默认 `WaitStrategy` 只封装 `threading.Event.wait(timeout)`；测试注入 manual barrier/fake clock，
  禁止 sleep。线程名 `koawa-agent-lease-{agent_id}-{attempt}`、`daemon=False`。
- thread只保存 control端口、agent/run/attempt和稳定配置，不保存 provider、runtime、message正文。
- `start` 只能一次；`stop` set Event并有界 join。超时或线程仍活着报
  `agent_lease_keeper_failed`，禁止继续 terminal；close路径必须再次收束并作为资源泄漏失败。
- 每次 heartbeat用同一 run/attempt，独立稳定 command slot；WEV 后若同 run只是其他合法版本
  推进则全量重读重试，单次最多 `max_cas_retries`，耗尽映射
  `agent_heartbeat_retry_exhausted`。
- run/state改变为 `agent_lease_lost`；后台只保存稳定错误码并设置 failed Event，不保存异常正文。
  `assert_owned` 同步读取 Agent，要求 RUNNING且 run/attempt匹配、lease未过期，并重抛稳定码。
- provider返回后先 `assert_owned`，再 record result；`stop(assert_owned=True)` 必须 join后再次
  assert，不能把停止线程等同于仍拥有 lease。

### 4.7 scheduler 顺序

1. CREATED/ORPHANED/可恢复 WAITING执行 `start_attempt`，立即启动 keeper。
2. 先处理 RESULT_RECORDED：只 ACK，不调用 provider；有 cancel_requested则记住 terminal取消意图。
3. takeover已把旧 DELIVERED变成 UNRESOLVED。若还有 UNRESOLVED：停止 keeper，原子写
   waiting事件并返回 WAITING；若出现不属于当前 run 的裸 DELIVERED，fail closed而非等待猜测。
4. 取最小 sequence QUEUED。
5. TASK/FOLLOWUP：deliver → provider → assert_owned → record result → ACK。
6. 已知 provider AgentError：记录 error result并 ACK，再 terminal FAILED。
7. 未知崩溃未记录 result：takeover 后只能 UNRESOLVED/WAITING，不猜 FAILED。
8. CANCEL：deliver → deterministic cancel result → ACK → terminal CANCELLED。
9. 全部 WAITING blockers被 requeue后使用 fresh run/attempt；全部被 cancel后不调 provider并
   terminal CANCELLED。
10. mailbox无 unfinished才可 terminal；真实 outcome/ref/digest来自已持久 result，删除固定
    completed。cancel_requested 的 late result仍 ACK，但 terminal state为 CANCELLED。

### 4.8 I2 fault points 与测试

固定点：

- d11.enqueue.before_append / after_commit
- d11.deliver.before_append / after_commit
- d11.provider.entered / returned
- d11.result.before_append / after_commit
- d11.ack.before_append / after_commit
- d11.unresolved.after_commit
- d11.waiting.after_commit
- d11.resume.before_append / after_commit
- d11.cancel.before_append / after_commit

facts 只能含 ID/version/attempt，不能含 task/result正文。

必测：

- enqueue m1/m2 后 deliver m1 使用 mailbox头成功。
- terminal/takeover 与 mailbox transition barrier竞争，loser零事件。
- result-recorded 后 kill，重启只 ACK，provider counter仍为1。
- deliver 后/result前 kill，转 UNKNOWN，不自动调用 provider。
- actual provider outcome持久化，terminal不再 hard-code。
- provider known error先 result+ACK，再 FAILED。
- slow live provider至少两个 heartbeat后 discover_orphans=0。
- takeover后旧 provider late result被 fence。
- old delivered v1可重放但要求resolution。
- takeover的Agent+多条unresolved是同一 commit且按sequence；中途kill不出现半批。
- WAITING在全部requeue后以fresh run/attempt恢复；未全解决及cancel/requeue混合均拒绝。
- WAITING全部cancel后不调用provider并收口CANCELLED。
- DELIVERED cancel-request后late result只记录/ACK一次，最终CANCELLED且保留真实digest。
- result 1 MiB边界、summary 4096字节边界、多字节截断和相同ref不同digest冲突。
- response loss重试只有一个 ACK。
- keeper停止后无线程/ResourceWarning。

稳定新错误：

- mailbox_stream_conflict
- message_transition_invalid
- message_result_required
- message_result_identity_conflict
- message_outcome_unresolved
- message_requeue_not_authorized
- legacy_delivery_requires_resolution
- stale_message_delivery_attempt
- agent_lease_lost
- agent_lease_keeper_failed
- message_result_too_large
- message_resolution_not_authorized
- message_resolution_mixed

I2 完成后运行 D1/D6/D7/D11 相关测试和全量回归。

## 5. I3：Agent spawn、资源、terminal 与 LeaseKeeper 原子化

### 5.1 文件

- src/koawa_agent_v2/control/event_store.py
- src/koawa_agent_v2/control/sqlite_store.py
- src/koawa_agent_v2/agents/resources.py（新增且立即被 control 使用）
- src/koawa_agent_v2/agents/control.py
- src/koawa_agent_v2/agents/graph.py
- src/koawa_agent_v2/agents/scheduler.py
- src/koawa_agent_v2/agents/__init__.py
- tests/test_d11_agent_control.py
- tests/test_event_store.py
- tests/test_d11_agent_scheduler.py
- tests/test_d11_agent_concurrency.py（新增）
- tests/test_d11_agent_process_kill.py
- tests/fixtures/d11_fault_worker.py
- examples/day11_multi_agent_readonly.py

### 5.2 资源 projection

新增：

~~~python
@dataclass(frozen=True, slots=True)
class ResourceReservation:
    reservation_id: UUID
    child_agent_id: UUID
    parent_agent_id: UUID
    root_agent_id: UUID

@dataclass(frozen=True, slots=True)
class ParentCapacity:
    parent_agent_id: UUID
    version: int
    active_reservations: tuple[ResourceReservation, ...]

@dataclass(frozen=True, slots=True)
class RootAgentBudget:
    root_agent_id: UUID
    version: int
    active_reservations: tuple[ResourceReservation, ...]
~~~

tuple按 `reservation_id` 文本排序；reducer内部以 ID map检查，DTO不暴露可变 dict。新 spawn 的
capacity/budget使用同一个 `reservation_id = UUIDv5(spawn_command_id,
"resource-reservation")`；旧 `budget.reserved.v1` 的 legacy reservation ID固定等于
`child_agent_id`，不可换另一套派生规则。

stream：

- agent-capacity-{parent_agent_id}
- agent-budget-{root_agent_id}（保留）

事件：

agent.capacity-baseline-imported.v1：

~~~json
{
  "parent_agent_id": "uuid",
  "reservations": [
    {
      "reservation_id": "legacy-child-uuid",
      "child_agent_id": "uuid",
      "root_agent_id": "uuid"
    }
  ],
  "source_global_position": 42,
  "source_digest": "sha256"
}
~~~

agent.capacity-reserved.v1 / agent.capacity-released.v1：

~~~json
{
  "parent_agent_id": "uuid",
  "root_agent_id": "uuid",
  "child_agent_id": "uuid",
  "reservation_id": "uuid"
}
~~~

reserved exact keys就是上面四项。released exact keys为：

~~~json
{
  "parent_agent_id": "uuid",
  "root_agent_id": "uuid",
  "child_agent_id": "uuid",
  "reservation_id": "uuid",
  "terminal_state": "completed|failed|cancelled",
  "terminal_run_id": "uuid",
  "released_at": "UTC"
}
~~~

budget.reserved.v2 exact keys为 parent/root/child/reservation四项；budget.released.v2与上面
capacity release完全同 keys。旧 reserved/released.v1继续重放，以 child_agent_id 构造 legacy
reservation；projection 使用 identity map，不信任 payload.total_agents。

重复 reserve、重复 release、release unknown、root/parent 漂移均为 corrupt stream，不能用
max(value, 0) 隐藏。

兼容说明：本切片保留当前语义，max_total_agents 计算同一 root 下活跃的非 root descendant
reservation；root 自身不收费。若以后把 root计入总数，必须新配置/schema，不能在稳定化中
悄悄产生 off-by-one。

### 5.3 legacy baseline 与 reconcile

新旧二进制不得同时写同一 DB。升级流程先取得独占 writer 条件。

为固定扫描边界，EventStore增加：

~~~python
def current_global_position(self) -> int: ...

def read_all(
    self,
    *,
    after_position: int = 0,
    through_position: int | None = None,
    limit: int = 500,
) -> tuple[StoredEvent, ...]: ...
~~~

SQLite 的 high-water 是同一只读 statement的 `COALESCE(MAX(global_position), 0)`；之后分页只读
`after < global_position <= through`。event log不可变，因此后续 append不影响该快照。

parent由 `agent.spawned.v1`/legacy建立且capacity stream不存在时：

1. 捕获 global high-water；只用不超过该位置的 Agent events扫描该 parent 的旧 child spawn。
2. 每个 child也只重放到 high-water；CREATED/RUNNING/WAITING/ORPHANED 计为 active。
3. 每个 legacy `reservation_id=child_agent_id`，并从祖先链求 root；身份缺失/环/未知事件fail
   closed为 `agent_resource_reconciliation_required`。
4. 生成下面 exact source document，使用第2.2节 canonical JSON UTF-8求 SHA-256：

~~~json
{
  "schema_version": 1,
  "source_global_position": 42,
  "parent_agent_id": "uuid",
  "active_children": [
    {
      "child_agent_id": "uuid",
      "root_agent_id": "uuid",
      "reservation_id": "same-as-child-uuid",
      "agent_state": "created|running|waiting|orphaned",
      "agent_stream_version": 3
    }
  ]
}
~~~

5. baseline payload的 `reservations` 是 source document的 identity子集，均按 child UUID文本排序；
   command ID为 UUIDv5(parent, `capacity-baseline:` + high-water + source digest)。
6. baseline作为独立的首次 commit，`expected_version=-1`；成功后才开始新的 reserve。不能把 baseline
   偷塞进第一个 spawn事务。并发初始化loser先查 receipt再重读；已有非 baseline首事件即 corrupt。

旧 terminal 已写但 budget release 未写时，新增：

budget.legacy-reconciled.v1：

~~~json
{
  "root_agent_id": "uuid",
  "releases": [
    {
      "reservation_id": "legacy-child-uuid",
      "child_agent_id": "uuid",
      "parent_agent_id": "uuid",
      "terminal_state": "completed|failed|cancelled",
      "terminal_run_id": "uuid"
    }
  ],
  "source_global_position": 42,
  "source_digest": "sha256",
  "reconciled_at": "UTC"
}
~~~

只释放能够从 Agent event证明 terminal 且 budget仍 active 的 reservation。公开：

~~~python
@dataclass(frozen=True, slots=True)
class ResourceReconcileReceipt:
    root_agent_id: UUID
    command_id: UUID
    source_global_position: int
    source_digest: str
    released_reservation_ids: tuple[UUID, ...]
    budget_stream_version: int
    changed: bool

def reconcile_legacy_resources(
    root_agent_id: UUID,
    *,
    expected_budget_version: int | None = None,
) -> ResourceReconcileReceipt
~~~

reconcile同样先捕获 high-water并只重放到该边界。source document exact keys为
`schema_version/root_agent_id/source_global_position/releases`，其中 releases与事件payload同形且排序；
digest用canonical JSON。command ID为 UUIDv5(root, `legacy-resource-reconcile:` + high-water +
digest)，fingerprint含完整 source digest/释放IDs但不含expected version。

若 releases为空，返回 `changed=false`、当前budget version，零事件/零receipt写入；有释放时只向
budget stream以exact version追加一个 typed reconcile事件。WEV后先查receipt，再捕获新high-water
全量重算，最多3次。该方法不改历史；未知、非terminal、reservation身份或parent/root不一致
时fail closed。capacity baseline已只纳入当时 active child，因此旧terminal不需要capacity release；
新 terminal必须走5.6的capacity+budget双 release。

### 5.4 Agent event

agent.spawned.v2 使用 exact payload：

~~~json
{
  "agent_id": "uuid",
  "parent_agent_id": "uuid|null",
  "root_agent_id": "uuid",
  "task_id": "bounded-text",
  "attempt": 1,
  "run_id": null,
  "principal_id": "bounded-id",
  "scopes": ["sorted-scope"],
  "context_mode": "fresh|fork",
  "created_at": "UTC",
  "depth": 0,
  "capacity_reservation_id": null,
  "budget_reservation_id": null
}
~~~

root 的 `parent_agent_id=null`、`root_agent_id=agent_id`、`depth=0`，两个 reservation字段必须
显式 null；它不占自己的 budget/capacity。非 root 的 parent/root/depth非空，两个 reservation
字段都等于5.2定义的同一个 resource reservation UUID。exact-key reducer拒绝省略/null漂移。

parent stream新增 agent.child-spawn-authorized.v1：

~~~json
{
  "parent_agent_id": "uuid",
  "parent_run_id": "uuid|null",
  "parent_attempt": 1,
  "child_agent_id": "uuid",
  "root_agent_id": "uuid",
  "depth": 2,
  "reservation_id": "uuid"
}
~~~

它是有意义的 parent事实，会推进 parent version。Agent reducer保持 lifecycle state/run不变，
但校验 parent/run/depth并推进版本。

terminal v2 wire：

~~~json
{
  "agent_id": "uuid",
  "run_id": "uuid",
  "attempt": 2,
  "result_ref": "agent-run-result:uuid",
  "result_digest": "64-lower-hex",
  "terminal_at": "UTC"
}
~~~

上面是 `agent.completed.v2` exact keys。`agent.failed.v2`、`agent.cancelled.v2` 在相同 keys外
恰好增加 `reason` 稳定码；completed不保存自由 `outcome`。reducer同时支持 v1。

补强旧 reducer：

- started 只允许 CREATED；
- takeover 只允许 ORPHANED；
- heartbeat 只允许 RUNNING且 run/attempt一致；
- terminal 只允许 RUNNING；
- 不合法历史为 corrupt_agent_stream。

### 5.5 spawn API 与四流事务

完整签名：

~~~python
def spawn_agent(
    *,
    parent_agent_id: UUID | None,
    task_id: str,
    principal_id: str,
    scopes: tuple[str, ...],
    context_mode: ContextMode = ContextMode.FRESH,
    turn_id: UUID | None = None,
    parent_run_id: UUID | None = None,
    semantic_idempotency_key: str | None = None,
) -> AgentRecord
~~~

生产调用必须给1..256字节 semantic key；旧调用省略时只适合作为一次性本地命令并发出
deprecation，不得用于自动重试。command UUID为 UUIDv5(NAMESPACE_URL,
`koawa-v2:spawn:{parent-or-root}:{semantic-key}`)，child ID为 UUIDv5(command, `child`)，
reservation ID为 UUIDv5(command, `resource-reservation`)。root spawn里的 `root` 是字面域分隔，
其 root ID随后等于child ID。fingerprint绑定：

- parent、parent run/attempt；
- task/principal/scopes/context；
- turn；
- limits/config version。

root spawn只写一个 `StreamWrite(agent, -1, spawned.v2)`；spawned中两个reservation字段为null，
不创建伪 reserve/release。root自己的capacity stream不存在表示0；terminal以 version=-1
precondition证明它仍为空。非 root 若 parent是旧 v1且capacity stream不存在，必须先独立完成
5.3 baseline；parent为spawned.v2时不存在的capacity stream可直接按 version=-1 reserve。

每次 WEV 后必须重读 parent、capacity、root budget并重新验证：

- parent只允许 CREATED/RUNNING/WAITING；
- ORPHANED和terminal不能spawn；
- RUNNING时 parent_run_id必须匹配；
- child scopes是parent scopes子集；
- depth、parent slot、root total均未超限。

非 root spawn 单一 append_batch：

1. parent agent stream：child-spawn-authorized，expected parent.version。
2. parent capacity stream：capacity-reserved，expected capacity.version。
3. root budget stream：budget-reserved.v2，expected budget.version。
4. child agent stream：spawned.v2，expected -1。

四事件共享 command_id/commit_id。parent terminal与spawn同抢parent stream；两个spawn同抢
capacity和budget。任何一个冲突都全量重读，不只刷新budget。

### 5.6 terminal 原子事务

删除 `_release_budget()` 二次提交。完整 public API：

~~~python
def terminal(
    agent_id: UUID,
    *,
    run_id: UUID,
    expected_attempt: int,
    state: Literal[AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELLED],
    reason: str | None,
    result_ref: str,
    result_digest: str,
    source_message_ids: tuple[UUID, ...],
) -> AgentRecord
~~~

completed要求 `reason=None`；failed/cancelled要求稳定reason。控制面从 mailbox重建下面 exact
aggregate，并与调用方 ref/digest逐字节比较，不能信任 scheduler传入值：

~~~json
{
  "schema_version": 1,
  "agent_id": "uuid",
  "run_id": "uuid",
  "terminal_state": "completed|failed|cancelled",
  "reason": null,
  "results": [
    {
      "message_id": "uuid",
      "delivery_attempt": 1,
      "result_ref": "agent-result:uuid",
      "result_digest": "64-lower-hex",
      "is_error": false,
      "error_code": null
    }
  ]
}
~~~

results按message sequence排序，`source_message_ids`必须恰好等于本 Agent所有已处理、非CANCELLED
输入消息且每条为 ACKED；不能漏掉或加入别的 delivery。completed/failed至少一条；只有
`cancelled_before_dispatch` 允许空 results。digest为canonical JSON UTF-8的SHA-256；
`result_ref = "agent-run-result:" + UUIDv5(NAMESPACE_URL,
"koawa-v2:agent-run-result:" + agent_id + ":" + run_id)`。同ref不同digest为
`agent_result_identity_conflict`。

terminal command ID稳定为 UUIDv5(agent_id, `terminal:{run_id}:{state}`)，fingerprint包含
agent/run/attempt/state/reason/result ref+digest和固定策略 `enqueue_parent_if_nonterminal`，不含
parent当前状态、mailbox head或其他observed version。因此父状态在重试中改变仍能先返回原receipt。

每次尝试先查receipt，再读取：

- agent必须同 run/attempt且 RUNNING；
- agent自己的capacity projection active=0；有活跃child返回 `agent_children_active`；
- 非root的原spawn reservation必须同时存在于 parent capacity和root budget且身份完全一致；
- parent Agent与一个MailboxSnapshot。

所有分支都把 agent自己capacity stream的exact head放入 `StreamPrecondition`，证明检查与terminal
同一线性化点。写集固定如下：

**root terminal**：一个 Agent terminal v2 `StreamWrite` + 自身capacity head precondition；没有
budget/capacity release，也不创建 parent RESULT。

**非root、parent非terminal**：同一append_batch包含 child Agent terminal v2、parent capacity
released.v1、root budget released.v2、parent mailbox `message.enqueued.v1`；另有自身capacity和
parent Agent exact `StreamPrecondition`。RESULT消息使用：

~~~text
message_id = UUIDv5(terminal_command_id, "parent-result-message")
sequence = parent_snapshot最后sequence + 1（空mailbox为0）
kind = "result"
from_agent_id = child agent_id
body_ref = terminal result_ref
idempotency_key = "child-result:" + child_agent_id + ":" + child_run_id
status = "queued"
event slot = "parent-result-enqueued"
~~~

parent非terminal定义为 CREATED/RUNNING/WAITING/ORPHANED。mailbox write使用真实stream head；
enqueue payload沿用 `message.enqueued.v1` exact keys。所有 terminal/release/enqueue events共享
command/commit，parent消息不保存summary正文。

**非root、parent已terminal**：只写 child terminal + 两个resource release，并保留自身capacity
precondition；不写parent mailbox，也不要求parent precondition。这个分支只用于legacy/修复窗口，
不能把parent terminal视为跳过resource结算的理由。

WEV后先以同一稳定fingerprint查receipt；未提交则重读 Agent、两个resource、own capacity、parent
和mailbox并重选上述两个分支，最多3次。parent仍active时其terminal若先提交，parent precondition
使本批失败；重读后走terminal-parent分支。parent仍active但mailbox head变化只重算sequence。
response loss只返回同一receipt，不重复release/result。

竞态合同：terminal先赢则后续spawn被parent/capacity CAS挡住；spawn先赢则terminal重读own
capacity得到 `agent_children_active`；child terminal与其他spawn通过capacity/budget stream串行，
不产生负数、双release或超限。

### 5.7 完整 AgentLeaseKeeper

I2 keeper在I3完成：

- heartbeat event补 attempt；
- heartbeat遇 parent child-spawn-authorized 等同 run版本推进时重读重试；
- keeper background异常只保存稳定 code；
- stop(assert_owned=True)后立即做terminal/wait CAS；
- 无法取消的 provider返回后必须通过 Agent StreamPrecondition才能写result；
- discover_orphans和double takeover用exact version竞争，只有一个winner。

AgentRunResult扩为：

~~~python
@dataclass(frozen=True, slots=True)
class AgentRunResult:
    state: AgentState
    summary: str
    result_refs: tuple[str, ...] = ()
~~~

旧两字段构造保持兼容。

### 5.8 fault、测试与错误码

fault points：

- d11.resources.baseline.before_append / after_commit
- d11.spawn.after_read / before_append / after_commit
- d11.heartbeat.before_append / after_commit
- d11.orphan.before_append / after_commit
- d11.takeover.before_append / after_commit
- d11.terminal.after_read / before_append / after_commit

tests/test_d11_agent_concurrency.py：

- barrier同抢最后一个parent slot，只一个child。
- 不同parent同抢最后root slot，只一个child。
- 比较四流StoredEvent commit_id/commit_size。
- root spawn wire显式null reservation且只写Agent；root terminal只写Agent并有own-capacity precondition。
- legacy baseline在固定high-water重建；边界后child不进入digest，重复初始化只一个baseline。
- empty reconcile零事件changed=false；有修复时receipt/typed release可幂等重放。
- parent terminal vs spawn两种提交顺序。
- terminal commit后响应丢失，所有release/result各一条。
- terminal aggregate漏/多/伪造ACKed result均拒绝；parent RESULT的ID/ref/sequence确定且无正文。
- parent active分支WEV后变terminal，重读省略enqueue但两项release仍同commit。
- double orphan/takeover只有一个新attempt。
- 连续100轮预算不负、不超、无双terminal。

process kill：

- terminal commit后/return前kill，重开后terminal真实且预算只释放一次。
- provider阻塞期间观察至少两次heartbeat，discover=0。
- 停止keeper并越过DB expiry后才能takeover。
- 旧进程late result稳定fence。

新增稳定码：

- agent_spawn_idempotency_conflict
- parent_agent_orphaned
- parent_run_required
- parent_run_fenced
- child_scope_escalation
- agent_capacity_projection_corrupt
- agent_budget_projection_corrupt
- agent_resource_reconciliation_required
- agent_children_active
- agent_terminal_retry_exhausted
- agent_result_enqueue_conflict
- agent_result_identity_conflict
- agent_heartbeat_retry_exhausted
- agent_takeover_conflict

### 5.9 I2/I3 推荐提交边界

I2：

1. database_time + deterministic event/fingerprint helper。
2. mailbox v1兼容 reducer、snapshot和新状态。
3. deliver/result/ack/unknown/requeue precondition。
4. 最小 keeper和scheduler真实outcome。
5. subprocess kill tests与文档。

I3：

1. resources reducer和legacy baseline/reconcile。
2. semantic spawn和四流事务。
3. terminal/result/capacity/budget原子结算。
4. concurrency/takeover/response-loss tests。
5. example/API export/文档与全量。

每个边界都必须保持discovery全绿。

## 6. I4：durable JSON、canonical text 与 strict config

### 6.1 文件

- src/koawa_agent_v2/control/durable_json.py（新增且立即被EventStore/config使用）
- src/koawa_agent_v2/control/event_store.py
- src/koawa_agent_v2/control/sqlite_store.py
- src/koawa_agent_v2/control/runtime.py
- src/koawa_agent_v2/control/models.py
- src/koawa_agent_v2/recovery/execution.py
- src/koawa_agent_v2/execution/loop.py
- src/koawa_agent_v2/execution/worker.py
- src/koawa_agent_v2/runtime/config.py
- src/koawa_agent_v2/runtime/assembly.py
- src/koawa_agent_v2/runtime/app.py
- src/koawa_agent_v2/runtime/session.py
- tests/test_event_store.py
- tests/test_runtime_config.py
- tests/test_thread_runtime.py
- tests/test_turn_worker.py
- tests/test_d16_interactive_session.py

### 6.2 DurableJsonLimits

新增：

~~~python
@dataclass(frozen=True, slots=True)
class DurableJsonLimits:
    max_utf8_bytes: int
    max_depth: int
    max_nodes: int
    max_string_utf8_bytes: int
    max_object_members: int
    max_array_items: int
    max_key_utf8_bytes: int
~~~

历史读取使用不可变 protocol profile；它不能被后来调小的 RuntimeConfig改变：

| profile | bytes | depth | nodes | string bytes | members | items | key bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| EVENT_PAYLOAD_READ_V1 | 4,194,304 | 32 | 100,000 | 2,097,152 | 20,000 | 20,000 | 256 |
| EVENT_METADATA_READ_V1 | 16,384 | 16 | 2,048 | 8,192 | 512 | 512 | 256 |
| IDEMPOTENCY_RECEIPT_READ_V1 | 1,048,576 | 24 | 20,000 | 262,144 | 4,096 | 4,096 | 256 |
| CHECKPOINT_READ_V2 | 4,194,304 | 32 | 100,000 | 2,097,152 | 20,000 | 20,000 | 256 |
| CONFIG_READ_V1 | 1,048,576 | 24 | 50,000 | 262,144 | 4,096 | 4,096 | 128 |

`runtime.durable_limits` 是exact-key新 ingress policy，只约束本次进程产生的新 event/text：

| key | default | min | max |
| --- | ---: | ---: | ---: |
| event_payload_max_utf8_bytes | 4,194,304 | 1,024 | 4,194,304 |
| event_payload_max_depth | 32 | 4 | 32 |
| event_payload_max_nodes | 100,000 | 64 | 100,000 |
| event_payload_max_string_utf8_bytes | 2,097,152 | 256 | 2,097,152 |
| event_payload_max_object_members | 20,000 | 16 | 20,000 |
| event_payload_max_array_items | 20,000 | 16 | 20,000 |
| event_payload_max_key_utf8_bytes | 256 | 32 | 256 |
| user_input_max_utf8_bytes | 65,536 | 1,024 | 65,536 |
| resume_interrupt_max_utf8_bytes | 16,384 | 256 | 16,384 |
| terminal_text_max_utf8_bytes | 65,536 | 256 | 65,536 |
| instruction_max_utf8_bytes | 131,072 | 1,024 | 131,072 |

缺整个 `durable_limits` 使用defaults；对象存在则必须十一个key齐全，禁止部分隐式混合policy。
metadata/receipt/checkpoint/config读取永远只用上表固定profile。EventStore写入用
`min(EVENT_PAYLOAD_READ_V1, runtime ingress)`；因此调小配置后旧事件仍可读，新写按新阈值拒绝。

validator：

- 按UTF-8 bytes计；
- root depth=1；
- 拒绝cycle、surrogate、NUL、NaN/Infinity、非字符串key；
- limit+1抛content-free DurableJsonLimitExceeded；
- StoredEvent读取时再次验证，并检查version/global/commit/schema suffix不变量；
- 不递归到RecursionError，使用显式stack或先做深度保护。

NewEvent构造前冻结同一快照；SQLite fingerprint和入库使用这份快照。EventStore绝不执行
credential redaction或trim。

SQLite损坏库读取必须“先量、后解码”：同一只读事务先用
`length(CAST(column AS BLOB))`（或有界CASE）取得payload/metadata/receipt/checkpoint字节长度，超过
对应protocol profile时不把TEXT/BLOB取回Python。合格后以 `sqlite3.text_factory=bytes` 取严格
UTF-8 bytes，再调用共享 `strict_json_loads_bytes`；它用 `object_pairs_hook`拒绝任意层duplicate、
`parse_constant`拒绝非有限数，随后做nodes/depth/member验证。禁止先 `json.loads(str)`、被duplicate
折叠后再计数。分页中任一行损坏则整页fail closed，不返回部分可信projection。

### 6.3 CanonicalText DTO

新增typed policy/DTO，可放control/durable_json.py或独立control/durable_text.py，但只能创建
一个权威实现：

~~~python
@dataclass(frozen=True, slots=True)
class CanonicalText:
    value: str
    utf8_bytes: int
    digest: str
    redaction_policy_version: int
    redaction_count: int
~~~

顺序固定：

1. 类型检查。
2. CRLF/CR转换为LF。
3. Unicode NFC。
4. 拒绝NUL、surrogate、非法control，只允许LF/TAB。
5. credential-shape redaction。
6. UTF-8 byte limit。
7. 只用 strip判断空，不改变正文前后空白。

默认上限：

- user_input 64 KiB；
- resume/interrupt 16 KiB；
- terminal text 64 KiB；
- system/developer prompt 128 KiB。

首轮provider、event、history和resume使用同一 `CanonicalText.value`；不能首轮用raw、落库时才
redact。request fingerprint永远不放 value，只放 `{digest, utf8_bytes,
redaction_policy_version, redaction_count}`以及业务identity；idempotency receipt/fingerprint表的
canary测试必须证明完整正文不存在。

对需要真实credential的动作：

- 输入只允许reference/scope；
- literal value在action DTO构造时拒绝；
- 不在policy/ledger digest形成后静默替换为[REDACTED]。

### 6.4 ThreadRuntime 与执行链

ThreadRuntime增加 text_policy。以下字段在fingerprint/event之前canonicalize：

- create_turn.user_input；
- wait prompt；
- resume response；
- pause/cancel/timeout reason；
- complete summary/fail error。

workspace_ref只做结构和bytes限制，不按credential regex改写路径身份。

`start_turn` 不再接任意 execution seed Mapping，改为 frozen `ExecutionSeedDTO`，并写
`run.context-seeded.v2`。DTO `to_document()` 的 exact wire：

~~~json
{
  "seed_schema_version": 2,
  "thread_id": "uuid",
  "turn_id": "uuid",
  "run_id": "uuid",
  "attempt": 1,
  "turn_stream_version": 2,
  "request_semantics": {
    "protocol_version": 1,
    "provider": "bounded-id",
    "model": "bounded-id",
    "max_output_tokens": 4096,
    "input_items": ["exact canonical context documents"],
    "tool_definitions": ["exact ToolDefinition documents sorted by name"],
    "tool_catalog_digest": "64-lower-hex"
  },
  "projection": {
    "context": ["same canonical context documents"],
    "model_round": 0,
    "tool_count": 0,
    "output_chars": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "phase": "ready_for_model",
    "pending_tool_calls": [],
    "final_text": null
  },
  "resume": null
}
~~~

`input_items`/`context`不是任意 Mapping：逐项使用 model protocol现有 exact kind validator；两者
在 seed 时逐字节相等。tool definition覆盖name/description/input schema/profile semantic ID，受
EVENT_PAYLOAD profile和 `max_tools=256`、每schema 256 KiB约束。catalog digest是排序definitions
canonical bytes的SHA-256。

首个 seed 固定已 canonical 的 system/developer instructions、history和本Turn user message，以及
provider/model/max_output/tool catalog；恢复时忽略后来RuntimeConfig中的这些值。后续每个 fresh
Run（approval/input resume、stale requeue）也必须以一个 v2 seed开始，`resume` 使用：

~~~json
{
  "turn_event_id": "uuid",
  "turn_event_type": "turn.input-resumed.v1|turn.approval-resumed.v1|turn.stale-run-requeued.v1",
  "context_item": "exact canonical user/tool-result context document",
  "content_digest": "64-lower-hex"
}
~~~

reducer先完成上一Run projection，再验证新seed：thread/turn/attempt/Turn stream fence匹配；
provider/model/max_output/tools与首seed逐字段相等；所有counter/final/pending/phase不得重置、跳变
或信任调用方。无resume时projection必须逐字段等于上一projection；有resume时只允许context在
末尾追加由所引用Turn event payload可重新构造的那一个canonical item，其他字段相等，phase只按
对应typed transition从waiting改为ready。第二个seed不能改instructions/history/config或覆盖旧
context。segment内任何非seed event的run_id必须与segment seed一致；新run没有seed即corrupt。

旧 `run.context-seeded.v1` 只作为第一个legacy segment读取；legacy Turn再次运行前先以当前完整
projection + typed resume event写v2 seed，不能生成第二个v1。`_event` 从event_type后缀推导
schema_version。

execution/loop.py必须在assistant/tool/MCP result加入live context前形成canonical projection，并把
同一对象交durable recorder。tool executable args和展示用redacted projection分开。

App/session history必须使用queued TurnState中的canonical user_input/outcome，不得继续把CLI原始
text直接追加到进程内history。

### 6.5 strict config loader

load_runtime_config：

1. stat/read `CONFIG_MAX_BYTES + 1`，其中 `CONFIG_MAX_BYTES = 1_048_576`。
2. strict UTF-8 decode。
3. object_pairs_hook在任意层拒绝duplicate key。
4. parse_constant拒绝NaN/Infinity。
5. DurableJsonLimits限制depth/nodes/items。
6. exact top-level/nested key sets。
7. 所有DTO和路径preflight。
8. 完成后才允许创建DB、client或MCP process。

I4 引入顶层 `config_schema_version`：显式值必须为2；缺失视为legacy v1，只经过单一兼容
translator并产生deprecation，unknown/higher version拒绝。translator输出完整v2 DTO后再进入同一
validator，不能维护第二套宽松parser。I6会用下一schema区分旧MCP profile。

新稳定码：

- config_file_too_large
- config_duplicate_key
- config_non_finite_number
- config_json_limit_exceeded
- config_secret_in_generic_field

provider_options正向字段只允许：`temperature`、`top_p`、`frequency_penalty`、
`presence_penalty`（有限float -2..2）、`seed`（signed 64-bit int）、`parallel_tool_calls`
（bool）、`service_tier`（1..64字节id）、`stop`（最多16个、每个256字节字符串）和
`response_format`（exact `{"type":"text|json_object"}`）。禁止覆盖model/messages/input/tools/
stream/max_tokens等runtime-owned key；任何api_key/token/authorization/password/secret形态key
都拒绝。system_prompt走CanonicalText。

其他集合与数字合同固定如下：

| 项 | 合法范围/上限 |
| --- | --- |
| test_profiles / mcp_servers | 64 / 32 |
| 单argv条目 / argv总数 / argv总UTF-8 | 4,096 bytes / 128 / 65,536 bytes |
| 单env name/value / env总数 / env总UTF-8 | 128/4,096 bytes / 128 / 65,536 bytes |
| policy rules / principal scopes / budget principals | 4,096 / 256 / 1,024 |
| provider_options members/depth/nodes/bytes | 32 / 8 / 4,096 / 262,144 |
| lease_seconds | integer 3..3,600 |
| model_rounds / max_tool_calls | integer 1..256 / 0..1,024 |
| history_max_turns / history_max_chars | 0..1,024 / 0..4,194,304 |
| compact_min_turns | 1..max(1, history_max_turns) |
| provider timeout / max_stream | finite 0.1..600 / 1..3,600 seconds |
| max_output_tokens | integer 1..1,048,576 |
| request/response/SSE byte budget | 1,024..4 MiB / 1,024..32 MiB / 1,024..4 MiB |
| test timeout/stdout/stderr | 0.1..3,600 sec / 0..16 MiB / 0..16 MiB |

MCP phase范围使用I1表；durable limits使用6.2表。现有公开配置若落在范围外，loader返回稳定
配置错误并要求显式迁移，不能为兼容自行放宽硬顶。

### 6.6 I4 测试

- event byte/depth/node/string/member/key boundary与+1。
- 多流任一payload超限时0 event、0 head、0 receipt。
- cycle/deep nesting/NaN/surrogate不抛裸Python异常。
- 手工损坏DB payload/metadata/receipt的oversized、bad UTF-8、nested duplicate在解码前/strict parse
  fail closed，不能先折叠duplicate或返回半页。
- EventStore验证但不改写业务payload。
- credential-shaped user input在第一次ModelRequest前已canonical。
- uninterrupted与kill/resume ModelRequest.input_items逐项相等。
- resume/prompt/system/assistant/tool/MCP projection同一策略。
- CRLF/NFC/多字节boundary，不trim正文。
- executable args含credential literal时稳定拒绝。
- config大小、nested duplicate、NaN、坏UTF-8、unknown、limit+1。
- config失败后DB路径不存在、MCP spawn count=0。
- 同进程第二轮history与重启from_thread相同。
- 调小runtime ingress后旧合法event仍按immutable read profile重放，新event按小阈值拒绝。
- idempotency SQLite canary不含CanonicalText.value，只含digest/bytes/policy/count。
- forged第二seed不能改context/counter/phase/instruction；缺seed的新run fail closed。
- system_prompt/provider/model/max token/tool config在重启前改变，恢复的下一ModelRequest语义仍与
  已持久seed逐项相等。

I4完成门：D1/D2/D3/D6/D7/D13/D16/D21聚焦和全量绿；SQLite canary不含运行时托管
credential value。

## 7. I5：DB schema、checkpoint v2、recovery port 与 legacy export

### 7.1 文件

- src/koawa_agent_v2/control/sqlite_store.py
- src/koawa_agent_v2/control/schema.py（新增且立即管理DB版本）
- src/koawa_agent_v2/control/runtime.py
- src/koawa_agent_v2/recovery/protocol.py
- src/koawa_agent_v2/recovery/context.py
- src/koawa_agent_v2/recovery/execution.py
- src/koawa_agent_v2/recovery/store.py
- src/koawa_agent_v2/recovery/coordinator.py
- src/koawa_agent_v2/runtime/store_migration.py（新增）
- src/koawa_agent_v2/runtime/cli.py
- tests/test_s3_schema_migration.py（新增）
- tests/test_s3_checkpoint_v2.py（新增）
- tests/test_s3_legacy_export.py（新增）
- tests/test_d6_recovery.py
- tests/test_d6_process_kill.py

### 7.2 DB schema manager

canonical version使用PRAGMA user_version，schema_migrations记录id/checksum/applied_at。

~~~python
@dataclass(frozen=True, slots=True)
class MigrationStep:
    from_version: int
    to_version: int
    migration_id: str
    checksum: str
    apply: Callable[[sqlite3.Connection], None]
    postcheck: Callable[[sqlite3.Connection], None]
~~~

规则：

- 路径不存在或确认为空SQLite（user_version=0、无任何非`sqlite_%` table/index/trigger/view）才是
  fresh；直接创建latest。
- user_version=0且用户对象集合/列/index/trigger恰好匹配注册的历史
  `LEGACY_V0_FINGERPRINTS`：`database_legacy_export_required`，零写。
- user_version=0且有任意其他用户对象（包括只有一半events/streams、陌生表、残缺index/trigger）：
  `database_schema_unknown`，零写，绝不在旁边补表。
- 1..current：schema signature、migration ledger都精确匹配注册状态后才forward；当前版本也复验。
- >current：database_schema_too_new。
- 表签名/checksum未知：database_schema_unknown。
- migration在BEGIN EXCLUSIVE或IMMEDIATE中重读version，逐条execute DDL，不使用隐式提交的裸
  executescript。
- DDL、postcheck、foreign_key_check、migration row和user_version同事务提交。

分类发生在目录创建、journal mode改变、DDL和普通runtime connection之前。现有文件不交给磁盘
SQLite连接：以OS只读句柄有界采集DB/WAL/SHM/journal，每个介质完整读取两遍并复验路径/句柄身份、
大小、mtime/ctime和内容摘要；Windows句柄必须允许SQLite现有read/write/delete共享。总源介质上限
256 MiB、SHM上限4 MiB、WAL frame上限262,144、私有image上限256 MiB，四次新鲜重试共享调用
deadline，失败尝试的内存image必须丢弃。解析WAL header/salt/page size及滚动checksum，只覆盖最后一个
checksum-valid commit marker之前的frame；合法未提交tail不可见，截断、checksum错误、hot journal、
变化中或超限介质均fail closed。重建后的私有image才用`sqlite3_deserialize`装入`:memory:`，设置
trusted_schema=OFF、query_only=ON并开启read transaction；绝不在磁盘建立raw副本，也不触发源库
recovery/checkpoint。分类前后DB/WAL/SHM字节必须完全不变且不能新建sidecar；无法证明一致快照则
`database_classification_failed`，不能猜fresh。

本次migration registry固定为：

- v1：`0001_event_store_v1`，versioned streams/events/idempotency/schema_migrations。
- v2：`0002_recovery_projection_v2`，checkpoint cache、recoverable/lease projection。
- fresh DB也按同一registry执行v1→v2 bootstrap，并写两条ledger；不能用另一份“latest DDL”。

fresh-v2与migrated-v2必须具有相同用户对象signature，以及按to_version排序完全相同的
`(from_version,to_version,migration_id,checksum)` ledger；`applied_at`只允许不同且都来自DB UTC。
启动时重新计算每个注册step的normalized DDL checksum，与ledger不符即unknown。测试fixture固定：

- `legacy-v0-real.db`：从当前基线源码真实建库并含canary，只能export；
- `versioned-v1.db`：具有0001 ledger的sanitized已发布fixture，可原地迁移；
- `fresh-v2.db`：由空路径生成，用来与migrated-v2比较signature/ledger。

现有D6表从CheckpointStore移到migration；append路径删除sqlite_master探测。

### 7.3 RecoveryProjectionPort

协议放recovery/store.py，业务层不拿database_path、connection或私表：

~~~python
@dataclass(frozen=True, slots=True)
class CheckpointCacheRecord:
    turn_id: UUID
    cache_version: int
    checkpoint_id: UUID
    run_id: UUID
    turn_version: int
    execution_version: int
    reducer_name: str
    reducer_version: int
    source_event_id: UUID
    source_global_position: int
    projection_digest: str
    checkpoint_json: bytes
    updated_at: datetime

@dataclass(frozen=True, slots=True)
class RecoverableTurn:
    turn_id: UUID
    turn_version: int
    run_id: UUID
    lease_expires_at: datetime

@dataclass(frozen=True, slots=True)
class CacheReceipt:
    turn_id: UUID
    cache_version: int
    checkpoint_id: UUID
    changed: bool

class RecoveryProjectionPort(Protocol):
    def database_time(self) -> datetime: ...
    def publish_checkpoint_cache(
        self, record: CheckpointCacheRecord, *, expected_cache_version: int,
    ) -> CacheReceipt: ...
    def load_checkpoint_cache(self, turn_id: UUID) -> CheckpointCacheRecord | None: ...
    def list_recoverable(
        self, *, expired_before: datetime, after_turn_id: UUID | None, limit: int,
    ) -> tuple[RecoverableTurn, ...]: ...
~~~

SqliteEventStore或独立SQLite adapter实现。CheckpointStore(event_store, projections)只依赖协议。

checkpoint_cache业务列exact为上述 `CheckpointCacheRecord` 十三项：turn_id主键；cache_version、
turn/execution/reducer version为带CHECK的INTEGER；ID/digest/time为规范TEXT；checkpoint_json用BLOB
保存严格UTF-8 canonical JSON并有长度CHECK。除migration bookkeeping/index外不得悄加另一套truth
列。recoverable/lease projection exact列来自typed lease event，不能保存caller自报状态。

cache是可丢投影，不是真相。publish在BEGIN IMMEDIATE中核对source event、Turn run/version和旧
cache_version，再monotonic upsert。terminal race必须拒绝。

恢复状态变化只能由ThreadRuntime typed event驱动。删除recovery/store.py中raw INSERT events、
UPDATE streams和对SqliteEventStore concrete type的依赖。

port禁止暴露裸 `acquire/heartbeat/release_lease` mutator。恢复协调器只能调用ThreadRuntime：

~~~python
def claim_recovery_run(
    turn_id: UUID, *, expected_version: int, owner_id: str,
    lease_seconds: int, command_id: UUID,
) -> TurnState: ...
def heartbeat_recovery_run(
    turn_id: UUID, *, expected_version: int, run_id: UUID,
    claim_token: UUID, lease_seconds: int, command_id: UUID,
) -> TurnState: ...
def release_recovery_run(
    turn_id: UUID, *, expected_version: int, run_id: UUID,
    claim_token: UUID, command_id: UUID,
) -> TurnState: ...
~~~

对应 `turn.recovery-lease-claimed/heartbeated/released.v1` payload都 exact包含
thread/turn/run/owner/claim_token/attempt/lease_expires_at（released用released_at且不含expiry）。
claimed写fresh run和token；heartbeat/release必须同run/token。每个命令用Turn exact version，
append与backend的recoverable/lease projection更新在同一SQLite事务；投影由control层注册的typed
event projector维护，recovery包不能拿connection或构造SQL。projection写失败则event也回滚。
`list_recoverable`只读该可重建投影并用Turn stream复验；cache publish仍必须绑定已提交source
event，它不是权威command truth。

### 7.4 Checkpoint v2 wire

~~~json
{
  "checkpoint_schema_version": 2,
  "checkpoint_id": "uuid-v5",
  "reducer": {
    "name": "run-execution",
    "version": 2
  },
  "source": {
    "stream": {
      "category": "run-execution",
      "aggregate_id": "turn-uuid"
    },
    "covered_stream_version": 7,
    "covered_event_id": "uuid",
    "covered_global_position": 42,
    "covered_commit_id": "uuid",
    "covered_event_hash": "64-lower-hex"
  },
  "fence": {
    "thread_id": "uuid",
    "turn_id": "uuid",
    "run_id": "uuid",
    "turn_stream_version": 3
  },
  "projection": {
    "context": [],
    "model_round": 2,
    "tool_count": 1,
    "output_chars": 120,
    "input_tokens": 300,
    "output_tokens": 50,
    "phase": "ready_for_model",
    "pending_tool_calls": [],
    "final_text": null,
    "last_run_id": "uuid"
  },
  "projection_digest": "64-lower-hex",
  "previous": null,
  "created_at": "UTC"
}
~~~

exact key sets；counter是non-bool 0..2^63-1；UUID/UTC/hash严格。唯一 identity serializer是
`control.durable_json.canonical_json_bytes_v1`：先按相应hard profile验证，字符串NFC、key按Unicode
code point排序，`json.dumps(sort_keys=True,separators=(",",":"),ensure_ascii=False,
allow_nan=False)`，严格UTF-8；时间统一六位微秒 `YYYY-MM-DDTHH:MM:SS.ffffffZ`，UUID小写连字符。
Python float只允许有限且不是负零，并固定Python 3.12 shortest representation；identity DTO应优先
使用integer毫秒。任何实现不得另写checkpoint专用serializer。

namespace固定：

~~~text
KOAWA_WIRE_NAMESPACE = UUIDv5(NAMESPACE_URL, "https://koawa-agent.dev/wire/v2")
                     = 4fd0eee5-8c22-586c-bf58-1df6a65b322f
CHECKPOINT_ID_NAMESPACE = UUIDv5(KOAWA_WIRE_NAMESPACE, "checkpoint-id")
                        = 54eb737c-4f72-56d8-8f76-0174550723d0
projection_digest = lower_hex(SHA256(canonical_json_bytes_v1(projection)))
checkpoint_id = UUIDv5(CHECKPOINT_ID_NAMESPACE,
                       canonical_json_bytes_v1(checkpoint_identity).decode("utf-8"))
~~~

`checkpoint_identity` exact shape：

~~~json
{
  "domain": "koawa.checkpoint-id.v2",
  "reducer": {"name": "run-execution", "version": 2},
  "source": {
    "category": "run-execution",
    "aggregate_id": "uuid",
    "covered_stream_version": 7,
    "covered_event_id": "uuid",
    "covered_global_position": 42,
    "covered_commit_id": "uuid",
    "covered_event_hash": "64-lower-hex"
  },
  "projection_digest": "64-lower-hex"
}
~~~

`created_at`与`previous`不参与checkpoint ID；source全部字段、reducer和projection digest参与。

`stored_event_hash_v2` 是以下exact document的canonical bytes SHA-256：

~~~json
{
  "domain": "koawa.stored-event.v2",
  "event_id": "uuid",
  "stream": {"category": "bounded-id", "aggregate_id": "uuid"},
  "stream_version": 0,
  "global_position": 1,
  "commit": {"id": "uuid", "index": 0, "size": 1},
  "event_type": "run.context-seeded.v2",
  "schema_version": 2,
  "occurred_at": "2026-01-02T03:04:05.000000Z",
  "payload": {},
  "metadata": {}
}
~~~

hash不含自身或数据库row encoding，除此之外上述字段无排除项。

必须把以下三组golden固定在测试，不允许运行时重算expected：

- 空初始projection（last_run_id=`33333333-3333-4333-8333-333333333333`）canonical bytes为
  `{"context":[],"final_text":null,"input_tokens":0,"last_run_id":"33333333-3333-4333-8333-333333333333","model_round":0,"output_chars":0,"output_tokens":0,"pending_tool_calls":[],"phase":"ready_for_model","tool_count":0}`，digest为
  `785460b706e7092440d7065440a8e886b9948346611234370e8398d3f4f2595f`。
- stored-event sample使用 event=`11111111-1111-4111-8111-111111111111`、
  aggregate=`22222222-2222-4222-8222-222222222222`、
  run=`33333333-3333-4333-8333-333333333333`、
  commit=`44444444-4444-4444-8444-444444444444`、v0/global1/time如上、payload恰为
  `{"run_id":"33333333-3333-4333-8333-333333333333","seed_schema_version":2}`，hash为
  `03f1f97afb777b37ca89f5a18de5def2bcd794a83568c8067fed18640ad0825e`。
- 以上source + reducer v2 + projection digest的checkpoint ID为
  `c5c549f1-dafd-5146-a4f2-fadf33065e30`。

v1 checkpoint永远cache miss，不作为base。

### 7.5 canonical reducer

recovery/context.py是唯一实现：

~~~python
reduce_execution(events, initial=None) -> ExecutionProjection
projection_document(projection) -> Mapping
projection_digest(projection) -> str
~~~

必须验证：

- stream version连续；
- schema suffix一致；
- aggregate/metadata/payload thread/turn/run一致；
- 每个run从6.4的exact v2 seed开始且segment内run不漂移；首seed固定request semantics，后续seed
  必须由前一projection + 所引用Turn resume event逐字段导出，禁止覆盖context或重置counter；
- model_round严格+1；
- tool_count由result配对派生；
- output_chars/usage/pending/final/phase由event重新计算；
- tool call/result不成对时保持pending，不信任checkpoint字段；
- corrupt event log fail closed。

删除或私有化允许任意context/counter/phase拼可信状态的checkpoint_state。

DurableExecutionRecorder append成功拿到source anchor后调用publish_from_source；Store从0到anchor
重放reducer后才发布cache，不能把recorder内存counter直接当真相。

### 7.6 Coordinator

reconstruct：

1. bounded parse checkpoint。
2. schema/reducer/source/fence检查。
3. 从v0重放至covered version。
4. 比较canonical projection每字段和digest。
5. valid则reduce tail；miss则full replay。
6. 最终与full replay逐字段等价。

索引phase/automatic只是hint。claim_stale调用ThreadRuntime.requeue_stale_run typed command：

~~~python
def requeue_stale_run(
    turn_id: UUID,
    *,
    expected_version: int,
    abandoned_run_id: UUID,
    command_id: UUID,
) -> TurnState
~~~

该命令追加turn.stale-run-requeued.v1并使用exact version。projection adapter随append更新索引/
lease；Coordinator不直接写SQL。

### 7.7 legacy fresh export

CLI：

    koawa-agent-v2 export-legacy-store --source OLD --destination NEW

实现规则：

1. source/destination canonical且不同；destination不存在。
2. source只经7.2定义的有界OS介质快照读取；DB/WAL/SHM共同source digest来自同一次成功采集，
   schema分类、行读取和digest不能跨快照拼接。SQLite只读取私有内存image，源介质绝不
   UPDATE/DELETE/VACUUM/recovery/checkpoint。
3. 验证known legacy schema fingerprint并流式读取；最多100,000条事件、payload JSON UTF-8累计
   64 MiB，任一超限在发布destination前fail closed。
4. 不复制raw events、idempotency receipts、checkpoint、lease、trace。
5. 在同目录.partial-uuid创建latest fresh DB。
6. 写独立legacy-import audit stream：
   - legacy-store-imported.v1；
   - legacy-turn-snapshot-imported.v1。
7. terminal只保留IDs/status/time和canonical有界摘要。
8. active/nonterminal记录requires_manual_restart，不创建可resume普通Turn。
9. integrity/foreign-key/replay和canary扫描通过后os.replace。
10. source DB/WAL/SHM/backup不删除，输出受限遗留介质说明。

crash前最终destination必须不存在；partial仅含sanitized数据并可重跑。

### 7.8 I5 fault、测试与错误

fault：

- s3.event.after_validate_before_begin
- s3.event.mid_batch_before_receipt
- s3.checkpoint.after_source_read
- s3.checkpoint.before_cache_commit / after_cache_commit
- s3.checkpoint.after_verify_before_tail
- s3.migration.after_ddl / before_user_version / after_user_version_before_commit
- s3.export.after_source_scan / mid_destination_import / after_verify_before_rename

checkpoint tests：

- 合法covered hash配伪context/phase/counter/pending/final，cache miss且full replay正确。
- source/turn/run/event/global/commit任一替换拒绝。
- bad/truncated/duplicate/oversized/deep/unknown checkpoint miss。
- corrupt execution event fail closed。
- v1 miss；valid v2+tail=full replay。
- old checkpoint晚到不能覆盖新cache。
- terminal/publish race只允许有效winner。

migration/export tests：

- empty v0→latest；真实`legacy-v0-real`只要求export；注册`versioned-v1`→v2；重复打开幂等；
  fresh-v2与migrated-v2的signature/ledger相等。
- 每个migration fault后重开是完整old或完整new。
- 双进程migration只有一个执行。
- unknown higher/signature mismatch零写。
- exact legacy unversioned populated DB普通打开零写并要求export；残缺/陌生v0返回unknown且零写。
- raw canary在source/WAL/freelist，destination所有物理文件无原值。
- mid-export kill无最终文件，重跑成功。

稳定码：

- database_legacy_export_required
- database_schema_too_new
- database_schema_unknown
- database_migration_failed
- database_classification_failed
- legacy_export_destination_exists
- legacy_export_source_invalid
- legacy_export_verification_failed

I5完成门：recovery目录零sqlite3 import/私表SQL/raw event INSERT；checkpoint/full replay等价；
legacy source不变；D1/D6/D7/D13/D16/D21与全量绿。

## 8. I6：MCP activation、sandbox、binding identity 与惰性执行面

### 8.1 核心决策

MCP启动授权与tool-call授权是两层不同动作：

- activation决定“是否允许启动这个process/container”；
- D9/D10 tool policy决定“是否允许调用某个binding”。

status、doctor、approvals、approve/deny、cancel只需要控制面，禁止为了列状态先spawn MCP。
execution plane只能在run/resume真正需要工具时惰性装配。

### 8.2 文件

- src/koawa_agent_v2/runtime/config.py
- src/koawa_agent_v2/runtime/subprocess_env.py
- src/koawa_agent_v2/mcp/activation.py（新增）
- src/koawa_agent_v2/mcp/launcher.py（新增）
- src/koawa_agent_v2/mcp/transport.py
- src/koawa_agent_v2/mcp/protocol.py
- src/koawa_agent_v2/mcp/connection_manager.py
- src/koawa_agent_v2/mcp/tool_binding.py
- src/koawa_agent_v2/runtime/composite_registry.py
- src/koawa_agent_v2/execution/loop.py
- src/koawa_agent_v2/ledger/protocol.py
- src/koawa_agent_v2/ledger/store.py
- src/koawa_agent_v2/ledger/executor.py
- src/koawa_agent_v2/ledger/recovery.py
- src/koawa_agent_v2/policy.py
- src/koawa_agent_v2/approval_service.py
- src/koawa_agent_v2/runtime/assembly.py
- src/koawa_agent_v2/runtime/app.py
- src/koawa_agent_v2/runtime/cli.py
- src/koawa_agent_v2/mcp/fixture_server.py
- D9/D10/D15/D21相关测试

### 8.3 配置

~~~python
class McpExecutionProfile(StrEnum):
    SANDBOXED = "sandboxed"
    HOST_TRUSTED = "host_trusted"
~~~

I4的config schema是v2；I6把schema升为v3。持久JSON中的每个MCP server在v3必须显式给
`execution_profile`，即使值是sandboxed也不能省略。v1/v2或无version配置缺profile时返回
`mcp_profile_migration_required`，不能静默当host_trusted/sandboxed；程序内直接构造DTO时的
Python default可以是sandboxed，但不参与JSON缺字段语义。v3 unknown/缺profile按strict key
validator拒绝，并同步更新I4 top-level/nested schema测试。

McpServerConfig增加：

- execution_profile，默认sandboxed；
- immutable image_id（sandboxed必需）；
- McpResourceLimits(cpu/memory/pids/tmpfs/process count)；
- read_only_mounts，默认空；
- host_trusted code_artifacts；
- I1已有deadlines和limits。

host command[0]在preflight解析为绝对文件。解释器脚本、JAR等code-bearing argv由
code_artifacts显式声明，不能由runtime猜测。

环境规则：

- 拒绝重复key（Windows case-insensitive）。
- 拒绝provider key变量、secret-like key/value。
- 拒绝LD_PRELOAD、DYLD_*、PYTHONSTARTUP、NODE_OPTIONS等loader/code injection。
- config digest只持久化env name/value digest，不存value。

### 8.4 Launch identity

mcp/activation.py：

~~~python
@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    canonical_path: str
    platform_file_id: str
    size: int
    content_sha256: str

@dataclass(frozen=True, slots=True)
class CodeArtifactIdentity:
    role: Literal["executable", "interpreter_script", "jar", "bundle"]
    argv_index: int
    source: ExecutableIdentity
    staged: ExecutableIdentity

@dataclass(frozen=True, slots=True)
class EnvironmentIdentity:
    name: str
    value_digest: str

@dataclass(frozen=True, slots=True)
class ReadOnlyMountIdentity:
    container_path: str
    source_file_id: str
    manifest_digest: str

@dataclass(frozen=True, slots=True)
class McpLaunchIdentity:
    server_id: str
    execution_profile: McpExecutionProfile
    code_artifacts: tuple[CodeArtifactIdentity, ...]
    argv_digest: str
    cwd_identity_digest: str | None
    environment: tuple[EnvironmentIdentity, ...]
    image_digest: str | None
    read_only_mounts: tuple[ReadOnlyMountIdentity, ...]
    resource_digest: str
    deadline_limit_digest: str
    config_digest: str
~~~

每次spawn紧邻前重新canonicalize/no-follow解析并核对file identity/content digest。任一漂移：

- 旧grant失效；
- 产生新ASK；
- launcher调用数为零。

TOCTOU：

- host artifacts复制到controller管理的content-addressed staging；
- 限制ACL/权限；
- 复验source与staged digest；
- 按`argv_index`把可执行文件、解释器脚本、JAR和bundle全部重写为staged path，实际argv不得再
  引用source；role/原argv位置/source+staged identity都进入launch digest。
- 未声明的code-bearing arg拒绝；目录/package默认禁止。若未来允许目录，只能用no-follow排序
  manifest（raw relative path/type/mode/size/content hash）并整体staging，不能只hash目录名。
- environment按name排序的typed pair，禁止两个平行tuple错配。
- mount在container create紧邻前重新以no-follow handle复验source file ID、manifest和是否
  symlink/junction/reparse；任一漂移使grant失效且create数为零。

不能证明approval identity等于将执行内容时fail closed。动态库和未声明依赖仍属于显式
host_trusted边界，不声称完整供应链隔离。

host_trusted resource limits不是声明性digest：Windows必须由Job Object、Linux必须由cgroup/
受控wrapper实际强制CPU/memory/pids/process-tree；当前平台无法逐项强制就拒绝
`mcp_host_limits_unsupported`，不能裸进程降级。sandbox profile在create后、start前inspect实际
image digest/entrypoint/cmd/user/network/mounts/rootfs/caps/security options/resource/labels，逐字段
等于grant identity才允许start。

### 8.5 activation 与 allocation 事件

授权状态：

    AUTH_REQUESTED -> GRANTED | DENIED | EXPIRED
    GRANTED -> EXPIRED | REVOKED

allocation：

    INTENDED -> CLAIMED -> STARTED -> READY -> STOPPED
                  \-> FAILED_BEFORE_START | OUTCOME_UNKNOWN
                            STARTED/READY -> OUTCOME_UNKNOWN
    OUTCOME_UNKNOWN -> STOPPED | FAILED_BEFORE_START  (evidence-bound resolution)

stream固定为：

- mcp-activation-{request_id}
- mcp-allocation-{allocation_id}

事件：

- mcp.activation-requested.v1
- mcp.activation-granted.v1
- mcp.activation-denied.v1
- mcp.activation-expired.v1
- mcp.activation-revoked.v1
- mcp.process-intended.v1
- mcp.process-claimed.v1
- mcp.process-started.v1
- mcp.process-ready.v1
- mcp.process-stopped.v1
- mcp.process-failed-before-start.v1
- mcp.process-outcome-unknown.v1
- mcp.process-start-observed.v1
- mcp.process-outcome-resolved.v1

payload只含：

- server/profile/launch identity digest；
- principal/scope；
- request/allocation/claim/epoch/nonce；
- image/container/process identity digest；
- stable code/time。

禁止env value、stderr、credential、完整config/argv正文。

人工grant/deny命令必须携带pending document暴露的exact `expected_version`。operator decision事件
同时记录`decision_expected_version`、布尔decision和approver principal。响应丢失后，只有“当前
latest事件恰为expected_version+1且决定/批准人/代际全部相同”才能返回原回执，不能续TTL；旧代批准
不能命中同request ID后来追加的REQUESTED代际。grant append并发发生`WrongExpectedVersion`时先重读：
同一精确决定返回已提交回执，异决定稳定返回`activation_version_conflict`，不得泄漏存储层异常。

host_trusted需要principal的mcp.host_process.execute scope和identity-bound durable grant。
sandboxed可被本地policy ALLOW，但仍写authorization与allocation事实。

`mcp.process-intended.v1` 固定绑定 request ID、grant digest/version、allocation ID、launch digest、
profile、principal和expiry。claim是启动外部效果的线性化点：同一append_batch向allocation exact
head写 `mcp.process-claimed.v1`，并对activation stream施加 exact version + latest status=GRANTED +
launch/principal digest payload precondition。grant已expired/revoked或digest漂移则claim零写、
launcher零调用。

activation service只在claim commit后签发一次性内存ticket：

~~~python
@dataclass(frozen=True, slots=True)
class AuthorizedLaunchTicket:
    request_id: UUID
    grant_stream_version: int
    grant_digest: str
    allocation_id: UUID
    allocation_stream_version: int
    claim_epoch: int
    claim_token: UUID
    nonce: UUID
    launch_identity_digest: str
    not_after: datetime
~~~

launcher回调activation service原子consume：核对ticket类型/nonce未消费、DB allocation仍为CLAIMED、
claim token/epoch/head和not_after；每张ticket至多进入一次OS/container create。ticket不序列化、不进
event/trace。claim后grant被撤销是合法竞态：revoke提交后必须以allocation exact identity触发stop；
不能继续声称spawn=0，truth以CLAIMED/STARTED/STOPPED或UNKNOWN记录。

spawn成功但 `process-started` ACK/事件响应丢失时，recovery用ticket nonce + exact Job/container/
process identity检查：确认存在则追加 `mcp.process-start-observed.v1`（与started同一状态转换）并立即
收束到STOPPED；确认从未创建才FAILED_BEFORE_START；两者都不能证明则OUTCOME_UNKNOWN。
`mcp.process-outcome-resolved.v1` 必须含原claim token、expected allocation version、reconciler
principal、evidence kind/digest和resolved state，只允许可重复核验的“不存在”或“已停止”证据；
不能凭operator文字清UNKNOWN。

无grant时：

- 写activation request；
- App返回bounded request_id；
- Popen/Docker process调用数=0。

新v3执行顺序选择“Turn创建前activation preflight”：App先canonicalize输入和计算launch identity，
但在 policy得到GRANTED前不创建Thread/Turn/Run，也不持久用户正文；ASK只返回request_id。批准
命令只写grant，不自动resume，用户以同一semantic command重新发起run，随后才创建Turn并claim。
因此不会留下孤立QUEUED Turn，也不需要发明WAITING_FOR_ACTIVATION。任何仍先建Turn的legacy入口
必须拒绝迁移，不能同时保留两种语义。

pending/approve/deny API同时列tool和mcp_process_start。host风险提示必须明确其可绕过MCP protocol
直接使用宿主用户权限。

### 8.6 launcher

~~~python
class McpProcessLauncher(Protocol):
    def launch(self, ticket: AuthorizedLaunchTicket) -> McpProcessEndpoint: ...
~~~

production launcher只接受activation service签发的opaque ticket，assembly不直接Popen。

host_trusted：

- shell=False、close_fds=True、禁止handle inheritance；
- Windows Job Object/POSIX process group；
- exact staged executable及所有code-bearing argv重写；
- controller private temp；
- 最小environment；
- Job/cgroup/wrapper逐项强制grant中的resource limits；不支持即launch前拒绝；
- 整棵process tree有界收束。

sandboxed：

- immutable image；
- network none；
- non-root/read-only root/cap-drop all/no-new-privileges；
- CPU/memory/PID/time限制；
- private tmpfs；
- 默认零workspace mount；
- mount仅来自批准identity中的最小只读路径，create紧邻前复验source identity；
- allocation labels/nonce用于exact reaper。

sandbox启动固定为 `create -> inspect -> start`：create后inspect effective image digest、entrypoint、
cmd、user、network、mount source/destination/ro、read-only root、capabilities、no-new-privileges、
CPU/memory/PID/tmpfs、allocation labels/nonce；任一项与grant不同就在start调用数0时删除container并
写FAILED_BEFORE_START，无法确认删除则UNKNOWN。禁止只相信传给CLI的flags。

controller崩溃后不尝试重接未知旧stdio。按exact external identity清理；无法证明终态则
OUTCOME_UNKNOWN。

McpProcessEndpoint提供pipes和可验证external identity；不允许subprocess handle进入durable
payload。

### 8.7 transport/session 联动

StdioTransport不再接raw command/env，改接endpoint/factory。I1生命周期继续保留。

- start/close推进allocation events。
- 发送结果区分NOT_SENT/SENT/UNKNOWN。
- timeout/EOF/protocol desync且可能已发送时抛McpOutcomeUncertain。
- close返回CloseReport；cleanup不确定写allocation unknown。
- refresh worker和pending都随endpoint关闭。

fixture增加：

- host env-name/hash probe；
- host file/credential directory/network canary probe；
- startup/init/list独立delay；
- repeated cursor/notification storm；
- 大stderr后正常响应；
- shutdown hang/partial write/EOF。

### 8.8 prepared binding identity

必须分离“跨进程可恢复语义”与“只约束当前live handle的物理会话”：

~~~python
@dataclass(frozen=True, slots=True)
class SemanticMcpBinding:
    server_id: str
    launch_identity_digest: str
    catalog_digest: str
    catalog_epoch_id: UUID
    tool_name: str
    schema_hash: str
    binding_digest: str

@dataclass(frozen=True, slots=True)
class PhysicalSessionFence:
    session_instance_id: UUID
    connection_epoch: int
    catalog_generation: int

@dataclass(frozen=True, slots=True)
class PreparedMcpInvocation:
    prepared_call: object
    semantic_binding: SemanticMcpBinding
    physical_fence: PhysicalSessionFence
~~~

`catalog_digest`覆盖排序tool names + exact schemas；`catalog_epoch_id=UUIDv5(launch identity
namespace, "catalog:" + catalog_digest)`，所以相同已批准launch与catalog在新session仍产生相同
semantic binding。`binding_digest`只覆盖上面Semantic字段，明确排除session instance/connection/
live generation。physical fence只防止旧prepared handle在refresh/close后调用，不进入ledger、
approval或recovery identity。

现有context-free `delegate.prepare(call)`只能返回immutable prepared + semantic/physical binding，
不能构造LogicalExecutionIdentity；它不知道turn/model-turn。禁止prepare后按tool name重新查询
动态registry。

CompositeRegistry提供frozen `ToolCatalogSnapshot`，同一snapshot同时包含definitions、completion
profiles、action resolvers、handlers和semantic bindings。AgentLoop在每个model round开始原子取得
一次snapshot，ModelRequest definitions和该response产生的全部tool call都钉在这个snapshot；
refresh只发布下一snapshot，不能只替换registry其中一张map。当前已prepared调用继续使用旧
snapshot，下一model round才看到新catalog。

Ledger统一类型：

~~~python
@dataclass(frozen=True, slots=True)
class LogicalExecutionIdentity:
    turn_id: UUID
    model_turn_id: UUID
    call_id: str
    binding_digest: str | None
~~~

ledger/claim/load/recovery/approval全部接该类型。executor顺序：

1. delegate.prepare；
2. LedgerExecutor用 `ToolExecutionContext(turn_id, model_turn_id, call_id)` + prepared的semantic
   binding digest构造LogicalExecutionIdentity；
3. ledger load/prepare(identity)，prepared event只保存semantic binding；
4. policy/approval；
5. claim；
6. invoke同一个prepared。

checkpoint pending call保存execution_id/binding_digest。恢复不能用当前binding猜旧identity。
旧binding不可证明时mcp_binding_unavailable_for_recovery或保留UNKNOWN。

跨进程 ASK → approve → 再次run/resume时，physical session必然变化但semantic binding在launch/
catalog相同情况下不变。ledger仍为PREPARED且有证据表明未claim/未发送时，可以在新session精确
重建prepared handle并继续同一execution ID；一旦CLAIMED、SENT或可能执行，physical handle丢失
只能按现有result恢复或UNKNOWN，禁止重调。catalog/schema/launch digest改变则旧approval不平移，
返回 `mcp_binding_unavailable_for_recovery`，不得形成无限重新ASK循环。

ResolvedAction加入mcp_binding_digest/server_identity_digest，并进入action digest。

### 8.9 control plane 与 execution plane

runtime/assembly.py拆为：

~~~python
def assemble_control_plane(config) -> ControlPlaneRuntime: ...
def preflight_execution_activation(
    control: ControlPlaneRuntime, *, command_context: CanonicalCommandContext
) -> GrantedExecutionPlan | ActivationPending: ...
def assemble_execution_plane(
    control: ControlPlaneRuntime, *, granted_plan: GrantedExecutionPlan,
    turn_context: DurableTurnContext,
) -> ExecutionPlaneRuntime: ...
~~~

控制面只创建：

- bounded config；
- EventStore；
- ThreadRuntime；
- ApprovalService；
- status/recovery readers。

preflight只计算/授权launch identity；返回Pending时按8.5不创建Turn。得到GrantedExecutionPlan后
才由App创建Thread/Turn/Run，再交执行面：

    config preflight
    -> launch identity
    -> policy/grant
    -> allocation intent/claim
    -> launcher
    -> initialize/list
    -> registry activation
    -> model client/worker

使用ExitStack；每获得resource立即注册cleanup。完整成功才pop_all。AssembledRuntime/AppRuntime实现
幂等close/context manager。

CLI所有路径with AppRuntime或finally close。status/doctor/approvals/approve/deny/cancel断言：

- model client创建数0；
- MCP launcher调用数0；
- Docker create数0。

legacy doctor不在用户DB创建RUNNING probe。写探针只能用隔离temp DB并完整terminal/close。

### 8.10 I6 测试与迁移

配置：

- v3 JSON显式profile、程序内safe default、v1/v2缺profile migration required；
- six deadlines/resource limits；
- env duplicate/secret/provider/loader拒绝；
- config/code/image drift。

activation：

- host grant前spawn=0；
- deny=0；
- ASK发生在Turn创建前，grant后同semantic command重发只创建一个Turn；
- claim与grant exact precondition同commit；claim后revoke必收束allocation；
- ticket过期/重放/nonce不匹配使OS create=0；
- 批准后替换executable，旧grant失效且spawn=0；
- restart config/argv drift重新ASK；
- staged copy关闭path replacement window。
- spawn成功/started ACK丢失可observed→stopped；不可证明则UNKNOWN，evidence resolution可重放。

sandbox：

- initialize之前host file、credential目录、network不可达；
- immutable image/resource/no mount；
- create后inspect任何effective field漂移都start=0；mount reparse漂移create=0；
- host平台不能强制resource limit时拒绝而非裸spawn；
- crash后exact reaper或UNKNOWN。

identity：

- prepare/claim/recovery同一个binding identity；
- refresh新binding使旧grant失效；
- 旧pending call不平移当前binding；
- ASK后关闭进程、批准、重启session仍以同semantic binding继续PREPARED，不重复ASK；
- physical session变化不改变execution ID，但fence旧live handle；
- 每个model round的definitions/profile/resolver/handler来自同一ToolCatalogSnapshot；
- non-idempotent timeout/EOF为UNKNOWN。

lifecycle：

- control commands不spawn；
- execution assembly任一点失败都关闭已获资源；
- Ctrl+C/异常/正常退出均无进程/线程/container。

兼容：

- 旧request_timeout只读一版。
- 旧MCP config无profile不自动变host。
- 旧ledger若能从历史prepared event唯一恢复binding可读；0或多条为legacy_mcp_identity_ambiguous。
- 旧tool approval不等于process activation grant。

I6完成门：D8/D9/D10/D15/D21聚焦和全量绿；Linux真实sandbox fixture；host/sandbox/env canary；
连续十次无flake与ResourceWarning。

## 9. I7：Unified truth、workspace effect ledger 与 trace 隔离

### 9.1 文件

- src/koawa_agent_v2/control/models.py
- src/koawa_agent_v2/control/runtime.py
- src/koawa_agent_v2/execution/worker.py
- src/koawa_agent_v2/runtime/unified.py
- src/koawa_agent_v2/runtime/app.py
- src/koawa_agent_v2/runtime/cli.py
- src/koawa_agent_v2/runtime/session.py
- src/koawa_agent_v2/workspace/effects.py（新增）
- src/koawa_agent_v2/workspace/content.py（新增）
- src/koawa_agent_v2/workspace/artifacts.py（新增且立即被integration使用）
- src/koawa_agent_v2/workspace/store.py
- src/koawa_agent_v2/workspace/worktree.py
- src/koawa_agent_v2/workspace/integration.py
- src/koawa_agent_v2/workspace/container.py
- src/koawa_agent_v2/telemetry/trace.py
- src/koawa_agent_v2/execution/loop.py
- src/koawa_agent_v2/ledger/executor.py
- src/koawa_agent_v2/mcp/connection_manager.py
- D12/D14/D15/D16/D19/D22测试

### 9.2 显式 Run truth

新增Run aggregate和stream：

~~~python
class RunStatus(StrEnum):
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    ABANDONED = "abandoned"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    OUTCOME_UNKNOWN = "outcome_unknown"
~~~

events：

- run.started.v1
- run.interrupted.v1
- run.abandoned.v1
- run.completed.v1
- run.failed.v1
- run.cancelled.v1
- run.timed-out.v1
- run.outcome-unknown.v1

`INTERRUPTED` 表示该attempt因 Turn进入WAITING_FOR_INPUT/WAITING_FOR_APPROVAL/PAUSED而已收口；
`ABANDONED` 表示expired/stale run被typed requeue并永久fence。两者都是“Run attempt终态”，但不是
business Turn终态。`OUTCOME_UNKNOWN` 对应Turn PAUSED或WAITING_FOR_RECOVERY，Thread保持attached；
证据化resolution把Turn转QUEUED，后续才创建fresh Run。queued-before-start取消不创建伪Run。

start_turn同事务写Turn RUNNING和Run STARTED。以下转换也必须单一append_batch：

- wait/pause：旧Run INTERRUPTED + Turn waiting/paused + Thread仍attached；
- stale requeue：旧Run ABANDONED + Turn QUEUED + lease projection释放；
- uncertainty：Run OUTCOME_UNKNOWN + Turn PAUSED/WAITING_FOR_RECOVERY + Thread attached；
- final terminal：

1. Turn terminal；
2. Thread detach；
3. Run terminal；
4. completion evidence reference。

terminal响应丢失通过receipt恢复。checkpoint/lease/recoverable只是projection，不能反向改写已经
terminal的truth；同一terminal事务必须更新projection，之后若projection仍显示active只报告
`projection_stale`并fail health gate/重建，不能把真实COMPLETED改成UNKNOWN。

旧terminal DB可由Turn started/terminal + Thread detach合成只读LegacyRunState。旧非终态Run禁止
resume/terminal，返回 `legacy_active_run_restart_required`，由I5 export标记manual restart后创建
新Turn；不能在升级后继续向没有显式Run的旧attempt写新事实。

TERMINAL_TURN_STATUSES作为单一常量，包含TIMED_OUT。App/CLI/recovery不各自维护集合。

### 9.3 Unified与App

UnifiedAgentRuntime._durable_turn返回RuntimeTruthDocument，不返回裸turn UUID。删除：

    turn_status = "completed"

UnifiedResult字段从重建truth取得：

- thread_id/turn_id/run_id；
- thread/turn/run status；
- evidence digest；
- agent/result refs；
- ledger/workspace uncertainty。

terminal不接受裸digest，使用：

~~~python
@dataclass(frozen=True, slots=True)
class CompletionEvidenceRef:
    stream_id: StreamId
    stream_version: int
    event_id: UUID
    evidence_digest: str
~~~

权威source是当前run-execution stream上的 `run.final-output-recorded.v1`（或failed/cancelled对应的
typed final evidence），payload绑定run、final canonical text digest、agent result refs、usage和
model receipt digest。terminal batch对该stream exact head + event type/id/payload digest施加
`StreamPrecondition`；不能让调用者随便传一个digest。

每个Run另有 `run-effect-index-{run_id}`。创建tool ledger PREPARED、MCP allocation INTENDED或
workspace effect INTENDED时，同一append_batch追加 `run.effect-linked.v1`，exact记录 effect kind、
stream ID、identity digest、causation run/child agent和首次version。terminal读取index并只检查当前
run及其显式child causation；历史无关UNKNOWN不阻塞。它对每个引用effect的当前exact head施加
precondition，并要求：

- tool ledger只为 SUCCEEDED或FAILED；PREPARED/CLAIMED/OUTCOME_UNKNOWN不允许；
- MCP allocation只为 STOPPED或FAILED_BEFORE_START；CLAIMED/STARTED/READY/UNKNOWN不允许；
- workspace effect只为APPLIED（可含known negative result）或FAILED_BEFORE_EFFECT；open/UNKNOWN
  不允许。

effect index本身也用exact head precondition，防止检查后又新增effect。任一uncertain/open状态时，
同一命令只能写Run OUTCOME_UNKNOWN + Turn PAUSED/WAITING_FOR_RECOVERY并保持Thread attached；禁止
先COMPLETED再事后降级。

成功前RuntimeTruthVerifier验证：

- Turn terminal；
- Thread detached；
- Run terminal；
- evidence匹配；
- 当前run effect index完整，全部引用处于上面的允许终态；
- terminal receipt中的所有StreamPrecondition与重建head/evidence一致。

lease/recoverable projection作为health diagnostic另验；terminal事务本应同步清理，若显示active则
报告projection stale并阻断release gate/触发重建，但不能把已由权威流证明的business terminal
改写成unknown。

不一致返回runtime_outcome_unknown，不能completed。

App的run/chat/resume在worker返回后重新读取Turn/Thread/Run/evidence，再生成CommandOutcome；不使用
loop_result.final_text作为authoritative state。

resume只允许：

- QUEUED；
- recovery coordinator已经typed requeue的expired RUNNING；
- 有明确typed resolution后转换为QUEUED的WAITING/PAUSED。

status报告business truth与diagnostic分区。doctor read-only；写probe只在temp DB。

.session.json是可重建projection，bounded parse，temp+fsync+replace；损坏时从EventStore重建。
任何 `/journal` 写入（包括明确export目录）都必须走JOURNAL_EXPORT typed effect + exact expected
version；目录只改变policy/resource scope，不能绕过effect ledger。只有 `.session.json` 这种可从
EventStore完整重建的projection可直接原子replace。

### 9.4 WorkspaceEffect

~~~python
class WorkspaceEffectKind(StrEnum):
    WORKTREE_ADD = "worktree_add"
    WORKTREE_REMOVE = "worktree_remove"
    ARTIFACT_APPLY = "artifact_apply"
    ARTIFACT_RETEST = "artifact_retest"
    ARTIFACT_DELIVER = "artifact_deliver"
    JOURNAL_EXPORT = "journal_export"
~~~

stream workspace-effect-{effect_id}：

- workspace.effect-intended.v2
- workspace.effect-claimed.v1
- workspace.effect-applied.v2
- workspace.effect-failed-before-effect.v1
- workspace.effect-outcome-unknown.v1
- workspace.effect-outcome-resolved.v1

`workspace.effect-intended.v2` exact identity payload：

~~~json
{
  "effect_id": "uuid-v5",
  "semantic_command_id": "uuid",
  "kind": "workspace-effect-kind",
  "repository_identity_digest": "64-lower-hex",
  "agent_id": "uuid|null",
  "run_id": "uuid",
  "resource_nonce": "uuid-v5",
  "resource_ref": "bounded-controller-relative-ref",
  "base_digest": "64-lower-hex|null",
  "input_digest": "64-lower-hex",
  "precondition_digest": "64-lower-hex",
  "expected_postcondition_digest": "64-lower-hex",
  "intended_at": "UTC"
}
~~~

resource_ref禁止绝对宿主路径，使用controller state/repo内相对ref或opaque ID。其他transition exact
公共fence为 effect_id、claim_epoch、claim_token：claimed另含owner_id/claimed_at；applied.v2另含
`result_kind=success|known_negative`、`result_code`、nullable integer exit_code、postcondition/evidence
digest、applied_at；failed-before-effect另含stable error_code/evidence_digest/failed_at；unknown另含
uncertainty_code、nullable evidence_digest/observed_at。除这些列出的字段外拒绝unknown key。

events只存diff/content/evidence digest，不存整份diff、user正文或stdout/stderr。

`semantic_command_id`来自上层稳定命令；
`effect_id=UUIDv5(NAMESPACE_URL,"koawa-v2:workspace-effect:"+kind+":"+semantic_command_id)`，
`resource_nonce=UUIDv5(effect_id,"resource")`。重试禁止uuid4生成新path/effect。

每个transition exact version + claim epoch/token。`effect-applied.v2` 明确带
`result_kind="success|known_negative"`、postcondition/evidence digest和bounded typed result；例如
ARTIFACT_RETEST确实执行但exit非零是APPLIED/known_negative，不是FAILED/UNKNOWN。
`FAILED_BEFORE_EFFECT` 只用于证明确实未产生效果；旧`effect-failed.v1`只有同等negative evidence
才映射该状态，否则legacy UNKNOWN。部分写入或无法核验为UNKNOWN。

UNKNOWN不是永久死端。`effect-outcome-resolved.v1` exact payload包含 effect_id、原claim token/
epoch、unknown event ID、`resolved_state=applied|failed_before_effect`、result kind、reconciler
principal、evidence kind/digest、resolved_at；append使用unknown exact version。只有exact
repo/path/nonce的可重算postcondition或权威“不存在/未开始”证据可解析，operator自由文本不行。
同一evidence重试返回receipt，不同evidence复用command报conflict。Recovery核对exact
repo/path/nonce/postcondition，不盲重试。

### 9.5 Workspace store/worktree

inventory states：

    INTENDED -> ACTIVE -> REAP_PENDING -> REAPED
              \-> UNKNOWN

store只写projection，不做物理删除。

create顺序：

1. resolve repo/base/dirty snapshot。
2. 生成run+nonce exact path。
3. effect INTENDED/CLAIMED。
4. git worktree add。
5. 核对path、HEAD、git-common-dir、worktree registry。
6. effect APPLIED或UNKNOWN。

旧永远expected=-1模型改为同agent不同run可再次分配。

reap由WorktreeManager orchestration：

1. claim remove effect；
2. git worktree remove exact；
3. 核对path不存在；
4. 核对主库worktree metadata不存在；
5. 才写REAPED/APPLIED。

禁止store先记reaped再直接删目录。cleanup不跟随symlink/junction/reparse。

本切片固定使用detached worktree：从新v2 allocation DTO/event/config删除`branch`参数，命令始终
`git worktree add --detach <exact-path> <base-commit>`。旧event里的branch只读为legacy informational，
不得据此checkout/create分支；以后若需要branch必须新schema和独立fence。

Git command：

- absolute executable；
- shared minimal env；
- no prompt/global config/hooks/external diff；
- stdout/stderr/time有界。

### 9.6 content 与 artifact identity

`workspace/content.py` 先形成 exact `RepoPrestate`：HEAD commit、index content/file identity、
`git status --porcelain=v2 -z --untracked-files=all` raw digest、working tree manifest digest。Git命令
固定绝对executable、禁global config/prompt/hooks/external diff，并使用：

~~~text
git diff --binary --full-index --no-ext-diff --no-textconv <base> --
~~~

repo内 `.gitattributes` 作为受哈希内容生效；外部attributes/config禁用。diff digest是上述raw bytes
SHA-256。manifest按raw relative path bytes排序，记录path bytes digest、entry type/mode、size、
content/target SHA-256；包含tracked/untracked，排除`.git` control dir，不跟随symlink。

每个文件以no-follow handle读取，stat-before/stat-after的file ID/type/size/mtime一致；扫描结束再复验
HEAD、index和status raw digest。任一变化报 `workspace_changed_during_digest`，不能接受混合时点
manifest。上限固定：10,000 entries、单file 16 MiB、decoded总64 MiB、path 4,096 bytes；超限
fail closed，不对截断manifest算digest。

摘要本身不能crash-resume apply。新增JSON-only content-addressed `ArtifactPackageStore`，controller
state根下按 `artifact-packages/<sha256>.json` 原子temp+fsync+replace，私有权限。package exact wire：

~~~json
{
  "package_schema_version": 2,
  "repository_identity_digest": "64-lower-hex",
  "base_commit": "40-lower-hex",
  "tracked_binary_patch_base64": "bounded-base64",
  "entries": [
    {
      "path_bytes_base64": "sorted-raw-relative-path",
      "kind": "untracked_file|symlink|gitlink",
      "mode": 33188,
      "content_or_target_base64": "bounded-base64",
      "content_sha256": "64-lower-hex"
    }
  ],
  "prestate_digest": "64-lower-hex",
  "poststate_manifest_digest": "64-lower-hex"
}
~~~

tracked deletion/content/mode由binary patch表达；untracked file、symlink target、gitlink显式列出。
目录本身不落空entry；submodule working tree有dirty/untracked时拒绝。base64严格canonical、decoded
字节受上述限额，JSON encoded总上限96 MiB。package digest覆盖canonical JSON bytes，ref固定
`sha256:<digest>`；event只存ref/digest/size，不存package正文。package写成功后在artifact stream
追加 `workspace.artifact-package-pinned.v1`，直到apply/retest/deliver/reconcile全部终态并追加
released事件前GC禁止删除；因此源worktree被reap或进程重启仍可恢复。

ArtifactV2 exact字段：schema_version=2、artifact_id、agent_id、run_id、repo_identity_digest、
base_commit、repo_prestate_digest、package_ref/package_digest/package_json_bytes、diff_digest、
working_tree_content_digest、`test_evidence_ref`（stream/version/event/digest）、sandbox image/profile
identity和created_at。artifact ID由run + package digest UUIDv5生成。`head_commit`只作为legacy
informational，不证明未提交内容；old artifact必须重建package才升级。

accept从package和source（若仍在）重算所有identity。integrate要求同base/repo，为apply/retest/
remove写effect，并把权威receipt写 `workspace-integration-{receipt_id}`：

~~~python
@dataclass(frozen=True, slots=True)
class IntegrationReceiptRef:
    receipt_id: UUID
    stream_version: int
    event_id: UUID
    receipt_digest: str
~~~

`workspace.integration-recorded.v1` exact payload包含 receipt_id、repo identity、base commit、排序
artifact IDs、artifact-set digest、combined package ref/digest、integrated content digest、agent/run
fences、所有workspace effect stream refs、test evidence stream ref、sandbox image/profile identity和
recorded_at。receipt digest覆盖该payload。`deliver` 只接 `IntegrationReceiptRef`，必须从EventStore
加载并复验event/head/digest；禁止接受调用方自行构造的字段dataclass。

deliver INTENDED持久 expected `HEAD + working-tree-prestate digest` 和预期postcondition digest。
claim需要per-repo durable delivery lease/stream exact version及同repo OS exclusive lock；apply紧邻前
再次核对完整prestate，不是dirty bool。外部用户不遵守lock时，stat/status postcheck仍能发现；
不符合预期即UNKNOWN。apply后从no-follow snapshot重算content/diff再APPLIED。partial apply为
workspace_outcome_unknown，不自动rollback、覆盖或重投。

InjectedContainerRunner明确test-only；production config不能选。Docker retest绑定sandbox
allocation/execution/image/profile digest。

### 9.7 Trace

保留TraceStore.append供严格管理/测试，新增：

~~~python
class TraceSink(Protocol):
    def emit(self, probe: TraceProbe) -> None: ...

class BestEffortTraceSink:
    def emit(self, probe: TraceProbe) -> None: ...
~~~

- 同进程按correlation串行；
- 跨进程CAS固定次数重读重试；
- 只缓存已allowlist/脱敏/限量的小DTO，不缓存raw；
- per-field和总event bytes硬上限；
- 只捕获`Exception`及已知storage-domain error并映射稳定码；绝不吞KeyboardInterrupt、
  SystemExit、GeneratorExit或async/thread cancellation；
- 维护process-local dropped_since_start/last_error_code/last_failure_at；
- status明确其只是diagnostic。

security/business audit必须在ledger/approval/MCP activation/workspace effect业务事务内。trace不能作为
唯一证据。

production构造器只依赖`TraceSink` Protocol，不能误接“严格失败会冒泡”的裸TraceStore。
execution/loop、ledger/executor、MCP、Unified全部改用sink。ledger result commit后的trace失败不
改变tool返回或Turn状态；trace failure与用户取消同时发生时必须传播取消。

### 9.8 I7 测试与兼容

Runtime：

- Unified响应=重建Thread/Turn/Run/evidence。
- 没有hard-coded completed。
- restart后status/resume/cancel一致。
- TIMED_OUT处处terminal。
- WAITING/PAUSED同批收口Run INTERRUPTED；stale requeue同批ABANDONED；queued cancel不造Run。
- Run UNKNOWN使Turn暂停/Thread attached，typed resolution后fresh Run；legacy nonterminal拒绝resume。
- completion evidence和run-effect index任一head变化使terminal CAS失败；open/UNKNOWN effect只能
  提交OUTCOME_UNKNOWN，不能COMPLETED。
- doctor无active probe和外部resource。
- control commands不启动execution plane。

Workspace fault windows：

- INTENDED后kill；
- CLAIMED后kill；
- git add/remove/apply后ack前kill；
- retest/deliver中kill；
- reaper不能先记成功；
- 主库metadata清理；
- branch contract；
- content digest；
- digest扫描中替换file/index/HEAD/status稳定报workspace_changed_during_digest。
- artifact package含tracked binary、untracked、mode、symlink；源worktree删除后重启仍能integrate。
- forged IntegrationReceipt dataclass无效，只接受durable ref；package retention fence阻止早删。
- per-repo deliver lock + exact prestate；外部并发修改使postcheck UNKNOWN。
- known-negative retest记APPLIED而非FAILED；UNKNOWN凭evidence exact resolution且不可盲重试。
- journal即使写export目录也有typed effect，response loss幂等。
- partial deliver UNKNOWN；
- cleanup失败不覆盖主错误。

Trace：

- concurrent CAS有界retry；
- trace store故障不能破坏已提交model/tool/MCP/workspace结果；
- status显示drop diagnostic。
- trace故障与KeyboardInterrupt/SystemExit/cancellation并发时不吞进程控制信号。

兼容：

- old workspace allocated/reaped标legacy_unverified，exact reconcile后追加v2 APPLIED/UNKNOWN。
- old artifact head只能重算diff/content后升级，否则拒绝deliver。
- old trace只读，无业务迁移。
- oldRun从Turn只读合成，历史event不改。

I7完成门：D1/D4/D5/D6/D7/D12/D14/D15/D16/D19/D22聚焦和全量绿；无遗留Run/Turn/
worktree/effect/trace-induced业务失败。

## 10. I8：生产故障注入、projection 与容量

### 10.1 目标

I2–I7 已在各模块添加命名fault。I8把它们统一接入生产路径、建立可重放脚本和容量证据。
I8不新增业务能力，也不为了指标做大爆炸重构。

### 10.2 文件

- src/koawa_agent_v2/telemetry/faults.py
- I2–I7已有fault hook的生产模块
- src/koawa_agent_v2/agents/index.py（只有benchmark证明需要时才新增并立即使用）
- src/koawa_agent_v2/control/schema.py
- src/koawa_agent_v2/control/sqlite_store.py
- tests/test_stability_fault_matrix.py（新增）
- tests/test_stability_capacity.py（新增）
- tests/fixtures/stability_fault_worker.py（新增）
- scripts/stability_benchmark.py（新增）
- docs/stability-capacity-baseline.json（运行后生成的受审计基线）

### 10.3 FaultInjector

保留现有D14 failure points兼容，新增stable names。提供：

~~~python
class FaultPort(Protocol):
    def hit(self, point: str, facts: Mapping[str, JsonPrimitive]) -> None: ...
~~~

生产默认NoOpFaultPort。只有显式测试注入对象可启用；禁止从不可信config/env开启。

facts：

- 只允许小型ID/version/attempt/phase/code primitive；
- 不含task/user text/result/diff/env/credential；
- 先走durable JSON小profile。

after_commit fault表示“提交成功但响应丢失”，不能rollback已提交事实；before_append必须0 event。
外部effect后的fault必须按实际ledger状态恢复，不把异常一律当FAILED。

`telemetry/faults.py` 建唯一 machine-readable `FAULT_REGISTRY: tuple[FaultSpec, ...]`；spec字段固定为
`name/point_class/marker_timing/expected_event_delta/recovery_oracle`。生产代码只能用registry导出的
常量调用 `hit()`，测试双向扫描：每个生产hit都在registry、每个registry point至少一个生产hit和
kill test。不得用prefix/wildcard表示未列出的point。

I2/I3 exact registry：

| name | class | marker/event delta | recovery oracle |
| --- | --- | --- | --- |
| d11.enqueue.before_append | BEFORE_APPEND | 调用前/0 | safe retry |
| d11.enqueue.after_commit | AFTER_COMMIT | receipt提交后/+1 | receipt replay |
| d11.deliver.before_append | BEFORE_APPEND | 调用前/0 | safe retry |
| d11.deliver.after_commit | AFTER_COMMIT | delivered已提交/+1 | same delivery |
| d11.provider.entered | EXTERNAL_ENTERED | provider marker/0 | may not retry after later uncertainty |
| d11.provider.returned | EXTERNAL_RETURNED | result在内存/0 | no durable result => unresolved |
| d11.result.before_append | BEFORE_APPEND | result canonical完成/0 | unresolved unless provider proves retryable |
| d11.result.after_commit | AFTER_COMMIT | RESULT_RECORDED/+1 | ACK only |
| d11.ack.before_append | BEFORE_APPEND | 调用前/0 | ACK retry |
| d11.ack.after_commit | AFTER_COMMIT | ACKED/+1 | receipt replay |
| d11.unresolved.after_commit | AFTER_COMMIT | UNRESOLVED/+1 | WAITING |
| d11.waiting.after_commit | AFTER_COMMIT | Agent WAITING/+1 | operator resolution |
| d11.resume.before_append | BEFORE_APPEND | 调用前/0 | stay WAITING |
| d11.resume.after_commit | AFTER_COMMIT | Agent RUNNING/+1 | same fresh run |
| d11.cancel.before_append | BEFORE_APPEND | 调用前/0 | decision retry |
| d11.cancel.after_commit | AFTER_COMMIT | cancel event/+1 | receipt replay |
| d11.resources.baseline.before_append | BEFORE_APPEND | snapshot fixed/0 | recompute snapshot |
| d11.resources.baseline.after_commit | AFTER_COMMIT | baseline/+1 | receipt/replay |
| d11.spawn.after_read | AFTER_READ | 四流snapshot/0 | full reread |
| d11.spawn.before_append | BEFORE_APPEND | 写集已构造/0 | full retry |
| d11.spawn.after_commit | AFTER_COMMIT | 4 events/同commit | receipt replay |
| d11.heartbeat.before_append | BEFORE_APPEND | 调用前/0 | same-run retry |
| d11.heartbeat.after_commit | AFTER_COMMIT | heartbeat/+1 | receipt replay |
| d11.orphan.before_append | BEFORE_APPEND | candidate已复验/0 | rediscover |
| d11.orphan.after_commit | AFTER_COMMIT | ORPHANED/+1 | takeover race |
| d11.takeover.before_append | BEFORE_APPEND | 两流写集/0 | full reread |
| d11.takeover.after_commit | AFTER_COMMIT | Agent+unresolved同commit | same run receipt |
| d11.terminal.after_read | AFTER_READ | effect heads固定/0 | full reread |
| d11.terminal.before_append | BEFORE_APPEND | 写集完成/0 | full retry |
| d11.terminal.after_commit | AFTER_COMMIT | terminal/releases/result同commit | receipt replay |

I4/I5 exact registry：

| name | class | marker/event delta | recovery oracle |
| --- | --- | --- | --- |
| s3.event.after_validate_before_begin | BEFORE_APPEND | bounded snapshot/0 | retry |
| s3.event.mid_batch_before_receipt | IN_TRANSACTION | 事务内/0 committed | rollback old |
| s3.checkpoint.after_source_read | AFTER_READ | source anchor/0 | recompute |
| s3.checkpoint.before_cache_commit | IN_TRANSACTION | cache未提交/0 | full replay |
| s3.checkpoint.after_cache_commit | AFTER_COMMIT | cache projection/+0 truth | cache可丢 |
| s3.checkpoint.after_verify_before_tail | AFTER_READ | verified base/0 | full verify |
| s3.migration.after_ddl | IN_TRANSACTION | DDL未提交/0 | complete old |
| s3.migration.before_user_version | IN_TRANSACTION | ledger未提交/0 | complete old |
| s3.migration.after_user_version_before_commit | IN_TRANSACTION | version仅事务内/0 | old或new原子 |
| s3.export.after_source_scan | AFTER_READ | source RO/0 | source unchanged |
| s3.export.mid_destination_import | EXTERNAL_PARTIAL | partial only/0 final | discard partial |
| s3.export.after_verify_before_rename | BEFORE_EXTERNAL_COMMIT | verified partial/0 final | atomic rename retry |

I6 exact registry：

| name | class | marker/event delta | recovery oracle |
| --- | --- | --- | --- |
| s4.activation.after_request_commit | AFTER_COMMIT | REQUESTED/+1 | show pending |
| s4.activation.before_grant_append | BEFORE_APPEND | decision ready/0 | resolve retry |
| s4.activation.after_grant_commit | AFTER_COMMIT | GRANTED/+1 | receipt replay |
| s4.allocation.after_intent_commit | AFTER_COMMIT | INTENDED/+1 | claim or stop |
| s4.allocation.after_claim_commit | AFTER_COMMIT | CLAIMED/+1 | inspect exact external identity |
| s4.launch.after_external_create_before_started | EXTERNAL_AFTER_EFFECT | Job/container exists/0 started | observed/UNKNOWN |
| s4.allocation.after_started_commit | AFTER_COMMIT | STARTED/+1 | reap exact identity |
| s4.allocation.after_ready_commit | AFTER_COMMIT | READY/+1 | normal close/reap |
| s4.mcp.initialize.after_send_before_result | EXTERNAL_AFTER_SEND | bytes sent/0 result | UNKNOWN/close |
| s4.mcp.initialize.after_result_before_commit | EXTERNAL_RETURNED | response bounded/0 ready | safe session rebuild, no tool effect |
| s4.mcp.list.after_page_before_cursor | EXTERNAL_RETURNED | bounded page/0 catalog | restart whole bounded list |
| s4.mcp.list.after_catalog_commit | AFTER_COMMIT | snapshot published/+1 catalog fact | use same snapshot |
| s4.mcp.call.after_send_before_result | EXTERNAL_AFTER_SEND | call bytes sent/0 result | ledger UNKNOWN |
| s4.mcp.call.after_result_before_ledger | EXTERNAL_RETURNED | result bounded/0 ledger | reconcile by execution ID or UNKNOWN |
| s4.mcp.refresh.after_list_before_publish | AFTER_READ | new snapshot in memory/0 | discard |
| s4.mcp.close.after_terminate_before_stopped | EXTERNAL_AFTER_EFFECT | tree terminate issued/0 stopped | inspect/reap/UNKNOWN |

I7 exact registry：

| name | class | marker/event delta | recovery oracle |
| --- | --- | --- | --- |
| s5.run.before_terminal_append | BEFORE_APPEND | all heads verified/0 | full reread |
| s5.run.after_terminal_commit | AFTER_COMMIT | Turn/Thread/Run/evidence同commit | receipt replay |
| s5.workspace.add.after_intent_commit | AFTER_COMMIT | INTENDED/+1 | claim |
| s5.workspace.add.after_claim_commit | AFTER_COMMIT | CLAIMED/+1 | inspect path |
| s5.workspace.add.after_effect_before_ack | EXTERNAL_AFTER_EFFECT | git add发生/0 applied | postcondition/UNKNOWN |
| s5.workspace.remove.after_intent_commit | AFTER_COMMIT | INTENDED/+1 | claim |
| s5.workspace.remove.after_claim_commit | AFTER_COMMIT | CLAIMED/+1 | inspect path/metadata |
| s5.workspace.remove.after_effect_before_ack | EXTERNAL_AFTER_EFFECT | remove发生/0 applied | postcondition/UNKNOWN |
| s5.workspace.apply.after_intent_commit | AFTER_COMMIT | INTENDED/+1 | claim |
| s5.workspace.apply.after_claim_commit | AFTER_COMMIT | CLAIMED/+1 | verify prestate |
| s5.workspace.apply.after_effect_before_ack | EXTERNAL_AFTER_EFFECT | apply发生/0 applied | postcondition/UNKNOWN |
| s5.workspace.retest.after_intent_commit | AFTER_COMMIT | INTENDED/+1 | claim |
| s5.workspace.retest.after_claim_commit | AFTER_COMMIT | CLAIMED/+1 | test identity check |
| s5.workspace.retest.after_effect_before_ack | EXTERNAL_AFTER_EFFECT | test ran/0 applied | evidence positive/negative |
| s5.workspace.deliver.after_intent_commit | AFTER_COMMIT | INTENDED/+1 | claim |
| s5.workspace.deliver.after_claim_commit | AFTER_COMMIT | CLAIMED/+1 | verify repo prestate |
| s5.workspace.deliver.after_effect_before_ack | EXTERNAL_AFTER_EFFECT | user tree may change/0 applied | postcondition/UNKNOWN |
| s5.trace.cas_conflict | SYNTHETIC_CONFLICT | trace WEV/0 business | bounded trace retry |
| s5.trace.drop | DIAGNOSTIC_FAILURE | trace dropped/0 business | business unchanged |

`marker_timing` 必须对应表中可观察事实：AFTER_COMMIT先从新connection读到event再通知父进程；
EXTERNAL_AFTER_EFFECT先取得OS/Git/provider marker再通知；IN_TRANSACTION在同connection fault并由kill
触发rollback。`expected_event_delta`在registry中使用结构化整数/stream集合，不解析上表文本。

同一seed/script必须得到同一normalized event sequence和failure taxonomy。时间、UUID、临时绝对路径
在比较前规范化，但不能删除业务state差异。

### 10.4 subprocess kill矩阵

tests/fixtures/stability_fault_worker.py：

- 父进程通过marker等到命名point durable可见；
- 使用process.kill真正终止OS进程；
- 新进程从同一DB/repo恢复；
- 不能只销毁Python object。

每个窗口断言：

- no lost durable task；
- no silent duplicate external effect；
- unknown explicit；
- stale owner fenced；
- receipt重试幂等；
- resource最终APPLIED/STOPPED或UNKNOWN；
- 无预算/slot/lease/inventory漂移。

### 10.5 projection/index

Event log仍是真相。只有benchmark证明现有全流扫描不达阈值时才新增projection。

AgentIndex建议端口：

~~~python
class AgentIndex(Protocol):
    def children(self, parent_id: UUID) -> tuple[AgentIndexRow, ...]: ...
    def running_with_expired_lease(self, now: datetime) -> tuple[AgentIndexRow, ...]: ...
    def active_resources(self, root_id: UUID) -> ResourceCounts: ...
    def rebuild_from_events(self) -> None: ...
~~~

若使用SQLite表：

- append_batch同事务投影已知typed events；
- 表可删除后从Event log重建；
- 读取用于定位candidate，最终授权/transition仍重放目标stream并exact fence；
- schema在control/schema.py注册migration；
- projection mismatch触发rebuild或fail closed，不把row当truth。

禁止让AgentGraph依赖SqliteEventStore concrete type；通过端口注入。

### 10.6 benchmark

scripts/stability_benchmark.py只用标准库，输出包含commit、Python、OS、SQLite、硬件摘要、样本数、
p50/p95/max和资源增长的JSON。

可重复协议 `stability-benchmark-v1`：

- `PYTHONHASHSEED=0`、CPython 3.12 release build、GC开启、无debugger/profiler；数据seed固定
  `0x4b4f4157415632`。
- 每个scenario在本地SSD独立temp目录生成相同layout；SQLite使用生产设置
  `journal_mode=WAL, synchronous=FULL, foreign_keys=ON, busy_timeout=5000`，page/cache/temp设置不
  另行调优。报告完整PRAGMA结果。
- read scenario分cold（新进程/新connection）和warm（同进程新connection）；write scenario每个
  sample从同一verified seed DB副本开始。并发spawn使用独立OS processes和barrier，不用threads/
  sleep。
- 每场先5个不计样warmup，再至少30个measured samples；10k rebuild至少20个。duration用
  `perf_counter_ns`，p95用nearest-rank `sorted[ceil(0.95*n)-1]`，不插值。每场运行3批，门取三批
  p95的中位数；任一批有错误则整场FAIL。
- 背景CPU在采样前60秒平均必须<5%，可用空间≥数据集的5倍；否则标
  `environment_not_qualified`，只收集不判性能。
- reference lane要求≥4个独占x86_64/arm64逻辑核、≥16GiB RAM、本地SSD，并且CPU model、OS
  build、Python/SQLite、filesystem和power profile匹配受审计baseline的
  `reference_environment_digest`。不匹配的开发机永远只收集，不能据下表宣称release pass/fail。

场景：

- 10,000条run-execution event rebuild/checkpoint。
- 1,000 Agent × 每Agent 100 message 的next/list/wait。
- 100并发spawn争固定parent/root budget。
- 1,000 heartbeat/takeover循环。
- 100 MCP pending + notification storm。

初始门：

| 操作 | p95 |
| --- | --- |
| 10k event verified checkpoint rebuild | <1秒 |
| 单Agent next mailbox | <100ms |
| 无竞争spawn事务 | <100ms |
| wait_agents 100活跃Agent周期 | <250ms |

Nightly soak 24小时：

- 无budget/capacity漂移；
- 无stuck DELIVERED/claim；
- 前30分钟warmup后每60秒采样RSS、线程、handle/fd、DB/WAL大小；RSS least-squares slope
  ≤1 MiB/hour且结束值≤warmup median+64 MiB，thread结束≤+2、handle/fd结束≤+8且二者斜率
  ≤0.1/hour；
- 无不可解释active resource。

阈值如需调整，必须提交新基线、硬件证据和原因；不能只改数字让失败变绿。

`docs/stability-capacity-baseline.json` exact记录protocol version、commit、reference environment
digest、dataset digests、三批raw summary、nearest-rank结果、thresholds和soak slopes/absolute
deltas。reference runner才强制p95/soak阈值；非reference report字段
`threshold_enforced=false`，I9 release manifest不能拿它替代reference lane。

### 10.7 I8 测试与完成门

- 每个命名point实际命中一次，unknown point稳定拒绝。
- 同seed两次事件序列等价。
- after_commit response loss只产生一份事实。
- legacy export任一fault后source DB/WAL/SHM字节不变、最终destination不存在或完整；migration任一
  fault后重开必须是完整old或完整new，user_version/ledger/schema/postcheck互相一致且无partial。
- projection删除重建结果等于Event replay。
- projection stale不能授权状态变化。
- benchmark达到门；soak无增长。
- 全量回归绿。

## 11. I9：mandatory lanes、完整E2E与发布证据

### 11.1 本地lane runner

新增 scripts/stability_gate.py，标准库实现：

~~~text
python scripts/stability_gate.py --lane pr-fast --report out.json
python scripts/stability_gate.py --lane integration --report out.json
python scripts/stability_gate.py --lane mcp --report out.json
python scripts/stability_gate.py --lane docker --report out.json
python scripts/stability_gate.py --lane golden --report out.json
python scripts/stability_gate.py --lane soak --report out.json
python scripts/stability_gate.py --lane provider-opt-in --report out.json
python scripts/release_manifest.py verify --manifest release-manifest.v1.json
~~~

runner：

- 使用unittest loader和自定义TestResult；
- 记录discovered/passed/fail/error/skip/duration以及每个exact test ID的PASS/FAIL/ERROR/SKIP；
- 记录Python/OS/SQLite/Git HEAD/Docker image digest；
- ResourceWarning视为error；
- report使用temp+fsync+os.replace，并带`report_schema_version=1`和排除自身digest字段后canonical
  JSON的SHA-256；
- unexpected skip使lane失败；
- Docker doctor不ready在mandatory lane是FAIL，不是skip。

approved skip manifest固定为 `docs/stability-approved-skips.json`，schema v1 exact keys为
`schema_version/generated_for_commit/entries/manifest_digest`；每个entry必须包含：

- exact test id；
- 当前lane；
- reason code；
- 替代执行lane；
- 替代test id；
- 到期条件。

只有同一exact commit/build/config上的替代lane report中 `replacement_test_id=PASS` 时skip才批准；
verifier重算manifest digest，entry过期、replacement只在计数中没有逐test PASS、或lane report缺失
都fail closed。

仓库根CI wiring不在v2范围内。禁止在v2/.github创建一个GitHub不会读取的假workflow；需要中央CI
接入时由用户另行授权根目录变更。本文交付可独立调用的lane runner。

### 11.2 lane定义

| Lane | 环境 | 模块 | 核心skip |
| --- | --- | --- | --- |
| pr-fast | Windows+Linux Python3.12 | 全离线unit/contract/security/eval | 仅映射平台项 |
| integration | Windows+Linux | SQLite multiprocess、kill/restart、Git/worktree | 0 |
| mcp | Windows+Linux fixture process | D9/D10 activation/lifecycle/ledger/env | 0 |
| docker | Linux real daemon immutable image | D8/D12/MCP sandbox、OOM/timeout/reaper/escape | 0 |
| golden | Linux fresh DB/repo + Docker + MCP fixture | 强制矩阵/composite/dispatch | 0 |
| soak | 标准机 | I8长流/并发/recovery/resource | 0 |
| provider-opt-in | 隔离release runner + credential reference | real provider smoke/multiturn/empty completion/cleanup | 0 |

Docker、MCP、golden每一类必须在指定lane完整discovery、零核心skip、全部PASS；“三类没有全部
跳过”不是合格门槛。

### 11.3 强制E2E矩阵

扩展现有 tests/test_golden_composite_e2e.py 与 tests/fixtures/golden_worker.py；不得再用当前
“一个只读subagent + 固定completed”作为最终证据。

必须覆盖：

1. 单Agent成功修改并测试。
2. 首次测试失败，有界repair后成功。
3. 坏模型流/缺terminal时工具执行数0。
4. 安全checkpoint后kill，新进程恢复。
5. effect可能已发生时OUTCOME_UNKNOWN且不盲重试。
6. approval allow、deny、参数/资源漂移再ASK。
7. Docker路径、network、resource、secret隔离。
8. 真实sandboxed MCP initialize/list/call/close。
9. 两个写Agent独立worktree改不同文件并集成成功。
10. 两写Agent改同行进入显式artifact conflict。
11. 恶意repo文本/MCP输出不能扩大action权限，transport未授权调用数0。
12. 旧Worker/Agent late result被fence。
13. 全进程重启后state/checkpoint/ledger/trace/artifact/diff均可查询且与Event replay一致。

### 11.4 不可拆 golden composite

同一个durable Turn真实贯通：

    Policy/Approval
    -> atomic Ledger claim
    -> sandboxed MCP fixture
    -> readonly + two writing subagents
    -> independent worktrees + Docker
    -> first test failure + repair
    -> OS process kill
    -> new process resume
    -> artifact integration/delivery
    -> evidence final

kill用subprocess真正终止。最终oracle从EventStore/ledger/workspace/Run重建，不用model final或
state.json自证。

### 11.5 dispatch contract

单独测试built-in、MCP、subagent三类入口都遵循：

    Schema/Registry
    -> Policy/Approval
    -> Ledger
    -> Sandbox/Transport

对每类分别注入绕过尝试，断言：

- transport/handler调用0；
- ledger无succeeded；
- audit有稳定DENY；
- credential canary不落库。

三个模块各自绿不能替代这条装配接线测试。

### 11.6 release truth audit

新增只读命令：

    koawa-agent-v2 release-audit --config ... --report ...

报告：

- active Run/Turn/Agent；
- DELIVERED/RESULT_RECORDED/UNRESOLVED/REQUEUED message；
- active capacity/budget reservation；
- unclosed ledger claim/pending approval；
- expired/live lease；
- MCP allocation；
- workspace inventory/effect；
- trace dropped diagnostic；
- DB schema/migration；
- canary scan result。

release场景明确允许的WAITING/pending必须逐项列出；默认所有active resource为0。命令只读，不因
审计自动reap、release、cancel或修复。

### 11.7 provider evidence

真实provider不进入无secret PR lane，但发布候选必须执行 `provider-opt-in`。credential只通过
runner secret reference/scope注入；report保存scope digest，不保存name/value。lane逐test证明
smoke、multi-turn、empty completion稳定处理、timeout/cancel和resource cleanup，记录provider/model、
safe config digest、release commit/build digest和日期；截图/人工文字不算证据。

新增 `scripts/release_manifest.py` 作为跨lane fail-closed聚合器；`release-audit`继续只检查durable
runtime truth，不承担CI证据判断。release manifest exact顶层：

~~~json
{
  "manifest_schema_version": 1,
  "candidate_id": "bounded-id",
  "release_commit": "40-lower-hex",
  "build_artifact_digest": "64-lower-hex",
  "canonical_config_digest": "64-lower-hex",
  "docker_image_digest": "sha256:...",
  "approved_skip_manifest_digest": "64-lower-hex",
  "generated_at": "UTC",
  "lane_reports": [
    {
      "lane": "pr-fast",
      "os": "windows|linux",
      "series_id": "candidate-id:lane:os",
      "ordinal": 1,
      "report_path": "relative/path.json",
      "report_digest": "64-lower-hex"
    }
  ],
  "release_audit_report": {"path": "relative", "digest": "sha256"},
  "fresh_demo_report": {"path": "relative", "digest": "sha256"},
  "migration_reports": [{"kind": "current-next|legacy-export", "path": "relative", "digest": "sha256"}],
  "manifest_digest": "64-lower-hex"
}
~~~

verifier重算每个report/manifest canonical digest并要求report内 commit/build/config/image（适用lane）
与manifest完全一致。`pr-fast`、`integration`、`mcp` 在Windows和Linux各要求同series ordinal
1/2/3连续PASS；`docker`、`golden` 在Linux各三次；`soak`恰有一次reference 24h PASS；
`provider-opt-in`至少一次PASS。每份report的unexpected skip=0，approved skip逐条在替代report中
找到同commit exact test PASS。

release audit、fresh offline demo、current→next与legacy export report也必须同candidate且PASS。
普通lane/provider/demo在manifest生成前7天内，24h soak在14天内，且全部finished_at晚于candidate
build time；混commit、缺digest、过期、非reference性能、缺ordinal或替代test只被skip均FAIL。
任何代码、build、canonical config、image或approved-skip manifest变化都会改变identity并使旧证据
失效，不能拿历史D20证据替代。

### 11.8 I9发布门

1. release manifest verifier通过；Windows/Linux规定lane各连续三次failures=errors=0。
2. Docker/MCP/golden各自零核心skip并PASS。
3. P0/P1为零，独立review无阻断项。
4. ResourceWarning、遗留process/thread/handle/worktree/container为零。
5. release audit active resource为零或逐项允许。
6. current→next migration和legacy export证据绿。
7. DB/WAL/SHM/temp/backup canary无credential原值/完整env。
8. I8性能/soak通过。
9. fresh machine离线demo确定性通过。
10. provider-opt-in逐test证据绑定当前release candidate且在新鲜度窗口内。
11. 路线图、实现文档、错误码、config example与代码一致。

## 12. 文件所有权与冲突边界

| 文件域 | 主单元 | 其他单元规则 |
| --- | --- | --- |
| agents/** | I2/I3 | I9只改fixture/集成，不重写状态机 |
| control/event_store.py | I2/I4 | I2只加clock/fingerprint合同；I4加JSON限制 |
| control/sqlite_store.py | I2/I4/I5 | I5拥有schema/recovery projection；不得平行修改 |
| recovery/** | I5 | I2/I3只通过公开接口 |
| runtime/config.py | I1/I4/I6 | 按顺序演进；每次同步example/tests |
| mcp/transport/session | I1/I6 | I1生命周期；I6 launcher/activation接线 |
| ledger/**、policy/approval | I6 | I7只换trace sink，不改identity |
| workspace/** | I7 | I9只构造场景 |
| telemetry/** | I7/I8 | I7 sink；I8统一fault |
| golden/eval/scripts | I8/I9 | 不反向改变生产语义 |

若两个ready单元都需要同一文件，后者必须基于前者全量绿的HEAD开始；不使用cherry-pick覆盖另一
单元的用户改动。

## 13. Agent 任务卡模板

维护者向实现Agent下发：

~~~text
实施单元：
上游计划章节：
源码基线：
允许修改文件：
必须先读文件：
必须保持的旧事件/错误码：
需要新增的事件/stream：
精确append_batch：
兼容/迁移：
聚焦测试：
跨切片测试：
禁止事项：
完成命令：
~~~

实现Agent handoff必须回答：

~~~text
1. 实际修改文件
2. 新增/改变的公开API
3. 新事件wire与reducer兼容
4. 每个多流事务的writes/preconditions/fingerprint
5. DB/config/旧数据迁移行为
6. fault points与kill窗口
7. 运行命令、passed/failed/skipped
8. process/thread/handle/worktree/active resource检查
9. 尚未完成或新发现P0/P1
10. 下一单元是否ready
~~~

不接受“测试通过了”但没有命令/计数，也不接受只给diff不解释durable transaction。

## 14. 实现中的停止条件

立即停止并回报：

- 需要修改legacy Java或v2外文件。
- 需要改写/删除历史event才能让reducer通过。
- 需要把UNKNOWN降成FAILED或自动retry非幂等effect。
- 需要完整host env、credential value、raw hidden reasoning落库。
- 需要先spawn MCP才能处理activation approval。
- 需要先记workspace成功再做git/Docker外部效果。
- CAS只能通过去掉expected version或run fence。
- checkpoint只能通过信任caller context而非event reducer。
- 为完成当前单元必须新增未审计依赖或扩大网络。
- 聚焦绿但全量出现新failure/error/skip/resource leak。

## 15. 文档同步规则

每个单元完成时同步：

- 本文对应单元的实际API/event/error与偏差；
- 上游规划的issue状态；
- 对应day文档的完成/重开状态；
- examples/p0_config.example.json；
- test数量不写死，只记录当次discovery报告；
- 新配置的兼容窗口和deprecation。

若实现与本文不同但通过测试，仍不能直接把本文改成代码现状。先确认差异没有削弱Event truth、
fence、ledger、sandbox、UNKNOWN和用户workspace保护，再更新规范。

## 16. 本实施文档的完成定义

本文可交给其他Agent使用，意味着：

- 每个问题有明确实施单元和文件所有权；
- state machine/event payload/stream/transaction/precondition已写明；
- 旧事件/config/DB/artifact兼容路径已写明；
- fault和process-kill窗口已写明；
- 聚焦、跨切片、全量、mandatory、release门已写明；
- Agent知道何时继续、何时停止以及handoff必须提供什么。

它不意味着I1–I9已经完成。代码状态只能由Event/ledger/workspace事实、测试与release audit证明。
