# I8 实施进度与未闭环项

更新日期：2026-08-28。依据：`v2-stabilization-detailed-implementation.md` 第 10 节。

此文件是实施交接记录，不是 I8 完成证明。I7 的既有完成记录不替代 I8 回归；I9 尚未开始。

当前特别注意：下文保留了真实WAL反例的历史失败记录；该反例已在2026-08-30增量中修复并有
聚焦绿色证据，但尚未完成最终全量回归、77点全矩阵、三批reference资格和24h soak，因此仍不能
声称当前工作树或I8已完成。

## 已有可执行证据

- `telemetry/faults.py` 提供唯一命名注册表、FaultPort、默认 NoOp、RecordingFaultPort 和 D14 兼容选择器。分类采用完整名称的显式映射，不用前缀推导；修正了 checkpoint-cache 和 Run-terminal 两个 after-commit 窗口。
- `tests/test_stability_fault_matrix.py` 验证注册表和生产调用点存在性、未知点拒绝、终态响应丢失幂等，以及 workspace 的 15 个转换窗口。
- `tests/fixtures/stability_fault_worker.py` 已覆盖四个真实 OS kill 窗口：`s5.run.before_terminal_append`、`s5.run.after_terminal_commit`、`s3.event.after_validate_before_begin`、`s3.event.mid_batch_before_receipt`。marker 发布前通过新连接核对可见事实；父进程 kill；新解释器从原 DB 恢复并重试两次；结果断言三流原子终态、无重复和规范化事件摘要等价。
- `scripts/stability_scenarios.py` 建立真实 typed run-execution 数据集（model/tool transcript）、Agent/mailbox 数据集和四流 spawn seed。checkpoint 测量调用 `RecoveryCoordinator.reconstruct`；mailbox/list/wait 测量调用生产控制面，不再用模拟事件查询代替。
- `scripts/stability_benchmark.py` 记录冷进程/热进程测量、nearest-rank、原始纳秒样本、三批协议配置、生产连接 PRAGMA、实际逻辑事件摘要、环境摘要和显式未完成场景。write 样本从 SQLite backup 的同一已校验 seed 开始；新命令的计时不包含初始化/复制。系统 CPU 采样使用系统忙闲时间，不用采集器自身的 CPU 时间。
- `tests/test_stability_capacity.py` 的 12 项测试通过，涵盖百分位计算、同种子数据等价、真实生产路径、checkpoint 丢失后的重建、spawn 四流原子性与回执幂等、PRAGMA 和完整 quick 子进程报告。
- `docs/stability-capacity-baseline.json` 是实际生成的 **quick/developer collection**。`threshold_enforced=false`、`release_pass=null`，不能作为 reference lane 或发布成功证据。

### 本轮增量：真实压力场景与 soak 驱动

- `scripts/stability_load.py` 新增三个可单独运行、也已接入 benchmark 三批采样协议的场景：100 个独立 OS 进程经 socket barrier 同时争 parent/root budget；1,000 heartbeat/orphan/takeover 循环；100 个 MCP 请求同时 pending 时接收 2,048 条通知和乱序响应。
- spawn 逐个验证进程身份、成功回执、四流提交、预算/槽位/子节点集合一致性，并在结算后要求所有预留归零。单次 100 进程试跑已完成：4 个成功，96 个 CAS 重试耗尽，预留峰值 4、最终 0；这是实际争用结果，不代表必须把 8 个槽位填满，也不是性能门通过。
- heartbeat/takeover 每轮验证心跳回执幂等、新 attempt/run、旧 owner 无法再写、预算不漂移；保留每轮耗时及状态轨迹。生产 run UUID 本来就是随机值，原始最终事件摘要不伪装成跨独立运行相同。1,000 次独立长跑已结束，最终资源归零；耗时异常及其证据边界见下文，不能作为 reference PASS。
- MCP fixture 暂扣所有响应直到显式 barrier 放行；实测峰值 100 pending，第 101 个请求在发送前被拒绝，100 个结果正确匹配，pending 最终为 0，通知合并后的 refresh 收敛且关闭无相关线程残留。
- `scripts/stability_resources.py` 使用 Windows API / Linux proc 读取真实 RSS、OS 线程、handle/fd、DB/WAL 大小；实现 warmup median、least-squares slope、绝对增量及样本完整性判定。短时数据与非参考机数据不能返回 24h PASS。精确采样 cadence/reference 证据仍需完成门审计，不因存在统计函数就视为验收完成。
- `scripts/stability_soak.py` 提供持续混合工作负载和独立分钟采样线程，覆盖真实 mailbox 生命周期、heartbeat/takeover、OS 进程争用、checkpoint 重建和 MCP storm；最终必须无未释放资源。quick 模式使用较小数据形状、明确禁止 reference pass。
- `tests/test_stability_resources.py`、`tests/test_stability_load.py` 与既有 capacity 合计 24 项通过；`tests/test_stability_soak.py` 的 2 项测试通过，其中短跑必须实际覆盖五类工作负载且不能声称 24h 通过。
- 约 30 秒 quick soak 已运行，五类工作负载各 6 次、资源最终归零；报告在 `.dsh_tmp/i8-soak-smoke.json`，`reference_qualified=false`、`passed=null`。九个场景的 quick benchmark 已重新生成，`errors=[]`。

