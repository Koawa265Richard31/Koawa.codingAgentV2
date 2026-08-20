from __future__ import annotations

import unittest
from dataclasses import replace

from koawa_agent_v2.policy import (
    ActionKind,
    ActionRequest,
    CredentialScope,
    Decision,
    NetworkTarget,
    Origin,
    PolicyEngine,
    PolicyError,
    PolicyRule,
    Principal,
    ResolvedAction,
    ResolvedResource,
    ResourceBudgetLimits,
    ResourceRequest,
    canonical_arguments,
    narrow_child_principal,
    normalize_https_url,
    preflight_resource_request,
    resolve_action_request,
    resolve_network_target,
    resolve_redirect,
)
from koawa_agent_v2.policy import SideEffectClass


PUBLIC_DNS_1 = "8.8.8.8"
PUBLIC_DNS_2 = "1.1.1.1"


def _dns(*addresses: str):
    answers = tuple(addresses)

    def resolve(_: str) -> tuple[str, ...]:
        return answers

    return resolve


def _local_action(**changes: object) -> ResolvedAction:
    values: dict[str, object] = {
        "kind": ActionKind.BUILTIN_TOOL,
        "tool_name": "read_file",
        "canonical_arguments_json": '{"path":"README.md"}',
        "principal": Principal("local-user", ("read", "write")),
        "side_effect_class": SideEffectClass.READ_ONLY,
        "sandbox_profile_id": "sandbox.read-only.v1",
        "policy_version": "policy-v1",
    }
    values.update(changes)
    return ResolvedAction(**values)  # type: ignore[arg-type]


def _allow_rule(rule_id: str = "allow") -> PolicyRule:
    return PolicyRule(rule_id, Decision.ALLOW)


class PolicyDecisionTest(unittest.TestCase):
    def test_deny_precedes_ask_and_allow_regardless_of_rule_id(self) -> None:
        action = _local_action()
        engine = PolicyEngine(
            "policy-v1",
            (
                PolicyRule("z-allow", Decision.ALLOW),
                PolicyRule("a-ask", Decision.ASK),
                PolicyRule("m-deny", Decision.DENY),
            ),
        )

        verdict = engine.evaluate(action)

        self.assertIs(Decision.DENY, verdict.decision)
        self.assertEqual("rule_denied", verdict.code)
        self.assertEqual(("a-ask", "m-deny", "z-allow"), verdict.matched_rule_ids)

    def test_ask_precedes_allow_and_allow_is_returned_when_alone(self) -> None:
        action = _local_action()
        ask = PolicyEngine(
            "policy-v1",
            (PolicyRule("allow", Decision.ALLOW), PolicyRule("ask", Decision.ASK)),
        ).evaluate(action)
        allow = PolicyEngine("policy-v1", (_allow_rule(),)).evaluate(action)

        self.assertIs(Decision.ASK, ask.decision)
        self.assertEqual("approval_required", ask.code)
        self.assertIs(Decision.ALLOW, allow.decision)
        self.assertEqual("allowed", allow.code)

    def test_no_matching_rule_denies_by_default(self) -> None:
        verdict = PolicyEngine(
            "policy-v1",
            (
                PolicyRule(
                    "other-tool",
                    Decision.ALLOW,
                    tool_names=("write_file",),
                ),
            ),
        ).evaluate(_local_action())

        self.assertIs(Decision.DENY, verdict.decision)
        self.assertEqual("denied_by_default", verdict.code)


