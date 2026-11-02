# 逻辑层绕行面审计（issue #10）

日期：2026-11-01。基线：`926a9df` + 本 PR 分支。

本文只回答一个问题：**逻辑层的哪些约束可以被绕开，绕开之后由谁兜底。**
每条结论必须标 `实测` / `代码推断` / `未知`，三种状态不得混写。

## 总表

| # | 绕行面 | 兜底层 | 状态 | 复验方式 |
| --- | --- | --- | --- | --- |
| 1 | 测试执行 = 任意代码执行 | 容器（`--network none`、非 root、只读根）；host 需显式 `allow_unisolated_execution` | 实测 | `tests/test_dependency_deadlock.py::...host_runner_requires_explicit_opt_in` |
| 2 | 测试面被改写制造假通过 | 默认 `protected_paths` + 收集数基线 | 实测 | 同上 `test_test_surface_is_protected_by_default`、`test_collection_regression_is_not_a_pass` |
| 3 | 路径校验与使用之间的 TOCTOU | 容器只读挂载；逻辑层仅有包含校验 | **未知**（竞态未实测） | 见下节 |
| 4 | MCP server 自身副作用 | 仅容器化 MCP 有边界 | 代码确认 | `tests/test_logic_bypass_audit.py`（envelope 标注） |
| 5 | 解析器 bug 面（ReDoS） | `search_text` 为字面匹配，不是正则 | 实测 | `tests/test_logic_bypass_audit.py::test_search_text_is_literal` |
| 6 | 失败归因交给模型 | 运行时确定性分类 | 实测 | `tests/test_dependency_deadlock.py`（A 组） |
| 7 | 披露边界 ≠ 副作用边界 | 文档区分 | 已完成（本文 §2） | 人工阅读 |

## 1. 测试执行 = 任意代码执行

模型写的代码在 `run_test_profile` 时被真实执行。

- **容器路径**：`--network none`、`--user 65532:65532`、`--cap-drop ALL`、
  `no-new-privileges`、`--read-only`、只挂候选副本。内核裁决，逻辑层写错也炸不出工作区。
- **host 路径**：以调用账户身份执行，**没有**文件系统或网络隔离。

处置：JSON 配置里 `runner: "host"` 必须同时给出 `allow_unisolated_execution: true`，
否则装配失败（`unisolated_execution_forbidden`）。这是一次显式的、可 grep 的决策，
而不是继承来的默认值。回执与 `finalize_task` 都会带 `isolation` 标记。

残余风险：选择了 host 就等于接受该风险。这一点不再由代码兜底。

## 2. 测试面被改写

处置：
- `protected_paths` 默认保护 `tests/`、`test/`、`conftest`、`pyproject.toml`、
  `pytest.ini`、`tox.ini`、`setup.cfg`；把它显式配成空列表会导致装配失败
  （`verification_requires_protected_tests`）。
- 收集数基线：首次通过的运行记录收集数，之后出现更少的收集数即拒绝该证据
  （`test_collection_regression`）。

残余风险：完全重写测试路径之外的"夹具"文件（例如自建 helper）仍可能改变语义；
基线只比较数量，不比较身份。若要更强，需要把测试面摘要并入候选身份。

## 3. TOCTOU（未知）

已确认的确定性部分：路径解析后必须落在工作区内，junction/symlink 逃逸被拒绝
（既有 `tests/test_d21_agent_security.py` 覆盖）。

**未确认的部分**：校验与打开之间的时间窗。Windows 没有 `dir_fd`，理论上可以在两步之间
替换中间目录。目前没有复现该竞态，也没有稳定的复现方法（需要并发替换与精确时序）。

结论必须写成：**未知**。不能写成"已关闭"，也不能写成"不会发生"。
在没有复现之前，唯一可靠兜底是容器只读挂载。

## 4. MCP 副作用

MCP envelope 控制的是"什么回到模型"，不是"MCP server 能做什么"。
stdio server 是以调用账户身份运行的子进程，文件与网络副作用不受逻辑层约束。

处置：envelope 增加 `mcp_isolation` 字段（`container` / `none`）。
`none` 表示该 server 没有内核边界，是否接受由部署方决定。

## 5. 解析器 bug 面

`search_text` 的 `query` 是 `casefold()` 字面匹配（`tools/repository.py:773-774`），
**不编译正则**，因此模型可控的 pattern 不构成 ReDoS 向量。
`include` / `exclude` 走 `fnmatch`，同样不是正则。

其余解析器（补丁 JSON、路径、事件 JSON）在进程内运行，出错即运行者权限；
这条由容器路径兜底，逻辑层不承诺。

## 6. 失败归因

见 issue #7 A。模型对"这次失败是环境还是代码"的自我判断不再进入回执；
分类由运行时在本地完成，只输出封闭枚举与包名。

## 7. 披露与副作用必须分开表述

- **披露边界**：什么内容可以进入模型（输出门、披露契约、撤销）。
- **副作用边界**：执行了什么、改变了什么（账本、隔离、候选身份）。

两者不是同一个保证。输出门全绿不代表副作用没发生；反过来，隔离到位也不代表
输出可以随意放行。任何安全声明都要标注它覆盖的是哪一侧。

## 复验命令

```powershell
$env:PYTHONPATH = "src"
python -m unittest tests.test_logic_bypass_audit tests.test_dependency_deadlock tests.test_shell_tool -v
```
