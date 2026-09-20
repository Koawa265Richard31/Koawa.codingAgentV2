# F20 更正（2026-09-19，推翻 f20-smoking-gun.md 的"零播种"结论）

更读 seed 事件正确层级（`payload['projection']['context']`，此前误读顶层不存在的 `context` 键）：

- run11 seed 实际含 **6 个上下文项**：instruction、agents note、journal reminder、旧 user、assistant、原始输入——**非零播种**；
- 首次压缩 intended 声明 source 范围 0..18 且 intended+compacted 双事件成功落盘——首次压缩事实层成功；
- 失败发生在**后续压缩**（替换边界已存在后的再一次映射）→ `compaction_source_range_missing`。

修正后的 F20 真实形态：**同一 run 内第二次及以后的压缩**，在"loop 采纳 synced_context 后的新索引"与"recorder 替换后的版本表"之间映射错位。run10 同 run 18 次压缩成功是反例约束——差异变量需在 run10 与 run11 的种子/轮次内容间找（run11 前缀含 session 块与 assistant 对；run10 种子更短）。

WP-A 基线测试应钉住的真实条件：**第二次压缩的批次映射**（采纳 synced_context 后 loop 索引 vs recorder 替换后版本表）。f20-smoking-gun.md 的"零播种"结论作废；F21 修复（CheckpointError 落终态）不受影响，仍然有效且必要。

下窗口定位法：对 run11 事实流跑 reduce_execution 后打印投影 `_source_versions` 全表与两次压缩各自的映射入参出参；并在受控测试中构造"同 run 第二次压缩"直接复现。

## Recorder 级探针补充（同日）

受控 recorder 探针（6 轮、含 reasoning echo、真实 durable 记录）结果：
- `_source_versions` 形态：seed 各项共享版本 0（`[0,0,0,0,0]`），轮次项版本非连续跳跃（`1,1,3,4,4,6,...`）——**版本表不是逐项递增**；
- `parse_closed_groups` 对"无边界连续工具轮"聚成 **1 个大组**——组边界依赖 assistant/user 项；
- 因此 run11 的 intended 范围 0..18 很可能是"**从 seed 项开始的一个巨组**"（首个映射版本 0 = seed 项），第一次压缩即吞掉 seed 前缀；
- 替换后 `_source_versions[start:end]=[head+2]` 单版本替代——**第二次压缩的失败机制**聚焦为：替换后 loop 索引 vs 版本表的映射在"巨组替换+后续追加"下错位（具体断言需在受控探针中再造第二次压缩）。

WP-A 修正后的基线核验清单：
1. 受控场景加 assistant 边界使多组成立 → 复现"第二次压缩"；
2. 钉住替换后 `_source_versions` 与 loop 索引的对齐不变量；
3. `parse_closed_groups` 对巨组/边界的行为契约化（防止吞 seed 前缀——本次 intended 0..18 包含 seed 项本身就是可疑点：instruction/user 是边界不应入组）。
