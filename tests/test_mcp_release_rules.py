"""A2 acceptance (WP-C §8): per server/tool MCP release rules.

Default stays WP-1 (metadata only, result: None).  An explicit rule may widen
the metadata field set, and may release a BOUNDED excerpt only together with
an audience contract - the same proof-before-disclosure rule profiles use.
Every envelope keeps untrusted_mcp_result.
"""
from __future__ import annotations

import unittest

from koawa_agent_v2.mcp.release_rules import (
    MAX_EXCERPT_CHARS,
    McpReleaseRule,
    McpReleaseRuleError,
    build_envelope,
)

SECRET = "TOPSECRET-MCP-BODY"


class DefaultEnvelopeTest(unittest.TestCase):
    def test_no_rule_is_metadata_only(self) -> None:
        envelope = build_envelope(
            server_id="srv",
            tool_name="query",
            redacted_body=SECRET,
            is_error=False,
            rule=None,
        )
        self.assertIsNone(envelope["result"])
        self.assertIs(True, envelope["untrusted_mcp_result"])
        self.assertEqual("metadata_only", envelope["result_visibility"])
        self.assertEqual(len(SECRET), envelope["body_bytes"])
        self.assertNotIn(SECRET, str(envelope.values()))

    def test_rule_for_other_pair_does_not_apply(self) -> None:
        rule = McpReleaseRule(
            server_id="srv",
            tool_name="other",
            excerpt_chars=10,
            audience_policy_ref="audience/x",
        )
        envelope = build_envelope(
            server_id="srv",
            tool_name="query",
            redacted_body=SECRET,
            is_error=False,
            rule=rule,
        )
        self.assertIsNone(envelope["result"])


class RuleEnvelopeTest(unittest.TestCase):
    def test_fields_whitelist_limits_metadata(self) -> None:
        rule = McpReleaseRule(
            server_id="srv", tool_name="query", fields=("body_bytes",),
        )
        envelope = build_envelope(
            server_id="srv",
            tool_name="query",
            redacted_body="abc",
            is_error=False,
            rule=rule,
        )
        self.assertIn("body_bytes", envelope)
        self.assertNotIn("is_error", envelope)
        # Provenance markers always ride.
        for key in ("untrusted_mcp_result", "server_id", "tool", "output_policy"):
            self.assertIn(key, envelope)
        self.assertIsNone(envelope["result"])

    def test_excerpt_requires_audience_contract(self) -> None:
        with self.assertRaises(McpReleaseRuleError) as raised:
            McpReleaseRule(server_id="srv", tool_name="query", excerpt_chars=10)
        self.assertIn("audience", str(raised.exception))

    def test_excerpt_is_bounded(self) -> None:
        rule = McpReleaseRule(
            server_id="srv",
            tool_name="query",
            excerpt_chars=5,
            audience_policy_ref="audience/x",
        )
        envelope = build_envelope(
            server_id="srv",
            tool_name="query",
            redacted_body=SECRET,
            is_error=False,
            rule=rule,
        )
        self.assertEqual(SECRET[:5], envelope["result"])
        self.assertEqual("bounded_excerpt", envelope["result_visibility"])
        self.assertIs(True, envelope["excerpt_truncated"])
        self.assertIs(True, envelope["untrusted_mcp_result"])
        self.assertNotEqual(SECRET, envelope["result"])

    def test_excerpt_size_is_capped(self) -> None:
        with self.assertRaises(McpReleaseRuleError):
            McpReleaseRule(
                server_id="srv",
                tool_name="query",
                excerpt_chars=MAX_EXCERPT_CHARS + 1,
                audience_policy_ref="audience/x",
            )
        with self.assertRaises(McpReleaseRuleError):
            McpReleaseRule(
                server_id="srv", tool_name="query", excerpt_chars=0,
                audience_policy_ref="audience/x",
            )

    def test_bad_field_name_rejected(self) -> None:
        with self.assertRaises(McpReleaseRuleError):
            McpReleaseRule(
                server_id="srv", tool_name="query", fields=("raw_body",),
            )


class HandlerIntegrationTest(unittest.TestCase):
    """bind_tool_handler honours the configured rule set."""

    def test_handler_default_and_configured(self) -> None:
        from koawa_agent_v2.mcp import connection_manager as cm

        class _Result:
            content = SECRET
            is_error = False
            uncertain = False

        class _Session:
            def call(self, binding, arguments_json):
                return _Result()

        class _Binding:
            server_id = "srv"
            tool_name = "query"

        handler = cm.bind_tool_handler(_Session(), _Binding())
        self.assertIsNone(cm._mcp_release_rule(_Binding()))

        cm.configure_release_rules(())
        self.assertIsNone(cm._mcp_release_rule(_Binding()))

        cm.configure_release_rules(
            (
                McpReleaseRule(
                    server_id="srv",
                    tool_name="query",
                    excerpt_chars=4,
                    audience_policy_ref="audience/x",
                ),
            )
        )
        rule = cm._mcp_release_rule(_Binding())
        self.assertIsNotNone(rule)
        cm.configure_release_rules(())


if __name__ == "__main__":
    unittest.main()
