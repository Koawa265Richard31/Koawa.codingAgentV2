# D14：Trace、Evaluation 与 Failure Injection

## 1. 完成边界

- `TraceStore`：全链 correlation id；stream allowlist（thread/turn/run/model/
  tool/ledger/mcp/sandbox/subagent）；字段 allowlist；落盘前 `redact_json_value`
  脱敏，raw secret/正文绝不先落盘。
- trace 已接入生产路径：`LedgerExecutor` 发出 tool/ledger 探针、`McpSession.call`
  发出 mcp 探针、`AgentLoop` 发出 model 回合探针（`trace_store`/`correlation_id`
  可选注入，缺省不产生副作用）。
- `FaultInjector`：12 个命名 failure point；seed/script 确定性触发；稳定失败
  分类（timeout/concurrency/policy/uncertainty/contract/other）。
- `evals/run_eval.py`：20 个固定任务（fresh repo + 注入 runner + oracle 校验 +
  测试命令）；mandatory 用 deterministic scripted provider；输出 JSON 报告与
  失败分类，不用模型 final 自证。
- `examples/day14_trace_failure_replay.py`：trace 往返 + 故障注入重放。

## 2. 测试与命令

~~~powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = 'src'
python -W error::ResourceWarning -B -m unittest tests.test_d14_trace_fault_eval -v
python -B evals/run_eval.py evals/tasks evals/report.json
~~~

`tests/test_d14_trace_fault_eval.py`（3）：脱敏/allowlist、注入确定性/分类、eval
报告。当前 20/20 任务成功，失败分类为空（未注入）。

## 3. 推迟到后续日的风险

- 真实模型 opt-in 多次运行与 A/B（需凭据）。
- `FaultInjector` 仍是独立模块，尚未挂到运行时的各失败点；subprocess 级 kill
  Worker 的 crash-window 矩阵由 D6/D7/D8 fixtures 覆盖，D15 golden E2E 组合其一。
