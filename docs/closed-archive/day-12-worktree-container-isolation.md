# D12：Per-Agent Worktree + Container Isolation

## 1. 完成边界

为每个写 Agent 提供独立 Git worktree 与 durable workspace inventory；父级先集成
候选 artifact（integration worktree + retest），再以当前 HEAD hash 门控投递用户
工作区。`DockerContainerRunner` 已接入 D8 的真实 `DockerCommandRunner`
（`run_profile_object`），每个 worktree 在只读挂载的 Linux 容器内重测；
`InjectedContainerRunner` 仅保留给确定性/无 Docker 的测试环境。

已实现：

- `AgentWorkspaceStore`：per-agent workspace 事件流（allocated/reaped），
  只允许 `managed_root` 下的精确路径，reaper 不做广泛递归删除；
- `WorktreeManager`：宿主执行 `git worktree add --detach`；写 Agent 默认拒绝
  dirty 用户工作区（`dirty_user_worktree_requires_snapshot`），read-only 可用；
- `ArtifactIntegrator`：artifact 绑定 `(agent_id, run_id, base, head, diff,
  evidence, image_digest)`；接受时校验 run fence/base/evidence；串行 apply 到
  integration worktree，冲突显式 `artifact_conflict`；retest 通过后按
  `user_base_commit == 当前 HEAD` 门控投递；
- 兄弟目录不可写：每个 agent 只见自己的 worktree；取消后旧 run 的 artifact 被
  `artifact_run_fenced` 拒绝。

## 2. 稳定错误码

| 错误码 | 含义 |
|---|---|
| `workspace_outside_managed_root` | worktree 路径逃逸管理目录 |
| `invalid_workspace_identity` | base/branch 非法 |
| `dirty_user_worktree_requires_snapshot` | 写 Agent 遇到 dirty 用户基线 |
| `git_worktree_failed` / `git_apply_failed` / `git_integration_failed` | 宿主 Git 失败 |
| `artifact_run_fenced` / `artifact_base_mismatch` / `artifact_empty` / `artifact_missing_evidence` | artifact 验收失败 |
| `artifact_conflict` / `artifact_retest_failed` | 集成冲突或重测失败 |
| `user_workspace_drift` | 投递时用户 HEAD 已变化 |
| `stale_workspace_fenced` | 旧 run 不能 reap |
| `docker_unavailable` / `container_run_failed` | daemon/镜像不可用或容器运行失败 |

## 3. 测试与示例

- `tests/test_d12_workspace_integration.py`（6）：分配/安全 reap、dirty 基线拒绝、
  两 Agent 不同文件集成+投递、同行冲突、stale run artifact fence、投递 HEAD 门控。
- `examples/day12_isolated_writing_agents.py`：两 Agent 独立 worktree 修改不同
  文件 → 集成重测 → 投递；再演示同行冲突。

聚焦命令：

~~~powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = 'src'
python -W error::ResourceWarning -B -m unittest tests.test_d12_workspace_integration -v
~~~

## 4. 推迟到后续日的风险

- 真实模型 provider 与 D15 golden composite E2E 组合。
- dirty 用户基线的版本化快照支持（当前默认拒绝写 Agent）。
- artifact 自动 merge 策略（当前冲突显式交给父级）。
