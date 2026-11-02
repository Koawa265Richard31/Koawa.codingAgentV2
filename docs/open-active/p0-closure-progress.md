# P0 闭环进度（issue #7 / #8 / #9 / #10）

日期：2026-11-01。基线：`926a9df`。分支：`feat/p0-dependency-deadlock`。

本文件记录每个 issue 的**实际落地范围**与**明确未做的部分**。未做项不写"后续优化"，
一律写清楚为什么没做、现在是什么状态。

## issue #7 — 依赖死锁与验证门

| 子项 | 状态 | 落地位置 | 未做部分 |
| --- | --- | --- | --- |
| A1 确定性分类 | 完成 | `verification/environment_signals.py` | — |
| A2 回执改判 | 完成 | `verification/tools.py`、`release_contracts.py` | — |
| A3 预检（collect-only） | **未做** | — | 需要 profile 级探针定义；当前用 C 的收集数基线覆盖最坏情形 |
| A4 受理工具 | 完成 | `request_dependency_preparation` | — |
| A5 执行器 | **部分** | `PreparationExecutor` 协议 + `UnavailablePreparationExecutor` | 没有真实镜像构建/安装执行器；`PREP_READY` 在未注入执行器时返回 `executor_unavailable` + `human_required` |

行为变化：缺依赖不再报 `completed + failed`，而是
`not_completed + not_evaluated + repair_safe_environment`，并带
`missing_dependencies`（`limited` 模式也可见）。

## issue #7 B/C/D/E

| 子项 | 状态 | 说明 |
| --- | --- | --- |
| B1 测试面默认保护 | 完成 | `_DEFAULT_PROTECTED_PATHS`；`required_test_profiles` 非空且保护集为空 → `verification_requires_protected_tests` |
| B2 测试面并入候选身份 | **未做** | 需要候选身份 schema 变更，单独切片 |
| C 收集数基线 | 完成 | `parse_test_count` + 按 (profile, profile_version) 记录最大值 |
| D1 host 显式 opt-in | 完成 | JSON 加载器要求 `allow_unisolated_execution: true` |
| D2 隔离标注 | 完成 | 回执 `isolation`；`finalize_task` 输出 `evidence_isolation` |
| D3 AppContainer/namespace | **未做** | 工程级，独立 issue |
| E1 声明≠证明 | 完成 | host 证据 + `environment_ref` → 降级为 limited 并标 `environment_unproven` |
| E2 容器参数校验接入装配 | **未做** | 需要装配期拉起容器；现有校验仍在运行后审计路径 |
| E3 `environment_proven` 推导 | 完成（回执面） | 由 E1 的降级推导，不再由配置文本直接置真 |

## issue #8 — 受限 shell

| 项 | 状态 |
| --- | --- |
| 结构化 argv、`shell=False` | 完成 |
| 程序白名单 | 完成（未列即拒，不是黑名单） |
| 工作区路径约束 | 完成（`..` 与工作区外绝对路径拒绝） |
| `disabled` / `container_only` / `trusted_workspace` | 完成 |
| 容器模式（`docker run` 一次性容器） | 完成（无 Docker 时返回 `shell_container_unavailable`） |
| host 模式输出脱敏 | 完成（`redact_text` + 截断） |
| 账本：NON_IDEMPOTENT + MANUAL | 完成（崩溃不自动重执行） |
| 管道（pipeline） | **未做**：参数 schema 只编译扁平数组；两个显式调用替代 |
| 二进制危险参数子集（`find -exec` 等） | **未做**：白名单只到程序级，未到参数级 |

残余风险（必须写在 PR 里）：程序级白名单不等于安全白名单。`find`、`tar`、`git`
都能通过参数间接执行命令，因此**不要把这三个放进 allowlist**，除非另行审计参数面。

## issue #9 — 模型审查门

| 项 | 状态 |
| --- | --- |
| 判定协议、封闭 reason_code | 完成 |
| 失败语义全部 fail-closed | 完成（超时/空/非法 JSON/低置信/provider 异常 → deny） |
| 确定性门在前、模型门在后 | 完成（确定性门失败时不调用模型） |
| 事件只记 reason_code | 完成（不落自由文本） |
| provider 后端 | **未接入**：`CallableShellReviewer` 需要部署注入 judge；未注入时 `required` 模式对每次调用返回 `shell_review_denied`（可见、可诊断，不静默放行） |

## issue #10 — 绕行面审计

- 审计文档：`docs/open-active/logic-bypass-surface-audit.md`
- 已验证：`search_text` 为字面匹配（非正则，无 ReDoS 面）、MCP envelope 标注隔离、父目录逃逸被拒
- **TOCTOU 竞态**：未复现，状态写为"未知"；唯一兜底是容器只读挂载

## 验证

```powershell
$env:PYTHONPATH = "src"
python -m unittest tests.test_dependency_deadlock tests.test_shell_tool tests.test_logic_bypass_audit -v
python -m unittest discover -s tests
```

全量回归记录：`.dsh_tmp/full-regression-p0.txt`（本机临时文件，不入库）。
