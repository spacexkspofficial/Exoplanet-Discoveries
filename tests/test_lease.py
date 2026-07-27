"""Machine-wide coordinator exclusion."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from exohunt.lease import acquire_machine_lock, coordinator_lock_is_free


def _unique_name() -> str:
    return f"exohunt-test-{uuid.uuid4().hex}"


def test_second_acquire_is_denied_until_release(tmp_path: Path) -> None:
    name = _unique_name()
    first = acquire_machine_lock(name, directory=tmp_path)
    assert first is not None
    assert acquire_machine_lock(name, directory=tmp_path) is None
    assert not coordinator_lock_is_free(name, directory=tmp_path)
    first.release()
    second = acquire_machine_lock(name, directory=tmp_path)
    assert second is not None
    second.release()


def test_file_lock_backend_matches_mutex_semantics(tmp_path: Path) -> None:
    name = _unique_name()
    first = acquire_machine_lock(name, directory=tmp_path, force_file_lock=True)
    assert first is not None
    assert (
        acquire_machine_lock(name, directory=tmp_path, force_file_lock=True)
        is None
    )
    first.release()
    second = acquire_machine_lock(name, directory=tmp_path, force_file_lock=True)
    assert second is not None
    second.release()


def test_release_is_idempotent(tmp_path: Path) -> None:
    name = _unique_name()
    lock = acquire_machine_lock(name, directory=tmp_path)
    assert lock is not None
    lock.release()
    lock.release()


_CHILD_SCRIPT = """
import sys, time
from exohunt.lease import acquire_machine_lock
lock = acquire_machine_lock(sys.argv[1])
print("acquired" if lock is not None else "denied", flush=True)
if lock is None:
    raise SystemExit(3)
time.sleep(60)
"""


def test_lock_excludes_other_processes_and_dies_with_holder(
    tmp_path: Path,
) -> None:
    """A killed holder must free the lock without any cleanup code running."""

    name = _unique_name()
    src = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
    # The child must use the same backend the parent probes below; force the
    # portable file backend so the test is deterministic on every platform.
    env["EXOHUNT_STATE_DIR"] = str(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT, name],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = child.stdout.readline().strip()
        assert line == "acquired"
        if sys.platform == "win32":
            held = acquire_machine_lock(name)
        else:
            held = acquire_machine_lock(name, directory=tmp_path / "locks")
        assert held is None, "the child holds the lock; the parent must be denied"
        child.kill()
        child.wait(timeout=30)
        deadline = time.monotonic() + 30
        reacquired = None
        while reacquired is None and time.monotonic() < deadline:
            if sys.platform == "win32":
                reacquired = acquire_machine_lock(name)
            else:
                reacquired = acquire_machine_lock(
                    name, directory=tmp_path / "locks"
                )
            if reacquired is None:
                time.sleep(0.2)
        assert reacquired is not None, "lock must be freed by holder death"
        reacquired.release()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
