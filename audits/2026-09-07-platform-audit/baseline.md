# 全项目审计基线与执行前验收定义

范围：D1–D25、I1–I9、RT/J、PSEC，含实际生产接线。仅审计、隔离复现和报告，不修改生产代码、原测试、威胁模型，不提交推送。未完成领域不得算通过。

HEAD：f3d2664ca3b848e0d73bf1170ba5a132be6c1e83；缓存 origin/main：e213580。实际有五个未推送提交（首次口头计数六个有误，以 git 输出为准）。工作区 README、assembly/config、verification/finalization/tools 已修改，随后 tests/test_d5_vertical_slice.py、tests/test_runtime_config.py 修改及 tests/test_d5_required_profiles.py 新增。归属未知，全部保持。其他 scratch、.dsh_tmp、.mimosa、docs 未跟踪材料不计作本轮证据。

Windows；本轮解释器使用 Codex bundled Python 3.12.14。py -0p 在最初受限环境返回无安装；不由此推断机器无其他 Python。未启动付费模型或下载依赖。

## 执行前定义

| ID | 预期产物与独立验收 | 代表性及局限 | 预算/介入 | 分类 |
|---|---|---|---|---|
| B1 | 隔离 Git repo 中实际 patch value=1→2，真实 Python assert 验证；记录 status/diff/final，再外部改为3，完成门应拒绝 | 验证→内容漂移→交付的阶段依赖；不是长任务能力证明 | 本机固定 Python，30秒单命令；审计器只注入一次外部改动 | 旧测试放行新内容=假完成；拒绝=边界保持 |
| B2 | D23 100轮 golden：≥3次压缩、重建等价、无悬挂调用 | 上下文压力与重建；脚本模型/假工具，不能证明任务交付 | 120秒；无人工修复 | 测试过只记模拟机制通过 |
| B3 | D5真实文件失败→修复→测试→完成现有专项，D6/D7恢复专项 | 文件和持久恢复分段验证，不冒充同一长任务 | 总300秒；不改断言 | 失败按实际错误，skip未执行 |
| B4 | eval任务执行测试与oracle，核查是否经过模型/Registry/Policy/Ledger | 多文件依赖；如果直接写答案，仅为fixture验收 | 60秒，无人工修复 | 不得把fixture成功计作Agent成功 |
| B5 | 真实模型+Docker+委派+压缩+kill/resume最终独立验收 | 完整超长任务基线 | 本轮未授权付费服务；条件不足时未执行 | UNKNOWN/未交付，不能记成功 |

安全实验分四类：完成且边界保持；安全阻断但未完成；完成但越权；未交付或不确定。现有专项成功不能替代这四类任务级统计。

## 初始覆盖矩阵（逐项待核验，不是通过清单）

| 领域 | 入口/关键代码 | 声明保证 | 生产接线待追踪 | 已有测试入口 | 本轮验证 | 跨模块依赖/发现/未覆盖 |
|---|---|---|---|---|---|---|
| 控制、事件、状态、并发 | control/runtime,sqlite_store,event_store,schema | typed event/exact CAS/current run | CLI→AppRuntime→ThreadRuntime | test_event_store,test_thread_runtime,test_s3_* | 待核验 | recovery/approval/agents跨流；全实现未审完 |
| 模型、Loop、Registry | model/protocol,stream,openai_client;execution/loop;tools/registry | 完整terminal后执行、有界schema | assembly→AgentLoop→LedgerExecutor | test_agent_loop,test_model_stream,test_d2_* | 待核验 | 取消/修复/预算/压缩组合未验 |
| 文件、测试、Git、完成 | editing/transaction;verification/* | 当前产物通过测试、保护已有改动 | build_verified_coding_tool_registry→completion gate | test_patch_*,test_d5_*,test_d22_baseline_fingerprint | B1/B3计划 | generation/内容绑定/恢复/报告消费者 |
| checkpoint、压缩、记忆 | recovery/*;execution/compaction;runtime/memory,session,turn_conclusion | 权威事实恢复、摘要不增权 | worker recorder→resume→memory | test_d6_*,test_d23_* | B2/B3计划 | 跨真实失败/恢复/压缩未联合验证 |
| ledger、幂等、执行权、UNKNOWN | ledger/*;runtime/claim_gate | claim先行、token fence、未知不盲重试 | assembly profiles→executor→recovery | test_d7_*,test_i7_* | B3计划 | apply_patch RETRY与文件崩溃需联合注入 |
| policy、approval、身份、凭据、预算 | policy;approval_service;runtime/config;security/* | 默认拒绝、精确授权、原子预算 | _bind_ledger_policy→authorize/claim | test_d9_*,test_j2_* | 待核验 | 配置/恢复/sticky/取消交集 |
| Docker、MCP、网络、第三方 | sandbox/*;mcp/* | 不可变镜像、最小权限、绑定代际 | activation→launcher→dual ledger | test_d8_*,test_d10_*,test_d25_* | 未执行真实容器 | daemon/镜像/网络/第三方行为未核验 |
| 多Agent、消息、worktree、集成 | agents/*;workspace/* | scope收窄、隔离、重测、fence | 实际子Loop与结果集成待追踪 | test_d11_*,test_d12_*,test_golden_composite_e2e | 未完成 | B-3、真实写委派、takeover×sticky |
| 会话、装配、取消、报告 | runtime/app,assembly,session,truth,turn_summary | 终态真实、持续会话、取消收束 | CLI/config run与interactive分支 | test_d16_*,test_i7_*,test_d22_* | 未完成 | 与D5完成报告和memory消费者一致性 |
| 安全实验、eval、故障、证据 | evals/run_eval;redteam/adapter;telemetry/* | 独立oracle、正控、无假成功 | RT bridge与生产装配须区别 | test_d14_*,test_j1_*,test_rt*,test_stability_* | B4计划 | 能力基线/消融/真实长任务/成本误拦截未证明 |

独立静态审查与架构子审查已启动，但均因账户usage limit中止且没有返回结论；不得将其算覆盖。security-scan id=42478287-a099-48ae-a4f2-f35749ee3eaf，预检ready，独立审查未完成。
