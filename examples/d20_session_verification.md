# D20 Part B：全会话真模型验证证据（真实 provider 会话转写摘要）

> 状态：COMPLETE（2026-08，5 轮实跑）。provider：SiliconFlow Qwen/Qwen3.5-35B-A3B。
> 复现：examples/d20_session_verify.py（opt-in，真实 API 计费，不进 CI）。
> 原始转写：D:/koawa-demo/d20_verify_a.txt / d20_verify_b.txt（演示机本地，不入库）。
> 协议：docs/day-20-real-session-robustness.md §3 Part B。

## 1. 环境与方法

- 每轮运行都新建唯一 git 仓库（仅一个已提交的 README.md，三行内容）；
- 会话 A：reasoning_effort=off、history_max_turns=2、compact_min_turns=2、全新 sqlite db；
- 会话 B：同仓库同 db（续同一 thread），reasoning_effort=low，1 轮问答；
- 覆盖 5 轮（第 1 轮脚本缺陷弃用、第 2/3 轮协议修正、第 4/5 轮为最终协议；第 5 轮含 update-index 实验组）。

## 2. 输入序列（最终协议，会话 A）

1. 修改已提交的 README.md：把第一行改为 # hello-koawa（先读 sha256，hunk 的 old_lines 必须与文件一致）；
2. 我上一条消息的第一个任务要求修改哪个文件？请用一句话回答；
3. 创建 calc.py（一行 def add(a, b): return a + b）；
4. 再次修改 README.md 第二行为 edition=d20；失败时请直接复述失败原因，不要反复重试；
5. 创建 index.html（doctype / h1 / p / 空行四行）；
6. /history；7. /recall hello；8. /journal；9. /exit。

## 3. 断言结果（第 4/5 轮，最终协议；11/14 与 10/14）

| # | 断言 | r4 | r5 | 说明 |
| --- | --- | --- | --- | --- |
| A0 | reasoning=off 时无思考流 | PASS | PASS | 思考流只在 low 时出现 |
| A1 | 记忆投影：第 2 轮回答引用第 1 轮任务 | FAIL | FAIL | 模型未引用历史（2 轮回答为角色复述，1 轮"无法得知上一条消息"）；机制本身在第 1/2 轮协议下正确回答过（引用 hello.md）——模型依赖性强的行为断言 |
| A2 | 压缩触发：compacted_blocks>=1 且投影有界 | PASS | PASS | /history 输出 {"turns":4,"projected_items":5,"compacted_blocks":1} |
| A2b | 压缩块含 files= 字段 | FAIL | FAIL | 会话内没有任何成功的文件修改（见 F2），changed_files 为空；另发现 files= 只覆盖 git diff 可见改动，未跟踪 ADD 不产生 files=（D19 局限，见 F4） |
| A3 | /recall 命中目标会话 | PASS | PASS | 命中任务 1/3 的用户消息与工具列表 |
| A3b | calc.py 真实存在（防无工具幻觉） | PASS | PASS | 第 4/5 轮模型真实调用了 ADD；第 1/2 轮曾出现无工具"已完成"的幻觉（见 F1） |
| A4 | /journal 生成 SESSION.md | PASS | PASS | D:/koawa-demo/d20_repo_*/SESSION.md |
| A5 | index.html 完成且内容正确 | PASS | PASS | 每轮都成功：doctype + h1 + p + 空行 |
| A5a | 完成真实性：最后回答伴随工具事件 | PASS | PASS | 对应窗口可见 → apply_patch 与 ✓ apply_patch |
| A5b | 形状类失败（invalid_patch_change / patch_context_mismatch）必带 detail | PASS | PASS | 本轮无形状类失败（单元契约由 tests/test_d20_tool_repair.py 覆盖）；产生的是 baseline_dirty 类（无 detail，见 F2/F3） |
| A5c | 全程无预算耗尽 | PASS | PASS | root=40 未触发 resource_budget_exceeded；但 UPDATE 轮曾以 max_output_tokens 结束（见 F2） |
| A5e | UPDATE 探针：任务 1 修改 README 第一行 | FAIL | FAIL | 所有轮次、包括干净基线 + update-index 实验组，UPDATE 全部被 baseline_dirty_path_forbidden 拦截（见 F2） |
| A5f | 任务 4 失败如实呈现 | PASS | PASS | 任务 4 在"失败请复述原因"措辞下模型如实报告错误，未无限重试（错误信息可读性改进生效） |
| B | reasoning=low 思考链实时输出 | PASS | PASS | 转写含实时思考片段，如："… 思考: 用户问的是上一轮创建的 index.html 文件中 h1 标题是什么…" |

## 4. 会话转写摘录（第 5 轮）