补充执行命令：

```powershell
python scripts/stability_load.py --case spawn --workers 100 --capacity 8 --budget 8 --report .dsh_tmp/i8-spawn-100.json
python scripts/stability_load.py --case heartbeat --cycles 1000 --report .dsh_tmp/i8-heartbeat-1000.json
python scripts/stability_load.py --case mcp --pending 100 --notifications 2048 --report .dsh_tmp/i8-mcp-100.json
python scripts/stability_soak.py --quick --hours 0.008333334 --sample-seconds 1 --report .dsh_tmp/i8-soak-smoke.json
```

完整 24h 驱动命令为 `python scripts/stability_soak.py --hours 24 --sample-seconds 60 --report <path>`。
当前硬件 attestation 未完成，不能把执行这条命令等同于取得 reference PASS。

执行命令（PowerShell，在 `v2/`）：

```powershell
$env:PYTHONPATH = 'src'
python -m unittest tests.test_stability_capacity -v
python -m unittest tests.test_stability_fault_matrix tests.test_d14_trace_fault_eval -v
$env:PYTHONHASHSEED = '0'
python scripts/stability_benchmark.py --quick --report docs/stability-capacity-baseline.json
```

10,000 条真实执行事件的单次开发机检查已成功生成及重建，重建约 5 秒；这不是 reference 环境下的三批统计，不可据此判定性能门通过/失败或调整阈值。

## 必须继续实现/验证，不能视作完成

1. D11 callable hooks、S3 hooks 与 FaultPort 的统一接入、兼容适配及生产常量化已完成并有聚焦证据；完整双向实际命中/kill 覆盖证明仍未完成，AST 引用存在不等于执行证据。
2. 对每个注册点建立实际命中的 kill/restart 案例。共享 worker 已有 25 个窗口的执行证据（原四点 + S3 十点 + trace 两点 + worktree 六点 + activation 三点），不能替代 D11、allocation/MCP、artifact apply/retest/deliver 的全矩阵；后续生产修正还需重跑受影响场景。S3 export 还缺真实 WAL/SHM 下的保全与 kill 证据。对 external-after-effect 窗口必须取得真实 OS/Git/provider marker，不能以直接调用 `record_applied` 充当外部执行。
3. 精确补齐 `expected_event_delta` 的流集合及变体：例如 Run 暂停与完成、资源有无父节点/多个 unresolved 消息，不能通用硬编码一个总数。
4. facts 已经过 durable JSON 小 profile 与逐字段元数据白名单；UUID、surrogate/NUL、嵌套值、整数/布尔边界已有反例，MCP page/tool_count/generation 漏项已修复；仍需在后续实际故障矩阵中验证所有路径。
5. 三个压力场景已实现并接入 benchmark；100 进程、100 MCP pending 与 1,000 接管的单次规模试跑完成。全部 full 形状的三批性能证据及参考机判定仍缺失，不能拿 quick 或单次规模试跑替代；接管每轮耗时增长仍需定位。
6. 24h soak 驱动、资源采样、严格分钟 cadence 和统计判定已有实现及短跑/单测证据；真实 24h、完整 reference qualification 及资源无增长的验收证据仍未取得。
7. 硬件/reference attestation 和资格核对仍缺 filesystem、power profile、本地 SSD、独占 CPU 等证据。当前故意不允许任何 quick/full 采集被判定为 reference pass；不得仅输入一个 digest 就解锁门禁。
8. 跑完整参考场景后决定是否需要 AgentIndex。当前不应根据 quick 单次结果新增索引；若新增必须真实使用、注册 schema migration、删除重建等价且 stale projection 不能授权。
9. 全部 I8 改动的全量回归及逐项完成门审计。只在上述项目和规范第 10.7 节全部有证明后完成 I8、进入 I9。

