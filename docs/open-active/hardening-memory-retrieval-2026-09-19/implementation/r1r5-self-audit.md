# R1–R5 自审表（/goal 完整落实轮，2026-10-03）

对照闭环审查报告（implementation-closure-review-2026-09-25.md）的 R1–R5
验收条款与"完成标准 6 条"逐条自审；证据 = 提交 a1ebde8（已推送）+ 本轮
补全（自审发现 GAP-1/2/3 并当轮实施）。状态分级：✅ 已闭合（有测试/实验
证据）｜🟡 部分（环境/维护者输入约束，边界在案）｜❌ 缺口（本轮补全）。

## R1（投影发布版本冲突/静默中止）

| 验收条款 | 状态 | 证据 |
| --- | --- | --- |
| 同 turn ≥5 结果全部发布 | ✅ | test_five_facts（5 发全中、版本连续） |
| 同身份重试不重复/异内容拒绝/并发 CAS | ✅ | test_five_facts + test_concurrent_head_advance |
| 发布失败登记/暴露、重启可恢复 | ✅ | unavailable 事件 + status + RestartRecoveryEntryTest（真实 resume） |
| 不因投影失败重执行工具 | ✅ | 计划 B 交付测试 + 真实生产运行 |
| **撤销/过期拒绝** | ❌→✅ 本轮补全 | GAP-1：`result.projection-revoked.v1`/`-expired.v1` + 读链/状态/终态补发/backfill 全链尊重（test_revocation 4 项） |

## R2（发布时序/读取来源/长流）

| 验收条款 | 状态 | 证据 |
| --- | --- | --- |
| 事实→投影→后续轮次顺序 | ✅（部分：当轮回执顺序=已裁决开放） | InLoopPublicationOrderingTest + 真实生产运行交付链 |
| 首次结果与回读一致、不借 body_ref 开旧正文 | ✅ | 计划 B：交付=已发布投影规范 JSON（digest 互验）；读链只给元数据 |
| 重启后身份与内容一致 | ✅ | W1–W3 窗口测试（正式 resume 入口） |
| **撤销/过期拒绝** | ❌→✅ 本轮补全 | GAP-1（同上） |
| 超过扫描页边界仍正确 | ✅ | LongStreamScanTest（501 事件翻页） |
| 扫描来源绑定可信调用身份 | ✅ | ledger 绑定 + untrusted/unverified 分离 |

## R3（metadata_only ≠ 已授权元数据）

| 验收条款 | 状态 | 证据 |
| --- | --- | --- |
| 用户输入/最终回复自由文本预览不自动开放 | ✅ | recall 去预览（长度元数据 + content_release 状态）+ 请求观察测试 |
| 可信 profile/适配器决定允许字段 | ✅（测试面） | ReleaseRule 契约链（config→runner→回执→计划 B 交付继承） |
| 缺释放规则 → 独立 withheld/human_required，不伪装失败 | ✅ | test_release_rules（sensitive 无契约 → result_withheld + human_required，is_error=False） |
| **撤销权限/更换接收方后旧内容不自动重放** | ❌→✅ 本轮补全 | GAP-1（撤销拒绝）+ GAP-2（读链当前策略检查 `policy_superseded`——历史交付不授当前读权） |
| 允许的安全测试仍得足够诊断 | ✅ | WP-C 诊断摘录（隔离运行）+ 默认完整安全诊断集 |
| MCP 适配器释放 | 🟡 | Docker manifest 与 MCP 适配器释放规则为后续切片（环境约束如实声明） |

## R4（最终请求门）

| 验收条款 | 状态 | 证据 |
| --- | --- | --- |
| 独立检查（压缩开关/sink/可压缩组不影响） | ✅ | _assert_request_fits + test_final_gate_blocks_before_provider |
| 含协议开销与输出预留 | ✅ | 全量计量（name+description+schema+48/项） |
| 实测误差与预算选择记录 | ✅ | r4-measured-metering-report.md（2.911–4.628 chars/token，安全下界 2.5；局限如实） |
| 不过度删减 call/result 配对 | ✅ | 门为只读检查，不做删减 |

## R5（隔离诊断与能力链）

| 验收条款 | 状态 | 证据 |
| --- | --- | --- |
| 无敏感测试失败能得到断言/堆栈并完成修复 | ✅ | WP-C v1 诊断摘录 + 真实 DeepSeek 修复任务（生产 CLI 全链绿） |
| 敏感受限任务正确转人工 | ✅（机制+配置级） | withheld+human_required 非 failure（测试）；sensitive+host 配置期拒绝（不静默放宽）；真实敏感容器运行需 Docker/部署值，边界在案 |
| 带敏感输入不借日志/文件/缓存回流泄漏 | ✅（工作区面） | 清单核验/只读候选/私有 scratch/清理隔离/回执摘录有界且无未标记秘密（test_wp_c 11 项） |
| 宿主/Docker 不可用不静默放宽 | ✅ | sensitive_profile_requires_sandbox（配置期）；Docker doctor 显式失败（既有） |
| 真实生产入口任务+故障恢复链证据 | ✅ | 真实修复任务（a1ebde8 轮）+ 恢复链 W1–W3 测试 |
| Docker manifest / MCP 适配器 / 敏感容器实验矩阵 | 🟡 | 需 Docker 环境与维护者部署值；设计细化已在案（bounded-capture.md WP-C 节） |

## 完成标准 6 条核对

1. 条款→执行点→测试映射：✅（r2r4-progress + 本表）。
2. 多结果/长流/并发 CAS/崩溃/索引缺失/**权限撤销**/uncertain 状态明确：✅（撤销为本轮补全；uncertain 沿用 D7 ledger 层）。
3. 观察最终模型请求/发送边界：✅（recall 请求观察、交付内容断言、关压缩 pre-send 拒绝）。
4. 全量 unittest、跳过项不记实测：✅（1131/0/0/32 记录口径）。
5. 普通修复任务可交付 + 敏感受限转人工：✅（真实任务 + withheld 机制/配置级证据）。
6. 进度表区分验证等级、不掩盖：✅（r2r4-progress 各节 + 开放项清单）。

## 本轮补全内容（提交即闭合）

- GAP-1：撤销/过期事件族（`revoke`/`expire`，reason/expires_at 入 payload，
  幂等身份含 reason）——lookup 显式拒绝（revoked/projection_expired）、
  `read_publication_status` 计 terminal revoked 非 pending、app 终态补发
  跳过已撤销事实（永不重发布）、backfill 把撤销写入交付决策（quota 命中
  则 paused 不下错判）。
- GAP-2：读链当前策略检查——投影 policy_version 落后当前契约 →
  `policy_superseded` 拒读（历史交付不授当前读权）。
- GAP-3：读取链扫描配额（2000 事件上限，命中报告 `scan_truncated`，
  backfill 对 truncated 转 paused，不静默截断）。
- 测试：tests/test_revocation.py 4 项（撤销全链拒绝/过期按时限/策略过期
  拒读/终态补发跳过撤销事实）。
- 边界重申（裁决在案）：撤销影响读链与未来发布；已按 SPEC-1 重放裁决
  落盘的交付决策与已进上下文的历史内容不回溯改写（重放确定性优先）。
