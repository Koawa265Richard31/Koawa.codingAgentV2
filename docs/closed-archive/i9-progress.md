# I9 实施进度与交接记录

更新日期：2026-09-01。依据：`docs/v2-stabilization-detailed-implementation.md` 第 11 节（I9 合同）。
性质：实施交接记录 + 环境边界清单，不是发布成功证明。

执行分工（维护者 2026-08-31/09-01 指定）：前段由外部 Agent（GPT）实现并提交；2026-09-01 起由
本记录作者接手收尾（复杂决策与审计侧）。排期事实：**I9 在 I8 完成门未闭合（77 点矩阵余量、
reference 三批、24h soak 未取得）时由维护者指定启动**，本文即该决定的正式记录。

## 零、维护者决策：I9 内部工程闭环（2026-09-01）

### 0.1 决策背景与适用边界

维护者于 2026-09-01 决定：本项目当前不计划上线，因此 I9 不再以完整的生产发布资格认证
作为完成条件。I9 改按“内部工程闭环”收尾：保留能够直接降低长任务 Coding Agent 工程风险的
验证，去除主要服务于上线审计、SLA 与发布合规的仪式性要求。

本决定不否认或改写原 I9 合同及既有发布门事实。原生产发布门仍作为历史基线保留在本文
§四；未满足其中被豁免的项目，不再阻断内部工程闭环，但仍意味着不得宣称已取得生产发布资格。

### 0.2 内部工程闭环的必做项

以下各项均须在最终候选代码上完成并留下可核对记录，方可结束 I9：

1. **Windows 全量回归一次**：从 `v2/` 按 AGENTS.md 运行完整 unittest 集，确认最终候选没有
   已知回归；环境型 skip 必须可解释，不得把强制场景静默降级为 skip。
2. **真实 Linux lane 一次**：在本机 `Ubuntu` WSL2 的 Linux 原生 ext4 文件系统中，以最终
   候选快照执行一次有意义的 Linux 序列。不得直接在 `/mnt/d` 的 v9fs 挂载目录中运行，也不得
   以独立的 `docker-desktop` 发行版或仅有 Linux 容器外壳的 Windows 执行冒充 Linux host。
   该 lane 应覆盖 Linux 特有的路径、权限、信号/进程回收、文件锁、Git worktree 与资源清理
   行为；不要求机械重复三轮。
3. **最终候选身份绑定的真实 provider lane 一次**：报告必须绑定最终 commit；如门禁使用构建
   制品或 canonical config，则同时记录对应 digest。使用真实供应商验证正常响应、多轮、空
   completion/重试、超时取消及清理，不再要求为同一候选反复付费执行。
4. **资源零遗留与 release audit**：核验任务结束后无遗留非 daemon 线程、子进程、未回收句柄、
   容器/worktree 或 active durable 状态；运行 release audit 并保存成功或可解释的失败记录。
5. **凭据与环境安全 canary**：扫描持久化数据、WAL/数据库、事件、报告与构建产物，确认 API Key、
   canary secret、credential 字段、完整环境变量及隐藏推理未落盘。此项是安全门，不属于下文被
   豁免的“上线 canary”。
6. **仓库外 fresh install smoke**：构建 wheel，在仓库外临时目录创建全新 venv 并安装该 wheel，
   执行一次最小启动与恢复路径，证明 console script、包内资源和运行时 import 不依赖仓库根目录。
   不要求另备 fresh machine。
7. **迁移自动化测试**：现有 schema/data migration 自动化测试继续保留并必须通过；只豁免用于
   正式发布签署的 migration 报告包。
8. **最终独立审计式评审**：由未直接实施该变更的 Agent/评审者检查恢复、并发、外部副作用账本、
   资源与安全边界，确认没有已知 P0/P1 阻断。无需组织级审批或发布签字。
9. **文档一致性**：最终证据、已知限制和完成口径须回写本文，确保后续 Agent 不会把内部工程
   闭环误报为生产发布认证。

### 0.3 明确豁免的生产发布认证项

以下项目对当前不计划上线的项目投入产出过低，经维护者明确豁免，不再阻断 I9 内部工程闭环：

- 24h reference soak；
- Windows/Linux 双 OS 各连续三轮；
- reference 性能/稳定性 attestation；
- 正式 `current-next`、`legacy-export` 等 migration 发布报告；
- 严格 release manifest、冻结包签署及七天/十四天证据新鲜度窗口；
- 组织级独立发布审批；
- 面向真实上线流量的 canary/灰度流程。

