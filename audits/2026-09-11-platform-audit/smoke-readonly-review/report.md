# 只读审查冒烟报告（2026-09-13，按维护者指令终止）

目的：DS-V4-Flash 经生产 CLI 对本仓库做只读长任务审查（patch 策略 deny），验证执行链并监视提示词缓存命中。维护者在 run9 后因双侧余额耗尽叫停（"把报告保留，先不跑了"）。**Agent 三次尝试均未产出最终审查报告**；本报告记录冒烟本身的发现与遥测。产物：cfg.json / task1.txt / run7-9.log / run9-usage.json（本目录）。

## 执行记录（均在 interactive 管道模式，repo=本仓库，patch=deny）

| 运行 | 终态 | 轮/工具/压缩 | 输入 token / 命中率 |
|---|---|---|---|
| run7 | `context_capacity_exhausted`（6 轮） | — | 前期轮 69-92% |
| run8 | `max_model_rounds_exceeded`（60 轮上限） | 60/115/12 | 1,335,286 / **84.5%** |
| run9 | `turn_completed` 但 **final_text 为空** | 2/2/0 | 6,526 / 86.3% |

run1-6（更早的调参尝试，见主报告 §10 上下文）：db_inside_repo、facade 属主 bug、未跟踪杂物撑爆 status、budget_action_limits 默认 20、多行任务被拆多 turn、SiliconFlow 402/401。

## 缓存命中结论（F19 遥测首次实战）

- SiliconFlow 对 DS-V4-Flash **两种形态都回报**（`prompt_tokens_details.cached_tokens` 与 `prompt_cache_hit_tokens`），5/5 轮有数据。
- 长任务稳态命中率 **91-97%**；压缩事件后一轮回落至 ~24-69%（前缀重排使缓存失效）——与 in-run 压缩的代价模型一致：每次压缩牺牲一轮命中率换取上下文上限。
- run8 全程 84.5%（含 12 次压缩的回落轮）；未命中输入约 20.6 万 token。
- run9 首轮即 86%（无压缩）。

## 冒烟发现（编号接主审计）

- **S1（已修待回归）** GitFacade 封闭环境忽略全局 gitconfig 的 safe.directory → 本仓库（属主 CodexSandboxOffline）`not_a_git_repository`，生产链在任何非当前用户属主的真实仓库上不可用。修复：facade 对配置根自授权 `-c safe.directory=<root>`（verification/git.py）。
- **S2** 真实脏仓库一发 `git_status`（622+ 未跟踪条目）可单独击穿 64k 硬预算；status 条目无界进上下文是结构性问题（本轮用 .git/info/exclude 规避）。
- **S3 预算调参面**（默认值对长任务普遍偏紧）：`budget_action_limits` root 默认 **20**（独立于 max_tool_calls 的暗坑）；上下文 soft/hard 48k/64k 对真实仓库过小（read_file 单结果 3-4 万字符）；`in_run_keep_groups` 与组累积速度互锁——keep=6 时前 6 组不可压，大结果会在攒够组前击穿 hard（run7 根因）；`max_compaction_epochs_per_run`=16 在 60 轮任务中不够。最终可用档：rounds 120、actions 250、ctx 120k/176k/target 96k/reserve 24k、keep 3、epochs 96。
- **S4（新，待查）** run9：turn `completed` 但 final_text 空，且模型在内容里输出原始 `<tool_calls>` XML（DeepSeek 偶发非流式工具调用格式）——空文本 STOP 为何未被 `empty_final_answer` 拦截、XML 是否被当纯文本吞掉，需要一次离线复现定位。
- **S5（新）** 模型三次把 repo 根当子目录（`repo_map {"path":"v2"}`、`list_files` 同类）→ `workspace_path_not_found`；工具描述未说明"路径相对仓库根"。
- Provider 侧：中途出现 `openai.missing_done`/`http_error`（与余额耗尽过程重叠）；401 后换 `SF_CodingAgentTestKey` 恢复。

## DS 侧挖出的缺口（run8 进度行提取，2026-09-13 补记）

Agent 未及产出最终报告（死于轮数上限），以下从其 60 轮工作日志的进度叙述中提取，定性沿用其自标：

- **S6（新，DS 标"核心 P0/P1 候选"[确认]）任务模式主 loop 无防幻觉 claim gate**：主装配 loop（task 路径）`claim_gate=False` 仅有 completion_gate；chat/resume 相反（claim 有、completion 无）——两门互补互斥的结构性权衡。任务执行中途轮次"声称改动却未调写工具"不被拦截，仅最终 finalize 兜底。**是对 F16 修复范围的合法扩展**（F16 只补了 chat/resume 侧）。附带：`build_worker(claim_gate=True, task_mode=True)` 的 claim_gate 参数被静默忽略（task_mode=True 复用 self.loop）——API 陷阱[确认]。
- **S7（新[确认]）工具契约摩擦（多轮浪费的直接原因）**：`update_plan` 状态只接受 pending/done，"in_prog" 被拒且描述未写明（DS 试错一次自修）；`search_text` ×7 `workspace_not_directory`、`repo_map` 多次路径错——工具描述未说明"路径相对仓库根"与参数形态（与 S5 同源，DS 以真实轮数验证了代价）。
- **S8（新[推测]，DS 自标 P2 需裁决）`run_test_profile` 声明为 READ_ONLY 副作用类**：测试命令实际执行代码，"只读"是设计假设而非事实；登记为设计裁决项，非缺陷。
- **交叉验证（正面）**：DS 独立复核了 F12-F16 修复后的装配面并确认接线正常（policy 规则完整、completion_gate 正确委托 assert_complete、durable_sink 为 per-run recorder、压缩绑定）——真实模型侧对修复状态的一次交叉验证。
- **DS 未完成部分**：recovery 链深读、tests 生产路径 vs 直构覆盖统计、P0-P2 汇总（死前 pending）。

**动手排序建议（等余额后）**：① S6 task 主 loop claim gate（改动小、语义清楚）；② S7 工具描述补路径语义与状态枚举（纯文案）；③ S4 离线定位。S8 与 build_worker 参数陷阱登记为设计裁决项。



## 续跑入口（余额恢复后）

配置/任务已归档且最终档可用：直接 `interactive --config cfg.json < task1.txt`（key 经 `SF_CodingAgentTestKey`）。预期 60-120 轮内产出 Agent 审查报告，随后按既定分工：我复核发现 → Agent 动工（任务模式+完成门）→ 独立终验。S4 建议先行离线定位。
