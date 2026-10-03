# E 轨道实现闭环审查与修复交接

日期：2026-09-25。审查基线：`73270c2dbddfbf790ba6b295351920add041b003`。审查时本地 HEAD 与实时查询的 GitHub main 一致；交接写入前工作区干净。本报告是针对既定设计的接线与正确性检查，不是全仓安全扫描，也不是修复完成声明。

## 结论与交接范围

当前只有部分生产接线，尚未形成“按当前权限生成安全结果 → 发布 → 首次回执 → 历史读取 → 恢复与撤销 → 最终请求检查”的闭环。优先收口已引入链路，不继续用新增模块数量代替完成度。

本会话仅审查并记录，未修改运行时代码、未提交或推送。下一实施会话应先按 AGENTS.md 对齐仓库，检查本报告之后的修改，避免重复修复或覆盖并行工作。

设计依据：

- [完整设计](hardening-memory-retrieval-2026-09-19/proposals/bounded-result-retrieval.md)
- [首版实施契约](hardening-memory-retrieval-2026-09-19/implementation/contracts.md)
- [工作包与验收顺序](hardening-memory-retrieval-2026-09-19/implementation/bounded-capture.md)

用户已经确认设计方向；“下一部分”默认确认上一方案，有修订时以最后接受版本为准。敏感信息边界优先，无法安全自动完成时转人工。容量数字须实测，不自行拍默认值。真实敏感资源权限不能由设计授权推导。

## R1：第四次投影发布版本冲突，生产路径静默中止

优先级：先修。性质：独立存储探针已复现；生产异常吞没由源码确认。

位置：`src/koawa_agent_v2/retrieval/projection.py:40` 的 `_head_version` 使用 `after_version=-1, limit=2`。`control/sqlite_store.py:513` 按 stream_version 升序返回，因此这里只得到最早两条，不是流头。第三条写入后，读取仍返回版本 1，第四次追加会与实际版本 2 冲突。

独立临时 SQLite 存储中，使用同一 turn 和五个不同 call/event 身份依次调用 publish，实际输出：

```text
publish 1 OK
publish 2 OK
publish 3 OK
publish 4 WrongExpectedVersion ... expected 1, actual 2
publish 5 WrongExpectedVersion ... expected 1, actual 2
stored 3
```

`runtime/app.py:481` 的 `_publish_result_projections` 把整个扫描/发布循环包在 try 中，`:527` 使用 `except Exception: return`。因此实际 App 遇到第四次冲突后会退出循环，后面的记录也不会继续发布，调用者没有可观察的投影缺失状态。

修复要求：正确读取流头；处理并发 CAS 与幂等冲突；发布失败可以不改写已完成的工具执行结果，但必须登记/暴露安全的投影不可用或待恢复状态，不能默默称作完成。不能因投影失败重执行工具。

验收：同 turn 至少五个不同结果全部可发布；同身份重试不重复写；同身份不同内容拒绝；并发写、发布故障和重启后可恢复；生产 App 能观察发布失败。不要只把测试从一次发布改成三次。

## R2：投影发布时间、读取来源和长流扫描未闭环

优先级：与 R1 连续收口。性质：生产路径源码观察。

`runtime/app.py:478` 在 worker.execute 返回后调用投影发布。模型先前工具回合已拿到回执，此处才补写元数据事件；它不满足设计的“首次安全正文来自已提交投影”。该结构也无法为正在执行的长 turn 提供刚产生结果的已发布读取来源。

`retrieval/recall_tool.py` 调用 SessionMemory.recall，查询会话历史，而不是 `result-projection` 流。源码中未见模型读取工具连接 ResultProjectionStore.read；当前回忆工具不能当成设计要求的按结果引用读取。投影目前只针对带测试输出标记的记录，也不能代表测试与 MCP 的通用发布链已完成。

`retrieval/projection.py:109` 扫描 run-execution 仅取前 1000 条；`:96` 读取投影也只取 1000 条，没有翻页和不完整标记。超长任务后续事实可能被遗漏。此截断问题是源码推断，本次未构造超过 1000 条的实验。

另外，scan_test_results 仅凭回执 JSON 包含 `test_output_policy` 字段识别测试来源（`:131`），没有在该函数内核对可信工具调用身份。字段标记不能独立充当来源证明；实施时用可信调用身份与契约版本绑定。本次未实施伪造来源攻击实验。

