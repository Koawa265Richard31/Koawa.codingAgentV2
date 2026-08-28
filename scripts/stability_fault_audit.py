"""AST audit for named production faults; reachability still needs kill tests."""
from __future__ import annotations

import ast
import json
from pathlib import Path

from koawa_agent_v2.telemetry.faults import FAULT_SPECS, FaultPoint, _FACT_KEYS


def production_files(root: Path):
    return sorted(path for path in root.rglob("*.py") if path.name != "faults.py")


def audit_sites(root: Path) -> dict:
    declared, direct, literals, unknown, unknown_facts = set(), set(), [], [], []
    for path in production_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "FaultPoint":
                member = FaultPoint.__members__.get(node.attr)
                if member is None:
                    unknown.append(f"{path.name}:{node.lineno}:{node.attr}")
                else:
                    declared.add(member.value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in FAULT_SPECS:
                literals.append(f"{path.name}:{node.lineno}:{node.value}")
            if isinstance(node, ast.Call) and node.args:
                function = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if function not in {"hit", "_fault", "inject_fault", "emit_fault"}:
                    continue
                if function == "_fault" and path.relative_to(root).as_posix() not in {
                    "agents/control.py", "agents/scheduler.py",
                }:
                    # Other historical APIs use _fault for error construction
                    # or their separate D5/D7 contract, not the I8 FaultPort.
                    continue
                if len(node.args) > 1 and isinstance(node.args[1], ast.Dict):
                    allowed = _FACT_KEYS | ({"message_ids"} if function == "_fault" else set())
                    for key in node.args[1].keys:
                        if isinstance(key, ast.Constant) and key.value not in allowed:
                            unknown_facts.append(f"{path.name}:{node.lineno}:{key.value}")
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    if argument.value not in FAULT_SPECS:
                        unknown.append(f"{path.name}:{node.lineno}:{argument.value}")
                if isinstance(argument, ast.Attribute) and isinstance(argument.value, ast.Name) and argument.value.id == "FaultPoint":
                    member = FaultPoint.__members__.get(argument.attr)
                    if member is not None:
                        direct.add(member.value)
    return {"declared": sorted(declared), "direct_calls": sorted(direct),
            "legacy_literals": literals, "unknown": unknown, "unknown_fact_keys": unknown_facts,
            "missing": sorted(set(FAULT_SPECS) - declared)}


def constant_patch(root: Path) -> tuple[str, list[str]]:
    """Produce, but never apply, a mechanical literal -> registry constant patch."""
    patch, changed = ["*** Begin Patch"], []
    by_value = {member.value: member.name for member in FaultPoint}
    for path in production_files(root):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        replacements = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in by_value:
                if node.lineno != node.end_lineno:
                    raise ValueError("multiline fault literal requires manual edit")
                replacements.setdefault(node.lineno, []).append((node.col_offset, node.end_col_offset, by_value[node.value]))
        if not replacements:
            continue
        lines = source.splitlines()
        patch.append(f"*** Update File: {path.as_posix()}")
        if not any(isinstance(node, ast.ImportFrom) and any(alias.name == "FaultPoint" for alias in node.names)
                   for node in ast.walk(tree)):
            patch.extend(["@@", " from __future__ import annotations",
                          "+from koawa_agent_v2.telemetry.faults import FaultPoint"])
        for line_number, positions in sorted(replacements.items()):
            old = lines[line_number - 1]
            new = old.encode("utf-8")
            for start, end, member in sorted(positions, reverse=True):
                new = new[:start] + f"FaultPoint.{member}".encode() + new[end:]
            patch.extend(["@@", "-" + old, "+" + new.decode("utf-8")])
        changed.append(path.relative_to(root).as_posix())
    patch.append("*** End Patch")
    return "\n".join(patch), changed


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "src/koawa_agent_v2"
    patch, files = constant_patch(root)
    print(json.dumps({"patch": patch, "files": files}))
