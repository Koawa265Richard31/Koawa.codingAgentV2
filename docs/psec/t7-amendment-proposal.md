# T7 威胁模型增补提案（PSEC/S5 产出；未应用）

状态：**PROPOSAL——未应用**。RT/J 规划 §1.3 冻结"不新增 T7"且威胁模型 §4 须 byte-identical；本提案登记 PSEC 账本 blocker，**经维护者按 RT/J 版本化审批后方可应用**。应用时遵循 RT/J §5"威胁模型精确影响"纪律（先例：e213580 的 T2/T4 精确行级增补，hash 0d81b274→021fecb9）。

## 1. 自检清单（RT/J §5 格式）

| 项 | 值 |
|---|---|
| 目标文件 | `docs/agent-security-threat-model.md` |
| 当前文件 sha256（提案时点，基线 e213580） | `021fecb978cecffff8b838cc1fa24ddc6e8d920b300783c9de7821a64e61e17e` |
| 插入锚点 | `### T6 供应链 / 配置投毒（OpenClaw 风格）` 节末（:88-100 区间）之后、`## 3. 拦截点一览（面试讲述用）`（:101）之前，作为新的 `### T7` 节 |
| diff allowlist | **仅允许**上述锚点处插入 §2 全文（纯新增物理行）；T1–T6 各节、§1、§3、§4、§5、文件其余部分**任一字节变化即失败** |
| 应用步骤 | ①校验当前 sha256 与上表一致；②应用纯插入 diff；③重算 sha256 并把 before/after 双 hash 记入提交信息；④`git diff` 复核仅含锚点插入 |
| 应用后义务 | §3"拦截点一览"表若需加行，须**同提案**明确列出（本提案不含，见 §3 说明） |

## 2. 拟插入全文（after 文本）

```markdown
### T7 多 Agent 委派链（confused deputy / 信号盲区 / mailbox 注入）

威胁描述：父 agent 将不可信内容（T1 仓库文本、T2 工具结果、T3 MCP 声明）转述进子
agent 的任务文本或 mailbox 消息；子 agent 工具面按 scopes 收窄（子权限只收窄），
但写/命令/MCP 能力（D12 起）的后果可能在父上下文不可见。安全信号不随委派传播：
会话 canary 令牌绑定单 turn（derive_canary_token(key, turn_id)），父上下文种下的
canary 种子对子 turn 结构性不可见；mailbox 消息体无信任等级标注。

拦截点：子 agent 动作经同一共享 executor（policy ALLOW/DENY/ASK → 审批五事件原子批
→ J2 canary 门 → budget reserve → Turn fence → claim 前 re-resolve），scopes 在
spawn 事件契约中收窄且运行期只减不增；mailbox 幂等键与 run fence 保证投递与提交
不重不丢；orphan takeover 旧 run 提交被拒。

失效条件：canary 令牌 turn 域不传播，父种子在子上下文的命中不升级（RT/J §1.2
declared non-scope）；mailbox 消息无信任语义，子可能把含不可信内容的消息当可信
指令；orphan takeover 新 attempt 与 execution-scoped sticky escalation 的交集未
定义；scopes 收窄正确性依赖装配层契约，运行时无独立自证；本条目不引入任何新
detector，升级语义以 J2 既有纪律为准（信号与 policy effect 分离、精确命中才升级、
digest-only）。
```

## 3. 范围说明

- 本提案**只增不并**：不修改 T1–T6 与既有章节的任何字节（对照 e213580 先例：那次是对既有表格行的精确增补；本次是新节插入）；
- §3"拦截点一览"表的对应行（T7 的拦截点摘要）**不在本提案内**——若维护者批准 T7 正文，拦截点表的加行作为同一审批的第二步或独立微提案执行，避免一次 diff 跨两处锚点；
- 措辞对齐：条目风格（威胁描述/拦截点/失效条件三段）与 T1–T6 及 RT/J §5 增补段的表述纪律一致；"declared non-scope"引用 RT/J §1.2 原文语义；
- 本提案由 PSEC/S5 产出（`s5-delegation-security.md` §2 威胁草案为内容来源），审批链接与 blocker 状态见 `docs/psec-progress.md`。
