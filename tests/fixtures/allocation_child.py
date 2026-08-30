"""Tiny owned process with an OS lock used by allocation kill tests."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


lock_path, marker_path, nonce = map(Path, sys.argv[1:4])
handle = lock_path.open("a+b")
if os.name == "nt":
    import msvcrt
    handle.seek(0)
    handle.write(b"x")
    handle.flush()
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
marker_path.write_text(
    json.dumps({"pid": os.getpid(), "nonce": nonce.name}), encoding="utf-8",
)
while True:
    time.sleep(60)
