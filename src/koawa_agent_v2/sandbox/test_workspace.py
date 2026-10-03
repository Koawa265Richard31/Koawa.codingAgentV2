"""WP-C v1 (closure review R5): isolated test workspace + safe diagnostics.

Per the confirmed 2026-09-21/23 rulings: a test profile may declare a FIXED
input manifest; execution then runs in a per-run ephemeral workspace that
contains ONLY those files (read-only candidate copies) plus a private
writable scratch area - never the repository, never inherited environment
(the shared minimal-env contract already applies), and never the network
for sensitive profiles (those refuse the host runner at config validation
instead of silently relaxing).

Cleanup failures quarantine the workspace (renamed aside, never reused);
source manifests are verified against symlinks/escapes/size bounds before
anything is copied, so candidate inputs are exactly what the operator
listed.  The bounded diagnostics adapter turns isolated-run output into a
small structured excerpt (assertion lines + stack head) - rich enough for
ordinary automated repair, never a raw body dump.
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

MAX_MANIFEST_ENTRIES = 64
MAX_ENTRY_BYTES = 2 * 1024 * 1024
_TOTAL_BYTES_BUDGET = 16 * 1024 * 1024

_DIAGNOSTIC_LINE = re.compile(
    r"AssertionError|assert\s|FAILED|ERROR|Error|Traceback|Exception|FAIL",
)
_MAX_EXCERPT_LINES = 10
_MAX_EXCERPT_CHARS = 200


class WorkspaceError(RuntimeError):
    """Content-free workspace failure carrying a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_manifest(root: Path, manifest: Sequence[str]) -> tuple[Path, ...]:
    """Verify a fixed input manifest: relative, in-tree, real files only."""

    if not isinstance(manifest, tuple) or not manifest:
        raise WorkspaceError("workspace_manifest_required")
    if len(manifest) > MAX_MANIFEST_ENTRIES:
        raise WorkspaceError("workspace_manifest_too_large")
    if len(set(manifest)) != len(manifest):
        raise WorkspaceError("workspace_manifest_duplicate")
    resolved: list[Path] = []
    total = 0
    for item in manifest:
        if not isinstance(item, str) or not item or "\\" in item:
            raise WorkspaceError("workspace_manifest_invalid_path")
        candidate = Path(item)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise WorkspaceError("workspace_manifest_invalid_path")
        source = root / candidate
        if source.is_symlink() or not source.is_file():
            raise WorkspaceError("workspace_manifest_source_invalid")
        size = source.stat().st_size
        if size > MAX_ENTRY_BYTES:
            raise WorkspaceError("workspace_manifest_entry_too_large")
        total += size
        if total > _TOTAL_BYTES_BUDGET:
            raise WorkspaceError("workspace_manifest_too_large")
        resolved.append(candidate)
    return tuple(resolved)


class PreparedTestWorkspace:
    """One run's ephemeral workspace: read-only candidate + private scratch."""

    def __init__(self, base: Path, entries: tuple[Path, ...]) -> None:
        self.base = base
        self.candidate_dir = base / "candidate"
        self.scratch_dir = base / "scratch"
        self.entries = entries

    def cleanup(self) -> None:
        """Best-effort removal; failures quarantine, never reuse."""

        try:
            shutil.rmtree(self.base, ignore_errors=False)
        except OSError:
            quarantine = self.base.with_name(
                self.base.name + ".quarantined-" + str(uuid.uuid4())[:8]
            )
            try:
                os.replace(self.base, quarantine)
            except OSError:
                # Quarantine itself failed: leave the directory untouched
                # (never delete-and-reuse); operators inspect it manually.
                pass


@contextmanager
def prepare_test_workspace(
    source_root: Path,
    manifest: Sequence[str],
    *,
    label: str,
) -> Iterator[PreparedTestWorkspace]:
    """Materialize the isolated workspace for one profile run."""

    root = Path(source_root)
    entries = validate_manifest(root, manifest)
    base = Path(
        os.environ.get("TEMP") or os.environ.get("TMP") or str(Path.cwd()),
        f"koawa-wpc-{label}-{uuid.uuid4().hex[:12]}",
    )
    base.mkdir(parents=True)
    workspace = PreparedTestWorkspace(base, entries)
    try:
        workspace.candidate_dir.mkdir()
        workspace.scratch_dir.mkdir()
        for relative in entries:
            destination = workspace.candidate_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, destination)
            # Best-effort read-only candidate (Windows raises on writes to
            # read-only files; POSIX needs the bit as intent marker).
            os.chmod(destination, 0o444)
        yield workspace
    finally:
        workspace.cleanup()


def extract_diagnostics(stdout_text: str) -> dict:
    """Bounded structured excerpt of an isolated run's output.

    Returns at most ``_MAX_EXCERPT_LINES`` lines of ``_MAX_EXCERPT_CHARS``
    chars each, split into assertion lines and the head of the first
    traceback.  This is the model-visible "detailed safe diagnostics" for
    synthetic/isolated runs - never a raw body dump.
    """

    assertions: list[str] = []
    stack_head: list[str] = []
    in_traceback = False
    exception_seen = False
    collected = 0
    for raw_line in stdout_text.splitlines():
        line = raw_line.rstrip()[:_MAX_EXCERPT_CHARS]
        if not line:
            continue
        if line.startswith("Traceback"):
            in_traceback = True
            exception_seen = False
            if collected < _MAX_EXCERPT_LINES:
                stack_head.append(line)
                collected += 1
            continue
        if in_traceback:
            indented = line.startswith((" ", "\t"))
            if indented and len(stack_head) < 5:
                if collected < _MAX_EXCERPT_LINES:
                    stack_head.append(line)
                    collected += 1
                continue
            if not indented and not exception_seen:
                # The exception line itself.
                exception_seen = True
                if collected < _MAX_EXCERPT_LINES:
                    stack_head.append(line)
                    collected += 1
                continue
            # Anything after the exception line is NOT stack - fall through
            # to the ordinary assertion matching only.
            in_traceback = False
        if (
            _DIAGNOSTIC_LINE.search(line)
            and not in_traceback
            and len(assertions) < 5
            and collected < _MAX_EXCERPT_LINES
        ):
            assertions.append(line)
            collected += 1
        if collected >= _MAX_EXCERPT_LINES:
            break
    return {
        "assertions": assertions[:5],
        "stack_head": stack_head[:5],
        "excerpt_lines": collected,
    }
