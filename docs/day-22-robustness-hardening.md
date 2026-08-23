# D22：真实会话健壮性加固（参考 Codex CLI / Harness 设计）

> 状态：COMPLETE（2026-08 实现，tests/test_d22_*.py 30 用例全绿、全量回归绿）。
> 上游：D20 Part B 真模型验证 7 轮暴露的问题（F1/F2/F3/F4/F6），证据 docs/../examples/d20_session_verification.md。
> 设计参考（仓库内其他 Agent 源码，仅参考不复制）：
>   - codex-cli-code（OpenAI Codex CLI，Rust）apply-patch 引擎 / turn_diff_tracker / responses_retry / compact
>   - deepseekharness（DeepSeek Harness）会话与工具执行契约
> 原则（沿用 D20 事故教训 + D21 诚实边界）：改动都要有确定性断言测试；不改模型行为，只改运行时；每项先写"参考了什么、为什么"。

## 1. 范围（来自 D20 Part B 归因清单）

| 编号 | 问题 | 性质 | 修复目标 |
| --- | --- | --- | --- | --- |
| F2 | UPDATE 被 baseline_dirty_path_forbidden 高发误拦（35B 4/4、122B 1/2） | 运行时 | 基线判定改为内容锚定，消除 stat 幻影 |
| F6 | 122B 首轮 2/2 invalid_completed_snapshot，整轮丢失 | 端点/解码器 | 空完成归一 + 无副作用前提下的单次有界重试 |
| F1 | 无工具幻觉完成（35B 3 次） | 运行时兜底 | 交互完成门：声称改了文件却没有写工具事件 → 判失败 |
| F4 | files= 记忆缺未跟踪 ADD | 运行时 | changed_files 改从 apply_patch 成功结果收集 |
| F3 | baseline_dirty 类错误无 detail | 运行时 | 为基线类错误补 detail（仅稳定码，不含路径正文） |

## 2. 逐项设计

### 2.1 F2：UPDATE 授权改内容锚定（参考 codex-rs apply-patch；判定与 status 解耦，已定案）

参考：Codex 的 apply_patch 引擎（codex-rs/apply-patch）从不在编辑授权时查询 git status；正确性由"上下文/哈希 vs 当前文件内容"保证；git 状态只用于 UX（git-utils/apply.rs 解析 "Applied patch ... cleanly"）。

现状：editing/tools.py:92-96 把 change.path ∈ protected_paths（= GitFacade 启动时 status 的 baseline_paths，verification/git.py:144）当作 UPDATE 前置否决。D20 实况证明该 status 会被 stat 缓存幻影污染（会话中报 " M"，磁盘 hash 与 HEAD 一致）。

方案（已定案，2026-08 评审收敛）：
- **授权判定 = 内容锚定，与 status 完全解耦**（实现采用归一化内容 diff）：
  对每个候选 UPDATE/DELETE 路径执行 `git diff --quiet --ignore-space-at-eol -- <path>`
  （工作区 vs 索引，忽略行尾 CR）：退出码 0 视为净 → 放行；非 0（真实文本改动/删除）→ 脏 → 拦截；
  未跟踪文件（无索引条目）恒定保护。
  **实现期根因修正**：初版按设计用 hash-object vs ls-files -s 索引 blob 逐字节比对，
  实测在 Windows 上干净仓库仍全部判脏——facade 为安全置空全部 git 配置（GIT_CONFIG_*=devnull，
  D5 加固）使 core.autocrlf 失效：Python/编辑器写盘 CRLF、git add 存 LF，逐字节不同 → 确定性 " M"，
  而 git diff 归一化后为空（这正是 D20 六轮所谓 stat 幻影的完整机制）。
  --ignore-space-at-eol 让判定配置中立、跨机器可复现：CRLF 与 LF 视为同一文本，真实改动仍检出
  （探针实测：CRLF-only exit 0，真实改动 exit 1）。
