# 安全诊断、历史检索与外发：证据清单

## 2026-09-23 补充核对（优先于下方历史解释）

当前基线为 `06ab2d3`。E7 的零 seed 解释已经被后续更正推翻；新增 E10：`audits/2026-09-11-platform-audit/smoke-readonly-review/f20-correction.md`，SHA-256 `cd11c6c347d92deae50b5b5c91356142a6cce7579146bbeb99c1e6099340120f`。实际 seed 六项，首次压缩已落盘，后续压缩映射仍需定位；巨组吞前缀不能当成已验证根因。下方原集合摘要继续标识历史快照，不声称匹配全部当前源码。

当前 loop 已增加指令和部分定义计量，但检查仍在压缩路径且存在提前返回；独立最终请求门的设计尚未全部实现。新增[首版实施契约](implementation/contracts.md)，本次仅文档变更；未执行运行时测试或 provider 实验。

更新于 2026-09-20。检查基线 HEAD、origin/main 与实时 GitHub main 均为 `84d22889228e629b5cd6f146591aca750408ea77`。工作区有并行改动 `tests/test_unified_runtime.py`，未纳入本设计、未修改；sourceDrift 因此为 present。下面记录的是设计开始时的实际文件哈希，后续并行推进可能使它们变化。

用户已选定方案并授权文档交付。没有运行真实 provider 实验或安全扫描；本文不宣称漏洞修复或 benchmark 完成。设计正文见 [完整设计](proposals/bounded-result-retrieval.md)，实施顺序见 [交接](implementation/bounded-capture.md)。

## 证据含义

| ID | 标题 | 范围 |
| --- | --- | --- |
| E1 | 后续记忆缺口记录 | 历史请求容量与接线记录 |
| E2 | 请求容量原始记录 | 完整消息及工具定义预算 |
| E3 | 结果与请求链 | loop、ledger、recovery 与模式脱敏 |
| E4 | 测试及 MCP 回执 | 工具日志、最终测试证据与 MCP 包装 |
| E5 | 测试运行边界 | Docker 整仓库只读挂载与运行器选择 |
| E6 | 动作身份与批准 | policy 的规范化参数、目标及绑定摘要 |
| E7 | F20 最新取证 | 空 seed 与非空 loop；替代早期泛化解释 |
| E8 | F21 历史诊断 | 异常终态问题；c9dfb5e 声明修复但本次未复验 |
| E9 | 会话投影 | session 的历史上下文入口 |

最新 F20 文档记录的是取证结论，本次未重放私有数据库。历史 F21 文档的“待修”状态不能覆盖其后的修复提交；同样，修复提交不能代替独立重验。

## 固定输入清单

所有路径相对 v2 根；这些输入只读，旧台账与审计记录不被本设计改写。

| 路径 | SHA-256 |
| --- | --- |
| `docs/closed-archive/d13-d23-memory-followup-gaps-2026-09-19.md` | `5caae54629e72fbf2e9e7ed1fcccb4c78c467d7bc31020a0558d677945b1661f` |
| `docs/closed-archive/d13-d23-request-capacity-gap-2026-09-19.md` | `9cc30369960b6bafe4160026644c256cac046c2684be513c3837593ee565f093` |
| `src/koawa_agent_v2/execution/loop.py` | `665e885c7c1d769be27ac285a486ea806e1dfac456df4e356a523f7368ee8219` |
| `src/koawa_agent_v2/recovery/execution.py` | `024a35262883b7c4cf4006c387305385bd6acb68a977df2ad6931215c59cb820` |
| `src/koawa_agent_v2/recovery/redaction.py` | `02ade6f2fb3cc8e13f0806bd728a91667f4fcb0183f5e37dbd9014dfa23c41bd` |
| `src/koawa_agent_v2/ledger/store.py` | `d92c03b0c2331bcb04f0711a1f5caca2218ef18519db23a1f2d8e09273f60127` |
| `src/koawa_agent_v2/verification/tools.py` | `3014e636c00a41da7766b51a243d09add4d060a8fceade54ababcadeb15e8c45` |
| `src/koawa_agent_v2/verification/finalization.py` | `e4ff561b210faab8df7cea38fa3f899fa69fedd4db6bbff40cb066659b681984` |
| `src/koawa_agent_v2/mcp/connection_manager.py` | `aafb321664a558d5da5add6075ba140d56787be9f4aba5460722018599a0b59a` |
| `src/koawa_agent_v2/sandbox/runtime.py` | `f5ff7fbee09da02c5dff8be1e57d0c17c2e6d4f259daf9ae0e85161143092127` |
| `src/koawa_agent_v2/runtime/assembly.py` | `492d9f8bbfd1deb7cad6434076b634036513560dad3d333ecd8167722358c08e` |
| `src/koawa_agent_v2/runtime/session.py` | `cca4789568d5fa7f28490d95abbf4a746c7dba6487fe76282153ccd451ec3563` |
| `src/koawa_agent_v2/policy.py` | `00c2eecbf23bf123b5d9bf9ac2eaaeb348e43475f51a017036b005ea045eeca8` |
| `audits/2026-09-11-platform-audit/smoke-readonly-review/f20-smoking-gun.md` | `04abdc77eecbb50271796115ff144f656148b37458f7eec38dc19fc1177491d9` |
| `audits/2026-09-11-platform-audit/smoke-readonly-review/f20-f21-diagnosis.md` | `af1111b9b92160397fd849a35957417ecff9b8b1808229eb10cba3c5bd5303e6` |

集合摘要算法：按上表顺序，将每行“路径＋单个空格＋小写 SHA-256”以 LF 连接，不加末尾 LF，对 UTF-8 字节作 SHA-256。结果：`f08de167e205c1d9ddc586cda367fe452fdf8c84d79217d79d4024c6b29e92d6`。机器可读同一清单存于 [hardening.json](hardening.json)。

## 观察范围与限制

观察覆盖选定的输出、恢复、请求与发送入口，不构成全仓不存在其他出口的证明。完整设计要求实施时盘点剩余入口与出口。性能变化为预计方向；本次只校验文档、引用、结构化状态与证据身份。所有拟定接口、事件和状态均为 Proposed。

## 本次文档验收

2026-09-20：hardening.json 解析成功；15 处包内 Markdown 链接均存在且没有越出设计包；15 份固定输入逐一匹配哈希，集合摘要一致；选项/证据 ID、推荐项、必需权衡维度和图文件引用检查通过；Markdown 代码围栏成对；git diff --check 通过。Mermaid 图只做源码与引用检查，未做图形渲染。未修改运行时代码，未运行实现回归或真实模型基准。并行测试改动保持不动。
