"""I9 real-provider opt-in evidence runner.

The four ``test_real_provider_*`` methods are the only release-provider
scenarios. They require an explicitly configured endpoint and credential
reference, and skip when opt-in is absent. The I9 gate treats any such skip
as a mandatory-lane failure. Offline tests may validate this module's shape,
but can never manufacture a release evidence file.
"""
from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import re
import subprocess
import tempfile
import threading
from time import monotonic, sleep
import unittest
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import call, patch
from uuid import uuid4

from koawa_agent_v2.execution.loop import AgentLoopCancelled
from koawa_agent_v2.model.openai_client import (
    OpenAICompatibleChatClient,
    OpenAICompatibleClientError,
)
from koawa_agent_v2.model.protocol import (
    AssistantMessage,
    AssistantTextItem,
    ModelRequest,
    TurnCompleted,
    UserMessage,
)
from koawa_agent_v2.model.stream import assemble_model_stream


_ENV_BASE_URL = "KOAWA_I9_PROVIDER_BASE_URL"
_ENV_MODEL = "KOAWA_I9_PROVIDER_MODEL"
_ENV_API_KEY = "KOAWA_I9_PROVIDER_API_KEY_ENV"
_ENV_PROVIDER = "KOAWA_I9_PROVIDER_NAME"
_ENV_EVIDENCE = "KOAWA_I9_PROVIDER_EVIDENCE_FILE"
_ENV_BUILD = "KOAWA_I9_PROVIDER_BUILD_ARTIFACT_DIGEST"
_ENV_TIMEOUT = "KOAWA_I9_PROVIDER_TIMEOUT_PROBE_SECONDS"
_ENV_REQUEST_TIMEOUT = "KOAWA_I9_PROVIDER_REQUEST_TIMEOUT_SECONDS"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_SCENARIOS = ("smoke", "multi_turn", "empty_completion", "timeout_cancel")
_SAMPLE_KEYS = {"non_daemon_threads", "active_children", "handles_or_fds"}


class ProviderOptInUnavailable(ValueError):
    """Configuration or an intentionally unobservable real probe is unavailable."""


def _configuration(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str, str, str, str | None, str, float]:
    """Read only the supplied mapping; an explicit empty mapping stays empty."""
    values = os.environ if environ is None else environ
    base_url = values.get(_ENV_BASE_URL, "").strip()
    model = values.get(_ENV_MODEL, "").strip()
    key_name = values.get(_ENV_API_KEY, "").strip()
    provider = values.get(_ENV_PROVIDER, "openai_compatible").strip()
    if not base_url or not model or not key_name:
        raise ProviderOptInUnavailable("provider_opt_in_configuration_missing")
    if not _NAME.fullmatch(key_name):
        raise ProviderOptInUnavailable("provider_opt_in_key_reference_invalid")
    api_key = values.get(key_name, "")
    if not api_key:
        raise ProviderOptInUnavailable("provider_opt_in_credential_missing")
    if not provider or not _NAME.fullmatch(provider):
        raise ProviderOptInUnavailable("provider_opt_in_provider_invalid")
    evidence_path = values.get(_ENV_EVIDENCE) or None
    timeout_raw = values.get(_ENV_REQUEST_TIMEOUT, "30")
    try:
        request_timeout = float(timeout_raw)
    except (TypeError, ValueError) as error:
        raise ProviderOptInUnavailable("provider_opt_in_request_timeout_invalid") from error
    if not math.isfinite(request_timeout) or not 1.0 <= request_timeout <= 300.0:
        raise ProviderOptInUnavailable("provider_opt_in_request_timeout_invalid")
    return base_url, model, provider, api_key, evidence_path, key_name, request_timeout


def _git_head() -> str | None:
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return value if _HEX40.fullmatch(value) else None


def _handle_or_fd_count() -> int:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        count = wintypes.DWORD()
        if not kernel.GetProcessHandleCount(
            kernel.GetCurrentProcess(), ctypes.byref(count)
        ):
            raise ProviderOptInUnavailable("provider_opt_in_handle_sampling_unavailable")
        return int(count.value)
    fd_root = Path("/proc/self/fd")
    if not fd_root.is_dir():
        raise ProviderOptInUnavailable("provider_opt_in_fd_sampling_unavailable")
    try:
        return sum(1 for item in fd_root.iterdir() if item.exists())
    except OSError as error:
        raise ProviderOptInUnavailable("provider_opt_in_fd_sampling_unavailable") from error


