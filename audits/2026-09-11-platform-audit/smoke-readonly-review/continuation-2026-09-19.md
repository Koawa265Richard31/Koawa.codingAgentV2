# 冒烟续跑记录（2026-09-19，6 元预算）

| 运行 | 轮/工具/压缩 | 命中率 | 终态 |
|---|---|---|---|
| run10 | 64/101/18 | 82.3%（1.55M 输入） | `d2:openai.malformed_sse_json`（服务商流完整性错误，非运行时） |
| resume | — | — | `turn_already_terminal`（CLI resume 不接管已干净失败的 turn——语义如设计，但"失败后续跑"缺用户入口，已记录） |
| run11（同线程新 turn，携带历史重建上下文） | 若干 | — | **`compaction_source_range_missing` → 新发现 F20** |

## F20（新，P1，实测确认）

带 SessionHistory 前缀的 turn（chat/续跑/同线程新任务）在首次 in-run 压缩时，loop 选出的上下文范围在 recorder 的 `source_versions_for` 中无法映射为版本连续段（recovery/execution.py:824-830）→ `compaction_source_range_missing` → 整个 turn 失败。这是 F12 接线后"生产路径分叉"的又一实例：run10（无历史注入的全新 turn）压缩 18 次全部正常；同线程续跑 turn 一压即死。

根因方向：recorder 播种/版本映射对"注入式前缀上下文"（历史块、journal/plan 提醒、失败回显等非 seed 同源项）的覆盖假设不成立，需对齐 loop 上下文索引与 recorder 版本表的对应关系。修复前，chat/续跑场景的 in-run 压缩实际不可用（回到"有机制无保护"状态，但 fail-closed 诚实）。

## 消耗与状态

本轮 6 元预算用于 run10+resume+run11（合计约 2.2M 输入 token，命中率 82.3%）。余额已尽，冒烟暂停；续跑入口与配置不变（cfg.json + task1/task2）。证据：run10.log、run10-resume.log、run11.log、task2.txt（本目录）。