## 全量回归

2026-08-28 已执行 `PYTHONPATH=src python -m unittest discover -s tests -v`：

```text
Ran 683 tests in 522.127s
OK (skipped=17)
```

本次完整输出位于 `.dsh_tmp/i8-full-regression.log`。17 项为现有环境/平台能力跳过；
这次绿色回归证明当前改动没有破坏已有测试，不证明上述待办或 I8 完成门已经满足。

以上压力/soak 增量后的全量回归已经结束，日志为 `.dsh_tmp/i8-load-full-regression.log`：

```text
Ran 697 tests in 498.191s
OK (skipped=17)
```

本次 697 项回归只覆盖该次运行启动时的代码；仍不能替代接管长跑的最终结果、参考机三批容量基线、
24h 实测或尚未补齐的 77 点 kill/restart 全矩阵。

## 2026-08-28 后续增量：S3 kill/restart 与真实反例

- `telemetry/faults.py`：注册表导出 `FaultPoint` 常量，D11 旧 callback 通过适配器接新 port；
  旧 message ID 列表仅保留在旧 callback，新 port 仅接 count/digest。S3 采用显式 scoped port，
  不开放 config/env 开关。telemetry 包改为延迟导出，冷启动不同导入顺序测试无循环依赖。
- `scripts/stability_fault_audit.py` 双向审计源码常量和已知调用；77 个注册点均有引用，
  没有未知点、旧字符串字面调用和未知静态 facts key。该审计不宣称 kill 覆盖完成。
- `tests/fixtures/stability_s3_faults.py` 接入共享 worker：3 个迁移、3 个导出、4 个 checkpoint
  窗口，每点执行两次真实 OS kill / 新解释器恢复。迁移核对完整旧 schema/version/ledger 和
  原有数据保留，再重复升级；导出核对最终目标未提前发布及 partial 可恢复；checkpoint 核对
  新连接缓存可见性、缓存删除后的事件重建及重发幂等。组内同 seed 恢复事实等价。
- 该矩阵抓到并修复两个生产缺陷：`after_cache_commit` 原先在 commit 前；导出 partial 已提交后
  重跑因 `datetime.now()` 进入默认幂等指纹发生冲突。现将 hook 移到 commit 后，导出命令使用
  source digest + canonical summaries/manual restart 的语义指纹，并在发布前逐条核对摘要。
- 33 项 faults/S3 聚焦测试通过（61.932s，`ResourceWarning=error`）。随后加入真实 WAL 反例，
  **该数字不能代表加入 WAL 反例后的全部 S3 测试通过**。
- 元数据白名单最初漏掉 MCP 三个整数键，全量回归暴露后已修复。新的 38 项 metadata/MCP
  聚焦测试通过（22.587s）。初次全量运行 `.dsh_tmp/i8-s3-full-regression.log` 已中断，
  包含旧代码错误，不是完成证据；当前代码已重跑，日志 `.dsh_tmp/i8-s3-full-regression-current.log`，
  此记录更新时尚未结束。
- soak 校验现在拒绝密集采样、插入额外样本和累计时间漂移；允许 ±1s 调度误差及24h后的
  单个收尾样本。资源/soak 12 项通过，后续 registry/资源13项也通过。
- 当时 1,000 次接管长跑只读查询为 807 次 `agent.taken-over.v2`；最终结果已取得，见后续增量。

### 尚未修复的真实 WAL 反例（优先继续）

`tests.test_s3_legacy_export.LegacyExportTest.test_committed_wal_is_read_without_changing_source_db_wal_or_shm`
已实际失败：源 DB 和 WAL 摘要不变，但 SHM 摘要改变。构造使用真实 `journal_mode=WAL`、
已提交但未 checkpoint 的新 outcome，保留一个静止连接使 WAL/SHM 存在；不是伪造 sidecar 文本。
旧测试只证明假的 sidecar 文件和普通 rollback DB，不能覆盖这个窗口。

