# D2：可信模型流与 Agent Loop

D2 完成的不是“再写一个 `while` 循环”，而是 Coding Agent 最关键的模型执行边界：把 Provider 的流式输出转换为本地可验证事实，只在整次 response 合法结束后执行工具，再把结果送回下一轮模型，最终由 D1 持久化 Turn 终态。

一句话描述这条闭环：

```text
D1 QUEUED Turn
  -> TurnWorker 生成 fenced Run
  -> ModelClient 输出 typed events
  -> ModelStreamAssembler 完整校验 ModelTurn
  -> AgentLoop 执行完整 ToolCall
  -> ToolResult 进入下一轮 context
  -> 合法 STOP + final text
  -> D1 COMPLETED / Thread detach
```

## 1. 这一日新增的职责

| 文件 | 职责 | 不负责什么 |
| --- | --- | --- |
| `src/koawa_agent_v2/model/protocol.py` | Provider-neutral 请求、完成态输出、上下文包装与 typed stream 事件 | HTTP/SSE、工具执行、数据库 |
| `src/koawa_agent_v2/model/stream.py` | 校验并聚合整条事件流，产出唯一合法 `ModelTurn` | 调 Provider、执行工具 |
| `src/koawa_agent_v2/model/openai_client.py` | 用标准库调用 `/chat/completions`，把 SSE 映射为 canonical typed events | 自动重试、业务工具 |
| `src/koawa_agent_v2/execution/loop.py` | 有界执行“模型 → 工具 → 模型”，构造下一轮上下文 | D3 Tool Registry、持久化 checkpoint |
| `src/koawa_agent_v2/execution/worker.py` | 把 Loop 接到 D1 的 `ThreadRuntime`，用 `run_id` fence 提交终态 | 跨崩溃恢复模型上下文 |
| `examples/day02_streaming_agent_loop.py` | 离线跑通两轮模型、一次工具和真实 SQLite 生命周期 | 生产 Provider、生产工具 |

建议按上表从上到下读。先看“允许出现什么事实”，再看“怎样校验事实”，最后看“怎样驱动它”。

## 2. 模型协议解决了什么

### 2.1 请求不是松散的字典

`ModelRequest` 只接收明确的上下文类型：

- `InstructionMessage`：可信的 system/developer 指令；
- `UserMessage`：有稳定输入 ID 的用户消息；
- `AssistantMessage`：上一轮已经完成的 assistant 文本；
- `ReasoningSummaryEcho`：Provider 明确公开、允许回显的摘要，不是隐藏思维链；
- `ToolCallEcho`：上一轮已经完成的工具调用；
- `ToolResultMessage`：用 `ModelCallRef(model_turn_id, call_id)` 精确关联调用的结果。

这样做的重点是上下文白名单。Provider 原始 JSON、凭据、半截参数、未知输出和隐藏推理不能因为“反正都是 dict”而混进下一轮。

`ToolDefinition` 的 schema 和 `ToolCallItem.arguments_json` 都必须是严格 JSON object。重复 key、`NaN`、数组顶层和畸形 JSON 会在工具执行前被拒绝。

### 2.2 delta 只是预览，completed 才是事实

一条成功流的基本生命周期是：

```text
TurnStarted
  -> ItemStarted
  -> ContentDelta / ToolArgumentsDelta（0..n 个）
  -> ItemCompleted（权威完整快照）
  -> UsageReported（可选）
  -> TurnCompleted（唯一成功 terminal）
```

失败流以 `StreamFailed` 结束。没有 typed terminal 的 EOF 不是“差不多完成了”，而是 `unexpected_stream_eof`。

`ModelStreamAssembler` 会检查：

1. `sequence` 必须从 0 连续增长；
2. 同一流的 `model_turn_id`、provider 和 response ID 不能变化；
3. Item 必须先 start，delta 只能写入仍打开且身份匹配的 Item；
4. `ItemCompleted` 的完整内容必须与此前所有 delta 拼接结果完全一致；
5. canonical index 必须从 0 连续排列，item ID 与 call ID 不能重复；
6. `TurnCompleted` 的完整快照必须与已经关闭的所有 Item、usage 和模型身份一致；
7. terminal 之后不允许再出现事件；
8. 事件数、Item 数、文本、工具参数和总内容都有硬上限。

最重要的安全结论：**Loop 会消费和校验完整条流，才拿到 `ModelTurn`。即使前面的 Tool A 已经收到 `ItemCompleted`，只要后面的 Tool B 或 terminal 非法，Tool A 也不会执行。**

## 3. Agent Loop 怎样形成闭环

`AgentLoop.run()` 每轮做五件事：