豁免不等于删除相应实现或测试：迁移自动化测试仍须保留，凭据/环境安全 canary 仍是必做安全门；
24h soak 与正式发布材料也可在未来项目改变为上线目标时重新启用。

### 0.4 完成声明约束

全部 §0.2 项完成后，I9 的唯一准确完成声明为：

> **I9 内部工程闭环完成，适合继续开发和长任务验证；未执行生产发布资格认证。**

在完整生产发布门未恢复并通过前，不得使用“生产就绪”“正式发布完成”或等价表述。

### 0.5 内部工程闭环最终证据（2026-09-01）

以下记录绑定最终代码候选 `dd1c8863df3605d4e52c5ee7295ed090cb72e501`，用于判定 §0.2
内部工程闭环；证据文件位于本机 TEMP/WSL 工作目录，不是生产发布包：

- Windows Python 3.14.3 全量回归：898 discovered / 0 fail / 0 error / 22 skip，耗时
  1362.851s；无 `ResourceWarning`。
- Linux Ubuntu WSL2 原生 ext4：`pr-fast` 为 898 discovered / 875 pass / 0 fail /
  0 error / 23 skip，耗时 242.083s；commit `dd1c886`；报告 digest
  `53af141974bebfcfa26e0448d297ed53c4f30bb78f896e94ac3ccb764f6a49f6`；路径
  `/home/koawa265/koawa-i9-dd1c8863-run1/repo/.dsh_tmp/i9-lanes/pr-fast-linux-dd1c8863.json`。
  作为父候选 `67b610b` 的交叉记录，integration 为 29 pass / 0 fail / 0 error /
  1 Docker skip，MCP 为 114 pass 全绿，pr-fast 为 873 pass / 0 fail / 0 error /
  23 skip。
- Linux 实测发现并修复了 POSIX env 大小写、`sys.executable`、symlink cleanup、
  worktree metadata fail-closed，以及 MCP notification storm 导致 response starvation、
  gate traceback 诊断问题；这些修复已包含在最终候选中。
- 仓库外 fresh install smoke：fresh archive digest
  `4f9d705bab1cbe24fe3bf6e7b6847a45c4e296c59bb21f9074ea76fd6f94f9a2`，wheel digest
  `b9aae22139dc38ea1ceae996a7df5ad798f505ccf9c784234201a443963a6f8b`；离线安装后执行
  `CLI run-status-resume-status` 与副作用核验一次通过。证据路径
  `C:\Users\qaz14\AppData\Local\Temp\koawa-i9-dd1-d77985db0f7744daa058f65c29759b55\fresh-install-evidence.json`，
  文件 hash `cd6a5ed96483d586f9f57ef23f2a71703ee751bf1ca4b9cdf27e0cc375d0e6c6`。
- release-audit active 全部为 0；`credential_literals_present=false`；实际 key 与合成
  canary 命中均为 0。期间修复了 `task-repo` 中 `sk-` 字样造成的假阳性。审计证据路径
  `C:\Users\qaz14\AppData\Local\Temp\koawa-i9-dd1-d77985db0f7744daa058f65c29759b55\release-audit-canary-evidence.json`，
  文件 hash `026c9fa3d4212e9c8e12dfd0ee1cd1a49333b02075328ab82fe5e36719b06233`。
- 真实 SiliconFlow provider（精确模型 `Qwen/Qwen3.5-35B-A3B`）：四场景 10/10 PASS；
  cleanup zero delta；报告路径
  `C:\Users\qaz14\AppData\Local\Temp\i9-provider-df3e61ac28e34cf89134a2abb4140ac1.json`，
  digest `91ae0e8d7e058c85ab9050cc10c9c2ad3822d805148949cefcafce872e04c662`；safe config
  digest `19c6d7b01049c71dd94082273ffeaea11b5b42674d80cfdf6ba43382c615eb3c`。
- Sol 审计结论：恢复、并发、ledger、resource、security 差异均已审查，无已知 P0/P1
  阻断。
- Docker 边界：Ubuntu 未运行 Docker daemon，Linux integration 的唯一 skip 属环境事实；
  Docker/golden 已有 `da3983f` Windows/Docker Desktop 实测证据，内部工程闭环不要求在
  Ubuntu 重跑 Docker daemon。

据此，§0.2 必做项已完成；24h reference soak、双 OS 三连、正式发布材料等仍按 §0.3
明确豁免。唯一准确完成声明仍为：

