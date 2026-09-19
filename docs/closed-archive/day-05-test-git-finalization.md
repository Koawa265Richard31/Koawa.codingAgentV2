# D5：测试、Git Diff 与证据化完成

D5 把 D1–D4 已有能力串成第一个真正可验收的 Coding Agent 纵向闭环：

```text
用户任务
  -> 模型 read/search
  -> apply_patch
  -> 固定测试 profile
  -> git_status
  -> git_diff
  -> finalize_task
  -> AgentLoop 才允许最终文本完成 Turn
```

重点不是多了四个工具，而是“完成”第一次成为系统验证的状态，而不再是模型的一句
自然语言声明。

## 1. 为什么 D5 不是通用 Shell

`run_test_profile` 只接收一个 `profile_id`。真实 argv、cwd、环境变量、超时和输出上限
由宿主在启动时用 `CommandProfile` 固定。模型不能传：

- 命令字符串；
- argv；
- cwd；
- 环境变量；
- shell 管道、重定向或命令拼接。

底层始终 `shell=False`。stdout/stderr 由两个 drain 线程并行读取，只保留配置上限内
的 bytes，但会继续排空管道，避免子进程因 pipe 写满而死锁。超时或
`ToolExecutionContext.progress_guard` 检测到取消/失权时，Windows 用固定
`taskkill /T /F`，POSIX 用独立 process group 终止整棵进程树。

然而，固定 `python -m unittest`、pytest、npm 或 Gradle 依然会执行仓库代码。因此
D8 容器沙箱完成前，`TrustedCommandRunner` 默认是 `UNTRUSTED` 并拒绝执行。调用方
必须明确选择：

- `BUILTIN_FIXTURE`：项目自己的演示/测试仓库；
- `USER_CONFIRMED`：用户明确确认可信的仓库。

这是一条重要面试边界：**固定 argv 消除了模型命令注入，但没有隔离仓库代码。**

## 2. 生产文件与职责

### `verification/runner.py`

- `CommandProfile`：不可变的受信测试配置；启动时拒绝相对 executable、危险环境名、
  secret-like argv 和无界预算。
- `TrustedCommandRunner.validate_profile()`：在占用测试预算前执行 trust/profile gate。
- `TrustedCommandRunner.run()`：profile_id 到固定进程调用的唯一入口。
- `run_bounded_process()`：固定 cwd、最小环境、并行排空、有界输出、总超时、进程树终止。
- `CommandResult`：保存 outcome、exit code、脱敏 argv、timeout、输出字节数与截断事实。

普通测试失败不是 runtime 异常，而是 `CommandOutcome.FAILED`，会回填模型，让它在
预算内继续定位和修复。启动失败、超时也有独立 outcome。

### `verification/git.py`

`GitFacade` 在构造时记录任务开始前的 dirty baseline。后续：

- `status()` 返回排序后的 porcelain snapshot 与 canonical SHA-256；
- `agent_changed_paths()` 从当前 status 中排除 baseline dirty paths；
- `diff()` 只对 Agent 新变化生成 bounded diff，并为 untracked UTF-8 文件补统一 diff；
- `baseline_unchanged()` 用初始文件指纹证明用户原有 dirty 文件没有被改动。

Git 命令完全固定，并显式关闭：

- hooks path；
- fsmonitor/untracked cache；
- pager/color；
- external diff；
- textconv；
- interactive diff filter；
- system/global Git config；
- terminal prompt 和 submodule recursion。

`git diff` 额外使用 `--no-ext-diff --no-textconv`。测试 fixture 会配置恶意
`diff.external`、`.gitattributes` 和 textconv，断言 marker 从未执行。

### `verification/finalization.py`

`VerificationLedger` 是 D5 的进程内证据账本。它不是 D7 的 durable Tool Ledger。

每个 `run_id` 有一个 `generation`：

1. `apply_patch` 成功后 generation + 1；
2. 测试记录自己验证的是哪个 generation；
3. status/diff 同样记录 generation；
4. 新 Patch 会使旧测试、旧 Git 证据和旧 final report 全部失效。

`finalize()` 必须同时证明：

- 当前 generation 有测试，并且最后一次测试通过；
- status 与 diff 都来自当前 generation；
- status/diff digest 一致；
- diff 完整，没有被截断；
- 至少有一次成功 Patch；
- 所有当前变化都来自本 Run 的成功 Patch，而不是测试或旁路进程偷偷产生；
- baseline dirty 文件指纹未改变。

通过后生成确定性 JSON 报告，包含 changed paths、baseline dirty paths、patch digest、
diff digest、测试 argv/exit/timeout/截断信息与 D8 前的 host-runner 风险提示。

### `verification/tools.py`

它组装八个模型可见工具：

```text
read_file / list_files / search_text
apply_patch
run_test_profile
git_status / git_diff
finalize_task
```

`build_verified_coding_tool_registry()` 会按以下顺序启动：

