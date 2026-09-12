# Koawa CodingAgent V2：Runtime 安全审计交接文档

## 1. 项目与任务

- 待审项目：<https://github.com/Koawa265Richard31/Koawa.codingAgentV2>
- 本轮已审版本：`dba375bf2d761ec80e10e8a3192caac5f5fcd106`
- 第一套产品基线：
  - OpenHands 前端：<https://github.com/OpenHands/OpenHands>
  - OpenHands SDK / Agent Server / Workspace：<https://github.com/OpenHands/software-agent-sdk>
- 后续补充基线：Cline、OpenCode。

任务目标不是提高一般产品完整度，也不是逐项实现论文方案，而是判断 Koawa 的 runtime 是否形成安全闭环，并只补齐直接服务安全闭环的能力。

## 2. 固定范围

只研究和改进 runtime 层：

1. 进程、文件系统、工作区和网络隔离。
2. 工具及 MCP 权限在真实执行点的强制落实。
3. action、参数、授权和执行结果之间的一致性。
4. 取消、超时、崩溃恢复及 `outcome_unknown` 语义。
5. 工作区污染控制、变更审计和可恢复性。
6. secret 的注入、传播、日志脱敏和出站边界。
7. memory、日志或工具结果形成跨会话持久化攻击的可能性。

明确排除：

- 模型层提示注入检测、对抗训练和提示词防御。
- Web UI、按钮、交互流程和视觉设计。
- CLI 输出格式、提示语和审批信息如何展示。
- 会话列表、自动标题、插件市场等一般产品功能。
- 单纯为了测试数量、覆盖率或性能指标而增加工作。
- 尚无现实攻击面的通用 provenance/taint 大框架。

说明：diff 的机器生成、完整性验证及回滚价值仍属于安全范围；只是不讨论如何向用户展示。

## 3. 安全闭环判定标准

一项能力只有能加强以下至少一个环节，才可以进入整改计划：

```text
识别受保护对象
→ 作出权限决策
→ 在执行点强制约束
→ 留下可信执行证据
→ 终止、隔离、恢复或回滚
```

每项候选改动必须标记为：

- **S（Security closure）**：直接闭合当前安全链路，可以进入整改候选。
- **C（Conditional）**：只在特定部署或攻击面存在时需要，保留扩展点，不立即实现。
- **N（Non-security product feature）**：主要服务易用性或普通产品完整度，不进入本轮。

不能因为论文提出过、成熟产品实现过或测试容易编写，就自动判为 S。

## 4. 产品基线的使用方式

判断顺序固定为：

1. 检查成熟开源 Coding Agent 的真实源码、默认配置、执行路径和测试。
2. 判断这些机制解决的是安全问题、可靠性问题还是产品体验问题。
3. 对照 Koawa 已有实现，确认是否存在真实、可到达的缺口。
4. 最后使用论文解释攻击链和验证方法，不把论文当需求清单。

不能只阅读 README、架构图或设计文档。必须找到实际装配入口、默认值、执行器和恢复路径。声明、metadata、日志记录不能当作强制安全控制。

产品基线也不是安全真理。例如 OpenHands 为兼容渐进式 MCP 工具发现，会在运行中接受 `tools/list_changed`；这属于产品取舍，不代表 Koawa 必须复制。

## 5. 已确认的 Koawa 能力

Koawa 不是只有安全接口的空壳，以下实现已有实质价值：

- Policy、Approval、Ledger 和 action digest 形成确定性执行链。
- 具有 `ALLOW / DENY / ASK` 和失败关闭逻辑。
- 路径解析包含越界及符号链接/Windows junction 防护。
- 测试 Docker 沙箱使用无网络、只读根目录、非 root、cap-drop、资源限制和只读仓库挂载。
- MCP 工具具有命名空间、schema 校验、catalog/semantic binding digest。
- MCP 返回结果会被标记为不可信内容。
- 具有耐久执行记录、恢复和 `outcome_unknown` 相关设计。

后续审计不能重复建设这些能力；必须验证其是否接入真实主路径，以及是否存在绕过路径。

## 6. 当前重点风险假设

以下只是需要继续验证的候选，不等于全部都要实现。

### S 候选：MCP 进程边界

当前 `SandboxedLauncher.launch()` 会返回 `mcp_sandbox_unavailable`；实际可用路径主要是 legacy host 或 `host_trusted`。后者没有完整文件系统和网络隔离。

需要确认：

- 任意第三方 stdio MCP 是否属于产品支持范围。
- MCP 是否与 Koawa 主进程共享宿主权限、文件、网络和环境。
- 恶意 Server 能否在启动阶段绕过工具调用 Policy。

如果产品允许任意本地 MCP，这属于 S；如果只允许可信 MCP，或整个 agent 运行在外层隔离容器中，则降为 C。

### S 候选：终止和结果不确定性

需要验证取消、超时和崩溃是否真正终止整个进程树，以及远端/MCP 动作超时后是否可能继续产生副作用。结果无法确认时不得自动按“未执行”重试。

### S/C 候选：工作区隔离