> **I9 内部工程闭环完成，适合继续开发和长任务验证；未执行生产发布资格认证。**

## 一、GPT 前段（提交 f3c652b + 31c4d7d，约 4900 行）

- `scripts/stability_gate.py`（lane runner，341 行）：七条 lane、逐 exact test id 记录、
  ResourceWarning→error、原子报告 + digest 自排除、强制 lane skip 即失败、provider 证据装载。
- `scripts/release_manifest.py`（408 行）：fail-closed 聚合 verifier（series ordinal、身份绑定、
  时间窗、skip 替代 PASS、soak 恰一次、provider 必需）。
- `scripts/stability_reference.py`（151 行）、`scripts/release_audit.py`（292 行，后被移入包内）。
- golden composite 扩展（+263）：`REQUIRED_MATRIX` 与合同 §11.3 的 13 场景一一映射
  （composite 内场景 + 精确外部测试锚点混合）。
- dispatch contract（423 行，三类入口）、release controls（210）、manifest e2e（598）、
  provider evidence（578+）、D11/MCP fault fixtures（476，补 I8 矩阵）。
- 遗留一个未提交的 provider 测试修改（reasoning_effort=off 显式化 + 断言测试），完整自洽，
  接手后验证通过并保留。

## 二、接手阶段修复与补齐（全部带证据）

1. **release-audit 安装环境缺陷修复**：`from scripts.release_audit import` 在 console script
   （仓库外）实测 `ModuleNotFoundError`（证据：临时目录复现 traceback）。修复：模块移入
   `src/koawa_agent_v2/runtime/release_audit.py`（git mv），CLI 与测试 import 同步更新，
   新增回归测试 `test_release_audit_console_script_imports_outside_repo_root`
   （temp cwd 子进程，断言稳定错误而非 traceback）。修复后仓库外实测返回
   `{"code": "release_audit_failed", "ok": false}`。
2. **mcp lane 补 D9**（合同 §11.2「D9/D10」）：LANES.mcp 加入
   tests.test_d9_approval / test_d9_integration / test_d9_policy。
3. **`docs/stability-approved-skips.json` 建立**（零条目 schema v1，generated_for_commit 绑定
   当时 HEAD，digest 与 verifier canonicalization 自检通过）。注意：候选 commit 变化时须按
   同一 canonical 规则重生成。
4. **provider 场景收尾（三处，各自带实测根因）**：
   - 冷进程预热：首个 HTTPS 请求一次性分配 ~16 个运行时句柄（实测 0→16→稳定 151），
     字母序首场景会被记账；新增 `_warm_up_process_once`（一次最小请求，失败不影响场景判定）。
   - `gc.collect()` 后测量：异常 traceback 经引用环钉住生成器帧，句柄释放依赖 GC 时点。
   - 有界安定重采样（`_settled_after_snapshot`，≤4 次 × 50ms）：Windows 上 close() 返回后
     句柄记账存在异步销毁窗口（实测同条件三连跑 败/过/过）；仅句柄计数可重采样，
     线程/子进程为精确 Python 事实绝不重采样，真泄漏跨窗口持续仍失败。
   - 稳定性证据：修复后 provider lane 连续 3 次全绿（provider-win-8/9/10）。
   - 另修正运行环境：KOAWA_I9_PROVIDER_NAME 须为 `siliconflow`（`openai_compatible` 不在
     reasoning_family 表内，fail-closed 报 reasoning_effort_unsupported）。
5. **wheel 构建**：`--no-build-isolation`（默认索引镜像在本环境不可用，须
   `-i https://pypi.org/simple`；本机默认 `python` 为 3.11，I9 代码含 PEP 701 语法须
   py -3.12+，与 AGENTS.md 一致）。

## 三、Windows 侧 lane 证据（2026-09-01，Docker daemon 29.6.1 在线）

本节保留收尾前的逐 lane 历史记录；最终候选的统一结果与闭环判定见 §0.5。

| lane | 报告 | 结果 |
|---|---|---|
| docker | `.dsh_tmp/i9-lanes/docker-win-1.json` | ok（零 skip） |
| golden | `.dsh_tmp/i9-lanes/golden-win-1.json` | ok（零 skip） |
| mcp（含 D9） | `.dsh_tmp/i9-lanes/mcp-win-1.json` | ok（零 skip） |
| integration | `.dsh_tmp/i9-lanes/integration-win-1.json` | ok（零 skip） |
| soak | `.dsh_tmp/i9-lanes/soak-win-1.json` | ok（零 skip） |
| provider-opt-in | `.dsh_tmp/i9-lanes/provider-win-10.json`（8/9/10 三连绿） | ok（真实 SiliconFlow + Qwen3.5-35B-A3B，build digest 绑定） |
| pr-fast | `.dsh_tmp/i9-lanes/pr-fast-win-1.json` | 见下方全量记录 |

