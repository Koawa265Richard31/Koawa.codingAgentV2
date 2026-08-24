"""I1 Stage F: deterministic contract tests for the shared minimal subprocess
  environment builder (docs/v2-stabilization-detailed-implementation.md 3.4).

Every test is deterministic: no subprocess spawn, no network, no Docker, no
sleep. The private temp directory comes from a TemporaryDirectory.  The POSIX
branch and the reserved/injection deny paths are exercised by patching
os.name so the same assertions run unchanged on every host.
"""

from __future__ import annotations

import ctypes
import os
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from koawa_agent_v2.runtime.subprocess_env import (
    SubprocessEnvError,
    build_minimal_environment,
)

_FIXED_PYTHON_ENV = {
    "PYTHONHASHSEED": "0",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONDONTWRITEBYTECODE": "1",
}

# 3.4: refuse secret/token/password/api-key/authorization-shaped names.
_SECRET_SHAPED_NAMES = (
    "API_KEY",
    "DB_PASSWORD",
    "ACCESS_TOKEN",
    "AUTHORIZATION",
    "PRIVATE_KEY",
    "CLIENT_CREDENTIALS",
    "X-API-KEY",
    "SIGNATURE",
)

# 3.4: loader/code-injection vectors are hard-denied for everyone.
_INJECTION_VARIABLES = (
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "PYTHONPLUGLIBDIR",
    "BASH_ENV",
    "ENV",
    "PERL5LIB",
    "RUBYOPT",
    "NODE_OPTIONS",
)

# 3.4: names owned by the builder must never be overridden by explicit input.
_RESERVED_NAMES = (
    "LANG",
    "LC_ALL",
    "PYTHONHASHSEED",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "PYTHONDONTWRITEBYTECODE",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
)


def _build(
    explicit: dict[str, str],
    *,
    allowed: tuple[str, ...],
    private_temp: Path,
):
    return build_minimal_environment(
        explicit,
        allowed_names=frozenset(allowed),
        private_temp=private_temp,
    )


