# WP-C 剩余项落地记录（A/B/C，2026-10-04）

维护者裁决：per-profile 部署值需要具体业务环境，当前不具备，跳过（D 类
保持"待维护者提供"）；A（无环境依赖）、B（Docker）、C（受控依赖准备）
全部实施。本文逐项给出"设计条款 → 生产执行点 → 验证观察"。

## A1 输入清单溯源与冻结

- 执行点：`sandbox/test_workspace.py`
  - `manifest_snapshot()`：清单冻结为 相对路径→sha256；
  - `prepare_test_workspace()`：复制后校验**副本**与**源**都等于快照
    （check-then-swap 防护），不一致即 `workspace_manifest_input_changed`；
  - `validate_manifest()`：路径解析后必须落在根内（`workspace_manifest_escape`），
    覆盖 Windows junction（实测：junction 指向根外被拒）；
  - `inputs_unchanged()`：运行后重算源哈希。
- 证据链：`CommandResult.inputs_changed` → `finalization.finalize()` 对
  `required_test_profiles` 拒绝不稳定证据（`required_test_inputs_changed`），
  即"输入变了不能拿它证明固定候选通过"。
- 测试：`tests/test_wp_c_manifest_provenance.py`（冻结/变更/删除检测、
  junction 逃逸拒绝、替换可见性、runner 稳定性标志）。

## A2 MCP 适配器释放规则

- 执行点：`mcp/release_rules.py` + `mcp/connection_manager.bind_tool_handler`
  - 无规则 = 现行 WP-1 行为（`result: None`、metadata-only）；
  - `fields` 白名单只裁剪元数据，`untrusted_mcp_result` 等来源标记恒随行；
  - `excerpt_chars` 有界摘录**必须**同时声明 `audience_policy_ref`
    （`mcp_excerpt_requires_audience`），摘录 ≤2000 字符且 `excerpt_truncated` 标记；
  - 规则与 (server, tool) 不匹配则不生效（fail-closed 到元数据）。
- 测试：`tests/test_mcp_release_rules.py`（默认/白名单/摘录前提/上限/坏字段/
  handler 集成），并通过既有 `test_d10_integration`。

## A3 候选准入绑定

- 执行点：`verification/admission.py`
  - `CandidateIdentity` 绑定 code/tests/deps/inputs/environment + profile
    version，六项任一变化产生新 `candidate_id`；
  - `AdmissionPolicy`（批准集合、是否需本地审查、单轮候选变更额度）；
  - `AdmissionTracker.evaluate()`：未批准 → `local_review_required`；
    额度耗尽同样返回 `local_review_required`（设计中的"固定需新增授权状态"，
    不是崩溃也不是静默继续）。
- 测试：`tests/test_candidate_admission.py`（六项敏感性、稳定性、批准失效、
  额度、重复同候选不耗额度）。

## C1 受控依赖准备决策

- 执行点：`verification/dependency_preparation.py`（只做决策，不安装）
  - 未开启安装 → `requires_authorization / install_disabled`；
  - 越界包名 → `requires_authorization / out_of_scope`（一个越界则整批不装）；
  - 超额度 → `budget_exceeded`；畸形名（`--index-url=`、`../`、`;rm -rf`）→ `refused`；
  - 只安装 `missing` 集合内的包；索引必须是 https。
- 测试：`tests/test_dependency_preparation.py`（7 项）。

## B 容器内 manifest 执行 + 秘密通道矩阵（Docker 实测）

- 执行点：`sandbox/runtime.py`
  - `SandboxCommandProfile.workspace_manifest` 非空 → `_run_manifest_profile`：
    只挂载清单候选到 `/workspace`（只读）+ 私有 scratch 到 `/scratch`，
    **宿主仓库不再挂载**；`scratch_policy_ref` 缺失直接 `scratch_policy_required`；
  - 运行后 `inputs_unchanged` 复核，写入 `CommandResult.inputs_changed`。
- 容器既有防线（实测确认）：`--network none`、`--read-only`、非 root
  (65532)、`--cap-drop ALL`、`no-new-privileges`、tmpfs、镜像 digest 固定。
- 测试：`tests/test_wp_c_container_isolation.py`（Docker 实测，daemon 不可用则跳过）
  - 容器内只看到清单文件（`host-file False`）、工作区写被拒、`/scratch` 可写、
    出站连接失败；
  - 敏感 profile + 合成秘密分布（文件内容 / 路径 / URL / base64 编码，均在
    原始 body 中出现）→ 模型回执为 `result_withheld`，**秘密一个字符都不出现**；
  - env 通道控制：任意环境变量注入被 `unsafe_sandbox_environment` 拒绝。

## 仍开放（如实）

- 依赖准备的**执行器**（真正在隔离环境安装并固定依赖身份）未实现——本次只
  落地决策面；执行器需要维护者批准的来源范围与预算。
- 镜像层通道未单独构造（依赖"镜像 digest 固定"这一控制，未做注入实验）。
- per-profile 部署值（D 类）按裁决待维护者提供业务环境。
- 敏感容器矩阵仍是合成秘密，不是真实敏感数据。
