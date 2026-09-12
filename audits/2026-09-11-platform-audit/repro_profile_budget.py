"""F7: config allows up to 64 required_test_profiles (config.py:788-811), but
production assembly passes required_test_profiles WITHOUT verification_limits
(assembly.py:516-523), so the default budget max_test_runs=4 applies
(finalization.py:32-48). A config-valid 5-profile setup is then unsatisfiable:
the 5th run_test_profile reservation fails and finalize can never pass.

Production-shape wiring: registry built exactly like assembly.py (no
verification_limits argument). Isolated temp git repo; no production files.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.verification.runner import CommandOutcome, CommandResult
from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry

PROFILES = ("p1", "p2", "p3", "p4", "p5")


class Runner:
    profile_ids = PROFILES

    def validate_profile(self, profile_id: str) -> None:
        pass

    def run(self, profile_id, *, progress_guard=None, execution_id=None):
        return CommandResult(
            profile_id=profile_id,
            outcome=CommandOutcome.PASSED,
            exit_code=0,
            stdout="",
            stderr="",
            stdout_bytes=0,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=1,
            argv=("trusted-test", profile_id),
            timeout_seconds=10.0,
            backend="docker",
            immutable_image_id="sha256:" + "1" * 64,
            profile_digest="2" * 64,
        )


def _git(root: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    subprocess.run(
        (executable, "-C", str(root), *arguments),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def _call(name: str, arguments: dict, call_id: str) -> ToolCallItem:
    return ToolCallItem(
        0, f"item-{call_id}", call_id, name,
        json.dumps(arguments, separators=(",", ":")),
    )


def main() -> None:
    out: dict = {
        "profiles": list(PROFILES),
        "wiring": "required_test_profiles passed, verification_limits omitted (production shape)",
        "runs": [],
        "finalize_error": None,
        "assert_complete": None,
    }
    with tempfile.TemporaryDirectory(prefix="koawa-audit-f7-") as tmp:
        root = Path(tmp)
        (root / "app.py").write_text("value = 1\n", encoding="utf-8")
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "audit@example.invalid")
        _git(root, "config", "user.name", "audit")
        _git(root, "config", "core.autocrlf", "false")
        _git(root, "add", "--all")
        _git(root, "commit", "-qm", "baseline")
        registry = build_verified_coding_tool_registry(
            root, command_runner=Runner(), required_test_profiles=PROFILES
        )
        try:
            run_id = uuid4()
            mid = uuid4()

            def ctx(call_id: str) -> ToolExecutionContext:
                return ToolExecutionContext(
                    run_id, mid, 1, ModelCallRef(mid, call_id)
                )

            for pid in PROFILES:
                result = registry.execute(
                    _call("run_test_profile", {"profile_id": pid}, f"t-{pid}"),
                    context=ctx(f"t-{pid}"),
                )
                document = json.loads(result.content)
                out["runs"].append(
                    {
                        "profile": pid,
                        "error": (document.get("error") or {}).get("code"),
                    }
                )
            final = registry.execute(
                _call("finalize_task", {}, "final"), context=ctx("final")
            )
            out["finalize_error"] = (json.loads(final.content).get("error") or {}).get("code")
            try:
                registry.assert_complete(run_id)
                out["assert_complete"] = "accepted"
            except Exception as error:  # noqa: BLE001 - record the gate code
                out["assert_complete"] = getattr(error, "code", str(error))
        finally:
            registry.close()
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
