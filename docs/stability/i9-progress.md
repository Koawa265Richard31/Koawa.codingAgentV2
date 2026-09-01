# I9 实施进度与交接记录

更新日期：2026-09-01。依据：`docs/v2-stabilization-detailed-implementation.md` 第 11 节（I9 合同）。
性质：实施交接记录 + 环境边界清单，不是发布成功证明。

执行分工（维护者 2026-08-31/09-01 指定）：前段由外部 Agent（GPT）实现并提交；2026-09-01 起由
本记录作者接手收尾（复杂决策与审计侧）。排期事实：**I9 在 I8 完成门未闭合（77 点矩阵余量、
reference 三批、24h soak 未取得）时由维护者指定启动**，本文即该决定的正式记录。

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

注：上表 lane 报告的 `commit` 字段为提交前 HEAD（31c4d7d），实际被测代码是**包含 §二修复的
工作树**（报告时点修复尚未提交）；官方候选运行须在收尾提交后带 build/config digest 重跑，
该身份差异不构成完成证据。

manifest verifier fail-closed 演示（`.dsh_tmp/i9-release-demo/`）：用仅含 Windows 单轮的
部分 manifest 连续触发三层稳定拒绝——`approved_skip_manifest_missing`（bundle 布局 root 规则）、
`report_time_invalid`（时间序校验）、`report_identity_missing`（lane 报告未携带 build 身份）。

## 四、未完成 / 环境阻断（I9 发布门 §11.8 在本机不可全满足）

1. **双 OS lane × 连续 3 次**：需 Linux 环境（Windows 单机无法产出 linux 序列）。
2. **24h reference soak ×1**：需 reference 机与 attestation（I8 同源遗留）。
3. **lane 报告统一 build/canonical-config 身份**：官方候选运行须全部 lane 携带
   `--build-artifact-digest` / `--canonical-config-digest` 重跑（本次除 provider 外未带）。
4. **fresh_demo_report 与 migration_reports**：未产出。
5. **P0/P1 清零 + 独立 review 无阻断**：I8 遗留项仍开。
6. 发布门第 3–7 条（canary、迁移证据、资源零遗留的正式核验）未做正式记录。

## 五、与 RT/J 轨道的接口

RT/J v1.1 §8.1 启动前置要求「I9 已完成（证据：I9 记录、审批记录、冻结包 manifest）」。当前
状态为**实现完成 + Windows 侧 lane 证据**，正式 release manifest 未满足（见 §四）。RT/J 核心
切片是否放行由维护者依据本记录判定；J1 离线件不受影响。

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
