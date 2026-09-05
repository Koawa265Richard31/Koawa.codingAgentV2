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
