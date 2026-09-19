# D8：真实 Docker Sandbox 与崩溃回收

## 1. 完成边界

D8 把 D5 的测试命令从宿主进程迁入真实 Linux 容器。模型仍然只能选择 Runtime
注册的 profile_id；image、argv、cwd、环境变量、资源预算、mount 和 Docker
安全参数都由可信配置决定。

D8 可以声明：

- 每次测试运行都创建独立 Docker 容器，而不是给 subprocess 换名字；
- runtime 只接受完整小写 sha256 image ID，拒绝 tag；
- workspace 是唯一 host bind，且只读；可写空间只有受限 /tmp tmpfs；
- network、root、capability、提权、PID、CPU、memory、time 和 output 都有硬边界；
- allocation intent 先于 docker create 持久化，容器 ID 后写；
- timeout、output overflow、OOM 和 cancellation 是不同结果；
- reaper 只根据 durable intent、不可猜 nonce、完整 labels、确定性名称和完整
  container ID 对账，拒绝未知或篡改容器。

D8 不声明：

- Docker daemon 本身是不可信的。能控制 daemon 的主体等同于 host root；
- 任意外部副作用 exactly-once；
- 可写仓库隔离。D8 workspace 整体只读，可写 per-agent worktree 留给 D12；
- 允许联网。D9 才定义 policy、approval、proxy 和 allowlist；
- bind mount 是不可变快照。D8 通过拒绝 link/reparse/hard link、create 前后复验
  降低竞态；真正的隔离快照由 D12 worktree/container 组合完成。

## 2. 信任边界

| 主体 | 信任 | 能力 |
|---|---|---|
| 模型与目标仓库 | 不可信 | 只能选择固定 profile；不能提供 Docker argv |
| 容器内命令 | 不可信 | 读取只读 workspace，写受限 tmpfs |
| KoawaAgent host controller | 可信 | 持久化 intent，调用 Docker CLI，精确回收 |
| Docker CLI | 可信控制面工具 | 只接收结构化 argv，shell=False |
| Docker daemon | 高权限可信基座 | daemon socket 永不挂入容器 |
| Event Store | durable control plane | 保存纯 JSON allocation facts，不保存 client/process handle |

Docker 官方文档明确指出 daemon 控制能力具有高权限，因此保护 daemon socket 是
安全前提。D8 不把 /var/run/docker.sock、用户目录、凭据目录或完整 host 环境放进
容器。

## 3. 固定容器配置

每次 create 都由 production runtime 生成等价于以下结构化参数：

~~~text
docker container create
  --name koawa-v2-<allocation uuid hex>
  --pull never
  --network none
  --read-only
  --user 65532:65532
  --cap-drop ALL
  --security-opt no-new-privileges
  --pids-limit <trusted limit>
  --cpus <trusted limit>
  --memory <trusted bytes>
  --memory-swap <same bytes>
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=<trusted bytes>,mode=1777
  --mount type=bind,src=<canonical workspace>,dst=/workspace,readonly
  --workdir <fixed /workspace path>
  --init
  --log-driver none
  --label <durable identity labels>
  --env <explicit allowlisted NAME=value>
  --entrypoint <fixed absolute POSIX executable>
  <immutable sha256 image ID>
  <fixed argv tail>
~~~

create 后、start 前，Runtime 读取 exact container inspect，核验 image、确定性名称、
UID/GID、working directory、entrypoint/args、network、read-only rootfs、cap-drop、
no-new-privileges、PID/CPU/memory/swap、tmpfs、log driver、只读 workspace mount
和显式环境变量。核验失败时不会执行目标命令。

## 4. Allocation 事件流

每次物理容器运行都有随机 allocation_id，D7 的稳定 execution_id 作为 owner。
同一个逻辑工具调用恢复后仍是同一个 owner，但新容器必须使用新的 allocation。

| versioned event | 状态 | 含义 |
|---|---|---|
| sandbox.allocation-intended.v1 | INTENDED | 已保存 owner、nonce、image/mount/profile/command digest、deadline |
| sandbox.container-bound.v1 | BOUND | create 已返回并写入完整 64-hex container ID |
| sandbox.container-started.v1 | STARTED | 即将执行 attach/start |
| sandbox.container-finished.v1 | FINISHED | 保存 typed outcome、exit、OOM、时间 |
| sandbox.container-released.v1 | RELEASED | exact remove 已证明成功或已明确证明不存在 |

所有转换都追加 typed event，并使用精确 expected stream version。事件 payload 只含
JSON；Docker client、subprocess handle、host env 和凭据不进入 Event Store。

