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


# R3 (closure review 2026-09-25): per-profile field release contracts.
# A profile with NO contract keeps the default safe-diagnostic set (the
# authorized safe-test capability); an explicit contract whitelists exactly
# the named diagnostic fields; a SENSITIVE profile without a contract
# yields the fixed withheld state below - never a test failure and never a
# quiet full release.  Policy markers and profile identity always ride
# (provenance), regardless of the contract.
ALWAYS_RELEASED_FIELDS = frozenset(
    {
        "profile_id",
        "test_output_policy",
        "test_output_visibility",
        "isolated_workspace",
    }
)
RELEASEABLE_FIELDS = frozenset(
    {
        "allocation_id",
        "argv",
        "backend",
        "diagnostics_excerpt",
        "container_id",
        "duration_ms",
        "exit_code",
        "immutable_image_id",
        "outcome",
        "profile_digest",
        "stderr_bytes",
        "stderr_truncated",
        "stdout_bytes",
        "stdout_truncated",
        "timeout_seconds",
    }
)
RESULT_WITHHELD = "result_withheld"
HUMAN_REQUIRED = "human_required"


class ReleaseRule:
    """One profile's field-release contract (deployment input)."""

    __slots__ = ("fields", "sensitive")

    def __init__(self, fields=None, sensitive: bool = False) -> None:
        if fields is not None:
            if (
                not isinstance(fields, tuple)
                or not fields
                or any(
                    not isinstance(item, str) or item not in RELEASEABLE_FIELDS
                    for item in fields
                )
                or len(set(fields)) != len(fields)
            ):
                raise ValueError("invalid_release_fields")
        if not isinstance(sensitive, bool):
            raise TypeError("sensitive must be bool")
        self.fields = fields
        self.sensitive = sensitive

    @property
    def withheld(self) -> bool:
        return self.sensitive and self.fields is None


def apply_release_rule(payload: dict, rule: "ReleaseRule | None") -> dict:
    """Filter a receipt payload by the profile's release contract."""

    if rule is None or rule.fields is None:
        return dict(payload)
    released = dict(payload)
    for key in list(released):
        if key in ALWAYS_RELEASED_FIELDS:
            continue
        if key not in rule.fields:
            released.pop(key)
    return released


def withheld_receipt(profile_id: str) -> dict:
    """The fixed sensitive-without-contract state (distinct from failure)."""

    return {
        "profile_id": profile_id,
        "test_output_policy": POLICY_VERSION,
        "test_output_visibility": BODY_VISIBILITY,
        "availability": RESULT_WITHHELD,
        "human_required": True,
    }


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
