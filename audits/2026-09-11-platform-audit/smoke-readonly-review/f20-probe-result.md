# F20 探针结果（2026-09-19）

最小复现（SessionHistory 形态前缀：project note + compact block + 旧 turn 对 + journal reminder + 4 个工具组 + 完成轮，soft=250）**未复现**：8 次压缩事件、turn 完成。脚本存 .dsh_tmp/f20-diag 复现参数。

结论：F20 触发面比"任意历史前缀"窄——run11 特有因素候选：① from_thread 装载的**失败回合回显**（error=d2:openai.malformed_sse_json）或**结论块**进入前缀；② 前缀中 `session:compact` 块的 untrusted 标记组合；③ recorder 对含 AssistantMessage（.item.text）前缀项的 context_document 序列化差异。下一步：以 run11 的真实事件库（audits/.../smoke-readonly-review/ 的 state.sqlite3 未归档——需从 D:/A_Dev_Projects/koawa-smoke-review/state.sqlite3 归档）重建精确前缀做差分。
