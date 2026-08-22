# 切片 A：异常修复（预算可配置 + 清晰报错）与思考链显示

> 状态：COMPLETE（2026-08）。起因：真实交互中"写一个 HTML"任务因模型反复用错工具格式
> 烧光 20 次动作预算，整轮失败且 CLI 只显示晦涩的 d2:resource_budget_exceeded。

## 1. 问题链（事件库实证）

对失败 turn 的事件存储诊断：
- apply_patch 连续失败：invalid_patch_change x6、invalid_patch_document x1、
  workspace_path_not_found x1 —— 模型不知道新建文件（ADD）的精确格式；
- run_test_profile 用错 profile id：unknown_command_profile x2（配置里的 id 是 unit）；
- 动作预算（root=20，硬编码）耗尽 → resource_budget_exceeded → 整轮失败，CLI 无解释。

另发现：非 git 目录装配时报模糊的 runtime_assembly_failed（真实用户踩到）。

## 2. 修复内容

### 2.1 预算可配置（runtime/config.py + assembly.py）
- RuntimeConfig 新增 budget_action_limits: tuple[(principal, limit)]，默认 (('root', 20),)；
  JSON 配置接受对象形式 {"root": 40}；校验：正整数、principal 名白名单、去重。
- assembly 的 ApprovalService 不再硬编码 {"root": 20}，改读配置。
- 演示配置 D:/koawa-demo/my_config.json 已调为 root=40。

### 2.2 清晰报错
- GitFacade：非 git 目录的 rev-parse probe 失败（git_command_failed）映射为
  not_a_git_repository（verification/git.py）；装配层把 GitFacadeError 透出为
  具体错误码（assembly.py 新增 except GitFacadeError）。
- CLI：turn 失败时若为 resource_budget_exceeded，打印中文解释（预算值、如何调大、
  建议把请求写具体）。

### 2.3 思考链显示（model/openai_client.py + assembly.py + app.py + cli.py）
- OpenAICompatibleChatClient 新增 reasoning_sink 显示通道：reasoning_content 增量
  实时转发给 sink；不入库、不回传、不进 canonical context（延续 D2 契约）。
- 装配与 AppRuntime 透传 reasoning_sink；interactive CLI 用 _ThinkingDisplay 流式
  打印思考片段（懒加载头部）。
- 注意：reasoning_effort=off 时模型不产出 reasoning，sink 不会被调用（无噪音）。

## 3. 测试

- test_runtime_config.py：budget_action_limits 解析/非正拒绝/重复 principal 拒绝（+3）；
- test_openai_compatible_client.py：reasoning_sink 收到片段且 canonical stream 不受影响（+1）；
- test_runtime_assembly.py：非 git 目录 → RuntimeAssemblyError(not_a_git_repository)（+1）。

## 4. 边界与遗留

- 思考链仍不持久化、不可回传；仅交互显示（面试叙事：显示通道与协议分离）。
- 预算默认 20 保持 D9 语义；交互场景建议按需调大。

## 5. Definition of Done

- 三项修复落地，5 个新测试通过；全量回归绿；本切片文档落地。
