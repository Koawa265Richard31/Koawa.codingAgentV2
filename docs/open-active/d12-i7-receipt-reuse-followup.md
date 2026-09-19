# 切片阅读问题台账

按用户要求，后续阅读发现的问题统一追加到本文件，保留此文件路径。记录不等于授权修复；区分静态确认、待动态验证、已复现与已修复，不把候选问题直接写成已证实漏洞。明确的保守设计边界不列作待修漏洞，应单独说明，由用户决定是否扩展设计。

| 编号 | 问题 | 当前状态 |
|---|---|---|
| D12-I7-001 | 复用失败集成回执时返回成功状态 | 静态路径已确认，待动态复现与修复 |
| D12-I7-002 | 首次投递缺少预期完整用户基线比对，可能在写入后才拒绝漂移 | 候选实现缺口；静态路径明确，具体失败场景待复现 |
| D12-I7-003 | worktree 清理未强制要求增强版 Artifact 已持久化并保留 | 静态接线缺口已确认，待设计与实现 |
| D13-D23-001 | Run 内压缩丢失工具结果语义，成功与失败可生成相同替换块 | 静态确认，压缩函数级对照已复现；未验证真实模型影响 |
| D13-D23-002 | 旧回合结论注入未携带已持久化的测试证据引用 | 静态投影缺口已确认；未验证模型行为影响 |
| D13-D23-003 | CLI 连续聊天失败回合未加入历史，失败回显无法自动覆盖该路径 | 静态接线缺口确认；未做端到端动态复现 |
| D13-D23-004 | 跨回合压缩块持续累积，不受近期窗口限额约束 | 静态确认及 SessionHistory 级复现；未做长会话端到端耗尽测试 |
| D13-D23-005 | 跨回合模型摘要只存内存，重启后重新生成 | 静态恢复路径确认；未测真实 provider 的文本差异与费用 |
| D13-D23-006 | 请求容量计数遗漏指令与工具定义，硬上限检查依赖压缩路径 | 静态确认并复现指令漏计；未复现真实 provider 超窗 |
| D13-D23-007 | conclusion_model_summary 与 journal_inject_latest 缺少运行时消费路径 | 静态引用核查确认；未做真实模型开关对照 |
| REVIEW-001 | 高耦合模块修改的依赖分析、组合验证与返工成本 | 用户指定待核查项，尚未认定实现缺陷 |

## D12-I7-001：复用失败集成回执时返回成功状态

## 状态与范围

- 记录日期：2026-09-14。
- 检查基线：`2d42746`；行号仅用于定位，后续以符号名为准。
- 状态：静态代码路径已确认，待动态复现、修复与回归验证。
- 本次仅新增此文档；没有修改实现、运行测试、创建委派任务或推送提交。
- 范围：增强版 `DurableArtifactIntegrator.integrate()` 的已有回执复用分支，不是历史版 `ArtifactIntegrator`。
- 本次源码引用检索未找到正式 runtime 的该集成器调用接线；已知调用在 I7 测试和故障注入 fixture 中。不得据此宣称线上事故或生产交付绕过。

## 问题概述

同一批 Artifact 首次集成重测失败后，会持久化 `known_negative` 回执。再次调用 `integrate()` 时，已有回执分支不读取原始测试结果，而是直接返回退出码 `0` 与 `success`。

因此，调用方收到的 `DurableIntegrationResult` 与持久化回执可能不一致。持久化失败回执没有因此被改写，现有 `deliver()` 仍读取真实回执并拒绝失败结果。

本问题是返回结果正确性缺陷；不能夸大为已证实的失败补丁交付绕过。

## 代码证据

### 1. 回执复用键及错误返回

位置：`src/koawa_agent_v2/workspace/integration.py`，`DurableArtifactIntegrator.integrate()`，约 223–227 行。

```python
set_digest = _digest_doc([str(item.artifact_id) for item in ordered])
receipt_id = uuid5(NAMESPACE_URL, f"koawa-v2:integration:{set_digest}")
if existing := self._load_receipt_head(receipt_id):
    return DurableIntegrationResult(existing, 0, "success")
```

`ordered` 由 Artifact ID 排序得到。存在回执时，该分支直接返回，不再执行后续应用与重测。存在回执本身不代表回执记录的是成功。

### 2. 首次执行会保存失败结果

