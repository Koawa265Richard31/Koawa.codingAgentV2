# D25 sandboxed MCP：doctor / 运行 / 清理 操作说明

适用对象：按 `examples/d25_sandboxed_mcp.example.json` 配置的镜像内置第三方 stdio MCP server。
全部命令从 `v2/` 执行（PowerShell）。安全基线由代码强制，配置无法放宽：`network=none`、
零挂载、零秘密、只读 rootfs、`user 65532:65532`、`cap-drop ALL`、`no-new-privileges`、
CPU/内存/PID/tmpfs 上限、精确 `sha256:<64hex>` 镜像身份。

## 1. doctor（启动前核验）

```powershell
PYTHONPATH=src py -3.14 -B -c "from koawa_agent_v2.sandbox.runtime import DockerSandboxDoctor; r = DockerSandboxDoctor('docker').check('sha256:' + 'a'*64); print(r.ready, r.error_code)"
```

- daemon 不可达 → `docker_daemon_unavailable`（修 Docker Desktop 后重试）；
- 镜像本地不存在 → `sandbox_image_unavailable`（先构建并记录 digest，见下）；
- 两者皆通过 → `ready True`。

## 2. 构建与钉定镜像（只接受本地 `sha256:<64hex>`）

```powershell
docker build -t koawa-d25-filesystem:fixed --pull tests/fixtures/d25_mcp_server
docker images --no-trunc --format "{{.ID}}" koawa-d25-filesystem:fixed
```

把输出的 `sha256:...` 填入配置的 `image_id`，并同步
`tests/fixtures/d25_mcp_server/provenance.json`（npm 版本/shasum/许可证/镜像 digest）。
tag 不能进入已授权执行；`pull=never` 由代码固定。

## 3. 运行（真实链路）

交互会话照常启动；sandboxed server 的 grant→intent→claim→create→inspect→attach 全部
自动完成，工具经 policy/ledger 执行：

```powershell
$env:PYTHONPATH = "src"
py -3.14 -B -m koawa_agent_v2.runtime.cli interactive --config examples/d25_sandboxed_mcp.example.json --repo .
```

容器生命周期事件见事件存储 `mcp-allocation-*` 与 `sandbox-allocation-*` 两条流
（同一 allocation id 关联）；宿主崩溃后重启，恢复流程按
`docs/day-25-third-party-mcp-security-governance.md` §W3 窗口表精确收口。

## 4. 清理（精确回收）

会话正常关闭会自动 stop+rm 容器并释放 allocation。孤儿回收（宿主崩溃残留）：

```powershell
PYTHONPATH=src py -3.14 -B -c "
import uuid
from pathlib import Path
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.mcp.activation import ActivationService
from koawa_agent_v2.mcp.docker_endpoint import DockerAdapter
from koawa_agent_v2.mcp.sandbox_reconcile import DualLedgerReconciler, ReconcileFacts
from koawa_agent_v2.sandbox.runtime import SandboxAllocationStore
store = SqliteEventStore('agent.sqlite3')
activation = ActivationService(store, clock=lambda: __import__('datetime').datetime.now(__import__('datetime').timezone.utc))
reconciler = DualLedgerReconciler(activation=activation,
    sandbox_store=SandboxAllocationStore(store), docker_adapter=DockerAdapter(),
    docker_executable='docker', container_labels=(('koawa.managed', 'mcp-sandbox'),))
# 对每个已知 allocation_id 执行：
# print(reconciler.reconcile(ReconcileFacts(...)))
"
```

只回收 label + inspect 身份全部匹配的容器；未知/缺标签/被篡改对象仅报告，绝不删除。

## 5. 明确不做

公网/代理/远程 MCP、宿主目录挂载、秘密注入、特权容器、`latest` tag、24h soak、
生产发布认证。需要这些能力 → 另立切片（受控 egress/secret broker）或显式选择
`host_trusted`（每次 ASK，拥有宿主用户权限，不是沙箱）。
