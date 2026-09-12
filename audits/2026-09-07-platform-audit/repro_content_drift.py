"""Read-only product audit; all mutations occur in a TemporaryDirectory."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.verification.runner import CommandProfile, RepositoryTrust
from koawa_agent_v2.verification.tools import build_verified_coding_tool_registry


def main():
    with tempfile.TemporaryDirectory(prefix="koawa-audit-drift-") as tmp:
        root = Path(tmp)
        target = root / "app.py"
        target.write_text("value = 1\n", encoding="utf-8")
        for args in (("init", "-q"), ("config", "user.email", "audit@example.invalid"),
                     ("config", "user.name", "Audit"), ("config", "core.autocrlf", "false"),
                     ("add", "."), ("commit", "-qm", "fixture")):
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
        registry = build_verified_coding_tool_registry(root, command_profiles=(
            CommandProfile("unit", (sys.executable, "-B", "-c", "import app; assert app.value == 2")),
        ), repository_trust=RepositoryTrust.BUILTIN_FIXTURE)
        run_id = uuid4()
        def call(name, args):
            mid = uuid4()
            item = ToolCallItem(0, "item", name, name, json.dumps(args))
            return registry.execute(item, context=ToolExecutionContext(run_id, mid, 1, ModelCallRef(mid, name)))
        try:
            patch = {"schema_version": 1, "changes": [{"operation": "update", "path": "app.py",
                "base_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "hunks": [{"old_start": 1, "old_lines": ["value = 1"], "new_lines": ["value = 2"]}]}]}
            results = {}
            for name, args in (("apply_patch", {"patch_json": json.dumps(patch)}),
                               ("run_test_profile", {"profile_id": "unit"}),
                               ("git_status", {}), ("git_diff", {}), ("finalize_task", {})):
                result = call(name, args)
                results[name] = {"is_error": result.is_error, "content": json.loads(result.content)}
                assert not result.is_error, result.content
            before = registry.git.status().digest
            target.write_text("value = 3\n", encoding="utf-8")
            after = registry.git.status().digest
            try:
                registry.assert_complete(run_id)
                accepted = True
                error = None
            except Exception as exc:
                accepted = False
                error = getattr(exc, "code", type(exc).__name__)
            independent = subprocess.run([sys.executable, "-B", "-c", "import app; assert app.value == 2"], cwd=root, capture_output=True)
            print(json.dumps({"scenario": "B1", "python": sys.version, "results": results,
                "status_digest_equal": before == after, "completion_accepted_after_drift": accepted,
                "completion_error": error, "independent_acceptance_exit": independent.returncode}, indent=2))
        finally:
            registry.close()

if __name__ == "__main__":
    main()
