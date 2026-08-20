from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import UUID

from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.sandbox.protocol import SandboxCommandProfile
from koawa_agent_v2.sandbox.runtime import DockerCommandRunner


database = Path(sys.argv[1])
workspace = Path(sys.argv[2])
docker_executable = sys.argv[3]
image_id = sys.argv[4]
signal_path = Path(sys.argv[5])
execution_id = UUID(sys.argv[6])


def die_after_create(point, allocation, container_id) -> None:
    del allocation
    if point != "after_create_before_bind":
        return
    if container_id is None:
        os._exit(72)
    signal_path.write_text(container_id, encoding="ascii")
    os._exit(73)


runner = DockerCommandRunner(
    workspace,
    (
        SandboxCommandProfile(
            "crash",
            ("/usr/local/bin/python", "-c", "import time;time.sleep(30)"),
            timeout_seconds=30.0,
        ),
    ),
    SqliteEventStore(database),
    image_id,
    docker_executable=docker_executable,
    fault_hook=die_after_create,
)
runner.run("crash", execution_id=execution_id)