修复要求：按设计明确工具执行、投影发布和首次回执的顺序；执行事实与投影失败分离；将模型查询/读取接到受权限约束的发布结果；长流分页或明确不完整，不能默默遗漏。首次结果与回读内容一致，不能借 body_ref 直接开放旧原始事件正文。

验收：真实 App 路径执行测试后，在同 turn 或后续授权回合找到并读取相同安全投影；重启后身份与内容一致；撤销/过期拒绝；超过扫描页边界仍正确；写正文后 CAS 失败、已发布但索引未更新、正文缺失均不导致工具重执行。

## R3：“metadata_only”未等同于经过授权的安全元数据

优先级：安全边界必须先于扩展正文读取。性质：源码确认返回内容；未声称已发生真实秘密泄漏。

`retrieval/recall_tool.py:90` 直接返回 user_input 前 120 字，`:92` 返回 final_text 前 120 字，同时带文件名/工具名。当前线程限制存在，但没有针对这些字段的当前接收方与内容释放检查。历史输入或回复可能含受限内容；裁短和 `visibility=metadata_only` 标记不提供安全证明。

`verification/tools.py:171` 的回执虽然移除了 stdout/stderr，仍直接返回 argv、exit_code、duration_ms、输出长度等。`mcp/connection_manager.py:685` 返回正文长度。所检查路径没有按具体 profile 决定这些事实是否可披露，也没有落实独立的 withheld/human_required 语义。

注意：在获准的安全测试中返回这些诊断是合理能力；问题是当前实现按统一字段直接公开，不能表达敏感测试的不同释放权限。不是要求一律隐藏所有结果。

修复要求：可信 profile/适配器决定允许字段；不把用户输入/最终回复的自由文本预览当作元数据自动开放；当前模型接收方及策略在首次回执、历史读取、恢复、摘要和最终请求都必须生效。缺乏敏感释放规则时返回独立不可提供状态，不能伪装成测试失败。

验收：使用合成秘密置入历史输入、回复、路径、argv、测试名和输出长度相关场景，观察最终 provider 请求；撤销权限或更换模型接收方后旧内容不自动重放；允许的安全测试仍能得到足够诊断；不可披露不会触发错误补丁或探测式重测。

## R4：请求容量门仍依赖压缩路径，未满足最终发送检查

优先级：与 R3 共同收口。性质：源码确认；现有小范围测试通过不覆盖全部分支。

`execution/loop.py:346` 在 memory 缺失或关闭 in_run_compaction 时提前返回；`:494` 的 `_definitions_chars` 只计 input_schema_json，不计工具名/描述等最终协议内容。压缩后部分检查再次只算 context_chars + reserve。当前仍先 `_maybe_compact`，后取本轮工具快照和构造 ModelRequest。

已实现的正向增量：InstructionMessage 已加入计量；无 sink 时也检查部分总量。不能把这两项等同于完整契约。

修复要求：以最终确定的消息与同一本轮工具快照进行独立检查，包含协议开销及适用输出预留；是否压缩、是否有 sink、是否存在可压缩组不影响门禁。使用适用计量器或明确的保守估算；不把字符串长度称为已验证的 token 精确计量。

验收：关闭压缩、动态工具目录、巨大工具描述、压缩后仍超限、恢复请求及无可压缩组均在实际 provider 发送前受检；不过度删减 call/result 配对；记录实测误差和预算选择。

## R5：隔离诊断与普通安全测试的能力链尚未完成

性质：已声明的未完成工作，不把它伪装成新发现漏洞。

`sandbox/runtime.py:1337` 仍以只读方式挂载整个仓库。新输出策略统一隐藏测试/MCP 正文，但尚无允许文件工作区、合成输入隔离证明和详细安全诊断适配器。因此实现了部分限制，但没有证明普通安全任务仍能依靠详细诊断完成修复。

按 WP-C 落实：最小输入清单、受控依赖准备、只读候选、私有临时区、禁网络与来源核验；安全测试详细诊断，敏感测试按契约披露，不能披露则人工交接。缓存及产物遵守已定来源与清理规则。

