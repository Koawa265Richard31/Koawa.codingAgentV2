# D3：Tool Registry 与有界只读仓库工具

D3 把 D2 的抽象 `ToolExecutor` 端口接成了真实 Coding Agent 能力：模型看到一份
冻结的工具目录，完整 ToolCall 先经过同源 JSON Schema 和 typed args 校验，再通过
固定 workspace root 的路径解析器执行有界 `read_file`、`list_files`、`search_text`。

它解决的不是“Python 能不能 `open()` 文件”，而是下面这条可信边界：

```text
Provider ToolCall
  -> D2 完整 response 校验
  -> Registry 工具名 + schema gate
  -> frozen typed arguments
  -> workspace-relative path gate
  -> bounded read/list/literal search
  -> 有界 JSON ToolResult
  -> 下一轮模型基于真实证据回答
```

## 1. 模块职责

| 文件 | 负责什么 | 刻意不负责什么 |
| --- | --- | --- |
| `src/koawa_agent_v2/tools/errors.py` | 稳定工具错误类型；构造不回显原始值的有界错误 JSON | 文件系统、schema 编译、持久化 |
| `src/koawa_agent_v2/tools/schema.py` | 从一份 `ToolSpec` 同时生成 Provider definition、运行时 validator、typed dataclass decoder | 工具分发、业务 handler |
| `src/koawa_agent_v2/tools/registry.py` | 注册、确定性 definition 快照、seal、唯一 dispatch 入口 | 路径解析、具体工具语义 |
| `src/koawa_agent_v2/tools/workspace.py` | 固定 root；拒绝路径逃逸/链接；通过已验证的 fd/handle 做有界读与目录枚举 | 文本编码、搜索、模型结果 JSON |
| `src/koawa_agent_v2/tools/repository.py` | 三个只读工具的 schema、typed args、资源预算、结果格式与 Registry 装配 | 写文件、Git、Shell、MCP |
| `examples/day03_repository_read_loop.py` | 真实临时仓库 + SQLite + 三轮脚本模型的 search→read→final 闭环 | 网络 Provider、虚构文件结果 |

读源码时建议按这个顺序：`tools/schema.py` → `tools/registry.py` →
`tools/workspace.py` → `tools/repository.py` → 示例。先弄清“调用是否允许”，再看
“允许后怎样读”。

## 2. 放进一个完整 Turn 里看

示例实际执行三次 ModelTurn，而不是把搜索和读取藏在一个本地函数里：

```text
D1 create Thread + QUEUED Turn
  -> TurnWorker.start_turn() 得到本次 run_id
  -> ModelTurn #1: search_text("CHECKPOINT_PAYLOAD", path=".")
  -> ModelStreamAssembler 验完整条流
  -> ToolRegistry.decode(SearchTextArguments)
  -> WorkspacePathResolver 有界遍历并读取候选文件
  -> ToolResultMessage(search JSON)
  -> ModelTurn #2: 模型解析真实 match.path，再调用 read_file(match.path)
  -> Registry + Resolver 读取已验证文件并返回 content + sha256
  -> ToolResultMessage(read JSON)
  -> ModelTurn #3: 模型检查真实正文后输出 final
  -> TurnWorker.complete_turn(run_id fence)
  -> D1 同事务写 COMPLETED + thread.turn-detached
  -> 新 Runtime 从 SQLite 重放出相同终态
```

这里有三种容易混淆的 `Turn`：

- D1 `Turn`：一次用户任务的 durable 生命周期；示例只有一个。
- `ModelTurn`：一次 Provider request/response；示例有三个。
- ToolCall：某个 `ModelTurn` 的完成态输出；示例前两轮各一个。

只有 `ItemCompleted` 和 `TurnCompleted` 都通过 D2 校验，ToolCall 才能到达 D3。
D3 不消费半截 `ToolArgumentsDelta`。

## 3. ToolSpec：为什么必须只有一份 schema

一个工具若分别维护以下三份规则，很快就会漂移：

1. 发给模型的 JSON Schema；
2. Registry 运行时校验；
3. handler 接收的 Python 参数类型。

`ToolSpec(name, description, arguments_type, input_schema)` 在启动时把同一份输入编译
为三者：

- `definition()` 返回 D2 `ToolDefinition`，用于 `ModelRequest.tool_definitions`；
- `decode(arguments_json)` 严格解析模型参数，并构造 frozen dataclass；
- 编译得到的 canonical schema 使用稳定 key 顺序，Provider 看见的上限就是 handler
  实际执行的上限。

