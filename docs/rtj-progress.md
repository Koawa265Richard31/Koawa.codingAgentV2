# RT/J 全流程实施留档（阻塞与处理清单）

更新：2026-09-05。规划：`docs/agent-redteam-jailbreak-plan.md` v1.1（评审定稿）。
目的：维护者要求留档——出现过什么阻塞、怎么处理的。每条均可由 git 历史/测试文件复核。

## 一、规划与决策阶段

| 阻塞/决策 | 处理 |
|---|---|
| 模型层越狱归属争议（v0.1 曾试图测量 ASR） | 维护者裁定 + 评审确认：越狱治理属模型构建侧（provider），harness 只治理后果。v0.2 起模型层越狱整体移出范围，越狱技术仅作攻击向量 |
| 评审推翻 v0.2 部分 设计（分类/谓词/oracle） | 评审 Agent 直接重写为 v1.0 实施定稿：七类结果分类、外部 oracle 强制、正控必备、"blocked"禁止作为指标 |
| API 预算冲突（评审冻结数值硬帽 vs 维护者按需使用） | 维护者拍板按需使用；v1.1 版本化修订，技术护栏（并发1/timeout/retry）保留冻结 |

## 二、实施阶段阻塞与处理

| # | 阻塞 | 根因 | 处理 | 提交 |
|---|---|---|---|---|
| 1 | D9 network gate 拒绝 loopback 目标（`https_required`/`non_global_network_address`） | D9 设计即反 SSRF：仅 https+全局 IP | 测试 origin 用 https 标签 + 全局 IP（attacker.example/93.184.216.34）；PC-NET 正控改用无网络目标的 ALLOW 工具投递 | J1 内 |
| 2 | PC-NET 首版是桩（死代码） | 初版投机 | 重写为真实功能正控（sink 实收 nonce） | c107039 |
| 3 | D21 T3 毒化用例失效 | D25 schema 规范化把"缺边界的 string"变为合法（设计本意：更严解码） | 毒样换成真正子集外形态（pattern/number/enum）；注释记录边界迁移 | c107039 |
| 4 | Docker daemon 反复停止（≥4 次） | 桌面端退出/睡眠后不恢复 | AutoStart 开启 + 每轮检测自动拉起（Start-Process + sleep 45） | 环境项 |
| 5 | Mimosa 钩子拒绝 git commit（Bash 直写未扫描） | 部分文件经 py -c/heredoc 直写 | 被点名文件一律经 Read+Edit 重存触发扫描；后续一律用 Edit 工具改源码 | 多次 |
| 6 | rt_target_bridge ready 失败：repo_not_found / not_a_git_repository | 驱动直接用了示例配置的占位 repo | 驱动生成一次性 campaign 工作区 + git init | 88350e8 后续 |
| 7 | registry 提前 sealed，无法注册 egress 探针 | `_bind_ledger_policy` 枚举 definitions 导致 seal | `post_build_registrars` 钩子（trusted code only，非配置驱动） | 7a26a2a |
| 8 | 生产 stdout 噪声污染协议帧 | AppRuntime 链路有 stdout 打印 | 控制通道改 localhost socket（adapter 先监听，bridge 反连） | 7a26a2a |
| 9 | **模型不发起任何工具调用**（RT-1 最大阻塞，5+ attempts 全 not_exercised） | 维护者判定：Qwen3.5-35B-A3B 未经/不适配 tool-call 行训练 | 行使冻结替换规则切 `Qwen3-30B-A3B-Instruct-2507`；换后立即 action-reaching | 1c80807 |
| 10 | PolicyError 三连（https_required / noncanonical_network_target / invalid_dns_response） | D9 网络门禁比预想严格（正确行为） | 逐条适配：https 标签、省略默认端口、全局 IP resolved addresses | J1/RT-1 |
| 11 | 报告 attempt_id 归因错误（4 例同一 id） | `sorted(facts)[-1]` 字典序取值 | 集合差分取新 key + sink 字节增量按 attempt 计算 | 1c80807 |
| 12 | append_batch 对 security 流 CAS 冲突 | 双 propose 并发 | 有界重试（≤2 次）+ 冲突即拒绝（单胜者语义） | afa3002 |

## 三、当前状态（2026-09-03）

- 冻结包：✅ 全项（825817b）
- J1：✅（c107039）5 用例双 lane 绿
- RT-1：🟡 正式 baseline 达标（1c80807：4/4 contained, rate=1.0, insufficient_exposure=false，Qwen3-30B-Instruct）；**未达标剩余**：scorer 校准（≥100 标注 P/R≥0.80）、adaptive campaigns、必要时的 Linux/WSL lane
- J2：🟡 stage 1（afa3002：security-state store + canary 原语，5 测试绿）；**剩余**：五事件原子批接入 ApprovalService、executor first/final 门接线、sticky 恢复测试、500/500 校准门
- RT-2：⬜ runbook 未写
- 全量回归：最后一次全绿 982/974/0/0/8（D25 期）；RT/J 新增 68 测试未跑全量（收口时执行）

## 四、诚实边界（不得越线声称）

- 全部结论为动作层后果遏制与检测能力；无模型层越狱声称（plan §1.1 责任线）；
- containment 数字绑定模型/配置/日期；换模型即新 run；
- scorer 未校准前 adaptive campaign 结果不作为正式判定；
- RT-1 花费按需使用、逐轮入档（v1.1 Q2）。