验收：无敏感测试失败能得到实际断言与堆栈并完成修复；带敏感输入运行不能借日志/文件/缓存回流泄漏；宿主或 Docker 不可用不静默放宽；至少一条生产入口的真实任务和故障恢复链有证据。真实 provider 实验需要可用预算，未跑明确写未覆盖。

## 实际测试与局限

本次使用 Python 3.13，PYTHONPATH=src，运行以下四个模块：

- tests.test_wp_d_projection
- tests.test_output_policy_gate
- tests.test_recall_history_tool
- tests.test_request_metering_gate

合计 9 项，8 项通过、1 项失败。失败位置 `tests/test_wp_d_projection.py:94`：预期 completed，实际 failed。测试使用 build_coding_tool_registry 却调用 run_test_profile，夹具没有正确构建验证工具路径；因此这项失败本身不能作为生产工具执行失败的证明。

该测试在后半段手工模拟投影发布，没有直接执行 App._publish_result_projections。修正夹具之外，还要增加真实 App 入口测试，避免存储 helper 成功被当成生产闭环。

运行测试前，将该模块的 FIXED_DIR 在内存中改为 tempfile 下的新目录，避免测试内 shutil.rmtree 删除仓库固定暂存目录。未编辑测试源文件。测试产生的已跟踪 pyc 修改均已恢复，审查后工作区干净。复验建议设置 PYTHONDONTWRITEBYTECODE=1，并继续使用隔离临时目录。

R1 的五次发布探针独立于上述失败夹具，直接使用真实 SqliteEventStore 和 ResultProjectionStore，已复现第四次版本冲突。未跑全量回归、Docker 实测或真实模型任务；没有验证全部旁路和所有恢复组合。

## 建议修复顺序与完成条件

先修 R1 并恢复可信测试夹具；再将 R2 的生产发布/读取/恢复链接通，同时完成 R3 数据释放规则和 R4 最终请求门；随后完成 R5 安全详细诊断并做真实任务验证。安全契约未就绪时可以测试合成组件，不能先开放旧正文再补权限。

完成标准：

1. 每项交付“设计条款 → 生产执行点 → 测试/实验”的映射，列出仍未覆盖部分。
2. 多结果、长流、并发 CAS、崩溃、索引缺失、权限撤销和 uncertain 都有明确状态，不吞错、不盲重跑。
3. 测试观察最终模型请求和实际发送边界，不只检查 helper 返回的标签。
4. 在 v2 目录按 AGENTS.md 执行全量 unittest；Docker/provider 跳过项不能记为实测通过。
5. 至少证明一条普通安全修复任务可交付，以及敏感受限任务能够正确转人工；未具备实验条件时不得宣称端到端闭环。
6. 更新进度表，准确区分已接线、已单测、已生产入口集成验证、已真实任务验证。保持未完成项目可见，不用“best-effort”掩盖必须的安全发布与恢复契约。

## 修复闭合记录（2026-09-25，实施会话追加）

本节由实施会话追加；上文审查内容保持原样。基线 `73270c2`，修复后工作区未提交（等维护者指示）。

### R1：核心修复完成（实测确认）；正式恢复入口及重启后状态展示待补（复验修正）

- 流头根因：`retrieval/projection.py` `_head_version` 改为翻页走到短页取真实流头（与 `recovery/execution.py` 既有 `_stream_head_version` 同型）。审查探针场景（同 turn 5 个不同结果）现全部发布，流版本连续 0–4。
- 冲突语义：新增 `_append_guarded`——`WrongExpectedVersion` 重读流头重试（上限 8 次，耗尽上抛）；同身份重试由存储幂等回执去重（不写重复事件）；同身份不同内容 `IdempotencyConflict` 直接拒绝（不重试、无部分写）。
- 失败登记：新增 `result.projection-unavailable.v1`（设计 WP-D 事件族命名），error_code 仅取异常类名；同失败重试幂等，不同错误码分别登记。
- 生产可观察：`runtime/app.py` `_publish_result_projections` 重写为逐条隔离——单条失败后其余事实继续发布，状态并入 run/resume 结果 payload 的 `result_projections{published, failed, pending, scan_error}`；投影失败不失败 turn、不改写工具结果、不触发重执行。
- 验收映射与验证等级见 `hardening-memory-retrieval-2026-09-19/implementation/wpd-r1-progress.md`。

