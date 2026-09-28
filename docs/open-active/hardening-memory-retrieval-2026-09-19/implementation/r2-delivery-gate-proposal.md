# R2 切片实施规格：修正版 B——发布门禁的 durable 交付

日期：2026-09-25。状态：**规格第四版（第五轮审阅补充已并入：SPEC-1
通过；SPEC-2/3 方向通过 + 补充约束全部并入），未经批准不得启动实现**。
裁决基础：修正版 B（保留同步诊断工作流），五条约束见"约束总纲"。
本文是下一切片的唯一实现依据；补充约束属于既定唯一交付与安全恢复
契约，不扩大到新工作包。

## 约束总纲（前轮裁决，不变）

1. 交付的是已发布的安全投影派生内容，不是执行路径原回执；执行记录
   不新增敏感原文持久化。
2. 每次调用只有一个首次交付决策（SPEC-1，已通过）。
3. 交付决策写入失败 → 暂停后续模型请求；恢复补决策不重执行工具；
   占位决策落盘后补发成功不改写历史，结果经按引用读取获得。
4. 版本化覆盖恢复与压缩：旧协议不兼容、日志损坏、合法崩溃中间态
   三类分别处理（SPEC-3 状态机）。
5. 历史交付 ≠ 当前读取权限；读取链闭环仍以当前权限检查与生产入口
   验证为前提。

## SPEC-1：delivery 幂等键与请求指纹（已通过，含重放约束）

```
delivery_key := (turn_id, model_turn_id, call_id)
command_id   := uuid5(NAMESPACE_URL, "result-delivery:" turn_id : model_turn_id : call_id)
```

- 幂等键**不含任何内容维度**；内容与决策变化进入指纹并触发
  `IdempotencyConflict`，不产生第二个首次交付决策。
- 进入指纹（payload）、不进入键：delivery 值、error_code（unavailable
  时）、source digest、immutable projection reference（receipt 时）、
  delivery content digest、占位符文案版本号（unavailable 时）。
- **恢复重放约束（审阅第五轮并入）**：恢复时发现该调用已存在 durable
  决策 → 校验指纹一致后**重放原决策**；禁止根据当前投影状态重新计算
  相反决策（补发成功不得翻转已落盘的 unavailable）。
- 验收四条维持：同调用/同内容/同决策幂等；异 source digest 拒绝；
  异 delivery 值拒绝；范围仅限 delivery 决策身份，投影流
  published/unavailable 事件身份不变。

## SPEC-2：三个摘要与统一规范化（方向通过 + 五条补充并入）

统一规范化：单一函数实现于 `retrieval/projection.py`，live 路径与
reducer 共同调用；规则 = 既有 `canonical_json_bytes_v1`（UTF-8、键
字典序、无冗余空白、禁 NaN）。

| 概念 | 计算 | 绑定对象 | 出现位置 |
| --- | --- | --- | --- |
| **source digest** | SHA-256(cn(源回执 JSON，output gate 产物)) | 源执行结果 | 投影事件 body_ref、delivery payload |
| **immutable projection reference** | 见补-1/补-2 | 确切那一条已发布事件（非"最新"） | delivery payload（receipt 时） |
| **delivery content digest** | 见补-3 | 实际交付内容，reducer 重算可校验 | delivery payload |

补充精确定义（审阅第五轮，逐条并入）：

- **补-1（完整流身份）**：projection_ref 必须定位完整流身份 =
  `stream category + stream aggregate ID + event_id + stream_version`；
  aggregate ID 允许由已验证的 turn_id 确定（result-projection 流以
  turn 为聚合），但四个字段缺一不可。
- **补-2（版本字段区分）**：一律使用 `stream_version`（流内位置），
  与事件 `schema_version`（载荷 schema 版本）严格区分；规格与载荷
  字段命名禁止出现含糊的 `event_version`（现有 body_ref 的
  `event_version` 字段实施时更名 `stream_version`）。
- **补-3（交付摘要输入固定）**：delivery content digest 的输入 =
  **实际交付字符串的 UTF-8 字节**。流程固定为：先对投影/占位符做
  规范化得到交付字符串，再对字符串字节计算摘要；**不得再次对字符串
  做 JSON 编码后摘要**（避免双重转义歧义）。
- **补-4（自排除范围）**：只排除被计算摘要**自身**的字段，不递归
  移除载荷中的其他摘要字段（source digest / projection_sha256 保留在
  载荷里参与 delivery content digest 计算）。
