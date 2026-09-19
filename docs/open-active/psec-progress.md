# PSEC 轨道进度账本

规划：`docs/agent-security-platform-track-plan.md` v1.1（评审定稿 + T7 应用修订）。
基线：`PSEC_BASE_COMMIT = e213580`（2026-09-06 核对，与 origin/main 同步）。
规则：认领先行、单切片单会话、证据必填、禁止缩小范围自救（见规划 §3）。
状态图例：⬜ pending ｜ 🟨 claimed（会话登记后）｜ 🟩 done（四件套齐）｜ 🟥 blocked。

## 一、切片状态

| 切片 | 状态 | 认领会话 | 起点 commit | 证据（文档/测试/commit） | 备注 |
|---|---|---|---|---|---|
| S0 框架词汇映射 | 🟩 | 本会话（ZCode 主会话，2026-09-06 顺序执行，无并行争用） | e213580 | docs/psec/s0-framework-mapping.md；无代码变更 | 框架版本已钉定（访问 2026-09-06） |
| S1 MCP 授权+secret 审计 | 🟩 | ZCode 主会话 2026-09-06 | e213580 | docs/psec/s1-mcp-auth-identity.md；无代码变更（C 级三项：CLI env 继承 / 远程 MCP OAuth / 凭据代管通道） | env 层 S 候选闭合：secret_variable_forbidden 结构拒绝 + 摘要入档 + content-free 错误码 |
| S2 工具面固化验证 | 🟩 | ZCode 主会话 2026-09-06 | e213580 | docs/psec/s2-tool-surface-pinning.md；无代码变更、无新增测试（既有测试已钉定） | 矩阵 6 格闭合 + 1 格 N-A(行为漂移)；观察 2 项 C 级（pending_refresh 无消费者 / refresh 生产不可达） |
| S3 PDP/PEP 设计研究 | 🟩 | ZCode 主会话 2026-09-06 | e213580 | docs/psec/s3-pdp-pep-externalization.md；无代码变更 | 定级 C，四条 C→S 触发条件；OPA(decision-logs)/Cedar(v4.5) 已钉定；决策留痕复用 D1 事件流 |
| S4 scoped taint 设计研究 | 🟩 | ZCode 主会话 2026-09-06 | e213580 | docs/psec/s4-scoped-taint.md；无代码变更 | 定级 C：untrusted 标记产生于 connection_manager.py:673、零消费者（grep 实证）；最小设计=动作边界 taint 门复用 J2 五事件模式；触发条件主项（实际出站 sink）不成立 |
| S5 委派安全+T7 提案 | 🟩 | ZCode 主会话 2026-09-06 | e213580 | docs/psec/s5-delegation-security.md + docs/psec/t7-amendment-proposal.md；**T7 已应用**（维护者批准，威胁模型 021fecb9→3d131d82，三锚点）；边界钉定测试 3 用例绿 | 四空白定位到机制级；推荐语义 C（digest 种子集上浮，待设计评审） |

## 二、阻塞清单

| # | 切片 | 阻塞 | 根因 | 处理 | 状态 |
|---|---|---|---|---|---|
| B-1 | S5 | ~~T7 增补提案待审批~~ **已解决（2026-09-06）**：维护者批准应用；RT/J v1.2 解除"不新增 T7"冻结；T7 三锚点应用（威胁模型 021fecb9→3d131d82）+ 边界钉定测试交付。遗留子项：J2 树级传播语义 C 仍待设计评审（非阻塞） | RT/J §1.3 冻结"不新增 T7" | 提案文件 t7-amendment-proposal.md（含 sha256/锚点/after 文本/diff allowlist 自检）+ rtj-progress §八 治理事件 | 🟩 已解决 |
| B-2 | T7 应用回归 | ~~D10 两个 subTest 确定性失败~~ **已解决（2026-09-06，维护者"一次性实现"指令，方向 b 落地）**：docstring 改为如实描述无条件翻转（含显式 True）；裸 string 期望迁移为 `pattern` 形态（同 rtj 阻塞 #3 技术）；`additionalProperties: True` 改为翻转钉定测试 `test_additional_properties_true_is_coerced_not_honored`（绑定成功且永不生效）。原根因记录保留：D25 文档 :463"强制（只严不松）"与代码一致，失准的是 docstring 与 D10 旧期望 | 期望漂移 vs D25 schema 规范化（88350e8） | docs/psec/b2-d10-normalization-ruling.md + 代码/测试修复 | 🟩 已解决 |
| B-3 | 语义 C 接线 | 委派图 → 子 loop 的 `ancestor_turn_ids` 传参未接（子 agent 工具执行路径未核实） | agents/ 不直接驱动 execution.loop；子 agent 工具执行的装配方式需要 D12+ 归属会话核实 | 机制/上下文字段/执行器扫描已全部就绪并测试（`AgentLoop(ancestor_turn_ids=...)` 一参数即接）；接线是纯调用方工作 | 🟨 待 D12+ 归属会话 |

## 三、全量回归归档（代码切片后追加）