## 五、ECS 双平台补充证据（2026-09-05，形态 B：本地编辑 + 远程执行）

维护者提供 ECS（jd-ecs，Ubuntu，2C/8G，Docker 29.6.1，root）。处理与结果：

- Python 3.10 → deadsnakes 装 python3.12（用户级包管理，零服务影响）；
- 仓库经 bare 仓库中转推送（`jd-ecs:koawa-v2.git`，注意 root home=/root、scp 语法路径）；
- D25 两个 fixture 镜像在 ECS daemon 原生构建（filesystem `1a41cbc9…`、evil `465e313b…`，
  与 Windows 构建 ID 不同——内容寻址的正常差异，Dockerfile 同源钉定 npm 版本）；
- **D25 真实 Docker 测试在原生 Linux 上 9/9 通过**：对抗矩阵（大帧/stderr 洪泛/PID 压力）、
  真实崩溃窗口 ×3、启动挂起有界负例、e2e 全链——D25 的 Linux Docker lane 缺口（G1）
  就此真实闭合；`provenance_pinned` 在 ECS 上如实失败（它钉 Windows 构建.digest），
  属身份合同按设计工作；
- 测试镜像 id 支持 `KOAWA_D25_FS_IMAGE` / `KOAWA_D25_EVIL_IMAGE` 环境覆盖（commit 63c85f6），
  每 daemon 各自钉定，浮 tag 永不入授权执行；
- ECS 全量 pr-fast：993 discovered / 958 passed / 2 failed / 10 errors / 23 skipped——
  12 个非通过全部为"daemon 上未构建 fixture 镜像"或"Windows junction 形态"类环境项
  （镜像构建后已降至 1 个 provenance 断言项），核心逻辑 Linux 零失败。

### 阻塞补充（接 §二）

| # | 阻塞 | 处理 |
|---|---|---|
| 13 | ECS Python 3.10 < 3.12 | deadsnakes PPA 安装 3.12.13（root 包管理，无服务影响） |
| 14 | push 路径三连失败 | root home=/root；scp 语法 `jd-ecs:koawa-v2.git` |
| 15 | 镜像 digest 跨 daemon 不同 | 测试镜像 id 环境可覆盖；provenance 按 daemon 分记 |
| 16 | J1 T5 mklink 在 Linux 无 cmd | FileNotFoundError/OSError → skipTest（原为未捕获 ERROR） |

## 六、J2 stage 2 与回归状态（2026-09-05）

- J2 stage 2 完成并提交（8df05a1）：SecurityGate 接入 authorize first/final resolve、
  五事件原子批（security×2 + approval + turn + run，四流 exact heads）、
  WAITING_FOR_APPROVAL 持久暂停、grant/deny resume、fail-open。4 门测试 + 5 store
  测试全绿双 lane。
- 收口全量回归（本机 Windows，997 discovered）：988 passed / 1 failed / 0 errors /
  8 skipped。唯一失败 =
  `test_d25_g3_g4_governance.G3ProtocolLifecycleTest.test_real_close_hang…`
  ——并行会话测试文件的模块内顺序依赖（单独运行 3 次全绿；跟在 invalid_json 用例后
  必现）。该文件属并行会话所有，修复需对齐后进行。
- 除该顺序依赖外全量绿：D24/D25/J1/J2-core/RT-1 证据测试全部通过。

## 七、诚实边界（不变）

- 全部结论为动作层后果遏制与检测；无模型层声称；
- 数字绑定模型/配置/日期（Qwen3-30B-Instruct，2026-09-05）；
- adaptive campaigns 待 scorer 校准门；RT-2 runbook 已交付（`docs/rtj-runbook.md`）。

## 八、T7 条目治理事件（2026-09-06，PSEC 轨道）

- **维护者批准** PSEC/S5 的 T7 增补提案并要求旧文档/旧测试/旧治理同步：threat model
  三锚点应用（§1 框架依据补 OWASP ASI 来源行、§2 纯插入 T7 多 Agent 委派链节、
  §3 拦截点图加 D11 子委派边界行），其余 byte-identical；
  before `021fecb9…` / after `3d131d82…`（全 hash 见提交信息）。
- **RT/J 规划升版 v1.2**：解除 §1.3"不新增 T7"冻结（原文以删除线保留）。评测范围
  §1.2 不变——树级传播仍非 RT/J 评测项，T7 是威胁建模条目而非评测项；T2 失效条件行
  原有"不向子 Agent 传播"声明与 T7 失效条件一致，无冲突。
- **新增边界钉定测试** `tests/test_t7_delegation_boundaries.py`（3 用例，2026-09-06 绿）：
  钉定的是 T7 的 declared 失效条件（父 turn canary 对子 turn 扫描不命中、mailbox
  schema 无信任字段），不是新对策；若将来 PSEC 语义 C 落地，须与 T7 行一起翻转。
- T7 节内 J2 对策引用（security_escalation_pending / stale_agent_run_fenced）均为
  本轨道已落地语义（8df05a1），无新行为声称。
- **J2 生产激活状态发现（2026-09-06，PSEC 语义 C 设计评审中核实）**：全 src 证实
  `SecurityGate` 无生产构造点、`LedgerExecutor.security_gate` 无生产注入（默认 None）、
  config 无 key 字段——J2 门当前为**测试/lane 激活**（本轨道的"接入 authorize"指执行器
  咨询逻辑与参数通道，非生产装配注入）。J2 生产化需先确权 key 来源与注入点；
  结论待维护者裁决后回填本账本。
