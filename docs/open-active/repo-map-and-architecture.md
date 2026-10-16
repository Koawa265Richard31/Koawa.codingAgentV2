# KoawaAgent V2 仓库地图与架构图

基线：`a051b65`（2026-10-03）。两图均为现状快照；计划 B 交付门与 WP-C
隔离工作区为本季新增层。配套：`hardening-memory-retrieval-2026-09-19/`。

## 一、仓库地图（v2/，116 源文件 / 133 测试文件）

```text
v2/
├─ src/koawa_agent_v2/
│  ├─ control/          # 事件溯源内核：EventStore 契约 + SQLite 实现
│  │                    #   (typed events, 精确版本 CAS, 幂等键, 流:
│  │                    #    thread/turn/run-execution/tool-execution/
│  │                    #    result-projection), ThreadRuntime 状态机,
│  │                    #    schema 迁移, read_snapshot     ~6.7k 行
│  ├─ runtime/          # 应用层：AppRuntime(run/chat/resume/status/
│  │                    #   cancel/doctor), assembly(控制面/执行面装配),
│  │                    #   config, cli, memory(D23 预算), session(D19),
│  │                    #   turn_conclusion/turn_summary, claim_gate,
│  │                    #   truth, subprocess_env, mcp_sandbox_labels ~8.5k
│  ├─ execution/        # AgentLoop：模型轮循环、D23 压缩、三重门
│  │                    #   (D22 claim gate / D5 completion gate /
│  │                    #    R4 独立最终请求门), 计划 B 交付集成
│  │                    #   TurnWorker：run 租约/围栏/终态化        ~2.0k
│  ├─ model/            # Provider 流协议：typed stream(Header/Item/
│  │                    #   Turn), 请求构造, 流装配器                ~2.4k
│  ├─ ledger/           # D7 工具执行账本：claim→execute→commit,
│  │                    #   恢复档案, J2 security gate 接线, 策略引擎 ~2.0k
│  ├─ verification/     # D5 验证：验证注册表(run_test_profile/git/
│  │                    #   finalize), TrustedCommandRunner, output_policy
│  │                    #   (安全诊断 + R3 释放契约), finalization    ~1.9k
│  ├─ retrieval/        # 有界结果检索（本季新增面）：projection(发布/
│  │                    #   撤销/过期/交付决策/lookup + 配额),
│  │                    #   recall_tool(会话历史元数据检索),
│  │                    #   result_read_tool(按引用读取)             ~1.4k
│  ├─ recovery/         # D6 恢复：DurableExecutionRecorder(run-execution
│  │                    #   事实 + 活投影), 压缩协议, checkpoint 验证缓存,
│  │                    #   RecoveryCoordinator, delivery backfill    ~3.6k
│  ├─ sandbox/          # D8/D25 容器执行：DockerCommandRunner, 分配/租约,
│  │                    #   WP-C v1 隔离测试工作区(test_workspace)    ~2.8k
│  ├─ tools/            # 工具地基：registry, schema, workspace 解析,
│  │                    #   repo_map                                  ~2.9k
│  ├─ editing/          # D4 补丁协议：apply_patch 事务/故障注入      ~1.4k
│  ├─ mcp/              # D10/D24/D25 MCP：connection_manager(白名单+
│  │                    #   元数据-only 门), activation, 适配器       ~5.9k
│  ├─ agents/           # D12+ 委派图（子代理，B-3 接线在案）        ~5.1k
│  ├─ security/         # J2 金丝雀检测（semantics-C）               ~0.3k
│  ├─ workspace/        # 工作区快照/基线证据                        ~3.6k
│  ├─ context/ plan/ telemetry/  # 上下文片段 / 计划板 / 故障注入点
├─ tests/               # 133 个测试模块：day 切片套件 + 加固套件
│  │                    #   (test_delivery_gate/test_revocation/
│  │                    #    test_release_rules/test_wp_c_*/
│  │                    #    test_result_read_tool/...) + fixtures/
├─ docs/                # closed-archive(D1-D25 历史) / open-active(活文档)
│  │                    #   hardening-memory-retrieval-2026-09-19/ = 本季
│  │                    #   设计/规格/进度/定档唯一审阅面
├─ scripts/             # 稳定场景 / r4 计量探针
└─ audits/              # 平台审计档案（09-07 / 09-11）
```

## 二、架构总图（双平面 + 单一事实源）

