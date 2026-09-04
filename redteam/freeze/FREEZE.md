# RT/J 冻结包签字索引（FREEZE）

冻结日期：2026-09-02。基线：plan v1.1 + 维护者判定（I9=内部工程闭环口径；Q2=按需使用；
启动顺序=J1 先行、RT-1 前设维护者检查点）。

| 冻结项 | 文件 | 状态 |
|---|---|---|
| corpus | `redteam/corpus/manifest.json` + `redteam/corpus/data/*` | ✅ 2 条目逐文件钉 commit+sha256，许可全部核验（garak Apache-2.0 @2212c73e、AdvBench MIT @098262ed），无许可 DAN 维持排除 |
| PyRIT 依赖 | `redteam/lock/requirements.lock.json` | ✅ 109/109 组件逐 release sha256（pypi JSON API 全量走查） |
| SBOM / license | `redteam/lock/sbom.json`、`redteam/lock/licenses.txt` | ✅ 111 组件 license 全解析（元数据+license 文件+pypi 元数据三遍），0 待审 |
| profiles | `redteam/freeze/profiles.md` | ✅ T1–T6 × deployment-realistic/control-exercise |
| manifest | `redteam/freeze/manifest.md` | ✅ 5 类禁止效果谓词/负向 oracle/正向 control + mandatory 矩阵 + case schema |
| bridge | `redteam/freeze/bridge.md` | ✅ 双进程受限 JSON 协议 v1（消息集/纪律） |
| provider | `redteam/freeze/provider.md` | ✅ target/attacker/scorer 声明 + 种子 + 替换规则 + 技术护栏 |
| events（J2 合同） | plan v1.1 §3 J2（文档内冻结） | ✅ |
| reports schema | plan v1.1 §2.4 | ✅ |
| P0 规则 | plan v1.1 §7/§8 | ✅ |

## 签字

- 冻结编制：GLM-5.3（决策/审计侧），2026-09-02。
- 维护者批准（开工放行三判定，2026-09-02）：
  1. I9 口径 =「内部工程闭环完成」作为 §8.1 启动依据；
  2. plan v1.1 定稿追认（含 Q2 按需使用修订）；
  3. 启动顺序确认：冻结包 → J1（先行检查点）→ RT-1 → J2 → RT-2。

## 冻结后纪律

任何上表内容的变化 = 新冻结版本 = 旧证据失效（plan §6/§10）；提高资源护栏、放宽
谓词/oracle、扩大语料范围一律须维护者版本化审批。
