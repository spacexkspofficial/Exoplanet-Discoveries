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
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from filelock import FileLock, Timeout

from .lease import ALREADY_RUNNING_MESSAGE, acquire_machine_lock
from .paths import path_is_within, resolve_cache_dir
from .screening import _classify_screening_result, _sensitivity_depth_at_period


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
        try:
            return cli_module._run_batch_hunt(args)
        finally:
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
    prefetch = max(workers, int(prefetch) if prefetch is not None else workers * 2)
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
    for attempt in range(1, 4):
        try:
            return cli_module._download_light_curve(
                str(spec["target"]),
                list(spec["sectors"]),
                args.author,
                args.cadence_seconds,
                cache_namespace=namespace,
            )
        except Exception as exc:
            if attempt >= 3 or not _is_transient_search_error(exc):
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
    for key in ("stellar_radius_solar", "stellar_mass_solar"):
        if spec.get(key) is not None:
            metadata[key] = spec[key]
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
