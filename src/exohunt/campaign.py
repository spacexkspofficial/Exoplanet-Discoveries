"""Batch campaign scheduling for the EXOHUNT command-line application."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .paths import path_is_within, resolve_cache_dir


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
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    workers = max(1, int(getattr(args, "workers", 1)))
    if workers > 8:
        raise ValueError("At most 8 analysis workers are supported.")
    prefetch_arg = getattr(args, "prefetch", None)
    prefetch = max(
        workers,
        int(prefetch_arg) if prefetch_arg is not None else workers * 2,
    )
    if prefetch > 64:
        raise ValueError("At most 64 targets may be staged for download-ahead.")
    download_workers_arg = getattr(args, "download_workers", None)
    download_workers = (
        int(download_workers_arg)
        if download_workers_arg is not None
        else min(2, workers)
    )
    if download_workers <= 0 or download_workers > 8:
        raise ValueError("Use between 1 and 8 download workers.")
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
        "download_workers": download_workers,
        "prefetch_targets": prefetch,
        "downloads_in_flight": 0,
        "analyses_in_flight": 0,
        "downloaded_waiting": 0,
        "targets_remaining": 0,
    }
    last_progress_publish = 0.0

    def roll_cache() -> None:
        if not getattr(args, "retain_rejected_plots", False):
            try:
                plot_report = prune_rejected_plots(
                    results_by_index.values(),
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
            report = prune_fits_cache(cache_dir, max_bytes=cache_max_bytes)
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

    def publish_progress(state: str = "running") -> None:
        nonlocal last_progress_publish
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
            "runtime": runtime_state,
            "counts": _campaign_counts(results),
            "results": results,
        }
        _atomic_write_json(progress_path, progress)
        _publish_followup_queue(output_dir, results)
        try:
            from .dashboard import export_dashboard_data

            export_dashboard_data(Path.cwd())
        except Exception:
            # Search checkpoints remain authoritative if the optional UI refresh fails.
            pass

    pending_specs: deque[dict[str, object]] = deque()
    for spec in specs:
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
    # Enforce storage before any new download is submitted. Subsequent rolling
    # passes preserve headroom for the bounded prefetch queue.
    roll_cache()
    publish_progress()

    download_futures: dict[Future, dict[str, object]] = {}
    analysis_futures: dict[Future, dict[str, object]] = {}
    downloaded_waiting: deque[
        tuple[dict[str, object], tuple[np.ndarray, np.ndarray, dict[str, object]]]
    ] = deque()
    completed_since_prune = 0
    cache_prune_due = False

    def refresh_runtime() -> None:
        runtime_state.update(
            {
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
        if cache_prune_due:
            return
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
            staged += 1

    def submit_analyses(executor: ThreadPoolExecutor) -> None:
        while downloaded_waiting and len(analysis_futures) < workers:
            spec, downloaded = downloaded_waiting.popleft()
            future = executor.submit(
                _analyze_downloaded_batch_target,
                spec,
                args,
                downloaded,
                output_dir,
            )
            analysis_futures[future] = spec

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

    with (
        ThreadPoolExecutor(
            max_workers=download_workers,
            thread_name_prefix="exohunt-download",
        ) as download_executor,
        ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="exohunt-analysis",
        ) as analysis_executor,
    ):
        submit_downloads(download_executor)
        while (
            pending_specs
            or download_futures
            or downloaded_waiting
            or analysis_futures
        ):
            submit_analyses(analysis_executor)
            submit_downloads(download_executor)
            refresh_runtime()
            active_futures = set(download_futures) | set(analysis_futures)
            if not active_futures:
                if cache_prune_due:
                    roll_cache()
                    completed_since_prune = 0
                    cache_prune_due = False
                    submit_downloads(download_executor)
                    continue
                raise RuntimeError("Parallel batch scheduler stalled without active work.")
            done, _ = wait(active_futures, return_when=FIRST_COMPLETED)
            for future in done:
                if future in download_futures:
                    spec = download_futures.pop(future)
                    try:
                        downloaded_waiting.append((spec, future.result()))
                    except Exception as exc:
                        record_result(spec, _batch_error_row(spec, exc))
                else:
                    spec = analysis_futures.pop(future)
                    try:
                        result_row = future.result()
                    except Exception as exc:
                        result_row = _batch_error_row(spec, exc)
                    record_result(spec, result_row)
            if cache_prune_due and not download_futures:
                roll_cache()
                completed_since_prune = 0
                cache_prune_due = False

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
