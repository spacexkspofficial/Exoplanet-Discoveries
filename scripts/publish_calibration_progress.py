"""Make a running calibration visible on the live dashboard panel.

The dashboard's live panel globs `results/campaign/*/batch_progress.json` and
`results/*/batch_progress.json` -- deliberately bounded, because `rglob` over
`results/` walks 64,614 per-target reports and cost 587.7 ms on an endpoint the
browser polls every five seconds. A calibration writes `p3_progress.json`, two
levels down, under a name nothing globs. So a 20-hour calibration is invisible
to the dashboard by construction: the owner sees an idle screen and cannot tell
a running job from a dead one.

This bridges that without touching the calibration.

Where the numbers come from, and what is deliberately not read
-------------------------------------------------------------
**It never opens `p3_progress.json`.** On Windows `os.replace` fails when any
other process holds the destination open, even read-only, and a three-minute
progress watcher reading that file is exactly what killed the first v2 attempt
at 187/1,000 stars after 13 hours with `PermissionError: [WinError 5]`. The
driver's `_atomic_json` retries with backoff now (`aee28b7`), but a monitor has
no business being the reason that retry is needed.

Everything here comes from two sources that take no lock the writer cares about:
the driver's append-only stdout log, and a count of files in `stars/`.

The rate is measured, not divided
---------------------------------
The driver's own `searches_per_hour` and `eta_hours` are cumulative averages
over wall clock including the cold-start download, during which zero searches
complete. They read 231 h and then 89.5 h on v2 while the true marginal rate was
344 searches/hour -- correction 55's trap baked into a file. This publishes a
*rolling* rate differenced between successive samples, and labels the cumulative
one separately so the two can never be confused.

Why the published state is "calibrating"
----------------------------------------
`dashboard.py`'s file-derived exporter rglobs `batch_progress.json` and treats
any checkpoint in state `running`/`finalizing`/`retry_pending` as an active
campaign, folding its rows into `active_results` and its target list into
`_sector_coverage`. A synthetic checkpoint must not become a synthetic campaign
in exported science. Publishing state `calibrating`, with an empty `target_list`
and no `results`, keeps this visible to the live panel and invisible to the
exporter.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# "P3 688/6760 searches; 16/1000 stars; 206/hr; ETA 29.423 h; errors 0"
PROGRESS_LINE = re.compile(
    r"P3\s+(?P<searches>\d+)/(?P<searches_total>\d+)\s+searches;\s*"
    r"(?P<stars>\d+)/(?P<stars_total>\d+)\s+stars;.*?errors\s+(?P<errors>\d+)",
)

# The live panel drops any checkpoint older than 900 s, so refresh well inside
# that or the run vanishes from the screen while still running.
DEFAULT_INTERVAL_SECONDS = 60.0

# Enough history for a rolling rate that survives one slow star without going
# to zero, short enough to track a real slowdown.
ROLLING_WINDOW_MINUTES = 30.0


def _latest_log(calibration_dir: Path) -> Path | None:
    logs = sorted(
        calibration_dir.glob("*.stdout.log"),
        key=lambda path: path.stat().st_mtime,
    )
    return logs[-1] if logs else None


def read_progress(calibration_dir: Path) -> dict[str, int] | None:
    """Latest counts from the driver's append-only stdout log.

    Read backwards: the log grows for the whole run and the interesting line is
    always the last one.
    """

    log = _latest_log(calibration_dir)
    if log is None:
        return None
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        match = PROGRESS_LINE.search(line)
        if match:
            return {key: int(value) for key, value in match.groupdict().items()}
    return None


def count_star_files(calibration_dir: Path) -> int:
    """A directory listing takes no handle on the checkpoint. That is the point."""

    stars = calibration_dir / "stars"
    if not stars.is_dir():
        return 0
    return sum(1 for path in stars.iterdir() if path.is_file())


def _earliest_log(calibration_dir: Path) -> Path | None:
    logs = sorted(
        calibration_dir.glob("*.stdout.log"),
        key=lambda path: path.stat().st_mtime,
    )
    return logs[0] if logs else None


def _started_at(calibration_dir: Path) -> datetime | None:
    """When the *calibration* started, not when the current process did.

    A resumed run reports cumulative counts -- the driver skips stars whose
    `stars/TIC_*.json` exists, so its first progress line already reads
    1333/6760 searches. Dividing that by the new process's elapsed time reports
    953,657 searches/hour, which is the cold-start trap wearing a different hat.
    The elapsed clock therefore runs from the *earliest* log in the directory.
    """

    log = _earliest_log(calibration_dir)
    if log is None:
        return None
    stamp = re.search(r"(\d{8})-(\d{6})", log.name)
    if stamp:
        try:
            return datetime.strptime(
                stamp.group(1) + stamp.group(2), "%Y%m%d%H%M%S"
            ).astimezone()
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(log.stat().st_ctime).astimezone()
    except OSError:
        return None


def _rolling_rate(
    samples: list[dict[str, float]], window_minutes: float, key: str
) -> tuple[float | None, int]:
    """Units/hour differenced across the window, never a wall-clock average."""

    if len(samples) < 2:
        return None, len(samples)
    newest = samples[-1]
    cutoff = newest["at"] - window_minutes * 60.0
    within = [sample for sample in samples if sample["at"] >= cutoff]
    if len(within) < 2:
        within = samples[-2:]
    oldest = within[0]
    elapsed = newest["at"] - oldest["at"]
    done = newest.get(key, 0.0) - oldest.get(key, 0.0)
    if elapsed <= 0 or done < 0:
        return None, len(within)
    return done / elapsed * 3600.0, len(within)


def build_checkpoint(
    calibration_dir: Path,
    samples: list[dict[str, float]],
) -> dict[str, object] | None:
    progress = read_progress(calibration_dir)
    if progress is None:
        return None

    now = datetime.now(timezone.utc).replace(microsecond=0)
    started = _started_at(calibration_dir)
    elapsed_hours = (
        (now - started.astimezone(timezone.utc)).total_seconds() / 3600.0
        if started
        else None
    )

    stars_done = max(progress["stars"], count_star_files(calibration_dir))

    # Two rates, because the units are not interchangeable here and putting the
    # wrong one behind a label named `stars_per_hour` is the whole family of
    # traps this project keeps finding.
    #
    # Stars/hour is violently non-linear: injection stars cost 43 searches each
    # and the other ~906 cost 3, so the early figure understates the finish by
    # more than an order of magnitude. On v2 it read ~7/h at 10 h and the run
    # still completed 1,000 stars in ~21 h.
    #
    # Searches/hour is the stable one -- v2 measured 318/h over its first half
    # and 325/h over its second -- so the ETA is derived from searches even
    # though the displayed rate must be stars, to match its label.
    rolling_stars, sample_count = _rolling_rate(
        samples, ROLLING_WINDOW_MINUTES, "stars"
    )
    rolling_searches, _ = _rolling_rate(samples, ROLLING_WINDOW_MINUTES, "searches")
    remaining_searches = progress["searches_total"] - progress["searches"]
    searches_reference = rolling_searches or (
        progress["searches"] / elapsed_hours
        if elapsed_hours and elapsed_hours > 0
        else None
    )
    eta_hours = (
        remaining_searches / searches_reference
        if searches_reference and searches_reference > 0 and remaining_searches > 0
        else None
    )
    cumulative_stars = (
        stars_done / elapsed_hours if elapsed_hours and elapsed_hours > 0 else None
    )
    cumulative_searches = (
        progress["searches"] / elapsed_hours
        if elapsed_hours and elapsed_hours > 0
        else None
    )

    return {
        "schema_version": 1,
        # Deliberately not "running": see the module docstring. This keeps the
        # entry on the live panel and out of the file-derived exporter.
        "state": "calibrating",
        # Empty so `_sector_coverage` cannot fold 1,000 unanalysed targets into
        # exported coverage from a monitoring artifact.
        "target_list": "",
        "sectors": [],
        "results": [],
        "total_targets": progress["stars_total"],
        "completed_targets": stars_done,
        "counts": {"error": progress["errors"]},
        "started_at_utc": started.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        if started
        else None,
        "updated_at_utc": now.isoformat(),
        "runtime": {
            "analysis_workers": 0,
            "download_workers": 0,
            "prefetch_targets": 0,
            "downloads_in_flight": 0,
            "analyses_in_flight": 0,
            "downloaded_waiting": 0,
            "targets_remaining": progress["stars_total"] - stars_done,
            "science_products_downloaded": 0,
            "in_flight": [],
            "stages": [],
            "performance": {
                "average_stars_per_hour": cumulative_stars,
                "rolling_stars_per_hour": rolling_stars,
                "rolling_window_minutes": ROLLING_WINDOW_MINUTES,
                "rolling_samples": sample_count,
                "elapsed_hours": elapsed_hours,
                "eta_hours": eta_hours,
                "estimated_completion_utc": (
                    (now + timedelta(hours=eta_hours)).replace(microsecond=0).isoformat()
                    if eta_hours
                    else None
                ),
            },
        },
        # Named so nobody mistakes searches for stars: 6,760 searches over 1,000
        # stars, because injection stars cost 43 each and the rest cost 3.
        "calibration": {
            "searches_completed": progress["searches"],
            "searches_total": progress["searches_total"],
            "stars_completed": stars_done,
            "stars_total": progress["stars_total"],
            "errors": progress["errors"],
            "rolling_searches_per_hour": rolling_searches,
            "average_searches_per_hour": cumulative_searches,
            "eta_basis": (
                "searches, not stars: injection stars cost 43 searches each and "
                "the remaining ~906 cost 3, so stars/hour rises sharply once the "
                "injection block is done and cannot be extrapolated early"
            ),
            "source": "driver stdout log; p3_progress.json is never opened",
        },
    }


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calibration-dir", required=True)
    parser.add_argument(
        "--publish-dir",
        default="results/calibration_live",
        help=(
            "Must be exactly one level under results/ -- the live panel's glob "
            "is bounded and will not find it deeper."
        ),
    )
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    calibration_dir = Path(args.calibration_dir)
    publish_dir = Path(args.publish_dir)
    checkpoint_path = publish_dir / "batch_progress.json"
    samples_path = publish_dir / "rate_samples.json"

    samples: list[dict[str, float]] = []
    if samples_path.exists():
        try:
            loaded = json.loads(samples_path.read_text(encoding="utf-8"))
            samples = [row for row in loaded if isinstance(row, dict)]
        except (OSError, json.JSONDecodeError):
            samples = []

    while True:
        progress = read_progress(calibration_dir)
        if progress is not None:
            samples.append(
                {"at": time.time(), "searches": float(progress["searches"])}
            )
            # One sample a minute over a day is 1,440 rows; keep it bounded.
            samples = samples[-2000:]
            checkpoint = build_checkpoint(calibration_dir, samples)
            if checkpoint is not None:
                _write_atomic(checkpoint_path, checkpoint)
                _write_atomic(samples_path, samples)  # type: ignore[arg-type]
                performance = checkpoint["runtime"]["performance"]  # type: ignore[index]
                rolling = performance["rolling_stars_per_hour"]  # type: ignore[index]
                print(
                    f"{checkpoint['completed_targets']}/{checkpoint['total_targets']} "
                    f"stars, {progress['searches']}/{progress['searches_total']} "
                    f"searches, errors {progress['errors']}, rolling "
                    + (f"{rolling:.0f}/h" if rolling else "measuring"),
                    flush=True,
                )
        else:
            print("no progress line in the calibration log yet", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
