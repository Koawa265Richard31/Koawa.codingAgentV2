# D4：结构化 Atomic Apply Patch

## 1. 本日完成了什么

D4 把 D3 的只读仓库侦察能力扩展为第一个真实写工具：模型可以先用
`read_file` 取得文件正文和 SHA-256，再提交版本化 `PatchSet`，一次事务中新增、更新
或删除多个 UTF-8 文件。

真实执行链为：

```text
Model completed ToolCallItem(apply_patch)
  -> ToolRegistry 同源 name/schema gate
  -> ApplyPatchArguments(patch_json)
  -> parse_patch_document()
  -> AtomicPatchWorkspace.apply()
  -> 全量 read/base hash/hunk preflight
  -> workspace mutation lock
  -> 所有新内容写入同目录 stage 文件并 fsync
  -> 每个目标重新检查 parent identity + file identity + SHA-256
  -> backup/replace/delete
  -> 全量终态验证
  -> 删除 backup/stage
  -> bounded PatchTransactionResult + deterministic diff
  -> ToolResultMessage
  -> 下一模型轮重新 read/list 验证
  -> final
```

D4 的离线示例真实贯通 D1 Turn、D2 typed stream/Agent Loop、D3 Registry/read/list 和
D4 多文件事务，不用内存假文件系统冒充写入。

## 2. 文件与职责

### `editing/protocol.py`

它负责不可信 Patch 文档进入文件系统前的全部纯内存语义：

- `PatchLimits`：Patch 字符数、文件数、hunk 数、行数、单行字符、单文件字节、
  总输入/输出字节和模型结果字符的硬上限；bool 不能冒充 int。
- `PatchOperation`：只支持 `add`、`update`、`delete`。Move 明确未实现。
- `PatchHunk`：原文件中的一段精确行匹配，包含 `old_start`、`old_lines`、
  `new_lines`。
- `AddFileChange`、`UpdateFileChange`、`DeleteFileChange`：三种互斥结构。
- `PatchSet`：`schema_version=1`、不可变 changes 和 canonical document SHA-256。
- `parse_patch_document()`：拒绝重复 JSON key、未知字段、宽松类型、未知版本、
  重复/大小写冲突路径和所有资源超限。
- `apply_update()`：以原文件行号为坐标，严格比较 `old_lines`，不做 fuzzy match。
- `decode_text_document()` / `encode_text_document()`：识别并保留 UTF-8 BOM、
  LF/CRLF 和末尾换行。
- `encode_add()`：ADD 的正文协议统一使用 LF，写入时根据显式 `newline` 转 LF 或
  CRLF；`utf8_bom` 也必须显式声明。

### `editing/transaction.py`

它负责真实磁盘事务：

- `AtomicPatchWorkspace.apply()`：唯一写入口，锁住 workspace 后执行 plan、stage、
  commit、verify、cleanup。
- `_plan()`：安全读取所有旧文件，验证 base hash 和精确 hunk，在任何写入前计算
  全部新字节、hash、行数和 diff。
- `_stage()`：在目标同目录创建随机 stage 文件，写入、flush、fsync、设置权限，
  再通过 `WorkspacePathResolver` 重新打开并核对 hash/identity。
- `_commit()`：每个 replace 前再次校验 parent identity、旧文件 identity 和 SHA-256；
  原文件先移到随机 backup，再安装 stage。DELETE 只保留 backup，最后统一清理。
- `_rollback()`：反序反做已提交项；只有新文件 identity/hash 与本事务 stage 一致时
  才删除，随后把原 backup 移回并核对原 identity/hash。
- `_workspace_mutation_lock()`：同进程使用每 workspace `RLock`，跨进程使用只含
  workspace path digest 的 OS lock file。两个基于同一 base 的协作 Worker 只有一个
  能提交，另一个会在锁内读到 `stale_patch_base`。
- `PatchTransactionResult.to_tool_content()`：返回 changed files、before/after hash、
  行数统计和 unified diff；diff 超限会标记 `diff_truncated=true`。

### `editing/tools.py`

- `ApplyPatchArguments`：Registry 解码后的 frozen typed 参数。
- `apply_patch_tool_spec()`：生成模型可见 `apply_patch` definition。
- `build_coding_tool_registry()`：把 D3 的 `read_file/list_files/search_text` 与 D4 的
  `apply_patch` 注册进同一个可 seal Registry。

外层 D3 ToolSchema 目前只支持简单 object 和标量数组，因此 `apply_patch` 的参数是
一个 `patch_json` 字符串；字符串内部仍是独立、严格、版本化的嵌套 Patch 协议，
不是自由格式 diff。

### D3 的两个必要扩展

- `WorkspaceFileRead.identity` 把已打开文件句柄对应的 OS object identity 交给事务，
  后续重读可同时比较 hash 与 identity。