### D3 接受的 JSON Schema 子集

根对象必须精确包含：

```json
{
  "type": "object",
  "properties": {},
  "required": [],
  "additionalProperties": false
}
```

属性只允许：

- `string`：必须有 `maxLength`，可有 `minLength`、`description`；
- `integer`：必须有 `minimum` 和 `maximum`；Python 的 `bool` 不算 integer；
- `boolean`；
- `array`：必须有 `items` 和 `maxItems`，元素只能是 scalar，不能嵌套 object/array。

不支持的 keyword、`$ref`、组合 schema、嵌套对象会在启动时被拒绝，不会“尽量
理解”。`arguments_type` 必须是 frozen dataclass，字段集合与 schema 完全相同；required
字段不能有默认值，可选字段必须有经过同一 schema 验证的默认值。

直接调用 `ToolSpec.decode()` 时，运行期 `arguments_json` 还会拒绝：

- malformed JSON、顶层非 object、`NaN` 等非 JSON number；
- duplicate key；
- 缺 required 字段、额外字段、错误类型；
- 超过 string/integer/array 边界的值，以及不能编码为 UTF-8 的未配对 surrogate。

正常 AgentLoop 中，duplicate key 会更早被 D2 canonical `ToolCallItem` 当作 Provider
protocol failure 拒绝，根本不会进入 Registry。其他预期参数错误不会抛出含原始值的
异常，而是返回：

```json
{"error":{"code":"invalid_tool_arguments","field":"path","reason":"wrong_type"}}
```

handler 没有被调用，模型可以在下一轮修正参数。

### `tools/schema.py` 关键函数职责

| 函数/类 | 意义 |
| --- | --- |
| `ToolSpec.__init__()` | 启动时校验工具名/描述/schema/dataclass，并冻结 canonical definition 与 decoder |
| `ToolSpec.definition()` | 返回给 D2/Provider 的 immutable `ToolDefinition` |
| `ToolSpec.decode()` | 严格解析一次完整 `arguments_json`，返回 typed frozen args |
| `_compile_object_schema()` | 校验根 object、required、properties、`additionalProperties=false` |
| `_compile_value_schema()` | 编译受支持的 string/integer/boolean/array 及其上下限 |
| `_bind_arguments_type()` | 证明 dataclass 字段、annotation、required/default 与 schema 一致 |
| `_strict_arguments_object()` | 拒绝 duplicate key、非标准数字、畸形或顶层非 object JSON |

这些下划线函数是启动期/解码期内部边界，不是让业务 handler 绕过 `ToolSpec` 直接调用。

## 4. ToolRegistry：工具目录与分发表为什么必须一起冻结

`ToolRegistry` 有两个阶段：

```text
注册期: register(spec, handler) ...
  -> definitions() 第一次取模型目录
运行期: sealed，register() 永久拒绝
```

`AgentLoop` 构造时从 executor 取得一次 `definitions()` 快照，因此 Registry 在 durable
Turn 启动前就 seal。模型看到的工具目录和 `execute()` 使用的分发表来自同一组 entry，
不会出现“模型没见过新工具，但另一个线程刚好注册后还能执行”的竞态。

### `tools/registry.py` 三个核心函数

| 函数 | 意义 |
| --- | --- |
| `register(spec, handler)` | 只在注册期接收 callable typed handler；拒绝重名和 seal 后修改 |
| `definitions()` | 按工具名排序、缓存同一个 tuple，并原子 seal Registry |
| `execute(call, context=...)` | 先确保 seal，再做 name gate、`ToolSpec.decode()`、typed handler 调用和结果类型检查 |

错误分层要区分：

- 配置错误，如 `duplicate_tool_name`、`invalid_tool_schema`、
  `tool_registry_sealed`：启动前抛出，不能先把 D1 Turn 置为 RUNNING；
- 模型可修正错误，如 `invalid_tool_arguments`、路径不存在、二进制文件：返回
  `ToolExecutionResult(is_error=True)`，仍进入下一轮模型；
- handler 崩溃或返回非 `ToolExecutionResult`：属于本地执行器合同破坏，Registry 不把
  底层异常正文发给模型，外层 AgentLoop 统一收口为 `tool_executor_failed`。

