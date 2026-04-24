"""Concurrent install_casa_plugin calls serialize on .install.lock
(spike §Key learning 5)."""
from __future__ import annotations

import multiprocessing as mp
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    sys.platform == "win32", reason="fcntl.flock is POSIX-only",
)]


def _acquire_and_hold(lock_path: str, hold_seconds: float, result_queue) -> None:
    import fcntl
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        result_queue.put(("acquired", time.time()))
        time.sleep(hold_seconds)
        fcntl.flock(fh, fcntl.LOCK_UN)


def test_flock_serializes(tmp_path: Path) -> None:
    lock = tmp_path / ".install.lock"
    lock.touch()
    q: mp.Queue = mp.Queue()
    p1 = mp.Process(target=_acquire_and_hold, args=(str(lock), 0.5, q))
    p2 = mp.Process(target=_acquire_and_hold, args=(str(lock), 0.1, q))
    p1.start()
    time.sleep(0.05)
    p2.start()
    p1.join(5)
    p2.join(5)

    t1 = q.get(timeout=1)
    t2 = q.get(timeout=1)
    # Second acquisition must be ≥ 0.4s after the first (first holds for 0.5s).
    assert abs(t2[1] - t1[1]) >= 0.4