- **status 的角色收敛为三类非决策用途**：
  ① 枚举：`status --porcelain -z --untracked-files=all` 仍是找出"磁盘上有但不在索引/HEAD"的未跟踪
     文件路径的最廉价方式（未跟踪文件无索引 blob 可比，直接视为脏保护）；
  ② 展示/审计：git_diff、会话状态摘要（Codex git-utils 的 "Applied patch ... cleanly" 同为 UX 用途）；
  ③ 后验：D5 完成门的 baseline_unchanged 检查（会话结束后验证用户工作未被篡改，非授权路径）。
  status 对"放行"不再有任何否决权——这正是 D20 六轮实测的结论：幻影 " M"（stat 假阳性、内容与 HEAD 一致）
  在 status 视角下永远脏，只有内容判定能区分真伪。
- **实现基础已存在**：verification/git.py:135-137 `_baseline_fingerprints` 启动时已为 baseline 路径算内容指纹；
  定案改为把"名单真伪"从 status 换成哈希比对（hash-object vs ls-files -s 索引 blob），
  并只对候选 UPDATE 路径按需计算（每轮通常 1–2 个路径，可缓存指纹）。
- 原备选 B（调用时重查 status）与 C（去 gate）废弃：B 仍受 stat 时序影响，C 丢失对未跟踪/真脏文件的保护。

保留 protected_paths 的原始意图（启动时已脏/未跟踪的文件 = 用户未提交工作，禁改），
只把"脏"的判定从"status 说有 M"改为"内容 hash 与索引 blob 不一致"。

测试：tests/test_d22_baseline_fingerprint.py——(1) 干净文件 + 伪造 stat 差异（touch 改 mtime）→ UPDATE 放行
（幻影不再错杀）；(2) 内容真的改过（hash-object ≠ 索引 blob）→ 仍拒（保护用户工作）；
(3) 未跟踪文件 UPDATE → 拒；(4) 现有 D5 脏基线保护测试全绿。

备注：第 5 轮磁盘复核（git hash-object == ls-files -s 索引 sha，status 干净）正是本判定的原始证据。

### 2.2 F6：空完成归一 + 单次有界重试（参考 codex-rs responses_retry）

参考：Codex 只对**传输类**错误有界重试（responses_retry.rs：max_retries + 指数退避 + 用户可见通知 + 传输回退），协议/内容校验错误不重试。

我们的差异点：invalid_completed_snapshot 属于内容类，Codex 也会失败回合——但 35B 从不出现、122B 首轮 2/2 必现，说明是**端点空完成**的规律行为而非随机内容损坏。

方案：
1. 解码器归一：完成快照为空输出且 finish/usage 缺省时，不再抛 invalid_completed_snapshot，而是产生稳定 stream 失败事件 `openai.empty_completion`（与既有 empty-delta 容错同类）；
2. 有界重试（仅当**未发出任何输出片段**时）：worker 对 empty_completion 同一请求重试 1 次（幂等安全：无工具副作用已发生）；仍失败 → 回合失败并记录事件 `stream.empty_completion_retried`；
3. 事件库可审计：重试次数、原因入事件（延续 D7 审计理念）。

判据与 Codex 的差别要写进注释：Codex 不重试内容类错误防副作用重复；我们只在"零输出"前提下单次重试，副作用风险为零。

#### 透明性原则（2026-08 评审新增，适用于所有自动动作）

任何自动动作（空完成重试、摘要回退等）都必须让用户可感知：
- CLI 明确显示发生了什么（重试第几次 / 用了哪个模型生成摘要）；
- **禁止静默降智**：绝不悄悄换模型（尤其换更小/更便宜的模型）而不告知；
- 事件库记录每个自动动作（原因 + 模型名），与 D7 审计同一纪律。

### 2.6 F6b：收尾摘要——回合主体完成但最终回复失败时给用户可见输出

场景（用户评审确认）：任务主体动作（工具调用）已成功且持久化，仅**最终回复流失败**（空完成/断流）。
现状：回合整体 FAILED，/history 不记该轮，用户只见 "[turn failed]"——工具开销在叙事上丢失
（工作没丢，故事丢了）。与"规划阶段失败"本质不同：这里是缺叙事，不是缺决策。

方案：
1. **确定性收尾摘要（必做，零模型依赖）**：最终回复失败且该回合存在成功工具事件时，
   CLI 打印事件驱动的摘要，素材全部来自事件库/会话记录（工具轨迹 ✓/✗、changed_files、续做方式）：
       【回合主体已完成，最终回复生成失败】
       已执行：✓ apply_patch (index.html) / ✓ git_status
       改动文件：index.html（动作已持久化；输入 /resume 可重试生成回复）
   确定性、可测试、无 provider 依赖——"至少可见输出"的最小满足；
