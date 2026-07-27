"""Truthful state repair for orphaned worker checkpoints.

A killed coordinator leaves ``state: "running"`` in its checkpoint with
nothing running, which drives a phantom live panel on the dashboard. This was
observed live on 2026-07-27: ``sector100_spoc`` claimed to be running 47
minutes after its process disappeared. Liveness is a property of a process,
not of a file, so a checkpoint that claims to be running is repaired to
``interrupted`` once two conditions hold:

* no live coordinator holds the machine lock (a running coordinator would);
* the checkpoint has not been updated for longer than the staleness window.

The repair is additive and audited: the previous state is preserved in a
``repair`` block, and nothing else in the checkpoint is touched. Completed
work is never affected -- per-target reports are durable and rediscovered on
resume regardless of checkpoint state.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .lease import COORDINATOR_LOCK_NAME, acquire_machine_lock

CHECKPOINT_FILENAMES = (
    "batch_progress.json",
    "context_vet_progress.json",
    "science_vet_progress.json",
)
LIVE_STATES = {"running", "finalizing"}
DEFAULT_STALE_MINUTES = 10.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _atomic_write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (2**attempt))


def _last_activity(path: Path, checkpoint: dict[str, Any]) -> datetime | None:
    """Return the newest evidence of coordinator activity for a checkpoint."""

    candidates = [
        _parse_utc(checkpoint.get("updated_at_utc")),
        _parse_utc(checkpoint.get("started_at_utc")),
    ]
    try:
        candidates.append(
            datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        )
    except OSError:
        pass
    known = [value for value in candidates if value is not None]
    return max(known) if known else None


def repair_stale_checkpoints(
    results_root: str | Path,
    *,
    stale_after_minutes: float = DEFAULT_STALE_MINUTES,
    dry_run: bool = False,
    now: datetime | None = None,
    lock_name: str = COORDINATOR_LOCK_NAME,
    lock_directory: Path | None = None,
    force_file_lock: bool = False,
) -> dict[str, Any]:
    """Mark stale live-state checkpoints ``interrupted``, with an audit trail.

    Refuses to touch anything while the coordinator lock is held: a live
    coordinator's checkpoint is the coordinator's to write.
    """

    if stale_after_minutes < 0:
        raise ValueError("stale_after_minutes must be non-negative")
    moment = now or _utc_now()
    root = Path(results_root)
    report: dict[str, Any] = {
        "schema_version": 1,
        "results_root": str(root),
        "dry_run": dry_run,
        "stale_after_minutes": stale_after_minutes,
        "generated_at_utc": moment.replace(microsecond=0).isoformat(),
        "refused": False,
        "examined": 0,
        "repaired": [],
        "left_alone": [],
    }

    lock = acquire_machine_lock(
        lock_name, directory=lock_directory, force_file_lock=force_file_lock
    )
    if lock is None:
        report["refused"] = True
        report["reason"] = (
            "A live coordinator holds the machine lock; its checkpoints are "
            "not stale and must not be rewritten underneath it."
        )
        return report

    try:
        if not root.exists():
            return report
        paths = [
            path
            for filename in CHECKPOINT_FILENAMES
            for path in sorted(root.rglob(filename))
        ]
        for path in paths:
            try:
                checkpoint = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                report["left_alone"].append(
                    {"path": str(path), "reason": f"unreadable: {error}"}
                )
                continue
            if not isinstance(checkpoint, dict):
                report["left_alone"].append(
                    {"path": str(path), "reason": "not a JSON object"}
                )
                continue
            report["examined"] += 1
            state = str(checkpoint.get("state") or "")
            if state not in LIVE_STATES:
                report["left_alone"].append(
                    {"path": str(path), "reason": f"state is {state or 'absent'!r}"}
                )
                continue
            last_activity = _last_activity(path, checkpoint)
            age_minutes = (
                (moment - last_activity).total_seconds() / 60.0
                if last_activity is not None
                else None
            )
            if age_minutes is not None and age_minutes < stale_after_minutes:
                report["left_alone"].append(
                    {
                        "path": str(path),
                        "reason": (
                            f"state {state!r} but updated "
                            f"{age_minutes:.1f} minutes ago"
                        ),
                    }
                )
                continue
            entry = {
                "path": str(path),
                "previous_state": state,
                "last_activity_utc": (
                    last_activity.replace(microsecond=0).isoformat()
                    if last_activity is not None
                    else None
                ),
                "stale_minutes": (
                    round(age_minutes, 1) if age_minutes is not None else None
                ),
            }
            if not dry_run:
                checkpoint["state"] = "interrupted"
                checkpoint["repair"] = {
                    "previous_state": state,
                    "repaired_at_utc": moment.replace(microsecond=0).isoformat(),
                    "reason": (
                        "No live coordinator held the machine lock and this "
                        "checkpoint had been idle past the staleness window. "
                        "Durable per-target reports are unaffected and will "
                        "be rediscovered on a genuine resume."
                    ),
                }
                _atomic_write_json(path, checkpoint)
            report["repaired"].append(entry)
        return report
    finally:
        lock.release()
