# R4 回收带读单元（讲授完成 2026-10-16）

命题一句话：**R1-R3 是安检（内容合不合规），R4 是值机称重（行李物理上
装不装得下）**。登机前最后一刻全量上秤，不管之前走过什么路径。

## 真实场景

模型服务有物理窗口（DeepSeek-V4-Flash = 128k tokens）。每轮请求 =
全部上下文 + 全部工具定义（name/description/schema 都随行）。超窗口 →
provider 400 → turn 死在别人家地盘。修复前三洞：只开压缩才称重（免检
通道）；只称 schema 漏 name/description；压缩后只称部分。

## 材料与类清单（loop.py）

| 对象 | 行号 | 职务 |
|---|---|---|
| `AgentLoopLimits` | 177 | 预算表：soft（触发压缩）/ hard（绝对上限，默认 64,000 字符）/ reserve（输出预留）/ compaction_target / keep_groups |
| 常量四枚 | 220-226 | 每项协议开销 48 / 每工具 48 / 未配置兜底 hard 2M / 兜底预留 8k |
| `_definitions_chars` | 515 | 工具秤：name + description + schema + 48/工具（旧版只称 schema = 漏称） |
| `_request_estimate` | 532 | 总秤：上下文 + 工具定义 + 48×条数；docstring 自认"保守估算非验证 token 计量" |
| `_assert_request_fits` | 547 | **终检门**：ModelRequest 构造前、对最终消息列表 + 同一 pinned 工具快照；压缩/sink/可压缩组三无关；超 → `request_capacity_exceeded` |
| `AgentLoopError` 两声哨 | 44+ | 家门口的 `request_capacity_exceeded` vs 压缩循环的 `context_capacity_exhausted`（fail closed） |
| compaction 分工 | 370-509 | 软线执行者：锚点保护（F20，压缩批次不跨 instruction/user 锚）；压缩后仍超 → exhausted |

## 实测报告人话版（r4-measured-metering-report.md，2026-10-03）

秤是字符秤，限重单位是 token → 汇率问题。探针 8 种行李真实过磅：
汇率区间 **2.9~4.6 chars/token**（schema 形状最费）。预算取 **2.5**（比
最差观测再严 14%）：64,000 ÷ 2.5 = 最多 25.6k tokens < 128k，安全。
取比最差更严的数字 = 错误永远落在安全侧（宁家门口误报，不 provider 爆炸）
—— R2 fail-closed 的同款哲学。局限：单模型单 provider 8 形状一次运行；
**换模型 = 汇率作废，必须复跑探针**。

## 检查点（已答）

Q1 ✓ 旧秤漏称 name/description；最大漏重场景 = 手册进 description
（20k 字符描述 + 200 字符 schema = 漏称 99%）。
Q2 已讲授（方向性：估算须 ≥ 真实才安全）。
延伸题（已由讲授收尾）：换真 tokenizer 精确计量——好 = 无需保守汇率；
贵 = tokenizer 按模型绑定（换模型换依赖），字符秤模型无关（换模型只复跑
探针）。选可移植的保守而非绑定的精确。

## 收尾

①冷稿 R2+R3 连讲仍欠；②博客 R4 节素材 = 实测校准故事（探针 + 汇率带 +
诚实局限）——这是"工程性"最强的一节；③R5 单元待备料。
