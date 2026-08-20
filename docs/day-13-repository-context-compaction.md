# D13：Repository Context 与 Compaction

## 1. 完成边界

已实现：

- `RepositoryIndex`：用 `git ls-files` 尊重 `.gitignore`；include/exclude 正则；
  max_files/max_file_bytes/max_total_bytes 限制；NUL 检测二进制（检索阶段跳过）；
  per-file SHA-256 与 size；`is_stale` 在文件变化后使旧 hash 失效。
- `ContextRetriever`：字面命中分数 + 路径长度加权，确定性排序/去重；snippet 有界
  （行数与字符数）；输出按 `ContextBudget` 截断；仓库文本一律标记 untrusted。
- `Compactor`：system/developer 指令原样保留；summary 是显式
  `[untrusted-model-summary]`；权威执行状态（goal/constraints/changed files/test
  evidence/pending approval/unknown outcome/active children/budget）由 typed
  projection 确定性拼接，绝不由 summary 推断；未闭合 ToolCall 拒绝压缩
  （`unresolved_tool_call`）。
- `rebuild_after_restart`：重启重建 = untrusted summary + authoritative
  projection + tail events，输出确定。

## 2. 稳定错误码

`index_file_limit_exceeded`、`index_total_bytes_exceeded`、`git_index_failed`、
`unresolved_tool_call`、`empty_compaction_summary`。

## 3. 测试与示例

- `tests/test_d13_context_compaction.py`（5）：ignore/限制、stale 失效、检索排序
  去重、compaction 权威状态保留 + 未闭合调用拒绝、重启重建确定性。
- `examples/day13_context_compaction.py`：fixture 仓库索引 → 预算检索 → 修改后
  stale 检测 → compaction 与重启重建。

聚焦命令：

~~~powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = 'src'
python -W error::ResourceWarning -B -m unittest tests.test_d13_context_compaction -v
~~~

## 4. 推迟到后续日的风险

- vector DB / 语义检索（本日从 tree + literal search 开始）。
- 自动 snapshot 生成（当前依赖 git 已提交状态）。
