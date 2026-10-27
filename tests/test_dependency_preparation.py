"""C1 acceptance (WP-C): controlled dependency preparation decisions.

The planner never installs; it classifies each request against the
maintainer-authorised scope and returns a fixed requires-authorization state
outside it - never a silent fallback to whole-repo mounts, host execution or
open network.
"""
from __future__ import annotations

import unittest

from koawa_agent_v2.verification.dependency_preparation import (
    PREP_READY,
    PREP_REFUSED,
    PREP_REQUIRES_AUTHORIZATION,
    DependencyPreparationError,
    DependencyScope,
    plan_preparation,
)


class DependencyScopeTest(unittest.TestCase):
    def test_scope_validation(self) -> None:
        with self.assertRaises(DependencyPreparationError):
            DependencyScope(allowed_packages=("ok",), index_urls=("http://insecure",))
        with self.assertRaises(DependencyPreparationError):
            DependencyScope(max_packages=0)


class PreparationDecisionTest(unittest.TestCase):
    def test_install_disabled_requires_authorization(self) -> None:
        decision = plan_preparation(
            ("pytest",), DependencyScope(allowed_packages=("pytest",))
        )
        self.assertEqual(PREP_REQUIRES_AUTHORIZATION, decision.status)
        self.assertEqual("install_disabled", decision.reason)
        self.assertEqual((), decision.install)

    def test_out_of_scope_requires_authorization(self) -> None:
        scope = DependencyScope(allowed_packages=("pytest",), allow_install=True)
        decision = plan_preparation(("pytest", "requests"), scope)
        self.assertEqual(PREP_REQUIRES_AUTHORIZATION, decision.status)
        self.assertEqual("out_of_scope", decision.reason)
        self.assertEqual(("requests",), decision.requires_authorization)
        # Nothing is installed when any part is out of scope.
        self.assertEqual((), decision.install)

    def test_in_scope_is_ready(self) -> None:
        scope = DependencyScope(
            allowed_packages=("pytest", "hypothesis"), allow_install=True,
        )
        decision = plan_preparation(("pytest", "hypothesis"), scope)
        self.assertEqual(PREP_READY, decision.status)
        self.assertEqual(("pytest", "hypothesis"), decision.install)

    def test_prefix_scope(self) -> None:
        scope = DependencyScope(allowed_packages=("pytest-*",), allow_install=True)
        decision = plan_preparation(("pytest-cov", "pytest-xdist"), scope)
        self.assertEqual(PREP_READY, decision.status)
        self.assertEqual(2, len(decision.install))

    def test_only_missing_packages_are_installed(self) -> None:
        scope = DependencyScope(allowed_packages=("pytest",), allow_install=True)
        decision = plan_preparation(
            ("pytest",), scope, missing=(),
        )
        self.assertEqual(PREP_READY, decision.status)
        self.assertEqual((), decision.install)

    def test_budget_exceeded_requires_authorization(self) -> None:
        scope = DependencyScope(
            allowed_packages=("a", "b", "c"), allow_install=True, max_packages=2,
        )
        decision = plan_preparation(("a", "b", "c"), scope)
        self.assertEqual(PREP_REQUIRES_AUTHORIZATION, decision.status)
        self.assertEqual("budget_exceeded", decision.reason)

    def test_malformed_name_is_refused(self) -> None:
        scope = DependencyScope(allowed_packages=("pytest",), allow_install=True)
        for name in ("--index-url=evil", "../../etc", "pkg;rm -rf /", ""):
            decision = plan_preparation((name,), scope)
            self.assertEqual(PREP_REFUSED, decision.status, name)
            self.assertEqual("invalid_package_name", decision.reason)


if __name__ == "__main__":
    unittest.main()
