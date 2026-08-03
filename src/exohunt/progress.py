"""What each in-flight target is doing right now.

A campaign publishes nothing about a target until it finishes, so a run in
progress is a pair of counters: four analyses, two downloads. That is enough
to know work is happening and not enough to see *what* is happening, which is
what makes a long run feel opaque.

This keeps a small registry of the handful of targets currently being worked
on -- roughly six at the configured concurrency -- and what stage each is in.
The campaign coordinator folds a snapshot into the checkpoint it already
writes, so the dashboard gets it for free on its existing poll.

Three deliberate constraints:

* **It is provenance, never evidence.** Nothing here is written to a report,
  votes on a status, or enters the ledger. A stage label is a progress
  message; losing it changes no scientific result.
* **It must not slow the hot path.** Recording a stage is a dictionary write
  under a short-lived lock, with no I/O. The coordinator serialises snapshots
  on its existing throttled checkpoint write, not per stage change.
* **It must never break a campaign.** Every entry point tolerates being
  called out of order, twice, or not at all, because instrumentation that can
  fail a twelve-hour run is worse than no instrumentation.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any


def _directory_bytes(path: object) -> int | None:
    """Total bytes currently on disk under a cache namespace.

    Returns None rather than raising: the directory may not exist yet, may
    vanish under a retry that clears a failed namespace, or may be briefly
    unreadable while Windows holds a handle. None simply means "no size to
    report", which the panel renders as a spinner rather than a wrong number.
    """

    if not isinstance(path, Path):
        return None
    try:
        total = 0
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        return total
    except OSError:
        return None

# The ordered pipeline a target passes through. The dashboard renders a bar
# across these, so the order is the display order; unknown stages sort last
# rather than breaking the layout.
STAGES: tuple[str, ...] = (
    "queued",
    "downloading",
    "preparing",
    "masking",
    "searching",
    "vetting",
    "writing",
)

# Which module owns each stage, so the panel can say what is running rather
# than only what phase it is in.
STAGE_MODULES: dict[str, str] = {
    "queued": "campaign.py",
    "downloading": "photometry.py",
    "preparing": "detrending.py",
    "masking": "catalogs.py",
    "searching": "search.py",
    "vetting": "vetoes.py",
    "writing": "cli.py",
}


class StageTracker:
    """Thread-safe registry of in-flight targets and their current stage."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._targets: dict[int, dict[str, Any]] = {}

    def begin(
        self,
        tic_id: int | None,
        *,
        target: str = "",
        stage: str = "queued",
        download_dir: "Path | None" = None,
    ) -> None:
        """Register a target as in flight, or reset it if already present.

        ``download_dir`` is the target's cache namespace. Archive clients do
        not report byte progress, but the partially written file on disk
        does, so the size of that directory is the honest measure of how far
        a download has actually got.
        """

        if tic_id is None:
            return
        now = time.time()
        with self._lock:
            self._targets[int(tic_id)] = {
                "tic_id": int(tic_id),
                "target": str(target),
                "stage": stage,
                "started_at": now,
                "stage_started_at": now,
                "download_dir": download_dir,
            }

    def stage(self, tic_id: int | None, stage: str) -> None:
        """Move a target to a new stage.

        Silently ignores unknown targets: the single-target analysis path is
        also reachable outside a campaign, where nothing registered it.
        """

        if tic_id is None:
            return
        now = time.time()
        with self._lock:
            entry = self._targets.get(int(tic_id))
            if entry is None:
                return
            if entry["stage"] != stage:
                entry["stage"] = stage
                entry["stage_started_at"] = now

    def finish(self, tic_id: int | None) -> None:
        if tic_id is None:
            return
        with self._lock:
            self._targets.pop(int(tic_id), None)

    def clear(self) -> None:
        with self._lock:
            self._targets.clear()

    def snapshot(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Return the current in-flight targets, longest-running first."""

        moment = time.time() if now is None else now
        with self._lock:
            entries = list(self._targets.values())
        rows = []
        for entry in entries:
            stage = str(entry["stage"])
            try:
                index = STAGES.index(stage)
            except ValueError:
                index = len(STAGES) - 1
            stage_elapsed = max(moment - entry["stage_started_at"], 0.0)
            row = {
                "tic_id": entry["tic_id"],
                "target": entry["target"],
                "stage": stage,
                "module": STAGE_MODULES.get(stage, ""),
                "stage_index": index,
                "stage_count": len(STAGES),
                "elapsed_seconds": round(max(moment - entry["started_at"], 0.0), 1),
                "stage_elapsed_seconds": round(stage_elapsed, 1),
            }
            if stage == "downloading":
                fetched = _directory_bytes(entry.get("download_dir"))
                if fetched is not None:
                    row["downloaded_bytes"] = fetched
                    # Rate is only meaningful once a little time has passed;
                    # reporting MB/s from a fraction of a second is noise.
                    if stage_elapsed >= 1.0:
                        row["download_bytes_per_second"] = round(
                            fetched / stage_elapsed
                        )
            rows.append(row)
        rows.sort(key=lambda row: -row["elapsed_seconds"])
        return rows


# One tracker per process. The campaign coordinator is the only writer of the
# checkpoint, and the worker threads share this process, so a module-level
# instance is the whole coordination mechanism needed.
TRACKER = StageTracker()
