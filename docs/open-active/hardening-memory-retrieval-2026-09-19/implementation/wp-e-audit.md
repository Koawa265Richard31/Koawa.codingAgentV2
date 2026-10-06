# WP-E 检索读取链核验定档表（2026-10-03，基线 4cabb7c，增量 f396bca 纯社区文档）

核验范围：`src/koawa_agent_v2/retrieval/`（recall_tool.py、projection.py、
result_read_tool.py）现状，逐条对照工作包账本 WP-E 节条款。验证分级：
实测确认（有测试名）/ 代码推断（读码无专门测试）/ 未实现。skipped 不计实测。

| 条款 | 定档 | 生产执行点 | 测试 / 缺测试 |
| --- | --- | --- | --- |
| 1. recall 数据源接投影流 | **recall=未实现（仍查会话历史 SessionMemory，session.py:746）**；按引用读取=已实现+已测（result_read_tool → lookup_projection） | recall_tool.py:72-131；result_read_tool.py:92-99 | test_result_read_tool（7 项）；test_recall_history_tool（数据源=会话历史的断言在 _StubMemory 契约） |
| 2. 游标/分页语义 | **部分**：固定 stream_version 升序翻页（不重复、不混淆版本）+ 查询绑定（call_id+model_turn_id 消歧，简写仅唯一匹配）已实现；跨调用续读游标、水位参数未实现（每次 lookup 独立扫描至配额） | projection.py:lookup_projection（版本序 page-walk + 歧义归并） | 歧义/全引用消歧=test_reused_call_id_shorthand；游标续读缺测试（未实现） |
| 3. complete/truncated/availability | **已实现未透传→本轮补全**：lookup 返回 `scan_truncated`（配额命中不冒充全量）；工具响应此前未透传该标志，本轮接线；三态 availability + ambiguous + 撤销/过期/策略错误码齐备 | projection.py:lookup_projection（scan_truncated）；result_read_tool.py（响应字段） | test_scan_truncated_is_propagated_not_silently_full（配额内 vs 外对照：not_found+truncated ≠ 确定性无结果）；test_revocation（verdict 透传） |
| 4. 四路配额 | **部分**：扫描工作量路 ✅（2000 事件/lookup，backfill 对 truncated 转 paused 不下错判）；响应/总读取累计/存储路与跨 turn/恢复不重置的累计计数=未实现。死常量 READ_SCAN_STREAM_QUOTA（定义未用）本轮删除并注明理由（避免虚报保证） | projection.py:READ_SCAN_EVENT_QUOTA + lookup/backfill | 配额路=test_scan_truncated；其余路缺测试（未实现） |
| 5. 旧事件仅审核字段；正文不可索引不提关键词 | **已实现**（构造即保证 + 代码推断）：投影 payload 由 build_published_payload 固定白名单构造；recall 词法池仅 user_input/final_text 元数据（session.py:775 text_pool），tool-result 正文从未进任何索引 | projection.py:build_published_payload；session.py:recall/_recall_score | test_hits_are_metadata_only（正文不出现）；"仅白名单字段"无专门测试（构造保证，代码推断） |
| 6. 验收反例均不回退原始正文 | **已实现+已测（投影流"损坏"专用反例记部分）**：伪造 ref→not_found；歧义→ambiguous_reference（不猜）；权限撤销→revoked 拒绝（test_revocation）；换模型接收方→policy_superseded（test_revocation + 工具层 test_revocation_verdict）；跨线程→cross_thread_read_denied；索引缺失→not_found；交付重复/摘要不符→ReconstructionError（test_delivery_gate）；原始 run-execution 正文无任何回退路径 | lookup_projection 各拒绝分支；result_read_tool cross-thread 分支 | 见左列；投影流载荷级损坏（非交付）无专门反例测试 |

## 结论

- 账本 WP-E 状态行"recall 仍未接投影流——主体未实施"**对 recall 半句仍准确**（本任务不改其未实现状态，只记账）；但**按引用读取半已实施且测试齐备**，账本过时——本轮更新。
- 本轮代码改动（小切片）：工具透传 `scan_truncated`；删除死常量
  `READ_SCAN_STREAM_QUOTA`；补 2 项工具层测试。lookup 语义本身无变化。
- 未实现记账（不实施）：recall 接投影流（含索引/水位/游标续读）、响应/
  总读取/存储三路配额与跨 turn 累计计数、投影流载荷级损坏反例。
- 边界：本任务不声称"WP-E 设计完成"，只定档。