### 复验修正（同日，维护者复验后）：正式恢复链未闭合

核心修复经维护者复验有效（Python 3.13 相关 14 项测试通过）。但以下缺口成立（实施会话按源码核对确认）：

- **resume 不补发**：`runtime/app.py` 的 `resume` 对终态 turn 提前返回 `turn_already_terminal`，不做投影补发。
- **status 无投影状态**：`status` 的 turn 文档经 `_turn_document_from_state` 构造，不含 `result_projections`；该字段只在 `run/chat/resume` 完成路径的 `_truth_outcome` 合并。
- **重启后不可见**：`_projection_publications` 是进程内字典，重启归零；持久的 `result.projection-unavailable.v1` 事件当前没有读取方，待处理投影在重启后无处展示。
- **测试证据边界**：现有恢复验证调用的是私有 `app._publish_result_projections`（手动补发），证明补发幂等可行，不证明生产 resume/status 已接通。

结论：R1 状态调整为“核心修复完成，正式恢复入口及重启后状态展示待补”。恢复入口补发与状态展示并入 R2 收口范围（发布时序/读取来源/恢复链本就属 R2）；store 级重启补发幂等（新 store 实例重发全量不重复）仍然成立。

### 残余已补（同日实施，复验缺口闭合）

上述复验缺口当日已实施并经真实入口测试验证（`tests/test_wp_d_projection.py`）：

- `retrieval/projection.py` 新增 `read_publication_status`：翻页读取投影流，
  按 (call_id, body_ref.event_id) 归并 published/unavailable，并与
  run-execution 扫描交叉核对——never-attempted 事实以 error_code=None 的
  pending 暴露；扫描失败记 `scan_error`，不冒充全发布。此为
  `result.projection-unavailable.v1` 的第一个持久读取方。
- `runtime/app.py` `resume` 终态分支改为恢复入口：幂等重跑发布，fresh 状态
  并入 payload `result_projections`（不再只是纯 `turn_already_terminal`）。
- `runtime/app.py` `status`：turn 文档逐个附加持久投影状态（不依赖进程内
  字典，重启后可见）。
- 测试 `RestartRecoveryEntryTest`：注入失败→close 模拟重启→新 App 的
  `status()` 持久暴露 pending→`resume()` 补发完成→`status()` 清零→流上
  无重复→二次 `resume()` 幂等零新增。全程真实入口，无私有方法调用。

R1 现状：核心修复 + 恢复入口与重启后状态展示均已完成并验证；真实模型任务
端到端验证仍未做（需预算）。R2（首次回执时序、检索读取接投影流、
scan/read 翻页、来源身份绑定）、R3、R4、R5 继续开放。

## R2–R5 修复记录（2026-09-25，实施会话追加）

详情与"条款→执行点→测试"映射见
`hardening-memory-retrieval-2026-09-19/implementation/r2r4-progress.md`。摘要：

### R2：已实施（时序/翻页/来源绑定）

- 长流翻页：scan/read 翻页走完全流（500/页），无 1000 硬上限；501 事件
  实测越过首页。
- 来源绑定：测试事实须有同 (turn, model_turn, call) 的 ledger
  `run_test_profile` 执行记录，仅凭回执策略字段标记不再发布；未绑定计入
  `untrusted` 并在状态暴露。
- 发布时序：loop 在 durable 事实提交后、回执进入任何后续轮次前调用发布
  钩子（resume/主循环两处 + 生产 assembly 接线 + chat 克隆继承）；身份
  统一 (turn, call, content_sha256)，钩子与终态补发幂等互去重；钩子故障
  隔离（登记 unavailable，不失败 turn，不重执行）。探针测试证明第二轮
  请求产生前投影已在流上。
- 残余：首轮回执本身仍来自工具执行路径（完全缓冲需 loop 协议改造，独立
  切片）；recall 读取接投影流未做（WP-E 范围）。

### R3：已实施（recall 面）

- recall 命中不再返回 user_input/final_text 文本预览（原 120 字），只给
  长度元数据；顶层 `content_release: unavailable_without_release_rule`
  独立状态。最终 provider 请求观察测试：种子历史文本不出现在任何请求。
- 残余：per-profile 字段释放规则（argv/诊断/MCP 长度）与
  withheld/human_required 在验证链的完整落地——释放规则是维护者定义的
  部署输入，不在实现侧推导。