原因定位：`classify_database` / `_read_legacy_streams` 的 SQLite `mode=ro` 仍可维护共享内存
read mark；`schema.py` 的实现只比较主 DB 摘要和 sidecar 是否新增，与完整字节保全合同不一致。
SQLite 官方说明 SHM 用于客户端访问协调及 WAL 索引，参见
[WAL-mode File Format](https://www.sqlite.org/walformat.html)。

已做本地探针：`immutable=1` 确实不改变源 SHM，但读到旧主 DB outcome，忽略 WAL 中的新结果，
因此不能作为修复；`mode=ro` + exclusive/nolock 在当前 Windows 环境返回 I/O/open 错误，亦未采用。
不能通过移除 SHM 断言、丢弃 WAL、回写源 SHM、自动 checkpoint 源库或把裸 canary 复制进
目的目录来伪造通过。这个反例必须修复后再声称全量绿色，之后将真实 WAL 种子接入三个 export kill 窗口。

已向维护者请求确认是否允许将直接 SQLite `mode=ro` 读取改为有界、仅驻留内存的 DB+WAL
一致快照读取（保留源字节不变，不落盘原始数据）。尚未收到明确答复；自动目标续行不是批准。
本轮不修改该规格，不宣称该问题已修复；继续推进其他不依赖此选择的 I8 工作。

## 2026-08-28 后续增量：trace 两点与初始化并发

- `tests/fixtures/stability_trace_faults.py` 已接共享 worker。真正的 SQLite 竞争写入先提交 trace，
  原请求再以旧 stream version 写入而产生 WEV，不直接伪造异常或调用 fault hook。
  分别在 `s5.trace.cas_conflict` 和 `s5.trace.drop` 命中后发布 marker，由父进程 OS kill。
- 工具使用 MANUAL_WRITE profile，由独立 OS 子进程创建并 fsync effect JSON；marker 先用
  新连接确认工具 ledger 已 SUCCEEDED、effect execution ID 匹配且 trace 重试没有改变业务事件。
  新解释器两次取回同一结果，禁止再次调用 delegate，再重复完成 Turn；claim epoch 始终1。
  每点两次独立运行，保留全部业务 payload/stream version/commit 结构，仅规范化 UUID 和
  忽略事件 envelope 时间及无关 trace 全局位置，归一化业务事件序列相同。
- `BestEffortTraceSink` 修复长期 correlation 锁表积累：按活跃 emitter + waiter 引用计数，
  最后一个使用者退出即移除；不依赖 GC 或 weakref（存储适配器保留异常 traceback 时仍可能
  持有旧 emit frame）。1000 个 correlation 失败后锁表为0，八个并发使用者共享同一把锁。
- trace 故障回调的 `concurrent.futures.CancelledError` 现在原样传播，不能记作普通 drop；
  取消路径也释放 correlation 引用。原进程控制信号测试继续通过。
- `.dsh_tmp/i8-s3-full-regression-current.log` 已完成：710 tests / 575.355s /
  FAILED(failures=2, skipped=17)。两项失败为已知 WAL 源 SHM 保全，以及双进程迁移后的
  `PRAGMA journal_mode=WAL` 锁竞争。此日志是失败证据，不是绿色完成证明。
- `SqliteEventStore._initialize` 修复后者：已有 WAL 不重复切换；仅对 SQLITE_BUSY 最多四次
  重试，所有尝试共享原 busy timeout 时间预算；先关闭旧连接，再用新连接的空 BEGIN IMMEDIATE /
  rollback 等待 SQLite writer lock，避免持有互相阻挡的读锁，不靠 sleep 决定先后。
  非 BUSY 不重试，返回非 WAL 时 fail closed。测试覆盖重试前关闭连接、次数上限、总 deadline、
  非 BUSY 分支和真实两进程场景。测试子进程创建禁用弹窗，失败清理也回收仍运行的子进程。
- 11 项 trace/D14 聚焦通过；随后 30 项 schema/trace/fault-matrix 聚焦通过（94.230s，
  ResourceWarning=error）；真实双进程迁移测试连续20次通过（30.876s）。这些不替代全量或77点矩阵。
- 该轮全量回归已结束，日志 `.dsh_tmp/i8-trace-full-regression.log`：718 tests / 578.810s /
  FAILED(failures=1, skipped=17)，唯一失败为真实 WAL 源 SHM 字节保全。
  真实 WAL/SHM 反例仍存在且未跳过，I8 尚未到达完成门，I9 尚未开始。

## 2026-08-28 后续增量：worktree 恢复与 activation 响应丢失

- `tests/fixtures/stability_worktree_faults.py` 覆盖 add/remove 的 INTENDED、CLAIMED、
  effect-before-ack 六点：使用真实 Git 仓库、父进程 OS kill、新解释器恢复；marker 前以新连接
  验证事件、以 Git 与文件验证物理状态。每点两轮规范化事件序列一致。
- `WorktreeManager` 为创建、删除和恢复加同仓库 OS 排他锁；父进程在故障进程仍持锁时尝试恢复，
  必须被拒且零事件。kill 释放锁后才允许恢复；CLAIMED 转 UNKNOWN，再以真实后置条件或权威
  不存在证据收口，不盲重放 Git。ADD 尚未发生可确认 FAILED_BEFORE_EFFECT；REMOVE 仍存在则
  保持 UNKNOWN。APPLIED 响应重试不再执行 add/remove，inventory 通过原语义命令补齐。
- 物理检查包含 managed 路径及 reparse、detached HEAD、toplevel、common Git dir 与注册表。
  不将 Git 查询失败当作“不存在”。metadata 检查包含同 nonce 残留及其他管理目录的 gitdir
  指针；指针使用有界 no-follow 读取和文件身份复验，无法核验不允许推导已清理。
- 修正 OS 锁的异常范围：只有锁获取错误映射 locked，受保护操作的 I/O 错误保留原异常并释放锁。
  Windows 实测 lstat/fstat 的 ctime 含义不同；ctime 在各自 API 前后比较，跨 API 仍严格比较
  file ID/type/link count/size/mtime。避免把未变化的指针误判为修改，不取消身份复验。
- 早一轮 worktree/fault-matrix/D12/effect-store 聚焦为36项、202.436s、OK(skipped=1)。随后新增
  metadata 等反例后的八项 worktree 恢复测试通过（23.533s）；随后 worktree恢复/D12/真实Git六窗口
  跨切片回归16项通过（184.141s，skipped=1：Docker daemon不可用）。
- `tests/fixtures/stability_activation_faults.py` 覆盖 request-after-commit、grant-before-append、
  grant-after-commit 三点，含人工批准和 policy allow 共五种场景，各两轮真实 kill/restart。
  marker 检查精确 activation 流/版本/commit；恢复不创建 allocation 或进程 ticket，不自动启动。
- 新测试复现人工批准的响应丢失缺陷：批准已提交后同请求重试抛 activation_already_resolved。
  现仅对当前同一批准人、同一人工批准返回原授权，不续 TTL、不恢复撤销授权；不同批准人、
  相反决定、policy 授权不冒充人工回执。非 bool 决定在写入前拒绝。
  三点矩阵和原七项 activation 测试合计8项通过（37.316s）；补充撤销及类型边界后九项
  ActivationServiceTest 通过（1.907s）。这些不证明 launcher/allocation 的剩余窗口已覆盖。
- `.dsh_tmp/i8-worktree-full-regression.log` 的完整 discovery 已结束：724 tests / 756.118s /
  FAILED(failures=1, skipped=17)，唯一失败仍为真实 WAL 源 SHM 字节保全。
  它启动于后续 metadata/activation 修正之前，不是最终当前工作树验收证据。

### 1,000 次接管最终结果与证据限制

`.dsh_tmp/i8-heartbeat-1000.json` 已生成：cycles=1000、attempts=1001、
stale_owners_fenced=1000；最终 active_children/budget/capacity 均0，历史 children=1。
总耗时35,662.093秒（约9.9小时），其中两轮超过60秒，最长21,402.175秒；没有证据把这些停顿
全部归因于实现。单轮中位3.105秒、p95=6.801秒；前十轮中位约0.138秒、后十轮约6.432秒，
仍有明确增长趋势待性能分析。该运行加载的是此前版本，不替代最终代码三批性能证据；
threshold_enforced=false、release_pass=null，不是reference lane或24h soak通过。

### 用户指定工作流与待决事项

用户已指定 huan-dev。当前验收范围仍为按顺序完成 I7–I9，不缩减完成门、不进入未通过前置的I9。
该工作流要求重型改动先确认独立压力审查人数：已建议2个只读审查Agent，尚未获明确答复，
因此尚未启动审查；自动目标续行不是批准。此前 WAL 只读合同的方案选择也仍待确认。
当前仅收取已运行测试、校准事实和维护交接记录；不据此声称实现或独立审查已完成。

## 2026-08-30 增量：内存WAL快照与压力审查缺陷收口

本节覆盖上面的历史待决状态：维护者已明确批准“有界、仅驻留内存的DB+WAL一致快照”，并批准
两个独立只读压力审查。两个审查均独立复现worktree非规范metadata指针缺陷；另分别指出Git子进程
输出未在采集前受限，以及activation缺少请求代际、并发append泄漏`WrongExpectedVersion`。第二个
审查在最终工具用量处中止，但已交付可复现发现；不能表述为两个审查均完整通过。

- `control/read_snapshot.py` 现在只用OS只读句柄采集DB/WAL/SHM/journal，双读+身份复验，解析WAL
  checksum与最后commit marker，在私有image中覆盖页面后deserialize到`:memory:`。总介质256 MiB、
  image 256 MiB、SHM 4 MiB、frame 262,144；最多四次新鲜重试共享deadline。源上不创建raw副本、
  不打开SQLite磁盘连接、不checkpoint/recovery。合法未提交tail被忽略，截断/checksum错误、hot
  journal、变化中和超限介质fail closed。
- legacy export分类、查询及source digest使用同一成功快照；行读取改为streaming，并增加100,000
  events及64 MiB payload UTF-8累计上限。
- worktree拒绝含`..`或非`normpath`的绝对gitdir metadata指针，不能把等价非规范路径当作权威
  不存在。Git子进程stdout/stderr各4 MiB实时受限、双管并发drain、60秒deadline，超限先kill再返回
  `git_worktree_output_limit`，不再先无界`capture_output`。
- activation operator决定现在强制exact `expected_version`并写入decision代际。响应丢失/并发同
  决定返回原回执且不续TTL；旧决定不能批准新REQUESTED代际；并发异决定稳定返回
  `activation_version_conflict`，不泄漏存储版本异常。CLI和交互pending路径传递document version。

当前聚焦证据（均`ResourceWarning=error`）：

- S3 snapshot/export/schema + worktree recovery + interactive联合：56 tests / 62.709s / OK。
- activation完整模块：27 tests / 21.376s / OK，含旧批准跨代、并发同决定、并发异决定。
- activation三点真实kill/restart矩阵：1 matrix test / 21.847s / OK（内部五场景各两轮）。
- 接手复核补充：fault-matrix + s3 read_snapshot/schema/legacy export + worktree recovery + d11 activation
  + i7 durable integration 联合：91 tests / 475.425s / OK（`ResourceWarning=error`）。

## 2026-08-30 接手复核：修复 write-in-progress 分类回归

复核聚焦矩阵时抓到并修复一个真实回归：`SqliteEventStore` 打开路径（`ensure_schema` →
`classify_database`）在**写事务进行中**（如 `s3.event.mid_batch_before_receipt` kill 窗口，fault
worker 的 `KillPort.hit` 契约要求 IN_TRANSACTION 时新连接仍读到 old 状态）打开失败，
`database_classification_failed` 稳定复现（两次 76/403s 聚焦均失败）。

- 根因：GPT 侧 08-30 增量把 `classify_database` 从 SQLite 原生 `mode=ro` 连接换成裸 OS 句柄
  `read_snapshot`（为修 export 的 SHM 字节保全）。但裸读会读取整个 `-shm` 文件，而 SQLite 写
  事务进行中通过 LockFileEx 持有 wal-index 字节范围的 **Windows 强制锁**，`os.read` 撞锁抛
  PermissionError（`database_snapshot_unavailable`，4 次重试全失败）。SQLite 原生 `mode=ro`
  由 SQLite 自身协调锁，写事务进行中仍可读已提交状态（WAL 快照隔离），不受影响。
- 修复：`classify_database` 保留 `read_snapshot` 作为静止源字节保全路径；仅当快照报
  `database_snapshot_unavailable`（活跃写者持锁）时回退到 `_read_only_connection`（SQLite
  `mode=ro`）完成分类。其他失败保持 fail-closed，非 SQLite 文件仍稳定
  `database_classification_failed`，静止库零写入、不新建 sidecar 语义不变。
- 验证：三场景最小复现（写事务中分类 OK / 文本文件 fail-closed / 静止库字节与 sidecar 不变）
  + 91 项聚焦全绿。

这些证据关闭上述三个压力审查缺陷、真实WAL源字节反例和 write-in-progress 分类回归；仍不替代
受影响范围的最终独立复审、全量discovery、剩余D11/allocation/MCP/artifact窗口、reference三批
与24h soak，I8仍未完成，I9未开始。