1. 从 D1 `run_id + model_round` 确定性派生 `model_turn_id`；
2. 用当前 context 和工具定义构造 `ModelRequest`；
3. 完整消费 `ModelClient.stream()`，通过 assembler 得到 `ModelTurn`；
4. 若 finish reason 是 `TOOL_CALLS`，先一次性验证所有工具名和预算，再按 canonical 顺序执行；
5. 把完成态 assistant/tool call 与对应 `ToolResultMessage` 追加到 context，进入下一轮；若是合法 `STOP`，返回非空 final text。

Loop 有三层预算：最大模型轮数、最大工具调用数、累计模型文本字符数。流本身还有独立的事件/Item/正文/参数上限。预算耗尽时 fail closed，不会用半截结果假装成功。

工具错误有两类：

- 工具正常执行但业务失败：执行器返回 `ToolExecutionResult(..., is_error=True)`；这个结果仍会进入下一轮，让模型根据失败信息调整计划；
- 执行器自身崩溃或违反返回协议：Loop 以稳定错误 `tool_executor_failed` / `invalid_tool_executor_result` 失败。

取消是协作式的：Loop 在模型事件之间和工具调用前检查 `CancellationToken`。真实 SSE 客户端还把同一个 progress guard 下沉到每条 wire line，因此只有 heartbeat、没有 canonical event 时也能发现取消或 D1 Run 失权；单次阻塞读取由 socket timeout 约束，整条响应另有 `max_stream_seconds` 总 deadline。

## 4. D1 与 D2 的接缝

`TurnWorker.execute()` 是控制面与执行面的接缝：

1. 先检查调用者提供的 `expected_version`；过期版本在调用 Provider 前就失败；
2. 对新 Turn，把可信 instructions 与 D1 持久化的 `user_input` 组装成初始 context；
3. 每个物理 dispatch 生成唯一启动命令；两个 Worker 即使同时读到 QUEUED，也只有一个能通过精确版本写入 `start_turn` 并得到本次 attempt 的 `run_id`；
4. 每轮 Provider 请求前、每次 ToolExecutor 调用前，以及真实 SSE 的 wire progress 处，重读 D1 Turn，核对 version、RUNNING 状态和 `run_id` ownership；
5. 把 `run_id` 传给 Loop，并在 `complete_turn` / `fail_turn` 时继续作为 fencing token 提交；
6. D1 在同一事务中写 Turn 终态和 `thread.turn-detached`，释放 Thread 的活跃 Turn。

如果旧 Worker 返回得很晚，而 Turn 已被其他命令取消或改版，精确 stream version 与 `run_id` fence 会拒绝旧结果。旧 Worker 不能把外部已经提交的终态覆盖成成功。

已知 D2 错误会转换为可持久化结果：

| 情况 | D1 结果 |
| --- | --- |
| 合法 `STOP` 且 final 非空 | `COMPLETED`，`outcome=final_text` |
| typed stream / protocol / Loop 错误 | `FAILED`，`error=d2:<stable_code>` |
| 协作取消或 `StreamFailureKind.CANCELLED` | `CANCELLED`，保留稳定错误码 |
| 版本过期或旧 run 提交 | 抛出 `WrongExpectedVersion` / fence 冲突；保留已存在的新状态 |
| 进程被杀死或未分类异常 | 当前 D1 Turn 可能保持 `RUNNING`；D6 才补齐模型上下文 checkpoint 与中途恢复 |

D2 不接受调用方传入任意 `input_items` 覆盖 durable 用户目标：首次 attempt 只能使用配置的可信 instructions 与 D1 `user_input`；恢复 attempt 在 D6 的可信 rehydrator 落地前统一抛 `durable_context_unavailable`。

## 5. 运行离线闭环

在 `D:\KoawaAgent\v2` 下执行：

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = "src"
python -B examples/day02_streaming_agent_loop.py
```

示例会：

1. 创建真实文件型 SQLite Event Store；
2. 创建 D1 Thread 与 QUEUED Turn；
3. 第一轮脚本流产生一个分片的 `read_note` ToolCall；
4. 演示执行器返回只读结果；
5. 第二轮确认 `ToolResultMessage` 已进入 context，再产生分片 final；
6. Worker 提交 `COMPLETED`；
7. 新建 Store/Runtime 实例，从 SQLite 重放 Turn，并确认 Thread 已 detach。

示例里的 `ScriptedModelClient` 和 `ReadNoteExecutor` 只用于离线展示协议边界，不冒充真实 Provider 或 D3 Registry。

## 6. 使用真实 OpenAI-compatible Client

适配器只依赖 Python 标准库。`base_url` 传 API 根路径，客户端会追加 `/chat/completions`：

```python
import os