同一方法约 255–274 行：

```python
result_kind = "success" if result.exit_code == 0 else "known_negative"
# payload 中保存：
"test_result_kind": result_kind,
"test_exit_code": result.exit_code,
```

首次返回使用实际的 `result.exit_code` 与 `result_kind`；只有复用分支把它们写死为成功。

### 3. 交付仍检查持久化回执

位置：同文件 `deliver()`，约 282–285 行。

```python
payload = self._load_receipt(receipt_ref)
if payload["test_result_kind"] != "success":
    raise AgentError("integration_known_negative_not_deliverable")
```

`_load_receipt()` 检查引用、事件类型与内容摘要，返回存储中的 payload。它不使用 `DurableIntegrationResult.test_result_kind` 作为交付依据。

## 预期复现场景（尚未执行）

优先复用 `tests/test_i7_durable_integration.py` 的现有 fixture，不需要真实模型或 Docker。

1. 使用有效 Artifact 调用 `integrate()`，测试命令为 `[sys.executable, "-c", "raise SystemExit(3)"]`。
2. 确认首次返回 `test_exit_code == 3`、`test_result_kind == "known_negative"`，并保存回执引用。
3. 保持 Artifact、测试命令与环境不变，再次调用 `integrate()`。分别覆盖同一实例，以及从同一持久化存储重建的实例。
4. 根据当前代码，第二次返回会变为 `0 / success`，但引用指向的持久化 payload 仍为 `3 / known_negative`。
5. 将第二次返回的回执交给 `deliver()`，应仍抛出 `integration_known_negative_not_deliverable`，用户工作区不变。

区分三层结果：

| 层次 | 当前预期观察 |
|---|---|
| 重复调用的返回对象 | 错误地报告 `0 / success` |
| 持久化回执 | 保留 `3 / known_negative` |
| 交付入口 | 读取失败回执并拒绝交付 |

## 影响与现有保护

- 调用方若相信返回对象，可能错误显示测试通过、漏掉修复步骤，或作出错误后续决策。
- 未发现该分支改写失败回执；不能描述为“历史失败被持久化覆盖成成功”。
- 现有 `deliver()` 的失败回执检查仍是保护边界，修复不得削弱或移除。
- `APPLIED` 是重测操作已有已记录结果的状态；`KNOWN_NEGATIVE` 仍表示明确失败。不得通过混淆两者解释或掩盖本问题。

## 后续修复建议

1. 先补回归测试，证明失败回执复用时返回状态错误，而交付依然拒绝。
2. 在已有回执分支读取并验证持久化 payload，以其中的测试结果构造 `DurableIntegrationResult`，而不是填入固定成功值。
3. 优先复用现有回执读取/验证入口。缺失或非法字段应给出稳定错误，不得默认为成功；具体类型约束与现有 `ContainerResult.exit_code` 契约一并核对。
4. 保持正常复用不重复执行测试、不新增重复集成回执、不改写旧回执的语义。
5. 保持 `deliver()` 独立读取持久化回执的检查，不改成信任调用方传入的成功标记。

## 验收条件

- 首次失败与复用失败均返回相同的非零退出码和 `known_negative`。
- 成功回执复用仍返回原成功结果。
- 同进程与重建集成器后的复用行为一致。
- 使用可计数 runner 证明复用没有再次执行测试；检查回执引用及事件流，确认没有重复写入集成回执。
- 失败回执无论首次还是复用后传给 `deliver()`，都被拒绝，用户工作区保持不变。
- 回执读取异常/非法结果字段不会被当成成功。
- 原有回执伪造、故障恢复、交付重试测试不退化。
- 先运行聚焦测试 `python -m unittest discover -s tests -p "test_i7_durable_integration.py" -v`，再按仓库规则在 `v2/`、`PYTHONPATH=src`、Python 3.12+ 下运行 `python -m unittest discover -s tests -v`。报告实际结果；未跑或失败的项目必须如实列出。

现有 `test_known_negative_is_applied_but_not_deliverable` 只覆盖首次失败与禁止交付；本次检查未找到明确覆盖“失败后重复 integrate 的返回值”的测试。

## 不随本任务扩展的事项