1. 绑定安全 `WorkspacePathResolver`；
2. 用 `GitFacade` 捕获 dirty baseline；
3. 创建 `VerificationLedger` 和受信 runner；
4. 注册 D3 只读工具；
5. 注册带 baseline 路径保护和 Patch observer 的 D4 工具；
6. 注册 D5 四个验证工具；
7. 第一次给模型 definitions 时由 Registry seal。

`CodingToolRegistry` 同时实现 AgentLoop 的 `CompletionGate`。

### `execution/loop.py` 与 `editing/tools.py` 的 D5 接缝

- `ToolExecutionContext.progress_guard` 让长命令和 Patch 事务内部持续检查取消与 D1
  Run ownership，而不是只在进入工具前检查一次。
- `AgentLoop` 收到 `STOP` 后先调用 `CompletionGate.assert_complete(run_id)`；没有有效
  report 时抛 `verification_required`，Turn 不能伪装 COMPLETED。
- `register_patch_tool()` 允许 D5 提供 protected baseline paths 与成功 observer；D4
  单独使用时行为保持不变。

## 3. 一个具体例子

初始仓库：

```python
def add(left, right):
    return left - right
```

测试要求 `add(2, 3) == 5`。

完整执行：

1. 模型调用 `run_test_profile({"profile_id":"unit"})`，拿到 `outcome=failed`；
2. 调用 `read_file`，取得源码和 base SHA-256；
3. 调用结构化 `apply_patch`，把减法精确改为加法；generation 从 0 变为 1；
4. 再跑同一个固定 profile，得到 generation 1 的 `passed`；
5. `git_status` 证明只有 `app.py` 是 Agent 新变化；
6. `git_diff` 返回真实 diff 和 digest；
7. `finalize_task` 汇总测试与 diff 证据；
8. 下一轮模型返回最终文本；CompletionGate 再读取当前 Git status，确认报告后没有
   新变化，D1 `TurnWorker` 才提交 COMPLETED。

如果第 3 步后模型直接说“完成了”，CompletionGate 会拒绝；如果修改后没有重跑测试，
得到 `tests_stale_after_patch`；如果测试始终失败并耗尽次数，得到
`test_run_budget_exceeded`，不能伪成功。

## 4. Dirty baseline 为什么要单独处理

假设用户开始任务前已经手改了 `settings.py`，尚未 commit。D5 构建 Registry 时会把
它记录进 baseline：

- `apply_patch` 若尝试写这个路径，直接返回 `baseline_dirty_path_forbidden`；
- status 报告把它列在 `baseline_dirty_paths`，不列入 `agent_changed_paths`；
- Finalizer 重新计算其指纹，测试脚本或旁路进程如果改了它，最终失败。

这避免 Agent 把用户自己的未提交工作算成“我的修复”，也避免覆盖后再偷偷恢复。

## 5. 失败语义

| 场景 | 结果 |
|---|---|
| 未确认信任的仓库运行测试 | `repository_not_trusted_for_host_execution` |
| 模型提供未知 profile | `unknown_command_profile` |
| 测试非零退出 | typed `failed`，允许模型修复 |
| 测试超时 | typed `timed_out`，终止进程树 |
| 输出过大 | 内容截断，保留总 bytes、exit、truncated 标记 |
| 测试次数耗尽 | `test_run_budget_exceeded` |
| 写用户 dirty 文件 | `baseline_dirty_path_forbidden` |
| Git 缺失/非仓库/超时/输出过大 | 稳定 Git error，fail closed |
| diff 来自测试生成物而非 Patch | `unattributed_workspace_changes` |
| 修改后没重跑测试 | `tests_stale_after_patch` |
| status/diff 陈旧或不一致 | `git_evidence_inconsistent` / `verification_evidence_stale` |
| 没调用 finalize 就返回 final | `verification_required` |

## 6. 运行

```powershell
Set-Location D:\KoawaAgent\v2
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = "src"

python -W error::ResourceWarning -B -m unittest tests.test_d5_vertical_slice -v
python -W error::ResourceWarning -B examples/day05_coding_vertical_slice.py
python -W error::ResourceWarning -B -m unittest discover -s tests -v
```

## 7. D5 之后仍缺什么

- D5 的测试/Git/最终证据仍在进程内；进程崩溃后不能从中间轮恢复。D6 会把 canonical
  ModelTurn、ToolResult、phase 与 checkpoint 持久化，并接管僵死 Run。
- Patch 和测试副作用还没有 durable claim。guard 与真正副作用之间仍有 TOCTOU；D7
  才加入 active-run fence + Tool Ledger 和 `OUTCOME_UNKNOWN`。
- D5 HostRunner 不是恶意仓库沙箱。D8 会把测试迁进真正的 Docker backend，并默认
  `network=none`。
- D5 不 commit、push、install dependency，也不开放任意 Shell。

面试时最准确的一句话是：

> D5 做出了第一个可验证的 Coding 闭环：模型只能选固定测试 profile，所有修改都和
> 当前 generation 的测试、status、diff 绑定，完成还要过 Finalizer；但宿主执行只对
> 明确信任仓库开放，崩溃恢复、副作用 ledger 和真容器隔离分别留给 D6–D8。