```mermaid
flowchart TB
    subgraph Entry["入口层"]
        CLI["runtime/cli<br/>生产命令"]
        APP["AppRuntime<br/>run/chat/resume/status"]
    end
    subgraph Ctrl["控制面（无模型/无 MCP）"]
        RT["ThreadRuntime<br/>turn 状态机"]
        POL["policy.py + approvals<br/>D9 审批"]
        SCHEMA["schema 迁移<br/>(SQLite WAL)"]
    end
    subgraph Exec["执行面（懒装配）"]
        ASM["assembly<br/>装配验证注册表+MCP"]
        TW["TurnWorker<br/>租约/围栏/终态化"]
        LOOP["AgentLoop<br/>模型轮 + D23 压缩<br/>+ 三重完成门"]
        EXE["LedgerExecutor<br/>D7 claim→execute→commit"]
        REG["CodingToolRegistry<br/>D3 读查/D4 补丁/D5 测试验证<br/>+ recall/read_result_projection"]
        MCP["MCP 连接管理<br/>白名单 + 元数据门"]
    end
    subgraph Gate["有界结果检索 + 计划 B 交付门（本季新增）"]
        PUB["projection.publish<br/>元数据投影"]
        REL["R3 释放契约<br/>ReleaseRule 过滤"]
        DEC["delivery-decided<br/>SPEC-1 幂等决策"]
        RDR["recovery/delivery<br/>resume backfill"]
    end
    subgraph Store["单一事实源（append-only SQLite）"]
        EST["EventStore<br/>thread/turn/run-execution/<br/>tool-execution/result-projection"]
    end
    subgraph Rec["恢复面"]
        REC_D["DurableExecutionRecorder<br/>事实 + 活投影(占位符)"]
        COORD["RecoveryCoordinator<br/>列表/认领/重建"]
        RED["reducer v3<br/>占位符→交付内容替换"]
    end
    CLI --> APP --> ASM
    APP --> RT
    RT --> EST
    ASM --> TW --> LOOP --> EXE --> REG
    ASM --> MCP
    REG --> PUB
    PUB --> REL
    LOOP -->|"publisher 决策"| DEC
    DEC --> EST
    TW --> REC_D --> EST
    COORD --> RED --> EST
    COORD --> RDR --> EST
    EST -.->|"唯一事实源<br/>重放=状态"| Ctrl
```

## 三、计划 B 交付生命周期（测试回执的模型可见性）

```mermaid
sequenceDiagram
    participant M as 模型轮
    participant L as AgentLoop
    participant R as Recorder(事实/活投影)
    participant P as ProjectionStore
    participant C as 上下文(下轮请求)
    M->>L: run_test_profile 回执
    L->>R: tool_completed(原回执=运维真相)
    R-->>R: 事实落盘 + 活投影=占位符
    L->>P: publish(元数据投影)
    alt 发布成功
        P-->>L: 已发布(投影引用+载荷)
    else 发布失败
        L->>P: register unavailable
        P-->>L: 占位符决策
    end
    L->>R: delivery_decided(SPEC-1 键, 三摘要)
    R-->>R: 交付事件落盘(写失败=暂停)
    L->>C: 交付内容(投影规范 JSON / 占位符)
    Note over C: 原回执从不进入任何重建上下文
```

## 四、读取链双通道现状

```mermaid
flowchart LR
    subgraph Ch1["通道 1：recall_history（会话历史）"]
        A1["SessionMemory.recall<br/>IDF/新近排序"] --> A2["命中=长度元数据<br/>content_release=不可提供"]
    end
    subgraph Ch2["通道 2：read_result_projection（投影流，按引用）"]
        B1["(turn, call[, model_turn])"] --> B2{"lookup_projection"}
        B2 -->|published| B3["元数据投影+不可变引用<br/>(当前策略检查)"]
        B2 -->|revoked/expired/<br/>policy_superseded| B4["拒绝"]
        B2 -->|歧义| B5["ambiguous_reference<br/>候选清单, 不猜"]
        B2 -->|"配额(2000 事件)"| B6["scan_truncated=true<br/>not_found≠全量"]
    end
```

## 五、关键不变量速查

- 状态变更 = typed event + 精确 expected version；幂等键决定重放语义。
- 敏感原文（stdout/stderr/环境）不进模型服务：output gate（安全诊断）
  → R3 释放契约 → 计划 B 交付（投影派生）三道闸。
- 恢复 = 重放事实 + 交付决策；backfill 先验后写（协议版本/源事实/围栏）；
  paused / protocol_mismatch / corruption 三态分离。
- 测试基线：全量 ~1137 项 / 0 失败 / 32 跳过（Docker/provider）。