若直接调用空 Registry，未知名称返回稳定 `unknown_tool` JSON。进入 AgentLoop 时，
D2 会先检查本轮所有调用名；只要一个不存在，就以 `unknown_tool_requested` 在执行
第一个 handler 前整体失败，避免部分执行。

## 5. WorkspacePathResolver：校验和使用不能分成两步

下面这种 API 不安全：

```text
resolve(model_path) -> host Path
稍后 open(host Path)
```

因为验证结束到 `open()` 之间，路径组件可能被替换成 symlink/junction。D3 的 resolver
不把 host `Path` 返回给 handler；它暴露的是“验证并立即使用”的操作：

- `read_bytes(path, max_bytes, progress_guard=None)` → `WorkspaceFileRead`；
- `list_directory(path, max_entries, max_scan_entries, progress_guard=None)` →
  `WorkspaceDirectoryListing`；
- `close()` / context manager 释放绑定 root 的 fd/handle。

结果只含规范化的 `/` 相对路径、bytes/大小/SHA-256 或目录 entry，不暴露 workspace
绝对路径。

### 第一层：跨平台 lexical gate

`_relative_parts()` 在碰文件系统前拒绝：

- POSIX 绝对路径、Windows drive/root、UNC、device path；
- `..`、盘符切换、ADS 的 `:`；
- NUL/控制字符、过长路径/组件；
- Windows 尾随点/空格与 `CON`、`NUL`、`COM1` 等保留名。

