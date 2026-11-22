# 会话签发身份:root 从哪来,子代理凭什么被信任

日期:2026-11-02。对应 issue #18(F2),代码在 PR 分支 `feat/session-root-issuance`。

这篇文档只回答一个问题:**在这个系统里,"谁是 root"是凭什么确定的。** 不涉及多 Agent 的环境隔离(那部分在 `agent-team-security-baseline.md`)。

## 一、之前的问题,用一句话说

**root 只是一个可以随便传的名字。**

具体在哪:

- `Principal("root", scopes)` 是装配时写死的字符串(`assembly.py:1142`);
- 策略规则里 `principal_ids=("root",)` 也是字符串;
- 谁能写出这个字符串,谁就是 root。

所以任何能调用接口的代码,传一个 `"root"` 就拿到了 root 的全部工具权限。这不是理论问题,探针跑出来是 ACCEPTED。

## 二、决定

**root 身份在会话创建时签发。**

也就是说:

> 你启动一个会话 → 这个会话就是 root;
> 别人想当 root → 必须先是"你创建的那个会话",而不是"声明自己叫 root"。

这条把"名字"换成了"记录":root 的凭据不是字符串,而是**事件流里那一行 `thread.created.v1`**。

## 三、具体改了什么

### 1. 会话记录里写上是谁建的

`thread.created.v1` 的 payload 增加了 `owner_id`:

```json
{"thread_id": "...", "workspace_ref": "...", "owner_id": "local-user"}
```

老的会话没有这个字段,回放时是 `None`——它们只是"无法证明主人是谁",不会导致回放失败。

### 2. 创建 root 必须指向一个已存在的会话

`AgentControlPlane` 现在可以配置:

```python
AgentControlPlane(store, owner_id="local-user", root_principal_id="root")
```

配了 owner 之后,创建 root 必须满足全部四条,否则拒绝:

| 检查 | 失败码 |
| --- | --- |
| 必须带 `thread_id` | `root_spawn_requires_session` |
| 该会话必须存在,且第一条事件是 `thread.created.v1` | `session_root_unissued` |
| 会话的 owner 必须等于本运行时的 owner | `session_owner_mismatch` |
| 只能用被签发的 root 名字 | `root_principal_not_issued` |

### 3. 组合运行时先建会话再建 root

`runtime/unified.py` 原来是直接 spawn 一个 root;现在先 `create_thread`,再带着 `thread_id` 去 spawn。

### 4. 没配 owner 时保持原样

嵌入式用法和测试不需要建会话。这是**为兼容保留的路径**,生产装配会配 owner。

## 四、为什么这样就"不可伪造"

伪造 root 需要什么?

| 做法 | 结果 |
| --- | --- |
| 传 `principal_id="root"` | 没有会话 → `root_spawn_requires_session` |
| 传别人的 `thread_id` | owner 不匹配 → `session_owner_mismatch` |
| 自己造一个会话 | 需要往 **thread 流**写事件:精确版本 CAS + 幂等键 + 全量审计 |
| 直接用 `Principal("root", ...)` | 见第五节,这条还没堵 |

所以攻击面从"传一个字符串"变成了"往审计日志里写一条可追溯的伪造记录"。后者不是不可能,但会留痕,而且需要拿到事件存储。

## 五、还没做的(必须说清楚)

1. **`Principal` 仍然可以被凭空构造。** 它是一个普通 dataclass,进程内有 store 的人可以自己造一个叫 `root` 的。这次堵的是**委派路径**,不是**进程内路径**。
   → 要堵这条,需要"上下文能力"(MAC),那是下一步。

2. **策略规则仍然按名字匹配。** 单根前提下没问题;出现多个特权身份时必须改成按 `agent_id` 绑定。

3. **合并边界(F8)没做。** 子代理的改动怎么进父代理的候选,目前没有授权和溯源。

## 六、怎么验证

```powershell
$env:PYTHONPATH = "src"
python -m unittest tests.test_session_root_issuance -v
```

7 项,覆盖:没有会话拒绝、陌生会话拒绝、别人建的会话拒绝、非 root 名字拒绝、正常签发、会话记录带 owner、无 owner 时兼容。

相关回归:thread / d11 控制面 / 调度 / 恢复 / 装配 / 统一运行时共 128 项通过。

## 七、一句话总结

**以前 root 是一个名字,现在 root 是一条记录。**
名字可以随便写,记录写进去要过 CAS、要留痕、要能追溯到是谁建的会话。