- **补-5（持久数据兼容负担）**："工作区未提交"不等于没有持久数据
  兼容负担——旧数据库与 checkpoint 可能已产生。字段更名
  （`content_sha256`→`source_content_sha256`、`event_version`→
  `stream_version`）遇既有数据时**按已定版本拒绝规则显式处理**
  （`protocol_version_mismatch` 拒绝恢复），不静默改写、不删除、
  不做读时兼容猜测。

## SPEC-3：delivery pending 状态机（方向通过 + 四条恢复安全约束并入）

```
fact committed → delivery_pending →(backfill)→ delivery_decided → 可重建/可继续
                     └─(backfill 查询异常/暂时无法核验)→ delivery_paused（显式暂停态，可再恢复）
```

- `delivery_pending` = 合法崩溃中间态（事实已提交、delivery 缺失），
  不认定日志损坏；此状态下含原回执的模型上下文在**生成点**被拒绝。
- 正式恢复入口 = 既有 resume 路径（RecoveryCoordinator），处理顺序
  ①→⑤ 见下；全程不重执行工具、原回执不达模型。

恢复安全约束（审阅第五轮，逐条并入）：

- **补-1（前置校验与所有权，先验后写）**：backfill 任何写入前，先
  校验协议版本、源事实身份、当前恢复执行权（lease/fence）。扫描
  delivery_pending 可先于完整上下文 reduce，但**不得先写后验**。一切
  状态变更走 typed event + 精确 expected stream version + 既有 turn
  fence 约定。
- **补-2（无决策时定位投影）**：delivery_pending 尚无 projection_ref。
  先以**完整调用身份 + source digest** 查找匹配的已发布事件，再绑定
  确切不可变引用（SPEC-2 补-1 四字段）；禁止按"最新"选取；多个
  不一致候选 = 完整性异常（`log_corruption` 族），不替调用方猜测。
- **补-3（不存在与读取失败分离）**：
  - 确认无投影（查询成功且无匹配）→ 允许持久化
    `unavailable / not_published`；
  - 查询异常或暂时无法核验 → `delivery_paused`，**不持久化"未发布"
    的判断**（避免把暂态故障固化为错误结论）；
  - 已有 delivery → 校验后重放（SPEC-1 重放约束），不因后续补发
    改写历史。
- 三类状态区别维持：`delivery_pending`/`delivery_paused`（backfill）；
  `protocol_version_mismatch`（旧协议，拒绝恢复）；`log_corruption`
  族（畸形事实 / digest 重算不符 / durable 决策冲突，停止）。

### 崩溃验收矩阵（三窗口 + 三新增，每项四断言）

基础窗口：W1 事实提交后/发布前（backfill → unavailable/not_published，
补发后不改写历史、按引用可读）；W2 发布后/delivery 前（按补-2 绑定
确切引用 → receipt）；W3 delivery 后/发送前（校验重放，无新事件）。

新增三项（审阅第五轮）：① backfill 查询故障 → `delivery_paused`，
无持久化结论；② 并发恢复竞争 → 单一决策胜出，败者重放（幂等/CAS）；
③ 相同调用不同 source digest → `IdempotencyConflict` 拒绝。

每项断言：正式 resume 可处理；工具执行次数不增；交付决策唯一；
上下文不回流原回执。

## 不可用占位符（固定，逐字节，沿用）

状态码 `projection_unavailable`，`is_error=False`。占位符 JSON：
`{"availability":"projection_unavailable","turn_id","model_turn_id",
"call_id","source_content_sha256","message":<固定文案>}`（字段名随
补-5 更名）。固定文案："本次调用的可读结果暂不可用。此状态不表示
工具执行失败，也不表示未执行。不要仅因结果不可用重试原工具；请使用
结果引用查询，或等待恢复处理。" 引用只含上述字段，不带原日志/
异常原文/路径。

## 实现落点与验收（不变，按第四版规格执行）

实现落点表沿用（recorder `delivery_decided` / 钩子返回决策 / loop
两路径 + 暂停 / reducer 状态机 / 语义版本 / 切片 b 读取链）。验收 =
约束总纲六条 + SPEC-1 四条 + SPEC-2 三摘要独立可验 + SPEC-3 崩溃矩阵
（3+3 项 × 4 断言）。读取歧义修复（`ambiguous_reference`）为已通过
基线。当前权限检查、检索资源限额、生产读取链验证继续作为开放边界。