`.` 和重复分隔符会规范化，`\` 与 `/` 都可作为输入分隔符，结果统一成 `/`。
workspace root 必须是已经存在的真实目录，构造时 strict canonicalize 并绑定身份。

### 第二层：拒绝链接和特殊文件

`_preflight()` 会逐组件 `lstat`。D3 采取保守规则：workspace 下所有 descendant
symlink、junction、reparse point 都拒绝，即使它们最终仍指向 workspace 内。目录枚举
还拒绝 FIFO、socket、device 等特殊 entry；文件读取只接受 regular file。

### 第三层：OS 句柄约束

POSIX：

- 保存 workspace root fd；
- 每个组件用 `dir_fd + O_NOFOLLOW` 逐级打开；
- 从已打开 fd 读取 `limit + 1`，并在前后核对 object/root identity；
- 文件在读取期间变化则返回 `workspace_object_changed`。

Windows：

- root 与目标目录使用 Win32 handle；
- 文件打开后通过 handle final path 再做 containment，核对 file identity；
- 目录 `scandir` 前后核对 handle identity，重新打开目录核对身份，并对每个 entry
  做 no-follow/preflight；
- 静态恶意仓库中的 junction/reparse 逃逸会 fail closed。

必须公开一个已知边界：Python 在 Windows 没有等价的 directory-relative handle 枚举
API。本实现不声称能抵抗一个拥有宿主并发改名能力、并在 `scandir` 小窗口内反复
swap/restore 的外部进程。D3 的目标是防住模型控制的静态恶意仓库路径；未来真正的
写工具仍需 Docker/worktree 隔离，不能拿 D3 resolver 冒充完整沙箱。

### 为什么目录有 scan limit 和 result limit 两个数

OS 枚举顺序不稳定。如果扫到第 N 项就停止，再排序，得到的“前 N 项”会随平台变化。
因此 `list_directory()` 必须先在独立 `max_scan_entries` 内完成枚举和排序，再截取
`max_entries`。超过 result limit 返回确定性的 `truncated=true`；超过 hard scan limit
则以 `workspace_directory_scan_limit_exceeded` 失败，绝不返回随机 partial list。

### `tools/workspace.py` 关键函数职责

| 函数 | 意义 |
| --- | --- |
| `WorkspacePathResolver.__init__()` | strict 绑定 root、硬预算和 root fd/handle identity |
| `read_bytes()` | lexical/preflight 后，从已验证对象做 `limit+1` 有界读并计算 SHA-256 |
| `list_directory()` | 有界枚举直接子项、稳定排序、返回独立 scan/result 元数据 |
| `_relative_parts()` / `_validate_component()` | 拒绝跨 POSIX/Windows 的路径攻击形式 |
| `_preflight()` | 逐组件拒绝链接/reparse，确认 canonical containment |
| `_open_posix()` | 使用 root fd、`dir_fd`、`O_NOFOLLOW` 逐级打开 |
| `_read_posix()` / `_read_windows()` | 针对 OS 的 handle-bound read 与前后 identity 检查 |
| `_list_posix()` / `_list_windows()` | 针对 OS 的安全目录枚举与 change detection |
| `close()` | 幂等释放 root fd/handle；关闭后稳定返回 `workspace_resolver_closed` |

`progress_guard` 是 resolver 的协作检查钩子。当前 D3 主链由 AgentLoop 在进入每个
handler 前执行 cancellation/ownership guard；单次仓库操作还有严格字节/条目预算。
D3 不宣称撤销一个已经完成的读取。

## 6. 三个仓库工具

`build_repository_tool_registry(workspace_root, limits=...)` 是装配入口。它创建一个
resolver、由同一 `RepositoryToolLimits` 生成三个 `ToolSpec`、注册 typed handler，返回
支持 `with`/`close()` 的 `RepositoryToolRegistry`。调用者不需要另外维护 definitions。

### 6.1 `read_file`

参数：

```json
{"path":"src/app.py","start_line":1,"max_lines":200}
```

执行顺序：

1. Registry 解码为 `ReadFileArguments`；
2. resolver 在 `max_file_bytes` 内读取 regular file；
3. NUL bytes 视为二进制，其他内容用严格 `utf-8-sig` 解码；
4. 按一基 `start_line` 选择最多 `max_lines`；
5. 返回相对路径、正文、line metadata、原文件 byte length 和 SHA-256；
6. 最终 JSON 若接近 `max_output_chars`，只裁剪 `content` 并标记
   `output_truncated=true`。

成功结果形状：

```json
{
  "ok": true,
  "path": "src/app.py",
  "content": "...",
  "start_line": 1,
  "end_line": 42,
  "total_lines": 120,
  "byte_length": 4096,
  "sha256": "...64 hex...",
  "truncated": true,
  "output_truncated": false
}
```

如果 `start_line` 已超过 EOF，工具显式返回空 `content`、`end_line=null` 和
`truncated=true`，而不是伪造一个存在的行号。
极长相对路径在总输出预算不足时还会显式增加 `path_truncated=true`，不会静默突破
JSON 上限。

`binary_file`、`invalid_utf8`、`workspace_file_too_large` 都是显式错误，不会把乱码或
无界 preview 塞进模型。

### 6.2 `list_files`

参数：

```json
{"path":".","max_depth":2,"max_entries":200}
```

`max_depth=0` 表示列目标目录的直接子项但不向子目录下钻。工具用 resolver 做
确定性 DFS，最终按 canonical relative path 排序；entry 只有 `file`/`directory`，文件
附带 `byte_length`。`.git`、`.hg`、`.svn`、`__pycache__`（含大小写变体）只会作为
父目录入口被列出，不能成为 read/list/search 的目标，也不会递归或搜索正文；这避免
linked worktree 的 `.git` 文件把宿主绝对路径带进模型。

成功 JSON 含 `entries`、`returned_entries`、`scanned_entries`、`truncated` 与
`truncation_reason`。常见 result-level 截断原因是 `entry_limit` 或 `output_limit`；安全
scan hard limit 采用稳定错误而不是任意 partial。

### 6.3 `search_text`

参数：

```json
{
  "query": "ToolRegistry",
  "path": ".",
  "max_depth": 6,
  "max_files": 300,
  "max_matches": 50,
  "case_sensitive": true,
  "include": ["*.py"],
  "exclude": ["test_*" ]
}
```

`query` 是 literal string，不是 regex；`[a-z]+` 会按普通字符搜索。include/exclude 是
受数量/长度限制的 glob，匹配规范化 relative path 或 basename，exclude 最后生效。
`max_depth=0` 只搜索目标目录的直接文件，不向子目录下钻。

搜索按确定性路径顺序读取候选 regular files；单文件、候选文件数、累计扫描 bytes、
深度、match 数、单 match 文本和最终 JSON 都有上限。match 按
`(path, line, column)` 排序，line/column 都是一基。二进制、非法 UTF-8 和单文件过大
不会进入模型正文，而是计入 `skipped_binary`、`skipped_invalid_utf8`、
`skipped_too_large`。

成功 JSON 还包含 `scanned_entries/files/bytes`、`returned_matches`、`truncated` 与
`truncation_reason`。可能的截断原因包括 `file_limit`、`byte_limit`、`match_limit`、
`output_limit`；每条 match 只有 `path/line/column/text`。

### `tools/repository.py` 关键函数职责

| 函数/类 | 意义 |
| --- | --- |
| `RepositoryToolLimits` | 三个 schema 和三个 handler 共用的硬预算；非法/布尔/非正配置启动失败 |
| `repository_tool_specs()` | 从 limits 生成 read/list/search 的唯一 `ToolSpec` 集合 |
| `build_repository_tool_registry()` | 绑定 workspace resolver、注册三个 handler、返回 context-managed Registry |
| `_RepositoryTools.read_file()` | 安全 bytes → 严格文本 → line slice → bounded JSON |
| `_RepositoryTools.list_files()` | 有界递归、确定性 entry 列表与截断元数据 |
| `_RepositoryTools.search_text()` | glob filter、literal matching、扫描预算、skip 统计与排序 |
| `_walk()` | 只经 resolver 枚举的确定性 DFS；统一累计 scan/file/depth 预算 |
| `_try_decode_text()` | 拒绝 NUL binary，严格解码 UTF-8 BOM/UTF-8 |
| `_iter_text_lines()` | 懒迭代 splitlines 边界；百万短行不会先物化成巨大 Python list |
| `_matches_patterns()` | 在 validated relative path/basename 上做 bounded glob |
| `_literal_columns()` | 最多按剩余 match budget 找 literal match；casefold 仍映射回原文列，不执行用户 regex |
| `_bounded_match_text()` | 裁剪单条命中行，保留查询附近证据 |
| `_bounded_string_payload()` | 在最终 JSON 字符预算内裁剪 read content 并标记元数据 |
| `_fit_metadata_strings()` | 极长 path/query 必要时也显式裁剪，并增加对应 metadata 标志 |
| `_json()` | canonical、拒绝 NaN 的紧凑 JSON 编码 |

## 7. 预算不是一个数字

`RepositoryToolLimits` 默认同时限制：

- 路径、查询、glob 个数与单 glob 字符；
- 单文件 bytes、read 返回行数；
- 目录返回 entry 与实际 scan entry；
- 搜索文件数、累计 bytes、递归深度；
- match 数、单 match 文本；
- 每次工具结果总字符数。

Provider schema 会把调用方可选的 `max_lines/max_entries/max_files/max_matches/max_depth`
限制在 server hard ceiling 内。模型可以请求“少读一点”，不能通过参数把硬上限调大。
handler 仍独立执行硬预算，因此即使绕过 Provider 直接调用 Registry，也不会失去边界。

构造期还有不可调大的绝对 ceiling：单文件 16 MiB、单次搜索累计 64 MiB、目录扫描
100,000 entries、搜索 10,000 files、深度 64、10,000 matches、单结果 262,144 字符；
路径、query 和 glob 也有独立上界。超出这些值会在启动 durable Turn 前失败，不会把
一个极大 Python integer 一路传到 `read()` 才崩溃。

## 8. 失败语义

| 失败 | 结果 | handler/读取是否发生 | 模型能否下一轮修正 |
| --- | --- | --- | --- |
| schema 不支持、dataclass 漂移、重名、非法 limits | 启动期 `ToolConfigurationError` | 否；durable Turn 不启动 | 不能，先修本地配置 |
| 本轮含未知工具 | AgentLoop `unknown_tool_requested` | 本轮所有 handler 都不执行 | Turn 失败 |
| missing/extra/wrong type/非法 Unicode | `invalid_tool_arguments` error result | 业务 handler 不执行 | 能 |
| duplicate JSON key | D2 Provider protocol failure；直接 Registry 调用也拒绝 | 不形成 canonical ToolCall | 不能在同一非法 response 内修正 |
| 绝对路径、`..`、UNC/device/ADS/保留名 | `invalid_workspace_path` error result | 不访问目标 | 能 |
| symlink/junction/reparse | `workspace_path_link_forbidden` | 不跟随 | 能 |
| `.git/.hg/.svn/__pycache__` 控制路径 | `repository_control_path_forbidden`，父目录仍可列入口 | 不读取或搜索正文 | 能改查普通源码路径 |
| missing/access/type/change | 对应稳定 `workspace_*` error result | 只做有界验证；无正文结果 | 能 |
| read 二进制或非法 UTF-8 | `binary_file` / `invalid_utf8` | 有界读取后拒绝正文 | 能 |
| read 超大 | `workspace_file_too_large` | 最多 `limit+1`；无正文结果 | 能 |
| result/file/byte/match/output 软预算 | 成功 JSON，`truncated=true` + reason | 有界部分扫描 | 能继续缩小范围 |
| 目录超过安全 scan hard limit | `workspace_directory_scan_limit_exceeded` | 不返回随机 partial | 能缩小 path/depth |
| handler 未分类异常/返回错误类型 | AgentLoop `tool_executor_failed` | 视注入点而定；不泄漏异常正文 | 当前 Turn 失败 |
| cancellation/Run 失权 | AgentLoop 在 handler 前停止 | 尚未进入该 handler | 由 D1 状态决定 |

所有模型可见错误是最多 512 字符的 canonical JSON，只放稳定 code/reason/field，不放
绝对路径、原始恶意参数或底层异常正文。

## 9. 运行示例与验收

在 `D:\KoawaAgent\v2` 下：

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = "src"

python -W error::ResourceWarning -B examples/day03_repository_read_loop.py
python -W error::ResourceWarning -B -m unittest discover -s tests -v
```

