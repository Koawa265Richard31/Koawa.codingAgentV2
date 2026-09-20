# F20 更正（2026-09-19，推翻 f20-smoking-gun.md 的"零播种"结论）

更读 seed 事件正确层级（`payload['projection']['context']`，此前误读顶层不存在的 `context` 键）：

- run11 seed 实际含 **6 个上下文项**：instruction、agents note、journal reminder、旧 user、assistant、原始输入——**非零播种**；
- 首次压缩 intended 声明 source 范围 0..18 且 intended+compacted 双事件成功落盘——首次压缩事实层成功；
- 失败发生在**后续压缩**（替换边界已存在后的再一次映射）→ `compaction_source_range_missing`。

修正后的 F20 真实形态：**同一 run 内第二次及以后的压缩**，在"loop 采纳 synced_context 后的新索引"与"recorder 替换后的版本表"之间映射错位。run10 同 run 18 次压缩成功是反例约束——差异变量需在 run10 与 run11 的种子/轮次内容间找（run11 前缀含 session 块与 assistant 对；run10 种子更短）。

WP-A 基线测试应钉住的真实条件：**第二次压缩的批次映射**（采纳 synced_context 后 loop 索引 vs recorder 替换后版本表）。f20-smoking-gun.md 的"零播种"结论作废；F21 修复（CheckpointError 落终态）不受影响，仍然有效且必要。

下窗口定位法：对 run11 事实流跑 reduce_execution 后打印投影 `_source_versions` 全表与两次压缩各自的映射入参出参；并在受控测试中构造"同 run 第二次压缩"直接复现。