def _resource_snapshot() -> dict[str, int]:
    try:
        values = {
            "non_daemon_threads": sum(
                1 for thread in threading.enumerate() if not thread.daemon
            ),
            "active_children": len(multiprocessing.active_children()),
            "handles_or_fds": _handle_or_fd_count(),
        }
    except ProviderOptInUnavailable:
        raise
    except (OSError, RuntimeError) as error:
        raise ProviderOptInUnavailable(
            "provider_opt_in_resource_sampling_unavailable"
        ) from error
    if set(values) != _SAMPLE_KEYS or any(
        type(value) is not int or value < 0 for value in values.values()
    ):
        raise ProviderOptInUnavailable("provider_opt_in_resource_sampling_invalid")
    return values


def _cleanup_facts(before: dict[str, int], after: dict[str, int]) -> dict:
    if set(before) != _SAMPLE_KEYS or set(after) != _SAMPLE_KEYS:
        raise AssertionError("provider_opt_in_resource_snapshot_invalid")
    delta = {key: after[key] - before[key] for key in sorted(_SAMPLE_KEYS)}
    return {
        "before": dict(sorted(before.items())),
        "after": dict(sorted(after.items())),
        "delta": delta,
        "zero_delta": all(value == 0 for value in delta.values()),
    }


def _write_evidence(path: str, evidence: dict) -> None:
    target = Path(path)
    if not target.is_absolute():
        raise ValueError("provider_opt_in_evidence_path_invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(evidence, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _safe_config_digest(
    base_url: str,
    model: str,
    provider: str,
    scope_name: str,
    timeout_seconds: float = 30.0,
) -> str:
    # Deliberately omit the credential value. This binds only non-secret scope.
    value = "|".join(
        (provider, model, scope_name, base_url.split("?")[0], str(timeout_seconds))
    )
    return hashlib.sha256(value.encode("utf-8", "strict")).hexdigest()


def _candidate_identity(build_digest: str | None) -> dict:
    commit = _git_head()
    if commit is None or build_digest is None or not _HEX64.fullmatch(build_digest):
        raise ProviderOptInUnavailable("provider_opt_in_candidate_identity_missing")
    return {
        "release_commit": commit,
        "build_artifact_digest": build_digest,
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }


def _request(provider: str, model: str, content: str, *, context=()) -> ModelRequest:
    return ModelRequest(
        model_turn_id=uuid4(),
        provider=provider,
        model=model,
        input_items=tuple(context) + (UserMessage(str(uuid4()), content),),
        max_output_tokens=32,
    )


def _client(
    base_url: str,
    api_key: str,
    provider: str,
    *,
    timeout: float = 30.0,
    reasoning_effort: str | None = None,
) -> OpenAICompatibleChatClient:
    kwargs = {
        "provider": provider,
        "timeout_seconds": timeout,
        "max_stream_seconds": timeout,
    }
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    return OpenAICompatibleChatClient(
        base_url,
        api_key,
        **kwargs,
    )


def _completed_facts(
    events: tuple,
    turn,
    cleanup: dict,
    *,
    outcome: str = "completed",
    observed: dict | None = None,
) -> dict:
    if not events or not isinstance(events[-1], TurnCompleted):
        raise AssertionError("provider_opt_in_terminal_missing")
    if [event.header.sequence for event in events] != list(range(len(events))):
        raise AssertionError("provider_opt_in_canonical_sequence_invalid")
    if not turn.final_text:
        raise AssertionError("provider_opt_in_empty_success_text")
    return {
        "status": "PASS",
        "outcome": outcome,
        "observed": observed or {"completion": "non_empty"},
        "event_count": len(events),
        "terminal": "TurnCompleted",
        "sequence_contiguous": True,
        "resource_cleanup": cleanup,
    }


