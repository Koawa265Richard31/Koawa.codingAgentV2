"""Issue #8: restricted command execution with a structured argv contract.

There is deliberately **no shell string**.  The caller supplies an argv
array, which is executed with ``shell=False``; metacharacters are data, not
syntax.  Redirection, pipelines, command substitution and variable expansion
are simply not expressible.

Two trust layers:

* ``container_only`` - the command runs in a one-shot container with no
  network, a read-only workspace view and a private scratch tmpfs.  Raw
  output may be returned because the environment holds no secrets.
* ``trusted_workspace`` - the command runs on the host with the invoking
  account's rights.  Output is redacted before it is returned, and the
  receipt declares ``isolation: "none"``.

Pipelines are intentionally NOT supported in this slice: the tool-argument
schema only compiles flat scalar arrays, and a half-typed nested shape would
be worse than two explicit calls.  Callers compose by calling twice.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..execution.loop import ToolExecutionContext, ToolExecutionResult
from ..tools.errors import ToolConfigurationError, tool_error_result
from ..tools.schema import ToolSpec
from ..verification.runner import run_bounded_process

SHELL_TOOL_NAME = "run_shell"


class ShellMode(StrEnum):
    DISABLED = "disabled"
    CONTAINER_ONLY = "container_only"
    TRUSTED_WORKSPACE = "trusted_workspace"


@dataclass(frozen=True, slots=True)
class ShellLimits:
    """Deployment policy.  Empty ``allowed_programs`` disables everything."""

    allowed_programs: tuple[str, ...] = ()
    max_argv: int = 32
    max_arg_chars: int = 4_096
    max_output_chars: int = 32_768
    max_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_programs, tuple) or any(
            not isinstance(item, str)
            or not item
            or os.sep in item
            or "/" in item
            or "\\" in item
            or len(item) > 64
            for item in self.allowed_programs
        ):
            raise ToolConfigurationError("invalid_shell_allowlist")
        if (
            not isinstance(self.max_argv, int)
            or isinstance(self.max_argv, bool)
            or not 1 <= self.max_argv <= 128
        ):
            raise ToolConfigurationError("invalid_shell_limits")
        if (
            not isinstance(self.max_arg_chars, int)
            or isinstance(self.max_arg_chars, bool)
            or not 1 <= self.max_arg_chars <= 16_384
        ):
            raise ToolConfigurationError("invalid_shell_limits")
        if (
            not isinstance(self.max_output_chars, int)
            or isinstance(self.max_output_chars, bool)
            or not 256 <= self.max_output_chars <= 1_000_000
        ):
            raise ToolConfigurationError("invalid_shell_limits")
        if (
            not isinstance(self.max_timeout_seconds, (int, float))
            or isinstance(self.max_timeout_seconds, bool)
            or not 1 <= self.max_timeout_seconds <= 600
        ):
            raise ToolConfigurationError("invalid_shell_limits")


@dataclass(frozen=True, slots=True)
class ShellArguments:
    argv: tuple[str, ...]
    timeout_seconds: int = 30


class ShellError(RuntimeError):
    """Content-free shell failure carrying a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _path_like(argument: str) -> bool:
    return (
        "/" in argument
        or "\\" in argument
        or argument.startswith(".")
        or (len(argument) > 1 and argument[1] == ":")
    )


def _escapes_workspace(argument: str, root: Path) -> bool:
    """True when a path-like argument resolves outside the workspace.

    Only arguments that LOOK like paths are checked, so a regex such as
    ``a|b`` or a glob is never mistaken for a filesystem location.
    """

    if not _path_like(argument):
        return False
    candidate = Path(argument)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, ValueError):
        return True
    return not resolved.is_relative_to(root)


def validate_shell_argv(
    argv: tuple[str, ...],
    limits: ShellLimits,
    root: Path,
) -> tuple[str, ...]:
    """Validate and return the argv the caller may execute."""

    if not isinstance(argv, tuple) or not argv:
        raise ShellError("invalid_shell_argv")
    if len(argv) > limits.max_argv:
        raise ShellError("shell_argv_too_long")
    for argument in argv:
        if (
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or len(argument) > limits.max_arg_chars
        ):
            raise ShellError("invalid_shell_argv")
    program = argv[0]
    if program not in limits.allowed_programs:
        # Allowlist, never a blocklist: an unlisted program is refused even
        # when it exists on the machine.
        raise ShellError("shell_program_not_allowed")
    if _escapes_workspace(program, root):
        raise ShellError("shell_program_path_forbidden")
    for argument in argv[1:]:
        if _escapes_workspace(argument, root):
            raise ShellError("shell_argument_path_forbidden")
    return argv


def _resolve_program(program: str) -> str | None:
    return shutil.which(program)


_CONTAINER_FLAGS = (
    "--rm",
    "--network",
    "none",
    "--read-only",
    "--user",
    "65532:65532",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--pids-limit",
    "128",
    "--memory",
    "512m",
    "--memory-swap",
    "512m",
    "--tmpfs",
    "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
    "-w",
    "/workspace",
)


@dataclass(frozen=True, slots=True)
class ShellResult:
    exit_code: int | None
    timed_out: bool
    start_failed: bool
    stdout: str
    stderr: str
    truncated: bool
    isolation: str