### R4：已实施（独立最终门）

- `_assert_request_fits` 在最终消息 + 同一 pinned 工具快照上、
  ModelRequest 构造前运行；压缩开关/sink/可压缩组不影响。计量含工具
  name+description+schema（原只 schema）、每项 48 字符协议开销保守估算、
  输出预留。memory=None 回退 schema 上限 fail-safe。压缩循环内三处硬检查
  同步改全量估算。
- 覆盖：关压缩 + 超限在 provider 调用前拒绝（client.requests 为空断言）；
  巨大工具描述单独触发；未配置 loop 有 fail-safe 上限。协议开销是保守
  估算，非实测 token 计量（WP-H）。

### R5：未实施（如实评估）

WP-C 是独立工作包（清单工作区/依赖准备/固定候选/敏感容器/诊断适配器/
合成秘密实验矩阵），验收依赖真实实验与维护者释放契约，不做实验不能
声称部分完成。整仓库只读挂载与详细诊断能力链保持审查原判。实施入口：
bounded-capture.md WP-C 节。

### 回归

目标模块 47 项 OK；全量 unittest discover 结果见本报告闭合记录最终数字。
另顺带移除 6367a16 遗留的 `DBG-445` stderr 调试打印（ledger/executor.py）。

### 计量修正的测试重定基线（如实记录）

R4 全量计量使两个按旧欠计量调校的测试重定基线：`test_f20_second_compaction`
（首回合预算 5000/6200，接受码增补同族的 `d2:request_capacity_exceeded`，
follow-up 独立放宽；压缩次数断言保持）与 `test_d23_long_task_golden`
（hard 20000→28000，≥3 次压缩与重启等价断言不变）。详见
`implementation/r2r4-progress.md`。`test_d25_g3_g4_governance` 的
close-hang 用例为已知负载相关 flaky（单跑通过），与本次改动无关。

## 复验第二轮（2026-09-25，维护者独立运行后裁决与必修）

维护者独立运行相关 22 项测试通过、核对全量日志但未重跑全量。两项必修
当场修复，四点裁决入档：

### 必修①（已修，实测确认）：发布身份漏 model_turn_id 导致跨轮错误去重

首版身份 `(turn_id, call_id, content_sha256)` 漏掉 model_turn_id——仓库
协议中 call_id 仅在所属模型轮次内唯一。复验实测：两轮复用 call_id=c1 且回
执相同，工具实际执行 2 次，投影只发布 1 条。已修：身份改为
**(turn_id, model_turn_id, call_id, content_sha256)**，body_ref 携带
model_turn_id，publish、失败登记、scan、状态归并全链同一规则。回归测试
`CallIdentityAcrossModelTurnsTest`（复验场景：双轮同 call_id 同内容 →
恰 2 条投影、model_turn_id 各异、digest 相同——内容校验，身份区分）。

### 必修②（已修，实测确认）：R3 provider 验收原为空转

原测试调用 `loop.run()` 未绑定 turn → recall 解析线程失败返回
`recall_unavailable`、实际检索 0 次——证明的是"检索失败无泄漏"而非
"命中敏感历史后安全结果进请求"。已重写：正确绑定 turn；中性查询（秘密
标记不作查询词）；先断言检索真实执行且命中种子 turn（stub 收到查询、
回执非 error、hits 含目标 turn_id），再检查全部请求无泄漏。

### 裁决入档

- **钩子故障隔离**：成立，但发布失败后原回执仍进入上下文——"先成功
  发布再交付"的完整契约不存在；当轮回执顺序保持为开放的 R2 切片。
- **预算重定基线**：接受；F20 ≥2 次压缩、golden ≥3 次压缩与恢复等价
  断言保留。
- **R2 总状态 = 部分完成**：在首次回执来源改造与按引用读取链（recall
  接投影流）完成前不称闭环。
- **untrusted 只计数**：足以表达"不可发布"，不足以决定可交付性；另已
  区分 ledger 读取异常为 `unverified`（fail-closed 不发布，与来源不匹配
  分离，`UnverifiedSourceClassificationTest` 覆盖）。
- R4 门为字符级估算，不称实测容量保证；R3 字段释放规则与 R5 继续开放。

