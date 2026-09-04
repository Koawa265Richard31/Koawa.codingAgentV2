# RT/J 攻击语料（纯数据）登记

依据：`docs/agent-redteam-jailbreak-plan.md` v1.1 §3 J1 与 §6 冻结包「corpus」行。
**公开 ≠ 可再分发；许可未核验的项目一律不入库；无许可 DAN 项目排除（v1.0 §9 Q5 决策）。**
语料只是攻击意图/编码种子，不含任何对模型层越狱的声称。

## manifest 字段（每条语料项必填，缺项不得入库）

见 `manifest.template.json`。字段与 v1.1 §6 一致：
corpus_id、source_url、immutable_revision、source_path、sha256、spdx、license_evidence、
modifications、review。

## 候选来源与核验状态（2026-08-31）

| 候选 | 用途 | 许可 | 状态 |
|---|---|---|---|
| NVIDIA/garak 探针数据（promptinject/dan/encoding 等） | 攻击意图种子 | Apache-2.0 | **已核实**：https://raw.githubusercontent.com/NVIDIA/garak/main/LICENSE（2026-08-31 抓取，全文 Apache-2.0）。仍需钉 immutable commit hash + 具体探针文件级 sha256 后入库 |
| Azure/PyRIT（示例 seed prompts） | 攻击编排参考/种子 | MIT | **已核实**：pyrit 1.0.1 dist-info `License-Expression: MIT` + LICENSE/NOTICE 文件（本地 venv 取证）。具体文件级入库同样待钉 revision/hash |
| llm-attacks/llm-attacks（AdvBench harmful_behaviors.csv） | 通用攻击意图清单 | **未核验** | 禁止在核验仓库 LICENSE 前入库；若为 MIT 则可作为候选，否则排除 |
| 各类无 LICENSE 的 DAN 合集 | — | 无许可 | **排除**（Q5 决策） |

## 入库流程（冻结包「corpus」项的完成方式）

1. 选定具体文件 → 记录 source_url + **immutable revision**（commit hash，不是分支名）。
2. 下载 → 计算 sha256 → 与 manifest 一并存 `redteam/corpus/data/`。
3. 任何本地修改必须记 modifications（默认零修改）。
4. review 记录审核人与日期（本轮维护者复审）。
5. manifest 冻结后，tests fixture 若引用语料，必须与 manifest hash-sync（v1.1 §7）。
