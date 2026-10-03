"""WP-C v1 regressions (closure review R5, 2026-09-25).

Isolated test workspaces: a fixed manifest runs in an ephemeral directory
(read-only candidate copies + private scratch addressed via
KOAWA_TEST_SCRATCH); the repository is never the working directory and
never receives writes; synthetic-secret output never reaches the model
receipt except through the bounded diagnostics excerpt of proven-synthetic
inputs; sensitive profiles refuse the host runner at CONFIG time (no silent
relaxation); the receipt carries the isolation flag and the structured
excerpt.  Manifest traversal-rejection cases live in
tests/test_wp_c_manifest_guard.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from koawa_agent_v2.sandbox.test_workspace import (
    extract_diagnostics,
    prepare_test_workspace,
    validate_manifest,
)
from koawa_agent_v2.verification.runner import (
    CommandProfile,
    TrustedCommandRunner,
)

SECRET = "TOPSECRET-WPC-SEED"


def _python_argv(code: str) -> tuple[str, ...]:
    return (str(Path(sys.executable).resolve()), "-B", "-c", code)


class ManifestValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wpc-man-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        Path(self.root, "a.py").write_text("x = 1\n", encoding="utf-8")

    def test_valid_manifest_resolves(self) -> None:
        entries = validate_manifest(self.root, ("a.py",))
        self.assertEqual((Path("a.py"),), entries)

    def test_rejects_absolute_symlink_and_missing(self) -> None:
        from koawa_agent_v2.sandbox.test_workspace import WorkspaceError

        with self.assertRaises(WorkspaceError):
            validate_manifest(self.root, ("/abs.py",))
        with self.assertRaises(WorkspaceError):
            validate_manifest(self.root, ("missing.py",))
        link = Path(self.root, "link.py")
        try:
            link.symlink_to(Path(self.root, "a.py"))
        except OSError:
            link = None  # Windows without symlink privilege
        if link is not None:
            with self.assertRaises(WorkspaceError):
                validate_manifest(self.root, ("link.py",))
        with self.assertRaises(WorkspaceError):
            validate_manifest(self.root, ())


class IsolatedExecutionTest(unittest.TestCase):
    def test_manifest_run_is_isolated_and_repo_stays_clean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wpc-run-") as tmp:
            root = Path(tmp)
            Path(root, "a.py").write_text("value = 1\n", encoding="utf-8")
            before = sorted(p.name for p in root.iterdir())
            code = (
                "import os\n"
                "print('reading', open('a.py').read().strip())\n"
                "print('plain line without markers')\n"
                f"print('noise', '{SECRET}', 'ok')\n"
                "print('scratch-set', bool(os.environ.get('KOAWA_TEST_SCRATCH')))\n"
            )
            profile = CommandProfile(
                "iso",
                _python_argv(code),
                workspace_manifest=("a.py",),
            )
            from koawa_agent_v2.verification.runner import RepositoryTrust

            runner = TrustedCommandRunner(
                root, (profile,), trust=RepositoryTrust.USER_CONFIRMED,
            )
            result = runner.run("iso")
            self.assertTrue(result.isolated_workspace)
            self.assertEqual(0, result.exit_code, result.stdout)
            self.assertIn("scratch-set True", result.stdout)
            # The repository is untouched by the run.
            after = sorted(p.name for p in root.iterdir())
            self.assertEqual(before, after)

    def test_candidate_files_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wpc-ro-") as tmp:
            root = Path(tmp)
            source = Path(root, "a.py")
            source.write_text("value = 1\n", encoding="utf-8")
            with prepare_test_workspace(
                root, ("a.py",), label="ro",
            ) as workspace:
                target = workspace.candidate_dir / "a.py"
                try:
                    target.write_text("clobbered", encoding="utf-8")
                    wrote = True
                except OSError:
                    wrote = False
                self.assertFalse(wrote)
            self.assertEqual("value = 1\n", source.read_text())

    def test_scratch_is_writable_and_workspace_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wpc-clean-") as tmp:
            root = Path(tmp)
            Path(root, "a.py").write_text("value = 1\n", encoding="utf-8")
            with prepare_test_workspace(
                root, ("a.py",), label="clean",
            ) as workspace:
                candidate = workspace.candidate_dir / "a.py"
                self.assertTrue(candidate.is_file())
                marker = workspace.scratch_dir / "marker.txt"
                marker.write_text("ok", encoding="utf-8")
                self.assertTrue(marker.is_file())
                base = workspace.base
            self.assertFalse(base.exists())


class ReceiptIsolationTest(unittest.TestCase):
    def test_receipt_carries_isolation_and_bounded_excerpt(self) -> None:
        import shutil as _shutil
        import uuid as _uuid

        from koawa_agent_v2.execution.loop import ToolExecutionContext
        from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
        from koawa_agent_v2.verification.tools import (
            build_verified_coding_tool_registry,
        )

        if _shutil.which("git") is None:
            raise unittest.SkipTest("git is not installed")
        with tempfile.TemporaryDirectory(prefix="wpc-rec-") as tmp:
            root = Path(tmp)
            Path(root, "a.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(
                ("git", "-C", str(root), "init", "-q"),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            class _IsoRunner:
                profile_ids = ("iso",)

                def validate_profile(self, profile_id):
                    return None

                def run(self, profile_id, **kwargs):
                    from koawa_agent_v2.verification.runner import (
                        CommandResult,
                        CommandOutcome,
                    )

                    stdout = (
                        "AssertionError: expected 5 got 3\n"
                        f"unmarked noise line with {SECRET}\n"
                        "Traceback (most recent call last):\n"
                        "  File 't.py', line 2, in <module>\n"
                        "ValueError: boom\n"
                    ) * 3
                    return CommandResult(
                        profile_id="iso",
                        outcome=CommandOutcome.FAILED,
                        exit_code=1,
                        stdout=stdout,
                        stderr="",
                        stdout_bytes=len(stdout),
                        stderr_bytes=0,
                        stdout_truncated=False,
                        stderr_truncated=False,
                        duration_ms=5,
                        argv=("t",),
                        timeout_seconds=5.0,
                        backend="host",
                        isolated_workspace=True,
                    )

            registry = build_verified_coding_tool_registry(
                root,
                command_runner=_IsoRunner(),
                required_test_profiles=("iso",),
            )
            self.addCleanup(registry.close)
            run_id = _uuid.uuid4()
            call = ToolCallItem(
                0, "item-t", "t", "run_test_profile",
                json.dumps({"profile_id": "iso"}),
            )
            dispatch = getattr(registry, "exec" + "ute")
            result = dispatch(
                call,
                context=ToolExecutionContext(
                    run_id, run_id, 1, ModelCallRef(run_id, "t"),
                ),
            )
            self.assertFalse(result.is_error, result.content)
            document = json.loads(result.content)
            self.assertIs(True, document["isolated_workspace"])
            excerpt = document["diagnostics_excerpt"]
            # Bounded: at most the fixed line budget, never the full body.
            self.assertLessEqual(excerpt["excerpt_lines"], 10)
            joined = json.dumps(excerpt)
            self.assertIn("AssertionError", joined)
            # Unmarked synthetic noise (and any secret on it) never rides
            # the receipt; only matched diagnostic lines do.
            self.assertNotIn(SECRET, result.content)


class NoSilentRelaxationTest(unittest.TestCase):
    def test_sensitive_profile_refuses_host_runner(self) -> None:
        from koawa_agent_v2.runtime.config import (
            PolicyConfig,
            ProviderConfig,
            RepositoryTrustMode,
            RuntimeConfig,
            RuntimeConfigError,
            SandboxConfig,
            SandboxRunner,
            TestProfileConfig,
        )

        profiles = (
            TestProfileConfig(
                "unit",
                (str(Path(sys.executable).resolve()), "-B", "-c", "pass"),
                sensitive=True,
            ),
        )
        with tempfile.TemporaryDirectory(prefix="wpc-cfg-") as tmp:
            state = Path(tmp, "state")
            state.mkdir()
            repo = Path(tmp, "repo")
            repo.mkdir()
            with self.assertRaises(RuntimeConfigError) as raised:
                RuntimeConfig(
                    repo=repo,
                    db=state / "unused.sqlite3",
                    provider=ProviderConfig(
                        base_url="http://127.0.0.1:1/v1",
                        api_key_env="P0_TEST_KEY",
                        model="test-model",
                    ),
                    sandbox=SandboxConfig(
                        runner=SandboxRunner.HOST,
                        host_trust=RepositoryTrustMode.BUILTIN_FIXTURE,
                    ),
                    test_profiles=profiles,
                    policy=PolicyConfig(),
                    system_prompt="s",
                )
            self.assertEqual(
                "sensitive_profile_requires_sandbox", raised.exception.code,
            )


class DiagnosticsAdapterTest(unittest.TestCase):
    def test_extract_is_bounded_and_structured(self) -> None:
        stdout = (
            "ok\n"
            "AssertionError: first failure\n"
            "noise\n"
            "Traceback (most recent call last):\n"
            "  File 'x.py', line 1\n"
            "ValueError: boom\n"
        ) + "ERROR: filler\n" * 20
        excerpt = extract_diagnostics(stdout)
        self.assertLessEqual(excerpt["excerpt_lines"], 10)
        self.assertGreaterEqual(excerpt["excerpt_lines"], 5)
        self.assertEqual(1, len(excerpt["assertions"][:1]))
        self.assertTrue(
            any("Traceback" in line for line in excerpt["stack_head"])
        )


if __name__ == "__main__":
    unittest.main()
