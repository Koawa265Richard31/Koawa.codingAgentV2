# P0 反例登记（稳定化 PREFLIGHT 产物）

> 目的：为规划书 §4.1 的 P0-01..P0-10 各保存一份确定性复现合同——禁止 sleep 猜时序，
> 使用 barrier / fake clock / 命名 fault point / 子进程 kill+新进程恢复。
> 修复后唯一 oracle 是后续 I 单元完成门的判定依据；预期红测在本单元修绿前不进主 discovery。
> 对应实现单元：I1=S4-A、I2=S1、I3=S2、I4=S3-A、I5=S3-B/S6-A、I6=S4-B、I7=S5、I8=S6。

## P0-01 mailbox 用单消息版本当流 expected version

- Owner I-unit：I2（S1）
- 现有聚焦命令：
  `py -3.14 -B -W error::ResourceWarning -m unittest tests.test_d11_agent_control tests.test_d11_agent_scheduler -v`
- 确定性 setup：同一 Agent 依次 enqueue m1、m2（barrier 保证两事件各自提交）；再 enqueue m3。
- 当前可观察反例：deliver m1 时若以 m1 的 MessageRecord.version 作为 mailbox stream expected version，
  m1 之后 m2/m3 的 enqueue 会推进 mailbox stream，使旧 m1 的 deliver 永远 WEV——消息卡死。
- 修复后唯一 oracle：deliver/ack/requeue 一律使用 mailbox 当前 stream head version；enqueue m2/m3 后
  m1 仍可 deliver，且 m2/m3 保持可 deliver；重放与 live projection 逐消息一致。
- 允许的临时资源与有界清理：内存 SQLite + TemporaryDirectory；无需子进程。

## P0-02 DELIVERED 后 ACK 前崩溃 → 任务永久丢失

- Owner I-unit：I2（S1）
- 现有聚焦命令：`py -3.14 -B -m unittest tests.test_d11_agent_scheduler -v`（另见 test_d7_process_kill 模式）
- 确定性 setup：命名 fault point `d11.provider.entered`（Provider 返回后、RESULT_RECORDED 提交前）
  用子进程 marker 等事实 durable 后真正 kill -9 OS 进程；新进程从同一 DB 恢复。
- 当前可观察反例：恢复路径只查 QUEUED → 把 DELIVERED 当成 no_more_messages，scheduler 直接把 Agent
  标 completed；已交付消息既无 result 也无 unresolved 标记。
- 修复后唯一 oracle：DELIVERED 且无 RESULT_RECORDED 的消息在恢复后进入 UNRESOLVED/OUTCOME_UNKNOWN（或经
  证明确未执行且幂等时显式 requeue）；任何状态都不得出现存在未结消息却又 completed。
- 允许的临时资源与有界清理：独立 sqlite 文件 + 子进程 fixture worker；kill 后进程树必须收束。

## P0-03 并发 spawn 越过最后一个 slot

- Owner I-unit：I3（S2）
- 现有聚焦命令：`py -3.14 -B -m unittest tests.test_d11_agent_scheduler -v`
- 确定性 setup：barrier 让两个 spawn 同时到达父 capacity 检查之后；命名 fault point
  `d11.spawn.before_append`（与实现文档 §5.8 权威名一致）。
- 当前可观察反例：parent 状态/深度/每父并发检查不在事务中，两个并发 spawn 可同时通过检查并
  各自提交 child/budget，越过最后一个 slot（容量超卖）。
- 修复后唯一 oracle：capacity/budget 与 spawn 同 append_batch 原子 committed；loser 零事件；
  capacity stream exact version 保证 slot 数不超过上限。

## P0-04 provider 期间无 LeaseKeeper → 慢 worker 被错误 takeover

- Owner I-unit：I2 最小 keeper（S1）/ I3 完整（S2）
- 现有聚焦命令：`py -3.14 -B -m unittest tests.test_d11_agent_scheduler tests.test_d6_recovery -v`
- 确定性 setup：fake DB clock；provider 用 barrier 持续存活跨过至少两个 lease 周期；
  另一线程执行 discover_orphans。
- 当前可观察反例：无 heartbeat 期间 owner 被判定 orphan，新 run takeover 后同一 provider 被再次调用
  （重复外部副作用窗口）。
- 修复后唯一 oracle：存活但慢的 provider 在 lease 内不被 redeliver；heartbeat 推进 owner 版本；
  takeover 后的 late result 被 run fence 拒绝；discover_orphans 排除带有效 lease 的活 run。

## P0-05 Agent terminal 与 budget release 分开提交 → 崩溃泄漏预算

- Owner I-unit：I3（S2）
- 现有聚焦命令：`py -3.14 -B -m unittest tests.test_d11_agent_control -v`
- 确定性 setup：命名 fault point `d11.terminal.before_append`（terminal 已提交、release 未提交窗口）。
- 当前可观察反例：terminal 与 budget 释放是两次 append；窗口内崩溃 → budget 永久占用，
  后续 spawn 被错误拒绝或 root 预算漂移。
