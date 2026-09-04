# RT/J 冻结包：Bridge 协议 v1（冻结）

RT-1 的 PyRIT（红队 venv）与生产目标（生产 venv）**双进程、双 venv**：生产 wheel 不含
PyRIT，桥接只传受限 JSON（§4 隔离）。

## 进程拓扑

```
[redteam venv] pyrit adapter (orchestrator + scorer)
      │ stdin/stdout 换行分隔 JSON（受限协议，下表）
      ▼
[生产 venv] target bridge（scripts/rt_target_bridge.py，实施期落文件）
      │ 进程内：AppRuntime / interactive lane
      ▼
   生产装配（policy/审批/账本/工具/MCP 全链，零绕过）
```

## 协议消息（v1 冻结集；未知字段=协议错误）

| 方向 | method | 必字段 | 语义 |
|---|---|---|---|
| adapter→bridge | hello | protocol_version=1 | 握手；版本不符即退出 |
| bridge→adapter | ready | config_hash, model_id | 装配完成 |
| adapter→bridge | prompt | attempt_id, text | 提交一次用户输入（run lane 单轮；chat lane 多轮） |
| bridge→adapter | response | attempt_id, final_text_digest, turn_status | 只回 digest+状态，不回原始全文（adapter 需要原文续攻击时经 `fetch_turn` 二段式，逐条审计） |
| adapter→bridge | fetch_turn | attempt_id | 取该轮投影文本（白名单投影，同 session 合同） |
| bridge→adapter | result | attempt_id, action_facts[] | 动作层事实：action digest、policy 码、ledger 状态（禁止效果判定的输入之一） |
| adapter→bridge | error | attempt_id?, code | 稳定码（禁止携带 env/stderr 正文） |
| 双向 | shutdown | — | 有界关闭 |

## 纪律

- 超时：bridge 单消息处理超时 = lane 的 process_start_timeout；超时→error+有界关闭；
- 重试≤2（v1.1 技术护栏）；并发=1；
- 凭证只经环境注入 bridge 进程；跨协议禁止：凭证、完整环境、隐藏推理、原始语料；
- adapter 侧 scorer 只消费 bridge 给出的文本/事实，产出仅驱动迭代（判分双轨制 §RT-1）。

## 实施期可定（不属安全门）

消息字段非安全增补、日志格式、进程启动参数细节。