任务 1（UPDATE 探针）：

    → read_file {"path": "README.md", ...}
    → apply_patch {UPDATE README.md, base_sha256=06358091...}
    → git_status {}
    → git_diff {}
    → apply_patch {UPDATE ...}
    ...
    ✗ apply_patch [baseline_dirty_path_forbidden]  （×3）
    agent> [turn_failed] d2:model_finish_max_output_tokens

事件库中的 git_status 结果（会话进行中）：

    {"agent_changed_paths":[],"baseline_dirty_paths":["README.md"],"clean":false,
     "status":[{"path":"README.md","status":" M"}]}

会话 B（reasoning=low）：

    … 思考: 用户问的是上一轮创建的 index.html 文件中 h1 标题是什么。根据我刚才的回复，
    h1 标题是 "KoawaAgent D20"。我应该用一句话直接回答这个问题。
    agent> index.html 的 h1 标题是："KoawaAgent D20"。

## 5. 发现（真实模型验证暴露的健壮性问题）

**F1 · 无工具幻觉完成（严重）**：第 1/2 轮中模型 3 次直接宣称"已创建 calc.py / index.html"而没有任何工具事件，
文件实际不存在。交互模式（task_mode=False）没有 D5 完成门，模型可以空手"完成"。第 4/5 轮未再出现，
但这是随机行为，不能靠运气。建议：交互模式也接一个轻量完成门（声明过的文件必须真实存在）。

**F2 · UPDATE 路径被高发误拦（严重：35B 4/4、122B 1/2 轮被拦；与模型无关）**：
对已提交文件的 UPDATE 经常返回 baseline_dirty_path_forbidden。事件库证据显示运行期 git status 将
未变更的 README 报为 " M"（baseline_dirty_paths=[README.md]，agent_changed_paths=[]）；会话结束后
磁盘 git status 干净、文件 hash 与 HEAD 一致。驱动在第 5 轮运行前执行 git update-index --really-refresh，
仍被拦截；但 122B 对比轮 q122a 中同一任务四 UPDATE **一次成功**（README 第二行确实被改）——
证明这是瞬态误判（高发而非必然），与模型无关，且 UPDATE 全链路本身可用。建议 D22 专项：GitFacade
保护路径判定回退到内容指纹比对（stat 脏但 hash 一致 → 视为干净）。ADD 路径不受影响。

**F3 · baseline_dirty 类错误无 detail 且不可自愈**：模型拿到 baseline_dirty_path_forbidden 后只会重试同一
patch（3 次后撞 max_output_tokens）。任务 4 在用户明确"失败就复述原因"时才停止。错误码是稳定的、正确
的，但缺少"为什么"（文件脏的依据）与"怎么办"。

**F4 · files= 记忆字段的覆盖缺口**：changed_files 来自 git_diff 的 changed_paths；未跟踪的新文件（ADD）
不进 git diff → 压缩块无 files=。对"会话创建过哪些文件"的记忆不完整。建议：apply_patch 成功结果
（含 path 列表）也并入 changed_files。

**F5 · 记忆投影是机制可用、行为随模型波动**：同一协议下 5 轮中 2 轮正确引用第一轮任务、3 轮答非所问。
投影与压缩机制本身（context_items、compacted_blocks=1）验证通过；回答质量依赖模型遵循上下文。

## 6. 协议内 PASS 的能力（面试讲稿）

- 记忆：会话上下文跨轮投递（有界投影 + 压缩块），第 2 轮可回答基于第 1 轮内容的问题（2/5 轮）；
- 压缩：4 轮后 compacted_blocks=1，projected_items 保持在 5（窗口恒定，不随轮次增长）；
- /recall：词面检索跨轮命中，输出用户输入/工具列表/文件；
- /journal：生成 SESSION.md 会话纪要；
- 思考链：reasoning_effort=low 时实时流式输出（reasoning_sink），off 时零输出，双路径验证；
- 自修复链路：ADD 任务每轮一次成功；形状类错误有 detail 契约（单元测试覆盖），本轮无触发；
- 预算：始终未触发 resource_budget_exceeded。

## 7. 成本与可重复性

- 每轮约 15–25 次真实模型调用（含 1–2 次压缩摘要），单轮 6–10 分钟；总计 5 轮。
- 模型输出有随机性：A1/A3b 等行为断言可能随轮次波动；机制断言（A2/A3/A4/B/A5 族）稳定。
- 验证不进 CI；复现命令见 examples/d20_session_verify.py 文件头。
- 对比轮（122B）另记：单轮 8–14 分钟；35B 5 轮 + 122B 2 轮，总计 7 轮真模型会话。

## 8. 对比轮：更强模型 Qwen/Qwen3.5-122B-A10B（2 轮，D20_RUN_TAG=_q122 / _q122b）