- `WorkspacePathResolver.inspect_directory()` 安全打开 parent directory，只返回相对
  display path 和 identity，不为一次修改扫描巨大目录。

`.koawa-patch-*` 是 D4 内部 stage/backup 保留命名空间。D3 read/search 和 D4 写入
都拒绝它，list 完全隐藏它，避免并发模型观察临时副本。

## 3. Patch v1 文档

### UPDATE

```json
{
  "schema_version": 1,
  "changes": [
    {
      "operation": "update",
      "path": "src/config.py",
      "base_sha256": "read_file 返回的 64 位小写 SHA-256",
      "hunks": [
        {
          "old_start": 1,
          "old_lines": ["RETRY_LIMIT = 1"],
          "new_lines": ["RETRY_LIMIT = 3"]
        }
      ]
    }
  ]
}
```

`old_start` 从 1 开始，并且始终指向原始文件，不随着前一个 hunk 的增删而改变。
Hunk 必须严格升序、互不重叠，`old_lines` 必须逐行完全相同。只要一处不匹配，整个
Patch 零写入失败；系统不会搜索“附近差不多的一段”然后猜测修改位置。

### ADD

```json
{
  "operation": "add",
  "path": "src/report.py",
  "content": "def status():\n    return 'ready'\n",
  "newline": "lf",
  "utf8_bom": false
}
```

`content` 使用 LF 表达逻辑换行，是否以 `\n` 结尾就是新文件是否有末尾换行。
`newline` 可为 `lf` 或 `crlf`。ADD 默认权限由协议固定为普通 `0644` 文件；D4 不负责
创建可执行脚本权限。

### DELETE

```json
{
  "operation": "delete",
  "path": "obsolete.txt",
  "base_sha256": "read_file 返回的 SHA-256"
}
```

DELETE 也必须携带 base hash，而且目标必须是有界 UTF-8 文本。D4 不允许模型把未知
二进制文件当普通源码直接删除。

## 4. 为什么不是直接让模型输出 unified diff

自由格式 diff 很难在 Provider、校验器和执行器之间维持稳定语义：路径头、上下文
模糊匹配、换行、文件创建/删除以及部分应用都容易出现歧义。D4 将“模型意图”拆成
显式 operation、path、base hash 和 exact hunk：

1. Provider 输出可以先完整结构化验证；
2. 模型必须证明它基于哪一版文件作决定；
3. 所有 hunk 能在内存中先算完；
4. 错误能有稳定分类，而不是解析一段人类终端文本；
5. unified diff 只作为结果证据生成，不反过来充当执行协议。

## 5. 两阶段事务与回滚

### Phase A：全量 plan

在第一次临时文件写入前，所有 changes 已完成：

- workspace-relative path 和控制目录检查；
- parent directory 句柄身份检查；
- 旧文件安全读取、普通文件/链接检查；
- `base_sha256` 检查；
- UTF-8、BOM、换行、文件/行数/总字节检查；
- 全部 hunk context 检查；
- 新字节、after hash、行数和 diff 计算。

所以 malformed document、过期 base、第二个文件 context 错误等都不会留下第一个文件
已经修改的状态。

### Phase B：stage + commit

所有 ADD/UPDATE 新内容先写入目标同目录的 stage 文件。全部 stage 成功后，commit
才开始。每项 commit 前又重读旧目标，比较：

```text
parent identity
+ target identity
+ current SHA-256
```

任意变化都返回 `stale_patch_base`，并回滚此前已经提交的其他文件。UPDATE/DELETE
先把原对象移到同目录 backup；这让普通异常回滚时能恢复原 inode、内容和权限，而
不是根据旧字符串重新造一个“看起来一样”的文件。

## 6. 稳定失败语义

| 场景 | 稳定 code | 是否写目标 |
|---|---|---|
| JSON 畸形/重复 key | `invalid_patch_document` | 否 |
| 未支持 schema 版本 | `unsupported_patch_schema` | 否 |
| change/hunk 字段错误 | `invalid_patch_change` / `invalid_patch_hunk` | 否 |
| 文件/hunk/行/字节预算超限 | `*_limit_exceeded` | 否 |
| 同一路径重复或大小写冲突 | `duplicate_patch_path` | 否 |
| `..`、绝对路径、设备路径 | `invalid_workspace_path` | 否 |
| `.git` 或内部事务路径 | `repository_control_path_forbidden` | 否 |
| ADD 目标已存在 | `patch_target_exists` | 否 |
| UPDATE/DELETE 目标不存在 | `patch_target_missing` | 否 |
| base hash 或 identity 已变化 | `stale_patch_base` | 否；若前项已提交则先回滚 |
| exact old lines 不匹配 | `patch_context_mismatch` | 否 |
| hunk 重叠 | `overlapping_patch_hunks` | 否 |
| 二进制/非 UTF-8/混合换行 | `binary_file` / `unsupported_text_encoding` / `mixed_line_endings` | 否 |
| stage I/O 失败 | `workspace_stage_failed` | 否，清理 stage |
| commit I/O 失败且回滚可证明 | `workspace_commit_failed` | 已恢复原状态 |
| 回滚或终态无法证明 | `workspace_outcome_unknown` | 不确定，禁止自动重试 |

