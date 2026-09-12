# KoawaAgent V2 平台审计：部分完成

日期：2026-09-07。状态：**全项目审计部分完成，不能宣称整体通过**。

当前证据不足以认定本项目已具备“产品级任务交付能力的 Agent 安全研究平台”这一完整能力。更明确地说，本轮已证实完成门能接受与通过测试不一致的产物，因此不能把当前完成报告直接当成可靠交付证明；安全增量在 JSON 配置入口还有实际断点。与此同时，已执行的组件专项说明它并非只有概念框架。项目可继续用作有明确范围的机制研究平台，但任务级防御收益的实验须先修复证据底座并补充真实执行链验收。

这不是企业部署/SLA/运维成熟度判断，也不等价于“所有任务都无法完成”。没有同条件 Codex 对照，本轮不作领先、落后或等同判断。

## 1. 基线、环境与证据纪律

- HEAD：`f3d2664ca3b848e0d73bf1170ba5a132be6c1e83`；缓存 `origin/main=e213580`，未 fetch，不声称掌握实时远程状态。
- `origin/main..HEAD` 实际五个提交：ba6b85d、a9d8896、54c6680、6367a16、f3d2664。首次进度消息误报六个，以此处及原始 Git 输出为准。
- 启动时 README、runtime/assembly.py、runtime/config.py、verification/finalization.py、verification/tools.py 有未提交改动。后续发现两个既有测试修改及新 test_d5_required_profiles.py，证明并行编辑持续发生。归属未确认，本轮没有覆盖、回滚、暂存或提交这些文件。
- 未跟踪 `.dsh_tmp`、`.mimosa`、scratch、docs 交接材料等未被当成本轮验证证据。报告与复现仅新增在本目录。相关文件及证据摘要见 `file-hashes.json`；这是收尾时指纹，不能证明每个测试执行期间文件完全静止。
- Windows，执行解释器 `C:/Users/qaz14/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`，Python 3.12.14；`PYTHONPATH=src`，`PYTHONDONTWRITEBYTECODE=1`。
- 已读取 V2 AGENTS、上级规则、README、路线图、D4/D5/D6/D7/D12 设计、RT/J/PSEC 账本与威胁模型及稳定化/I9 部分说明。路线图中的历史“完成”和测试数字不计本轮证明。较长治理/路线文档部分工具输出被截断，未声称逐行完整核阅。
- 全项目范围未缩小：十个领域全部列在覆盖矩阵。账户用量限制导致独立静态审查和架构子审查均在返回证据前中止；不能计作独立审计。主审完成下面列出的局部源码检查与专项。
- 无生产修改、无原断言修改、无提交或推送、无付费模型调用、无真实容器启动。没有完成编号实现切片，本轮未运行全量回归；154 项专项不替代全量，也不替代全链验收。
- 安全扫描 `42478287-a099-48ae-a4f2-f35749ee3eaf` 保留 `complete:false` 部分草稿，未封印、未宣称扫描完成。本报告是独立的交付能力/研究有效性审计报告，不冒充工具生成的完整安全扫描报告。TAC 状态 not_granted、grants=[]，不构成审计结论。

## 2. 执行前基线与本轮结果

执行前定义保存在 `baseline.md`。所有复现副作用发生于自动清理的临时仓库/状态库。

| 验证 | 实际执行与结果 | 证据等级及可支持结论 |
|---|---|---|
| B1 内容漂移反例 | 真实 Git、真实 apply_patch、真实 Python assert、status/diff/finalize；外部把已验证文件改为错误内容后完成门仍接受，独立 assert exit=1 | **实测确认**：产物/验证证据绑定失效；不依赖模型猜测 |
| B2 D23 长轮数 golden | 100 次脚本工具回合、≥3压缩、重建等价与无pending断言通过 | **实测确认（模拟）**：压缩/重建机制；没有真实文件工具、独立功能产物或跨进程重启，不能证明长任务能力 |
| B3 D5/配置/J2/T7 专项 | `focused-tests.txt`：30 tests / OK，68.712s，未报告skip | **实测确认**：包括真实失败修复闭环、required profiles、旧结果复用不制造新验证证据；J2配置测试只覆盖 helper/替身，见F2 |
| B3 控制/恢复/policy/memory | `core-tests.txt`：124 tests / OK，32.585s，未报告skip | **实测确认**：EventStore、ThreadRuntime、D6 recovery、D7 ledger、D9 policy/approval、D23 conclusion、D24 authority 所选专项；非本轮逐源码独立审查 |
| B4 eval链 | 20/20 success，阻断式 spy 检测到 AgentLoop.run 调用0次 | **实测确认**：fixture答案写入可通过验收；不能计为Agent完成率 |
| J2配置文件入口 | `load_runtime_config` 输入含canary_key_env时返回config_unknown_field | **实测确认**：字段入口被拒；最小配置在必填字段验证之前即失败，不是完整运行启动测试 |
| B5真实综合长任务 | 未执行真实模型+Docker+写委派+压缩+kill/resume的联合链 | **待验证**：不存在本轮该能力的成功率、介入率或成本数据 |