class CanonicalActionIdentityTest(unittest.TestCase):
    def test_canonical_json_sorts_keys_and_rejects_duplicates_and_nan(self) -> None:
        self.assertEqual('{"a":0,"b":1}', canonical_arguments('{"b":1.0,"a":-0.0}'))
        for raw, code in (
            ('{"a":1,"a":2}', "duplicate_tool_argument_key"),
            ('{"outer":{"x":1,"x":2}}', "duplicate_tool_argument_key"),
            ('{"value":NaN}', "invalid_tool_arguments"),
            ('{"value":Infinity}', "invalid_tool_arguments"),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(PolicyError) as raised:
                    canonical_arguments(raw)
                self.assertEqual(code, raised.exception.code)

    def test_digest_binds_every_security_relevant_dimension(self) -> None:
        origin = Origin("https", "example.com", 443)
        network = NetworkTarget(
            "https://example.com/api",
            origin,
            (PUBLIC_DNS_1,),
            True,
        )
        resource = ResolvedResource(
            "path",
            "src/app.py",
            "C:/repo/src/app.py",
            "volume-7:file-10",
            (("cwd", "C:/repo"), ("link_state", "regular")),
        )
        budget = ResourceRequest(
            cpus=0.5,
            memory_bytes=32_000_000,
            pids=8,
            tmpfs_bytes=1_000_000,
            duration_seconds=5.0,
            stdout_bytes=10_000,
            stderr_bytes=10_000,
            network_bytes=2_000,
            tool_calls=1,
            subagents=0,
        )
        base = _local_action(
            resources=(resource,),
            network_target=network,
            credential_scope=CredentialScope("api-token", (origin,), ()),
            resource_request=budget,
        )
        variants = {
            "tool": replace(base, tool_name="read_other"),
            "arguments": replace(base, canonical_arguments_json='{"path":"src/app.py"}'),
            "resource_path": replace(
                base,
                resources=(replace(resource, resolved="C:/repo/src/other.py"),),
            ),
            "resource_identity": replace(
                base,
                resources=(replace(resource, identity="volume-7:file-11"),),
            ),
            "resource_metadata": replace(
                base,
                resources=(replace(resource, metadata=(("cwd", "C:/other"),)),),
            ),
            "side_effect": replace(
                base,
                side_effect_class=SideEffectClass.IDEMPOTENT_WRITE,
            ),
            "sandbox": replace(base, sandbox_profile_id="sandbox.write.v1"),
            "network": replace(
                base,
                network_target=NetworkTarget(
                    "https://example.com/other",
                    origin,
                    (PUBLIC_DNS_1,),
                    True,
                ),
            ),
            "credential": replace(
                base,
                credential_scope=CredentialScope("other-token", (origin,), ()),
            ),
            "budget": replace(base, resource_request=replace(budget, stdout_bytes=9_999)),
            "policy": replace(base, policy_version="policy-v2"),
            "principal": replace(
                base,
                principal=Principal("other-user", ("read", "write")),
            ),
        }

        digests = {base.action_digest}
        for dimension, action in variants.items():
            with self.subTest(dimension=dimension):
                self.assertNotEqual(base.action_digest, action.action_digest)
                digests.add(action.action_digest)
        self.assertEqual(len(variants) + 1, len(digests))

    def test_fake_resource_reresolution_exposes_symlink_and_cwd_drift(self) -> None:
        state = {
            "cwd": "C:/repo",
            "resolved": "C:/repo/src/app.py",
            "identity": "volume-7:file-10",
            "link_state": "regular",
        }

        def resolver(reference: str) -> ResolvedResource:
            return ResolvedResource(
                "path",
                reference,
                state["resolved"],
                state["identity"],
                (("cwd", state["cwd"]), ("link_state", state["link_state"])),
            )

        request = ActionRequest(
            kind=ActionKind.BUILTIN_TOOL,
            tool_name="read_file",
            arguments_json='{"path":"src/app.py"}',
            principal=Principal("local-user", ("read",)),
            side_effect_class=SideEffectClass.READ_ONLY,
            sandbox_profile_id="sandbox.read-only.v1",
            resource_references=("src/app.py",),
        )
        approved = resolve_action_request(
            request,
            policy_version="policy-v1",
            resource_resolver=resolver,
        )

        state.update(
            cwd="C:/other-repo",
            resolved="C:/outside/target.py",
            identity="volume-9:file-2",
            link_state="symlink",
        )
        reresolved = resolve_action_request(
            request,
            policy_version="policy-v1",
            resource_resolver=resolver,
        )

        self.assertNotEqual(approved.resources, reresolved.resources)
        self.assertNotEqual(approved.action_digest, reresolved.action_digest)


class NetworkPolicyTest(unittest.TestCase):
    def test_https_normalization_uses_default_443_and_ascii_canonical_form(self) -> None:
        normalized = normalize_https_url(
            "HTTPS://EXAMPLE.COM:443/a/../b/%7e?q=%41"
        )

        self.assertEqual("https://example.com/b/~?q=A", normalized)

    def test_unsafe_url_forms_are_rejected(self) -> None:
        cases = {
            "http://example.com/": "https_required",
            "https://user:secret@example.com/": "network_userinfo_forbidden",
            "https://example.com/path#fragment": "network_fragment_forbidden",
            "https://example.com/line\nbreak": "invalid_network_url",
            "https://example.com/a\\b": "invalid_network_url",
            "https://例子.测试/": "invalid_network_url",
            "https://xn--fsqu00a.xn--0zwm56d/": "network_idna2008_required",
        }
        for url, code in cases.items():
            with self.subTest(url=url):
                with self.assertRaises(PolicyError) as raised:
                    normalize_https_url(url)
                self.assertEqual(code, raised.exception.code)

    def test_dns_rejects_loopback_private_mapped_and_mixed_answer_sets(self) -> None:
        cases = {
            "loopback": ("127.0.0.1",),
            "private": ("10.0.0.1",),
            "mapped-loopback": ("::ffff:127.0.0.1",),
            "mixed": (PUBLIC_DNS_1, "192.168.1.4"),
        }
        for label, answers in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(PolicyError) as raised:
                    resolve_network_target("https://example.com/", _dns(*answers))
                self.assertEqual("non_global_network_address", raised.exception.code)

    def test_dns_reresolution_rejects_rebinding(self) -> None:
        answers = [PUBLIC_DNS_1]

        def resolver(_: str) -> tuple[str, ...]:
            return tuple(answers)

        approved = resolve_network_target(
            "https://example.com/api",
            resolver,
            via_proxy=True,
        )
        answers[:] = [PUBLIC_DNS_2]

        with self.assertRaises(PolicyError) as raised:
            resolve_network_target(
                "https://example.com/api",
                resolver,
                previous=approved,
                via_proxy=True,
            )
        self.assertEqual("dns_rebinding_detected", raised.exception.code)

    def test_cross_origin_redirect_cannot_receive_bound_credentials(self) -> None:
        current = resolve_network_target(
            "https://api.example.com/v1",
            _dns(PUBLIC_DNS_1),
            via_proxy=True,
        )
        scope = CredentialScope("api-token", (current.origin,), ())

        with self.assertRaises(PolicyError) as raised:
            resolve_redirect(
                current,
                "https://other.example/v2",
                _dns(PUBLIC_DNS_2),
                credential_scope=scope,
            )
        self.assertEqual("credential_redirect_forbidden", raised.exception.code)

    def test_network_disabled_and_direct_proxy_bypass_are_denied(self) -> None:
        direct = resolve_network_target(
            "https://example.com/api",
            _dns(PUBLIC_DNS_1),
            via_proxy=False,
        )
        action = _local_action(network_target=direct)
        rule = _allow_rule()

        disabled = PolicyEngine("policy-v1", (rule,)).evaluate(action)
        bypass = PolicyEngine(
            "policy-v1",
            (rule,),
            network_enabled=True,
            proxy_required=True,
            allowed_origins=(direct.origin,),
        ).evaluate(action)

        self.assertEqual(
            (Decision.DENY, "network_disabled"),
            (disabled.decision, disabled.code),
        )
        self.assertEqual((Decision.DENY, "proxy_required"), (bypass.decision, bypass.code))

    def test_network_enablement_requires_allowlist_and_mandatory_proxy(self) -> None:
        cases = (
            (
                {"network_enabled": True},
                "network_allowlist_required",
            ),
            (
                {"proxy_required": False},
                "network_proxy_required",
            ),
        )
        for arguments, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(PolicyError) as raised:
                    PolicyEngine("policy-v1", (_allow_rule(),), **arguments)
                self.assertEqual(code, raised.exception.code)


class ResourceAndDelegationPolicyTest(unittest.TestCase):
    def test_resource_budget_excess_is_rejected_before_policy_allow(self) -> None:
        limits = ResourceBudgetLimits(max_memory_bytes=128)
        request = ResourceRequest(memory_bytes=129)

        with self.assertRaises(PolicyError) as raised:
            preflight_resource_request(request, limits)
        self.assertEqual("resource_budget_exceeded", raised.exception.code)

        verdict = PolicyEngine(
            "policy-v1",
            (_allow_rule(),),
            resource_limits=limits,
        ).evaluate(_local_action(resource_request=request))
        self.assertIs(Decision.DENY, verdict.decision)
        self.assertEqual("resource_budget_exceeded", verdict.code)

    def test_mcp_capability_claim_cannot_expand_local_principal_scope(self) -> None:
        action = _local_action(
            kind=ActionKind.MCP_TOOL,
            principal=Principal("mcp-caller", ("read",)),
            mcp_claimed_capabilities=("write",),
            mcp_server_id="mcp-caller-server",
            mcp_session_generation=1,
            mcp_schema_hash="a1" * 32,
        )
        engine = PolicyEngine(
            "policy-v1",
            (
                PolicyRule(
                    "write-required",
                    Decision.ALLOW,
                    required_scopes=("write",),
                ),
            ),
        )

        verdict = engine.evaluate(action)

        self.assertIs(Decision.DENY, verdict.decision)
        self.assertEqual("denied_by_default", verdict.code)

    def test_mcp_credentials_are_bound_to_trusted_resolved_server_identity(self) -> None:
        scope = CredentialScope("mcp-token", (), ("trusted-server",))
        engine = PolicyEngine("policy-v1", (_allow_rule(),))
        trusted = _local_action(
            kind=ActionKind.MCP_TOOL,
            credential_scope=scope,
            mcp_server_id="trusted-server",
            mcp_session_generation=1,
            mcp_schema_hash="a1" * 32,
        )
        wrong = replace(trusted, mcp_server_id="untrusted-server")
        missing = replace(trusted, mcp_server_id=None)

        self.assertIs(Decision.ALLOW, engine.evaluate(trusted).decision)
        for action, code in (
            (wrong, "credential_scope_denied"),
            (missing, "credential_server_missing"),
        ):
            with self.subTest(code=code):
                verdict = engine.evaluate(action)
                self.assertIs(Decision.DENY, verdict.decision)
                self.assertEqual(code, verdict.code)
        self.assertNotEqual(trusted.action_digest, wrong.action_digest)

    def test_child_scope_is_exact_parent_request_intersection(self) -> None:
        parent = Principal("parent", ("read", "write"))

        child = narrow_child_principal(
            parent,
            child_principal_id="child",
            requested_scopes=("admin", "write"),
        )

        self.assertEqual(("write",), child.scopes)
        self.assertEqual("parent", child.parent_principal_id)
        self.assertNotIn("admin", child.scopes)


if __name__ == "__main__":
    unittest.main()
