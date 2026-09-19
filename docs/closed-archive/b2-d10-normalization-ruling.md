# B-2 裁决备忘录：D10 binding 测试期望 vs D25 schema 规范化

PSEC 轨道 / 阻塞 B-2 的裁决材料。日期：2026-09-06。基线：a9d8896。**本备忘录只提供证据与建议，不做代码变更**（B-2 已按归属纪律登记待裁决）。

## 0. 建议（TL;DR）

**方向 (b)：修 docstring + 修测试期望 + 文档化翻转语义；不改规范化代码。** 理由：代码行为与 D25 v1.3 的书面设计意图（"强制 `additionalProperties:false`（只严不松）"，`docs/day-25-third-party-mcp-security-governance.md:461-464`）一致，失准的是函数 docstring 和 D10 旧期望；反向修代码会重新打破 88350e8 特意修复的官方 server 绑定链路。

## 1. 根因链（全部实证）

1. D10 时代（80a901b，2026-08-21）的 `test_invalid_tools_are_rejected` 期望：裸 string schema 与 `additionalProperties: True` 均应抛 `unsupported_mcp_schema`。
2. D25 v1.3（88350e8，2026-09-04）为修复"官方 server 目录无法绑定"引入 `_normalize_third_party_schema`：剥约束无关元关键字、注入保守默认边界、**无条件 `normalized["additionalProperties"] = False`**、`required` 缺省归一 `[]`。
3. 两个失败 subTest 均为该边界迁移的漏网：裸 string 被保守默认边界救活（与 rtj 阻塞 #3 同类迁移——当时 T3 毒样已迁、D10 期望漏迁）；`additionalProperties: True` 被翻转后通过。
4. **发现一处实现/文档矛盾**：函数 docstring 写"only *stricter* defaults are introduced (absent `additionalProperties` becomes `False`)"，而代码是无条件翻转（显式 `True` 也被翻）——docstring 低估了实际语义。但 D25 文档 :463 的书面意图就是"**强制**（只严不松）"，即代码符合 D25 设计意图、docstring 不符合代码。
5. 确定性：standalone 在 py -3.13 / py -3.14 双解释器复现；src 自 88350e8 零改动；与 PSEC/T7 变更无关（本轨零 src 变更）。

## 2. 两个方向的风险对照

| | 方向 (a)：改代码——仅对缺失键取默认，显式 `True` 拒绝 | 方向 (b)：改 docstring + 测试 + 文档化 |
|---|---|---|
| 与 D25 书面意图 | 相悖（D25 :463 明文"强制…只严不松"） | 一致 |
| 官方 server 绑定链路 | **高风险**：88350e8 专为打通官方目录而引入规范化，若官方 server 显式声明 `True` 将重新无法绑定，D25 双平台证据链需重验 | 无影响 |
| 安全方向 | 拒绝 = 装配期显式暴露语义分歧（更"诚实"但更脆） | 运行时永远比第三方声明更严（只收 `properties` 内参数），方向安全；代价是"静默覆盖第三方显式声明"需被文档正名 |
| 改动面 | src 行为变更，需 S 判定 + 全量重验 D25 lane | docstring 一句 + 测试两期望 + 记录；无 src 行为变更 |

## 3. 若采纳方向 (b)，最小修复清单（归属 D25/D10 owner 执行）

1. `_normalize_third_party_schema` docstring 改为如实描述："`additionalProperties` 无条件强制为 `False`（含显式 `True`）——运行时不接受第三方扩大参数面的声明；其余仅对缺失键引入更严默认"；
2. `test_invalid_tools_are_rejected` 两处过期期望迁移：裸 string 换成真正子集外形态（沿用 rtj 阻塞 #3 对 T3 毒样的同一技术：`pattern`/`number`/`enum`）；`additionalProperties: True` 从"拒绝"断言改为**翻转语义钉定断言**（显式 True 经规范化后绑定成功，且编译后 schema 的 additionalProperties 为 False）——钉住"静默覆盖"这个事实本身；
3. D25 文档 §15 或 T3 对策行补一句翻转语义说明（引用不改威胁模型，T3 对策行已有"严格解码"表述可覆盖）；
4. 全量回归后关闭 B-2。

## 4. 诚实边界

- 本备忘录基于 a9d8896 静态阅读与探测；未验证官方 `@modelcontextprotocol/server-filesystem` 实际声明是否含显式 `True`（若后续验证发现官方 server 依赖非 False 语义，方向 (a) 重新上桌）；
- 建议不等于裁决；B-2 的最终修复与关闭由 D25/D10 owner 执行。
