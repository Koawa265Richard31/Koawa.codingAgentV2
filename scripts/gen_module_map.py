"""Generate a detailed module map from the source tree (AST, no imports).

Output: docs/note/module-map-detailed.md

Why generated: a hand-written map drifts.  This walks every module, lists
public classes (with dataclass fields = contract tables), public functions
with signatures and one-line docstrings, module-level string constants
(event names, error codes) and the package's own docstring.

Run:  PYTHONPATH=src python scripts/gen_module_map.py
"""

from __future__ import annotations

import ast
import datetime as _dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "koawa_agent_v2"
OUT = ROOT / "docs" / "note" / "module-map-detailed.md"

MAX_DOC = 110


def _first_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node) or ""
    line = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    if len(line) > MAX_DOC:
        line = line[: MAX_DOC - 1] + "…"
    return line


def _sig(fn: ast.AST) -> str:
    try:
        args = ast.unparse(fn.args)
    except Exception:
        args = "..."
    returns = ""
    if getattr(fn, "returns", None) is not None:
        try:
            returns = " -> " + ast.unparse(fn.returns)
        except Exception:
            returns = ""
    return f"({args}){returns}"


def _class_fields(node: ast.ClassDef) -> list[str]:
    fields: list[str] = []
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            ann = ast.unparse(item.annotation)
            if item.value is not None:
                try:
                    fields.append(f"{item.target.id}: {ann} = {ast.unparse(item.value)}")
                except Exception:
                    fields.append(f"{item.target.id}: {ann}")
            else:
                fields.append(f"{item.target.id}: {ann}")
    return fields


def _constants(tree: ast.Module) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in tree.body:
        if (
            isinstance(item, ast.Assign)
            and len(item.targets) == 1
            and isinstance(item.targets[0], ast.Name)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        ):
            name = item.targets[0].id
            value = item.value.value
            if value and len(value) <= 80:
                out.append((name, value))
    return out


CURATED: tuple[tuple[str, str, str], ...] = (
    ("control/event_store.py", "StreamId / NewEvent / StreamWrite / AppendReceipt", "写入契约:精确版本 + 幂等键"),
    ("control/models.py", "ThreadState / TurnState", "聚合状态:重放出来的唯一真相"),
    ("policy.py", "Principal / PolicyRule / ResolvedAction", "授权三件套:身份、规则、动作"),
    ("execution/loop.py", "ToolExecutionContext", "工具上下文:身份与作用域应从这里取"),
    ("ledger/protocol.py", "ToolRecoveryProfile", "恢复语义:重试 / 查询 / 人工"),
    ("verification/runner.py", "CommandProfile / CommandResult", "测试执行契约:argv 与结果事实"),
    ("verification/release_contracts.py", "ProfileReleaseContract", "披露契约:四档 + 字段白名单"),
    ("verification/dependency_preparation.py", "DependencyScope / PreparationDecision", "依赖准备:三态决策"),
    ("agents/control.py", "AgentRecord / AgentBudgetLimits", "委派契约:父子、身份、scope、预算"),
    ("tools/shell.py", "ShellLimits", "受限 shell:白名单与上限"),
)


def _render_module(path: Path, rel: str, level: str = "###") -> tuple[str, int, int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return f"{level} `{rel}`\n\n> 解析失败\n", 0, 0
    lines: list[str] = [f"{level} `{rel}`", ""]
    doc = _first_line(tree)
    if doc:
        lines.append(f"> {doc}")
        lines.append("")
    classes = 0
    functions = 0
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            classes += 1
            base = ", ".join(ast.unparse(b) for b in node.bases) if node.bases else ""
            head = f"- **class `{node.name}`**" + (f" ({base})" if base else "")
            lines.append(head)
            cdoc = _first_line(node)
            if cdoc:
                lines.append(f"  - {cdoc}")
            fields = _class_fields(node)
            if fields:
                lines.append("  - 字段:")
                for field in fields:
                    lines.append(f"    - `{field}`")
            methods = [
                m
                for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not m.name.startswith("__")
            ]
            if methods:
                lines.append("  - 方法:")
                for method in methods:
                    mdoc = _first_line(method)
                    suffix = f" — {mdoc}" if mdoc else ""
                    lines.append(f"    - `{method.name}{_sig(method)}`{suffix}")
            lines.append("")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            functions += 1
            fdoc = _first_line(node)
            suffix = f" — {fdoc}" if fdoc else ""
            lines.append(f"- **`{node.name}{_sig(node)}`**{suffix}")
    consts = _constants(tree)
    if consts:
        lines.append("")
        lines.append("- 常量(字符串):")
        for name, value in consts:
            lines.append(f"  - `{name}` = `{value}`")
    lines.append("")
    return "\n".join(lines), classes, functions


def main() -> int:
    packages = sorted(
        p for p in SRC.iterdir() if p.is_dir() and not p.name.startswith("__")
    )
    doc: list[str] = [
        "# 模块详细地图(自动生成)",
        "",
        f"生成时间:{_dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "生成方式:`python scripts/gen_module_map.py`(AST 解析,不导入代码)",
        "",
        "**怎么用**:先看每节开头的职责行 → 再看「字段」找契约表 → 再看「方法/函数」找入口。",
        "私有成员(`_x`)不列出;文档字符串截断到 110 字符。",
        "",
        "## 目录",
        "",
    ]
    body: list[str] = []
    summary: list[tuple[str, int, int, int]] = []
    for package in packages:
        files = sorted(package.glob("*.py"))
        if not files:
            continue
        pkg_doc = ""
        init = package / "__init__.py"
        if init.exists():
            try:
                pkg_doc = _first_line(ast.parse(init.read_text(encoding="utf-8")))
            except SyntaxError:
                pkg_doc = ""
        body.append(f"## `{package.name}/`")
        body.append("")
        if pkg_doc:
            body.append(f"> {pkg_doc}")
            body.append("")
        total_c = total_f = 0
        for file in files:
            rel = f"{package.name}/{file.name}"
            text, c, f = _render_module(file, rel)
            body.append(text)
            total_c += c
            total_f += f
        summary.append((package.name, len(files), total_c, total_f))
    for name, nfiles, nclasses, nfuncs in summary:
        doc.append(
            f"- [`{name}/`](#{name}) — {nfiles} 文件 / {nclasses} 类 / {nfuncs} 函数"
        )
    loose = sorted(
        p for p in SRC.glob("*.py") if p.name != "__init__.py"
    )
    if loose:
        doc.append("")
        doc.append("顶层模块:")
        for file in loose:
            body.append(f"## `{file.stem}`")
            body.append("")
            text, c, f = _render_module(file, file.name, level="###")
            body.append(text)
            doc.append(f"- [`{file.stem}`](#{file.stem}) — {c} 类 / {f} 函数")
    doc.append("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.extend(
        [
            "## 关键契约速查",
            "",
            "20 个模块里,先从这几张表开始看;其余是能力实现,不是判定边界。",
            "",
            "| 文件 | 契约 | 管什么 |",
            "| --- | --- | --- |",
        ]
    )
    for path, names, why in CURATED:
        doc.append(f"| `{path}` | {names} | {why} |")
    doc.append("")
    OUT.write_text("\n".join(doc) + "\n" + "\n".join(body), encoding="utf-8")
    print(f"wrote {OUT} ({len(doc) + len(body)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