- 不把增强集成器接入正式 runtime。
- 不修改历史版集成器，不重构整个 Artifact 协议。
- 不自动合入或处理其他会话的工作区修改。
- 回执键目前根据 Artifact 集合生成，未包含测试命令等全部验证条件；改变验证条件后的复用语义应另行评估。本任务先固定输入，修复结果回放不忠实的问题，不顺带扩大回执身份设计。
- 上游测试证据的语义校验与补丁关联检查是另一项问题，不在本记录的修复范围内。

后续接手时应重新执行仓库对齐检查，以当前代码确认问题仍存在，并保留其他会话的修改。未获得额外授权前，不提交、推送或扩大修改范围。

## D12-I7-002：首次投递的完整用户基线前置检查缺口（待复现）

- 日期与基线：2026-09-14，`2d42746`。
- 状态：静态检查确认下述校验顺序；未动态复现，不声称已造成数据丢失。
- 位置：`workspace/integration.py` 的 `DurableArtifactIntegrator.deliver()`，约 328–388 行；`workspace/artifacts.py` 的 `apply_package()`，约 313 行起。

### 静态观察

首次投递会捕获用户工作区状态，但明确的基线拒绝检查是 `before.prestate.head_commit != payload["base_commit"]`。它会把当前前置摘要记入新建 effect，却未在首次投递的这一分支将其与批准/测试时的预期用户工作区前置状态进行比较。已有 effect 的复用身份检查与首次投递的前置限制应区分。

之后逐个应用 package，再捕获工作区内容，与回执的 `integrated_content_digest` 比较。不一致则报告 `artifact_delivery_postcheck_failed` 并尝试记录 `OUTCOME_UNKNOWN`；该异常路径没有回滚此前已应用 package 的逻辑。`git apply --check` 只检查相关补丁是否可应用，不能代替整个工作区的前置状态一致性检查。

### 待执行的复现方案

1. 在临时仓库集成一个修改 `a.txt` 的候选包，完成成功重测并保存回执。
2. 在投递前修改另一个已跟踪文件 `b.txt`，不提交；确认用户 HEAD 仍等于基线。
3. 调用 `deliver()`。检查是否先修改了 `a.txt`，再因 `b.txt` 导致内容摘要不一致而报告后置检查失败。
4. 检查用户目录的真实状态和 effect 状态，确认哪些修改已发生、哪些被保留；不要仅凭异常推断“未写入”或“已回滚”。

### 真正需要验证的实现缺口

本项待验证的是：首次投递未先确认完整用户基线是否仍是允许的状态，导致本可在写入前识别的既有用户修改，可能直到候选修改已经落地后才触发拒绝。它不是“所有异常必须自动回滚”的要求。动态复现后仍需核对交付契约，确定哪些用户脏状态应禁止、哪些允许保留，再制定修复，不凭字段名称推断已承诺的保证。

### 影响边界与后续决策

当前代码能拒绝 HEAD 漂移，并在正常到达后置检查时发现交付内容不匹配；这不等于它在所有用户未提交修改场景下都能写入前拒绝。候选风险是交付失败后用户工作区可能已混入部分候选修改。该场景不要求声称覆盖或删除用户修改。

后续修复前先明确允许的投递前置状态：是拒绝所有用户脏状态，还是允许经快照绑定的受控状态。不得简单要求所有用户目录摘要等于任意单个子 Agent 的修改后摘要。若选择写入前拒绝策略，验收应证明 HEAD 不变但存在未授权工作区漂移时不发生候选写入；同时保留后置校验及不确定结果记录。合作式投递锁也不能被描述为阻止所有外部编辑器写入。

本项仅登记待验证问题，不修改实现，不扩展为整仓审计。后续正式修复仍需聚焦测试与仓库规定的全量回归。

## 设计边界说明（不计入待修漏洞）

- **投递异常不盲目回滚**：用户或外部进程可能并发修改目录，未经归属确认的恢复会误删用户内容。当前记录不确定结果并停止，不因缺少无条件回滚而单独判为缺陷。
- **不确定结果阻止同一执行身份盲重试**：这是保守恢复策略，不是成功率缺陷。自动判断实际落地状态、选择继续或恢复，是尚需单独设计的能力；只有用户决定扩展后，才建立对应实现任务。
- **合作式锁不阻止所有外部进程**：是否支持不遵守锁的宿主写入者，应依据威胁模型和并发契约决定，不能把文件锁直接解释为全局文件系统隔离。

