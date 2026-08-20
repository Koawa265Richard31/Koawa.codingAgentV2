from __future__ import annotations

import json
import math
import unittest
from datetime import datetime, timezone
from uuid import UUID

from koawa_agent_v2.sandbox import (
    ALLOCATION_ID_LABEL,
    COMMAND_DIGEST_LABEL,
    IMAGE_ID_LABEL,
    MANAGED_LABEL,
    MANAGED_LABEL_KEYS,
    MANAGED_LABEL_VALUE,
    MOUNT_DIGEST_LABEL,
    OWNER_EXECUTION_ID_LABEL,
    OWNER_NONCE_LABEL,
    PROFILE_DIGEST_LABEL,
    AllocationState,
    DockerDoctorReport,
    ReapReport,
    SandboxAllocation,
    SandboxCommandProfile,
    SandboxError,
    SandboxLimits,
    validate_immutable_image_id,
)


IMAGE_ID = "sha256:" + "1" * 64
DIGEST_A = "2" * 64
DIGEST_B = "3" * 64
DIGEST_C = "4" * 64
CONTAINER_ID = "5" * 64
ALLOCATION_ID = UUID("11111111-1111-4111-8111-111111111111")
OWNER_ID = UUID("22222222-2222-4222-8222-222222222222")
DEADLINE = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class D8SandboxProtocolTest(unittest.TestCase):
    def assert_sandbox_error(self, code: str, action) -> None:
        with self.assertRaises(SandboxError) as caught:
            action()
        self.assertEqual(code, caught.exception.code)

    def test_immutable_image_identity_rejects_tags_short_and_uppercase_ids(self) -> None:
        self.assertEqual(IMAGE_ID, validate_immutable_image_id(IMAGE_ID))
        for unsafe in (
            "python:3.12-slim",
            "python@sha256:" + "1" * 64,
            "sha256:" + "1" * 63,
            "sha256:" + "A" * 64,
            "SHA256:" + "1" * 64,
        ):
            with self.subTest(value=unsafe[:24]):
                self.assert_sandbox_error(
                    "immutable_image_id_required",
                    lambda value=unsafe: validate_immutable_image_id(value),
                )

    def test_profile_rejects_relative_executable_and_workspace_escape(self) -> None:
        self.assert_sandbox_error(
            "command_executable_must_be_posix_absolute",
            lambda: SandboxCommandProfile("unit", ("python", "-V")),
        )
        for cwd in ("workspace", "/outside", "/workspace/../outside"):
            with self.subTest(cwd=cwd):
                self.assert_sandbox_error(
                    "invalid_sandbox_working_directory",
                    lambda value=cwd: SandboxCommandProfile(
                        "unit", ("/usr/local/bin/python", "-V"), value
                    ),
                )

    def test_profile_rejects_secret_like_argv_and_embedded_newlines(self) -> None:
        self.assert_sandbox_error(
            "secret_like_command_argument",
            lambda: SandboxCommandProfile(
                "unit", ("/usr/local/bin/python", "--api-key=value")
            ),
        )
        self.assert_sandbox_error(
            "invalid_sandbox_command",
            lambda: SandboxCommandProfile(
                "unit", ("/usr/local/bin/python", "-c", "print(1)\nprint(2)")
            ),
        )

    def test_profile_rejects_non_whitelisted_and_multiline_environment(self) -> None:
        self.assert_sandbox_error(
            "unsafe_sandbox_environment",
            lambda: SandboxCommandProfile(
                "unit",
                ("/usr/local/bin/python", "-V"),
                environment=(("HOME", "/host/home"),),
            ),
        )
        self.assert_sandbox_error(
            "invalid_sandbox_environment",
            lambda: SandboxCommandProfile(
                "unit",
                ("/usr/local/bin/python", "-V"),
                environment=(("LANG", "C.UTF-8\nHOST_SECRET=value"),),
            ),
        )

    def test_profile_digest_is_canonical_across_environment_input_order(self) -> None:
        first = SandboxCommandProfile(
            "unit",
            ("/usr/local/bin/python", "-B", "-m", "unittest"),
            environment=(("TZ", "UTC"), ("LANG", "C.UTF-8")),
        )
        second = SandboxCommandProfile(
            "unit",
            ("/usr/local/bin/python", "-B", "-m", "unittest"),
            environment=(("LANG", "C.UTF-8"), ("TZ", "UTC")),
        )
        self.assertEqual(first.environment, second.environment)
        self.assertEqual(first.command_digest, second.command_digest)
        self.assertEqual(first.profile_digest, second.profile_digest)
        self.assertEqual(first.canonical_json(), second.canonical_json())

    def test_allocation_labels_and_document_bind_every_security_identity(self) -> None:
        allocation = self._allocation()
        labels = dict(allocation.expected_labels)
        self.assertEqual(set(MANAGED_LABEL_KEYS), set(labels))
        self.assertEqual(MANAGED_LABEL_VALUE, labels[MANAGED_LABEL])
        self.assertEqual(str(ALLOCATION_ID), labels[ALLOCATION_ID_LABEL])
        self.assertEqual(str(OWNER_ID), labels[OWNER_EXECUTION_ID_LABEL])
        self.assertEqual("a" * 64, labels[OWNER_NONCE_LABEL])
        self.assertEqual(IMAGE_ID, labels[IMAGE_ID_LABEL])
        self.assertEqual(DIGEST_A, labels[MOUNT_DIGEST_LABEL])
        self.assertEqual(DIGEST_B, labels[PROFILE_DIGEST_LABEL])
        self.assertEqual(DIGEST_C, labels[COMMAND_DIGEST_LABEL])

        document = allocation.to_document()
        self.assertEqual(labels, document["expected_labels"])
        self.assertEqual("2030-01-02T03:04:05.000000Z", document["deadline_at"])
        self.assertEqual("intended", document["state"])
        self.assertEqual(document, json.loads(json.dumps(document, allow_nan=False)))
        self.assertNotIn("a" * 64, repr(allocation))

    def test_allocation_state_invariants_fail_closed(self) -> None:
        self.assert_sandbox_error(
            "allocation_state_conflict",
            lambda: self._allocation(container_id=CONTAINER_ID),
        )
        self.assert_sandbox_error(
            "allocation_container_not_bound",
            lambda: self._allocation(state=AllocationState.BOUND),
        )
        self.assert_sandbox_error(
            "allocation_outcome_missing",
            lambda: self._allocation(state=AllocationState.FINISHED),
        )
        self.assert_sandbox_error(
            "allocation_state_conflict",
            lambda: self._allocation(outcome="passed"),
        )
        finished = self._allocation(
            state=AllocationState.FINISHED,
            container_id=CONTAINER_ID,
            outcome="passed",
            exit_code=0,
            oom_killed=False,
            finished_at=DEADLINE,
        )
        self.assertEqual(AllocationState.FINISHED, finished.state)
        self.assertEqual(0, finished.exit_code)

    def test_limits_doctor_and_reap_reports_reject_invalid_documents(self) -> None:
        for kwargs in (
            {"cpus": math.nan},
            {"cpus": 0},
            {"memory_bytes": 1024},
            {"pids_limit": 0},
            {"tmpfs_bytes": 0},
            {"cleanup_grace_seconds": 0},
            {"max_workspace_entries": 0},
        ):
            with self.subTest(limits=kwargs):
                self.assert_sandbox_error(
                    "invalid_sandbox_limits",
                    lambda values=kwargs: SandboxLimits(**values),
                )

        invalid_doctors = (
            lambda: DockerDoctorReport(True),
            lambda: DockerDoctorReport(False),
            lambda: DockerDoctorReport(
                True,
                "29.0",
                "29.0",
                "linux",
                "amd64",
                IMAGE_ID,
                "daemon_unavailable",
            ),
        )
        for action in invalid_doctors:
            self.assert_sandbox_error("invalid_doctor_report", action)

        valid_doctor = DockerDoctorReport(
            True, "29.0", "29.0", "linux", "amd64", IMAGE_ID
        )
        self.assertTrue(valid_doctor.to_document()["ready"])

        invalid_reaps = (
            lambda: ReapReport(removed=(CONTAINER_ID, CONTAINER_ID)),
            lambda: ReapReport(
                removed=(CONTAINER_ID,), refused=(CONTAINER_ID,)
            ),
            lambda: ReapReport(removed=("short",)),
            lambda: ReapReport(errors=("Invalid-Code",)),
        )
        for action in invalid_reaps:
            self.assert_sandbox_error("invalid_reap_report", action)
        self.assert_sandbox_error(
            "nil_allocation_identity",
            lambda: ReapReport(released=(UUID(int=0),)),
        )

    @staticmethod
    def _allocation(**overrides) -> SandboxAllocation:
        values = {
            "allocation_id": ALLOCATION_ID,
            "owner_execution_id": OWNER_ID,
            "owner_nonce": "a" * 64,
            "image_id": IMAGE_ID,
            "mount_digest": DIGEST_A,
            "profile_digest": DIGEST_B,
            "command_digest": DIGEST_C,
            "deadline_at": DEADLINE,
        }
        values.update(overrides)
        return SandboxAllocation(**values)


if __name__ == "__main__":
    unittest.main()
