# KoawaAgent V2 事故 → 加固实录（agent 安全真实性包 · 交付物②）

> 只记录本项目真实发生并已修复（或已明确为环境态）的事故，不编造。
> commit 引用经 git log 核对：74d9278（D17 预算可配/异常修复）、932a218（D20 Part A 错误修复指引）。
> 配套：docs/agent-security-threat-model.md、docs/day-21-agent-security-authenticity.md。

## 事故 1：HTML 任务 20 次动作烧光预算

| 项 | 内容 |
| --- | --- |
| 现象 | 真实模型会话中，模型反复错误调用工具（错误的 profile_id/参数），root 预算 20 次动作在任务完成前耗尽，任务被迫终止 |
| 威胁窗口 | LLM06/LLM10 过度自主 + 错误不可恢复 → 资源耗尽（对应威胁模型 T4） |
| 根因 | 模型拿到的错误信息没有修复指引：同样的错误无法自我纠正，只能重试到预算耗尽 |
| 加固 | D17：budget_action_limits 可配置（config.py，默认 root=20）；CLI 解释预算与放行语义；D20 Part A：工具错误带 detail/expected/example 修复指引（tools/errors.py、tools/schema.py、editing/protocol.py） |
| 加固 commit | 74d9278、932a218 |
| 验证 | tests/test_d20_tool_repair.py（6 用例全绿）+ test_t4_budget_stops_runaway_loop（预算闸门 fail-closed） |
| 教训 | 安全不只是"拦"，还要"教"：让模型能在策略内自我纠正，才能避免用暴力重试对抗失败 |

## 事故 2：非 git 目录启动报错被吞

| 项 | 内容 |
| --- | --- |
| 现象 | 在非 git 目录启动时装配直接失败，错误信息不透明（runtime_assembly_failed），用户无法判断原因；用户随后在 D:\cliStart\cliTest 手动 git init 才恢复 |
| 威胁窗口 | 失败信息不可操作 → 用户只能猜（运维/可用性事故，兼 LLM10 之外的另一类失败面） |
| 根因 | GitFacade probe 的异常没有映射成稳定、可解释的错误码 |
| 加固 | D17：GitFacade/assembly 把 git_command_failed 映射为 not_a_git_repository，错误码稳定、CLI 给出可操作提示 |
| 加固 commit | 74d9278 |
| 验证 | D17 回归测试（断言 not_a_git_repository 映射） |
| 教训 | 失败信息本身是攻击面的一部分：模糊错误让用户没有可执行的下一步 |

## 事故 3：沙箱 ACL 临时目录 PermissionError（环境态，非产品缺陷）

| 项 | 内容 |
| --- | --- |
| 现象 | 受限 token 环境下，沙箱把临时目录重定向到 ACL 受限目录，测试/工具创建 tempfile 时 PermissionError，且残留大量锁定的 tmp 目录 |
| 威胁窗口 | 环境策略与运行时假设不一致 → 测试污染/锁冲突（运维面） |
| 根因 | 宿主沙箱策略限制，非产品代码缺陷 |
| 加固 | 无产品修复；记录为已知环境态（运维项）：全量回归需在不受限环境跑，创建临时目录的路径遵循既有统一规则 |
| 加固 commit | —（环境态） |
| 验证 | 全量回归在受限环境外跑绿（379 passed + 16 skipped，Docker down） |
| 教训 | 环境假设要写进文档，否则每次都是"神秘的 PermissionError" |

## 事故 4：模型不按 schema 调用 apply_patch

| 项 | 内容 |
| --- | --- |
| 现象 | 实际模型中，apply_patch 被以错误形状/错误字段调用（如缺少换行、多余字段），模型反复失败 |
| 威胁窗口 | 工具使用脆弱 → 错误无法恢复 → 重试烧预算（与事故 1 同一链条的另一半） |
| 根因 | 架构弱模型 + schema 错误信息只是"无效参数"，没有指出哪个字段、期望什么形状 |
| 加固 | D20 Part A：所有参数错误带 expected/example/detail：apply_patch 的 set 不匹配给 missing_field:newline / unexpected_field:base_sha256，hunk 不匹配给 first_mismatch_line:N（editing/protocol.py、tools/schema.py 值校验、tools/registry.py 最小示例）；错误正文保持脱敏 |
| 加固 commit | 932a218 |
| 验证 | tests/test_d20_tool_repair.py（6 用例）+ D20 设计文档 Part B 真实会话复核（可选，需用户同意 + API 成本） |
| 教训 | 工具的"可修复性"是 agent 安全的一部分：错误信息要告诉模型怎么改，而不是只告诉它错了 |

## 复盘：四条事故 → 四条对策

| 事故 | 落入威胁模型 | 对策条 |
| --- | --- | --- |
| 预算烧光 | T4 Excessive Agency | budget_action_limits + fail-closed + 可修复错误 |
| 报错被吞 | （失败面） | 稳定错误码 + 可操作提示 |
| 环境态冲突 | （运维面） | 环境假设入文档 |
| apply_patch 反复失败 | T4 链条 | 错误修复指引（expected/example/detail） |

> 这些事故都是"真实发生在本项目开发过程中"的记录；写完这条实录，我们才敢说威胁模型里的对策不是纸面设计。