release-audit：全新 durable 库正向报告 `ok:true`、active 全零
（`.dsh_tmp/i9-lanes/release-audit-demo.json`）；错误路径稳定码取证。

注：上表早期 lane 报告的 `commit` 字段为提交前 HEAD（31c4d7d），实际被测代码是包含 §二
修复的工作树；该历史身份差异已由 §0.5 的最终候选记录覆盖，不得单独作为正式发布证据。

manifest verifier fail-closed 演示（`.dsh_tmp/i9-release-demo/`）：用仅含 Windows 单轮的
部分 manifest 连续触发三层稳定拒绝——`approved_skip_manifest_missing`（bundle 布局 root 规则）、
`report_time_invalid`（时间序校验）、`report_identity_missing`（lane 报告未携带 build 身份）。

## 四、原生产发布门未闭合事实（保留作历史基线）

以下是 I9 原发布门 §11.8 的状态。它们继续用于说明“未执行生产发布资格认证”的边界；其中
已在 §0.3 明确豁免的项目，不再阻断 §0.2 的内部工程闭环。

1. **双 OS lane × 连续 3 次**：未按原合同完成；按 §0.3 豁免，已以一次真实 WSL2/ext4
   Linux lane 完成内部工程验证。
2. **24h reference soak ×1**：未执行；按 §0.3 豁免。
3. **lane 报告统一 build/canonical-config 身份**：内部闭环已由 §0.5 最终候选、provider
   报告及 safe config digest 绑定；未形成原合同要求的全套发布 manifest。
4. **fresh_demo_report 与 migration_reports**：未形成正式发布报告包；fresh install smoke
   已按 §0.5 完成，迁移自动化测试仍属于必做项。
5. **P0/P1 清零 + 独立 review 无阻断**：已完成内部审计式评审，无已知 P0/P1；不等同于
   组织级发布审批。
6. 发布门第 3–7 条的正式发布材料、冻结包和上线 canary：未执行；凭据/环境安全 canary、
   release-audit 与资源零遗留已按 §0.5 完成。

按 §零的新口径，不再补齐第 1 项的“三轮”数量、第 2 项、正式 migration reports 或严格
release manifest；§0.5 记录的内部工程闭环证据已满足当前收尾口径。

## 五、与 RT/J 轨道的接口

RT/J v1.1 §8.1 启动前置要求「I9 已完成（证据：I9 记录、审批记录、冻结包 manifest）」。当前
状态为**I9 内部工程闭环完成**，但原生产 release manifest/审批前置未满足（见 §四），因此
不得宣称生产就绪或原合同意义上的 I9 发布完成。RT/J 核心切片是否放行由维护者依据本记录
判定；J1 离线件不受影响。RT/J 若引用 I9，只能引用唯一完成声明及其限定边界，不得将内部
工程闭环等同于生产发布资格认证。

## 六、复现命令（PowerShell，v2/）

```powershell
py -3.14 -B scripts/stability_gate.py --lane docker --report .dsh_tmp/i9-lanes/docker-win-1.json
# 其余 lane 同理替换 --lane
$env:SF_CodingAgentTestKey = [Environment]::GetEnvironmentVariable('SF_CodingAgentTestKey','User')
$env:KOAWA_I9_PROVIDER_BASE_URL='https://api.siliconflow.cn/v1'
$env:KOAWA_I9_PROVIDER_MODEL='Qwen/Qwen3.5-35B-A3B'
$env:KOAWA_I9_PROVIDER_API_KEY_ENV='SF_CodingAgentTestKey'
$env:KOAWA_I9_PROVIDER_NAME='siliconflow'
$env:KOAWA_I9_PROVIDER_TIMEOUT_PROBE_SECONDS='0.1'
py -3.14 -B scripts/stability_gate.py --lane provider-opt-in --build-artifact-digest <digest> --report .dsh_tmp/i9-lanes/provider-win-N.json
py -3.14 -B scripts/release_manifest.py verify --manifest <bundle>/manifest.json
```
