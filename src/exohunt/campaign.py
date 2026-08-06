"""Batch campaign scheduling and support for the EXOHUNT command-line application.

This module owns the ``batch-hunt`` concern: reading a target list, deciding a
campaign's scientific identity, resuming from durable per-target reports,
running downloads and analyses, and publishing checkpoints and summaries.

Collaborators that still live in :mod:`exohunt.cli` are resolved on the live
``cli`` module at call time rather than bound at import time.  Tests and
operational tooling monkeypatch those names on ``cli``, and the campaign path
must keep seeing the patched objects after the move.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from filelock import FileLock, Timeout

from .lease import (
    ALREADY_RUNNING_MESSAGE,
    acquire_machine_lock,
    holder_description,
)
from .paths import path_is_within, resolve_cache_dir
from .progress import STAGES, TRACKER
from .screening import _classify_screening_result, _sensitivity_depth_at_period

# The dashboard defines "a campaign is running" as the age of this lease's
# heartbeat and nothing else -- deliberately, because a `state` string in a
# checkpoint file outlives the process that wrote it (the phantom `running`
# sector100_spoc checkpoint P0 had to repair). The kernel mutex guarantees
# exclusion but is invisible to another process, so without this row a live
# campaign is indistinguishable from no campaign at all.
COORDINATOR_LEASE_NAME = "coordinator"

# Upper bound on targets staged in memory at once. Each holds one sector's
# time and flux arrays, so the ceiling is about a hundred megabytes rather than
# anything that competes with the search itself.
MAX_PREFETCH_TARGETS = 256

# A cache prune runs while downloads are in flight, so it must not delete a
# file a download is still writing. Files touched inside this window are never
# pruned on the normal path; a download that takes longer than this is already
# a failure by any other measure.
IN_FLIGHT_CACHE_PROTECTION_SECONDS = 900.0

# A prune walks the cache and sizes the whole workspace, so it is far too
# expensive to run once per ten completions on a large workspace. The count
# still triggers it; this floor keeps the I/O from competing with the
# downloads it is meant to make room for.
MINIMUM_PRUNE_INTERVAL_SECONDS = 120.0

# Rebuilding the dashboard snapshot re-walks the results tree and re-parses the
# whole survey; measured at roughly 15 s against a 5 s checkpoint throttle, so
# inline it consumed the coordinator thread entirely. It is a progress view,
# not evidence, so it refreshes on this cadence instead.
DASHBOARD_EXPORT_INTERVAL_SECONDS = 120.0


def _plain_arrays_for_transport(downloaded):
    """Strip astropy masking so a light curve survives a process boundary.

    lightkurve hands back flux as a ``MaskedNDArray``. Pickling one loses its
    ``.mask`` -- the child receives an array whose mask is ``None``, and the
    first fancy-index in ``phase_fold`` raises ``'NoneType' object has no
    attribute '__getitem__'``. Every target fails, and the traceback is
    swallowed into an error row.

    The conversion is only safe because the mask carries nothing: the
    preparation path runs ``remove_nans`` before this point, and measured
    across cached Sector 100 targets the mask was set on 0 of ~10,000 cadences
    every time. That is an assumption about upstream behaviour rather than a
    guarantee, so a non-empty mask raises here instead of being dropped
    silently -- losing cadences would change which data the search sees.
    """

    time_values, flux, metadata = downloaded
    mask = getattr(flux, "mask", None)
    if mask is not None and bool(np.any(np.asarray(mask))):
        raise RuntimeError(
            "Refusing to send a masked light curve to an analysis process: "
            f"{int(np.count_nonzero(np.asarray(mask)))} masked cadences would "
            "be silently unmasked. Run with --analysis-processes 0."
        )
    # Strip the mask without touching the dtype: mission flux is float32, and
    # upcasting it here would silently change every fitted depth downstream.
    return (
        np.asarray(time_values),
        np.asarray(flux),
        metadata,
    )


def _analysis_executor(workers: int, processes: int):
    """The pool that runs the search, threads or processes.

    Threads keep the stage tracker, the monkeypatch seam the tests rely on, and
    zero start-up cost. Processes give real parallelism: the search is CPU-bound
    Python and holds the GIL, so thread workers plateau near one core no matter
    how many are configured.
    """

    if processes > 0:
        # Pin each worker's linear-algebra backend to one thread before any
        # child is spawned, so they inherit it at import time.
        #
        # Without this every worker sizes its own OpenBLAS pool from the core
        # count: eight workers on a sixteen-thread machine meant 128 BLAS
        # threads, each carrying its own buffers. A 64,000-target run died with
        # "OpenBLAS error: Memory allocation still failed after 10 retries" and
        # took the process pool with it. The parallelism that matters here is
        # across targets, not within one matrix operation, so one thread per
        # worker is also the faster arrangement.
        for variable in (
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
        ):
            os.environ.setdefault(variable, "1")
        return ProcessPoolExecutor(max_workers=processes)
    return ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="exohunt-analysis"
    )


class _CoordinatorHeartbeat:
    """Publish campaign liveness to the ledger, or stay silent if it cannot.

    A campaign must never fail because the control plane is unavailable, so
    every ledger error here degrades to "no heartbeat published" and is
    latched off rather than retried per target. The dashboard then reports
    the campaign as absent, which is the honest reading: nothing is claiming
    liveness.
    """

    def __init__(self) -> None:
        self._conn = None
        self._holder: str | None = None
        self._latched_off = False
        self._failures = 0
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start_background(self, interval_seconds: float = 10.0) -> None:
        """Beat on a timer, independent of target completion.

        Beating only when a target finishes ties liveness to throughput: a
        single slow archive fetch can exceed the dashboard's 45-second
        threshold and make a perfectly healthy campaign flicker between live
        and stale. The coordinator is alive whether or not a target happened
        to complete, so the heartbeat says so on its own schedule.
        """

        if self._thread is not None:
            return
        self._stop = threading.Event()

        def loop() -> None:
            self.beat()
            assert self._stop is not None
            while not self._stop.wait(interval_seconds):
                self.beat()

        self._thread = threading.Thread(
            target=loop, name="exohunt-heartbeat", daemon=True
        )
        self._thread.start()

    def beat(self) -> None:
        """Refresh the coordinator lease. Only the timer thread calls this.

        A SQLite connection belongs to the thread that created it, so this
        must have exactly one caller. It previously had two -- the timer and
        `publish_progress` on a worker thread -- and whichever lost the race
        raised `ProgrammingError`, which the blanket except turned into a
        permanent latch-off. The campaign then ran for hours reporting stale.
        """

        if self._latched_off:
            return
        try:
            from . import ledger

            if self._conn is None:
                self._holder = holder_description()
                self._conn = ledger.connect()
            outcome = ledger.acquire_db_lease(
                self._conn,
                name=COORDINATOR_LEASE_NAME,
                holder=self._holder,
            )
            # "denied" means a previous coordinator's row is still inside its
            # TTL -- normal right after a restart, and it resolves itself on
            # takeover. It is not an error and must not stop the beating.
            if outcome != "denied":
                self._failures = 0
        except Exception:
            # Recover rather than give up: drop the connection so the next
            # tick reconnects. Only a persistent fault latches off, and even
            # then the campaign is unaffected -- the dashboard simply reports
            # nothing as running.
            self._failures += 1
            self._close()
            if self._failures >= 5:
                self._latched_off = True

    def release(self) -> None:
        # Stop the timer first, so the thread cannot touch the connection
        # while this thread is closing it.
        if self._stop is not None:
            self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
        holder = self._holder
        # The timer thread owned self._conn, and a SQLite connection cannot
        # cross threads, so drop the reference and use a fresh one here.
        self._conn = None
        if holder is None:
            return
        conn = None
        try:
            from . import ledger

            conn = ledger.connect()
            ledger.release_db_lease(
                conn,
                name=COORDINATOR_LEASE_NAME,
                holder=holder,
            )
            conn.commit()
        except Exception:
            # A stale row is self-correcting: the dashboard ages it out of
            # "live", and the next coordinator takes it over after its TTL.
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None


_COORDINATOR_HEARTBEAT = _CoordinatorHeartbeat()


def _batch_hunt(args: argparse.Namespace) -> int:
    # Machine-wide exclusion first: more than one actor has started
    # coordinators on this machine (a scheduled automation restarted the
    # Sector 100 coordinator unprompted). A second coordinator exits
    # successfully so restart automations do nothing instead of crash-looping.
    from . import cli as cli_module

    coordinator = acquire_machine_lock()
    if coordinator is None:
        print(ALREADY_RUNNING_MESSAGE)
        return 0
    try:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(output_dir / ".batch-hunt.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError(
                f"Another batch worker already owns {output_dir}. "
                "Stop it before resuming this campaign."
            ) from exc
        # A campaign is tens of minutes of unattended work; letting the
        # machine sleep through it strands a partially finished cohort. The
        # request is process-scoped and self-releasing, so a crash cannot
        # leave the computer permanently awake.
        from .keepawake import KeepAwake

        awake = KeepAwake()
        if not getattr(args, "allow_sleep", False):
            awake.start()
            print(f"Power: {awake.reason}")
        # Liveness on its own clock, so a slow archive fetch cannot make a
        # healthy campaign read as stale.
        _COORDINATOR_HEARTBEAT.start_background()
        try:
            return cli_module._run_batch_hunt(args)
        finally:
            awake.stop()
            _COORDINATOR_HEARTBEAT.release()
            lock.release()
    finally:
        coordinator.release()


def _run_batch_hunt(args: argparse.Namespace) -> int:
    """Historical CLI name for the campaign scheduler entry point."""

    return run_batch_hunt(args)


def run_batch_hunt(args: argparse.Namespace) -> int:
    # Resolve collaborators at call time so existing CLI monkeypatch seams remain
    # authoritative while orchestration lives in this dedicated module.
    from . import cli as cli_module

    _read_target_rows = cli_module._read_target_rows
    _batch_target_spec = cli_module._batch_target_spec
    _legacy_checkpoint_matches = cli_module._legacy_checkpoint_matches
    _load_reusable_report = cli_module._load_reusable_report
    _result_row_from_report = cli_module._result_row_from_report
    _performance_snapshot = cli_module._performance_snapshot
    _vetting_coverage = cli_module._vetting_coverage
    _campaign_settings = cli_module._campaign_settings
    _campaign_counts = cli_module._campaign_counts
    _atomic_write_json = cli_module._atomic_write_json
    _publish_followup_queue = cli_module._publish_followup_queue
    _download_batch_target = cli_module._download_batch_target
    _analyze_downloaded_batch_target = cli_module._analyze_downloaded_batch_target
    _batch_error_row = cli_module._batch_error_row
    _quarantine_invalid_common_mode = cli_module._quarantine_invalid_common_mode
    _replace_with_retry = cli_module._replace_with_retry
    directory_size_bytes = cli_module.directory_size_bytes
    prune_fits_cache = cli_module.prune_fits_cache
    prune_rejected_plots = cli_module.prune_rejected_plots
    record_campaign = cli_module.record_campaign

    target_path = Path(args.targets)
    rows = _read_target_rows(target_path)
    if args.max_targets is not None:
        if int(args.max_targets) <= 0:
            raise ValueError("--max-targets must be greater than zero.")
        rows = rows[: args.max_targets]
    if not rows:
        raise RuntimeError("Target CSV contains no rows.")
    # Every campaign is stamped, including diagnostic work. Only an explicit
    # trusted-first-pass request is blocked on the release registry, so P3 can
    # run the calibration that creates the report without circular approval.
    from .config import code_version, hash_target_list, settings_signature

    signature = settings_signature(
        code=code_version(Path.cwd()),
        settings=cli_module._scientific_settings(args),
        product_family=f"{args.author}-{float(args.cadence_seconds):g}s",
        target_list_hash=hash_target_list(target_path),
    )
    args.scientific_signature = signature
    if bool(getattr(args, "trusted_first_pass", False)):
        from . import ledger
        from .config import require_clean_repository

        require_clean_repository(Path.cwd())
        conn = ledger.connect()
        try:
            ledger.require_released_signature(conn, signature)
        finally:
            conn.close()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    workers = max(1, int(getattr(args, "workers", 1)))
    if workers > 8:
        raise ValueError("At most 8 analysis workers are supported.")
    prefetch_arg = getattr(args, "prefetch", None)
    # `prefetch` bounds everything staged in memory at once -- downloads in
    # flight, downloaded-and-waiting, and analyses running -- so the read-ahead
    # buffer actually sitting in front of the analysers is `prefetch - workers`.
    # The old default of `workers * 2` therefore bought a buffer only as deep as
    # the worker count, which empties as fast as the analysers can drain it.
    # A staged target holds two float64 arrays of one sector's cadences, roughly
    # 300 KB, so a much deeper queue costs tens of megabytes and is the cheapest
    # part of this pipeline.
    prefetch = max(
        workers,
        int(prefetch_arg) if prefetch_arg is not None else workers * 6,
    )
    if prefetch > MAX_PREFETCH_TARGETS:
        raise ValueError(
            f"At most {MAX_PREFETCH_TARGETS} targets may be staged for "
            "download-ahead."
        )
    download_workers_arg = getattr(args, "download_workers", None)
    download_workers = (
        int(download_workers_arg)
        if download_workers_arg is not None
        else min(2, workers)
    )
    if download_workers <= 0 or download_workers > 8:
        raise ValueError("Use between 1 and 8 download workers.")
    # Analysis threads cannot use more than one core's worth of interpreter.
    # Measured on a 16-logical-CPU machine, eight analysis threads drew 1.7
    # cores -- 10.6% of the machine -- because BLS/TLS spends most of its time
    # holding the GIL. Processes are the only way to reach the rest, at the
    # cost of one interpreter start per worker and losing the child's stage
    # detail in the in-flight panel.
    analysis_processes = int(getattr(args, "analysis_processes", 0) or 0)
    if analysis_processes < 0 or analysis_processes > 16:
        raise ValueError("Use between 0 and 16 analysis processes.")
    # The real analysis concurrency, whichever pool is in use.
    analysis_slots = analysis_processes or workers
    specs = [
        _batch_target_spec(index, row, output_dir)
        for index, row in enumerate(rows, start=1)
    ]
    results_by_index: dict[int, dict[str, object]] = {}
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    progress_path = output_dir / "batch_progress.json"
    previous_progress: dict[str, object] = {}
    if progress_path.exists():
        try:
            previous_progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous_progress = {}
    allow_legacy_reports = _legacy_checkpoint_matches(
        previous_progress,
        args=args,
        target_path=target_path,
        total_targets=len(rows),
    )
    same_campaign_checkpoint = (
        str(previous_progress.get("target_list") or "") == str(target_path)
        and int(previous_progress.get("total_targets") or 0) == len(rows)
    )
    if same_campaign_checkpoint and previous_progress.get("started_at_utc"):
        started_at = str(previous_progress["started_at_utc"])
    cache_max_gb = float(getattr(args, "cache_max_gb", 2.0))
    if not np.isfinite(cache_max_gb) or cache_max_gb <= 0:
        raise ValueError("--cache-max-gb must be a finite number greater than zero.")
    cache_max_bytes = int(cache_max_gb * 1_000_000_000)
    workspace_max_gb = getattr(args, "workspace_max_gb", None)
    if workspace_max_gb is not None and (
        not np.isfinite(float(workspace_max_gb)) or float(workspace_max_gb) <= 0
    ):
        raise ValueError(
            "--workspace-max-gb must be a finite number greater than zero."
        )
    workspace_max_bytes = (
        int(float(workspace_max_gb) * 1_000_000_000)
        if workspace_max_gb is not None
        else None
    )
    workspace_root = Path.cwd().resolve()
    workspace_headroom_bytes = (
        min(
            1_000_000_000,
            max(100_000_000, workspace_max_bytes // 20),
        )
        if workspace_max_bytes is not None
        else 0
    )
    cache_dir = resolve_cache_dir(
        os.environ.get("EXOHUNT_CACHE_DIR"),
        workspace_root=workspace_root,
    )
    # With the cache outside the workspace (the default), cache bytes no
    # longer appear in workspace accounting and the workspace ceiling tracks
    # durable evidence only.
    cache_inside_workspace = path_is_within(cache_dir, workspace_root)
    cache_retention = {
        "files_deleted": 0,
        "bytes_deleted": 0,
        "last_bytes_after": 0,
        "configured_max_bytes": cache_max_bytes,
        "effective_max_bytes": cache_max_bytes,
        "workspace_max_bytes": workspace_max_bytes,
        "workspace_headroom_bytes": workspace_headroom_bytes,
        "workspace_bytes_before": None,
        "workspace_bytes_after": None,
        "errors": [],
    }
    rolling_plot_retention = {
        "files_deleted": 0,
        "bytes_deleted": 0,
        "errors": [],
    }
    runtime_state: dict[str, object] = {
        "analysis_workers": workers,
        "analysis_processes": analysis_processes,
        "download_workers": download_workers,
        "prefetch_targets": prefetch,
        "downloads_in_flight": 0,
        "analyses_in_flight": 0,
        "downloaded_waiting": 0,
        "targets_remaining": 0,
    }
    last_progress_publish = 0.0
    last_dashboard_export = 0.0
    dashboard_export_busy = threading.Event()

    def roll_cache(results_snapshot: list[dict[str, object]] | None = None) -> None:
        # Runs on a maintenance thread during a campaign, so it must never read
        # results_by_index directly -- the scheduler mutates it concurrently and
        # iterating it here would raise "dictionary changed size during
        # iteration". The caller passes a snapshot taken on its own thread.
        if results_snapshot is None:
            results_snapshot = list(results_by_index.values())
        if not getattr(args, "retain_rejected_plots", False):
            try:
                plot_report = prune_rejected_plots(
                    results_snapshot,
                    results_root=output_dir,
                    workspace_root=workspace_root,
                )
                rolling_plot_retention["files_deleted"] += int(
                    plot_report["files_deleted"]
                )
                rolling_plot_retention["bytes_deleted"] += int(
                    plot_report["bytes_deleted"]
                )
            except Exception as exc:
                message = str(exc)
                if message not in rolling_plot_retention["errors"]:
                    rolling_plot_retention["errors"].append(message)
                    print(
                        f"rejected-plot retention warning: {message}",
                        file=sys.stderr,
                    )

        try:
            workspace_before = (
                directory_size_bytes(workspace_root)
                if workspace_max_bytes is not None
                else None
            )
            report = prune_fits_cache(
                cache_dir,
                max_bytes=cache_max_bytes,
                min_age_seconds=IN_FLIGHT_CACHE_PROTECTION_SECONDS,
            )
            effective_cache_max = cache_max_bytes
            if workspace_max_bytes is not None:
                workspace_after_initial = max(
                    0,
                    int(workspace_before or 0)
                    - int(report["bytes_deleted"]),
                )
                non_cache_bytes = (
                    max(0, workspace_after_initial - int(report["bytes_after"]))
                    if cache_inside_workspace
                    else workspace_after_initial
                )
                effective_cache_max = min(
                    cache_max_bytes,
                    max(
                        0,
                        workspace_max_bytes
                        - workspace_headroom_bytes
                        - non_cache_bytes,
                    ),
                )
                if int(report["bytes_after"]) > effective_cache_max:
                    second_report = prune_fits_cache(
                        cache_dir,
                        max_bytes=effective_cache_max,
                        min_age_seconds=IN_FLIGHT_CACHE_PROTECTION_SECONDS,
                    )
                    report["files_deleted"] = int(report["files_deleted"]) + int(
                        second_report["files_deleted"]
                    )
                    report["bytes_deleted"] = int(report["bytes_deleted"]) + int(
                        second_report["bytes_deleted"]
                    )
                    report["bytes_after"] = int(second_report["bytes_after"])

            workspace_after = (
                directory_size_bytes(workspace_root)
                if workspace_max_bytes is not None
                else None
            )
            if (
                workspace_max_bytes is not None
                and workspace_after is not None
                and workspace_after > workspace_max_bytes
            ):
                # Deliberately unprotected: this is the last valve before the
                # workspace cap is breached, so disk safety outranks losing an
                # in-flight download, which the campaign records as one failed
                # target rather than a corrupted run.
                emergency_report = prune_fits_cache(cache_dir, max_bytes=0)
                report["files_deleted"] = int(report["files_deleted"]) + int(
                    emergency_report["files_deleted"]
                )
                report["bytes_deleted"] = int(report["bytes_deleted"]) + int(
                    emergency_report["bytes_deleted"]
                )
                report["bytes_after"] = int(emergency_report["bytes_after"])
                workspace_after = directory_size_bytes(workspace_root)
            if (
                workspace_max_bytes is not None
                and workspace_after is not None
                and workspace_after > workspace_max_bytes
            ):
                raise RuntimeError(
                    "The project workspace remains above "
                    f"{workspace_max_bytes / 1_000_000_000:.2f} GB after "
                    "removing all re-downloadable cache data. Increase the "
                    "workspace limit or remove durable artifacts before resuming."
                )
        except Exception as exc:
            message = str(exc)
            if message not in cache_retention["errors"]:
                cache_retention["errors"].append(message)
                print(f"storage retention warning: {message}", file=sys.stderr)
            if workspace_max_bytes is not None:
                raise
            return
        cache_retention["files_deleted"] += int(report["files_deleted"])
        cache_retention["bytes_deleted"] += int(report["bytes_deleted"])
        cache_retention["last_bytes_after"] = int(report["bytes_after"])
        cache_retention["effective_max_bytes"] = effective_cache_max
        cache_retention["workspace_bytes_before"] = workspace_before
        cache_retention["workspace_bytes_after"] = workspace_after
        runtime_state["storage"] = {
            "workspace_bytes": workspace_after,
            "workspace_max_bytes": workspace_max_bytes,
            "workspace_headroom_bytes": (
                workspace_max_bytes - int(workspace_after)
                if workspace_max_bytes is not None and workspace_after is not None
                else None
            ),
            "download_cache_bytes": int(report["bytes_after"]),
            "download_cache_effective_max_bytes": effective_cache_max,
        }

    def ordered_results() -> list[dict[str, object]]:
        return [results_by_index[index] for index in sorted(results_by_index)]

    def _refresh_dashboard_snapshot(state: str) -> None:
        """Rebuild the dashboard snapshot off the scheduler thread, rarely.

        This was called inline on every checkpoint publish. Profiling the
        coordinator's main thread over sixty targets found it running 64 times
        for 988 s of main-thread time -- 98% of the coordinator's time was
        work, not waiting, and this was nearly all of it: 804,098 scandir
        calls, 253,662 JSON decodes, 372,220 stats. Each export re-walks the
        results tree and re-parses the whole survey, so at roughly 15 s per
        export against a 5 s publish throttle it simply ran back to back and
        left nothing for scheduling.

        It is a UI convenience -- the checkpoints remain authoritative -- so it
        runs on its own thread and no more often than the interval below. A
        final state always exports synchronously, so a finished campaign leaves
        a complete snapshot behind.
        """

        nonlocal last_dashboard_export

        def export() -> None:
            try:
                from .dashboard import export_dashboard_data

                export_dashboard_data(workspace_root)
            except Exception as exc:
                # Checkpoints stay authoritative, so a failed refresh must not
                # end the campaign -- but it must not be silent either. This
                # swallowed its errors before, which is why a stale dashboard
                # looked like a code-path bug rather than a reported failure.
                print(
                    f"dashboard snapshot refresh failed: {exc!r}",
                    file=sys.stderr,
                )
            finally:
                dashboard_export_busy.clear()

        if state != "running":
            dashboard_export_busy.clear()
            export()
            return
        now = time.monotonic()
        if now - last_dashboard_export < DASHBOARD_EXPORT_INTERVAL_SECONDS:
            return
        # A skipped refresh is not a lost one: the next publish retries, and a
        # snapshot mid-campaign is only ever a progress view.
        if dashboard_export_busy.is_set():
            return
        last_dashboard_export = now
        dashboard_export_busy.set()
        threading.Thread(
            target=export, name="exohunt-dashboard-export", daemon=True
        ).start()

    def publish_progress(state: str = "running") -> None:
        nonlocal last_progress_publish
        # Deliberately does NOT beat the coordinator lease. Liveness runs on
        # its own timer thread; beating from here as well put two threads on
        # one SQLite connection, which SQLite forbids.
        now_monotonic = time.monotonic()
        # Reports are durable per target and are rediscovered on resume, so
        # limiting large checkpoint/dashboard rewrites to the browser's polling
        # cadence loses no completed work and avoids quadratic write pressure.
        if (
            state == "running"
            and last_progress_publish
            and now_monotonic - last_progress_publish < 5.0
        ):
            return
        last_progress_publish = now_monotonic
        results = ordered_results()
        runtime_state["performance"] = _performance_snapshot(
            results,
            started_at_utc=started_at,
            total_targets=len(rows),
        )
        runtime_state["vetting_coverage"] = _vetting_coverage(results)
        progress = {
            "schema_version": 1,
            "state": state,
            "started_at_utc": started_at,
            "updated_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "target_list": str(target_path),
            "output_dir": str(output_dir),
            "total_targets": len(rows),
            "completed_targets": len(results),
            "settings": _campaign_settings(args),
            "runtime": {
                **runtime_state,
                # Which targets are being worked on right now, and on what.
                # Provenance only: it never enters a report or the ledger.
                "in_flight": TRACKER.snapshot(),
                "stages": list(STAGES),
            },
            "counts": _campaign_counts(results),
            "results": results,
        }
        _atomic_write_json(progress_path, progress)
        # A compact companion for the dashboard's polling path. The checkpoint
        # carries every result row, so a multi-thousand-target run is megabytes
        # of JSON and re-parsing it per poll cost the summary endpoint 6-12 s --
        # longer than the 5 s poll interval, so calls overlapped and the
        # frontend never got past them. This holds only the fields that panel
        # reads, and stays small for the whole run.
        _atomic_write_json(
            progress_path.with_name("batch_status.json"),
            {
                key: progress[key]
                for key in (
                    "schema_version",
                    "state",
                    "started_at_utc",
                    "updated_at_utc",
                    "target_list",
                    "total_targets",
                    "completed_targets",
                    "counts",
                    "runtime",
                )
                if key in progress
            }
            # Derived here because the reader would otherwise have to walk
            # every result row to recover it.
            | {
                "sectors": sorted(
                    {
                        int(sector)
                        for row in results
                        for sector in str(row.get("sectors") or "").split(";")
                        if sector.strip().isdigit()
                    }
                )
            },
        )
        _publish_followup_queue(output_dir, results)
        _refresh_dashboard_snapshot(state)

    checkpoint_rows = (
        {}
        if args.force
        else _reusable_checkpoint_rows(
            previous_progress,
            specs=specs,
            args=args,
            target_path=target_path,
            output_dir=output_dir,
        )
    )
    pending_specs: deque[dict[str, object]] = deque()
    for spec in specs:
        checkpoint_row = checkpoint_rows.get(int(spec["index"]))
        if checkpoint_row is not None:
            results_by_index[int(spec["index"])] = checkpoint_row
            continue
        try:
            report = (
                None
                if args.force
                else _load_reusable_report(
                    Path(spec["expected_report"]),
                    target=str(spec["target"]),
                    tic_id=int(spec["tic_id"]),
                    sectors=list(spec["sectors"]),
                    args=args,
                    allow_legacy=allow_legacy_reports,
                )
            )
            if report is not None:
                results_by_index[int(spec["index"])] = _result_row_from_report(
                    report,
                    target=str(spec["target"]),
                    tic_id=int(spec["tic_id"]),
                    sectors=list(spec["sectors"]),
                    expected_report=Path(spec["expected_report"]),
                    run_state="resumed",
                )
            else:
                pending_specs.append(spec)
        except Exception as exc:
            pending_specs.append(spec)

    runtime_state["targets_remaining"] = len(pending_specs)
    terminal_resume_storage = _terminal_resume_storage_snapshot(
        previous_progress,
        pending_targets=len(pending_specs),
    )
    fast_terminal_resume = (
        bool(checkpoint_rows) and terminal_resume_storage is not None
    )
    if fast_terminal_resume:
        # A terminal retry with at most a handful of missing rows just finished
        # a measured retention pass. Re-walking the 90 GB cache and the entire
        # workspace before and after those rows cost twenty-plus minutes. The
        # checkpoint's bounded storage snapshot proves enough headroom for the
        # small retry; the explicit rejected-plot pass still runs at finalization.
        runtime_state["storage"] = dict(terminal_resume_storage)
        cache_retention["last_bytes_after"] = int(
            terminal_resume_storage.get("download_cache_bytes") or 0
        )
        cache_retention["effective_max_bytes"] = int(
            terminal_resume_storage.get("download_cache_effective_max_bytes")
            or cache_max_bytes
        )
        cache_retention["workspace_bytes_before"] = terminal_resume_storage.get(
            "workspace_bytes"
        )
        cache_retention["workspace_bytes_after"] = terminal_resume_storage.get(
            "workspace_bytes"
        )
    else:
        # Enforce storage before any new download is submitted. Subsequent rolling
        # passes preserve headroom for the bounded prefetch queue.
        roll_cache()
    publish_progress()

    download_futures: dict[Future, dict[str, object]] = {}
    analysis_futures: dict[Future, dict[str, object]] = {}
    # Service times, so "are we download-bound?" is answerable from measurement
    # rather than inferred from queue depths. Queue depth alone is ambiguous:
    # a full buffer can mean downloads are fast or that the prefetch ceiling is
    # holding them back.
    future_started: dict[Future, float] = {}
    download_seconds: deque[float] = deque(maxlen=200)
    analysis_seconds: deque[float] = deque(maxlen=200)
    downloaded_waiting: deque[
        tuple[dict[str, object], tuple[np.ndarray, np.ndarray, dict[str, object]]]
    ] = deque()
    completed_since_prune = 0
    cache_prune_due = False

    def _median(values: deque[float]) -> float | None:
        if not values:
            return None
        return round(float(np.median(np.fromiter(values, dtype=float))), 2)

    def refresh_runtime() -> None:
        # Per-target service times against worker counts say which side is the
        # limit: the throughput a stage can sustain alone is workers/median.
        download_median = _median(download_seconds)
        analysis_median = _median(analysis_seconds)
        runtime_state.update(
            {
                "download_seconds_median": download_median,
                "analysis_seconds_median": analysis_median,
                "download_capacity_per_hour": (
                    round(3600.0 * download_workers / download_median)
                    if download_median
                    else None
                ),
                # Must use the real concurrency, which is the process count
                # when running a process pool, not the thread-worker setting.
                "analysis_capacity_per_hour": (
                    round(3600.0 * analysis_slots / analysis_median)
                    if analysis_median
                    else None
                ),
                "downloads_in_flight": len(download_futures),
                "analyses_in_flight": len(analysis_futures),
                "downloaded_waiting": len(downloaded_waiting),
                "targets_remaining": (
                    len(pending_specs)
                    + len(download_futures)
                    + len(downloaded_waiting)
                    + len(analysis_futures)
                ),
            }
        )

    def submit_downloads(executor: ThreadPoolExecutor) -> None:
        # A pending cache prune deliberately does *not* stop new downloads.
        # It used to: the flag was set every ten completions and this returned
        # early until the whole pipeline had drained to zero in-flight work, so
        # the read-ahead buffer collapsed to empty every ten targets and the
        # analysers then waited on a cold download queue. `roll_cache` now
        # protects recently written files instead, which makes it safe to prune
        # while downloads are in flight.
        staged = (
            len(download_futures)
            + len(downloaded_waiting)
            + len(analysis_futures)
        )
        while (
            pending_specs
            and len(download_futures) < download_workers
            and staged < prefetch
        ):
            spec = pending_specs.popleft()
            future = executor.submit(_download_batch_target, spec, args)
            download_futures[future] = spec
            future_started[future] = time.monotonic()
            staged += 1

    def submit_analyses(executor) -> None:
        while downloaded_waiting and len(analysis_futures) < analysis_slots:
            spec, downloaded = downloaded_waiting.popleft()
            if analysis_processes:
                # The child owns its own tracker, so its stage changes never
                # reach this process. Mark the coarse stage here rather than
                # leaving the target reading "staged" for its whole analysis.
                TRACKER.stage(int(spec["tic_id"]), "searching")
                downloaded = _plain_arrays_for_transport(downloaded)
            future = executor.submit(
                _analyze_downloaded_batch_target,
                spec,
                args,
                downloaded,
                output_dir,
            )
            analysis_futures[future] = spec
            future_started[future] = time.monotonic()

    def record_result(spec: dict[str, object], result_row: dict[str, object]) -> None:
        nonlocal completed_since_prune, cache_prune_due
        result_row.setdefault(
            "completed_at_utc",
            datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )
        results_by_index[int(spec["index"])] = result_row
        completed_since_prune += 1
        if completed_since_prune >= 10:
            cache_prune_due = True
        refresh_runtime()
        publish_progress()
        completed = len(results_by_index)
        print(
            f"[{completed}/{len(rows)}] {spec['target']}: {result_row['status']}"
            + (
                f" / {result_row.get('screening_class', 'unclassified')} "
                f"at {float(result_row['period_days']):.5f} d, "
                f"S/N {float(result_row['depth_snr']):.2f}"
                if "period_days" in result_row
                else f" ({result_row.get('error', 'unknown error')})"
            )
        )

    maintenance_future: Future | None = None
    last_prune_finished = time.monotonic()

    with (
        ThreadPoolExecutor(
            max_workers=download_workers,
            thread_name_prefix="exohunt-download",
        ) as download_executor,
        _analysis_executor(workers, analysis_processes) as analysis_executor,
        ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="exohunt-maintenance",
        ) as maintenance_executor,
    ):
        submit_downloads(download_executor)
        while (
            pending_specs
            or download_futures
            or downloaded_waiting
            or analysis_futures
        ):
            # A finished prune reports here. Storage failures are fatal by
            # design, so the exception has to surface on this thread rather
            # than being swallowed by the executor.
            if maintenance_future is not None and maintenance_future.done():
                maintenance_future.result()
                maintenance_future = None
                last_prune_finished = time.monotonic()
            if (
                cache_prune_due
                and maintenance_future is None
                and time.monotonic() - last_prune_finished
                >= MINIMUM_PRUNE_INTERVAL_SECONDS
            ):
                # Off the scheduler thread. roll_cache walks the whole cache and
                # sizes the entire workspace twice; measured at 68,803 workspace
                # files that is about 15 s, and running it here stalled every
                # submission for that long once per ten completions. It also got
                # slower as the workspace grew, so throughput decayed over a run.
                maintenance_future = maintenance_executor.submit(
                    roll_cache, list(results_by_index.values())
                )
                completed_since_prune = 0
                cache_prune_due = False
            submit_analyses(analysis_executor)
            submit_downloads(download_executor)
            refresh_runtime()
            active_futures = set(download_futures) | set(analysis_futures)
            if not active_futures:
                if maintenance_future is not None:
                    # Nothing left to schedule until the prune frees headroom.
                    maintenance_future.result()
                    maintenance_future = None
                    last_prune_finished = time.monotonic()
                    submit_downloads(download_executor)
                    continue
                raise RuntimeError("Parallel batch scheduler stalled without active work.")
            done, _ = wait(active_futures, return_when=FIRST_COMPLETED)
            for future in done:
                if future in download_futures:
                    spec = download_futures.pop(future)
                    started = future_started.pop(future, None)
                    if started is not None:
                        download_seconds.append(time.monotonic() - started)
                    try:
                        downloaded_waiting.append((spec, future.result()))
                    except Exception as exc:
                        record_result(spec, _batch_error_row(spec, exc))
                    else:
                        # It is downloaded and queued, not still downloading.
                        TRACKER.stage(int(spec["tic_id"]), "staged")
                else:
                    spec = analysis_futures.pop(future)
                    started = future_started.pop(future, None)
                    if started is not None:
                        analysis_seconds.append(time.monotonic() - started)
                    # Clear the in-flight entry here, in the coordinator.
                    # `_analyze_downloaded_batch_target` also calls finish, but
                    # under a process pool that runs in the child against the
                    # child's own registry, so the coordinator's entry survived
                    # forever: the panel accumulated every target as
                    # permanently "searching" and the registry grew without
                    # bound. Clearing twice is harmless -- finish() ignores a
                    # target it does not hold.
                    TRACKER.finish(int(spec["tic_id"]))
                    try:
                        result_row = future.result()
                    except Exception as exc:
                        result_row = _batch_error_row(spec, exc)
                    record_result(spec, result_row)

        # Settle any prune still running so a storage failure raises here
        # rather than being swallowed by the executor shutdown below.
        if maintenance_future is not None:
            maintenance_future.result()
            maintenance_future = None

    if not fast_terminal_resume:
        roll_cache()
    results = ordered_results()
    common_mode_screen = _quarantine_invalid_common_mode(results)
    _publish_followup_queue(output_dir, results)

    rejected_plot_retention: dict[str, object] = {
        "files_deleted": 0,
        "bytes_deleted": 0,
        "retained_by_request": bool(getattr(args, "retain_rejected_plots", False)),
    }
    if not getattr(args, "retain_rejected_plots", False):
        try:
            plot_report = prune_rejected_plots(
                results,
                results_root=output_dir,
                workspace_root=Path.cwd(),
            )
            deleted_paths = set(str(value) for value in plot_report["deleted_paths"])
            for row in results:
                if row.get("plot"):
                    raw = Path(str(row["plot"]))
                    resolved = (raw if raw.is_absolute() else Path.cwd() / raw).resolve()
                    row["plot_retained"] = str(resolved) not in deleted_paths
            rejected_plot_retention = {
                key: value for key, value in plot_report.items() if key != "deleted_paths"
            }
            rejected_plot_retention["retained_by_request"] = False
        except Exception as exc:
            rejected_plot_retention = {
                "files_deleted": 0,
                "bytes_deleted": 0,
                "retained_by_request": False,
                "error": str(exc),
            }

    publish_progress("finalizing")
    summary = {
        "target_list": str(target_path),
        "settings": _campaign_settings(args),
        "counts": _campaign_counts(results),
        "vetting_coverage": _vetting_coverage(results),
        "campaign_level_screening": {"common_mode": common_mode_screen},
        "storage_retention": {
            "fits_cache": cache_retention,
            "rejected_plots": {
                **rejected_plot_retention,
                "rolling_files_deleted": rolling_plot_retention["files_deleted"],
                "rolling_bytes_deleted": rolling_plot_retention["bytes_deleted"],
                "rolling_errors": rolling_plot_retention["errors"],
            },
        },
        "results": results,
    }
    summary_path = output_dir / "batch_summary.json"
    _atomic_write_json(summary_path, summary)
    _publish_dip_registries(output_dir, results)
    csv_path = output_dir / "batch_summary.csv"
    fieldnames = sorted({key for row in results for key in row})
    temporary_csv = csv_path.with_name(csv_path.name + ".tmp")
    with temporary_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    _replace_with_retry(temporary_csv, csv_path)
    _, stats = record_campaign(summary_path)
    publish_progress(
        "completed" if int(summary["counts"]["error"]) == 0 else "retry_pending"
    )
    print(f"\nSaved {summary_path} and {csv_path}")
    print(f"Metrics snapshot: {json.dumps(stats, sort_keys=True)}")
    return 1 if summary["counts"]["error"] else 0


# Cohort registries are not about one star, but the evidence table is keyed
# by star. A sentinel id keeps them in the same append-only store the
# dashboard already reads without inventing a fake star row: the rows carry
# no verdict and never vote, so `star_state` and the star table are
# untouched.
COHORT_EVIDENCE_TIC_ID = 0


def _publish_dip_registries(
    output_dir: Path,
    results: list[dict[str, object]],
) -> dict[str, object]:
    """Build and publish this campaign's absolute-time dip registries.

    Derived from the per-target reports rather than in-memory state, so a
    resumed campaign publishes the same registry a fresh one would, and the
    result stays re-derivable later without photometry (T4 is defined as pure
    post-processing).

    Publication never fails a finished campaign: the science is already on
    disk, and a control-plane problem must not turn a completed cohort into
    an error.
    """

    from . import cli as cli_module
    from .population import registries_from_reports

    payload: dict[str, object] = {"schema_version": 1, "cohorts": {}}
    try:
        keyed: list[tuple[str, object]] = []

        def load_bins(row: dict[str, object]) -> tuple[str, object] | None:
            report_path = row.get("report")
            if not report_path:
                return None
            # Result rows record the report relative to the workspace root,
            # which is what the dashboard exporter also assumes; fall back to
            # the campaign directory so an absolute or relocated path still
            # resolves.
            path = Path(str(report_path))
            if not path.is_absolute() and not path.exists():
                path = output_dir / path.name
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            bins = report.get("population_bins")
            if not isinstance(bins, dict):
                return None
            return str(bins.get("cohort") or "unknown"), bins

        # Report files are independent, small, and already durable. Reading
        # 64,000 of them serially made the registry dominate terminal
        # publication (about thirteen minutes). Keep submission bounded so the
        # speedup does not create 64,000 Future objects at once.
        with ThreadPoolExecutor(
            max_workers=min(16, max(4, (os.cpu_count() or 4))),
            thread_name_prefix="dip-registry-read",
        ) as executor:
            for start in range(0, len(results), 512):
                for loaded in executor.map(load_bins, results[start : start + 512]):
                    if loaded is not None:
                        keyed.append(loaded)
        payload["cohorts"] = registries_from_reports(keyed)
        payload["stars_contributing"] = len(keyed)
        payload["scope"] = (
            "Absolute-time windows where an improbable fraction of stars "
            "observed on the same detector dipped together. A window marks "
            "an observatory event; its absence does not make an event "
            "astrophysical."
        )
        cli_module._atomic_write_json(output_dir / "dip_registry.json", payload)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"warning: could not build the dip registry ({exc})", file=sys.stderr)
        return payload

    _record_dip_registry_evidence(output_dir, payload)
    return payload


def _record_dip_registry_evidence(
    output_dir: Path,
    payload: dict[str, object],
) -> None:
    """Append each cohort registry to the ledger for the systematics view."""

    cohorts = payload.get("cohorts")
    if not isinstance(cohorts, dict) or not cohorts:
        return
    try:
        from . import ledger
        from .config import CURRENT_CONFIG

        # Section 3.6 wants the window list versioned like a catalog
        # snapshot. The config hash is that version: it changes exactly when
        # a threshold that could change the windows changes.
        signature = f"dip-registry:{CURRENT_CONFIG.config_hash()}"
    except Exception:
        return
    conn = None
    try:
        conn = ledger.connect()
        for key, registry in sorted(cohorts.items()):
            ledger.append_evidence(
                conn,
                tic_id=COHORT_EVIDENCE_TIC_ID,
                kind="dip_registry",
                source=f"dip_registry:{output_dir.name}:{key}",
                payload=dict(registry),
                verdict=None,
                affects_state=False,
                signature=signature,
            )
        conn.commit()
    except Exception as exc:  # pragma: no cover - defensive
        print(
            f"warning: could not record dip registry evidence ({exc})",
            file=sys.stderr,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _read_target_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"target", "tic_id", "sectors"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                "Target CSV is missing required columns: " + ", ".join(sorted(missing))
            )
        rows: list[dict[str, str]] = []
        identities: set[tuple[int, tuple[int, ...]]] = set()
        for row_number, row in enumerate(reader, start=2):
            target = str(row.get("target") or "").strip()
            try:
                tic_id = int(str(row.get("tic_id") or "").strip())
            except ValueError as exc:
                raise ValueError(
                    f"Target CSV row {row_number} has an invalid TIC ID."
                ) from exc
            try:
                sectors = tuple(
                    sorted(
                        {
                            int(value.strip())
                            for value in str(row.get("sectors") or "").replace(
                                ",", ";"
                            ).split(";")
                            if value.strip()
                        }
                    )
                )
            except ValueError as exc:
                raise ValueError(
                    f"Target CSV row {row_number} has an invalid sector list."
                ) from exc
            if not target or tic_id <= 0 or not sectors or any(value <= 0 for value in sectors):
                raise ValueError(
                    f"Target CSV row {row_number} requires a target name, a positive "
                    "TIC ID, and at least one positive sector."
                )
            identity = (tic_id, sectors)
            if identity in identities:
                raise ValueError(
                    f"Target CSV row {row_number} duplicates TIC {tic_id} in "
                    f"sector(s) {';'.join(str(value) for value in sectors)}."
                )
            identities.add(identity)
            rows.append(
                {
                    **{str(key): str(value or "") for key, value in row.items()},
                    "target": target,
                    "tic_id": str(tic_id),
                    "sectors": ";".join(str(value) for value in sectors),
                }
            )
        return rows


def _batch_target_spec(
    index: int,
    row: dict[str, str],
    output_dir: Path,
) -> dict[str, object]:
    from . import cli as cli_module

    target = row["target"]
    tic_id = int(row["tic_id"])
    sectors = [int(value) for value in row["sectors"].split(";") if value]
    stem = cli_module._artifact_stem(target, tic_id, sectors)
    stellar_parameters = {
        key: value
        for key in (
            "stellar_radius_solar",
            "stellar_mass_solar",
        )
        if (value := cli_module._optional_float(row.get(key))) is not None
        and value > 0
    }
    # Detector identity decides which stars can share an observatory event,
    # so it has to reach the report the T4 registry is built from. Target
    # lists that omit these columns leave the cohort at sector scope, which
    # the registry records explicitly rather than guessing a detector.
    for key in ("camera", "ccd"):
        value = cli_module._optional_float(row.get(key))
        if value is not None and value > 0:
            stellar_parameters[key] = int(value)
    return {
        "index": index,
        "target": target,
        "tic_id": tic_id,
        "sectors": sectors,
        "expected_report": output_dir / f"{stem}.json",
        **stellar_parameters,
    }


def _campaign_settings(args: argparse.Namespace) -> dict[str, object]:
    from . import cli as cli_module

    workers = max(1, int(getattr(args, "workers", 1)))
    download_workers_arg = getattr(args, "download_workers", None)
    download_workers = (
        max(1, int(download_workers_arg))
        if download_workers_arg is not None
        else min(2, workers)
    )
    prefetch = getattr(args, "prefetch", None)
    prefetch = max(workers, int(prefetch) if prefetch is not None else workers * 6)
    return {
        **cli_module._scientific_settings(args),
        "execution": {
            "analysis_workers": workers,
            "download_workers": download_workers,
            "prefetch_targets": prefetch,
            "checkpoint_writer": "single coordinator",
        },
        "storage_retention": {
            "fits_cache_max_gb": float(getattr(args, "cache_max_gb", 2.0)),
            "workspace_max_gb": (
                float(args.workspace_max_gb)
                if getattr(args, "workspace_max_gb", None) is not None
                else None
            ),
            "retain_rejected_plots": bool(
                getattr(args, "retain_rejected_plots", False)
            ),
            "durable_artifacts": [
                "metrics ledger",
                "campaign JSON/CSV summaries",
                "per-target JSON diagnostics",
                "survivor plots",
            ],
        },
    }


def _campaign_counts(results: list[dict[str, object]]) -> dict[str, int]:
    return {
        status: sum(row.get("status") == status for row in results)
        for status in ("survivor", "rejected", "error")
    }


def _vetting_coverage(results: list[dict[str, object]]) -> dict[str, object]:
    """Report mixed legacy/new vetting cohorts without implying retroactive checks."""

    tier_counts: dict[str, int] = {}
    pipeline_version_counts: dict[str, int] = {}
    for row in results:
        tier = str(row.get("vetting_tier") or "legacy_unmeasured")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        pipeline_version = str(
            row.get("data_pipeline_version") or "legacy_unversioned"
        )
        pipeline_version_counts[pipeline_version] = (
            pipeline_version_counts.get(pipeline_version, 0) + 1
        )
    legacy = tier_counts.get("legacy_unmeasured", 0)
    retry_required = tier_counts.get("retry_required", 0)
    eligible = max(0, len(results) - retry_required)
    measured = max(0, eligible - legacy)
    return {
        "eligible_targets": eligible,
        "measured_targets": measured,
        "legacy_unmeasured_targets": legacy,
        "coverage_fraction": round(measured / eligible, 4) if eligible else None,
        "tier_counts": dict(sorted(tier_counts.items())),
        "pipeline_version_counts": dict(sorted(pipeline_version_counts.items())),
        "warning": (
            "Legacy-unmeasured rows were completed before deeper vetting existed "
            "and have not been retroactively reprocessed."
            if legacy
            else None
        ),
    }


def _performance_snapshot(
    results: list[dict[str, object]],
    *,
    started_at_utc: str,
    total_targets: int,
    now: datetime | None = None,
    rolling_minutes: float = 15.0,
) -> dict[str, object]:
    """Summarize campaign throughput without treating reused rows as new work."""

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        started = datetime.fromisoformat(started_at_utc.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        started = current
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed_hours = max((current - started).total_seconds() / 3600.0, 1 / 3600)
    completed = len(results)
    average_rate = completed / elapsed_hours

    completion_times: list[datetime] = []
    cutoff = current - timedelta(minutes=rolling_minutes)
    for row in results:
        value = row.get("completed_at_utc")
        if not value:
            continue
        try:
            completed_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        if cutoff <= completed_at <= current + timedelta(minutes=1):
            completion_times.append(completed_at)
    completion_times.sort()
    if len(completion_times) >= 2:
        rolling_span_hours = max(
            (completion_times[-1] - completion_times[0]).total_seconds() / 3600.0,
            1 / 3600,
        )
        rolling_rate: float | None = (
            (len(completion_times) - 1) / rolling_span_hours
        )
    else:
        rolling_rate = None

    effective_rate = rolling_rate if rolling_rate and rolling_rate > 0 else average_rate
    remaining = max(0, total_targets - completed)
    eta_hours = remaining / effective_rate if effective_rate > 0 else None
    estimated_completion = (
        current + timedelta(hours=eta_hours)
        if eta_hours is not None
        else None
    )
    return {
        "average_stars_per_hour": round(average_rate, 1),
        "rolling_stars_per_hour": (
            round(rolling_rate, 1) if rolling_rate is not None else None
        ),
        "rolling_window_minutes": float(rolling_minutes),
        "rolling_samples": len(completion_times),
        "elapsed_hours": round(elapsed_hours, 2),
        "eta_hours": round(eta_hours, 2) if eta_hours is not None else None,
        "estimated_completion_utc": (
            estimated_completion.replace(microsecond=0).isoformat()
            if estimated_completion is not None
            else None
        ),
    }


def _is_transient_search_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    module = type(exc).__module__.lower()
    message = str(exc).lower()
    return bool(
        isinstance(exc, (TimeoutError, ConnectionError))
        or "timeout" in name
        or "connection" in name
        or module.startswith(("requests", "urllib3"))
        or any(
            marker in message
            for marker in (
                "timed out",
                "temporary failure",
                "temporarily unavailable",
                "connection reset",
                "connection aborted",
                "remote end closed",
                "too many requests",
                "http 429",
                "http 502",
                "http 503",
                "http 504",
                "bad magic number",
                "bad crc-32",
                "file name in directory",
                "process cannot access the file",
                "invalid argument",
                "codec can't decode byte",
            )
        )
    )


LEGACY_COMMON_MODE_REASON = (
    "transit midpoint is shared by at least five campaign targets within 0.75 day"
)
LEGACY_COMMON_MODE_REASONS = {
    LEGACY_COMMON_MODE_REASON,
    "transit midpoint is shared by at least three campaign targets",
}


def _quarantine_invalid_common_mode(
    results: list[dict[str, object]],
) -> dict[str, object]:
    """Remove the invalid large-campaign midpoint-density veto.

    A single fitted BLS reference epoch is not a measured common-mode event.
    Cadence-level detector/background evidence is required before a campaign
    screen may automatically reject a target.
    """

    repaired = 0
    for row in results:
        if row.get("status") == "error":
            continue
        original_reasons = [
            value.strip()
            for value in str(row.get("rejection_reasons", "")).split(";")
            if value.strip()
        ]
        reasons = [
            value
            for value in original_reasons
            if value not in LEGACY_COMMON_MODE_REASONS
        ]
        had_legacy_veto = len(reasons) != len(original_reasons)
        if had_legacy_veto:
            repaired += 1
            row["rejection_reasons"] = "; ".join(reasons)
            row["status"] = "rejected" if reasons else "survivor"
        row.pop("common_mode_peer_count", None)
        row["campaign_common_mode_screen"] = "not applied"
    return {
        "status": "quarantined",
        "automatic_rejection_applied": False,
        "legacy_rows_repaired": repaired,
        "reason": (
            "The former single-midpoint density rule is invalid at campaign scale. "
            "Common-mode rejection now requires future cadence-level detector or "
            "background evidence."
        ),
    }


def _legacy_checkpoint_matches(
    progress: dict[str, object],
    *,
    args: argparse.Namespace,
    target_path: Path,
    total_targets: int,
) -> bool:
    from . import cli as cli_module

    settings = progress.get("settings")
    if not isinstance(settings, dict):
        return False
    expected = cli_module._scientific_settings(args)
    return bool(
        str(progress.get("target_list")) == str(target_path)
        and int(progress.get("total_targets", -1)) == total_targets
        and settings.get("author") == expected["author"]
        and float(settings.get("cadence_seconds", -1))
        == float(expected["cadence_seconds"])
        and list(settings.get("period_range_days", []))
        == list(expected["period_range_days"])
        and float(settings.get("mask_width", -1)) == float(expected["mask_width"])
        and bool(settings.get("allow_no_known")) == bool(expected["allow_no_known"])
    )


def _reusable_checkpoint_rows(
    progress: dict[str, object],
    *,
    specs: list[dict[str, object]],
    args: argparse.Namespace,
    target_path: Path,
    output_dir: Path,
) -> dict[int, dict[str, object]]:
    """Reuse an atomic checkpoint without reopening every report JSON.

    A completed large campaign already paid the full report-validation cost
    before atomically publishing ``batch_progress.json``. Repeating 64,000
    JSON opens merely to retry a handful of error rows took 22 minutes on the
    production workspace. The checkpoint is a safe fast path only when its
    complete campaign identity still matches and every successful row still
    has the expected durable artifact name in one directory inventory.

    Error rows are never reused. Any identity or artifact mismatch falls back
    to the ordinary per-report validation path for that target.
    """

    from . import cli as cli_module

    if (
        str(progress.get("target_list") or "") != str(target_path)
        or int(progress.get("total_targets") or 0) != len(specs)
        or not cli_module._legacy_checkpoint_matches(
            progress,
            args=args,
            target_path=target_path,
            total_targets=len(specs),
        )
        or progress.get("state")
        not in {"running", "interrupted", "completed", "retry_pending"}
    ):
        return {}
    rows = progress.get("results")
    if not isinstance(rows, list) or len(rows) > len(specs):
        return {}
    try:
        artifact_names = {entry.name for entry in output_dir.iterdir()}
    except OSError:
        return {}

    specs_by_identity = {
        (
            int(spec["tic_id"]),
            tuple(sorted(int(value) for value in spec["sectors"])),
        ): spec
        for spec in specs
    }
    reusable: dict[int, dict[str, object]] = {}
    for raw_row in rows:
        if not isinstance(raw_row, dict) or raw_row.get("status") == "error":
            continue
        expected_report: Path | None = None
        try:
            row_sectors = sorted(
                int(value.strip())
                for value in str(raw_row.get("sectors") or "")
                .replace(",", ";")
                .split(";")
                if value.strip()
            )
            spec = specs_by_identity.get(
                (int(raw_row.get("tic_id") or 0), tuple(row_sectors))
            )
            if spec is None:
                continue
            expected_report = Path(spec["expected_report"])
            identity_matches = (
                str(raw_row.get("target")) == str(spec["target"])
                and int(raw_row.get("tic_id") or 0) == int(spec["tic_id"])
                and row_sectors == cli_module._sector_values(spec["sectors"])
                and bool(raw_row.get("scientific_configuration_verified"))
                and Path(str(raw_row.get("report") or "")).name
                == expected_report.name
            )
        except (TypeError, ValueError):
            identity_matches = False
        if (
            not identity_matches
            or expected_report is None
            or expected_report.name not in artifact_names
        ):
            continue
        if (
            raw_row.get("status") == "survivor"
            and expected_report.with_suffix(".png").name not in artifact_names
        ):
            continue
        row = dict(raw_row)
        row["run_state"] = "resumed"
        reusable[int(spec["index"])] = row
    return reusable


def _terminal_resume_storage_snapshot(
    progress: dict[str, object],
    *,
    pending_targets: int,
) -> dict[str, object] | None:
    """Return a safe recent storage snapshot for a tiny terminal retry."""

    if pending_targets < 0 or pending_targets > 10:
        return None
    runtime = progress.get("runtime")
    storage = runtime.get("storage") if isinstance(runtime, dict) else None
    if not isinstance(storage, dict):
        return None
    try:
        cache_bytes = int(storage["download_cache_bytes"])
        cache_max = int(storage["download_cache_effective_max_bytes"])
        workspace_bytes = int(storage["workspace_bytes"])
        workspace_max = int(storage["workspace_max_bytes"])
    except (KeyError, TypeError, ValueError):
        return None
    # One processed TESS light curve is tens of MB. Keep an additional 100 MB
    # floor so a snapshot exactly at its ceiling never authorizes a retry.
    required_cache_headroom = pending_targets * 50_000_000 + 100_000_000
    if cache_max - cache_bytes < required_cache_headroom:
        return None
    if workspace_max - workspace_bytes < 100_000_000:
        return None
    return dict(storage)


def _load_reusable_report(
    report_path: Path,
    *,
    target: str,
    tic_id: int,
    sectors: list[int],
    args: argparse.Namespace,
    allow_legacy: bool,
) -> dict[str, object] | None:
    from . import cli as cli_module

    plot_path = report_path.with_suffix(".png")
    if not report_path.exists():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    data = report.get("data")
    if not isinstance(data, dict):
        return None
    if (
        str(data.get("target")) != target
        or int(data.get("tic_id") or 0) != tic_id
        or cli_module._sector_values(data.get("requested_sectors"))
        != cli_module._sector_values(sectors)
        or str(data.get("requested_author") or data.get("author"))
        != str(args.author)
        or float(data.get("requested_cadence_seconds") or -1)
        != float(args.cadence_seconds)
    ):
        return None
    configuration = report.get("search_configuration")
    if configuration is None:
        configuration_matches = allow_legacy
    else:
        configuration_matches = configuration == cli_module._scientific_settings(args)
    if not configuration_matches:
        return None
    triage = report.get("automated_triage")
    is_rejected = isinstance(triage, dict) and triage.get("passes") is False
    # Rejected plots are intentionally pruned by the storage policy after a
    # completed campaign. The JSON report is written only after the plot was
    # successfully created, so it remains a valid completion marker. Survivor
    # plots are durable and must still exist before a survivor is reused.
    if not is_rejected and not plot_path.exists():
        return None
    return report


def _result_row_from_report(
    report: dict[str, object],
    *,
    target: str,
    tic_id: int,
    sectors: list[int],
    expected_report: Path,
    run_state: str,
) -> dict[str, object]:
    signal = dict(report["strongest_residual_signal"])
    triage = dict(report["automated_triage"])
    rejection_reasons = [
        str(value).strip()
        for value in triage.get("rejection_reasons", [])
        if str(value).strip()
    ]
    classification = report.get("followup_classification")
    if not isinstance(classification, dict):
        classification = _classify_screening_result(
            argparse.Namespace(**signal),
            rejection_reasons,
        )
    sensitivity = report.get("sensitivity_probe")
    sensitivity = sensitivity if isinstance(sensitivity, dict) else None
    deeper_vetting = report.get("deeper_vetting")
    deeper_vetting = (
        deeper_vetting if isinstance(deeper_vetting, dict) else None
    )
    report_configuration = report.get("search_configuration")
    report_configuration = (
        report_configuration if isinstance(report_configuration, dict) else None
    )
    try:
        completed_at_utc = (
            datetime.fromtimestamp(expected_report.stat().st_mtime, timezone.utc)
            .replace(microsecond=0)
            .isoformat()
        )
    except OSError:
        completed_at_utc = (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        )
    return {
        "target": target,
        "tic_id": tic_id,
        "sectors": ";".join(str(value) for value in sectors),
        "run_state": run_state,
        "completed_at_utc": completed_at_utc,
        "status": "survivor" if triage["passes"] else "rejected",
        "screening_class": classification["screening_class"],
        "followup_priority": int(classification["followup_priority"]),
        "followup_reasons": "; ".join(classification["followup_reasons"]),
        "vetting_tier": classification.get("vetting_tier", "legacy_unmeasured"),
        "data_pipeline_version": (
            report_configuration.get("data_pipeline_version", "unversioned")
            if report_configuration is not None
            else "legacy_unversioned"
        ),
        "scientific_configuration_verified": report_configuration is not None,
        "deeper_vetting_flags": "; ".join(
            str(value)
            for value in classification.get("deeper_vetting_flags", [])
        ),
        "recommended_data_sources": "; ".join(
            str(value)
            for value in classification.get("recommended_data_sources", [])
        ),
        "planet_free": False,
        "period_days": signal["period_days"],
        "depth_ppm": signal["depth_ppm"],
        "depth_snr": signal["depth_snr"],
        "observed_transits": signal["observed_transits"],
        "transit_time": signal["transit_time"],
        "duration_hours": signal["duration_hours"],
        "rejection_reasons": "; ".join(rejection_reasons),
        "report": str(expected_report),
        "plot": str(expected_report.with_suffix(".png")),
        "phase_curve_available": isinstance(report.get("phase_curve"), dict),
        "sensitivity_3d_ppm": _sensitivity_depth_at_period(sensitivity, 3.0),
        "sensitivity_12d_ppm": _sensitivity_depth_at_period(sensitivity, 12.0),
        "red_noise_adjusted_snr": (
            deeper_vetting.get("red_noise_adjusted_snr")
            if deeper_vetting is not None
            else None
        ),
        "event_coverage_fraction": (
            deeper_vetting.get("event_coverage_fraction")
            if deeper_vetting is not None
            else None
        ),
        "positive_depth_event_fraction": (
            deeper_vetting.get("positive_depth_event_fraction")
            if deeper_vetting is not None
            else None
        ),
    }


def _batch_error_row(
    spec: dict[str, object],
    exc: Exception,
) -> dict[str, object]:
    return {
        "target": spec["target"],
        "tic_id": spec["tic_id"],
        "sectors": ";".join(str(value) for value in spec["sectors"]),
        "run_state": "error",
        "completed_at_utc": (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        ),
        "status": "error",
        "screening_class": "search_error",
        "followup_priority": 100,
        "followup_reasons": "retry data retrieval or analysis",
        "vetting_tier": "retry_required",
        "data_pipeline_version": "not_completed",
        "scientific_configuration_verified": False,
        "deeper_vetting_flags": "",
        "recommended_data_sources": "",
        "planet_free": False,
        "error": str(exc),
    }


def _download_batch_target(
    spec: dict[str, object],
    args: argparse.Namespace,
) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    from . import cli as cli_module

    namespace = (
        f"TIC_{int(spec['tic_id'])}_s"
        + "-".join(str(value) for value in spec["sectors"])
    )
    try:
        download_dir = (
            resolve_cache_dir(
                os.environ.get("EXOHUNT_CACHE_DIR"), workspace_root=Path.cwd()
            )
            / "batch_targets"
            / cli_module._safe_name(namespace)
        )
    except Exception:
        download_dir = None
    TRACKER.begin(
        int(spec["tic_id"]),
        target=str(spec["target"]),
        stage="downloading",
        download_dir=download_dir,
    )
    for attempt in range(1, 4):
        try:
            return cli_module._download_light_curve(
                str(spec["target"]),
                list(spec["sectors"]),
                args.author,
                args.cadence_seconds,
                cache_namespace=namespace,
                # Detrending is CPU work and this runs on the coordinator's
                # download threads, where the GIL serialises it. Leaving the
                # curve unprepared moves that cost to the analysis worker,
                # which has capacity; the coordinator was pinned at 150% of a
                # core while eight workers idled at 2% each.
                flatten=False,
            )
        except Exception as exc:
            if attempt >= 3 or not _is_transient_search_error(exc):
                # A target that never reaches analysis must not linger in the
                # in-flight panel for the rest of the run.
                TRACKER.finish(int(spec["tic_id"]))
                raise
            try:
                cache_root = resolve_cache_dir(
                    os.environ.get("EXOHUNT_CACHE_DIR"),
                    workspace_root=Path.cwd(),
                )
                failed_namespace = (
                    cache_root / "batch_targets" / cli_module._safe_name(namespace)
                )
                cli_module.prune_fits_cache(failed_namespace, max_bytes=0)
            except Exception:
                # The next attempt remains isolated even if Windows still has
                # a failed archive open momentarily.
                pass
            delay = 2 ** (attempt - 1)
            print(
                f"{spec['target']}: transient download failure "
                f"(attempt {attempt}/3: {exc}); retrying in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise RuntimeError("Download retry loop exited unexpectedly.")


def _analyze_downloaded_batch_target(
    spec: dict[str, object],
    args: argparse.Namespace,
    downloaded: tuple[np.ndarray, np.ndarray, dict[str, object]],
    output_dir: Path,
) -> dict[str, object]:
    from . import cli as cli_module

    hunt_args = argparse.Namespace(
        target=str(spec["target"]),
        tic=int(spec["tic_id"]),
        sector=list(spec["sectors"]),
        author=args.author,
        cadence_seconds=args.cadence_seconds,
        min_period=args.min_period,
        max_period=args.max_period,
        mask_width=args.mask_width,
        allow_no_known=args.allow_no_known,
        output_dir=str(output_dir),
        quiet=True,
    )
    time_values, flux_values, downloaded_metadata = downloaded
    metadata = dict(downloaded_metadata)
    for key in ("stellar_radius_solar", "stellar_mass_solar", "camera", "ccd"):
        if spec.get(key) is not None:
            metadata[key] = spec[key]
    TRACKER.stage(int(spec["tic_id"]), "preparing")
    # The download stage hands over a normalized but undetrended curve, so the
    # edge-safe detrend happens here, on a worker, rather than on the
    # coordinator. The download stage says so explicitly rather than this
    # inferring it from a missing key: any caller that supplies already
    # prepared arrays without that key would otherwise be detrended twice.
    if metadata.pop("requires_preparation", False):
        time_values, flux_values, metadata = cli_module.prepare_search_arrays(
            time_values, flux_values, metadata
        )
    try:
        for attempt in range(1, 4):
            try:
                cli_module._hunt_from_light_curve(
                    hunt_args, time_values, flux_values, metadata
                )
                break
            except Exception as exc:
                if attempt >= 3 or not _is_transient_search_error(exc):
                    raise
                delay = 2 ** (attempt - 1)
                print(
                    f"{spec['target']}: transient catalog/analysis failure "
                    f"(attempt {attempt}/3: {exc}); retrying in {delay}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
    finally:
        # Clears on success and on failure alike, so the panel shows work in
        # progress rather than accumulating ghosts.
        TRACKER.finish(int(spec["tic_id"]))
    report_path = Path(hunt_args.generated_report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return _result_row_from_report(
        report,
        target=str(spec["target"]),
        tic_id=int(spec["tic_id"]),
        sectors=list(spec["sectors"]),
        expected_report=Path(spec["expected_report"]),
        run_state="completed",
    )


def _publish_followup_queue(
    output_dir: Path,
    results: list[dict[str, object]],
) -> None:
    from . import cli as cli_module

    queued = sorted(
        (
            {
                "tic_id": row.get("tic_id"),
                "target": row.get("target"),
                "sectors": row.get("sectors"),
                "screening_class": row.get("screening_class"),
                "followup_priority": int(row.get("followup_priority", 0)),
                "followup_reasons": row.get("followup_reasons", ""),
                "vetting_tier": row.get("vetting_tier", "legacy_unmeasured"),
                "deeper_vetting_flags": row.get("deeper_vetting_flags", ""),
                "recommended_data_sources": row.get(
                    "recommended_data_sources", ""
                ),
                "period_days": row.get("period_days"),
                "depth_ppm": row.get("depth_ppm"),
                "depth_snr": row.get("depth_snr"),
                "observed_transits": row.get("observed_transits"),
                "sensitivity_3d_ppm": row.get("sensitivity_3d_ppm"),
                "sensitivity_12d_ppm": row.get("sensitivity_12d_ppm"),
                "red_noise_adjusted_snr": row.get("red_noise_adjusted_snr"),
                "event_coverage_fraction": row.get("event_coverage_fraction"),
                "positive_depth_event_fraction": row.get(
                    "positive_depth_event_fraction"
                ),
                "report": row.get("report"),
            }
            for row in results
            if row.get("status") != "error"
            and int(row.get("followup_priority", 0)) >= 50
        ),
        key=lambda row: (-int(row["followup_priority"]), int(row["tic_id"])),
    )
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "warning": (
            "Queue entries are automated leads, not planet candidates. A missing "
            "entry does not establish that a star has no planet."
        ),
        "targets": queued,
    }
    cli_module._atomic_write_json(output_dir / "deep_followup_queue.json", payload)
    fieldnames = [
        "tic_id",
        "target",
        "sectors",
        "screening_class",
        "followup_priority",
        "followup_reasons",
        "vetting_tier",
        "deeper_vetting_flags",
        "recommended_data_sources",
        "period_days",
        "depth_ppm",
        "depth_snr",
        "observed_transits",
        "sensitivity_3d_ppm",
        "sensitivity_12d_ppm",
        "red_noise_adjusted_snr",
        "event_coverage_fraction",
        "positive_depth_event_fraction",
        "report",
    ]
    temporary = output_dir / "deep_followup_queue.csv.tmp"
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(queued)
    cli_module._replace_with_retry(temporary, output_dir / "deep_followup_queue.csv")
