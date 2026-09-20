# run12 记录（2026-09-19 续）

压缩预算放大后（soft=400k，不触发压缩）turn 在任何模型调用前即 `runtime_error`（空 payload，零 API 消耗）。疑点：与 C6 新加的 marker summaries 接线或 C5 merged 块在 from_thread 重建路径上的交互——需离线诊断 cli.py 启动路径。预算未消耗。F20 与 run12 诊断均已入队。证据：run12.log（本目录）。
