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
| B-2 | T7 应用回归 | D10 `test_invalid_tools_are_rejected` 两个 subTest 确定性失败（裸 string schema、`additionalProperties: True`——期望 `unsupported_mcp_schema` 未抛出） | 期望漂移 vs D25 schema 规范化（88350e8，2026-09-04）：①规范化引入保守默认边界使裸 string 合法（同 rtj 阻塞 #3 的边界迁移，T3 毒样当时已迁、D10 期望漏迁，测试停自 80a901b 08-21）；②`additionalProperties` 被无条件翻转为 False，**与规范化函数自身 docstring"仅 absent → False"矛盾** | 归属 D25/D10 owner 裁决：修代码（仅对缺失键取默认）或修 docstring+测试并文档化翻转语义。**非 T7/PSEC 引入**：src 自 88350e8 零改动、standalone 双解释器（3.13/3.14）复现、方向朝更严（运行时比 server 声明更严，非放宽） | 🟥 待 owner 裁决 |

## 三、全量回归归档（代码切片后追加）

| 切片 | 日期 | discovered / passed / failed / errors / skips | 归档 | 备注 |
|---|---|---|---|---|
| — | 2026-09-06 | 未触发：S0–S5 全部无代码变更（规划 §3.3 第 4 条仅约束代码切片） | — | 既有测试仅被引用未重跑（S2 诚实边界已声明） |
| T7 应用 | 2026-09-06 | 1004 / 1002 / 2 failed / 0 errors / 32 skipped（818.5s，py -3.13） | `.dsh_tmp/psec-lanes/t7-application-2026-09-06.{json,txt}` | 2 失败均为既有 B-2（D10 期望漂移，非本轨引入）；新增 T7 边界测试 3/3 绿 |

## 四、诚实边界（不得越线声称）

以规划书 §6 为准：不声称实现 MCP OAuth、策略引擎、通用 taint 框架、子 Agent 信号传播；S0 映射≠认证；结论绑定基线与材料版本。