以上边界在教学中明确告知用户，不自动转为修复要求。D12-I7-002 只追踪前置状态校验及已存在漂移导致写入后失败的问题，不把这些取舍捆绑进去。

## D12-I7-003：worktree 清理缺少 Artifact 封存前置条件

- 日期与基线：2026-09-14，`2d42746`。
- 状态：静态接线/不变量缺口已确认；未修改实现。
- 位置：`workspace/worktree.py` 的 `WorktreeManager.reap()`；`workspace/artifacts.py` 的 `ArtifactPackageStore.pin()`；调用检索覆盖 `src/`、`tests/` 与 `examples/`。

### 已确认事实

`reap(agent_id, run_id=...)` 会核对该运行登记的 workspace、受管路径和删除 effect，并在删除后检查目录、Git worktree 登记及管理元数据是否消失。它没有检查：

- 该 worktree 是否存在需要交付的修改；
- 修改是否已经生成 `ArtifactPackageV2` / `ArtifactV2`；
- package 是否已经持久化并 pin；
- 父级是否确认不再依赖原 worktree。

`ArtifactPackageStore.pin()` 是独立接口；当前未看到它与 `reap()` 共享前置条件或事务。源码检索也未找到正式 runtime 编排增强版 `ArtifactV2` 创建、pin、集成和 worktree reap 的完整链路。

现有 golden fixture 在旧版路径中先调用 `manager.diff()` 构造内存 `Artifact`，再调用 `manager.reap()`，说明测试流程人工遵守“先捕获、后清理”；它不等于增强版持久化不变量已被运行时强制。

### 风险场景

写 Agent 的 worktree 含有唯一一份未提交修改。上层错误地先调用 `reap()`，删除成功并登记为 reaped；由于增强版 Artifact/package 尚未形成或持久化，后续无法从 Artifact store 重建候选修改。此处风险是候选工作成果丢失，不应直接描述为用户主工作区数据被删除。

### 记录边界

本项只记录缺少强制生命周期不变量与正式接线这一事实，不在当前任务中选择修复方案、制定实现计划或验收计划。资源最终需要清理仍是合理目标；“取消或失败的工作成果是否值得封存”属于后续设计决策，不能在此预设为必须保存 Artifact。

### 后续规划建议（尚未采纳）

评估引入一种持久化的 workspace disposition，用明确状态表达工作成果的处置结果：

- `PRESERVED`：成果已封存；
- `DISCARDED`：成果已被明确放弃；
- `QUARANTINED`：成果被保留，但禁止自动使用；
- `PENDING`：处置尚未决定，不允许清理 worktree。

该建议目前仅作为后续设计候选，不代表仓库已采用，也不构成实施授权。后续评估应先确认它相较于更小的清理资格机制是否确有价值，并明确取消、失败、疑似污染和正常完成等场景的状态语义，避免为了统一形式而过度设计。

## D13-D23-001：Run 内压缩丢失工具结果语义

- 日期与基线：2026-09-15，HEAD 与本地 `origin/main` 引用为 `2d42746`；未执行 fetch，不声称检查了服务器最新提交。
- 状态：静态确认，并完成压缩函数级成功/失败对照复现。未验证真实模型误判、错误交付或审批绕过。
- 本次仅更新问题台账，不修改实现，不制定修复计划。
- 工作区已有其他会话改动；压缩生成器 `execution/loop.py`、恢复 reducer `recovery/context.py`、检索 `runtime/session.py` 无本地修改。`recovery/execution.py` 有既存修改，不归入本次修改。

### 契约与代码证据

`docs/day-23-memory-layer-upgrade.md` 第 2 节完成条件要求压缩前后改动、测试证据等事实不丢失；第 5.6 节明确规定确定性 summary 包含工具名、稳定结果码、文件、测试及已完成/未完成义务。因此本项超出一般有损压缩的合理取舍，是已声明能力与实现不符。

`execution/loop.py:AgentLoop._maybe_compact()`（约 389–407 行）生成替换块时只使用批次组数、去重后的工具名和 recorder 的累计 `tool_count`。不读取被选组的结果正文、错误标记、测试退出码或失败原因，也未在该替换块中输出文件、未完成义务或可直接使用的来源引用。工具调用参数和旧 assistant 文本也随所选范围退出模型上下文。