class MinimalEnvironmentContractTest(unittest.TestCase):
    """Deterministic tests for build_minimal_environment (3.4)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.private_temp = Path(self.temporary.name).resolve()

    def test_fixed_python_runtime_variables_always_present(self) -> None:
        # 3.4: fixed PYTHONHASHSEED/PYTHONUTF8/PYTHONIOENCODING/
        # PYTHONDONTWRITEBYTECODE are added and never copied from the parent.
        env = _build({}, allowed=(), private_temp=self.private_temp)
        for name, value in _FIXED_PYTHON_ENV.items():
            self.assertEqual(value, env[name])

    def test_parent_environment_is_never_inherited(self) -> None:
        # 3.4: HOME/USERPROFILE/APPDATA/PATH/SSH/Git creds/cloud and provider
        # keys are not copied; the child environment is built from scratch.
        parent_canaries = {
            "PATH": "C:\\FAKE_PATH",
            "HOME": "C:\\FAKE_HOME",
            "USERPROFILE": "C:\\FAKE_USERPROFILE",
            "APPDATA": "C:\\FAKE_APPDATA",
            "SSH_AUTH_SOCK": "C:\\FAKE_SSH",
            "GIT_ASKPASS": "C:\\FAKE_GIT",
            "AWS_ACCESS_KEY_ID": "AKIAFAKE",
            "OPENAI_API_KEY": "sk-fake",
            "AZURE_OPENAI_KEY": "az-fake",
            "PYTHONPATH": "C:\\FAKE_PYTHONPATH",
        }
        with patch.dict(os.environ, parent_canaries):
            env = _build({}, allowed=(), private_temp=self.private_temp)
        for name in parent_canaries:
            self.assertNotIn(name, env, name)

    @unittest.skipUnless(os.name == "nt", "Windows system-directory resolution (3.4)")
    def test_windows_system_directories_resolved_from_os_not_parent(self) -> None:
        # 3.4: Windows platform variables are resolved from the OS via
        # GetSystemWindowsDirectoryW, never copied from the parent environment.
        fake_parent = {
            "SYSTEMROOT": "C:\\FAKE_ROOT",
            "WINDIR": "C:\\FAKE_WINDIR",
            "COMSPEC": "C:\\FAKE\\cmd.exe",
            "TEMP": "C:\\FAKE_TEMP",
            "TMP": "C:\\FAKE_TMP",
        }
        kernel32 = ctypes.windll.kernel32
        buffer = ctypes.create_unicode_buffer(512)
        length = kernel32.GetSystemWindowsDirectoryW(buffer, 512)
        self.assertTrue(1 <= length < 512)  # probe the OS value used below
        expected_root = str(Path(buffer.value).resolve(strict=False))
        with patch.dict(os.environ, fake_parent):
            env = _build({}, allowed=(), private_temp=self.private_temp)
        self.assertNotEqual(fake_parent["SYSTEMROOT"], env["SystemRoot"])
        self.assertEqual(expected_root, env["SystemRoot"])
        self.assertEqual(env["SystemRoot"], env["WINDIR"])
        self.assertEqual(
            str(Path(env["SystemRoot"]) / "System32" / "cmd.exe"), env["COMSPEC"]
        )
        self.assertNotIn("PATHEXT", env)
        # 3.4: TEMP/TMP point only at the controller-created private directory.
        self.assertEqual(str(self.private_temp), env["TEMP"])
        self.assertEqual(str(self.private_temp), env["TMP"])

    def test_posix_branch_uses_fixed_locale_and_private_tmpdir(self) -> None:
        # 3.4: POSIX gets fixed LANG=C/LC_ALL=C and the private TMPDIR only.
        with patch.object(os, "name", "posix"):
            env = _build({}, allowed=(), private_temp=self.private_temp)
        self.assertEqual("C", env["LANG"])
        self.assertEqual("C", env["LC_ALL"])
        self.assertEqual(str(self.private_temp), env["TMPDIR"])
        for name in ("SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP"):
            self.assertNotIn(name, env)
        for name, value in _FIXED_PYTHON_ENV.items():
            self.assertEqual(value, env[name])

    def test_returned_mapping_is_an_immutable_proxy(self) -> None:
        # 3.4: output is an immutable mapping.
        env = _build({"FOO": "a"}, allowed=("FOO",), private_temp=self.private_temp)
        self.assertIsInstance(env, MappingProxyType)
        with self.assertRaises(TypeError):
            env["FOO"] = "z"
        with self.assertRaises(TypeError):
            env["NEW"] = "x"

    def test_secret_shaped_names_are_forbidden(self) -> None:
        # 3.4: secret/token/password/api-key/authorization shapes are refused.
        for name in _SECRET_SHAPED_NAMES:
            with self.subTest(name=name):
                with self.assertRaises(SubprocessEnvError) as raised:
                    _build({name: "x"}, allowed=(name,), private_temp=self.private_temp)
                self.assertEqual("secret_variable_forbidden", raised.exception.code)

    def test_injection_variables_are_forbidden(self) -> None:
        # 3.4: loader/code-injection variables are hard-denied for everyone.
        # The deny set stores canonical uppercase names and is compared against
        # the (case-sensitive) POSIX key; the Windows casefold mismatch is
        # recorded as a contract gap in the I1 Stage F handoff.
        with patch.object(os, "name", "posix"):
            for name in _INJECTION_VARIABLES:
                with self.subTest(name=name):
                    with self.assertRaises(SubprocessEnvError) as raised:
                        _build({name: "x"}, allowed=(name,), private_temp=self.private_temp)
                    self.assertEqual("injection_variable_forbidden", raised.exception.code)

    def test_reserved_owned_variables_cannot_be_overridden(self) -> None:
        # 3.4: owned names (fixed Python env, platform vars, locale, temp)
        # must not be overridden by explicit input.  Exercised through the
        # POSIX branch for the same reason as the injection test above.
        with patch.object(os, "name", "posix"):
            for name in _RESERVED_NAMES:
                with self.subTest(name=name):
                    with self.assertRaises(SubprocessEnvError) as raised:
                        _build({name: "override"}, allowed=(name,), private_temp=self.private_temp)
                    self.assertEqual("reserved_variable_overridden", raised.exception.code)

    def test_not_allowlisted_explicit_name_is_rejected(self) -> None:
        # 3.4: only caller-allowlisted explicit names pass.
        with self.assertRaises(SubprocessEnvError) as raised:
            _build({"OTHER": "v"}, allowed=("FOO",), private_temp=self.private_temp)
        self.assertEqual("environment_not_allowlisted", raised.exception.code)

    def test_empty_or_non_string_names_are_rejected(self) -> None:
        # Stable code for a malformed explicit name.  The non-string-name case
        # is exercised through the POSIX branch: on Windows the current code
        # raises AttributeError instead of the stable code (handoff finding).
        with self.assertRaises(SubprocessEnvError) as raised:
            _build({"": "v"}, allowed=("FOO",), private_temp=self.private_temp)
        self.assertEqual("invalid_environment_name", raised.exception.code)
        with patch.object(os, "name", "posix"):
            with self.assertRaises(SubprocessEnvError) as raised:
                _build({1: "v"}, allowed=("FOO",), private_temp=self.private_temp)
            self.assertEqual("invalid_environment_name", raised.exception.code)

    def test_invalid_environment_values_are_rejected(self) -> None:
        # 3.4: values must be str, NUL-free, and bounded in length.
        for value in (None, 42, "with\x00nul", "x" * 16_385):
            with self.subTest(value=repr(value)):
                with self.assertRaises(SubprocessEnvError) as raised:
                    _build({"FOO": value}, allowed=("FOO",), private_temp=self.private_temp)
                self.assertEqual("invalid_environment_value", raised.exception.code)

    @unittest.skipUnless(os.name == "nt", "Windows casefolded explicit keys (3.4)")
    def test_windows_casefolded_duplicate_keys_appear_once(self) -> None:
        # 3.4: explicit keys are casefold-deduplicated on Windows; a second
        # case-variant fails closed so the key appears at most once.
        with self.assertRaises(SubprocessEnvError) as raised:
            _build({"FOO": "a", "foo": "b"}, allowed=("FOO",), private_temp=self.private_temp)
        self.assertEqual("environment_not_allowlisted", raised.exception.code)
        env = _build({"Foo": "x"}, allowed=("Foo",), private_temp=self.private_temp)
        self.assertEqual("x", env["Foo"])
        matching = [key for key in env if key.casefold() == "foo"]
        self.assertEqual(1, len(matching))

    def test_allowlisted_explicit_entries_are_injected_as_is(self) -> None:
        # 3.4: allowed explicit entries are injected with their original casing.
        env = _build(
            {"APP_DEBUG": "1", "MODEL_DIR": "C:\\models"},
            allowed=("APP_DEBUG", "MODEL_DIR"),
            private_temp=self.private_temp,
        )
        self.assertEqual("1", env["APP_DEBUG"])
        self.assertEqual("C:\\models", env["MODEL_DIR"])

    def test_input_contract_type_errors(self) -> None:
        # Builder input contract: Mapping explicit, frozenset of non-empty str
        # allowed_names, absolute Path private_temp.
        with self.assertRaises(TypeError):
            build_minimal_environment(
                [("FOO", "a")],
                allowed_names=frozenset({"FOO"}),
                private_temp=self.private_temp,
            )
        with self.assertRaises(TypeError):
            build_minimal_environment(
                {}, allowed_names={"FOO"}, private_temp=self.private_temp
            )
        with self.assertRaises(TypeError):
            build_minimal_environment(
                {}, allowed_names=frozenset({""}), private_temp=self.private_temp
            )
        with self.assertRaises(TypeError):
            build_minimal_environment(
                {}, allowed_names=frozenset({"FOO"}), private_temp=str(self.private_temp)
            )
        with self.assertRaises(TypeError):
            build_minimal_environment(
                {}, allowed_names=frozenset({"FOO"}), private_temp=Path("relative")
            )

    def test_error_codes_are_stable_identifiers(self) -> None:
        # Stable content-free error codes (content-free failure contract).
        error = SubprocessEnvError("secret_variable_forbidden")
        self.assertEqual("secret_variable_forbidden", error.code)
        self.assertEqual("secret_variable_forbidden", str(error))
        with self.assertRaises(ValueError):
            SubprocessEnvError("Not A Code!")


class WindowsCasefoldRegressionTest(unittest.TestCase):
    """集成者回归：Windows 真实平台上 deny/owned 集合的 casefold 防护。

    修复记录：原先 key=name.casefold() 与仅存大写规范名的集合比对不匹配，
    导致 Windows 上保留名可被覆盖、注入变量可进入子进程环境（F 阶段审计
    发现，已修复为与 casefold 全集比较）。本机即 Windows，用例真实执行。
    """

    def _build(self, explicit: dict[str, str]) -> object:
        with tempfile.TemporaryDirectory() as private:
            return build_minimal_environment(
                explicit,
                allowed_names=frozenset(explicit),
                private_temp=Path(private),
            )

    def test_windows_reserved_names_cannot_be_overridden(self) -> None:
        for name in ("TEMP", "tmp", "PYTHONHASHSEED", "pythonhashseed", "SystemRoot", "systemroot"):
            with self.subTest(name=name):
                with self.assertRaises(SubprocessEnvError) as raised:
                    self._build({name: "evil"})
                self.assertEqual("reserved_variable_overridden", raised.exception.code)

    def test_windows_injection_names_are_denied(self) -> None:
        for name in ("LD_PRELOAD", "ld_preload", "PYTHONSTARTUP", "pythonstartup", "NODE_OPTIONS"):
            with self.subTest(name=name):
                with self.assertRaises(SubprocessEnvError) as raised:
                    self._build({name: "/tmp/shell.sh"})
                self.assertEqual("injection_variable_forbidden", raised.exception.code)

    def test_windows_non_string_name_yields_stable_code(self) -> None:
        with self.assertRaises(SubprocessEnvError) as raised:
            build_minimal_environment(
                {123: "x"},
                allowed_names=frozenset({"TZ"}),
                private_temp=Path(tempfile.gettempdir()),
            )
        self.assertEqual("invalid_environment_name", raised.exception.code)

if __name__ == "__main__":
    unittest.main()