本次 D3 收口在 Windows 上的实际全量结果是 `Ran 132 tests`、`OK (skipped=3)`；三个
skip 分别是当前主机无法创建普通 symlink、POSIX-only FIFO，以及 POSIX-only 非 UTF-8
文件名。Windows junction 攻击用例实际执行并通过。示例在 `ResourceWarning` 视为错误的条件下也通过，
输出 `model_rounds=3`、`tool_calls=2`、`turn_status=completed`、`detached=True`。

示例不是用 fake handler 返回预置字符串。它会：

1. 在临时目录创建真实 `.git` 形状仓库、README 和 `src/checkpoint.py`；
2. 用真实 SQLite Event Store 创建 D1 Thread/Turn；
3. 第一轮脚本模型调用真实 Registry 的 `search_text`；
4. 第二轮解析真实 ToolResult 的 `matches[0].path`，再调用 `read_file`；
5. 第三轮验证真实正文和 SHA-256 后才输出 final；
6. `TurnWorker` 提交 COMPLETED 并 detach Thread；
7. 关闭 Registry 的 root handle，新建 Store/Runtime，从 SQLite 重放并断言终态。

重点回归文件包括：

- `tests/test_tool_schema.py`
- `tests/test_tool_registry.py`
- `tests/test_workspace_paths.py`
- `tests/test_repository_tools.py`
- `tests/test_d3_registry_loop.py`
- `tests/test_d3_configuration_safety.py`