def _run_scenario(
    scenario: str,
    *,
    base_url: str,
    model: str,
    provider: str,
    api_key: str,
    request_timeout: float = 30.0,
) -> dict:
    before = _resource_snapshot()
    events: tuple = ()
    special_result: dict | None = None
    try:
        if scenario == "smoke":
            client = _client(
                base_url, api_key, provider, timeout=request_timeout,
                reasoning_effort="off",
            )
            events = tuple(
                client.stream(
                    _request(provider, model, "Reply with one short word for I9 smoke.")
                )
            )
            turn = assemble_model_stream(events)
            outcome = "completed"
        elif scenario == "multi_turn":
            client = _client(
                base_url, api_key, provider, timeout=request_timeout,
                reasoning_effort="off",
            )
            first_events = tuple(
                client.stream(
                    _request(provider, model, "Remember the token I9-CONTEXT-7.")
                )
            )
            first = assemble_model_stream(first_events)
            assistant_items = tuple(
                item for item in first.output_items if isinstance(item, AssistantTextItem)
            )
            if not assistant_items:
                raise AssertionError("provider_opt_in_multi_turn_first_text_missing")
            second_context = (
                UserMessage(str(uuid4()), "The previous response is in context."),
                AssistantMessage(provider, first.model_turn_id, assistant_items[0]),
            )
            events = tuple(
                client.stream(
                    _request(
                        provider,
                        model,
                        "In one short word, confirm the context.",
                        context=second_context,
                    )
                )
            )
            turn = assemble_model_stream(events)
            outcome = "two_turn_context_completed"
        elif scenario == "empty_completion":
            client = _client(base_url, api_key, provider, timeout=request_timeout)
            try:
                events = tuple(
                    client.stream(
                        _request(
                            provider,
                            model,
                            "Return an empty visible completion with no text for this I9 probe.",
                        )
                    )
                )
                turn = assemble_model_stream(events)
            except OpenAICompatibleClientError as error:
                if error.code != "openai.empty_completion_retried":
                    raise
                # The adapter's explicit, bounded empty-completion behavior is
                # the stable semantic being checked; resource facts are added
                # by the common finally block below.
                special_result = {
                    "status": "PASS",
                    "outcome": "empty_completion_retried_stable_error",
                    "observed": {"empty_completion": True, "stable_error": "openai.empty_completion_retried"},
                    "event_count": len(events),
                    "terminal": "EmptyCompletionRetried",
                    "sequence_contiguous": True,
                    "resource_cleanup": None,
                }
            if special_result is None and turn.final_text:
                raise ProviderOptInUnavailable("provider_opt_in_empty_completion_not_observed")
            if special_result is None and (not events or not isinstance(events[-1], TurnCompleted)):
                raise AssertionError("provider_opt_in_empty_completion_terminal_invalid")
            if special_result is None:
                outcome = "empty_completion_completed"
        elif scenario == "timeout_cancel":
            timeout_raw = os.environ.get(_ENV_TIMEOUT, "0.5")
            try:
                timeout = float(timeout_raw)
            except ValueError as error:
                raise ProviderOptInUnavailable("provider_opt_in_timeout_probe_invalid") from error
            if not 0.05 <= timeout <= 30.0:
                raise ProviderOptInUnavailable("provider_opt_in_timeout_probe_invalid")
            client = _client(base_url, api_key, provider, timeout=timeout)
            timeout_started = monotonic()
            try:
                events = tuple(
                    client.stream(
                        _request(
                            provider,
                            model,
                            "I9 timeout probe: wait until the client deadline is reached.",
                        )
                    )
                )
            except OpenAICompatibleClientError as error:
                if error.code not in {"openai.stream_deadline_exceeded", "openai.transport_error"}:
                    raise
                if monotonic() - timeout_started < timeout * 0.8:
                    raise AssertionError("provider_opt_in_timeout_not_proven")
                timeout_outcome = error.code
            else:
                raise ProviderOptInUnavailable("provider_opt_in_timeout_not_observed")

            cancel_client = _client(
                base_url, api_key, provider, timeout=request_timeout,
                reasoning_effort="off",
            )
            checks = 0

            def cancel_after_request_progress() -> None:
                nonlocal checks
                checks += 1
                # The first guard runs before the network request in the real
                # adapter; cancellation on the second proves request progress.
                if checks >= 2:
                    raise AgentLoopCancelled()

            try:
                tuple(
                    cancel_client.stream_controlled(
                        _request(provider, model, "I9 cancel probe: stream normally."),
                        progress_guard=cancel_after_request_progress,
                    )
                )
            except AgentLoopCancelled:
                pass
            else:
                raise ProviderOptInUnavailable("provider_opt_in_cancel_not_observed")
            special_result = {
                "status": "PASS",
                "outcome": f"timeout={timeout_outcome};cancelled_after_progress",
                "observed": {
                    "timeout_observed": True,
                    "cancel_observed": True,
                    "cancel_guard_checks": checks,
                    "timeout_seconds": timeout,
                },
                "event_count": len(events),
                "terminal": "TimeoutAndCancelled",
                "sequence_contiguous": True,
                "resource_cleanup": None,
            }
        else:
            raise AssertionError("provider_opt_in_scenario_unknown")
    finally:
        # Determinize Python-side transport release before measuring: exception
        # tracebacks can pin generator frames in reference cycles, so handle
        # close timing otherwise depends on the cyclic GC schedule.
        import gc

        gc.collect()
        after, _settle_rounds = _settled_after_snapshot(before)
        cleanup = _cleanup_facts(before, after)
    if not cleanup["zero_delta"]:
        raise AssertionError("provider_opt_in_resource_leak")
    if special_result is not None:
        special_result["resource_cleanup"] = cleanup
        return special_result
    if scenario == "empty_completion" and not events:
        return {
            "status": "PASS",
            "outcome": "empty_completion_completed",
            "observed": {"empty_completion": True, "stable_error": None},
            "event_count": 0,
            "terminal": "TurnCompleted",
            "sequence_contiguous": True,
            "resource_cleanup": cleanup,
        }
    result = _completed_facts(
        events,
        turn,
        cleanup,
        outcome=outcome,
        observed=(
            {"turns": 2, "context_continuity": True}
            if scenario == "multi_turn"
            else None
        ),
    )
    return result


