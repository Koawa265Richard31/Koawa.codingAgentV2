# D13 / D23 后续缺口记录：请求容量与记忆配置接线

记录日期：2026-09-19。

## 文档边界与冻结约定

按用户要求，旧问题台账 `d12-i7-receipt-reuse-followup.md` 已进入其他会话的处理流程，本阅读会话从现在起冻结对其追加、删改与状态更新。旧台账中已存在的 006、007 记录保留，不迁移删除，不干扰并行处理。

本文接续记录请求容量检查与记忆配置开关两项发现，保留原编号，避免被误认为新增的不同问题。此前独立的 `d13-d23-request-capacity-gap-2026-09-19.md` 保留为容量问题的详细原始记录；后续这两项阅读补充以本文为入口。新增发现也在本文追加，与旧台账的修复工作分开。

检查基线为本地 HEAD `2d42746`，与当时已有 `origin/main` 引用一致；没有执行 fetch。工作区有并行源码修改和文档归档，本文记录的是检查当时的行为，不代表之后的修复状态。本次仅写文档，不实现修复、不制定实施计划。

| 编号 | 问题 | 验证程度 |
|---|---|---|
| D13-D23-006 | 完整请求容量计数缺项，硬上限判断依赖压缩路径 | 静态确认；指令漏计与无 sink 提前返回已做函数级验证 |
| D13-D23-007 | 两个记忆配置开关缺少运行时消费路径 | 静态引用核查确认；未做真实模型开关对照 |

## D13-D23-006：完整请求容量检查缺口

### 实际行为

`src/koawa_agent_v2/execution/loop.py:AgentLoop.context_chars()` 累计 UserMessage 正文、AssistantMessage 文本、ReasoningSummaryEcho 摘要、ToolCallEcho 参数 JSON 和 ToolResultMessage 正文的字符长度。

它没有累计 InstructionMessage，也没有统计工具名称、描述和参数 schema。实际执行顺序是：

```text
_maybe_compact(context)
→ 获取本轮工具目录快照
→ 取得 tool_definitions
→ 构造 ModelRequest
→ 调用模型
```

因此本轮工具定义尚未参与压缩前的容量判断。ModelRequest 的类型及工具调用配对校验不补足容量计数。Python `len(str)` 也不是完整请求 UTF-8 字节数或 provider token 数；固定 reserve 不能证明覆盖全部遗漏项与输出预算。

### 提前返回

`_maybe_compact()` 在 memory 缺失、压缩关闭、sink 缺失或 durable pending calls 存在时，会在其预算判断前返回。不能把这段实现描述为所有路径均有独立、无条件的硬上限保护。

正常正式 worker 会绑定 recorder，仍不能消除指令和工具定义漏计。pending 分支之后可能被协议校验拒绝，不能据此推断开放调用一定被发送。

### 已执行的函数级验证

在 Python 3.13、`PYTHONPATH=src` 下，使用 ScriptedClient 构造 AgentLoop，不调用真实 provider：

```python
from koawa_agent_v2.execution.loop import AgentLoop
from koawa_agent_v2.model.protocol import (
    InstructionMessage, InstructionRole, UserMessage,
)
from koawa_agent_v2.runtime.memory import MemoryConfig
from tests.test_agent_loop import ScriptedClient

loop = AgentLoop(ScriptedClient(), memory=MemoryConfig())
items = [
    InstructionMessage(InstructionRole.SYSTEM, "S" * 100_000),
    UserMessage("u", "hello"),
]
print(loop.context_chars(items))  # 实测 5
print(loop._memory.request_context_hard_chars)  # 默认 64000
loop._maybe_compact(items)  # 无 sink，直接返回
```

该验证证明指令漏计，以及无 sink 分支提前返回；没有证明正式 worker 已发送超限请求，也没有真实 provider 超窗或费用损失复现。

### 影响与边界

当会话正文较小、指令或工具定义很大时，本地阈值可能通过，而实际请求超过容量预期。真实结果取决于模型、provider 与分词方式。

D23 设计要求完整 MemoryEnvelope 经协议、字符/字节预算及调用配对验证。当前实现只检查部分消息文本，属于容量管理和长任务可靠性缺口。不声称审批绕过或数据泄露；缓存命中也不能消除模型逻辑上下文容量的占用。

## D13-D23-007：记忆配置开关缺少运行时接线

### 已核查范围

在 `src/` 与 `tests/` 检索 `conclusion_model_summary` 和 `journal_inject_latest`，引用涉及 MemoryConfig 字段、允许键、类型校验和配置测试，未找到运行时读取它们以切换行为的分支。

核查时 `runtime/memory.py`、`runtime/session.py`、`runtime/cli.py`、`runtime/turn_conclusion.py` 没有本地修改。

### conclusion_model_summary

配置默认值为 False。`TurnConclusionStore.build()` 当前直接设置 `untrusted_summary=None`；DTO 可保存该字段，结论渲染器也能输出已存在的摘要，但正式生成链没有因开关为 True 而调用摘要模型。

须区分另外一条 session 历史摘要路径：CLI 按客户端是否具有 `_endpoint` 注入 `summarize_via_client()`，由 SessionHistory 压缩旧用户请求与最终回复。该路径不读取 conclusion_model_summary。

因此，False 不能被解释为关闭所有摘要模型调用；True 也不能证明 TurnConclusion 已生成模型摘要。这是两类摘要及开关接线的问题，不单独声称费用授权绕过。

### journal_inject_latest

配置默认值为 False。设置 True 后，没有找到相应的读取 SESSION.md 并将正文加入请求上下文的已接入路径。

以下能力彼此独立：

| 能力 | 当前核查结果 |
|---|---|
| journal reminder | 有上下文提醒逻辑 |
| CLI /journal 导出 | 有文件写入与 effect 路径 |
| 最新 journal 正文自动注入 | 未找到该配置开关对应的运行路径 |

配置测试证明 True 可以被解析和保存，不能证明模型最终请求中出现了相应内容。尚未执行真实模型开关对照实验。

## 后续阅读约定

先完成 D13 及增强链核查，再决定压缩、检索、缓存、完整请求预算和窗口阈值。本文只维护发现、证据与验证程度，不自动将候选策略转为实施任务。接手修复时需按最新代码重新核实，避免与已经开始的并行修复重复。
