"""D14 deterministic eval: fresh repos, oracle grading, failure report.

Run from ``v2/`` with ``PYTHONPATH=src``:

    python -B evals/run_eval.py evals/tasks evals/report.json
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from koawa_agent_v2.agents.graph import AgentError
from koawa_agent_v2.telemetry.faults import FaultInjector, classify_failure
from koawa_agent_v2.telemetry.trace import TraceStore


def run_task(
    task: dict[str, Any],
    *,
    trace: TraceStore,
    injector: FaultInjector,
) -> dict[str, Any]:
    task_id = task["id"]
    correlation_id = __import__("uuid").uuid5(
        __import__("uuid").NAMESPACE_URL, f"koawa-eval:{task_id}"
    )
    temporary = tempfile.TemporaryDirectory()
    repo = Path(temporary.name) / "repo"
    repo.mkdir()
    for name, content in task["files"].items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    try:
        if injector.should_fail("container_kill"):
            raise AgentError("container_kill")
        for name, content in task.get("patch", {}).items():
            (repo / name).write_text(content, encoding="utf-8")
        test = subprocess.run(
            task["test"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        tests_pass = test.returncode == 0
        oracle_pass = all(
            (repo / name).read_text(encoding="utf-8") == expected
            for name, expected in task["oracle"].items()
        )
        success = tests_pass and oracle_pass
        trace.append(
            correlation_id=correlation_id,
            stream="tool",
            kind="eval_task",
            fields={
                "kind": "eval_task",
                "result_code": "ok" if success else "failed",
                "failure_class": "" if success else "oracle",
                "duration_ms": 1,
            },
        )
        return {
            "id": task_id,
            "success": success,
            "tests_pass": tests_pass,
            "oracle_pass": oracle_pass,
            "tool_calls": len(task.get("patch", {})),
            "failure_class": "" if success else "oracle",
        }
    except AgentError as error:
        trace.append(
            correlation_id=correlation_id,
            stream="sandbox",
            kind="injected_failure",
            fields={
                "kind": "injected_failure",
                "result_code": error.code,
                "failure_class": classify_failure(error.code),
            },
        )
        return {
            "id": task_id,
            "success": False,
            "tests_pass": False,
            "oracle_pass": False,
            "tool_calls": 0,
            "failure_class": classify_failure(error.code),
        }
    finally:
        temporary.cleanup()


def main(tasks_dir: Path, report_path: Path, *, seed: str = "eval-seed") -> dict:
    tasks = sorted(
        [
            json.loads(path.read_text(encoding="utf-8"))
            for path in Path(tasks_dir).glob("*.json")
        ],
        key=lambda item: item["id"],
    )
    temporary = tempfile.TemporaryDirectory()
    store = __import__("koawa_agent_v2.control.sqlite_store", fromlist=["SqliteEventStore"])
    trace = TraceStore(store.SqliteEventStore(Path(temporary.name) / "trace.sqlite3"))
    injector = FaultInjector(seed=seed)
    results = [run_task(task, trace=trace, injector=injector) for task in tasks]
    report = {
        "total": len(results),
        "success": sum(1 for item in results if item["success"]),
        "failure_classification": _classify(results),
        "tasks": results,
        "injector": injector.to_document(),
        "mandatory_reliability": "scripted_deterministic_provider",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.cleanup()
    return report


def _classify(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        classification = item["failure_class"] or "success"
        counts[classification] = counts.get(classification, 0) + 1
    return counts


if __name__ == "__main__":
    report = main(Path(sys.argv[1]), Path(sys.argv[2]))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
