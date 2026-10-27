# 真实 issue 实验记录（2026-10-04 起）

方法：从真实仓库取一个**修复提交**，回到其父提交，只应用该提交里的
**测试改动**（`git checkout <fix>` + `git checkout <fix>^ -- <source>`
），形成“指定测试失败、其余通过”的夹具；任务提示用 issue 摘要（不点名
文件与行号）。夹具仓库全部在 `D:/A_Dev_Projects/koawa-real-issues/`，
**不入本仓库**；本仓库只记录结论与度量。

策略门（不是文本约定）：`policy.protected_paths = ["tests/", "conftest"]`
使补丁工具在工具边界直接拒绝改动测试——剥夺作弊机会，而不是事后审计 diff。

验收四条：FAIL_TO_PASS 全绿；PASS_TO_PASS 无回归（我方独立复跑）；
agent 的 diff 未触碰 `tests/`；D5 门诚实 finalize。

---

## Issue 1：more-itertools `one()`/`only()` 丢弃 falsy 自定义异常

- 上游修复提交：`def2dab`（"Fix one()/only() dropping a falsy user-supplied
  exception"）。夹具分支 `fixture-issue-1147-fixtarget`。
- 基线：`tests/test_more.py` **4 failed / 604 passed**（4 个目标测试失败）。
- 任务提示：只给 issue 摘要（`raise too_long or ValueError(msg)` 丢弃
  falsy 异常实例），不给文件、不给修法。

### 结果：✅ 解决

| 观测 | 值 |
| --- | --- |
| 终态 | `completed`（finalize 通过，evidence digest 有） |
| 我方独立复跑 | **608 passed / 0 failed**（含原 PASS_TO_PASS 604） |
| agent diff | 仅 `more_itertools/more.py`（+9/−3 语义等价实现） |
| 是否触碰 tests/ | **否**（`git diff --stat -- tests/` 为空） |
| 轮次 | 33 |
| tokens | 输入 628,385（缓存命中 604,413 = **96.2%**）/ 输出 6,321 |
| 工具分布 | read_file 12、search_text 9、run_test_profile 8、apply_patch 6、
  update_plan 3、finalize_task 2、repo_map/list_files/git_diff/git_status 各 1、
  **search_result_projections 1 + read_result_projection 1**（N1 读取链在
  真实任务中被实际调用） |

agent 的修法与上游修复语义等价（`is not None` 分支，且在构造默认消息前
抛出自定义异常，因此 `test_too_long_does_not_evaluate_repr` 也通过）。

### 本实验暴露并修复的 3 个真实产品缺陷

1. **detailed 档拿不到失败断言**（阻断级）：detailed 回执只有
   `exit_code`/`outcome`，模型看不到哪条断言失败 → 前两轮跑出现象：
   第 17–24 轮反复重测仍不知所措（每轮 in-token 只涨 ~75）。修复：
   detailed（且 audience+environment 双证明、effective 未降级）时附带
   **有界失败摘录**（断言行 + 首个 traceback 头，≤10 行×200 字符），
   limited 及以下仍无摘录。测试：`test_release_contracts`
   （detailed 有摘录 / limited 必无）。
2. **基线脏判定用原始 status，抵消 agent 真实改动**：幻影 “ M”
   （行尾/stat 缓存）被算作“基线已有”，补丁后 `agent_changed_paths`
   为空 → finalize 报 `no_agent_changes`（即使测试已绿）。修复：
   基线剔除改用与保护路径同一套**内容核验**结果（F2 parity）。
3. **隔离 git 配置缺 autocrlf 归一化**：GitFacade 的 hermetic 环境无全局
   配置 → `core.autocrlf` 默认 false；Windows 上 LF 仓库的 CRLF 工作树
   因此**全文件显示为已改**，`git_diff` 单条返回 262,144 字符（等于
   max_tool_result_chars 上限，超过整个 176k 字符上下文硬预算）→
   一轮即 `context_capacity_exhausted`。修复：hermetic 调用加
   `-c core.autocrlf=true`，diff/hash 比较内容而非行尾约定；夹具基线
   由 1 条幻影脏变为 **0**。

### 记录：模型报告但未能复现的问题

模型自述 `read_file` **把连续空行压缩成占位行**并导致行号错位。实测核验
（`l1



l5` 与 CRLF 文件）：`read_file` **完整保留空行**，返回
`start_line`/`end_line`/`total_lines`；以读出内容构造的补丁在 CRLF 文件上
也能正确应用并保留行尾。**未复现，判为模型自身的读取/对位失误**，不计入
产品缺陷。其两次 `patch_context_mismatch` 属同类（自造 old_lines 与文件
不符）。

### 第 4 个真实缺陷：测试运行预算不可配置

第二例中模型修好代码、604/604 全绿，却因 `test_run_budget_exceeded`
（1 个 required profile 时预算仅 4 次）拿不到“最终代际”的测试证据，
`finalize_task` 永久 `required_test_profile_stale`。修复：新增配置项
`verification_max_test_runs`（1..64，缺省沿用 `min(32, max(4, 4n))`
推导），真实难题可显式放宽而不静默提高默认。

---

## Issue 2：more-itertools `seekable` maxlen=0 丢项

- 上游修复提交：`6b1907d`（"seekable.peek/bool no longer drop items when
  maxlen is 0"）。夹具：`koawa-real-issues/mt2`，分支 `fixture-seekable`。
- 基线：`tests/test_more.py` **2 failed / 602 passed**。
- 任务提示：只给 issue 摘要（maxlen=0 时 peek/__bool__ 丢项）。

### 结果：❌ 未解决（三次尝试，如实记录）

| 尝试 | 配置 | 终态 | 测试结果 |
| --- | --- | --- | --- |
| A | 40 轮 / 测试预算推导值 | `max_model_rounds_exceeded` | **3 failed**——修法引入回归（原 602 通过中的 `test_simple_peeking` 变红） |
| B | 70 轮 / 测试预算 4 | `verification_required` | **604 passed 全绿**，但 `test_run_budget_exceeded` 耗尽 → 无最终代际证据 → finalize 永久 `required_test_profile_stale` |
| C | 70 轮 / 测试预算 20 | `verification_required` | 回到 **2 failed**（基线未修好），`required_test_profile_not_passing` 诚实拒绝 |

结论：该 issue 对当前 deepseek-chat + 本配置**难度足够**——出现“改错引入
回归”“修对但证据链断裂”“彻底没修对”三种真实失败模式，均被 D5 门如实
拦截（没有任何一次谎报完成）。B 的测试预算缺陷已修（可配置化）。

## 待跑队列（依次进行）

| # | 仓库 | 目标 | 状态 |
| --- | --- | --- | --- |
| 1 | more-itertools | `def2dab` falsy 异常 | ✅ 已解决（608/608） |
| 2 | more-itertools | `6b1907d` seekable maxlen=0 | ❌ 未解决（3 次尝试，暴露测试预算缺陷） |
| 3 | pypa/packaging | specifier/version 语义类 fix | 待跑 |
| 4 | pallets/click | OptionParser/帮助格式化类 fix（T2 难度） | 待跑 |

每跑一个：记录基线失败数、终态、我方复跑结果、是否触碰 tests、轮次与
tokens、以及新发现的产品缺陷（发现的缺陷按“策略门/机制 vs 接线”分类
修复并补测试，再纳入全量回归）。
