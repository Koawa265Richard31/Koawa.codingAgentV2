"""A2 (WP-C §8): per server/tool MCP release rules.

A whitelist proves the SOURCE, never the body (WP-1).  This module adds the
operator-side adapter that may release *more* than the fixed metadata, but
only under an explicit per-tool contract:

- no rule                      -> today's behaviour: metadata only,
                                  ``result: None``;
- rule with ``fields``          -> those metadata fields only;
- rule with ``excerpt_chars``   -> a bounded excerpt of the redacted body,
                                  and ONLY when the rule also names an
                                  ``audience_policy_ref`` (the same
                                  proof-before-disclosure rule the profile
                                  contracts use for detailed mode).

Every envelope keeps ``untrusted_mcp_result`` so downstream surfaces still
treat MCP-derived bytes as untrusted.  Raw bodies never ride the fallback
path: unknown content yields the fixed metadata-only receipt.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..verification.output_policy import MCP_BODY_VISIBILITY, MCP_POLICY_VERSION

MAX_EXCERPT_CHARS = 2_000

# Metadata fields the adapter may expose.  Everything here is derived facts
# about the call, never the body itself.
RELEASEABLE_MCP_FIELDS = frozenset(
    {
        "server_id",
        "tool",
        "is_error",
        "body_bytes",
        "result_visibility",
        "output_policy",
        "untrusted_mcp_result",
        "excerpt_truncated",
    }
)
ALWAYS_MCP_FIELDS = frozenset(
    {"untrusted_mcp_result", "server_id", "tool", "output_policy"}
)


class McpReleaseRuleError(RuntimeError):
    """Content-free, stable code for a malformed rule."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class McpReleaseRule:
    """One operator-side release contract for a server/tool pair."""

    server_id: str
    tool_name: str
    fields: tuple[str, ...] | None = None
    excerpt_chars: int | None = None
    audience_policy_ref: str | None = None

    def __post_init__(self) -> None:
        for name in ("server_id", "tool_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise McpReleaseRuleError("mcp_rule_identity_required")
        if self.fields is not None:
            if (
                not isinstance(self.fields, tuple)
                or not self.fields
                or len(set(self.fields)) != len(self.fields)
                or any(item not in RELEASEABLE_MCP_FIELDS for item in self.fields)
            ):
                raise McpReleaseRuleError("invalid_mcp_rule_fields")
        if self.excerpt_chars is not None:
            if (
                not isinstance(self.excerpt_chars, int)
                or isinstance(self.excerpt_chars, bool)
                or not 1 <= self.excerpt_chars <= MAX_EXCERPT_CHARS
            ):
                raise McpReleaseRuleError("invalid_mcp_excerpt_chars")
            # Proof before disclosure: an excerpt needs an audience contract.
            if not isinstance(self.audience_policy_ref, str) or not (
                self.audience_policy_ref.strip()
            ):
                raise McpReleaseRuleError("mcp_excerpt_requires_audience")

    @property
    def key(self) -> tuple[str, str]:
        return self.server_id, self.tool_name


def build_envelope(
    *,
    server_id: str,
    tool_name: str,
    redacted_body: str,
    is_error: bool,
    rule: McpReleaseRule | None,
) -> dict:
    """Build the model-facing envelope for one MCP call result."""

    envelope: dict = {
        "untrusted_mcp_result": True,
        "server_id": server_id,
        "tool": tool_name,
        "result_visibility": MCP_BODY_VISIBILITY,
        "output_policy": MCP_POLICY_VERSION,
        "body_bytes": len(redacted_body),
        "is_error": bool(is_error),
        "result": None,
    }
    if rule is None:
        return envelope
    if rule.server_id != server_id or rule.tool_name != tool_name:
        # A rule for another pair never applies; fail closed to metadata.
        return envelope

    if rule.fields is not None:
        allowed = set(rule.fields) | ALWAYS_MCP_FIELDS
        envelope = {
            key: value for key, value in envelope.items() if key in allowed
        }
        # The body slot always exists (None unless released below); filtering
        # metadata must never make the envelope shape ambiguous.
        envelope.setdefault("result", None)

    if rule.excerpt_chars is not None:
        excerpt = redacted_body[: rule.excerpt_chars]
        envelope["result"] = excerpt
        envelope["result_visibility"] = "bounded_excerpt"
        envelope["excerpt_truncated"] = len(redacted_body) > len(excerpt)
    return envelope
