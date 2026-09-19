"""Isolated config ingress and eval-path audit; no network or paid model."""
import importlib.util
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from koawa_agent_v2.runtime.config import load_runtime_config, RuntimeConfigError

root = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix="koawa-audit-config-") as tmp:
    config = Path(tmp) / "config.json"
    config.write_text(json.dumps({"canary_key_env": "AUDIT_CANARY_ENV_NAME"}), encoding="utf-8")
    try:
        load_runtime_config(config)
        outcome = "accepted"
    except RuntimeConfigError as exc:
        outcome = exc.code
    spec = importlib.util.spec_from_file_location("audit_eval", root / "evals/run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with patch("koawa_agent_v2.execution.loop.AgentLoop.run", side_effect=AssertionError("AgentLoop must be observed")) as spy:
        report = module.main(root / "evals/tasks", Path(tmp) / "eval.json")
    print(json.dumps({"config_input_fields": ["canary_key_env"], "loader_result": outcome,
        "note": "unknown-field rejection precedes required-field validation; tests JSON field ingress, not full config startup",
        "eval_total": report["total"], "eval_success": report["success"],
        "agent_loop_calls": spy.call_count, "eval_report_label": report["mandatory_reliability"]}, indent=2))
