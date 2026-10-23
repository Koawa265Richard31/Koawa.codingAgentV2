# 释放契约面：维护者审查指南（plain-language，2026-10-03）

对象：本轮提交 `09c5ffa`（contracts.md §2/§4 实现）。本文面向维护者审查，
不用源码术语也能逐条核对；想看代码时每节给出文件与测试名。

## 一句话说明做了什么

以前：测试跑完后，模型拿到一份固定格式的回执（退出码、耗时、输出大小……），
跟接收方是谁无关；只有把 profile 标成 sensitive 才会全部隐藏。
现在：每个测试 profile 可以在 cfg.json 里声明一份"释放契约"——四个档位
决定模型能看到多少；没声明的 profile 一律按最保守档（全部隐藏）处理。

## cfg.json 里长什么样（这是部署者写的，模型只能引用不能提交）

```json
"test_profiles": [{
    "profile_id": "unit",
    "profile_version": "v1",
    "argv": ["python", "-B", "-m", "unittest", "discover", "-s", "tests"],
    "release_mode": "detailed",
    "audience_policy_ref": "audience/main",
    "input_policy_ref": "inputs/code-only",
    "candidate_admission_ref": "admission/2026-10-001",
    "environment_ref": "env/host-proven",
    "scratch_policy_ref": "scratch/ephemeral",
    "budget_policy_ref": "budget/unit-default"
}]
```

九个字段 = contracts.md §2 的表。**缺失时的行为是硬编码的**（不是推断）：

| 字段缺失 | 实际发生 |
|---|---|
| profile_id / profile_version | 拒绝执行该 profile（不会跑一个没身份的命令） |
| audience_policy_ref | 不产生任何模型可读正文（detailed 档自动降级） |
| input_policy_ref | 已记录；挂载控制归 WP-C 隔离工作区 |
| candidate_admission_ref | 敏感验证保持关闭 |
| environment_ref | detailed 降级；回执里不宣称"环境已证明" |
| release_mode | 采用 local_only（最保守档） |
| release_fields | 未列出的字段不进回执 |
| scratch_policy_ref | 需要临时写入的隔离运行直接拒绝启动 |
| budget_policy_ref | 已记录；预算计量引擎属后续 |

## 四个档位，模型分别看到什么（同一份失败的测试结果）

| release_mode | 模型收到的回执 |
|---|---|
| local_only | 全隐藏：执行状态/结论都是 withheld，"人工处理"，不给任何细节 |
| aggregate | 只给结论："测试失败了"，无任何诊断字段 |
| limited | 结论 + 你白名单里的字段（如只给 exit_code） |
| detailed | 完整安全诊断（含隔离运行的报错行摘录），但原始输出永不进模型 |

关键语义（contracts.md §4 的硬规则，全部有测试）：
- withheld ≠ 失败——模型不会把"结果不可读"当成"测试失败"去乱改补丁；
- 测试没跑过，回执永远不会写 failed（写 not_evaluated）；
- "执行完成"不等于"测试通过"（两个字段分开）；
- 结果不确定（超时等）→ 明确告诉模型 recover_first，而不是猜。

## 怎么自己验证（不用读代码）

1. `PYTHONPATH=src py -3.13 -B -m unittest tests.test_release_contracts -v`
   → 12 项：九字段缺失处理、四档矩阵、§4 关键组合（对应验收反例
   K03/K04/K05）。
2. 打开 `tests/test_release_contracts.py`，每个测试名就是一条行为描述
   （例：`test_local_only_withholds_everything_and_is_not_failure`）。

## 已实现（本轮）

- §2 配置装载面：九字段进 cfg.json → 运行时校验 → 回执生成全链。
- §4 回执协议：九字段回执（execution_status/verdict/diagnostic_status/
  diagnostic/applicability/completeness/next_action）。
- 交付链继承：投影里嵌的就是契约过滤后的回执，模型经按引用读取拿到的
  内容与首投一致。
- 合成 profile 验收 12 项测试（真实 profile 绑定按 §2 明文属维护者部署值）。

## 未实现（如实清单，全部在案）

1. **真实 profile 绑定**：上面 cfg.json 里的 refs（audience/…、admission/…）
   目前是"已记录的引用"，还没有一个注册表把它们解析成具体规则——绑定真实
   值是你的部署输入，实现侧不代授权。
2. **audience 注册表**：audience_policy_ref 现在只作为 detailed 档的证明门
   （有 ref 才允许详细诊断）；"换接收方后按新规则重判"已由 policy_superseded
   覆盖一半，完整的按接收方可见性规则表未建。
3. **budget 计量引擎**：budget_policy_ref 缺失会禁用对应能力，但"已用了多少
   披露额度"的累计计量未建（搜索/读取配额已有 scan_truncated 一路）。
4. **MCP 回执**：本次只覆盖测试 profile；MCP 仍走白名单+元数据门（原状），
   per-server 释放契约未做。
5. **producer_rules**：字段已装载，"可信生成者/转换规则"的执行点未建
   （目前回执全部由运行时自身生成，尚无第三方 producer 可供规则约束）。

## 审查路径建议

先看本文件 → 再看 contracts.md §2/§4 原文对照 → 抽查
tests/test_release_contracts.py 的测试名与断言 → 若认可，剩余动作只有
"你提供真实 profile 的部署值"（我可以先把配置模板和校验报错样例准备好）。
