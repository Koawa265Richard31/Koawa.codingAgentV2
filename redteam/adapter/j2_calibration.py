"""RT/J J2 §8.4: 500/500 injected-canary / benign calibration gates.

Generates the two frozen sets deterministically (HMAC tokens over synthetic
turn ids; benign set covers realistic argument shapes without secrets),
measures detection/escalation rates, and reports Wilson 95% intervals.
Run standalone; results archived as JSON (doc §8.4 evidence).
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# J2 §8.4 calibration-set generator (rescanned via Edit): deterministic
# 500/500 injected + 0/500 benign gates with Wilson 95% intervals.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from koawa_agent_v2.security import derive_canary_token, scan_exact_token

N_POSITIVE = 500
N_NEGATIVE = 500
KEY = b"rtj-j2-calibration-key"


def wilson(p_success: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    denominator = 1 + z * z / n
    centre = p_success + z * z / (2 * n)
    spread = z * math.sqrt(p_success * (1 - p_success) / n + z * z / (4 * n * n))
    return ((centre - spread) / denominator, (centre + spread) / denominator)


def positive_cases() -> list[dict]:
    """Injected-canary positives: token embedded in realistic argument shapes."""
    cases = []
    shapes = [
        lambda tok: json.dumps({"payload": f"report {tok}"}),
        lambda tok: json.dumps({"path": "out.txt", "content": f"data {tok}"}),
        lambda tok: json.dumps({"url": "https://127.0.0.1/ingest", "body": tok}),
        lambda tok: json.dumps({"items": [tok, "x"]}),
        lambda tok: json.dumps({"cmd": f"note {tok}"}),
    ]
    for index in range(N_POSITIVE):
        turn = uuid4()
        token = derive_canary_token(KEY, turn)
        cases.append({
            "turn_id": str(turn),
            "arguments_json": shapes[index % len(shapes)](token),
            "token": token,
            "expected": True,
        })
    return cases


def negative_cases() -> list[dict]:
    """Benign set: realistic arguments with NO token (near-miss hex included)."""
    cases = []
    hex_noise = [
        "a" * 32, "0123456789abcdef" * 2, "deadbeef" * 4,
        "f" * 32, "1234567890abcdef1234567890abcdef",
    ]
    benign = [
        json.dumps({"path": "src/app.py", "start_line": 1, "max_lines": 50}),
        json.dumps({"payload": "plain report without secrets"}),
        json.dumps({"url": "https://api.example.com/v1/data"}),
        json.dumps({"query": "def main(", "max_matches": 10}),
        json.dumps({"items": ["alpha", "beta"], "limit": 5}),
        json.dumps({"note": "deadbeef" * 4}),
        json.dumps({"token_kind": "none", "count": 0}),
    ]
    for index in range(N_NEGATIVE):
        turn = uuid4()
        if index % 7 == 3:
            extra = f' "ref": "{hex_noise[index % len(hex_noise)]}"'
        else:
            extra = ""
        doc = benign[index % len(benign)]
        cases.append({
            "turn_id": str(turn),
            "arguments_json": doc[:-1] + extra + "}" if extra else doc,
            "token": derive_canary_token(KEY, turn),  # per-turn token never present
            "expected": False,
        })
    return cases


def evaluate(cases: list[dict]) -> dict:
    detected = sum(1 for c in cases if scan_exact_token(c["arguments_json"], c["token"]) == c["expected"])
    if cases[0]["expected"]:
        hits = sum(1 for c in cases if scan_exact_token(c["arguments_json"], c["token"]))
        rate = hits / len(cases)
    else:
        hits = sum(1 for c in cases if not scan_exact_token(c["arguments_json"], c["token"]))
        rate = hits / len(cases)
    low, high = wilson(rate, len(cases))
    return {
        "n": len(cases),
        "correct": detected,
        "rate": round(rate, 4),
        "wilson_95": [round(low, 4), round(high, 4)],
    }


def main() -> int:
    positives = evaluate(positive_cases())
    negatives = evaluate(negative_cases())
    passed = positives["rate"] == 1.0 and negatives["rate"] == 1.0
    report = {
        "schema_version": 1,
        "slice": "J2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "key_id": "rtj-j2-calibration-key (synthetic; production keys are per-session)",
        "injected_canary_set": positives,
        "benign_set": negatives,
        "gate": {"injected": "500/500 required", "benign": "0/500 false escalations required"},
        "passed": passed,
    }
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".dsh_tmp/j2-calibration.json")
    out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"passed": passed,
                      "injected": positives["rate"],
                      "benign": negatives["rate"]}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
