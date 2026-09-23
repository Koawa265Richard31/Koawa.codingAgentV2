# F20 忠实形态诊断结果（2026-09-20）

run11 seed 精确形态（含旧 assistant 项）+ 12 轮 read_file + 真实 durable worker：

- **映射对齐不变量成立**：连续 9 次 intended+compacted 全部成功，无 `compaction_source_range_missing`——同 run 多次压缩的索引↔版本映射在 run11 seed 形态下正确；
- **真实失效模式是累积天花板**：9 个替换块（每块约 400-600 字符）作为不可压缩边界线性累积，+ keep_recent=3 尾部组，超过 hard(4000) → `context_capacity_exhausted`；
- **范围收窄**：`compaction_source_range_missing`（run11 实际死因）未被本形态复现——差异变量指向**真实模型轮次投影的 ReasoningSummaryEcho / assistant 文本组合**（脚本轮无 reasoning 项），需以真实模型轮或忠实 reasoning 投影再复现；
- **结构性结论（与检索设计互证）**：替换块累积天花板是 bounded-result-retrieval 提案要解决的"替换块回不来/越压越大"问题本身——检索能力即是该天花板的正解，F20 的 range_missing 分支仍需单独立案定位。

WP-A 基线测试（test_f20_second_compaction.py）维持钉扎：终态诚实 + 线程可用。