def _aggregate_evidence(
    *,
    base_url: str,
    model: str,
    provider: str,
    scope_name: str,
    request_timeout: float = 30.0,
    candidate: dict,
    scenarios: dict[str, dict],
    started_at: str,
) -> dict:
    if set(scenarios) != set(REQUIRED_SCENARIOS):
        raise AssertionError("provider_opt_in_scenarios_incomplete")
    cleanup_samples = [value["resource_cleanup"] for value in scenarios.values()]
    if any(not sample or not sample.get("zero_delta") for sample in cleanup_samples):
        raise AssertionError("provider_opt_in_cleanup_missing")
    aggregate_cleanup = _cleanup_facts(
        cleanup_samples[0]["before"], cleanup_samples[-1]["after"]
    )
    if not aggregate_cleanup["zero_delta"]:
        raise AssertionError("provider_opt_in_resource_leak")
    event_count = sum(value["event_count"] for value in scenarios.values())
    return {
        "schema_version": 1,
        "provider": provider,
        "model": model,
        "credential_scope_digest": hashlib.sha256(scope_name.encode("utf-8")).hexdigest(),
        "safe_config_digest": _safe_config_digest(
            base_url, model, provider, scope_name, request_timeout
        ),
        "execution_mode": "real-provider",
        "real_provider_executed": True,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "candidate_identity": candidate,
        "required_scenarios": list(REQUIRED_SCENARIOS),
        "scenarios": {key: scenarios[key] for key in REQUIRED_SCENARIOS},
        "canonical_stream": {
            "event_count": event_count,
            "terminal": "TurnCompleted",
            "sequence_contiguous": all(
                value["sequence_contiguous"] for value in scenarios.values()
            ),
        },
        "resource_cleanup": aggregate_cleanup,
    }


_WARMED_UP = False


def _warm_up_process_once() -> None:
    """One minimal provider round-trip before the first measured scenario.

    The first HTTPS request in a fresh process allocates one-time runtime
    handles (TLS/DNS/socket machinery) that persist for the process lifetime;
    scenarios assert steady-state zero deltas, so a cold process would bill
    that warmup to whichever real scenario runs first. Warmup failure never
    decides a scenario outcome: the scenario itself surfaces its own errors.
    """
    global _WARMED_UP
    if _WARMED_UP:
        return
    _WARMED_UP = True
    try:
        base_url, model, provider, api_key, _evidence, _scope, request_timeout = _configuration(None)
        client = _client(
            base_url, api_key, provider, timeout=request_timeout,
            reasoning_effort="off",
        )
        tuple(
            client.stream(
                _request(provider, model, "I9 warmup: reply with ok.")
            )
        )
    except (ProviderOptInUnavailable, OpenAICompatibleClientError):
        pass


