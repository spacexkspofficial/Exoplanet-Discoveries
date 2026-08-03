"""Keep this computer awake while long-running local work is in flight.

A campaign is tens of minutes of unattended CPU and archive traffic, and the
dashboard is meant to stay reachable on loopback for as long as it is
running. Both are defeated by the machine going to sleep half way through.

Windows exposes exactly the right primitive for this:
``SetThreadExecutionState`` asserts a *request*, scoped to the calling
process, that the system not go idle-to-sleep. It is not a settings change --
nothing in the user's power plan is edited, no registry key is written, and
the request evaporates when the process exits, including if it is killed.
That property is why this is preferred over touching power configuration:
there is no state left behind to clean up or get wrong.

Display sleep is left alone by default. Keeping a monitor lit for an
unattended overnight run wastes power for no benefit; callers that genuinely
need the screen (a wall display of the dashboard) can ask for it explicitly.

On any non-Windows platform this degrades to a no-op that reports itself as
inactive, so callers can state honestly whether the machine is actually being
held awake rather than assuming it is.
"""

from __future__ import annotations

import sys

# Flags from the Win32 API. ES_CONTINUOUS makes the request persist until it
# is cleared, rather than resetting the idle timer once.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


class KeepAwake:
    """Hold a system-awake request for as long as this object is active."""

    def __init__(self, *, keep_display_on: bool = False) -> None:
        self.keep_display_on = keep_display_on
        self.active = False
        self.reason: str = "not started"
        self._kernel32 = None

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def start(self) -> bool:
        """Request that the system stay awake. Returns whether it took hold."""

        if self.active:
            return True
        if not self.supported:
            self.reason = (
                f"not supported on {sys.platform}; the system may sleep"
            )
            return False
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            if self.keep_display_on:
                flags |= ES_DISPLAY_REQUIRED
            # A zero return means the request was refused; treating that as
            # success would let the caller promise something untrue.
            if kernel32.SetThreadExecutionState(flags) == 0:
                self.reason = "the system refused the stay-awake request"
                return False
            self._kernel32 = kernel32
            self.active = True
            self.reason = (
                "system and display held awake"
                if self.keep_display_on
                else "system held awake; the display may still sleep"
            )
            return True
        except (OSError, AttributeError, ImportError) as exc:
            self.reason = f"stay-awake request unavailable ({exc})"
            return False

    def stop(self) -> None:
        """Release the request, letting normal power management resume."""

        if not self.active or self._kernel32 is None:
            return
        try:
            self._kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except OSError:
            # The request dies with the process anyway, so a failure here
            # cannot strand the machine awake indefinitely.
            pass
        finally:
            self.active = False
            self.reason = "released"
            self._kernel32 = None

    def __enter__(self) -> "KeepAwake":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