### 审查之外新增发现并修复：生产路径扫描恒为 0 事实

`scan_test_results` 原只接受 `str` 或精确 `dict` 的 `context_item`，而 StoredEvent payload 冻结为 `MappingProxyType`（非 `dict` 实例）——生产路径所有 `tool.result-recorded.v1` 事实全部被静默跳过；即使修复 R1，生产也发布不出任何投影。旧测试靠后半段手工发布掩盖。现接受任意 `Mapping`（tests/test_wp_d_projection.py 重写后经真实 `AppRuntime.run` 入口实测确认）。

### 夹具与测试

`tests/test_wp_d_projection.py` 重写：验证注册表 + 脚本化 runner 真实执行 `run_test_profile`（原夹具调用不存在的工具，turn 必 failed）；全部隔离临时目录，删除仓库固定 FIXED_DIR；新增真实 `AppRuntime.run` 全链路测试（git 证据 + finalize），覆盖生产发布与单点故障注入下的失败可观察。故障清除后的补发验证调用私有 `_publish_result_projections`（手动补发，证明幂等与无重复），生产 resume/status 恢复入口未接（见上文复验修正）。R1 验收测试共 6 项 + 入口 2 项全过。

### 回归

全量 `unittest discover`：1090 项，0 失败，0 错误，32 项跳过（Docker/provider 已知）。运行环境 Python 3.13、`PYTHONPATH=src`、`PYTHONDONTWRITEBYTECODE=1`。stderr 中个别 UnicodeDecodeError 为测试子进程读线程在 Windows GBK 输出下的噪音，不影响判定。

### 仍然开放（未动）

R2（发布时序、读取来源接投影流、1000 条翻页与不完整标记、来源身份绑定）、R3（字段释放规则与 withheld/human_required 语义）、R4（最终请求门去压缩依赖）、R5（WP-C 隔离诊断能力链）全部保持原状，按本报告“建议修复顺序”继续。

## 目标轮收口记录（2026-10-03，/goal 完整闭环 R1–R5）

审查→实施→验收→测试全链完成，映射与证据见
`hardening-memory-retrieval-2026-09-19/implementation/r2r4-progress.md`
第三轮切片节。摘要：

- **R2 切片(a) 修正版 B 实施**（规格 v4 全约束）：`result.delivery-
  decided.v1`（SPEC-1 键不含内容维度；冲突=拒绝；恢复重放原决策）、
  SPEC-2 三摘要与四字段不可变投影引用（`source_content_sha256`/
  `stream_version` 更名，遇既有数据版本拒绝）、SPEC-3 `delivery_pending`
  状态机（生成点禁原回执、先验后写 backfill、paused/protocol/
  corruption 三态分离、W1–W3 + 查询故障/并发重放/分歧 digest 崩溃矩阵
  全过）。**顺带修复隐性缺陷**：checkpoint wire 曾默认 reducer_version=2
  （record 与身份不一致），REDUCER_VERSION→3 后由 stability 故障矩阵
  暴露并修复（wire 现盖真实版本）。
- **R3**：ReleaseRule 释放契约 + sensitive 无契约固定 withheld/
  human_required；回执与计划 B 交付继承契约。
- **R4**：实测计量探针 + 报告（DeepSeek-V4-Flash，2.911–4.628
  chars/token，安全下界 2.5；局限如实）。
- **R5/WP-C v1**：隔离清单工作区（只读候选+私有 scratch+清理隔离+越界/
  符号链接拒绝）、有界诊断适配器（仅隔离运行）、sensitive 拒 host
  （配置期）；**真实生产证据**：生产 CLI + 真实 DeepSeek 修复任务完成
  （真实补丁、测试绿、finalize 证据），计划 B 交付链现场实证（1 决策
  事件 + 1 发布投影 + 重建上下文=投影派生回执 + 状态全绿）。
- 验证：全量回归两轮（1120/0/0/32 覆盖 planB+R3；最终轮覆盖全部改动，
  数字见提交信息）；新增测试 delivery_gate(9) + release_rules(6) +
  wp_c_workspace/manifest_guard(11)。
- 开放项不变：读取链当前权限/检索限额、Docker manifest、MCP 适配器
  释放、敏感容器真实实验矩阵、per-profile 部署值、WP-H。
