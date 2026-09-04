"""RT-1 adapter: PyRIT custom target over the Koawa bridge protocol (redteam venv).

The adapter owns the controlled sink (FE-NET oracle), the bridge subprocess
(production venv), and the frozen bridge protocol (redteam/freeze/bridge.md).
Forbidden-effect facts are collected per attempt for the report; the PyRIT
scorer path is NOT used for formal judgments (dual-track rule, plan §RT-1).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from pyrit.models import Message, MessagePiece
from pyrit.prompt_target import PromptTarget

REPO = Path(__file__).resolve().parents[2]


class LoopbackSink:
    """FE-NET external oracle (adapter side): records delivered payloads."""

    def __init__(self) -> None:
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self.port = self._server.getsockname()[1]
        self.received: list[bytes] = []
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        self._server.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (OSError, socket.timeout):
                continue
            with conn:
                conn.settimeout(1.0)
                try:
                    while True:
                        data = conn.recv(4096)
                        if not data:
                            break
                        self.received.append(data)
                except OSError:
                    pass

    def hits(self, nonce: str) -> int:
        return sum(1 for p in self.received if nonce.encode() in p)

    def close(self) -> None:
        self._stop.set()
        self._server.close()


class KoawaBridgeTarget(PromptTarget):
    """PromptTarget driving the real production chain over bridge.md v1."""

    def __init__(self, *, config_path: str, canary: str, production_python: str) -> None:
        super().__init__()
        self.canary = canary
        self.sink = LoopbackSink()
        self.facts: dict[str, dict] = {}
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        control_port = listener.getsockname()[1]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO / "src")
        environment["KOAWA_RT_SINK_PORT"] = str(self.sink.port)
        environment["KOAWA_RT_CANARY"] = canary
        import shlex
        python_command = shlex.split(production_python, posix=False)
        self._bridge = subprocess.Popen(
            [
                *python_command, "-B",
                str(REPO / "scripts" / "rt_target_bridge.py"),
                "--config", config_path,
                "--sink-port", str(self.sink.port),
                "--canary", canary,
                "--control-port", str(control_port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=str(REPO),
        )
        self.control, _ = listener.accept()
        self.control_file = self.control.makefile("rw", encoding="utf-8", newline="\n")
        ready_line = self.control_file.readline()
        if not ready_line:
            err = self._bridge.stderr.read(2000) if self._bridge.stderr else b"<none>"
            self._bridge.kill()
            raise RuntimeError(f"bridge_no_ready: {err!r}")
        ready = json.loads(ready_line)
        if ready.get("method") != "ready":
            raise RuntimeError(f"bridge_ready_failed: {ready}")

    def _send_line(self, document: dict) -> None:
        self.control_file.write(json.dumps(document, ensure_ascii=False) + "\n")
        self.control_file.flush()

    def _read_line(self) -> dict:
        line = self.control_file.readline()
        if not line:
            raise RuntimeError("bridge_control_closed")
        return json.loads(line)

    async def _send_prompt_to_target_async(self, *, normalized_conversation: list[Message]) -> list[Message]:
        import traceback
        try:
            return await self._send_inner(normalized_conversation)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            raise

    async def _send_inner(self, normalized_conversation: list[Message]) -> list[Message]:
        text = ""
        for piece in reversed(normalized_conversation[-1].message_pieces):
            value = piece.converted_value or piece.original_value
            if value:
                text = value
                break
        attempt_id = str(uuid.uuid4())
        self._send_line({"method": "prompt", "attempt_id": attempt_id, "text": text})
        response = None
        result = None
        while response is None or result is None:
            document = self._read_line()
            if document.get("method") == "response":
                response = document
            elif document.get("method") == "result":
                result = document
        self.facts[attempt_id] = {
            "attempt_id": attempt_id,
            "prompt_digest": __import__("hashlib").sha256(
                text.encode("utf-8", "replace")).hexdigest(),
            "turn_status": response.get("turn_status"),
            "ok": response.get("ok"),
            "action_facts": response.get("action_facts", []),
            "sink_hits": self.sink.hits(self.canary),
            "sink_bytes": len(b"".join(self.sink.received)),
            "final_text_snippet": final_text[:300],
        }
        final_text = response.get("final_text", "")
        return [
            Message(message_pieces=[MessagePiece(
                role="assistant", original_value=final_text, converted_value=final_text,
            )])
        ]

    def shutdown(self) -> None:
        try:
            self._send_line({"method": "shutdown"})
            self._read_line()
        except Exception:
            pass
        try:
            self._bridge.wait(timeout=30)
        except Exception:
            pass
        try:
            err = self._bridge.stderr.read() if self._bridge.stderr else b""
            if err:
                sys.stderr.write(
                    "[bridge stderr tail] "
                    + err.decode("utf-8", "replace")[-1500:]
                )
        except Exception:
            pass
        self.sink.close()

    def validate(self) -> None:  # PyRIT optional hook
        return None
