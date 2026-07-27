"""Single-coordinator exclusion, enforced by the operating system.

More than one actor has started campaign coordinators on this machine: a
scheduled automation restarted the Sector 100 coordinator unprompted, and two
dashboard servers have been observed running side by side. Exclusion therefore
cannot depend on knowing every scheduler; the system enforces it directly.

On Windows the guard is a named kernel mutex, which is released automatically
when the holding process exits or dies -- a crashed coordinator can never
wedge the lock. On other platforms (and wherever tests request it) a
``filelock`` file under the local state root provides the same semantics.

The contract for a second coordinator is deliberate: it exits *successfully*
with a clear message. Restart automations that fire while a coordinator is
already alive then do nothing, instead of crash-looping or, worse, running.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from filelock import FileLock, Timeout

from .paths import lock_dir

COORDINATOR_LOCK_NAME = "exohunt-coordinator"
DASHBOARD_LOCK_NAME = "exohunt-dashboard"

ALREADY_RUNNING_MESSAGE = (
    "Another EXOHUNT coordinator is already running on this machine. "
    "Exiting without starting a second one; stop the live coordinator first "
    "if you intended to replace it."
)


class MachineLock:
    """A machine-wide exclusive lock, released automatically on process death."""

    def __init__(self, name: str, backend: object) -> None:
        self.name = name
        self._backend = backend
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        backend = self._backend
        self._backend = None
        if backend is None:
            return
        if isinstance(backend, FileLock):
            backend.release()
            return
        import ctypes

        handle = int(backend)  # type: ignore[arg-type]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex(ctypes.c_void_p(handle))
        kernel32.CloseHandle(ctypes.c_void_p(handle))

    def __enter__(self) -> "MachineLock":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _acquire_windows_mutex(name: str) -> MachineLock | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    ERROR_ALREADY_EXISTS = 183

    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        raise OSError(
            f"CreateMutexW failed for {name!r}: error {ctypes.get_last_error()}"
        )
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        # Another process owns the mutex; we hold only an extra handle to it.
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return MachineLock(name, int(handle))


def _acquire_file_lock(name: str, directory: Path) -> MachineLock | None:
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(directory / f"{name}.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return None
    return MachineLock(name, lock)


def acquire_machine_lock(
    name: str = COORDINATOR_LOCK_NAME,
    *,
    directory: Path | None = None,
    force_file_lock: bool = False,
) -> MachineLock | None:
    """Try to take a machine-wide lock; return ``None`` if it is held elsewhere.

    ``force_file_lock`` exists for tests that must exercise the portable
    backend on any platform. Production callers accept the platform default:
    a named kernel mutex on Windows, a state-root lock file elsewhere.
    """

    if sys.platform == "win32" and not force_file_lock:
        return _acquire_windows_mutex(name)
    return _acquire_file_lock(name, directory or lock_dir())


def coordinator_lock_is_free(
    name: str = COORDINATOR_LOCK_NAME,
    *,
    directory: Path | None = None,
    force_file_lock: bool = False,
) -> bool:
    """Probe whether the coordinator lock could be acquired right now."""

    probe = acquire_machine_lock(
        name, directory=directory, force_file_lock=force_file_lock
    )
    if probe is None:
        return False
    probe.release()
    return True


def holder_description() -> str:
    """Describe this process for lease records and log lines."""

    return f"pid {os.getpid()} on {os.environ.get('COMPUTERNAME') or 'localhost'}"
