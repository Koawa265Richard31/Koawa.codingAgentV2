# R4 实测计量误差报告（closure review 2026-09-25 验收项）

日期：2026-10-03（实测）；探针 `scripts/r4_metering_probe.py`（只读、8 请求、
密钥仅从环境变量读取、端点 https+公网校验+禁重定向）。原始数据：
`.dsh_tmp/r2r4/r4-metering-probe.json`（未入库，可复跑）。

## 实测配置

- 模型：`deepseek-ai/DeepSeek-V4-Flash`（SiliconFlow，便宜档）
- 计量对照：loop 全量估算 `_request_estimate`（上下文字符 + 工具
  name/description/schema + 每项 48 字符协议开销）vs `usage.prompt_tokens`
- 形状：纯 ASCII / 中英混排 / 12k / 60k 上下文 / 单工具 schema / 8 工具
  目录 / 8 工具+中文 / 60k+8 工具

## 结果（chars/token = 估算字符数 ÷ 实测 prompt tokens）

| 形状 | 估算字符 | 实测 tokens | cached | chars/token |
| --- | ---: | ---: | ---: | ---: |
| small-ascii | 1,342 | 290 | 0 | 4.628 |
| mixed-cjk | 1,348 | 434 | 0 | 3.106 |
| medium-context | 12,122 | 2,754 | 0 | 4.402 |
| large-context | 60,142 | 13,730 | 2,048 | 4.380 |
| one-tool-schema | 2,067 | 710 | 0 | 2.911 |
| schema-catalog | 7,142 | 2,103 | 0 | 3.396 |
| schema-catalog-cjk | 7,148 | 2,247 | 0 | 3.181 |
| large-context-plus-schema | 65,942 | 15,543 | 0 | 4.243 |

## 结论与预算选择

- 观测区间 **2.911–4.628 chars/token**；最差（token 最密）出现在
  工具 schema 密集形状（JSON 结构 + 标识符 token 化）。
- 安全换算（字符门限 → token 上界）取 **2.5 chars/token**（观测最小值
  再留 ~14% 余量，覆盖未见过的更密形状）：`tokens ≤ chars / 2.5`。
- 由此验证现行配置的保守性：默认 hard 64,000 字符 ⇒ 最坏 ~25.6k
  prompt tokens（DeepSeek-V4-Flash 128k 窗口下安全）；smoke 长任务档
  176,000 字符 ⇒ 最坏 ~70.4k tokens + 输出预留，仍在窗口内。
- 输出预留 `4 × max_output_tokens` 字符 ⇒ 最坏 `1.6 × max_output_tokens`
  tokens，方向保守成立。
- 协议开销 48 字符/项的假设：与 tool-schema 形状的整体误差被 chars/token
  区间吸收，无需单独修正；CJK 场景由 2.5 下界覆盖。

## 局限（如实声明）

- 单模型、单 provider、8 个合成形状、一次运行；未覆盖代码混合、超长
  单行、稀有 token 等形状；cached tokens 只在 large-context 出现，
  缓存命中不改变 prompt_tokens 总量口径。
- 该报告把门的估算从"纯猜测"升级为"有实测误差带"；门的单位仍是字符，
  不是精确 token 计量。换模型/换 provider 时必须复跑探针更新区间。
