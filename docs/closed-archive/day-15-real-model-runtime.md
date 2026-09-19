# D15 补强：真实 Provider 装配、审批闭环与运行时配置

> 状态：P0/P1/P2 已实现；P3 文档与本文件同步。
> 目标：把 D15 从“FakeProvider 脚本演示”升级为“真实 OpenAI-compatible 模型走通同一条 durable 主链”。

## 1. 为什么做这一层

D15 原 CLI 的 `run_command` 使用 `FakeProvider`，只调用一个 `write_patch` 并写
`out.txt`。它是确定性契约测试的入口，但不是真实产品入口。面试时若被要求现场
读 `runtime/cli.py`，最容易被追问“模型在哪里”。

本补强保留 FakeProvider 兼容路径用于离线测试，同时新增：

```text
config.json
  -> runtime.config 严格解析（key 只从环境变量读取）
  -> runtime.assembly 组装 OpenAI client + verified registry + policy + ledger
  -> runtime.app 提供 run/resume/status/cancel/doctor/approvals/approve/deny
```

## 2. P0：真实模型装配

新增文件：

- `src/koawa_agent_v2/runtime/config.py`
- `src/koawa_agent_v2/runtime/assembly.py`
- `src/koawa_agent_v2/runtime/app.py`
- `src/koawa_agent_v2/runtime/composite_registry.py`
- `examples/day15_real_model_smoke.py`
- `examples/p0_config.example.json`

主链：

```text
OpenAICompatibleChatClient(SSE)
  -> AgentLoop
  -> LedgerExecutor
  -> PolicyEngine / ApprovalService
  -> CompositeToolRegistry(builtin verified registry + optional MCP)
  -> read/search/apply_patch/run_test_profile/git_status/git_diff/finalize_task
  -> CheckpointStore + TurnWorker + TraceStore
```

配置要点：

- `provider.base_url`：OpenAI 兼容根地址，例如 `https://api.siliconflow.cn/v1`
- `provider.api_key_env`：只写环境变量名，例如 `SF_CodingAgentTestKey`
- `provider.model`：实测推荐 `Qwen/Qwen3.5-35B-A3B`（便宜 MoE，schema 遵循好、快）；
  Qwen3-8B 思考链过长（单轮 2700+ chunks）且工具参数易错，不建议
- `provider.reasoning_effort`：**用户侧推理强度旋钮**（`off|low|medium|high`），运行时按
  provider/model 家族翻译成各自的请求体字段，未知组合 fail-closed（提示改用
  `provider_options`）。实测工具循环建议 `off`：

  | 家族 | off | low/medium/high |
  | --- | --- | --- |
  | siliconflow `Qwen3.5-*` | `thinking.type=disabled` | `thinking.type=enabled` |
  | siliconflow `Qwen3-*`（非 3.5） | `chat_template_kwargs.enable_thinking=false` | `=true` |
  | siliconflow `DeepSeek-*` | `thinking.type=disabled` | `thinking.type=enabled` |
  | openai `o1/o3/o4-*` | 不支持（报 `reasoning_effort_off_unsupported`） | `reasoning_effort=low/medium/high` |

  开 thinking 的真实代价（本轮实测）：Qwen3-8B 单轮 2700+ chunks 思考链几分钟一轮；
  Qwen3.5-35B-A3B 开思考会把答案全写在 `reasoning_content` 里导致空 final answer；
  关思考后同一任务 23 秒完成。agentic 循环的"推理"应交给多轮工具交互，单轮长思考
  反而浪费上下文预算。
- `provider.provider_options`：原始请求体覆盖（JSON 安全值、键名白名单、去重），
  与 `reasoning_effort` 同时设置时以后者为准（可写死任何供应商字段）
- `sandbox.runner=docker` 时 `image_id` 必须是 immutable `sha256:...`
- 每个 `test_profiles[].argv` 在 Docker runner 下必须是 POSIX 绝对路径

## 3. P1：审批闭环

- 配置 `policy.patch_decision = "ask"` 后，`apply_patch` 会持久化 ASK 并让
  Turn 进入 `WAITING_FOR_APPROVAL`。
- 新增命令：
  - `approvals --config cfg.json`：列出 pending 请求
  - `approve --config cfg.json --request-id <id>`：批准并自动 resume
  - `deny --config cfg.json --request-id <id>`：拒绝
- 批准后在执行前仍会重新 resolve action 并重算 digest；资源身份漂移会再次 ASK。

## 4. P2：MCP 配置化

`config.json` 可声明 stdio MCP server：

```json
"mcp_servers": [
  {
    "server_id": "echo",
    "command": ["python", "examples/mcp_fixture_server.py"],
    "request_timeout_seconds": 10,
    "decision": "ask",
    "side_effect_class": "read_only",
    "recovery_mode": "retry"
  }
]
```

每个 binding 仍然固定 `(server_id, session_generation, tool_name, schema_hash)`，
进入 D9 action digest 与 D7 logical execution identity。本地 server 声明中的
`readOnly` 不作为授权依据；`side_effect_class` 与 `recovery_mode` 只能由管理员
配置。

## 5. 实测验证（SiliconFlow，2026-08）

- 模型：`Qwen/Qwen3.5-35B-A3B` + `reasoning_effort=off`（配置层自动翻译为
  `{"thinking": {"type": "disabled"}}`）。
- smoke（`examples/day15_real_model_smoke.py`）：`ok:true`，7~9 轮模型、8 次工具调用、
  23~43 秒，完整走 `list_files → read_file → run_test_profile(红) → apply_patch ✓
  → run_test_profile(绿) → git_status → git_diff → finalize_task ✓`。
- 真实模型暴露并修复的适配层问题：usage 每 chunk 重复（last-wins）、
  `reasoning_content` 增量（忽略）、工具名续传空串（容忍）、finish 后空 delta chunk
  （容忍）；smoke fixture 补齐 `core.fsmonitor/autocrlf/filemode=false` 避免
  GitFacade 基线误判（与测试夹具一致）。
- 安装入口：`py -3.14 -m pip install -e .` 后可用 `koawa-agent-v2 run/status/...`。

## 6. P3：面试演示顺序

1. 离线确定性演示：`py -3.14 -B -m unittest discover -s tests`。
2. 真模型 smoke：`py -3.14 -B examples/day15_real_model_smoke.py`。
3. 真 repo CLI：`py -3.14 -B -m koawa_agent_v2.runtime.cli run --config ...`。
4. 审批闭环：把 `patch_decision` 改成 `ask`，观察 status/approvals/approve/resume。
5. 恢复闭环：运行中 kill 进程，用 `status` 查看，再用 `resume` 接管。

## 6. 明确边界

- 本层不做 TUI，不做浏览器/电脑操作，不做 LSP，不做通用语义向量检索。
- 模型驱动的写子 Agent 仍未接入真 Provider；D11/D12 子 Agent 继续使用脚本
  provider 证明控制面语义，作为显式非目标公开。
- 真实 Provider smoke 是 opt-in 人工命令，CI 仍然只依赖确定性 fake transport。