2. **可选摘要模型回退（默认关闭，用户显式配置 fallback_summary_model 才启用）**：
   仅在上述场景用该回退模型生成一次自然语言摘要。约束：
   - 只发一次、短输出、不放开预算（一次请求；缓存缺失成本有界）；
   - CLI 必须明示："最终回复失败，已用模型 X 生成摘要"——模型名可见，禁止静默降智；
   - 事件库记录 summary_fallback_used（含模型名、原因）；
   - 默认关闭：35B 五轮从未触发收尾失败，没有样本；等真机数据再评估是否默认开；   - **作用域硬保证（2026-08 评审新增）：回退只作用于这一次摘要请求（request-scoped）**。
     会话/回合的模型配置不可变：下一轮 turn 仍然使用主模型（用户配置的模型），
     绝不允许实现成"切换到降级模型"的会话级状态；
     实现位置约束：摘要请求由 app 层独立构造的一次性请求（直接走 client.stream 或专用摘要路径），
     不得修改 config/session/worker 的模型字段——保证下一轮 turn 的 ModelRequest.provider/model 仍是主模型；
   - 回退模型生成摘要也失败 → 不再重试，回落确定性摘要（失败路径可收敛，不无限折腾）；
3. **时机边界**：只允许收尾场景（主体动作已完成）。规划/中间阶段失败不做任何模型回退，
   失败即如实报告，模型选择权留在用户（2026-08 评审，理由：换模型=缓存全失+任务连续性风险）。

参考：Codex compact_model_fallback.rs 同取向——回退只用于窄任务（压缩/摘要），不用于主认知工作。
评估：确定性摘要无条件值得做（成本低、素材现成）；模型摘要回退价值受频率制约，opencode 未做
（其 UI 本身是反馈通道，用户可低代价重发），我们的 CLI 叙事差异不足以默认开启。

测试：tests/test_d22_turn_summary.py——scripted 流最后一段失败、前置工具成功 → CLI 输出确定性摘要
（断言含工具名与文件）；配置摘要模型时 → 断言输出明示模型名 + summary_fallback_used 事件且**仅一次**；
回退之后下一轮 turn 的 ModelRequest.provider/model 仍为主模型、config 未被修改（作用域不泄漏）；
回退模型也失败 → 回落确定性摘要（不二次回退）；规划阶段（首轮）失败 → 断言无任何回退动作、直接失败。

测试：tests/test_d22_stream_retry.py——scripted 流先给空完成再给正常完成 → 成功；连续两次空完成 → 失败且回合记录审计；有输出片段后空完成 → 不重试直接失败。

### 2.3 F1：交互完成门（防幻觉完成）

参考：Codex 不做"完成声明校验"，用 per-turn 已应用变更展示（turn_diff_tracker）让用户看到事实。我们的交互是纯文本 UI，需要程序化校验。

方案（保守启发式 + 稳定码）：
1. 回合结束时若最终文本匹配写入声明模式（已创建/已修改/已删除/文件已写等），且该回合**没有任何成功的写类工具事件**（apply_patch 成功），则回合判失败：码 `claimed_change_without_tool`（匹配函数白名单 + 中文术语表，避免误杀纯问答）；
2. 误杀保护：声明模式匹配不到 → 不判；模型已调用 finalize/完成类工具 → 不判；
3. CLI 显示失败原因（"模型声称修改了文件但没有工具调用"）。

诚实边界：这是启发式（模式匹配），不是语义校验；文档写明局限。

测试：tests/test_d22_completion_gate.py——scripted 模型声称创建但零调用 → FAIL；声称修改且有 apply_patch 成功 → PASS；纯文本问答 → PASS。

### 2.4 F4：changed_files 来源改 apply_patch 结果（参考 turn_diff_tracker）

参考：Codex turn_diff_tracker 的权威来源是**已提交的 apply_patch 变更记录**（AppliedPatchChange 列表），从不依赖 git diff 猜测（其注释："without rereading the workspace filesystem"）。

现状：cli.py _turn_changed_files 解析 git_diff 结果的 changed_paths → 未跟踪 ADD 不在 git diff 里。

