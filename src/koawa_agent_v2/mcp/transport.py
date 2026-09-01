"""MCP stdio transport with strict Content-Length framing (stdlib only).

I1 Stage C rewrite.  Contract highlights:

- The child process NEVER inherits the parent environment.  ``open()`` builds a
  minimal audited environment through build_minimal_environment (in
  runtime/subprocess_env.py); the admin-configured explicit ``env`` mapping is
  the allowlist (``allowed_env_names`` defaults to its keys).  Secret-shaped
  and code-injection variable names are rejected by the builder (stable
  SubprocessEnvError codes are surfaced as TransportError with the same code,
  so McpSession sees one stable code space).

- Private temporary directory is created at ``open()`` and removed at
  ``close()`` (or on failed open); cleanup failure is reported as the stable
  ``mcp_temp_cleanup_failed`` error instead of a silent leak.

- State machine: CREATED -> OPENING -> OPEN -> CLOSING -> CLOSED, with OPENING
  falling to FAILED on any start failure.  ``close()`` is terminal and supports
  never-opened, failed-open, repeated close and open/close races; a lock plus a
  close ``epoch`` guarantee no background thread can move the state back to
  OPEN after close wins.

- Process ownership: spawns go through the injectable ProcessSpawner/
  OwnedProcess ports.  The production adapter creates the child under Windows
  with CREATE_NEW_PROCESS_GROUP and terminates the exact tree with
  ``taskkill /T (/F)``; on POSIX the child is setsid-ed (start_new_session)
  and the tree is signalled via killpg.  Every kill/wait phase shares ONE
  shutdown absolute deadline (never reset per phase) and every kill/wait is
  bounded.  A spawn that exceeds ``process_start_timeout_seconds`` is treated
  as ACK-loss: the owned tree is force-collected and only after ``wait``
  confirms the tree is gone is the stable ``mcp_process_start_timeout``
  reported.  Platforms that cannot guarantee bounded kill/wait fail closed
  with ``mcp_process_start_unsupported``.

  Convergence note (allowed by the I1 task card): the production adapter does
  NOT use a kill-on-close Job Object helper.  taskkill /T /F is bounded and
  deterministic for the stdio lifecycle, and the transport wraps every phase in
  one shared shutdown deadline plus bounded joins, fail-closed on expiry.
  A Job Object would additionally close the "grandchild escapes wrapper" gap,
  but adds handle lifecycle complexity and OS-version quirks; I6 launcher can
  revisit containment when it owns activation.  See the comments in
  OwnedSystemProcess.

- The stderr thread always reads to EOF; once the retained limit
  (``max_stderr_bytes``) is exceeded it drops content, keeps counting total
  bytes (``stderr_bytes``) and sets ``stderr_truncated`` - it never stops
  reading, so a chatty child can never deadlock.

- The inbound message queue is bounded (``max_inbound_messages``).  A full
  queue fails closed for requests/responses: an ``inbound_queue_overflow``
  error is latched and a fail-closed cleanup is triggered.  Notifications are
  advisory and may be dropped when the queue is full; MCP session handling
  coalesces them into the pending-refresh signal.

- ``send()`` validates the outbound frame (UTF-8 strict, length capped by
  ``max_frame_bytes``) before touching the pipe and records the three-state
  outcome NOT_SENT / SENT / UNKNOWN (``send_state``).  A partial write or pipe
  error that cannot prove delivery raises the stable ``transport_send_uncertain``
  code and triggers fail-closed cleanup.
"""

from __future__ import annotations

import math
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from .protocol import (
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    McpProtocolError,
    parse_message,
)
# NOTE: build_minimal_environment is imported lazily inside open() to
# avoid the runtime package __init__ (it imports app -> mcp) creating an
# import cycle through mcp/__init__.py at module scope.


