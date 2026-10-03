"""R4 (closure review 2026-09-25): measured metering error probe.

Sends a small, fixed set of REPRESENTATIVE request shapes to a real provider
and records ``usage.prompt_tokens`` against the loop's char-level estimate,
so the final-send gate's conservatism can be stated as a measured error band
instead of a guess.

Endpoint safety (probe-side guard): https only, the host must resolve to
public addresses (private/loopback/link-local rejected), and redirects are
never followed.  The API key comes ONLY from the environment.

Usage:

    PYTHONPATH=src py -3.13 scripts/r4_metering_probe.py \
        --api-key-env SF_CodingAgentTestKey \
        --base-url https://api.siliconflow.cn/v1 \
        --model deepseek-ai/DeepSeek-V4-Flash \
        --output .dsh_tmp/r2r4/r4-metering-probe.json

Read-only (one-shot chat completions, no tools executed); 8 requests total.
Results feed implementation/r4-measured-metering-report.md; the gate itself
stays char-based.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import time
import urllib.request
from pathlib import Path

from koawa_agent_v2.execution.loop import _PROTOCOL_OVERHEAD_PER_ITEM_CHARS
from koawa_agent_v2.model.protocol import (
    InstructionMessage,
    InstructionRole,
    ToolDefinition,
    UserMessage,
)

# Representative shapes: plain instructions, mixed CJK/ASCII, one large
# tool schema, a schema-heavy catalog, and a long tool-result echo.
_SHAPES = (
    ("small-ascii", 1_200, 0, False),
    ("mixed-cjk", 1_200, 0, True),
    ("medium-context", 12_000, 0, False),
    ("large-context", 60_000, 0, False),
    ("one-tool-schema", 1_200, 1, False),
    ("schema-catalog", 1_200, 8, False),
    ("schema-catalog-cjk", 1_200, 8, True),
    ("large-context-plus-schema", 60_000, 8, False),
)

_SCHEMA_TEMPLATE = (
    '{"type":"object","properties":{"path_%d":{"type":"string","description":'
    '"%s"},"mode_%d":{"type":"string","enum":["a","b","c"]}},"required":["path_%d"]}'
)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect_not_allowed", headers, fp,
        )


_OPENER = urllib.request.build_opener(_NoRedirect())


def validate_endpoint(base_url: str) -> str:
    """https-only endpoint on a public host; redirects never followed."""

    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit("metering endpoint must be https with a hostname")
    try:
        infos = socket.getaddrinfo(
            parsed.hostname, 443, proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise SystemExit(f"endpoint host does not resolve: {exc}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        ):
            raise SystemExit(
                "metering endpoint resolves to a non-public address"
            )
    return base_url.rstrip("/")


def _payload(size_hint: int, cjk: bool) -> str:
    unit = ("配置说明" if cjk else "description text") + " padding 0123456789"
    repeats = max(1, size_hint // len(unit))
    return unit * repeats


def _definitions(count: int) -> tuple[ToolDefinition, ...]:
    definitions = []
    for index in range(count):
        schema = _SCHEMA_TEMPLATE % (
            index, _payload(320, False), index, index,
        )
        definitions.append(
            ToolDefinition(f"tool_{index}", _payload(240, False), schema)
        )
    return tuple(definitions)


def _estimate(items, definitions) -> int:
    chars = 0
    for item in items:
        if isinstance(item, (UserMessage, InstructionMessage)):
            chars += len(item.content)
    definitions_chars = sum(
        len(d.name) + len(d.description) + len(d.input_schema_json) + 48
        for d in definitions
    )
    return chars + definitions_chars + 48 * len(items)


def _post(url: str, key: str, body: dict, timeout: float = 120.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with _OPENER.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key-env", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()

    key = os.environ.get(arguments.api_key_env, "")
    if not key:
        raise SystemExit(f"api key env {arguments.api_key_env} is not set")
    endpoint = validate_endpoint(arguments.base_url)

    results = []
    for shape, chars, tool_count, cjk in _SHAPES:
        items = [
            InstructionMessage(
                InstructionRole.SYSTEM,
                "You are a metering probe. Reply with the single word ok.",
            ),
            UserMessage(f"probe-{shape}", _payload(chars, cjk)),
        ]
        definitions = _definitions(tool_count)
        estimated = _estimate(items, definitions)
        body = {
            "model": arguments.model,
            "messages": [
                {"role": "system", "content": items[0].content},
                {"role": "user", "content": items[1].content},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": definition.name,
                        "description": definition.description,
                        "parameters": json.loads(
                            definition.input_schema_json
                        ),
                    },
                }
                for definition in definitions
            ],
            "max_tokens": 8,
            "stream": False,
        }
        started = time.time()
        document = _post(endpoint + "/chat/completions", key, body)
        usage = document.get("usage") or {}
        actual = int(usage.get("prompt_tokens") or 0)
        cached = (
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or usage.get("prompt_cache_hit_tokens")
            or 0
        )
        results.append(
            {
                "shape": shape,
                "estimated_chars": estimated,
                "actual_prompt_tokens": actual,
                "cached_tokens": cached,
                "chars_per_token": (
                    round(estimated / actual, 3) if actual else None
                ),
                "elapsed_s": round(time.time() - started, 2),
            }
        )
        print(json.dumps(results[-1], ensure_ascii=False))
    Path(arguments.output).parent.mkdir(parents=True, exist_ok=True)
    Path(arguments.output).write_text(
        json.dumps(
            {
                "model": arguments.model,
                "base_url": arguments.base_url,
                "probe": "r4-metering-v1",
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {arguments.output}")


if __name__ == "__main__":
    main()
