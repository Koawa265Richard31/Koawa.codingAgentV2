"""S4 experiment driver: cross-turn retrieval + revocation visibility.

Turn flow on ONE thread (production AppRuntime.chat):
  turn1  repair the failing test (detailed contract) -> projections published
  turn2  ask the model to query recorded results via
         search_result_projections -> hits must reference turn1
  (revocation is applied between turns by the driver)
  turn3  re-query -> the revoked fact must be refused/absent
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID

from koawa_agent_v2.runtime.app import AppRuntime
from koawa_agent_v2.runtime.config import (
    PolicyConfig,
    ProviderConfig,
    RepositoryTrustMode,
    RuntimeConfig,
    SandboxConfig,
    SandboxRunner,
    TestProfileConfig,
)
from koawa_agent_v2.retrieval.projection import ResultProjectionStore
def _init_repo(root: Path) -> None:
    import shutil

    git = shutil.which("git")
    subprocess.run(
        (git, "-C", str(root), "init", "-q"), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for key, value in (
        ("user.email", "s4@example.invalid"),
        ("user.name", "S4 Fixture"),
        ("core.fsmonitor", "false"),
        ("core.autocrlf", "false"),
        ("core.filemode", "false"),
    ):
        subprocess.run(
            (git, "-C", str(root), "config", key, value), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    subprocess.run(
        (git, "-C", str(root), "add", "--all"), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        (git, "-C", str(root), "commit", "-qm", "s4 baseline"), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _stage(base: Path) -> RuntimeConfig:
    root = base / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(
        "def add(left, right):\n    return left - right\n", encoding="utf-8"
    )
    (root / "tests" / "test_app.py").write_text(
        "import unittest\nfrom app import add\n\n"
        "class AppTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(5, add(2, 3))\n",
        encoding="utf-8",
    )
    _init_repo(root)
    return RuntimeConfig(
        repo=root,
        db=base / "state" / "agent.sqlite3",
        provider=ProviderConfig(
            base_url="https://api.deepseek.com/v1",
            api_key_env="KOAWA_DEEPSEEK_KEY",
            model="deepseek-chat",
            max_output_tokens=16384,
        ),
        sandbox=SandboxConfig(
            runner=SandboxRunner.HOST,
            host_trust=RepositoryTrustMode.BUILTIN_FIXTURE,
        ),
        test_profiles=(
            TestProfileConfig(
                "unit",
                argv=(
                    str(sys.executable),
                    "-B",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                ),
                profile_version="s4-v1",
                release_mode="detailed",
                audience_policy_ref="audience/s4",
                environment_ref="env/s4-host",
                scratch_policy_ref="scratch/s4",
                timeout_seconds=60,
            ),
        ),
        policy=PolicyConfig(),
        system_prompt=(
            "You are repairing a failing unit test in this repository. "
            "Workflow: read the code, fix the bug with apply_patch, run the "
            "test with run_test_profile, and when it passes you MUST call "
            "the finalize_task tool before writing your final answer."
        ),
        model_rounds=40,
        max_tool_calls=60,
        budget_action_limits=(("root", 60),),
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="s4-exp-") as tmp:
        base = Path(tmp)
        config = _stage(base)
        app = AppRuntime(config, api_key=(Path("D:/A_Dev_Projects/key.txt").read_text().strip()))

        repair = "The unit test is failing. Fix the bug, run tests until green, then finalize."
        # Task path (run): D5 gate + terminal projection publication happen here.
        outcome1 = app.run(repair)
        print("TURN1:", outcome1.ok, outcome1.payload.get("status"),
              "| published:", outcome1.payload.get("result_projections", {}).get("published"))
        if not outcome1.ok:
            print("TURN1 payload:", json.dumps(outcome1.payload, ensure_ascii=False)[:600])
            return
        thread_id = outcome1.payload["thread_id"]
        turn1_id = outcome1.payload["turn_id"]

        query = (
            "Before doing anything else, call the search_result_projections "
            "tool (no arguments) to look up the recorded test results of this "
            "thread. Then summarize in plain text: how many results you found, "
            "each result's verdict and source_content_sha256 (first 8 chars). "
            "Do not modify any files."
        )
        outcome2 = app.chat(query, thread_id=thread_id)
        print("TURN2:", outcome2.ok, "| final:",
              str(outcome2.payload.get("final_text"))[:200])

        # Revoke one delivered fact between turns.
        store = app.assembled.store
        from koawa_agent_v2.control.event_store import StreamId

        run_events = store.read_stream(
            StreamId("run-execution", UUID(turn1_id)), after_version=-1, limit=300,
        )
        from koawa_agent_v2.retrieval.projection import scan_test_results

        scan = scan_test_results(store, UUID(turn1_id))
        victim = scan.facts[0]
        projections = ResultProjectionStore(store)
        projections.revoke(
            turn_id=UUID(turn1_id),
            thread_id=UUID(outcome1.payload["thread_id"]),
            run_id=None,
            call_id=victim["call_id"],
            model_turn_id=victim["model_turn_id"],
            reason="s4-experiment",
        )
        print("REVOKED:", victim["call_id"])

        requery = (
            "Call the search_result_projections tool again (no arguments). "
            "Report in plain text: how many results are returned now, and for "
            "each, the call_id and availability. Do not modify any files."
        )
        outcome3 = app.chat(requery, thread_id=thread_id)
        print("TURN3:", outcome3.ok, "| final:",
              str(outcome3.payload.get("final_text"))[:400])
        app.close()


if __name__ == "__main__":
    main()