- 修复后唯一 oracle：terminal v2 单事务同时写 Agent terminal + capacity-released + budget-released.v2；
  legacy 未释放条目可由 reconcile 稳定修复且不改历史。

## P0-06 Unified runtime 返回 completed 而持久 Turn 仍 running

- Owner I-unit：I7（S5）
- 现有聚焦命令：`py -3.14 -B -m unittest tests.test_unified_runtime tests.test_d15_e2e -v`
- 确定性 setup：run 正常返回后不 close；新进程重开同一 DB 读状态；doctor 执行后检查进程/线程残留。
- 当前可观察反例：Unified 响应 hard-code completed 或基于内存状态，而事件库 Turn 仍 running；
  doctor 的 probe 未收束。
- 修复后唯一 oracle：对外响应与重建后的 Thread/Turn/Run/evidence 一致；restart 后 status/resume/cancel
  同态；doctor 只读且无 active probe/资源残留。

## P0-07 checkpoint 接受覆盖位置与 hash 都合法的伪造 projection

- Owner I-unit：I5（S3-B）
- 现有聚焦命令：`py -3.14 -B -m unittest tests.test_d6_recovery -v`
- 确定性 setup：构造含合法 coverage hash 的 checkpoint，但 context/phase/counter/pending 与事件归约不同
  （写 checkpoint v2 之前先写一份 v1 伪造样本测试 v1 不被信任）。
- 当前可观察反例：checkpoint 只核对覆盖事件位置/hash → 伪造的内容被接受，恢复出错误状态。
- 修复后唯一 oracle：checkpoint 每个字段等于 reducer 对同段事件的归约；伪造内容即使 hash 合法也拒绝并
  回退到全量重放；v1 checkpoint 一律视为 cache miss。

## P0-08 host_trusted MCP 继承完整 os.environ → credential 扩散

- Owner I-unit：I1 最小环境（S4-A）/ I6 activation（S4-B）
- 现有聚焦命令：`py -3.14 -B -m unittest tests.test_d10_transport_protocol tests.test_d10_integration -v`
- 确定性 setup：父进程设置 provider key / 随机 canary / 云凭据形态变量；fixture server 输出其可见 env 键名。
- 当前可观察反例：StdioTransport.open 使用 dict(os.environ) → child 可见全部父环境变量（含密钥）。
- 修复后唯一 oracle：child 只看到平台必需 + 显式 allowlist；canary/secret-like 名称与值均不可见；
  脱敏规则拒绝 secret/token/password/api-key/authorization 形态；Windows 平台变量由受信目录解析。

## P0-09 durable 文本没有统一字节/深度/节点底线

- Owner I-unit：I4（S3-A）
- 现有聚焦命令：`py -3.14 -B -m unittest tests.test_event_store tests.test_d2_protocol_hardening -v`
- 确定性 setup：直接向 EventStore append 超深/超节点/超字节 payload（limit+1）。
- 当前可观察反例：部分入口缺少深度/节点/集合上限；错误正文可能回显 credential-like 原文。
- 修复后唯一 oracle：EventStore 对所有 payload 先验 canonical JSON 限制；limit 成功、limit+1 稳定拒绝且
  无部分事务；user/resume/prompt/summary 在持久前统一脱敏，首轮与恢复同值。

## P0-10 旧库可能含原始 user_input：无迁移策略

- Owner I-unit：I4/I5（S3-A/S3-B + S6-A export）
- 现有聚焦命令：`py -3.14 -B -m unittest tests.test_event_store tests.test_d6_recovery -v`
- 确定性 setup：构造旧 schema 的 SQLite（含 raw user_input 列样本）；尝试以 production mode 打开；
  运行 offline export；对 DB/WAL/SHM/备份做 canary 扫描。
- 当前可观察反例：production mode 直接打开旧库，raw 文本仍可被读取；无 fresh export 路径。
- 修复后唯一 oracle：旧 schema 在 production mode 拒绝打开；export-to-fresh 只归约允许的 terminal 元数据
  并经 canonical sanitizer 写入，带 legacy-store-imported.v1 来源 digest；DB/WAL/SHM/temp/backup 的 canary
  扫描不得出现原值；active 执行不续跑，改为 requires_manual_restart。

## 附：确定性手段清单（引用规划书 §6.2/§9/§12）

- barrier：多线程/多进程同时抵达后放行（不用 sleep 猜序）；
- fake/DB clock：lease/expiry 用注入时钟或 SQLite 权威时钟；
- fault point：D14 FaultInjector 命名点（d11.*、s3.*、s4.*、s5.* 见实现文档）；
- 子进程 kill：fixture worker marker 等事实 durable 后用 OS 级 kill；新进程从同一 DB/repo 恢复；
- canary：随机凭据形态串，断言不进入 child env / sqlite / trace / 转写。
