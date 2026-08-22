# D20：真实会话健壮性（工具错误增强与自修复指引 + 全会话真模型验证）

> 状态：Part A COMPLETE（2026-08）；Part B 待人工执行（真实模型验证，opt-in）。
> 动机：D16–D19 全部只经 scripted provider 验证；真模型下"廉价模型不按 schema 调工具"
> 是现场演示最容易翻车的点（已实证：HTML 任务 6 次 invalid_patch_change 烧光预算）。

## 1. 目标

1. 工具错误增强：让失败的工具调用返回"可自修复"的错误信息（哪里错、期望什么、正确示例），
   模型在既有 loop 内自己纠正——不改 loop 契约、不加隐式重试。
2. 全会话真模型验证：把 D16–D19 能力用真实模型端到端走一遍（待人工执行）。

## 2. 已实现（Part A，COMPLETE）

### 2.1 Patch 错误诊断细化
- PatchError 增加 detail（校验：a-z0-9_:-，≤127，不含文件正文）；
- ADD 字段集错误 → invalid_patch_change + detail=missing_field:newline /
  unexpected_field:base_sha256（按实际缺失/多余首字段）；
- UPDATE hunk 旧行不匹配 → patch_context_mismatch + detail=first_mismatch_line:N
  （第一个不匹配的 1-based 行号）；
- apply_patch 工具把 detail 透出到结果 JSON（错误码保持稳定，既有契约不受影响）。

### 2.2 工具参数错误增强
- ToolArgumentError 增加 expected（value-type:...，范围违规带边界值）；
- argument_error_result 增加 example（来自 Compile 后 schema 的最小合法参数示例，
  经 _minimal_example 构建：required 字段按类型取占位值，≤256 字符）；
- example 构建失败/无字段 → 优雅降级为无 example 的基础错误（错误码仍稳定）。

## 3. Part B 全会话真模型验证（待人工执行，opt-in）

协议（SiliconFlow Qwen3.5-35B-A3B + reasoning_effort=off，D:/koawa-demo）：
1. 记忆投影：两轮对话，第 2 轮问"我上一轮让你做了什么？";
2. 压缩：history_max_turns=2 跑 4 轮，确认 [ctx] 压缩块含 files=;
3. /recall calc；4. /journal 生成 SESSION.md；
5. 思考链：reasoning_effort=low 一轮确认实时输出；
6. 自修复：执行"写 index.html"任务，断言 apply_patch 失败时错误含 detail，模型在预算内完成。

证据产物：examples/d20_session_verification.md（转写摘要 + 断言结果表）。

## 4. 测试（tests/test_d20_tool_repair.py，6 个，全部通过）

- ADD missing_field / unexpected_field；
- UPDATE first_mismatch_line:N；
- 参数错误 expected + example（read_file 样例）；
- detail 无文件内容泄漏；
- example 构建降级保持稳定错误码。

## 5. 边界（不做）

- 不做 loop 级自动重试/自动改参；不做语义向量检索；
- Part B 人工验证不进 CI（延续真实 provider 不进 CI 原则）。

## 6. Definition of Done

- Part A：6 测试 + 全量回归绿 ✅；
- Part B：待人工执行后补证据，另把状态改为 COMPLETE。