正式装配 `runtime/assembly.py` 向 loop 传入 `config.memory`，`execution/worker.py` 绑定当前 Run recorder；压缩后 loop 采用 recorder 的 `synced_context()`。故此路径已接入正式执行，不只是历史 D13 示例。

`recovery/context.py` 在回放 compacted 事件时采用保存的 `replacement_item`，校验范围与上下文摘要；不会从原始工具结果重新生成更丰富的内容。恢复一致性不能补回替换块没有表达的信息。

### 检索补偿核查

`runtime/session.py:SessionMemory.recall()` 经 `_thread_records()` 读取终态 Turn（非终态被跳过），返回 `RecallHit` 的用户输入、最终回复、工具名、文件与评分。此接口不返回当前活动 Run 被压缩的原始工具结果，不能作为本缺口已闭环的依据。

原始 run-execution 事件仍在持久化存储中，其他权威业务状态也没有被摘要改写。本次确认的是模型上下文的信息保留缺口，不是存储数据丢失，也不主张仓库完全不存在其他重新读取业务状态的手段。

### 已执行的对照复现

使用 Python 3.13，在命令行复用 `tests/test_d23_loop_compaction.py` 的 `CompactionTriggerTest`、`_tool_turn` 和 `RecordingCompactionSink`；未新增或修改测试文件。

两次构造相同用户目标、相同调用身份、两个闭合组，保留最近一组，只改变最老组的合成 `ToolResultMessage`：

- A：`exit_code=0; passed=true`，`is_error=False`。
- B：`exit_code=1; passed=false; LOGIN_EXPECTED_401_GOT_200`，`is_error=True`。

两次调用真实 `_maybe_compact()`，断言生成的 `replacement.content` 完全相同，且均没有 `exit_code` 或失败原因。断言通过；替换块只有组数、工具名与累计计数。

复现范围：fixture 的工具名为 `read_file`，以上是用于控制变量的合成结果，没有真实运行测试命令。sink 是记录替换参数的测试替身，因此该对照证明生成器对成功/失败信息不作区分，不单独证明整个持久化/模型调用端到端行为。此前生产接线测试仅证明能触发压缩，不证明关键语义保留。

### 影响与边界

- 长 Run 的早期失败原因、测试结果或读取发现一旦落入压缩范围，可能从后续请求中消失，导致重复勘察、重复执行或错误判断任务进度。
- 当前已复现的是信息遗漏；没有证据说明生成器主动将失败改写为成功，也没有真实模型行为复现。
- 现有测试、审批和交付门禁仍可能拒绝错误操作；不得将本项直接升级为已证实的安全门禁绕过。
- 压缩无需保留完整 stdout/stderr 或所有历史正文；缺口依据是未保留契约要求的有界结果事实及义务，而非要求无限上下文。

## REVIEW-001：高耦合模块的依赖分析与组合验证（待核查）

- 记录日期：2026-09-16；用户要求先标记，继续阅读，读完后再定方向。
- 场景：模块 A 的修改通过局部测试，但改变了模块 B 依赖的返回值、异常或副作用契约，组合运行失败；反复局部修补增加工具调用、token 消耗和任务预算压力。
- 已知边界：`repo_map`、`search_text`、`read_file` 提供导航与读取能力，本身不强制分析调用方或选择组合测试。此事实不足以认定整个运行时缺少相关策略。
- 后续阅读关注：规划与验证链是否覆盖调用方/接口影响、相应集成证据，以及连续返工时是否能依据已有失败证据重新判断，而非反复修改同一局部。
- 状态：尚未检查完整规划与验证路径，未动态复现，不列为已确认漏洞；本次仅记录关注点，不制定实现方案或执行修复。

## D13-D23-002：旧回合结论投影遗漏测试证据引用