验收重点是：schema/decoder/definition 同源、Registry 冻结、路径逃逸失败、链接拒绝、
排序确定、每层预算有界、错误不泄漏，以及真实 search→read→final→SQLite replay 同时
成立。

## 10. D3 能讲什么，不能讲什么

面试时可以这样概括：

> 我没有把 Coding Tool 写成任意 `dict -> function`。每个工具用同一份 ToolSpec 生成
> Provider schema、运行时 validator 和 frozen typed args；Registry 第一次生成确定性
> 工具目录就 seal，保证模型视图和分发表一致。文件操作绑定固定 workspace root，
> 拒绝跨平台绝对路径、`..`、ADS 和所有 descendant symlink/junction/reparse；POSIX 用
> dir-fd/O_NOFOLLOW，Windows 用 handle final-path 与 identity 复核。Read/List/Search 的
> 文件、行、entry、扫描 bytes、match 和输出分别有预算，结果携带 hash 与 truncation
> 元数据。这个切片已真实贯通模型搜索、读取、回填 ToolResult、最终回答和 D1 终态。

D3 仍明确不能声称：

1. 没有写文件或 Apply Patch；D4 才实现原子 patch。
2. 没有 Git status/diff/test/Shell；D5 才形成修改审查闭环。
3. 没有 durable ModelTurn/ToolResult checkpoint；进程中途崩溃恢复属于 D6。
4. 没有 Tool Ledger 或 exactly-once 副作用；D3 只开放无外部写副作用的工具。
5. 没有 Docker sandbox、Policy/Approval、MCP 或多 Agent；分别属于后续切片。
6. Windows resolver 不等于抵抗有宿主并发改名权限的攻击者，也不等于容器隔离。
7. hard link 没有 symlink/reparse 标记；D3 只证明所读目录项位于绑定 workspace
   namespace 内，不证明同一 inode 在 root 外没有别名，也不处理 bind mount。完整隔离
   属于 D8 容器。

因此 D3 是一个可信、受限的只读 Coding Agent 工具闭环，不是“功能完整的 Coding
Agent”终点。