仓库存在 worktree 相关模块，但正常 `AppRuntime` 路径可能仍直接绑定 `config.repo`。需要判断：

- agent 修改是否直接污染用户工作区。
- 是否能根据可信记录丢弃、回滚或交付变更。
- worktree 是否确实形成执行边界，而非仅靠 system prompt 要求 agent 在指定目录工作。

### S 候选：secret 与出站边界

需要验证 secret 是否按工具、server 和任务最小化注入；是否可能进入 MCP 环境、日志、错误信息和工具返回；所有真实网络出口是否经过可强制的 runtime 边界。

### C 候选：资源/参数级授权

Koawa Policy 目前主要按工具、server、scope 和副作用等级决策。是否需要扩展到具体文件、URL、账户或资源 ID，应由真实产品用例决定。先解决仓库边界、网络和 secret，不预先建设通用策略语言。

### 暂缓：provenance、taint 与 memory quarantine

只有在存在自动长期 memory 写入、富文本自动加载、跨信任域数据组合或实际出站 sink 后，才重新评估。当前不得为论文覆盖率改造所有消息类型。

## 7. OpenHands 第一轮源码对齐得到的修正

OpenHands 当前拆分为前端仓和 `software-agent-sdk`，审计必须同时覆盖两者。

已观察到的生产取舍：

- 确认模式关闭时映射为 `NeverConfirm`。
- conversation worktree 已产品化，但 SDK 默认值仍为 `false`。
- 开源 `DockerWorkspace` 提供容器生命周期，但默认未加入 `--network none`、只读根目录、cap-drop 等完整强化参数。
- MCP 收到 `tools/list_changed` 后会刷新、增加、替换和删除运行中工具，而不是固定启动时 catalog。

由此得到的判断：

- 不应因为论文存在就把 catalog 固定、全链路 taint、memory quarantine 全部列为 P0。
- OpenHands 的默认值也不能直接复制；需要结合其云端外围基础设施和 Koawa 的本地部署拓扑判断。
- Koawa 已有的确定性账本与沙箱参数在部分安全点上比 OpenHands OSS 更严格。
- 后续更值得学习 OpenHands 的是进程生命周期、事件耐久性、workspace/worktree 接线、MCP 凭据生命周期和故障恢复，而不是一般前端产品功能。

## 8. 后续审计任务

### 阶段 A：完成 OpenHands runtime 对齐

重点检查：

1. LocalWorkspace、DockerWorkspace、CloudWorkspace 的真实隔离保证。
2. terminal 命令的启动、取消、超时和进程树清理。
3. conversation 事件的写入顺序、崩溃恢复和重复执行控制。
4. worktree 创建、失败降级、清理和变更交付路径。
5. MCP stdio/HTTP Server 实际运行位置、secret 展开、动态工具更新和故障恢复。
6. 哪些安全保证来自外部云编排，不能算作 OSS runtime 自身能力。

### 阶段 B：用 Cline 和 OpenCode 补充本机型基线

只检查与安全闭环有关的部分：

- 本机 terminal 权限和工作目录边界。
- auto-approve/permission rules 的真实强制点。
- MCP Server 的启动权限和凭据传播。
- 命令取消、后台进程和失败恢复。
- 文件修改的回滚、checkpoint 或 worktree 机制。

不分析其界面展示和普通 IDE 体验。

### 阶段 C：回到 Koawa 做最终裁决

每个差异必须回答：

1. 攻击者或故障通过什么真实入口触发？
2. 现有 Policy/Ledger/Sandbox 为什么截不住？
3. 成熟产品如何处理，还是明确选择不处理？
4. 最小可行修复是什么？
5. 不修的风险和适用前提是什么？
6. 分类为 S、C 还是 N？

## 9. 最终输出格式

输出一张裁决表：

| 安全链路 | Koawa 当前实现 | 产品基线做法 | 可到达缺口 | 分类 | 最小修复 | 不做什么 |
|---|---|---|---|---|---|---|

随后只列：

1. 已由代码证明的缺口。
2. 按 S/C 分类后的整改顺序。
3. 每项整改的明确验收条件。
4. 论文中被排除或暂缓的设计及原因。

不要输出笼统的“最佳实践清单”，不要以新增测试数量作为成果，也不要未经确认直接修改 Koawa 仓库。

## 10. 可直接交给工作会话的任务指令

> 继续审计 Koawa.codingAgentV2 的 runtime 安全闭环。不要只看 README，也不要把论文当需求清单。先完成 OpenHands 的源码级对齐，再用 Cline/OpenCode 补充本机型产品基线。只研究 sandbox、worktree、进程生命周期、事件持久化、MCP 生命周期、secret 边界、恢复与回滚；排除模型层防御、Web UI、CLI 展示和一般产品体验。所有候选改动必须按 S（直接闭合安全链路）、C（条件性需要）、N（非安全产品功能）分类。只有能证明存在真实入口、现有控制无法拦截且有明确执行强制点的 S 项才进入整改建议。先审计和报告，不要直接改仓库。
