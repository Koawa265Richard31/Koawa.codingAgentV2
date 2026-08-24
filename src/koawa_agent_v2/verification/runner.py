"""D5 受信测试配置与有界宿主进程执行。

这里不是通用 Shell。模型只能选择启动时注册的 ``profile_id``；argv、cwd、
环境变量和预算全部由宿主配置固定。D8 容器上线前，这个 runner 只允许内置
fixture 或用户明确确认可信的仓库。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence, runtime_checkable
from uuid import UUID


_PROFILE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_SECRET_LIKE_ARGUMENT = re.compile(
    r"(?i)(?:bearer\s+|password=|token=|api[_-]?key=|secret=|sk-[a-z0-9])"
)
_SAFE_ENV_NAMES = frozenset(
    {
        "LANG",
        "LC_ALL",
        "PYTHONHASHSEED",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "TZ",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_SYSTEM",
        "GIT_EXTERNAL_DIFF",
        "GIT_OPTIONAL_LOCKS",
        "GIT_PAGER",
        "GIT_TERMINAL_PROMPT",
    }
)


class CommandRunnerError(Exception):
    """可安全跨工具边界传播的稳定命令错误。"""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
            raise ValueError("invalid command runner error code")
        self.code = code
        super().__init__(code)


class RepositoryTrust(StrEnum):
    UNTRUSTED = "untrusted"
    BUILTIN_FIXTURE = "builtin_fixture"
    USER_CONFIRMED = "user_confirmed"


class CommandOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    START_FAILED = "start_failed"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT = "output_limit"
    OOM_KILLED = "oom_killed"
    CLEANUP_FAILED = "cleanup_failed"


@dataclass(frozen=True, slots=True, repr=False)
class CommandProfile:
    """可信调用方注册的固定命令；模型永远不能修改 argv/env/cwd。"""

    profile_id: str
    argv: tuple[str, ...]
    timeout_seconds: float = 60.0
    max_stdout_bytes: int = 256_000
    max_stderr_bytes: int = 256_000
    environment: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not _PROFILE_ID.fullmatch(
            self.profile_id
        ):
            raise CommandRunnerError("invalid_command_profile")
        if (
            not isinstance(self.argv, tuple)
            or not self.argv
            or len(self.argv) > 128
            or any(
                not isinstance(value, str)
                or not value
                or "\x00" in value
                or len(value) > 16_384
                or _SECRET_LIKE_ARGUMENT.search(value) is not None
                for value in self.argv
            )
        ):
            raise CommandRunnerError("invalid_command_profile")
        executable = Path(self.argv[0])
        if not executable.is_absolute():
            raise CommandRunnerError("command_executable_must_be_absolute")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 3_600
        ):
            raise CommandRunnerError("invalid_command_profile")
        for value in (self.max_stdout_bytes, self.max_stderr_bytes):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > 16 * 1024 * 1024
            ):
                raise CommandRunnerError("invalid_command_profile")
        seen: set[str] = set()
        for item in self.environment:
            if not isinstance(item, tuple) or len(item) != 2:
                raise CommandRunnerError("invalid_command_profile")
            name, value = item
            if (
                name not in _SAFE_ENV_NAMES
                or name in seen
                or not isinstance(value, str)
                or "\x00" in value
                or len(value) > 16_384
            ):
                raise CommandRunnerError("unsafe_command_environment")
            seen.add(name)

    def __repr__(self) -> str:
        return (
            f"CommandProfile(profile_id={self.profile_id!r}, argv_count={len(self.argv)}, "
            f"timeout_seconds={self.timeout_seconds})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CommandResult:
    profile_id: str
    outcome: CommandOutcome
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    argv: tuple[str, ...]
    timeout_seconds: float
    backend: str = "host"
    immutable_image_id: str | None = None
    profile_digest: str | None = None
    allocation_id: UUID | None = None
    container_id: str | None = None

    @property
    def passed(self) -> bool:
        return self.outcome is CommandOutcome.PASSED

    def __repr__(self) -> str:
        return (
            f"CommandResult(profile_id={self.profile_id!r}, outcome={self.outcome.value!r}, "
            f"backend={self.backend!r}, exit_code={self.exit_code}, "
            f"stdout_bytes={self.stdout_bytes}, "
            f"stderr_bytes={self.stderr_bytes})"
        )


@runtime_checkable
class CommandRunner(Protocol):
    """D5/D8 共用的结构化命令执行端口。"""

    @property
    def profile_ids(self) -> tuple[str, ...]:
        """返回可信配置中可供模型选择的 profile ID。"""

    def validate_profile(self, profile_id: str) -> None:
        """在占用验证预算前确定性校验 profile。"""

    def run(
        self,
        profile_id: str,
        *,
        progress_guard: Callable[[], None] | None = None,
        execution_id: UUID | None = None,
    ) -> CommandResult:
        """按固定 profile 执行，并返回带 backend 身份的类型化结果。"""


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    exit_code: int | None
    timed_out: bool
    start_failed: bool
    stdout: bytes
    stderr: bytes
    stdout_bytes: int
    stderr_bytes: int
    duration_ms: int


class TrustedCommandRunner:
    """只按 ``profile_id`` 运行不可变测试命令。"""

    def __init__(
        self,
        workspace_root: str | Path,
        profiles: Sequence[CommandProfile],
        *,
        trust: RepositoryTrust = RepositoryTrust.UNTRUSTED,
    ) -> None:
        try:
            root = Path(workspace_root).resolve(strict=True)
        except (OSError, TypeError, ValueError):
            raise CommandRunnerError("invalid_workspace_root") from None
        if not root.is_dir():
            raise CommandRunnerError("invalid_workspace_root")
        if not isinstance(trust, RepositoryTrust):
            raise TypeError("trust must be RepositoryTrust")
        entries: dict[str, CommandProfile] = {}
        for profile in profiles:
            if not isinstance(profile, CommandProfile):
                raise TypeError("profiles must contain CommandProfile")
            if profile.profile_id in entries:
                raise CommandRunnerError("duplicate_command_profile")
            entries[profile.profile_id] = profile
        if not entries:
            raise CommandRunnerError("empty_command_profiles")
        self._root = root
        self._profiles = entries
        self._trust = trust

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def validate_profile(self, profile_id: str) -> None:
        """在占用测试预算前验证 trust gate 与固定配置是否存在。"""
        if self._trust is RepositoryTrust.UNTRUSTED:
            raise CommandRunnerError("repository_not_trusted_for_host_execution")
        if profile_id not in self._profiles:
            raise CommandRunnerError("unknown_command_profile")

    def run(
        self,
        profile_id: str,
        *,
        progress_guard: Callable[[], None] | None = None,
        execution_id: UUID | None = None,
    ) -> CommandResult:
        del execution_id
        self.validate_profile(profile_id)
        profile = self._profiles[profile_id]
        process = run_bounded_process(
            self._root,
            profile.argv,
            timeout_seconds=profile.timeout_seconds,
            max_stdout_bytes=profile.max_stdout_bytes,
            max_stderr_bytes=profile.max_stderr_bytes,
            environment=dict(profile.environment),
            progress_guard=progress_guard,
        )
        if process.start_failed:
            outcome = CommandOutcome.START_FAILED
        elif process.timed_out:
            outcome = CommandOutcome.TIMED_OUT
        elif process.exit_code == 0:
            outcome = CommandOutcome.PASSED
        else:
            outcome = CommandOutcome.FAILED
        return CommandResult(
            profile.profile_id,
            outcome,
            process.exit_code,
            process.stdout.decode("utf-8", "replace"),
            process.stderr.decode("utf-8", "replace"),
            process.stdout_bytes,
            process.stderr_bytes,
            process.stdout_bytes > len(process.stdout),
            process.stderr_bytes > len(process.stderr),
            process.duration_ms,
            (Path(profile.argv[0]).name, *profile.argv[1:]),
            float(profile.timeout_seconds),
        )


def run_bounded_process(
    cwd: str | Path,
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    environment: Mapping[str, str] | None = None,
    progress_guard: Callable[[], None] | None = None,
) -> _ProcessResult:
    """固定 argv 的底层执行器；并行排空输出，超时/取消时终止进程树。"""
    root = Path(cwd)
    start = time.monotonic()
    # I1: shared minimal environment contract (never inherits the parent env).
    # Local import avoids the verification/runtime package cycle.
    from ..runtime.subprocess_env import (
        SubprocessEnvError,
        build_minimal_environment,
    )

    try:
        with tempfile.TemporaryDirectory(prefix="koawa-runner-tmp") as private_tmp:
            env = build_minimal_environment(
                environment or {},
                allowed_names=_SAFE_ENV_NAMES,
                private_temp=Path(private_tmp),
            )
    except SubprocessEnvError as error:
        raise CommandRunnerError(error.code) from None
    creationflags = 0
    popen_extra: dict[str, object] = {}
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        popen_extra["start_new_session"] = True
    try:
        process = subprocess.Popen(
            tuple(argv),
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            **popen_extra,
        )
    except (OSError, ValueError):
        return _ProcessResult(
            None, False, True, b"", b"", 0, 0, int((time.monotonic() - start) * 1000)
        )

    stdout = _BoundedCollector(max_stdout_bytes)
    stderr = _BoundedCollector(max_stderr_bytes)
    out_thread = threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True)
    err_thread = threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True)
    out_thread.start()
    err_thread.start()
    deadline = start + timeout_seconds
    timed_out = False
    try:
        while process.poll() is None:
            if progress_guard is not None:
                progress_guard()
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_tree(process)
                break
            time.sleep(0.025)
        if process.poll() is None:
            _terminate_process_tree(process)
        process.wait(timeout=5)
    except BaseException:
        _terminate_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    return _ProcessResult(
        process.returncode,
        timed_out,
        False,
        bytes(stdout.kept),
        bytes(stderr.kept),
        stdout.total,
        stderr.total,
        int((time.monotonic() - start) * 1000),
    )


class _BoundedCollector:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.kept = bytearray()
        self.total = 0

    def drain(self, stream: object) -> None:
        if stream is None or not hasattr(stream, "read"):
            return
        while True:
            chunk = stream.read(8_192)
            if not chunk:
                return
            self.total += len(chunk)
            remaining = self.limit - len(self.kept)
            if remaining > 0:
                self.kept.extend(chunk[:remaining])





def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        taskkill = str(Path(system_root) / "System32" / "taskkill.exe")
        try:
            subprocess.run(
                (taskkill, "/PID", str(process.pid), "/T", "/F"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