命令：上述解释器执行 `-B -m unittest tests.test_d5_vertical_slice tests.test_d5_required_profiles tests.test_d23_long_task_golden tests.test_j2_semantics_c tests.test_t7_delegation_boundaries -v`；另执行 `-B -m unittest tests.test_event_store tests.test_thread_runtime tests.test_d6_recovery tests.test_d7_tool_ledger tests.test_d9_policy tests.test_d9_approval tests.test_d23_turn_conclusion tests.test_d24_authority_contract -v`。原始日志保留全部测试名。复现脚本以相同PYTHONPATH运行。

人工介入：测试中的脚本模型、预设修复、mock与审计故障注入均由测试作者/审计器安排，不计自主推理。没有人工把失败代码修好再记成功。历史真实provider结果仅从文档读到，不计本轮实测。

## 3. 发现

### F1：完成门未把测试/最终报告绑定当前文件内容（交付底座 P1）

**实测确认；实现违反当前产物证据契约。**

- 入口：`verification/git.py:174–191` 的status digest只含状态码和路径；`verification/finalization.py:255–269`完成重检只比较该摘要及原dirty baseline指纹。
- 触发：成功patch并测试、status、diff、finalize后，另一编辑者或外部工具修改同一已变更路径，状态仍为` M`。
- 预期：旧证据失效，拒绝完成并重新验证。实际：`completion_accepted_after_drift=true`、`status_digest_equal=true`，独立验收exit=1。
- 证据：`repro_content_drift.py`与`content-drift.json`。使用真实文件/工具/测试执行器；直接调用registry完成接口，未声称完整CLI端到端已复现。assembly.py:563将registry作为AgentLoop completion_gate，说明生产消费者使用该门。
- 影响：可以交付未经所报测试验证的内容；并行修改和多阶段任务中尤其影响信任。不是声称可绕过操作系统或拥有任意外部进程权限的安全漏洞。
- 最小建议：证据绑定实际工作树内容及必要HEAD/index身份；测试开始/结束与最终放行分别验证绑定，漂移使证据失效。已有路径集合和generation不足以替代内容绑定。验收至少包含同路径同状态不同字节、测试运行中漂移、final后漂移和恢复后漂移。
- 现有相关测试通过但未阻断本反例；不能以全绿消除此发现。

### F2：J2 JSON配置入口拒绝激活字段（安全增量接线 P1，非已确认越权）

**实测确认；实现与生产激活声明冲突。**

- `runtime/config.py:977–1001`允许字段列表缺`canary_key_env`，未知字段立即拒绝；同文件:1067却读取它。`assembly.py:1131–1139`确实存在SecurityGate构造，所以问题不是没有任何机制实现。
- 实际JSON配置无法带此字段进入后续解析；无此字段时默认None，门不开启。Python直接构造RuntimeConfig是另一入口，不能据此宣布CLI文件配置已贯通。
- `tests/test_j2_semantics_c.py:85`起使用SimpleNamespace测resolve helper，绕过JSON loader，解释了专项全绿仍有断点。
- 证据：`repro_config_and_eval.py`、`config-and-eval.json`；本轮没有使用真实密钥。
- 影响：基于文档配置J2的实验启动失败；删掉字段获得运行又失去该增量，不能把这种运行算J2-on。基础policy不因此失效，不能据此声称已发生泄露。
- 最小建议：修通JSON字段入口，并用实际配置文件→装配→已允许的合成canary动作→持久ASK→重启/批准的集成测试验收，同时用off配置对照。不要仅加helper测试。

