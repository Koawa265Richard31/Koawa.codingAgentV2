"""RT-1 target bridge (production venv): drives the real production chain.

Protocol (redteam/freeze/bridge.md v1): newline-delimited JSON on stdin/stdout.
Methods: hello/ready, prompt -> response+result, fetch_turn, shutdown.
Restricted payloads: text projections and digest-only facts cross the boundary;
credentials stay in the environment; no hidden reasoning is ever emitted.

The target is the REAL production assembly (AppRuntime, real provider, sealed
verified registry) plus the frozen control-exercise additions: a test-only
``loopback_egress`` tool (default-deny policy) and the canary/sink environment
for the FE-NET oracle.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def emit(document: dict) -> None:
    CONTROL_CHANNEL.send(document)


class ControlChannel:
    """Protocol frames over the adapter's localhost socket (never stdout)."""

    file = None

    @staticmethod
    def send(document: dict) -> None:
        CONTROL_CHANNEL.file.write(
            json.dumps(document, ensure_ascii=False) + "\n"
        )
        CONTROL_CHANNEL.file.flush()


CONTROL_CHANNEL = ControlChannel()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--sink-port", type=int, required=True)
    parser.add_argument("--canary", required=True)
    parser.add_argument("--control-port", type=int, required=True)
    parser.add_argument("--mode", choices=("run", "chat"), default="run")
    arguments = parser.parse_args()

    # Production code prints to stdout; the control channel is a localhost
    # socket the adapter listens on first, so protocol frames never mix with
    # process stdout/stderr noise.
    control = socket.create_connection(("127.0.0.1", arguments.control_port), timeout=30)
    CONTROL_CHANNEL.file = control.makefile("rw", encoding="utf-8", newline="\n")
    control_file = CONTROL_CHANNEL.file

    sink_port = arguments.sink_port
    canary = arguments.canary

    def _egress_handler(arguments, *, context):
        # control-exercise action: delivers the payload to the controlled
        # local sink; policy default-denies it, so reaching here means an
        # ALLOW path was taken (positive-control only in J1 lanes).
        with socket.create_connection(("127.0.0.1", sink_port), timeout=3) as conn:
            conn.sendall(arguments.payload.encode("utf-8"))
        return __import__("koawa_agent_v2.execution.loop", fromlist=["ToolExecutionResult"]).ToolExecutionResult(
            json.dumps({"delivered": True, "bytes": len(arguments.payload)})
        )

    from koawa_agent_v2.runtime.app import AppRuntime
    from koawa_agent_v2.tools.schema import ToolSpec
    from dataclasses import dataclass

    @dataclass(frozen=True, slots=True)
    class EgressArguments:
        payload: str

    egress_spec = ToolSpec(
        "loopback_egress",
        "RT-1 control-exercise egress probe (default-deny)",
        EgressArguments,
        {
            "type": "object",
            "properties": {
                "payload": {"type": "string", "minLength": 1, "maxLength": 4096},
            },
            "required": ["payload"],
            "additionalProperties": False,
        },
    )

    app = AppRuntime.from_config_file(arguments.config)

    def _register_rt1_probe(registry):
        registry.register(egress_spec, _egress_handler)

    # Register before the first execution-plane build seals the catalog.
    app._post_build_registrars = (_register_rt1_probe,)
    try:
        execution = app._ensure_execution_plane()
        emit({"method": "ready", "config_hash": "bound-at-runtime",
              "model_id": config_model(config=None, app=app)})

        for control_line in control_file:
            control_line = control_line.strip()
            if not control_line:
                continue
            request = json.loads(control_line)
            method = request.get("method")
            if method == "prompt":
                outcome = run_attempt(app, request, mode=arguments.mode)
                emit({"method": "response", **outcome})
                emit({"method": "result", "attempt_id": request.get("attempt_id"),
                      "action_facts": outcome.get("action_facts", []),
                      "sink_receipts": sink_receipts(sink_port)})
            elif method == "fetch_turn":
                emit({"method": "turn_text",
                      "attempt_id": request.get("attempt_id"),
                      "text": outcome.get("final_text", "")})
            elif method == "shutdown":
                emit({"method": "bye"})
                return 0
            else:
                emit({"method": "error", "code": "bridge_unknown_method"})
        return 0
    finally:
        closer = getattr(app, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:
                pass


def config_model(*, config=None, app=None) -> str:
    return app.config.provider.model


def sink_receipts(sink_port: int) -> list[dict]:
    # The sink itself lives in the adapter process (redteam side); the bridge
    # reports only its own egress handler activity through action_facts.
    return []


def run_attempt(app, request: dict, *, mode: str) -> dict:
    attempt_id = request.get("attempt_id") or str(uuid.uuid4())
    text = request.get("text", "")
    collect_new_events(app)  # advance past pre-attempt events
    outcome = app.run(text)
    action_facts = [
        {"event_type": event.event_type,
         "digest": __import__("hashlib").sha256(
             json.dumps(event.payload, sort_keys=True, ensure_ascii=False,
                        default=str).encode("utf-8")).hexdigest()}
        for event in collect_new_events(app)
        if event.event_type.startswith(("tool.", "resource.", "mcp."))
    ]
    payload = dict(outcome.payload or {})
    final_text = ""
    for key in ("final_answer", "final", "answer", "report", "message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            final_text = value
            break
    if not final_text:
        final_text = json.dumps(payload, ensure_ascii=False)[:4000]
    return {
        "attempt_id": attempt_id,
        "final_text_digest": __import__("hashlib").sha256(
            final_text.encode("utf-8", "replace")).hexdigest(),
        "turn_status": outcome.code,
        "ok": outcome.ok,
        "final_text": final_text[:4000],
        "payload_keys": sorted(payload),
        "action_facts": action_facts,
    }


def collect_new_events(app):
    """Advance the bridge cursor and return only newly-appended events."""
    cursor_attr = "_rt_cursor"
    store = app.assembled.store
    cursor = getattr(app, cursor_attr, 0)
    collected = []
    while True:
        page = store.read_all(after_position=cursor, limit=500)
        collected.extend(page)
        if not page:
            break
        cursor = page[-1].global_position
    setattr(app, cursor_attr, cursor)
    return collected


if __name__ == "__main__":
    raise SystemExit(main())
