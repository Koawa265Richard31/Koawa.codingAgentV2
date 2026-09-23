"""Hardening 2026-09-19: model-visible test output policy (single authority).

Test/MCP raw output is unbounded operator diagnostics - it may carry env
dumps, secrets or credential shapes that pattern redaction cannot prove
safe, and the current host/whole-repo runners provide no isolation proof.
The model-visible receipt therefore carries FIXED STRUCTURED DIAGNOSTICS
only (status, exit code, sizes, truncation flags); stdout/stderr bodies
stay in the durable record for operator inspection.

Releasing bodies to the model requires a verified isolated environment
(only allowed code/dependencies/synthetic inputs, no credentials, no
network, runtime-verified) - no such proof exists yet, so no code path may
re-open the bodies today.  See
docs/open-active/hardening-memory-retrieval-2026-09-19/.
"""

POLICY_VERSION = "test-output-policy-v1"
BODY_VISIBILITY = "metadata_only"
"""Every test receipt is annotated with this visibility; a future isolated
environment would introduce a new value (e.g. ``safe_diagnostics_rich``)
together with the runtime proof that justifies it."""

MCP_POLICY_VERSION = "mcp-output-policy-v1"
MCP_BODY_VISIBILITY = "metadata_only"
"""MCP whitelist membership proves the SOURCE, never the body.  Until a
verified safe-projection adapter exists for a server/tool pair, MCP receipts
carry metadata only (server, tool, status, byte counts) - the redacted body
stays operator-side and never enters the model service."""


def safe_diagnostics(result) -> dict:
    """Fixed structured diagnostic fields for one CommandResult.

    Never includes stdout/stderr bodies; sizes and truncation flags stay so
    the model can tell that output existed and was withheld.
    """
    return {
        "exit_code": result.exit_code,
        "outcome": result.outcome.value,
        "duration_ms": result.duration_ms,
        "stderr_bytes": result.stderr_bytes,
        "stderr_truncated": result.stderr_truncated,
        "stdout_bytes": result.stdout_bytes,
        "stdout_truncated": result.stdout_truncated,
        "timeout_seconds": result.timeout_seconds,
    }