- 日期与基线：2026-09-16，本地 HEAD 与已有 `origin/main` 引用为 `2d42746`；未执行 fetch。
- 状态：静态投影缺口已确认；未动态验证真实模型是否因此错误复用历史测试结论。
- `TurnConclusionStore.build()` 会从终态 Run 的 `completion_evidence` 构造 `test_evidence_refs`，其中包含 stream、version、event ID 和 evidence digest，并持久化在结论 DTO 中。
- `runtime/session.py:_conclusion_text()` 将窗口外旧回合重新注入模型时，只输出 Turn/Run 状态、错误码、成功工具、修改文件、不确定状态和未完成义务；没有输出 `test_evidence_refs`、`source_heads_digest` 或可供后续取回证据的等价引用。
- 因此，证据引用仍存在持久化结论中，但旧回合离开近期窗口后，模型可见的 reconstructed conclusion 无法据此区分“曾有绑定证据”与“只有历史状态文本”，也不能直接沿引用核验测试对应的版本。
- 本项是跨回合记忆与证据可追溯性的能力缺口。当前交付门禁仍可独立检查当前 Run 的 completion evidence，不得描述为已证实的测试门禁绕过或错误交付。
- 本次只记录事实和边界，不修改实现，不制定修复方案。

## D13-D23-003：连续聊天失败回合未接入下一轮历史

- 日期与基线：2026-09-16，`2d42746`，相关 cli.py、app.py、session.py 无工作区修改。
- `runtime/cli.py` 普通聊天后仅在 `outcome.ok and payload.get("turn_id")` 时调用 `history.append()`；失败分支只打印错误或失败摘要。
- `runtime/app.py:_truth_outcome()` 对非 COMPLETED Turn 返回 `ok=False`；下一次 `chat()` 从调用方传入的 `history.context_items()` 获取上下文，没有在此刷新失败历史。
- `SessionHistory.context_items()` 虽能对已有的、无 final_text 的 SessionTurn 调用 `_failed_turn_text()`，但上述连续 CLI 路径没有把失败 Turn 放入其输入列表。因此“失败回显函数存在”不等于同进程失败后下一轮能看到该失败。
- `SessionHistory.from_thread()` 会从数据库加载终态 Turn，包含失败项，所以重建历史与同进程连续交互存在差异；不能描述为失败记录未持久化或所有入口都不能回显。
- 当前失败回显只渲染 status、error 和已提供的 changed_files。from_thread 构造 SessionTurn 未填 changed_files；回显函数也不读取 TurnConclusion 的 Run 状态、开放 effect 或 uncertainty。故不能把设计文档中的完整失败证据投影当成当前已实现行为。
- 状态：静态接线确认，尚未进行端到端故障注入或真实模型行为验证。仅记录，不实现、不制定修复计划。

## D13-D23-004：跨回合压缩块持续累积

- 日期与基线：2026-09-16，`2d42746`；相关 session.py、worker.py、loop.py 无本地修改。
- `SessionHistory._bounded_recent()` 仅限制近期 Turn 数与用户输入/最终回复字符数。`maybe_compact()` 将新增移出窗口的回合生成 CompactionResult，并持续 append 到 `_compacted`；`context_items()` 每次注入全部已有压缩块，没有在此合并、淘汰或给压缩块设置累计预算。
- 即使可选模型摘要关闭，`_authoritative_projection()` 仍逐回合输出状态与用户请求片段，所以压缩块总量随历史增长。
- 正式 chat 经 `history.context_items()` 将这些块交给 worker，块类型为 UserMessage，不能成为当前 run 内 closed-group 压缩的来源。全局 run 字符预算可拒绝过大的请求，但无法通过该机制缩小累积的历史块；不声称必然发送超限请求。
- 已执行 Python 3.13 函数级复现：SessionHistoryLimits(max_turns=2, max_chars=200, compact_min_turns=2)，连续追加 20 个短成功回合，每次构造 context_items；得到 9 个 session:compact 块，共 1853 字符，证明近期 200 字符限额不限制历史压缩块总量。没有使用模型，没有触发端到端容量耗尽。
- 与单 Run 的旧摘要累积区别：本项确认同一交互会话跨多个用户请求也会累积，不能以“每次请求创建新 Run”消除此问题。
- 硬上限拒绝仍是合理保护。待解决的是长期会话记忆能否持续保持有界；具体合并、检索及缓存成本策略按用户要求留到 D13 与增强全部读完后再定。本项只记录，不修复、不制定实施计划。

## D13-D23-005：跨回合模型摘要未持久化绑定

