# RT/J 依赖锁取证（spike 阶段）

依据：`docs/agent-redteam-jailbreak-plan.md` v1.1 §6 冻结包「PyRIT」行。

## 已取得的证据（2026-08-31）

- **pyrit==1.0.1 在官方 PyPI 存在且可安装**：安装于 `redteam/.venv`（Python 3.11），
  `importlib.metadata.version('pyrit') == '1.0.1'`，import 成功。
- **隔离三腿验证通过**：见 `redteam/spike/results/isolation-20260831-141521.json`
  （venv 可导入 / 系统 Python ModuleNotFoundError / src+tests 零违禁 import /
  pyproject 零 pyrit 声明）。
- **依赖清单快照**：`pyrit-freeze.txt`（109 项，pip freeze，venv 内）。

## 环境发现（spike 产出，实施期必须知道）

1. 本机 pip 默认索引为清华镜像（`pypi.tuna.tsinghua.edu.cn`），**当前对该环境不可用**
   （连 `six` 都返回 "from versions: none"）；安装必须显式 `-i https://pypi.org/simple`。
2. 系统 Python 为 3.11，venv 继承同版本；pyrit 1.0.1 在 3.11 下安装与导入正常。

## 冻结包前仍缺（开工前必须补齐，属 §6 冻结项）

- [ ] **传递依赖完整 hash lock**：以 `pyrit-freeze.txt` 为底，用
      `pip-compile --generate-hashes`（或对 `pip download` 的每个 wheel/sdist 计算
      sha256）生成 `requirements.lock`；本 spike 只固化了版本清单。
- [ ] **SBOM**：cyclonedx-py 或等价工具从 venv 生成（SBOM 工具本身装在 venv，不入生产）。
- [ ] **license 汇总**：pyrit (MIT) 及全部传递依赖的许可清单与兼容性记录
      （`pip-licenses` 或 SBOM 附带），随 manifest 冻结。
