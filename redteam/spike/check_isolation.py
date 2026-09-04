"""RT/J isolation spike checker (stdlib only).

Verifies the isolation contract of docs/agent-redteam-jailbreak-plan.md v1.1 §4:
  1. src/ and tests/ contain no pyrit or redteam imports.
  2. pyproject.toml declares no pyrit dependency (production stays stdlib-only).
  3. The redteam venv (if present) can import pyrit; the interpreter running
     production code paths (system python) cannot.

Never imports pyrit itself from this process: venv import is checked in a
subprocess so a globally-installed pyrit can never satisfy check 3 by accident.

Output: redteam/spike/results/isolation-<YYYYMMDD-HHMMSS>.json (JSON evidence).
Exit code 0 = pass, 1 = fail. Read-only with respect to the repository tree.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
TESTS_DIR = REPO_ROOT / "tests"
PYPROJECT = REPO_ROOT / "pyproject.toml"
VENV_PYTHON = REPO_ROOT / "redteam" / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
RESULTS_DIR = Path(__file__).resolve().parent / "results"

FORBIDDEN_TOKENS = ("import pyrit", "from pyrit", "from redteam", "import redteam")


def scan_forbidden_imports() -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for base in (SRC_DIR, TESTS_DIR):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                findings.append({"file": str(path.relative_to(REPO_ROOT)), "token": "<unreadable>", "line": str(exc)})
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if any(token in line for token in FORBIDDEN_TOKENS):
                    findings.append(
                        {
                            "file": str(path.relative_to(REPO_ROOT)),
                            "token": next(t for t in FORBIDDEN_TOKENS if t in line),
                            "line": f"{lineno}: {line.strip()[:120]}",
                        }
                    )
    return findings


def check_pyproject() -> dict[str, object]:
    if not PYPROJECT.is_file():
        return {"present": False, "pyrit_declared": None}
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    declared: list[str] = []

    def _scan(dep_map: object) -> None:
        if isinstance(dep_map, dict):
            for key, value in dep_map.items():
                if key == "dependencies" and isinstance(value, list):
                    declared.extend(str(item) for item in value if "pyrit" in str(item).lower())
                else:
                    _scan(value)
        elif isinstance(dep_map, list):
            for item in dep_map:
                _scan(item)

    _scan(data)
    return {"present": True, "pyrit_declared": declared}


def _probe_import(python: str) -> tuple[bool, str]:
    probe = subprocess.run(
        [python, "-c", "import pyrit; print(pyrit.__file__)"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return probe.returncode == 0, (probe.stdout or probe.stderr).strip()[:300]


def check_venvs() -> dict[str, object]:
    system_ok, system_detail = _probe_import(sys.executable)
    result: dict[str, object] = {
        "system_python": sys.executable,
        "system_can_import_pyrit": system_ok,
        "system_detail": system_detail,
    }
    if VENV_PYTHON.is_file():
        venv_ok, venv_detail = _probe_import(str(VENV_PYTHON))
        result["redteam_venv_present"] = True
        result["venv_can_import_pyrit"] = venv_ok
        result["venv_detail"] = venv_detail
    else:
        result["redteam_venv_present"] = False
        result["venv_can_import_pyrit"] = None
    return result


def main() -> int:
    findings = scan_forbidden_imports()
    pyproject = check_pyproject()
    venvs = check_venvs()

    failures: list[str] = []
    if findings:
        failures.append(f"forbidden imports found: {len(findings)}")
    if pyproject.get("pyrit_declared"):
        failures.append("pyproject declares pyrit")
    if venvs.get("system_can_import_pyrit"):
        failures.append("system/production python can import pyrit")
    if venvs.get("redteam_venv_present") and not venvs.get("venv_can_import_pyrit"):
        failures.append("redteam venv exists but cannot import pyrit")

    report = {
        "check": "rt-j isolation spike",
        "plan_ref": "docs/agent-redteam-jailbreak-plan.md v1.1 §4/§8.1",
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "forbidden_import_findings": findings,
        "pyproject": pyproject,
        "venvs": venvs,
        "failures": failures,
        "passed": not failures,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RESULTS_DIR / f"isolation-{stamp}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"evidence: {out}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
