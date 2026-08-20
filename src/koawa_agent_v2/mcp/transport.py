"""MCP stdio transport with strict Content-Length framing (stdlib only)."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence

from .protocol import (
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    McpProtocolError,
    parse_message,
)


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


class StdioTransport:
    """Spawn one stdio MCP process and frame JSON-RPC messages over it."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str | None = None,
        max_frame_bytes: int = 1_048_576,
        max_stderr_bytes: int = 262_144,
    ) -> None:
        if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
            raise TypeError("command must be a sequence")
        if not command or any(not isinstance(item, str) for item in command):
            raise ValueError("command must contain non-empty strings")
        if not isinstance(env, Mapping):
            raise TypeError("env must be a mapping")
        self._command = tuple(command)
        self._env = dict(env)
        self._cwd = cwd
        self._max_frame_bytes = max_frame_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._process: subprocess.Popen[bytes] | None = None
        self._messages: queue.Queue[object] = queue.Queue()
        self._write_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._stderr_truncated = False
        self._threads: list[threading.Thread] = []

    @property
    def stderr_truncated(self) -> bool:
        return self._stderr_truncated

    @property
    def closed(self) -> bool:
        return self._closed

    def open(self) -> None:
        if self._closed:
            raise TransportClosed()
        base_env = dict(os.environ)
        base_env.update(self._env)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = subprocess.Popen(
            list(self._command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._cwd,
            env=base_env,
            creationflags=creationflags,
        )
        stdout_thread = threading.Thread(
            target=self._read_loop,
            name=f"mcp-stdout-{id(self)}",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name=f"mcp-stderr-{id(self)}",
            daemon=True,
        )
        self._threads = [stdout_thread, stderr_thread]
        stdout_thread.start()
        stderr_thread.start()

    def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._messages.put(_FrameError("transport_closed"))
            self._messages.put(None)
            return
        try:
            while True:
                frame = _read_frame(process.stdout, max_frame_bytes=self._max_frame_bytes)
                if frame is None:
                    break
                try:
                    message = parse_message(frame.decode("utf-8", "strict"))
                except (McpProtocolError, UnicodeError) as error:
                    code = getattr(error, "code", "malformed_frame")
                    self._messages.put(_FrameError(code))
                    continue
                self._messages.put(message)
        except TransportMalformedFrame as error:
            self._messages.put(_FrameError(error.code))
        except (OSError, ValueError):
            pass
        finally:
            self._messages.put(None)

    def _stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        total = 0
        while total < self._max_stderr_bytes:
            chunk = process.stderr.read(4096)
            if not chunk:
                return
            total += len(chunk)
        if total >= self._max_stderr_bytes:
            self._stderr_truncated = True

    def send(self, payload: str) -> None:
        if not isinstance(payload, str):
            raise TypeError("payload must be str")
        body = payload.encode("utf-8", "strict")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self._write_lock:
            if self._closed or self._process is None or self._process.stdin is None:
                raise TransportClosed()
            if self._process.poll() is not None:
                raise TransportClosed()
            try:
                self._process.stdin.write(header)
                self._process.stdin.write(body)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                raise TransportClosed() from None

    def read(self, timeout: float) -> JsonRpcRequest | JsonRpcResponse | JsonRpcNotification:
        try:
            item = self._messages.get(timeout=timeout)
        except queue.Empty:
            raise TransportTimeout() from None
        if isinstance(item, _FrameError):
            raise TransportMalformedFrame(item.code)
        if item is None:
            raise TransportClosed()
        return item

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        for thread in self._threads:
            thread.join(timeout=5)
        self._messages.put(None)


def spawn_fixture_command(extra_env: Mapping[str, str] | None = None) -> list[str]:
    """Return the stdio command for the local MCP fixture server."""

    return [sys.executable, "-m", "koawa_agent_v2.mcp.fixture_server"]