class TransportError(RuntimeError):
    """Stable, content-free MCP transport failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TransportClosed(TransportError):
    def __init__(self) -> None:
        super().__init__("transport_closed")


class TransportTimeout(TransportError):
    def __init__(self) -> None:
        super().__init__("transport_timeout")


class TransportMalformedFrame(TransportError):
    def __init__(self, code: str) -> None:
        super().__init__(code)


class TransportPayloadInvalid(TransportError):
    """Outbound payload rejected before any byte reached the pipe (NOT_SENT)."""

    def __init__(self) -> None:
        super().__init__("transport_payload_invalid")


class TransportSendUncertain(TransportError):
    """A write partially failed and delivery cannot be proven (UNKNOWN)."""

    def __init__(self) -> None:
        super().__init__("transport_send_uncertain")


class TransportOverflow(TransportError):
    """Inbound messages exceeded the bounded queue; transport failed closed."""

    def __init__(self) -> None:
        super().__init__("inbound_queue_overflow")


class _FrameError:
    def __init__(self, code: str) -> None:
        self.code = code


def _read_frame(stream, *, max_frame_bytes: int) -> bytes | None:
    """Read one Content-Length frame; None on clean EOF before any header."""

    header = bytearray()
    while True:
        line = stream.readline()
        if not line:
            if not header:
                return None
            raise TransportMalformedFrame("frame_truncated")
        if len(header) + len(line) > 8_192:
            raise TransportMalformedFrame("frame_header_too_large")
        if line == b"\r\n":
            break
        header.extend(line)
    if not header:
        raise TransportMalformedFrame("missing_content_length")
    content_length: int | None = None
    for raw_line in header.split(b"\r\n"):
        if not raw_line:
            continue
        try:
            text = raw_line.decode("ascii", "strict")
        except UnicodeDecodeError:
            raise TransportMalformedFrame("non_ascii_frame_header") from None
        if not text.startswith("Content-Length:"):
            raise TransportMalformedFrame("unexpected_frame_header")
        value = text[len("Content-Length:"):].strip()
        if content_length is not None:
            raise TransportMalformedFrame("duplicate_content_length")
        if not value.isdigit() or int(value) <= 0:
            raise TransportMalformedFrame("invalid_content_length")
        content_length = int(value)
    if content_length is None:
        raise TransportMalformedFrame("missing_content_length")
    if content_length > max_frame_bytes:
        raise TransportMalformedFrame("frame_too_large")
    body = stream.read(content_length)
    if len(body) != content_length:
        raise TransportMalformedFrame("frame_truncated")
    return body


@dataclass(frozen=True, slots=True)
class SpawnSpec:
    """One immutable spawn request: already-validated command + env + cwd."""

    command: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str | None = None


@dataclass(frozen=True, slots=True)
class CloseReport:
    """What the terminal close actually proved (§8.7).

    ``uncertain`` means a phase hit its shared deadline and the process
    tree may still exist; the caller must record the allocation as
    OUTCOME_UNKNOWN instead of a clean stop.
    """

    process_exited: bool | None
    process_terminated: bool
    uncertain: bool
    stderr_truncated: bool
    stderr_bytes: int
    cleanup_error: str | None


class OwnedProcess(Protocol):
    """Process tree owned by the transport, with deadline-bounded control."""

    @property
    def pid(self) -> int: ...

    @property
    def stdin(self) -> BinaryIO: ...

    @property
    def stdout(self) -> BinaryIO: ...

    @property
    def stderr(self) -> BinaryIO: ...

    def poll(self) -> int | None: ...

    def terminate_tree(self, *, deadline: float) -> None: ...

    def kill_tree(self, *, deadline: float) -> None: ...

    def wait(self, *, deadline: float) -> int: ...

    def close_handles(self) -> None: ...


class ProcessSpawner(Protocol):
    """Injectable spawn port; ``deadline`` is an absolute monotonic deadline."""

    def spawn(self, spec: SpawnSpec, *, deadline: float) -> OwnedProcess: ...


class SystemProcessSpawner:
    """Production spawner: controlled spawn + exact tree termination.

    Windows: CREATE_NEW_PROCESS_GROUP detaches the child from our console;
    CREATE_NO_WINDOW suppresses console windows; trees are terminated with
    taskkill /PID <pid> /T (graceful) and taskkill /PID <pid> /T /F (force).
    POSIX: start_new_session=True (setsid) makes the child a session leader so
    the controller owns the process group; trees are signalled with os.killpg.

    No Job Object helper is used here - deliberate convergence (see module
    docstring): taskkill /T keeps tree kill bounded and deterministic, and all
    kill/wait calls are additionally bounded by an absolute deadline.  If a
    platform offers neither bounded tree kill nor session handling, the
    transport fails closed with mcp_process_start_unsupported instead of
    degrading to a bare spawn.
    """

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock

    def spawn(self, spec: SpawnSpec, *, deadline: float) -> OwnedProcess:
        if os.name not in ("nt", "posix"):
            raise TransportError("mcp_process_start_unsupported")
        if self._clock() >= deadline:
            # Fail fast before touching the OS: nothing to collect yet.
            raise TransportError("mcp_process_start_timeout")
        kwargs: dict[str, object] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": dict(spec.env),
        }
        if spec.cwd is not None:
            kwargs["cwd"] = spec.cwd
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            # setsid in the exec'd child; the controller owns the pgid.
            kwargs["start_new_session"] = True
        try:
            popen = subprocess.Popen(list(spec.command), **kwargs)
        except OSError as error:
            raise TransportError("mcp_process_start_failed") from error
        return OwnedSystemProcess(popen, clock=self._clock)


class OwnedSystemProcess:
    """Production OwnedProcess backed by subprocess.Popen (see spawner doc)."""

    def __init__(
        self,
        popen: subprocess.Popen[bytes],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._popen = popen
        self._clock = clock

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def stdin(self) -> BinaryIO:
        return self._popen.stdin

    @property
    def stdout(self) -> BinaryIO:
        return self._popen.stdout

    @property
    def stderr(self) -> BinaryIO:
        return self._popen.stderr

    def poll(self) -> int | None:
        return self._popen.poll()

    def terminate_tree(self, *, deadline: float) -> None:
        self._signal_tree(force=False, deadline=deadline)
        self._wait_or_raise(deadline)

    def kill_tree(self, *, deadline: float) -> None:
        self._signal_tree(force=True, deadline=deadline)
        self._wait_or_raise(deadline)

    def wait(self, *, deadline: float) -> int:
        code = self._popen.poll()
        if code is not None:
            return code
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TransportError("mcp_process_wait_timeout")
        try:
            return self._popen.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise TransportError("mcp_process_wait_timeout") from None

    def close_handles(self) -> None:
        for pipe in (self._popen.stdin, self._popen.stdout, self._popen.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass

    def _signal_tree(self, force: bool, *, deadline: float) -> None:
        if os.name == "nt":
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TransportError("mcp_process_wait_timeout")
            args = ["taskkill", "/PID", str(self.pid), "/T"]
            if force:
                args.append("/F")
            try:
                subprocess.run(
                    args,
                    timeout=remaining,
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass  # bounded: either the tree died or the signal failed
            if force and self._popen.poll() is None:
                # taskkill itself can miss its short deadline on a loaded
                # Windows host.  The process handle is still exact, so force
                # the owned root through Popen as a final bounded fallback;
                # the preceding /T attempt remains responsible for children.
                try:
                    self._popen.kill()
                except OSError:
                    pass
            return
        signal_number = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(self.pid, signal_number)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _wait_or_raise(self, deadline: float) -> None:
        code = self._popen.poll()
        if code is not None:
            return
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TransportError("mcp_process_wait_timeout")
        try:
            self._popen.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise TransportError("mcp_process_wait_timeout") from None


class StdioTransport:
    """Spawn one stdio MCP process and frame JSON-RPC messages over it."""

    CREATED = "created"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str | None = None,
        max_frame_bytes: int = 1_048_576,
        max_stderr_bytes: int = 262_144,
        allowed_env_names: frozenset[str] | None = None,
        process_start_timeout_seconds: float = 30.0,
        shutdown_timeout_seconds: float = 5.0,
        max_inbound_messages: int = 1024,
        process_spawner: ProcessSpawner | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        # I6 §8.7: launcher path.  When set, open() binds an already-
        # spawned endpoint instead of building env + spawning itself.
        process_factory: Callable[[], OwnedProcess] | None = None,
        allocation_state_reporter: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
            raise TypeError("command must be a sequence")
        if not command or any(not isinstance(item, str) for item in command):
            if not (not command and process_factory is not None):
                # I6 §8.7: only the launcher/factory path may supply an
                # empty command; the raw-command path still requires it.
                raise ValueError("command must contain non-empty strings")
        if not isinstance(env, Mapping):
            raise TypeError("env must be a mapping")
        if allowed_env_names is not None and not isinstance(
            allowed_env_names, frozenset
        ):
            raise TypeError("allowed_env_names must be a frozenset of str or None")
        for value, name in (
            (process_start_timeout_seconds, "process_start_timeout_seconds"),
            (shutdown_timeout_seconds, "shutdown_timeout_seconds"),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0.1
            ):
                raise ValueError(f"{name} must be a finite float >= 0.1")
        for value, name in (
            (max_frame_bytes, "max_frame_bytes"),
            (max_stderr_bytes, "max_stderr_bytes"),
            (max_inbound_messages, "max_inbound_messages"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive int")
        self._command = tuple(command)
        self._env = dict(env)
        self._cwd = cwd
        self._max_frame_bytes = max_frame_bytes
        self._max_stderr_bytes = max_stderr_bytes
        # Admin-configured explicit env IS the allowlist by default; narrowing
        # below the explicit keys causes the builder to reject (fail closed).
        self._allowed_names = (
            allowed_env_names if allowed_env_names is not None else frozenset(env)
        )
        self._process_start_timeout_seconds = float(process_start_timeout_seconds)
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._max_inbound_messages = max_inbound_messages
        self._clock = monotonic
        if process_factory is not None and not callable(process_factory):
            raise TypeError("process_factory must be callable or None")
        if allocation_state_reporter is not None and not callable(
            allocation_state_reporter,
        ):
            raise TypeError("allocation_state_reporter must be callable or None")
        self._factory = process_factory
        self._allocation_reporter = allocation_state_reporter
        self._spawner = process_spawner if process_spawner is not None else SystemProcessSpawner(clock=monotonic)
        self._messages: queue.Queue[object] = queue.Queue(
            maxsize=max_inbound_messages
        )
        # A notification burst may fill the bounded message queue.  Keep one
        # coalesced advisory notice out-of-band so a full queue cannot hide a
        # required catalog refresh or force correlated responses to overflow.
        self._notification_overflow_lock = threading.Lock()
        self._notification_overflow: JsonRpcNotification | None = None
        self._owned: OwnedProcess | None = None
        self._threads: list[threading.Thread] = []
        self._temp: tempfile.TemporaryDirectory[str] | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._teardown_lock = threading.Lock()
        self._teardown_done = threading.Event()
        self._state = self.CREATED
        self._epoch = 0
        self._stderr_truncated = False
        self._stderr_bytes = 0
        self._send_state = "not_sent"
        self._overflow_code: str | None = None
        self._cleanup_error: TransportError | None = None
        self._close_report: CloseReport | None = None
        self._close_uncertain = False

    # -- public state -------------------------------------------------------

    @property
    def stderr_truncated(self) -> bool:
        return self._stderr_truncated

    @property
    def stderr_bytes(self) -> int:
        return self._stderr_bytes

    def __del__(self) -> None:
        """Best-effort temp cleanup when a transport is GC'd without close()."""
        temporary = getattr(self, "_temp", None)
        if temporary is not None:
            try:
                temporary.cleanup()
            except Exception:
                pass

    @property
    def send_state(self) -> str:
        """NOT_SENT / SENT / UNKNOWN for the most recent send()."""

        return self._send_state

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._state in (self.CLOSING, self.CLOSED, self.FAILED)

    @property
    def pid(self) -> int | None:
        owned = self._owned
        return owned.pid if owned is not None else None

    def close_report(self) -> CloseReport | None:
        """Terminal CloseReport of the last close (None while open)."""

        return self._close_report

    def _report_allocation(self, state: str) -> None:
        if self._allocation_reporter is not None:
            try:
                self._allocation_reporter(state)
            except Exception:
                pass

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        # Lazy import: the runtime package __init__ imports app -> mcp, so a
        # module-level import here would circle back through mcp/__init__.
        from ..runtime.subprocess_env import (
            SubprocessEnvError,
            build_minimal_environment,
        )
        with self._state_lock:
            if self._state == self.CLOSED:
                raise TransportClosed()
            if self._state != self.CREATED:
                raise TransportError("transport_state_invalid")
            self._state = self.OPENING
            epoch = self._epoch
            temporary = tempfile.TemporaryDirectory(prefix="koawa-mcp-")
            self._temp = temporary
        if self._factory is not None:
            # I6 §8.7: the launcher already consumed the ticket, enforced
            # limits and created the OS process; open() only binds it.  The
            # private temp was created by the launcher.
            try:
                owned = self._factory()
            except TransportError as error:
                self._fail_open(getattr(error, "code", "mcp_process_start_failed"))
                raise
            except Exception as error:
                self._fail_open("mcp_process_start_failed")
                raise TransportError("mcp_process_start_failed") from error
            if owned is None:
                self._fail_open("mcp_process_start_failed")
                raise TransportError("mcp_process_start_failed")
        else:
            try:
                environment = build_minimal_environment(
                    self._env,
                    allowed_names=self._allowed_names,
                    private_temp=Path(temporary.name),
                )
            except SubprocessEnvError as error:
                self._fail_open(error.code)
                raise TransportError(error.code) from None
            spec = SpawnSpec(command=self._command, env=environment, cwd=self._cwd)
            deadline = self._clock() + self._process_start_timeout_seconds
            try:
                owned = self._spawner.spawn(spec, deadline=deadline)
            except SubprocessEnvError as error:
                self._fail_open(error.code)
                raise TransportError(error.code) from None
            except TransportError as error:
                self._fail_open(error.code)
                raise
            except OSError as error:
                self._fail_open("mcp_process_start_failed")
                raise TransportError("mcp_process_start_failed") from error
            if self._clock() >= deadline:
                # ACK arrived past the start deadline: the OS process may exist,
                # so force-collect the owned tree and only then report the
                # timeout (wait confirms the tree is gone before the error).
                self._collect_owned(owned)
                self._fail_open("mcp_process_start_timeout")
                raise TransportError("mcp_process_start_timeout")
        with self._state_lock:
            if self._epoch != epoch or self._state != self.OPENING:
                # A close raced open and won: collect our process, go terminal.
                self._state = self.CLOSING
                self._collect_owned(owned)
                self._mark_terminal()
                raise TransportClosed()
            self._owned = owned
            self._state = self.OPEN
        self._report_allocation("started")
        stdout_thread = threading.Thread(
            target=self._read_loop,
            args=(owned,),
            name=f"mcp-stdout-{id(self)}",
        )
        stderr_thread = threading.Thread(
            target=self._stderr_loop,
            args=(owned,),
            name=f"mcp-stderr-{id(self)}",
        )
        self._threads = [stdout_thread, stderr_thread]
        stdout_thread.start()
        stderr_thread.start()

    def send(self, payload: str) -> None:
        self._send_state = "not_sent"
        if not isinstance(payload, str):
            raise TypeError("payload must be str")
        try:
            body = payload.encode("utf-8", "strict")
        except UnicodeError:
            # Lone surrogates etc.: never valid UTF-8, NOT_SENT, fail closed.
            raise TransportPayloadInvalid() from None
        if len(body) > self._max_frame_bytes:
            raise TransportPayloadInvalid()
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self._write_lock:
            owned = self._owned
            if not self._is_open():
                raise TransportClosed()
            stdin = owned.stdin
            try:
                wrote_header = stdin.write(header)
                wrote_body = stdin.write(body)
                stdin.flush()
            except (BrokenPipeError, OSError) as error:
                # The pipe broke mid-write: delivery cannot be proven.
                self._send_state = "unknown"
                self._request_cleanup()
                raise TransportSendUncertain() from error
            except ValueError:
                if self._is_terminal():
                    raise TransportClosed() from None
                self._send_state = "unknown"
                self._request_cleanup()
                raise TransportSendUncertain() from None
            if wrote_header != len(header) or wrote_body != len(body):
                # Partial write: the frame may be half-delivered.
                self._send_state = "unknown"
                self._request_cleanup()
                raise TransportSendUncertain()
            self._send_state = "sent"

    def read(
        self, timeout: float
    ) -> JsonRpcRequest | JsonRpcResponse | JsonRpcNotification:
        if self._overflow_code is not None:
            raise TransportOverflow()
        with self._notification_overflow_lock:
            notification = self._notification_overflow
            self._notification_overflow = None
        if notification is not None:
            return notification
        try:
            item = self._messages.get(timeout=timeout)
        except queue.Empty:
            if self._is_terminal():
                raise TransportClosed()
            raise TransportTimeout() from None
        if isinstance(item, _FrameError):
            raise TransportMalformedFrame(item.code)
        if item is None:
            raise TransportClosed()
        return item

    def close(self) -> None:
        with self._teardown_lock:
            entered = False
            with self._state_lock:
                state = self._state
                if state in (self.CLOSED, self.FAILED):
                    return
                if state == self.CREATED:
                    self._state = self.CLOSED
                    self._epoch += 1
                    self._teardown_done.set()
                    return
                if state in (self.OPEN, self.OPENING):
                    self._state = self.CLOSING
                    entered = True
            if entered:
                try:
                    self._perform_teardown()
                finally:
                    self._mark_terminal()
                if self._cleanup_error is not None:
                    raise self._cleanup_error
            else:
                # A background fail-closed teardown is in flight; bounded wait.
                self._teardown_done.wait(timeout=self._shutdown_timeout_seconds)

    # -- internals ----------------------------------------------------------

    def _fail_open(self, code: str) -> None:
        self._report_allocation("failed_before_start")
        temporary = self._temp
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError:
                pass
            self._temp = None
        with self._state_lock:
            self._state = self.FAILED
            self._epoch += 1
        self._teardown_done.set()

    def _mark_terminal(self) -> None:
        with self._state_lock:
            self._state = self.CLOSED
            self._epoch += 1
        self._teardown_done.set()

    def _enter_closing(self) -> bool:
        with self._state_lock:
            if self._state not in (self.OPEN, self.OPENING):
                return False
            self._state = self.CLOSING
            return True

    def _is_terminal(self) -> bool:
        with self._state_lock:
            return self._state in (self.CLOSING, self.CLOSED, self.FAILED)

    def _is_open(self) -> bool:
        owned = self._owned
        with self._state_lock:
            if self._state != self.OPEN or owned is None or owned.stdin is None:
                return False
        try:
            return owned.poll() is None
        except OSError:
            return False

    def _process_exited(self, owned: OwnedProcess) -> bool:
        try:
            return owned.poll() is not None
        except OSError:
            return False

    def _request_cleanup(self) -> None:
        """Fail-closed teardown initiated from a background thread.

        Never joins the calling thread (that would self-deadlock); the teardown
        is idempotent via the CLOSING CAS.
        """

        if not self._enter_closing():
            return
        try:
            self._perform_teardown()
        finally:
            self._mark_terminal()

    def _perform_teardown(self) -> None:
        # ONE shared shutdown absolute deadline: phases never reset it.
        deadline = self._clock() + self._shutdown_timeout_seconds
        owned = self._owned
        if owned is not None:
            # 1) Graceful: EOF on stdin, brief window for a cooperative child.
            try:
                stdin = owned.stdin
                if stdin is not None:
                    stdin.close()
            except (OSError, ValueError):
                pass
            self._wait_bounded(owned, deadline=deadline, cap=0.5)
            # 2) Terminate the exact tree, bounded to a fixed SHARE of the one
            #    shared deadline.  A graceful terminate is often a no-op for
            #    console children (Windows taskkill without /F cannot close
            #    them), so it must not be allowed to burn the whole budget.
            if not self._process_exited(owned):
                terminate_until = min(deadline, self._clock() + 0.5)
                try:
                    owned.terminate_tree(deadline=terminate_until)
                except TransportError:
                    pass
            # 3) Force-kill the tree; the remaining shared budget is reserved
            #    for the force kill AND its reap (so Popen is never observed
            #    still-running at GC).
            if not self._process_exited(owned):
                try:
                    owned.kill_tree(deadline=deadline)
                except TransportError:
                    pass
            # 3b) Best-effort bounded reap with whatever budget remains.
            self._wait_bounded(owned, deadline=deadline, cap=None)
            # 4) Close the OS handles (unblocks any blocked reader thread).
            try:
                owned.close_handles()
            except (OSError, ValueError):
                pass
        # 5) Join reader threads, bounded; never self-join.
        for thread in self._threads:
            if thread is threading.current_thread():
                continue
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        self._owned = None
        # 6) Private temp cleanup; failures become the stable cleanup error.
        temporary = self._temp
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError:
                if self._cleanup_error is None:
                    self._cleanup_error = TransportError(
                        "mcp_temp_cleanup_failed"
                    )
            self._temp = None
        # 7) I6 §8.7: record what the close actually proved.  A phase that
        #    hit the shared shutdown deadline leaves outcome uncertain.
        self._close_report = CloseReport(
            process_exited=self._process_exited(owned) if owned is not None else None,
            process_terminated=owned is None or self._process_exited(owned),
            uncertain=self._close_uncertain or self._cleanup_error is not None,
            stderr_truncated=self._stderr_truncated,
            stderr_bytes=self._stderr_bytes,
            cleanup_error=
            None if self._cleanup_error is None else self._cleanup_error.code,
        )
        report = self._close_report
        if report is not None and report.uncertain:
            self._report_allocation("outcome_unknown")
        else:
            self._report_allocation("stopped")

    def _wait_bounded(
        self, owned: OwnedProcess, *, deadline: float, cap: float | None
    ) -> None:
        remaining = deadline - self._clock()
        if cap is not None:
            remaining = min(remaining, cap)
        if remaining <= 0:
            return
        try:
            owned.wait(deadline=self._clock() + remaining)
        except TransportError:
            pass

    def _collect_owned(self, owned: OwnedProcess) -> None:
        """Force-collect a started tree whose ACK was lost (bounded)."""
        deadline = self._clock() + self._shutdown_timeout_seconds
        try:
            terminate_until = min(deadline, self._clock() + 0.5)
            try:
                owned.terminate_tree(deadline=terminate_until)
            except TransportError:
                pass
            try:
                owned.kill_tree(deadline=deadline)
            except TransportError:
                pass
            owned.wait(deadline=deadline)
        except TransportError:
            pass
        finally:
            try:
                owned.close_handles()
            except (OSError, ValueError):
                pass

    def _enqueue(self, item: object) -> bool:
        try:
            self._messages.put_nowait(item)
        except queue.Full:
            # Notifications are advisory and the session layer coalesces them
            # into one refresh signal.  Do not let a notification burst evict
            # the bounded queue's capacity for correlated responses: the
            # latter must remain fail-closed when the queue is genuinely
            # saturated.  Requests are not expected from an MCP server, but
            # retain the same strict behavior as responses if they arrive.
            if isinstance(item, JsonRpcNotification):
                with self._notification_overflow_lock:
                    if self._notification_overflow is None:
                        self._notification_overflow = item
                return True
            if isinstance(item, JsonRpcResponse):
                # Preserve a correlated response when advisory notices have
                # occupied every queue slot.  Queue internals are protected
                # by their mutex; no task accounting is used by this queue.
                with self._messages.mutex:
                    retained: list[object] = []
                    evicted = False
                    while self._messages.queue:
                        queued = self._messages.queue.popleft()
                        if not evicted and isinstance(queued, JsonRpcNotification):
                            evicted = True
                            continue
                        retained.append(queued)
                    self._messages.queue.extend(retained)
                    if evicted:
                        self._messages.queue.append(item)
                        return True
            # Bounded queue overflow: latch the failure and fail closed.
            if self._overflow_code is None:
                self._overflow_code = "inbound_queue_overflow"
            self._request_cleanup()
            return False
        return True

    def _read_loop(self, process: OwnedProcess) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            while True:
                frame = _read_frame(
                    stream, max_frame_bytes=self._max_frame_bytes
                )
                if frame is None:
                    break
                try:
                    message = parse_message(frame.decode("utf-8", "strict"))
                except (McpProtocolError, UnicodeError) as error:
                    code = getattr(error, "code", "malformed_frame")
                    if not self._enqueue(_FrameError(code)):
                        return
                    continue
                if not self._enqueue(message):
                    return
        except TransportMalformedFrame as error:
            if not self._enqueue(_FrameError(error.code)):
                return
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._messages.put_nowait(None)
            except queue.Full:
                pass

    def _stderr_loop(self, process: OwnedProcess) -> None:
        stream = process.stderr
        if stream is None:
            return
        limit = self._max_stderr_bytes
        total = 0
        while True:
            try:
                chunk = stream.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            # Drain to EOF always; drop content past the retained limit so a
            # chatty child can never block on a full stderr pipe.
            total += len(chunk)
            self._stderr_bytes = total
            if total > limit:
                self._stderr_truncated = True


def spawn_fixture_command(extra_env: Mapping[str, str] | None = None) -> list[str]:
    """Return the stdio command for the local MCP fixture server.

    I1: absolute interpreter + absolute script path; no "python -m", no PATH
    or PYTHONPATH dependency.  The fixture module imports only the stdlib, so
    it also runs correctly as a plain script under the minimal environment.
    """

    script = Path(__file__).with_name("fixture_server.py").resolve(strict=True)
    return [sys.executable, str(script)]
