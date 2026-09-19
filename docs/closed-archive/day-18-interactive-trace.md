# 切片 B：交互 CLI 工具轨迹与上下文显示

> 状态：COMPLETE（2026-08）。目标：interactive 会话像 DSH 轨迹那样，让用户看到
> 模型每轮调用了哪些工具、结果如何、会话上下文规模。

## 1. 现状与目标

现状：interactive 只打印 agent> 最终回答；模型调了什么工具、成败如何、历史投影
有多大，用户完全不可见（失败时只有晦涩错误码）。

目标（本切片）：
- 实时：模型发出工具调用时立即打印 → name(args)；
- 结果：turn 结束后打印每个工具的 ✓ 成功 / ✗ 失败[错误码]；
NaN

## 2. 实现

### 2.1 实时工具调用（runtime/cli.py）
- 交互循环向 AppRuntime.chat 传入 event_sink（模型流事件通道）；
- 捕获 ItemCompleted 且 item.kind==TOOL_CALL 的事件，按 call_id 记录 name，
  并打印 → name arguments_json（flush 保证实时）。

### 2.2 工具结果轨迹
- 每轮 chat 前记录事件库位置（_store_position）；
- turn 结束后扫描该位置之后的 tool.execution-prepared/succeeded/failed 事件；
  prepared 提供 execution_id→call_id，配合流的 call_id→name 映射还原工具名；
- 失败事件从 result.content 解析稳定错误码（_tool_error_code），打印 ✗ name[code]。

### 2.3 上下文规模
- 每轮打印 history.turn_count 与 history.context_items() 长度（白名单投影规模），
  让用户直观看到会话记忆窗口的占用（阶梯①窗口内全量/②截断的可见化）。

## 3. 测试（tests/test_d16_interactive_session.py +2）

- test_chat_event_sink_sees_tool_calls：scripted provider 调 read_file，断言 event_sink
  收到 TOOL_CALL 完成事件且工具名正确（轨迹数据源契约）；
- test_cli_tool_trace_helper_prints_ok_lines：真实装配 + scripted provider 跑一轮，
  捕获 stdout 断言包含 ✓ read_file。

## 4. 边界

- 工具参数全文会打印（arguments_json）——交互场景面向本机用户，可接受；
- 事件库扫描为 O(n)/轮，演示规模足够，长会话优化留给后续；
- 轨迹仅显示，不影响 canonical 流与持久化。

## 5. Definition of Done

- 交互循环输出 →/✓/✗/ctx 轨迹；2 个测试通过；全量回归绿；本切片文档落地。