- 日期与基线：2026-09-16，`2d42746`；相关 cli.py、session.py 无本地修改。
- 交互 CLI 针对具有 `_endpoint` 的模型客户端向 `SessionHistory` 注入 `summarize_via_client()`。旧回合累计达到阈值时，`maybe_compact()` 将用户请求和最终回复拼为 transcript，再调用同一 provider/model 生成自然语言摘要。
- `CompactionResult` 只追加到当前 `SessionHistory._compacted` 内存列表；没有查到将 session compaction block、summary receipt 或其来源绑定写入事件流的路径。
- 进程重启后，`SessionHistory.from_thread()` 从终态 Turn 重建原始历史；下一次 `context_items()` 会重新运行 `maybe_compact()`，从而再次调用摘要模型。自然语言输出可能变化，并产生新的调用与缓存成本。
- 同一压缩块还包含由 `_authoritative_projection()` 生成的确定性字段；摘要失败会降级为仅使用该投影。因此本项不表示恢复后完全没有历史，也不同于已持久化、可重放的单 Run compaction 事件。
- 状态：静态恢复路径确认，尚未用真实 provider 复现摘要差异、计费或任务影响。只记录，不实现、不制定修复计划。

## D13-D23-006：完整模型请求容量检查缺口

- 日期与基线：2026-09-19，`2d42746`；execution/loop.py 无本地修改。此项仅补充工作区文档，不改变既有暂存版本或归档操作。
- `AgentLoop.context_chars()` 只累计 UserMessage、AssistantMessage、ReasoningSummaryEcho、ToolCallEcho 参数和 ToolResultMessage 的部分文本长度，未累计 InstructionMessage。工具定义也未进入计数；`_maybe_compact()` 在获取本轮工具目录快照并构造 ModelRequest 之前运行。
- 当前计量使用 Python `len(str)`，不是完整请求的 UTF-8 字节数或 provider token 数；固定 reserve 不能证明未计入部分始终被覆盖。ModelRequest 的类型与调用配对校验不补足完整请求容量检查。
- Python 3.13 函数级复现：100,000 字符的系统 InstructionMessage 加 5 字符 UserMessage，`context_chars()` 返回 5；默认 hard 为 64,000 字符。证明指令漏计，不证明真实 provider 已发生超窗或特定费用损失。
- `_maybe_compact()` 在 memory 缺失、压缩关闭、sink 缺失或 durable pending calls 存在时，均可在其硬上限判断前返回。此前另复现无 sink 时函数直接返回。pending 分支之后仍可能被协议校验拒绝，不能因此断言开放调用一定发送成功。
- 影响：本地软/硬阈值通过不能证明完整请求满足配置意图，更不能证明符合模型 token 窗口。关闭压缩与关闭容量保护在此函数中耦合。硬上限拒绝并非所有路径都无条件执行。
- 设计依据：D23 文档要求完整 MemoryEnvelope 经协议、字符/字节预算及工具配对验证后形成 ModelRequest。当前检查只覆盖部分消息文本，不能称为完整请求容量门禁。
- 本项为容量管理与长任务可靠性缺口；不声称审批绕过或数据泄露。未修改实现、未制定修复计划，待读完增强链后统一选择预算与压缩策略。

## D13-D23-007：两个记忆配置开关未接入实际行为

- 日期与基线：2026-09-19，`2d42746`；memory.py、session.py、cli.py、turn_conclusion.py 无本地修改。
- 在 src 与 tests 中检索 `conclusion_model_summary`、`journal_inject_latest`，引用仅涉及 MemoryConfig 字段、允许键、类型校验和配置测试，未找到运行时读取它们以切换行为的分支。
- `TurnConclusionStore.build()` 当前直接构造 `untrusted_summary=None`。持久 DTO 和结论文本渲染器支持该字段，但当前正式生成链未因 conclusion_model_summary=True 调用摘要模型。
- `journal_inject_latest=True` 没有对应已接入的读取 SESSION.md 并注入上下文的路径。journal reminder、CLI /journal 文件导出与正文自动注入是不同能力。
- CLI 另外按客户端是否具有 `_endpoint` 注入 `summarize_via_client()`，用于 SessionHistory 的旧对话压缩。该调用不读取 conclusion_model_summary；False 不能被解释为禁止所有摘要模型调用。这是开关作用域区别，不单独声称构成费用授权绕过。
- 配置解析测试通过只证明字段可被接受，不能证明运行时效果。状态为静态接线缺口确认，尚未做真实模型开关对照；只记录，不修改实现、不制定方案。
