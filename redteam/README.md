# redteam/ — RT/J 轨道隔离树

依据：`docs/agent-redteam-jailbreak-plan.md` v1.1 §4（隔离）与 §8.1（启动前置）。
本树是 RT/J 轨道唯一的红队工作区；I9 完成前，本树只允许 §8.1 列出的准备项：
corpus / license / 依赖 lock / 隔离 spike。**禁止改 src/、tests/、schema、policy，
禁止运行正式 production-equivalent campaign。**

## 隔离合同（违反任何一条即 spike 失败）

1. `src/koawa_agent_v2/` 与 `tests/` 不得 import 本树或 pyrit 的任何内容。
2. 生产依赖保持纯标准库：`pyproject.toml` 不得出现 pyrit；clean production venv
   不能 import pyrit。
3. 本树自有 venv（`redteam/.venv`，gitignore），红队依赖只装在这里。
4. 本树一切持久化产物只允许 JSON + 文本报告；不落真实秘密、原始密钥。
5. 报告目录（`redteam/reports/`）不得暴露给被测目标，且不提交大文件。

## 目录

- `corpus/` — 攻击语料（纯数据）：manifest（来源 URL、immutable revision/path/hash、
  SPDX/license 证据、修改与审核记录）+ 数据文件。许可未核验前不入库；无许可 DAN 项目排除。
- `lock/` — 依赖锁（pyrit==1.0.1 + 传递依赖版本/哈希）与 SBOM/license 取证。
- `spike/` — 隔离验证脚本与其 JSON 结果证据。
- `reports/` — 未来 RT-1 运行产物（gitignore，运行时创建）。

## 竞态边界（2026-08-31 起生效）

I9 由另一实现 Agent 进行中。本树工作期间：

- 只新增/修改 `redteam/**` 与 `docs/agent-redteam-jailbreak-plan.md`、
  `docs/day-24-capability-parity.md`；不触碰 `src/`、`tests/`、`examples/`、
  `evals/`、`docs/stability*`、`docs/v2-stabilization-*.md`。
- 不运行全量 unittest（避免与 I9 的测试运行互扰）；隔离验证用 spike 脚本自身的
  import/grep 检查，不执行生产测试套件。
- 不做 git commit；工作树文件留给维护者统一处置。

## 隔离验证

```powershell
python redteam/spike/check_isolation.py
```

输出 `redteam/spike/results/isolation-<date>.json`；退出码 0=通过。脚本纯标准库。