| 切片 | 日期 | discovered / passed / failed / errors / skips | 归档 | 备注 |
|---|---|---|---|---|
| — | 2026-09-06 | 未触发：S0–S5 全部无代码变更（规划 §3.3 第 4 条仅约束代码切片） | — | 既有测试仅被引用未重跑（S2 诚实边界已声明） |
| T7 应用 | 2026-09-06 | 1004 / 1002 / 2 failed / 0 errors / 32 skipped（818.5s，py -3.13） | `.dsh_tmp/psec-lanes/t7-application-2026-09-06.{json,txt}` | 2 失败均为既有 B-2（D10 期望漂移，非本轨引入）；新增 T7 边界测试 3/3 绿 |
| 一次性实现 | 2026-09-06 | 1016 / 1016 / 0 failed / 0 errors / 32 skipped（1100.8s，py -3.13） | `.dsh_tmp/psec-lanes/semantics-c-one-shot-2026-09-06.{json,txt}` | **全绿**：B-2 修复后 D10 全过；语义 C + J2 激活零回归 |

## 四、后续增量（2026-09-06，维护者"可以"指令后）

| 增量 | 产出 | 价值/发现 |
|---|---|---|
| RT/J v1.1 快照重建 | `docs/stability/rtj-plan-v1.1-snapshot.md` | 闭合审计盲点：v1.1 从未入库（v1.2 首次跟踪该文件），快照 = a9d8896 内容反向应用两处已知编辑，除头部三行与 §1.3 一行外与 v1.2 byte-identical |
| B-2 裁决备忘录 | `docs/psec/b2-d10-normalization-ruling.md` | **新证据改变建议方向**：D25 文档 :463 书面意图即"强制 additionalProperties:false（只严不松）"——代码符合 D25 意图，失准的是函数 docstring 与 D10 旧期望；方向 (a)（改代码拒绝显式 True）有重破 88350e8 官方绑定链路风险 → **建议方向 (b)**（修 docstring+测试+文档化），待 owner 签署 |
| 语义 C 设计评审稿 | `docs/psec/s5-semantics-c-design.md` | ①诚实缩小：单跳 spawn/send 已被父侧 J2 门覆盖，真实增量=多跳中继+非 action 通道；②自我纠正：纯 digest 扫描密码学上不可行 → 可信内存确定性导出扫描集（digest-only 契约保持）；③覆盖矩阵+四项实现前置 |
| **生产激活发现** | （记入 rtj-progress §八） | 全 src 证实：`SecurityGate` 无生产构造点、`LedgerExecutor.security_gate` 无生产注入（默认 None）、config 无 key 字段——**J2 门当前仅测试/lane 激活**。非本轨缺陷；语义 C/J2 生产化的第一前置 = 激活路径确权，待维护者裁决 → **已于同日"一次性实现"闭合**：`canary_key_env` 配置（env 变量名，key 本体只在环境）+ `resolve_canary_key` fail-closed + `_bind_ledger_policy` 注入；默认配置 gate=None（行为不变），配置即激活 |
| 一次性实现（2026-09-06，维护者 `/goal 一次性实现` 指令） | ①B-2 方向 (b) 落地：tool_binding docstring 如实化 + D10 裸 string 期望迁移为 `pattern` 形态 + `additionalProperties: True` 翻转钉定测试；②语义 C 全链：`SecurityGate.hit_multi` + `escalate(matched)`（own-turn 负载与 J2 原形 byte 一致）+ `ToolExecutionContext.ancestor_turn_ids` + `AgentLoop(ancestor_turn_ids=...)` + `_j2_check` 多模式扫描；③J2 生产激活：config/resolve/assembly 注入；④T7 威胁模型行/§3 图/测试按翻转计划同步（hash 3d131d82→见提交） | 新测试 `tests/test_j2_semantics_c.py`（10 用例：config fail-closed、hit_multi 优先级、执行器祖先升级含 payload 断言）+ `test_t7_delegation_boundaries.py` 翻转重写；受影响四套件 34/34 绿；**遗留**：B-3（agents 侧 `ancestor_turn_ids` 传参接线——机制就绪，唯此一点）；takeover×sticky 仍未审计 |
| Mimosa 深扫（钩子三次要求后启动） | 已封印：scan-2026-09-06T13-14-40.476Z-67f26f22b859，seal sha256:68779f40…；13 findings（10 high / 1 low 等：SQL 注入×9、代码注入、path-traversal、SSRF、命令注入 low） | **coverage=partial、runStatus=inconclusive**（30 秒静态快扫，未跑完覆盖）；扫描树=one-shot 提交（6367a16）之前。SQL×9 高概率为事件存储参数化查询的静态误报，但需逐条 file:line 分诊后才能定性——登记为后续分诊工作项，不在本轮自称"已审安全" |

## 五、诚实边界（不得越线声称）

以规划书 §6 为准：不声称实现 MCP OAuth、策略引擎、通用 taint 框架；S0 映射≠认证；结论绑定基线与材料版本。2026-09-06 一次性实现后更新：子 Agent 祖先种子扫描已实现（gate/executor/loop 层），**但 agents 侧委派图接线未接（B-3）**——不得声称端到端多 Agent 传播已在真实子 agent 运行中生效；takeover×sticky 交集仍未审计；变形/编码种子仍不检测（declared）。