### F3：eval成功率没有经过Agent执行链（实验可信度 P1）

**实测确认；证据契约不足/报告标签不准确。**

- `evals/run_eval.py:52–53`直接将task.patch写入临时文件，之后执行测试与逐文件oracle；不调用模型、Loop、Registry、policy或ledger。:129却标记`mandatory_reliability=scripted_deterministic_provider`。
- 证据：阻断AgentLoop.run的spy下20/20仍成功，调用0次。写答案并验答案可验证fixture自洽，不能验证Agent能构造答案、修复、恢复或交付。
- 独立oracle本身有价值；问题在测量对象与标签，不是在使用oracle或离线测试。
- 最小建议：保留其为fixture验收；另设真正调用生产装配的任务执行器，预期答案仅给独立验收器，运行器只收到任务和起始repo。原始成功数不能用于Agent能力/安全效用比较。

### F4：J2校准测的是字符串匹配，不是任务级安全收益（研究方法 P2）

**代码推断；契约不足以支持任务级研究结论。**

- `redteam/adapter/j2_calibration.py:39–107`正例直接嵌入精确token，负例预设无token，evaluate仅调用scan_exact_token。没有任务产物、策略/审批执行、模型、人工介入或机制开关对照。
- 500次字符串实例不能证明500个独立任务中的检测/误拦截表现，Wilson区间也不能弥补样本分布和测量对象偏差。UUID采用uuid4，源码“deterministically”说明还需限定到case形状而非字节复现。
- 未在本轮运行该校准；不推翻已记录的精确匹配测试结果。不能把“0/500”解释为正常长任务的零效用损失。
- 建议：把该指标标作detector单元校准；另外执行相同任务/起始repo/预算下off/on实验，区分完成且守界、安全阻断未完成、完成越权、未交付/UNKNOWN，记录介入、耗时、调用与token预算变化。

### F5：真实委派与祖先接线未获生产证据（交付/研究缺口 P1待验证）

**代码推断与待验证，不能当已确认安全漏洞。**

- 全src检索ancestor_turn_ids显示loop默认空tuple及两个context传递点，但assembly的两个AgentLoop构造点未传入委派祖先；与PSEC B-3账本一致。
- `runtime/unified.py:113–118`使用ScriptedAgentProvider；同文件将固定projection/compaction用于示例式流程。此路径不能证明真实模型写子Agent和集成已装配。实际Python入口、post_build扩展等是否另有可用组合未全审完。
- `execution/loop.py:233,255–257,533,697`与`runtime/assembly.py:168,563`是续审锚点；`agents/control.py:291`、`agents/scheduler.py:355`、`workspace/integration.py`需联查身份、执行权、产物与结果消费者。
- 必须用真实生产子执行入口验收A→B→C、mailbox中继、orphan takeover与sticky交叉，不要用手动传入ancestor tuple的单元测试声称委派图已贯通。

### O1：required profiles改动有正向实测，但仍属并行未提交版本

**实测确认（局部）**：必测失败不能被其他profile成功替代、遗漏/stale拒绝、D7复用结果不自动产生新D5证据，所选专项通过。因此本轮不再把“只取最后一条测试”当当前工作区已确认问题，也没有证实D7给旧测试自动重贴新generation。

**代码推断**：RuntimeConfig允许最多64个required profiles，而生产装配未传VerificationLimits，D5默认每run最多4次测试（finalization.py:34,109–115）。超过4个必测配置可能根本无法完成；多轮修复也易耗尽。应校验配置可满足性并明确恢复预算语义。此项属于正在修改的工作区契约，不在本轮宣称已完整回归或正式版本缺陷。

### O2：Patch崩溃恢复仍未闭合验证

**代码推断/待验证**：assembly.py:1125–1126仍将apply_patch注册为IDEMPOTENT_WRITE_PROFILE。D4设计明确stage/backup/rollback仅保证进程内异常，不保证kill后的多文件恢复。本轮未做真实D4提交中kill→D7重试的联合复现；不能直接断言自动恢复、重复成功、数据丢失或自动UNKNOWN。续审必须覆盖移走旧文件后、第一文件安装后、全部成功而result未落库这三个窗口。

## 4. 完成条件表