所有模型可见错误只包含稳定 code，不包含宿主绝对路径、Patch 正文或底层异常正文。

## 7. “Atomic”的准确边界

D4 可以声明：

- 所有 Patch 在第一次目标写入前完整解析和规划；
- 同一 workspace 的协作写者串行化；
- 正常进程内异常会回滚已经替换的文件；
- 回滚失败不会伪装成普通失败；
- 并发修改在 commit gate 通过 hash + identity 拒绝覆盖；
- 结果包含确定性 before/after hash、行数和 diff 证据。

D4 不能声明：

- 进程被 kill 或断电后自动恢复半个文件事务；
- 文件系统修改与 D1 SQLite 事件属于同一个 ACID 事务；
- 相同 ToolCall 跨崩溃 exactly-once；
- `workspace_outcome_unknown` 可以安全自动重试；
- 能抵抗拥有同等宿主机改名权限的恶意进程，在最后一次检查与 `os.replace` 的极小
  窗口反复 swap/restore；
- 路径检查能证明 hard link/bind mount 的数据来源；D8 容器才提供更完整隔离。

具体 crash 窗口：

| 崩溃位置 | 磁盘上可能状态 | D4 结论 |
|---|---|---|
| plan 前/中 | 原工作区 | 可重新决定 |
| stage 中 | 原目标 + 随机 stage | 目标未改，但需 reaper 清理（后续） |
| 原目标移到 backup 后 | 目标暂缺 + backup | 不能自动猜测，后续 D6/D7 恢复协议处理 |
| 新目标安装后、结果记录前 | 新目标可能已成功 | 不能重放 ToolCall，需 D7 ledger/unknown 语义 |
| 全部 commit 后、backup 清理中 | 目标为预期新状态，可能残留 backup | 仍不能把工具结果当 exactly-once |

D6 负责从已提交安全边界恢复模型上下文和 Run；D7 负责 durable tool claim、result
record 和这些崩溃窗口的 `OUTCOME_UNKNOWN`。D4 不提前复制一套不完整 ledger。

## 8. 测试与离线验收

D4 新增测试：

- `tests/test_patch_protocol.py`：版本、重复 key、严格类型、Add/Update/Delete、BOM、
  CRLF、末尾换行、exact context、重叠/no-op、行数和配置硬上限。
- `tests/test_patch_transaction.py`：多文件成功、所有 preflight 零写入、第二个 stage
  失败、原文件已移走故障、commit 中途故障、反向回滚、UNKNOWN、并发 identity
  变化、workspace lock 单赢家、bounded diff。
- `tests/test_patch_tools.py`：组合 Registry、稳定模型可见错误、内部命名空间、真实
  Agent Loop `read -> patch -> read -> final`。

全量命令：

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = "src"
python -W error::ResourceWarning -B -m unittest discover -s tests -v
```

D4 完成时结果：150 tests / OK，3 个既有平台能力 skip。

离线闭环：

```powershell
python -W error::ResourceWarning -B examples/day04_atomic_patch.py
```

示例实际执行：同一模型回合读取 update/delete 两个 base → 一次三文件事务
（update/add/delete）→ 重新读取两个文件并列目录确认删除 → final → 新 Runtime 从
SQLite 重放 D1 Turn 为 `COMPLETED`，Thread 已 detach。

## 9. 面试讲法

30 秒版本：

> 我没有让模型直接自由写文件，而是定义了带 schema version、operation、base
> SHA-256 和 exact hunks 的结构化 Patch。系统先在内存里验证全部文件，再把新内容
> stage 到同文件系统，commit 前重新检查文件 identity 和 hash，原文件移到 backup
> 后才替换；中途失败反向回滚，无法证明回滚则进入 outcome unknown。它解决的是正常
> 进程内的多文件部分应用问题，不冒充跨崩溃 exactly-once，后者由后续 ledger 处理。

常见追问：为什么 hash 和 identity 都要比较？

> Hash 证明内容仍是模型读取的版本，identity 证明路径没有在窗口中被换成另一个同内容
> 对象。二者覆盖的竞态不同；同时 parent directory 也绑定 identity。

常见追问：为什么不用数据库事务包文件写入？

> SQLite 和普通文件系统不是同一个事务资源，不能通过把两个函数放进一个 try 块就获得
> 原子提交。D4 对文件系统内部采用 stage/backup/replace/rollback；D7 再用 durable
> ledger 明确数据库记录与外部副作用之间的崩溃窗口，而不是声称不存在窗口。