## 5. create 崩溃窗口与 reaper

正常顺序：

~~~text
append INTENDED
  -> docker create
  -> inspect security
  -> append BOUND(container_id)
  -> append STARTED
  -> docker start --attach
  -> append FINISHED
  -> exact rm(container_id)
  -> append RELEASED
~~~

docker create 成功、BOUND 尚未提交时进程可能被强杀。容器因此携带 managed、
allocation UUID、D7 owner execution ID、256-bit owner nonce、immutable image ID
以及 mount/profile/command digest。

容器名是 koawa-v2-<allocation uuid hex>，只用于精确定位，不用于模糊删除。
INTENDED 只能收养具有确定性名称和全部 exact labels 的唯一容器。BOUND/STARTED/
FINISHED 只处理 Event Store 已记录的完整 container ID。label/nonce/name/image/mount
任一不符都返回 refused，保留 allocation 供人工审计。

inspect 有三种语义：FOUND、明确 ABSENT、ERROR。只有 daemon 明确返回 no-such-
container 才能写 container_not_found 并 release；超时、daemon 失败和无效 JSON
都保留 open allocation。create 返回超时或不完整结果也保持 INTENDED，不伪造
“未创建”结论。

## 6. 输出、超时、取消与资源结果

docker start --attach 的 stdout/stderr 由两个线程并行排空，各自只保留预算内字节，
同时记录真实总字节数。任一输出超限即按 exact ID kill 整个容器。

结果区分 passed、failed、timed_out、output_limit、oom_killed、start_failed 和
cleanup_failed。

取消由 D2 progress guard 触发。Docker runner 先 best-effort kill/remove，再始终
原样重抛原 AgentLoopCancelled；清理或 Event Store 故障不能把取消降级成普通
tool error。D7 LedgerExecutor 也显式传播取消，不把它记录成副作用未知。

## 7. Windows mount 防护

host mount source必须 strict resolve 为目录，且路径不能含 Docker --mount 解析
敏感的逗号、换行或 NUL。Runtime 有界扫描整棵 workspace：

- 任何 symlink、junction 或 reparse point 都拒绝；
- regular file 的 st_nlink > 1 拒绝，避免同卷 hard-link 暴露 workspace 外文件；
- 扫描权限错误和条目上限都 fail closed；
- create 前计算 canonical path + filesystem identity digest；
- create 后、start 前再次扫描并核对 inspect 中的 bind source/digest。

容器只能看到 /workspace，看不到 host workspace 的父目录。因此 V2 位于父 Git
仓库时，父 .git、Git config/attributes 和 controller metadata 不会因父路径而自动
进入容器。生产部署仍应把 Event Store 数据库放在 workspace 外。

## 8. D5/D7 接线

build_verified_coding_tool_registry 接受显式 CommandRunner。注入
DockerCommandRunner 时，UNTRUSTED repository 也能运行容器 profile；同时传 host
profiles 与 runner 会 fail closed。旧 TrustedCommandRunner 只保留测试和 bootstrap，
并在 final report 中继续显示 host-only warning。

D5 test/final evidence 包含 backend、immutable image ID、profile digest、allocation
ID 和 container ID。D7 传入稳定 execution ID；runner 在新运行前只对该 owner 做
精确恢复，未知或篡改候选会阻止重复执行。

## 9. 本地镜像与运行

Dockerfile 的 build context 只能是 docker/，不会读取目标仓库：

~~~powershell
docker build --pull=false -t koawa-agent-d8-runtime:local -f docker/agent-runtime.Dockerfile docker
docker image inspect koawa-agent-d8-runtime:local --format '{{.Id}}'
~~~

tag 只用于本地准备。把 inspect 得到的完整 image ID 交给 Runtime；Runtime 自身
拒绝 tag，并使用 --pull never。

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='src'
python -B -m unittest tests.test_d8_sandbox_protocol tests.test_d8_verification_integration tests.test_d8_docker_integration -v
python -B examples/day08_docker_sandbox.py
~~~

Docker 集成测试必须在至少一个真实 Linux daemon/image 环境中 0 skip 通过，不能
以“daemon 不可用所以全部 skip”作为 D8 完成证据。

## 10. 参考

- [docker container create](https://docs.docker.com/reference/cli/docker/container/create/)
- [Docker Engine security](https://docs.docker.com/engine/security/)
- [Protect the Docker daemon socket](https://docs.docker.com/engine/security/protect-access/)
- [Rootless mode](https://docs.docker.com/engine/security/rootless/)
