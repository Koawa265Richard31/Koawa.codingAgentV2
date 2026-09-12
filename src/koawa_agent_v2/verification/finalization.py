"""D5 修改/测试/Git 证据账本与最终完成闸门。

这是进程内验证账本，不是 D7 的 durable side-effect ledger。它只解决 D5 的
一个问题：没有当前修改版本对应的通过测试和 Git 证据，模型就不能把 Turn 标成
成功。D6/D7 会把恢复与副作用语义持久化。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import RLock
from uuid import UUID

from ..execution.loop import AgentLoopError, ToolExecutionContext
from .git import GitDiffSnapshot, GitFacade, GitStatusSnapshot
from ..editing.transaction import PatchTransactionResult
from .runner import CommandResult


class VerificationError(Exception):
    def __init__(self, code: str, *, profile_id: str | None = None) -> None:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
            raise ValueError("invalid verification error code")
        self.code = code
        self.profile_id = profile_id
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VerificationLimits:
    max_test_runs: int = 4
    max_report_chars: int = 100_000

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_test_runs, int)
            or isinstance(self.max_test_runs, bool)
            or self.max_test_runs <= 0
            or self.max_test_runs > 32
            or not isinstance(self.max_report_chars, int)
            or isinstance(self.max_report_chars, bool)
            or self.max_report_chars < 1_024
            or self.max_report_chars > 1_000_000
        ):
            raise VerificationError("invalid_verification_limits")


@dataclass(frozen=True, slots=True)
class TestEvidence:
    generation: int
    result: CommandResult


@dataclass(slots=True)
class _RunEvidence:
    generation: int = 0
    patch_digests: list[str] = field(default_factory=list)
    patched_paths: set[str] = field(default_factory=set)
    test_reservations: int = 0
    tests: list[TestEvidence] = field(default_factory=list)
    status: GitStatusSnapshot | None = None
    status_generation: int = -1
    diff: GitDiffSnapshot | None = None
    diff_generation: int = -1
    final_report: str | None = None
    final_generation: int = -1
    final_status_digest: str | None = None


class VerificationLedger:
    """按 D1 run_id 隔离 D5 证据，并用 mutation generation 防止陈旧测试。"""

    def __init__(
        self,
        git: GitFacade,
        *,
        profile_ids: Sequence[str],
        required_test_profiles: Sequence[str] | None = None,
        limits: VerificationLimits | None = None,
    ) -> None:
        if not isinstance(git, GitFacade):
            raise TypeError("git must be GitFacade")
        self._git = git
        self._limits = limits or VerificationLimits()
        if not isinstance(self._limits, VerificationLimits):
            raise TypeError("limits must be VerificationLimits")
        self._profile_ids = _validated_profile_ids(profile_ids)
        self._required_test_profiles = _validated_required_profiles(
            self._profile_ids, required_test_profiles
        )
        self._runs: dict[UUID, _RunEvidence] = {}
        self._lock = RLock()

    def record_patch(
        self,
        context: ToolExecutionContext,
        result: PatchTransactionResult,
    ) -> None:
        with self._lock:
            state = self._state(context.run_id)
            state.generation += 1
            state.patch_digests.append(result.patch_sha256)
            state.patched_paths.update(item.path for item in result.files)
            self._invalidate_final(state)

    def reserve_test(self, run_id: UUID) -> int:
        with self._lock:
            state = self._state(run_id)
            if state.test_reservations >= self._limits.max_test_runs:
                raise VerificationError("test_run_budget_exceeded")
            state.test_reservations += 1
            return state.generation

    def record_test(
        self,
        run_id: UUID,
        generation: int,
        result: CommandResult,
    ) -> None:
        if not isinstance(result, CommandResult):
            raise TypeError("result must be CommandResult")
        with self._lock:
            state = self._state(run_id)
            if result.profile_id not in self._profile_ids:
                raise VerificationError(
                    "unregistered_test_profile", profile_id=result.profile_id
                )
            if (
                not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation < 0
                or generation > state.generation
            ):
                raise VerificationError("invalid_test_generation")
            state.tests.append(TestEvidence(generation, result))
            self._invalidate_final(state)

    def record_status(self, run_id: UUID, status: GitStatusSnapshot) -> None:
        with self._lock:
            state = self._state(run_id)
            state.status = status
            state.status_generation = state.generation
            self._invalidate_final(state)

    def record_diff(self, run_id: UUID, diff: GitDiffSnapshot) -> None:
        with self._lock:
            state = self._state(run_id)
            state.diff = diff
            state.diff_generation = state.generation
            self._invalidate_final(state)

    def finalize(self, run_id: UUID) -> str:
        with self._lock:
            state = self._state(run_id)
            latest_by_profile: dict[str, TestEvidence] = {}
            runs_by_profile = {profile_id: 0 for profile_id in self._profile_ids}
            failures_by_profile = {profile_id: 0 for profile_id in self._profile_ids}
            for evidence in state.tests:
                profile_id = evidence.result.profile_id
                latest_by_profile[profile_id] = evidence
                runs_by_profile[profile_id] += 1
                if not evidence.result.passed:
                    failures_by_profile[profile_id] += 1
            for profile_id in self._required_test_profiles:
                latest = latest_by_profile.get(profile_id)
                if latest is None:
                    raise VerificationError(
                        "required_test_profile_not_run", profile_id=profile_id
                    )
                if latest.generation != state.generation:
                    raise VerificationError(
                        "required_test_profile_stale", profile_id=profile_id
                    )
                if not latest.result.passed:
                    raise VerificationError(
                        "required_test_profile_not_passing", profile_id=profile_id
                    )
            if state.status is None or state.status_generation != state.generation:
                raise VerificationError("git_status_required")
            if state.diff is None or state.diff_generation != state.generation:
                raise VerificationError("git_diff_required")
            if state.diff.status_digest != state.status.digest:
                raise VerificationError("git_evidence_inconsistent")
            if state.diff.truncated:
                raise VerificationError("git_diff_incomplete")
            if not state.diff.changed_paths:
                raise VerificationError("no_agent_changes")
            if not state.diff.diff:
                raise VerificationError("no_agent_diff")
            if not state.patch_digests:
                raise VerificationError("verified_patch_required")
            if any(path not in state.patched_paths for path in state.diff.changed_paths):
                raise VerificationError("unattributed_workspace_changes")
            if not self._git.baseline_unchanged():
                raise VerificationError("baseline_dirty_files_changed")
            required_tests = [
                _profile_evidence_json(
                    profile_id,
                    latest_by_profile[profile_id],
                    runs_by_profile[profile_id],
                    failures_by_profile[profile_id],
                )
                for profile_id in self._required_test_profiles
            ]
            optional_tests = [
                _profile_evidence_json(
                    profile_id,
                    latest_by_profile.get(profile_id),
                    runs_by_profile[profile_id],
                    failures_by_profile[profile_id],
                )
                for profile_id in self._profile_ids
                if profile_id not in self._required_test_profiles
            ]
            payload = {
                "agent_changed_paths": list(state.diff.changed_paths),
                "baseline_dirty_paths": list(self._git.baseline.paths),
                "diff_sha256": state.diff.diff_sha256,
                "generation": state.generation,
                "ok": True,
                "patch_sha256": list(state.patch_digests),
                "required_test_profiles": list(self._required_test_profiles),
                "required_tests": required_tests,
                "optional_tests": optional_tests,
                "test_runs": len(state.tests),
            }
            # One-profile reports retain the D5 field for compatible consumers.
            # Multi-profile reports deliberately have no singular success field.
            if len(self._required_test_profiles) == 1:
                payload["test"] = required_tests[0]["latest"]
            if any(
                latest_by_profile[profile_id].result.backend == "host"
                for profile_id in self._required_test_profiles
            ):
                payload["warning"] = (
                    "host_runner_dev_only_until_d8_container_sandbox"
                )
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if len(encoded) > self._limits.max_report_chars:
                raise VerificationError("finalization_report_too_large")
            state.final_report = encoded
            state.final_generation = state.generation
            state.final_status_digest = state.status.digest
            return encoded

    def assert_complete(self, run_id: UUID) -> None:
        with self._lock:
            state = self._state(run_id)
            if state.final_report is None or state.final_generation != state.generation:
                raise AgentLoopError("verification_required")
            expected_status = state.final_status_digest
        try:
            current = self._git.status()
        except Exception:
            raise AgentLoopError("verification_git_recheck_failed") from None
        if current.digest != expected_status or not self._git.baseline_unchanged():
            raise AgentLoopError("verification_evidence_stale")

    def _state(self, run_id: UUID) -> _RunEvidence:
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be UUID")
        return self._runs.setdefault(run_id, _RunEvidence())

    @staticmethod
    def _invalidate_final(state: _RunEvidence) -> None:
        state.final_report = None
        state.final_generation = -1
        state.final_status_digest = None


def _validated_profile_ids(profile_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(profile_ids, (str, bytes)) or not isinstance(profile_ids, Sequence):
        raise VerificationError("invalid_test_profiles")
    copied = tuple(profile_ids)
    if (
        not copied
        or len(copied) > 64
        or any(
            not isinstance(profile_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", profile_id)
            for profile_id in copied
        )
    ):
        raise VerificationError("invalid_test_profiles")
    if len(set(copied)) != len(copied):
        raise VerificationError("duplicate_test_profile")
    return tuple(sorted(copied))


def _validated_required_profiles(
    profile_ids: tuple[str, ...],
    required_test_profiles: Sequence[str] | None,
) -> tuple[str, ...]:
    if required_test_profiles is None:
        if len(profile_ids) != 1:
            raise VerificationError("required_test_profiles_required")
        return profile_ids
    if isinstance(required_test_profiles, (str, bytes)) or not isinstance(
        required_test_profiles, Sequence
    ):
        raise VerificationError("invalid_required_test_profiles")
    copied = tuple(required_test_profiles)
    if (
        not copied
        or len(copied) > 64
        or any(
            not isinstance(profile_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", profile_id)
            for profile_id in copied
        )
    ):
        raise VerificationError("invalid_required_test_profiles")
    if len(set(copied)) != len(copied):
        raise VerificationError("duplicate_required_test_profile")
    if any(profile_id not in profile_ids for profile_id in copied):
        raise VerificationError("required_test_profile_not_found")
    return tuple(sorted(copied))


def _profile_evidence_json(
    profile_id: str,
    evidence: TestEvidence | None,
    run_count: int,
    historical_failure_count: int,
) -> dict[str, object]:
    latest = None if evidence is None else _command_evidence_json(evidence)
    return {
        "historical_failure_count": historical_failure_count,
        "latest": latest,
        "profile_id": profile_id,
        "run_count": run_count,
    }


def _command_evidence_json(evidence: TestEvidence) -> dict[str, object]:
    result = evidence.result
    return {
        "allocation_id": (
            str(result.allocation_id) if result.allocation_id is not None else None
        ),
        "argv": list(result.argv),
        "backend": result.backend,
        "container_id": result.container_id,
        "duration_ms": result.duration_ms,
        "exit_code": result.exit_code,
        "generation": evidence.generation,
        "immutable_image_id": result.immutable_image_id,
        "outcome": result.outcome.value,
        "profile_id": result.profile_id,
        "profile_digest": result.profile_digest,
        "stderr_truncated": result.stderr_truncated,
        "stdout_truncated": result.stdout_truncated,
        "timeout_seconds": result.timeout_seconds,
    }