| 期望保证 | 权威来源 | 证据绑定 | 失效条件 | 生产强制入口 | 失败处理/本轮判定 |
|---|---|---|---|---|---|
| 所有必测通过 | trusted profile配置+真实CommandResult | 当前run/generation/profile | 遗漏、失败、patch后旧测试 | VerificationLedger.finalize | 专项拒绝成立；配置/恢复全链未全验 |
| 测试对应实际交付内容 | workspace内容+测试执行结果 | 当前只有generation/status-path摘要 | 同路径同状态内容变化 | assert_complete | F1实测未拒绝 |
| 用户已有修改保留 | 开始时dirty baseline内容指纹 | baseline fingerprints/protected paths | 并行变化/暂存变化/换行语义 | GitFacade+patch guard | 本轮未完整审查；不宣称覆盖所有已有改动 |
| UNKNOWN不伪装成功 | durable tool ledger | execution_id/claim token | handler结果丢失/不可查询 | LedgerExecutor/recovery | D7专项过，Patch联合窗口未验 |
| 恢复能继续并重建验证 | execution facts/checkpoint+产物 | run版本与新尝试 | 新run、外部漂移、旧结果复用 | TurnWorker+registry | D6专项过；D5进程内证据恢复交集待验 |
| 真实授权且不可增权 | policy+durable approval | action digest/principal/scope | 配置漂移、重放、接管 | authorize/claim | D9专项过；全边界未源码审完 |
| J2开启时生效 | 配置→装配→security state | key引用/turn/execution | JSON入口拒绝、祖先未传 | _bind_ledger_policy/_j2_check | F2断点；B-3未闭合 |
| 最终任务需求完成 | 用户需求+独立产物验收 | 具体repo版本和验收结果 | 测试不足、假答案、部分任务 | 不能只依赖自然语言final | 需要任务级oracle；当前eval不驱动Agent |

## 5. 全项目覆盖矩阵（本轮收尾逐项）

各行“未覆盖”仍在审计范围内。目录简称均相对src/koawa_agent_v2；源码搜索定位不算完整源码审查。