class ShellExecutor:
    """Runs an already-validated argv under the configured trust layer."""

    def __init__(
        self,
        root: Path,
        limits: ShellLimits,
        *,
        mode: ShellMode,
        image_id: str | None = None,
        docker_executable: str = "docker",
    ) -> None:
        if mode is ShellMode.DISABLED:
            raise ShellError("shell_disabled")
        if mode is ShellMode.CONTAINER_ONLY and not image_id:
            raise ShellError("shell_image_required")
        self._root = root
        self._limits = limits
        self._mode = mode
        self._image_id = image_id
        self._docker_executable = docker_executable

    @property
    def mode(self) -> ShellMode:
        return self._mode

    @property
    def root(self) -> Path:
        return self._root

    def _container_argv(self, docker: str, argv: tuple[str, ...]) -> tuple[str, ...]:
        return (
            docker,
            "run",
            *_CONTAINER_FLAGS,
            "-v",
            f"{self._root}:/workspace:ro",
            self._image_id or "",
            *argv,
        )

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        progress_guard=None,
    ) -> ShellResult:
        timeout = min(float(timeout_seconds), self._limits.max_timeout_seconds)
        if timeout <= 0:
            raise ShellError("invalid_shell_timeout")
        if self._mode is ShellMode.CONTAINER_ONLY:
            docker = shutil.which(self._docker_executable)
            if docker is None:
                raise ShellError("shell_container_unavailable")
            launch = self._container_argv(docker, argv)
            isolation = "container"
        else:
            executable = _resolve_program(argv[0])
            if executable is None:
                raise ShellError("shell_program_not_found")
            launch = (executable, *argv[1:])
            isolation = "none"
        process = run_bounded_process(
            self._root,
            launch,
            timeout_seconds=timeout,
            max_stdout_bytes=self._limits.max_output_chars * 4,
            max_stderr_bytes=self._limits.max_output_chars * 4,
            environment={},
            progress_guard=progress_guard,
        )
        stdout = process.stdout.decode("utf-8", "replace")
        stderr = process.stderr.decode("utf-8", "replace")
        if self._mode is ShellMode.TRUSTED_WORKSPACE:
            # Host output can contain arbitrary local content.  It is redacted
            # before it leaves the operator side - the contract says raw host
            # output never reaches the model.
            from ..recovery.redaction import redact_text

            stdout = redact_text(stdout)
            stderr = redact_text(stderr)
        truncated = False
        if len(stdout) > self._limits.max_output_chars:
            stdout = stdout[: self._limits.max_output_chars]
            truncated = True
        if len(stderr) > self._limits.max_output_chars:
            stderr = stderr[: self._limits.max_output_chars]
            truncated = True
        return ShellResult(
            exit_code=process.exit_code,
            timed_out=process.timed_out,
            start_failed=process.start_failed,
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
            isolation=isolation,
        )


class ShellTool:
    """Tool handler: validates, reviews, executes, reports a stable receipt."""

    def __init__(
        self,
        executor: ShellExecutor,
        limits: ShellLimits,
        *,
        reviewer=None,
        review_policy=None,
    ) -> None:
        from .shell_review import ReviewPolicy

        self._executor = executor
        self._limits = limits
        self._reviewer = reviewer
        self._review_policy = review_policy or ReviewPolicy()

    def __call__(
        self,
        arguments: ShellArguments,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        from .shell_review import review_or_deny

        root = self._executor.root
        try:
            argv = validate_shell_argv(arguments.argv, self._limits, root)
        except ShellError as error:
            return tool_error_result(error.code)
        # Issue #9: the heuristic gate runs only after the deterministic one,
        # and every uncertain outcome is a deny.
        allowed, review_fields = review_or_deny(
            self._reviewer,
            argv,
            mode=self._executor.mode.value,
            policy=self._review_policy,
        )
        if not allowed:
            return tool_error_result(
                "shell_review_denied",
                detail=str(review_fields.get("review_reason", "unavailable")),
            )
        try:
            result = self._executor.run(
                argv,
                timeout_seconds=arguments.timeout_seconds,
                progress_guard=context.check_progress,
            )
        except ShellError as error:
            return tool_error_result(error.code)
        payload = {
            "argv": list(argv),
            "exit_code": result.exit_code,
            "isolation": result.isolation,
            "output_truncated": result.truncated,
            # Shell side effects cannot be enumerated, so the ledger profile
            # is NON_IDEMPOTENT/MANUAL and the receipt says so explicitly.
            "side_effect_scope": "unenumerated",
            "start_failed": result.start_failed,
            "stderr": result.stderr,
            "stdout": result.stdout,
            "timed_out": result.timed_out,
        }
        payload.update(review_fields)
        return ToolExecutionResult(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )


def shell_tool_spec() -> ToolSpec:
    return ToolSpec(
        SHELL_TOOL_NAME,
        (
            "Run one allow-listed program with a structured argv (no shell, "
            "no pipes, no redirection). Paths must stay inside the workspace."
        ),
        ShellArguments,
        {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "minItems": 1,
                    "maxItems": 32,
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
    )
