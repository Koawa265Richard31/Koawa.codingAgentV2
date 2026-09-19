# 检索与敏感内容边界：证据清单

本地工作区：`D:\A_Dev_Projects\KoawaAgent\v2`。检查时 HEAD 和已有 `origin/main` 引用均为 `2d42746`；未执行 fetch。源码有并行未提交修改，因此源码观察对应检查时工作区，漂移状态为 `present`。没有执行真实 provider 实验或安全扫描。

| ID | 输入 | SHA-256 / 身份 | 说明 |
| --- | --- | --- | --- |
| E1 | `docs/closed-archive/d13-d23-memory-followup-gaps-2026-09-19.md` | `5caae54629e72fbf2e9e7ed1fcccb4c78c467d7bc31020a0558d677945b1661f` | 完整请求计量与配置接线缺口 |
| E2 | `docs/closed-archive/d13-d23-request-capacity-gap-2026-09-19.md` | `9cc30369960b6bafe4160026644c256cac046c2684be513c3837593ee565f093` | 请求容量原始记录 |
| E3 | `src/koawa_agent_v2/ledger/store.py`, `recovery/execution.py`, `recovery/redaction.py`, `execution/loop.py` | 检查时工作区；未冻结哈希 | 账本、恢复记录、脱敏、模型请求路径 |
| E4 | `src/koawa_agent_v2/verification/tools.py`, `verification/finalization.py`, `tools/repository.py`, `mcp/connection_manager.py` | 检查时工作区；未冻结哈希 | 工具输出上限、证据元数据及 MCP 封装 |
| E5 | `src/koawa_agent_v2/sandbox/runtime.py`, `runtime/assembly.py` | 检查时工作区；`assembly.py` 有并行未提交修改 | Docker 测试禁网络、只读挂载整个仓库；也可选择受信任主机运行器。仅凭 Docker 不能证明无法读取仓库内敏感文件 |

文档集合摘要：按 E1、E2 上述小写哈希顺序以单个 LF 连接后作 SHA-256，值为 `9bdd750eed1ae6b64db0d14e037709f7220f91457cd8dcedd474666adc03381e`。旧缺口台账由并行会话处理，本设计不修改它。