from koawa_agent_v2.execution.loop import AgentLoop
from koawa_agent_v2.model.openai_client import OpenAICompatibleChatClient
from koawa_agent_v2.execution.worker import TurnWorker

client = OpenAICompatibleChatClient(
    base_url="https://api.openai.com/v1",
    api_key=os.environ["OPENAI_API_KEY"],
    provider="openai_compatible",
    timeout_seconds=60,
    max_stream_seconds=300,
)
loop = AgentLoop(client, tool_executor=my_tool_executor)
worker = TurnWorker(
    runtime,
    loop,
    provider="openai_compatible",  # 必须与 client 的 provider 一致
    model="your-chat-completions-model",
    tools=my_tool_definitions,
)
```

真实客户端有这些边界：

- 每次 `stream()` 只发送一个 HTTP 请求；
- 配置 API key 时强制 HTTPS；默认 transport 拒绝所有 3xx，不会把 `Authorization` 跟随到另一个 URL；
- 只接受 `text/event-stream`，并限制请求、响应、单个 SSE event 的字节数和整条流的总耗时；
- 支持 assistant text、多个可交错的 `tool_calls[index]`、finish reason 与 usage；同一回合 text/tool_calls 的 canonical 顺序都能投影到下一轮；
- ToolResult 在 Chat wire 上使用 `{is_error, content}` JSON envelope，业务失败标志不会丢失；
- 只有收到 `[DONE]` 并完成全部校验后，才发出 canonical completed/terminal 事件；
- 不把 API key、原始响应正文或半截工具参数放进异常；
- **不自动重试**。Provider 没有承诺重试后复用相同 response/call identity，擅自重试可能重复生成调用。

工具定义可以发送给真实 Provider，但 D2 只有 `ToolExecutor` 端口。生产级 schema 校验、权限、工具注册与实际文件/命令工具属于 D3。

## 7. 验收

运行全量测试：

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = "src"
python -B -m unittest discover -s tests -v
```

D2 的验收重点不是“模型能吐字”，而是下面这些性质同时成立：

- typed stream 正常路径可以产出唯一完整 `ModelTurn`；
- sequence、identity、Item 生命周期、delta/snapshot、terminal 任一违规都会失败；
- 整条 response 校验完成前绝不执行工具；
- 多个工具会先整体校验，再执行第一个；
- 工具结果用 `ModelCallRef` 精确回填，并能驱动下一轮 final；
- 轮数、工具数、内容大小和取消均有明确边界；
- Worker 成功/失败/取消能提交 D1 终态并 detach Thread；
- stale Worker 不能覆盖更新后的 Turn；
- OpenAI-compatible SSE 映射、usage、交错工具、HTTP/流失败均有测试。

## 8. 现在不能声称什么

D2 已经是可信模型调用闭环，但还不是完整 Coding Agent。边界必须讲清楚：

1. **没有 D3 Tool Registry 和真实 coding tools。** 当前只有 `ToolExecutor` 接口；还缺统一注册、schema 校验、权限策略，以及读写文件、搜索、执行命令等工具实现。
2. **没有 D6 durable model context/checkpoint。** `ModelTurn`、Loop context 和工具结果仍是进程内对象。崩溃在 Loop 中途时，D1 能看见 `RUNNING` 控制状态，但不能从最后一轮模型位置无损续跑；D2 不开放任意 context 注入，恢复 attempt 会以 `durable_context_unavailable` 拒绝猜测性重跑。
3. **没有 D7 exactly-once tool ledger。** 当前 ownership guard 能挡住“到达工具边界前已经失权”的旧 Worker，但 guard 查询与真实副作用之间仍有不可消除的 TOCTOU；并且进程可能崩溃在“工具已执行、结果未记录”窗口。D7 必须把 active-run 校验与 ledger claim 原子化，执行器只消费已 claim 记录。因此现在不能宣称强取消或工具 exactly-once。

面试时可以这样概括 D2：

> 我把 Provider 流先规范化为带连续序号和稳定身份的 typed events，用严格状态机验证 Item 生命周期、delta、usage、完成快照和 terminal。Agent Loop 只有拿到完整且 provider 身份匹配的 ModelTurn 才会批量校验工具并执行，再按 call identity 回填结果。TurnWorker 用 D1 的精确版本、唯一物理 dispatch、run fence 和副作用前 ownership guard 保护执行权。这个阶段仍明确不声称跨崩溃上下文恢复、强取消或工具 exactly-once，它们分别由后续 checkpoint 和原子 side-effect ledger 解决。