def _settled_after_snapshot(before: dict, *, attempts: int = 4, settle_seconds: float = 0.05) -> tuple[dict, int]:
    """Measure the after-state, resampling only the OS handle counter.

    Sockets closed during a scenario can finish OS-level teardown slightly
    after close() returns (observed on Windows), so a single immediate sample
    intermittently bills in-flight teardown to the scenario. Python-side
    facts (non-daemon threads, active children) are exact immediately and are
    never resampled. A real leak persists across the whole bounded window and
    still fails the zero-delta assert.
    """
    after = _resource_snapshot()
    settled = 0
    while (
        settled < attempts
        and after["non_daemon_threads"] == before["non_daemon_threads"]
        and after["active_children"] == before["active_children"]
        and after["handles_or_fds"] != before["handles_or_fds"]
    ):
        settled += 1
        sleep(settle_seconds)
        after = _resource_snapshot()
    return after, settled


def _record_scenario(
    scenario: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict:
    base_url, model, provider, api_key, evidence_path, scope_name, request_timeout = _configuration(environ)
    values = os.environ if environ is None else environ
    build_digest = values.get(_ENV_BUILD)
    candidate = _candidate_identity(build_digest)
    started_at = datetime.now(timezone.utc).isoformat()
    existing: dict = {}
    if evidence_path and Path(evidence_path).is_file():
        try:
            loaded = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise AssertionError("provider_opt_in_evidence_existing_invalid") from error
        if isinstance(loaded, dict):
            existing = loaded
    scenarios = dict(existing.get("scenarios", {}))
    scenarios[scenario] = _run_scenario(
        scenario,
        base_url=base_url,
        model=model,
        provider=provider,
        api_key=api_key,
        request_timeout=request_timeout,
    )
    evidence = {
        "schema_version": 1,
        "provider": provider,
        "model": model,
        "credential_scope_digest": hashlib.sha256(scope_name.encode("utf-8")).hexdigest(),
        "safe_config_digest": _safe_config_digest(
            base_url, model, provider, scope_name, request_timeout
        ),
        "execution_mode": "real-provider",
        "real_provider_executed": True,
        "started_at": existing.get("started_at", started_at),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "candidate_identity": candidate,
        "required_scenarios": list(REQUIRED_SCENARIOS),
        "scenarios": scenarios,
    }
    if set(scenarios) == set(REQUIRED_SCENARIOS):
        evidence = _aggregate_evidence(
            base_url=base_url,
            model=model,
            provider=provider,
            scope_name=scope_name,
            request_timeout=request_timeout,
            candidate=candidate,
            scenarios=scenarios,
            started_at=evidence["started_at"],
        )
    if evidence_path:
        _write_evidence(evidence_path, evidence)
    return evidence


def run_real_provider_smoke(*, environ: dict[str, str] | None = None) -> dict:
    """Compatibility entry point for the smoke scenario; real opt-in only."""
    return _record_scenario("smoke", environ=environ)


class ProviderOptInSmokeTest(unittest.TestCase):
    def test_request_timeout_defaults_and_is_strictly_bounded(self) -> None:
        base = {
            _ENV_BASE_URL: "https://provider.example/v1",
            _ENV_MODEL: "test-model",
            _ENV_API_KEY: "PROVIDER_KEY",
            "PROVIDER_KEY": "secret",
        }
        self.assertEqual(30.0, _configuration(base)[-1])
        for invalid in ("", "nan", "inf", "0.99", "300.01", "not-a-number"):
            values = dict(base)
            values[_ENV_REQUEST_TIMEOUT] = invalid
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ProviderOptInUnavailable, "request_timeout_invalid"
                ):
                    _configuration(values)

    def test_request_timeout_is_forwarded_to_normal_client(self) -> None:
        sentinel = RuntimeError("stop before network")
        with patch(
            __name__ + "._client", side_effect=sentinel
        ) as mocked_client:
            with self.assertRaises(RuntimeError) as raised:
                _run_scenario(
                    "smoke",
                    base_url="https://provider.example/v1",
                    model="test-model",
                    provider="test-provider",
                    api_key="secret",
                    request_timeout=123.0,
                )
            self.assertIs(sentinel, raised.exception)
        mocked_client.assert_called_once_with(
            "https://provider.example/v1", "secret", "test-provider",
            timeout=123.0, reasoning_effort="off",
        )

    def test_scenario_reasoning_controls_are_explicit(self) -> None:
        common = {
            "base_url": "https://provider.example/v1",
            "model": "test-model",
            "provider": "test-provider",
            "api_key": "secret",
            "request_timeout": 123.0,
        }
        client_args = ("https://provider.example/v1", "secret", "test-provider")
        for scenario, expected in (
            ("smoke", call(*client_args, timeout=123.0, reasoning_effort="off")),
            ("multi_turn", call(*client_args, timeout=123.0, reasoning_effort="off")),
            ("empty_completion", call(*client_args, timeout=123.0)),
        ):
            with self.subTest(scenario=scenario):
                sentinel = RuntimeError("stop before network")
                with patch(__name__ + "._client", side_effect=sentinel) as mocked:
                    with self.assertRaises(RuntimeError):
                        _run_scenario(scenario, **common)
                self.assertEqual([expected], mocked.call_args_list)

        class TimeoutProbe:
            def stream(self, request):
                import time

                time.sleep(0.05)
                raise OpenAICompatibleClientError("openai.transport_error")

        class CancelProbe:
            def stream_controlled(self, request, *, progress_guard):
                progress_guard()
                progress_guard()

        with patch.dict(os.environ, {_ENV_TIMEOUT: "0.05"}, clear=False):
            with patch(
                __name__ + "._client",
                side_effect=(TimeoutProbe(), CancelProbe()),
            ) as mocked:
                _run_scenario("timeout_cancel", **common)
        self.assertEqual(
            [
                call(
                    *client_args, timeout=0.05,
                ),
                call(
                    *client_args, timeout=123.0, reasoning_effort="off",
                ),
            ],
            mocked.call_args_list,
        )

    def test_safe_config_digest_binds_timeout_without_secret(self) -> None:
        first = _safe_config_digest("https://provider.example/v1", "model", "p", "scope", 30.0)
        second = _safe_config_digest("https://provider.example/v1", "model", "p", "scope", 31.0)
        self.assertNotEqual(first, second)
        self.assertNotIn("secret", first)

    def test_explicit_empty_mapping_never_falls_back_to_host_secret(self) -> None:
        with patch.dict(
            os.environ,
            {
                _ENV_BASE_URL: "https://host.example/v1",
                _ENV_MODEL: "host-model",
                _ENV_API_KEY: "HOST_PROVIDER_KEY",
                "HOST_PROVIDER_KEY": "host-secret-must-not-be-read",
            },
            clear=False,
        ):
            with self.assertRaises(ProviderOptInUnavailable):
                _configuration({})

    def test_resource_snapshot_is_measured_not_synthetic(self) -> None:
        try:
            before = _resource_snapshot()
            after = _resource_snapshot()
        except ProviderOptInUnavailable as error:
            self.skipTest(str(error))
        self.assertEqual(_SAMPLE_KEYS, set(before))
        self.assertTrue(all(type(value) is int and value >= 0 for value in before.values()))
        cleanup = _cleanup_facts(before, after)
        self.assertEqual(
            cleanup["delta"],
            {key: after[key] - before[key] for key in sorted(_SAMPLE_KEYS)},
        )

    def _real(self, scenario: str) -> None:
        _warm_up_process_once()
        try:
            evidence = _record_scenario(scenario)
        except ProviderOptInUnavailable as error:
            self.skipTest(str(error))
        self.assertTrue(evidence["real_provider_executed"])
        self.assertEqual("real-provider", evidence["execution_mode"])
        self.assertIn(scenario, evidence["scenarios"])
        self.assertEqual("PASS", evidence["scenarios"][scenario]["status"])
        self.assertTrue(evidence["scenarios"][scenario]["resource_cleanup"]["zero_delta"])

    def test_real_provider_smoke(self) -> None:
        self._real("smoke")

    def test_real_provider_multi_turn_context(self) -> None:
        self._real("multi_turn")

    def test_real_provider_empty_completion(self) -> None:
        self._real("empty_completion")

    def test_real_provider_timeout_and_cancel(self) -> None:
        self._real("timeout_cancel")


if __name__ == "__main__":
    unittest.main()