方案：apply_patch 工具结果 JSON 已含每次 change 的 path（ADD/UPDATE/DELETE）。_turn_changed_files 增加来源：该回合 apply_patch 成功事件的结果里收集 paths，并与 git_diff 并集。保持 D19 的 turn.files= 语义。

#### git_diff 的残余职责（2026-08 评审：防止又把它当唯一事实源）

F4 之后 git_diff 从"文件清单唯一源"降级为三个明确职责：
- ①内容级 diff：unified diff 展示（用户审查 / SESSION.md 记录）——apply_patch 结果只有 path+hunks，
  无前后全量内容，内容对比只能问 git；
- ②外部变更检测：会话期间用户/其它进程对文件的修改——apply_patch 看不到模型不知道的事，
  这是唯一来源（与 D5 baseline_unchanged 后验同一意图）；
- ③完成门/审计后验：会话结束后的事实核对（baseline_unchanged、agent_changed_paths）。

分层原则（2026-08 评审，与 F2 的 status 收敛同一哲学）：
- **agent 自己的动作问自己**：文件清单/记忆/审计 ← apply_patch 结果 + 事件库（权威、无 git 延迟、无 stat 干扰）；
- **仓库的外部事实问 git**：内容 diff、用户变更、后验 ← git status/diff。
实现时不得把 files= 重新依赖 git_diff 解析（D19 既有 4 测试转为以 apply_patch 来源为主、git 为补集后仍全绿）。

测试：test_d22_changed_files_untracked.py——纯 ADD 回合 → files= 含新文件（覆盖 D19 4 测试 + 新断言）；含用户编辑文件（外部变更）时 → files= 含 git 补集的路径。

### 2.5 F3：基线类错误补 detail（低风险）

现状：baseline_dirty_path_forbidden 无 detail，模型只会重试。
方案：detail=protected_user_changes:start（启动时已存在用户未提交/未跟踪改动 → 提示管理员处理）；在内容哈希判定下幻影不会再进保护集合，detail 只出现在真实保护场景。给工具错误加 example 提示（"commit 或撤消该文件的未提交改动后可修改"，仅当有真实用户改动时）。

## 3. 测试与验证

- 新增 tests/test_d22_*.py（30 用例，全 scripted、离线、不依赖 Docker）：baseline_fingerprint 7 / stream_retry 3 /
  turn_summary 14 / completion_gate 6；
- 全量回归：387 + 新增 全部绿；
- 可选（真实模型，opt-in，需同意 + API 成本）：D20 driver 重跑一轮 35B + 一轮 122B，目标：A5e/A5f 转绿、F6 首轮不丢任务。

## 4. 不做（边界）

- 不改模型行为/不加隐式工具重试/不做语义检索；
- 不抄 Codex 的 Rust 实现细节（只借设计取向）；
- F1 完成门是启发式，不声称语义级防谎（D21 诚实边界延续）；
- 不做自动模型回退/轮换/熔断式换模型（规划与中间阶段；收尾摘要回退仅限用户显式配置且可见——见 §2.6）；
- 不建通用 provider 方言库：只归一实际使用端点的已观测怪癖，标注为 provider 债务（2026-08 评审）；
- 禁止静默降智：任何自动动作（重试/摘要/回退）必须用户可感知，事件库留痕（§2.2 透明性原则）。

## 5. Definition of Done

- F2 内容锚定判定 + F6 归一/重试 + F6b 收尾摘要（确定性必做 + 回退可选） + F1 完成门 + F4 files= 来源 + F3 detail 全部实现；
- 新增测试全绿、全量回归绿（与基线一致，无新跳过）；
- 事件库为 F6 重试与 F1 拒绝各留下可审计事件；
- 文档：day-22 置 COMPLETE、路线图 D22 → COMPLETE；
- commit + push origin/main。

## 6. 风险

- F2 内容哈希判定需对候选路径算 hash-object（强制读文件；每回合通常 1–2 个路径，同一回合内可缓存指纹，代价可接受；不再触发 git status 的子进程开销反而更少）；
- F1 模式匹配可能误杀（白名单+仅匹配声明词汇，测试覆盖三类）；
- F6 单次重试增加至多 1 次/回合的请求量（仅空完成时）。