> 目的：区分"35B-A3B 暴露的问题"哪些是模型能力问题、哪些是运行时问题。
> 方法：同一 driver、同一协议、仅 D20_MODEL=Qwen/Qwen3.5-122B-A10B；模型更贵更慢（单轮约 8–14 分钟）。
> 转写：D:/koawa-demo/d20_verify_a_q122.txt / d20_verify_a_q122b.txt。

| # | 断言 | q122a | q122b | 与 35B（r4/r5）对比 |
| --- | --- | --- | --- | --- |
| A0 | 思考关闭无思考流 | PASS | PASS | 一致 |
| A1 | 记忆投影回答 | FAIL¹ | FAIL¹ | 两轮首轮任务都被 F6 吞掉，模型如实说没有上一条消息——无法公平对比；行为诚实（优于 35B 轮 4 的答非所问） |
| A2 | 压缩触发 | PASS | PASS | 一致 |
| A2b | 压缩块含 files= | FAIL | FAIL | 一致（受 F2 影响：本会话无成功修改） |
| A3 | /recall | PASS | PASS | 一致 |
| A3b | calc.py 真实存在（防幻觉） | PASS | PASS | **显著改善**：35B 前两轮 3 次无工具幻觉完成；122B 两轮 0 次，全部真实调工具 |
| A4 | /journal | PASS | PASS | 一致 |
| A5 | index.html 完成 | PASS | PASS | 一致 |
| A5a | 完成真实性 | PASS | PASS | 一致 |
| A5b | 形状类失败带 detail | PASS（无触发） | PASS（无触发） | 一致 |
| A5c | 无预算耗尽 | PASS | PASS | 一致 |
| A5e | 任务一 UPDATE | FAIL¹ | FAIL¹ | 首轮任务被 F6 吞掉，未执行（见 F6） |
| A5f | 任务四 UPDATE 如实呈现 | PASS **成功**（line2=edition=d20） | PASS（本轮被 baseline_dirty 拦截） | **关键对照**：UPDATE 时而被拦时而成功 → 坐实 F2 是与模型无关的瞬态判定；q122a 证明 UPDATE 全链路 E2E 可用（幻影缺席时） |
| B | 思考链实时输出 | PASS | PASS | 一致（122B 思考内容更完整） |

¹ 伪影：首轮（任务一）在工具调用前就因 F6 失败，后续轮次无该任务上下文。

### 8.1 新发现 F6：122B 端点首轮必现 invalid_completed_snapshot（2/2）

- 现象：会话第一轮模型响应在**未产生任何工具调用**时失败：agent> [turn_failed] d2:openai.invalid_completed_snapshot，history_turns=0。122B 复现率 2/2；35B 五轮从未出现。
- 机制：model/openai_client.py 收流后构建 ModelTurn 的规范校验失败（TypeError/ValueError → openai.invalid_completed_snapshot，约 718 行）——122B 端点返回的完成快照（finish/usage 形态）不合解码器规范。
- 影响：整轮任务丢失且无自动重试（任务一从未执行，后续记忆断言失真）。这是"无隐式重试"设计在**流协议级失败**上的边界。
- 建议：随 D22 处理——对 provider 流协议级失败（非工具错误）做有界重试（1 次 + 审计事件），或解码器对 finish/usage 缺省做容错归一（与既有空 delta/usage 容错同一类）。

### 8.2 模型能力问题的诚实归因

1. **F1（无工具幻觉完成）= 模型能力问题**：35B 出现 3 次；122B 两轮 0 次、全部真实执行工具。更强模型显著改善，但样本小（n=2），完成门仍应作为兜底。
2. **F5（记忆遵循）= 部分模型能力问题**：122B 在无上下文时诚实承认（优于 35B 的复述/编造倾向），但两轮首轮任务丢失，无法验证"有上下文时"的记忆遵循；机制层（投影/压缩）两模型一致正常。
3. **F2（UPDATE 基线误判）= 运行时问题，与模型无关**：122B 两轮一成一败，与 35B 全败的差异是同机时序而非模型；q122a 的成功证明 UPDATE 自修复链路本身可用。
4. **F6 = 端点/解码器契约问题**：122B 特有 2/2，35B 无——指向运行时缺少流协议级有界重试（或解码器归一）。
5. 机制类断言（A0/A2/A3/A4/A5/A5a/A5b/A5c/B）两个模型完全一致通过：运行时能力不受模型影响。

## 9. 最终结论

- D20 Part B 协议全部机制通过；真实模型暴露的问题已按性质归因：模型能力 ×2（F1/F5）、运行时 ×3（F2/F3/F4）、端点 ×1（F6）。
- 建议 D22 专项：F2（基线判定回退内容指纹）、F1（交互完成门）、F6（流协议级有界重试）、F4（files= 覆盖 ADD）；F3（失败错误附修复指引）低风险优先。

