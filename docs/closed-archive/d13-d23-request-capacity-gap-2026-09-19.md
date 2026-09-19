# D13 / D23 完整模型请求容量检查缺口

日期：2026-09-19。编号：D13-D23-006。

## 状态与范围

检查基线为本地 HEAD `2d42746`，与已有 `origin/main` 引用一致；未执行 fetch。核心文件 `src/koawa_agent_v2/execution/loop.py` 在检查时没有本地修改。工作区存在其他会话的文档归档和源码改动，本文件不代表那些改动已提交或经过验证。

本项已静态确认，并完成指令漏计及缺少 recorder 时提前返回的函数级验证。尚未复现真实 provider 超窗、计费损失或任务中断。本文件只记录问题，不构成修复方案或实施授权。汇总入口仍为 [切片阅读问题台账](d12-i7-receipt-reuse-followup.md)。

## 结论

当前 Run 内字符预算检查只覆盖部分消息文本，没有覆盖完整模型请求。系统指令和工具定义被漏计；硬上限检查还依赖压缩路径是否继续执行。因此，本地软硬阈值检查通过，不能证明完整请求符合预期容量限制，也不能证明请求满足实际模型的 token 窗口。

## 1. 哪些内容被统计

`AgentLoop.context_chars()` 位于 `src/koawa_agent_v2/execution/loop.py`，约 314 行。它累计以下字段的 Python 字符串长度：

| 消息类型 | 被累计内容 |
|---|---|
| UserMessage | content |
| AssistantMessage | item.text |
| ReasoningSummaryEcho | item.summary |
| ToolCallEcho | item.arguments_json |
| ToolResultMessage | content |

没有处理 `InstructionMessage`，也没有接收或统计工具定义。该函数并非完整请求的序列化长度计算；Python `len(str)` 也不等于 UTF-8 字节数或模型 token 数。

实际调用顺序约在同文件 613 行：

```text
_maybe_compact(context)
→ 获取本轮工具目录快照
→ 取得 tool_definitions
→ 构造 ModelRequest
→ 调用模型
```

这说明本轮工具描述和参数 schema 尚未参与前面的容量判断。ModelRequest 校验消息类型、工具定义及工具调用配对，不因此补足完整请求容量计数。

## 2. 硬上限判断可以被提前跳过

`_maybe_compact()` 在以下条件下会在其预算判断前返回：

- memory 配置不存在；
- `in_run_compaction_enabled=False`；
- 没有绑定 compaction sink；
- durable 工具执行路径中仍有 pending tool calls。

因此该函数把自动压缩与硬上限检查耦合在一起。关闭压缩或缺少 sink，不能被描述为“仍无条件执行同样的硬上限检查”。

pending 分支后仍可能由工具配对协议校验拒绝请求；本项不声称开放调用一定发送成功。正式 worker 正常绑定 recorder，也不能消除指令和工具定义漏计这一独立问题。

## 3. 已执行的最小验证

使用 Python 3.13，在 `PYTHONPATH=src` 下运行以下函数级验证；不调用真实模型，不创建生产执行副作用：

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
print(loop.context_chars(items))
print(loop._memory.request_context_hard_chars)
loop._maybe_compact(items)
```

观察结果：

```text
context_chars = 5
request_context_hard_chars = 64000
_maybe_compact 在没有 sink 时直接返回，没有抛出异常
```

前两个数字证明系统指令不参与计数。最后一步单独证明缺少 sink 的提前返回；该最小验证不应被描述为正式 worker 已成功向 provider 发送超限请求。

## 4. 对正常任务的影响

假设会话历史和当前用户请求只有 20,000 字符，但系统指令与工具定义很大。当前字符计数可能仍低于软上限，运行时不触发压缩，后续 ModelRequest 却包含此前未计入的内容。

配置中的固定 reserve 可以留出一定余量，但没有在这段逻辑中证明余量足以覆盖所有指令、工具定义、协议开销和输出预算。实际是否超窗还取决于 provider、模型、分词方式和请求内容。

可能后果包括服务端拒绝请求、任务中断或容量评估失准；这些是风险推断，尚无本次真实 provider 复现。

## 5. 与已读机制的关系

- 近期 session 窗口限制只管理部分历史，不能代替完整请求容量检查。
- Run 内执行组压缩能减少部分消息，但不能弥补被漏计内容。
- 工具调用配对校验保证协议结构，不证明容量合规。
- 缓存命中降低部分处理成本，不减少模型逻辑上需要容纳的上下文内容。
- 设计文档 D23 要求完整 MemoryEnvelope 在请求构造前经过协议、字符/字节预算和工具配对验证；当前实现与完整预算要求存在差距。

## 6. 后续决策边界

本项属于容量管理与长任务可靠性缺口，不是已经证实的审批绕过或数据泄露。按阅读约定，完成 D13 及增强链后，再统一评估完整请求预算、压缩、检索、缓存成本和窗口阈值。本次不选择具体实现策略。