| 领域 | 入口/关键代码 | 声明保证 | 实际生产接线检查 | 已有测试 | 本轮验证 | 跨模块依赖 | 发现及未覆盖 |
|---|---|---|---|---|---|---|---|
| 控制面/事件/状态/并发 | control/runtime,event_store,sqlite_store,schema | typed/CAS/fenced state | 只映射架构，未逐路径审计 | test_event_store,test_thread_runtime,test_s3_* | 前两专项执行通过 | recovery/approval/agents多流 | schema迁移、并发组合、I1–I9各变更未逐项追踪 |
| 模型/Loop/工具 | model/*,execution/loop,tools/registry | terminal完整/schema同源/预算 | 查到assembly→loop→executor→gate | test_agent_loop,test_model_stream,test_d2_* | 本轮未直接全跑这些专项 | provider/ledger/取消/压缩 | 真实provider、流异常组合、registry全实现未审完 |
| 文件/测试/Git/完成 | editing/*,verification/* | 原子patch/必测/产物一致 | 阅读Git与finalization及注册关键路径 | test_patch_*,test_d5_*,test_d22_* | B1、D5两专项通过且反例成立 | 外部编辑/恢复/报告 | F1；O1；暂存/用户改动/多文件kill未验 |
| checkpoint/压缩/记忆 | recovery/*,runtime/memory,session,turn_conclusion | replay权威/摘要不增权 | 文档+loop/golden装配核对，非完整路径 | test_d6_*,test_d23_*,test_d24_authority_contract | recovery、100轮、conclusion、authority通过 | D5证据/用户约束/中断 | B2只脚本机制；真实任务压缩/重启联合未验 |
| ledger/幂等/执行权 | ledger/*,runtime/claim_gate | claim先行/UNKNOWN显式 | 检查terminal复用及profile装配 | test_d7_*,test_i7_* | D7 ledger及D5复用专项过 | patch/approval/recovery | O2；未执行真实kill专项/副作用联合链 |
| policy/approval/身份/凭据/预算 | policy,approval_service,runtime/config,security/* | default deny/action绑定/sticky | 检查J2 loader/helper/assembly/gate | test_d9_*,test_j2_* | D9 policy/approval与J2语义专项过；F2复现 | config/ledger/child/restart | F2；root/child权限全追踪、凭据全持久层扫描、sticky接管未验 |
| Docker/MCP/网络/第三方 | sandbox/*,mcp/* | exact image/最小权限/代际/dual ledger | 只看legacy/activation装配片段 | test_d8_*,test_d10_*,test_d25_* | 无本轮真实Docker/MCP实测 | 外部进程/网络/凭据/回收 | 独立审查中断；镜像/daemon条件也未检查，不能说不可用 |
| 多Agent/消息/worktree/集成 | agents/*,workspace/*,runtime/unified | scoped/fenced/隔离/重测 | 发现scripted路径与缺ancestor传参 | test_d11_*,test_d12_*,test_golden_composite_e2e | 未执行这些专项 | 子loop/approval/产物/D5 | F5；真实写委派、冲突集成、并行改动保护未完成 |
| 交互/装配/取消/报告 | runtime/app,assembly,session,truth,turn_summary | 正确终态/持续目标/收束 | 查completion gate消费者；其他未完整 | test_d16_*,test_i7_*,test_d22_* | conclusion专项间接涉及 | control/memory/D5/取消 | 报告所有消费者、新多profile格式、final恢复未逐项验 |
| 安全实验/评估/故障/证据 | evals/run_eval,redteam/adapter,telemetry/* | 独立oracle/正控/效用可测 | 全读eval与J2校准；RT bridge未全追 | test_d14_*,test_j1_*,test_stability_* | B4；未重跑RT/J攻击campaign | 真实模型/生产工具/外部oracle | F3/F4；I8故障矩阵、攻击变体、off/on与组合攻击未执行 |

## 6. 分开排列的改进优先级

**底座交付/成功指标**：优先修F1产物证据绑定和F3评估测量对象；随后以生产链验证D5证据跨run恢复、Patch uncertain窗口和真实委派交付，再处理长任务测试预算可满足性。验收是独立产物检查、故障后继续且不重复危险副作用，不是单纯提高轮数。

**现有安全边界**：本轮没有完成足以宣称全项目安全通过的审查，也没有将未验证候选定为已确认越权。F2是已确认的J2激活契约断点，应在任何J2-on实验之前修复。之后验证基础policy、ASK恢复、参数漂移与取消的实际组合。B-3/sticky与容器/MCP须续审，不可用缺省关闭能力来制造安全收益。

**前沿研究**：在可靠任务基线上做ancestor信号传播、scoped taint、工具行为漂移与细粒度权限研究。优先验证多跳中继/压缩/恢复对信号的影响。PSEC文档中的PDP/PEP外置、OAuth/凭据代管等不能仅凭设计文档列为已生效机制；是否实施应由明确攻击/能力需求决定，非一般生产化阻断项。

## 7. 续审入口

1. 先重新执行AGENTS对齐，确认并行D5改动归属和最终版本；冻结一个可重复测试的快照，重跑本目录反例与已过专项。
2. 恢复独立静态/架构审查；当前子审查没有可继承的有效结论。安全工具继续同一scan，不用零finding草稿推导无漏洞。
3. 优先完成 `runtime/app.py→assembly.py→worker.py→ledger/executor.py→verification` 的run/resume/interactive三条链，追踪required profiles、generation、最终报告与消费者。
4. 在临时repo用真实子进程kill注入D4三窗口并经实际resume恢复；独立核验每个文件、backup残留、ledger状态、验证证据和最终状态。
5. 以D12/golden实际入口证明真实子Loop构造、scope/ancestor来源、结果集成和重测；若入口仅fixture，记录缺失，不把孤立机制拼接描述为已交付能力。
6. 检查Docker实际条件再决定是否运行已有本地镜像测试；没有镜像时明确缺失条件，不擅自下载。真实模型付费验证需要另行授权；离线脚本只能证明harness协议。
7. 任务级对照基线：多文件依赖修改→首验失败→修复→至少一次真实上下文压力压缩→进程中断→恢复→独立验收；另加需要委派的任务。固定任务/repo/模型/预算，记录四分类、介入与成本，并开展机制off/on及组合攻击。可先在离线脚本上验证调度与证据协议，再独立报告真实模型结果。

没有完成上述关键范围前，只能继续称“全项目审计部分完成”。
