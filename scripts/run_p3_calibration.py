"""Run the locked P3 baseline, injection, inverted, and scramble gates."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from exohunt.calibration import (  # noqa: E402
    calibrate_downloaded_target,
    read_target_rows,
    select_calibration_sample,
    summarize_calibration,
)
from exohunt.campaign import (  # noqa: E402
    _analysis_executor,
    _batch_target_spec,
    _download_batch_target,
)
from exohunt.catalogs import (  # noqa: E402
    _catalog_cache_root,
    _read_fresh_cache,
    check_tic,
    warm_cache_bulk,
)
from exohunt.cli import _scientific_settings  # noqa: E402
from exohunt.config import (  # noqa: E402
    CURRENT_CONFIG,
    code_version,
    hash_target_list,
    require_clean_repository,
    settings_signature,
)
from exohunt.progress import TRACKER  # noqa: E402


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _specs(
    rows: list[dict[str, str]],
    *,
    output_dir: Path,
    injection_tics: set[int],
) -> list[dict[str, object]]:
    specs: list[dict[str, object]] = []
    for index, row in enumerate(rows, start=1):
        spec = _batch_target_spec(index, row, output_dir / "baseline_reports")
        for key in ("teff_k", "tmag", "distance_pc"):
            try:
                value = float(row.get(key, ""))
            except (TypeError, ValueError):
                continue
            if np.isfinite(value):
                spec[key] = value
        spec["run_injections"] = int(spec["tic_id"]) in injection_tics
        specs.append(spec)
    # Put expensive targets first so their long tail overlaps the null-only work.
    return sorted(
        specs,
        key=lambda spec: (not bool(spec["run_injections"]), int(spec["index"])),
    )


def _epoch_histogram(
    baseline_reports: list[dict[str, object]],
    *,
    bin_minutes: float = 30.0,
) -> dict[str, object]:
    """Measure fitted-ephemeris alignment against its phase-uniform null."""

    if not baseline_reports:
        return {"bins": [], "maximum_enrichment": None}
    step = bin_minutes / (24.0 * 60.0)
    start = min(float(report["observation_window"]["start_btjd"]) for report in baseline_reports)
    stop = max(float(report["observation_window"]["end_btjd"]) for report in baseline_reports)
    epochs = np.arange(start, stop + step / 2.0, step)
    rows: list[dict[str, object]] = []
    for epoch in epochs:
        observed = 0
        expected = 0.0
        covered = 0
        for report in baseline_reports:
            window = report["observation_window"]
            if not float(window["start_btjd"]) <= epoch <= float(window["end_btjd"]):
                continue
            signal = report["strongest_residual_signal"]
            period = float(signal["period_days"])
            transit_time = float(signal["transit_time"])
            tolerance = max(
                float(signal["duration_hours"]) / 48.0,
                CURRENT_CONFIG.calibration.epoch_alignment_minimum_tolerance_days,
            )
            offset = abs((epoch - transit_time + period / 2.0) % period - period / 2.0)
            observed += offset <= tolerance
            expected += min(1.0, 2.0 * tolerance / period)
            covered += 1
        if covered < 20 or expected <= 0:
            continue
        rows.append(
            {
                "epoch_btjd": round(float(epoch), 6),
                "covered_stars": covered,
                "aligned_signals": observed,
                "phase_uniform_expectation": round(expected, 6),
                "enrichment": round(observed / expected, 6),
            }
        )
    maximum = max((float(row["enrichment"]) for row in rows), default=None)
    return {
        "definition": (
            "30-minute epoch bins; each fitted ephemeris contributes when its "
            "folded offset is within max(half-duration, 0.02 d); expectation "
            "is the summed phase-uniform occupancy for stars covering the bin"
        ),
        "bin_minutes": bin_minutes,
        "bins": rows,
        "maximum_enrichment": maximum,
        "maximum_bin": (
            max(rows, key=lambda row: float(row["enrichment"])) if rows else None
        ),
    }


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, separators=(",", ":"))
                    if isinstance(value, (dict, list))
                    else value
                    for key, value in row.items()
                }
            )


def _warm_catalogs(tic_ids: list[int]) -> dict[str, object]:
    root = _catalog_cache_root()
    values = sorted(set(tic_ids))
    missing = [
        tic_id
        for tic_id in values
        if _read_fresh_cache(root / f"TIC_{tic_id}.json") is None
    ]
    if not missing:
        return {"fresh": len(values), "queried": 0, "fallback": False}
    try:
        result = warm_cache_bulk(
            missing,
            progress=lambda done, total: print(
                f"catalog cache {done}/{total}", flush=True
            ),
        )
        return {
            "fresh": len(values) - len(missing),
            "queried": len(missing),
            "fallback": False,
            "bulk": result,
        }
    except Exception as exc:
        print(
            f"Bulk catalog warm failed ({type(exc).__name__}: {exc}); "
            "falling back to bounded per-target checks.",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(check_tic, missing))
        return {
            "fresh": len(values) - len(missing),
            "queried": len(missing),
            "fallback": True,
            "bulk_error": f"{type(exc).__name__}: {exc}",
        }


def _aggregate(
    output_dir: Path,
    signature: str,
    calibration_signature: str,
    running_code_version: str,
) -> dict[str, object]:
    star_results: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    for path in sorted((output_dir / "stars").glob("TIC_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("calibration_signature") != calibration_signature:
            continue
        if payload.get("error"):
            errors.append(payload)
        else:
            star_results.append(payload)
    baselines = [row["baseline"] for row in star_results]
    inverted = [row["inverted"] for row in star_results]
    scrambled = [row["scrambled"] for row in star_results]
    injections = [trial for row in star_results for trial in row["injections"]]
    reports = [row["baseline_report"] for row in star_results]
    epoch = _epoch_histogram(reports)
    _atomic_json(output_dir / "epoch_histogram.json", epoch)
    summary = summarize_calibration(
        injections,
        baseline_rows=baselines,
        inverted_rows=inverted,
        scrambled_rows=scrambled,
        maximum_epoch_enrichment=epoch["maximum_enrichment"],
    )
    summary.update(
        {
            "scientific_signature": signature,
            "calibration_signature": calibration_signature,
            "code_version": running_code_version,
            "completed_stars": len(star_results),
            "errors": errors,
            "execution_complete": not errors,
        }
    )
    summary["release_gate_passes"] = bool(
        summary["release_gate_passes"] and not errors
    )
    _atomic_json(output_dir / "calibration_summary.json", summary)
    _write_csv(output_dir / "injections.csv", injections)
    _write_csv(output_dir / "baseline.csv", baselines)
    _write_csv(output_dir / "inverted.csv", inverted)
    _write_csv(output_dir / "scrambled.csv", scrambled)
    return summary


def run(args: argparse.Namespace) -> int:
    if not args.allow_dirty:
        require_clean_repository(ROOT)
    target_path = Path(args.targets)
    output_dir = Path(args.output_dir)
    rows = read_target_rows(target_path)
    if args.max_targets is not None:
        rows = rows[: args.max_targets]
    sample = select_calibration_sample(rows, seed=args.seed)
    injection_tics = set(int(value) for value in sample["sample_tic_ids"])

    science_args = argparse.Namespace(
        author=args.author,
        cadence_seconds=args.cadence_seconds,
        min_period=args.min_period,
        max_period=args.max_period,
        mask_width=args.mask_width,
        allow_no_known=True,
    )
    production_settings = _scientific_settings(science_args)
    settings_payload = {
        **production_settings,
        "calibration": asdict(CURRENT_CONFIG.calibration),
    }
    running_code_version = code_version(ROOT)
    signature = settings_signature(
        code=running_code_version,
        settings=production_settings,
        product_family=f"{args.author}-{args.cadence_seconds:g}s",
        target_list_hash=hash_target_list(target_path),
    )
    calibration_signature = settings_signature(
        code=running_code_version,
        settings=settings_payload,
        product_family=f"calibration:{args.author}-{args.cadence_seconds:g}s",
        target_list_hash=hash_target_list(target_path),
    )
    settings: dict[str, object] = {
        "author": args.author,
        "cadence_seconds": args.cadence_seconds,
        "min_period": args.min_period,
        "max_period": args.max_period,
        "mask_width": args.mask_width,
        "output_dir": str(output_dir / "transient"),
        "seed": args.seed,
        "scientific_signature": signature,
        "calibration_signature": calibration_signature,
        "dip_registry": args.dip_registry,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    injection_searches = (
        CURRENT_CONFIG.calibration.random_phase_injections_per_star
        + CURRENT_CONFIG.calibration.edge_injections_per_star
    )
    _atomic_json(output_dir / "sample_manifest.json", sample)
    _atomic_json(
        output_dir / "run_manifest.json",
        {
            "schema_version": 1,
            "targets": str(target_path),
            "target_list_sha256": hash_target_list(target_path),
            "scientific_signature": signature,
            "calibration_signature": calibration_signature,
            "code_version": running_code_version,
            "scientific_settings": production_settings,
            "calibration_science_settings": settings_payload,
            "calibration_settings": settings,
            "cohort_rows": len(rows),
            "expected_searches": len(rows) * 3 + len(injection_tics) * injection_searches,
        },
    )

    print(f"Scientific signature: {signature}", flush=True)
    print(f"Warming catalog cache for {len(rows)} locked targets...", flush=True)
    catalog_warm = _warm_catalogs([int(row["tic_id"]) for row in rows])
    print(json.dumps(catalog_warm), flush=True)

    specs = _specs(rows, output_dir=output_dir, injection_tics=injection_tics)
    complete: dict[int, dict[str, object]] = {}
    pending: deque[dict[str, object]] = deque()
    for spec in specs:
        path = output_dir / "stars" / f"TIC_{int(spec['tic_id'])}.json"
        if path.exists() and not args.force:
            try:
                prior = json.loads(path.read_text(encoding="utf-8"))
                if (
                    prior.get("calibration_signature") == calibration_signature
                    and not prior.get("error")
                ):
                    complete[int(spec["tic_id"])] = prior
                    continue
            except (OSError, ValueError):
                pass
        pending.append(spec)

    total_units = len(rows) * 3 + len(injection_tics) * injection_searches
    completed_units = sum(
        3 + (injection_searches if bool(row.get("run_injections")) else 0)
        for row in complete.values()
    )
    started = time.monotonic()
    download_futures: dict[Future, dict[str, object]] = {}
    analysis_futures: dict[Future, dict[str, object]] = {}
    waiting: deque[tuple[dict[str, object], object]] = deque()
    errors = 0

    for variable in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ.setdefault(variable, "1")

    with ThreadPoolExecutor(max_workers=args.download_workers) as downloads, _analysis_executor(
        args.workers, args.workers
    ) as analyses:
        while pending or download_futures or waiting or analysis_futures:
            while pending and len(download_futures) + len(waiting) + len(analysis_futures) < args.prefetch:
                spec = pending.popleft()
                future = downloads.submit(_download_batch_target, spec, args)
                download_futures[future] = spec
            while waiting and len(analysis_futures) < args.workers:
                spec, downloaded = waiting.popleft()
                future = analyses.submit(
                    calibrate_downloaded_target, spec, settings, downloaded
                )
                analysis_futures[future] = spec
            active = list(download_futures) + list(analysis_futures)
            if not active:
                continue
            done, _ = wait(active, timeout=5.0, return_when=FIRST_COMPLETED)
            for future in done:
                if future in download_futures:
                    spec = download_futures.pop(future)
                    try:
                        waiting.append((spec, future.result()))
                    except Exception as exc:
                        errors += 1
                        payload = {
                            "schema_version": 1,
                            "scientific_signature": signature,
                            "calibration_signature": calibration_signature,
                            "tic_id": int(spec["tic_id"]),
                            "target": spec["target"],
                            "error": f"download: {type(exc).__name__}: {exc}",
                        }
                        _atomic_json(
                            output_dir / "stars" / f"TIC_{int(spec['tic_id'])}.json",
                            payload,
                        )
                        TRACKER.finish(int(spec["tic_id"]))
                else:
                    spec = analysis_futures.pop(future)
                    try:
                        payload = future.result()
                        baseline_report = payload.pop("baseline_report")
                        _atomic_json(
                            output_dir
                            / "baseline_reports"
                            / f"TIC_{int(spec['tic_id'])}_residual.json",
                            baseline_report,
                        )
                        # Keep the report in the per-star checkpoint too: the
                        # aggregate can rebuild every gate without relying on
                        # filename inventories.
                        payload["baseline_report"] = baseline_report
                        _atomic_json(
                            output_dir / "stars" / f"TIC_{int(spec['tic_id'])}.json",
                            payload,
                        )
                        complete[int(spec["tic_id"])] = payload
                        completed_units += 3 + (
                            injection_searches if bool(spec["run_injections"]) else 0
                        )
                    except Exception as exc:
                        errors += 1
                        payload = {
                            "schema_version": 1,
                            "scientific_signature": signature,
                            "calibration_signature": calibration_signature,
                            "tic_id": int(spec["tic_id"]),
                            "target": spec["target"],
                            "error": f"analysis: {type(exc).__name__}: {exc}",
                        }
                        _atomic_json(
                            output_dir / "stars" / f"TIC_{int(spec['tic_id'])}.json",
                            payload,
                        )
                    finally:
                        TRACKER.finish(int(spec["tic_id"]))
            elapsed = max(time.monotonic() - started, 1e-6)
            rate = completed_units / elapsed * 3600.0
            remaining = max(0, total_units - completed_units)
            progress = {
                "state": "running",
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                "scientific_signature": signature,
                "calibration_signature": calibration_signature,
                "stars_completed": len(complete),
                "stars_total": len(rows),
                "searches_completed": completed_units,
                "searches_total": total_units,
                "searches_per_hour": round(rate, 1),
                "eta_hours": round(remaining / rate, 3) if rate > 0 else None,
                "downloads_in_flight": len(download_futures),
                "analyses_in_flight": len(analysis_futures),
                "downloaded_waiting": len(waiting),
                "errors": errors,
            }
            _atomic_json(output_dir / "p3_progress.json", progress)
            print(
                f"P3 {completed_units}/{total_units} searches; "
                f"{len(complete)}/{len(rows)} stars; {rate:,.0f}/hr; "
                f"ETA {progress['eta_hours']} h; errors {errors}",
                flush=True,
            )

    summary = _aggregate(
        output_dir,
        signature,
        calibration_signature,
        running_code_version,
    )
    _atomic_json(
        output_dir / "p3_progress.json",
        {
            "state": "completed" if not summary["errors"] else "retry_pending",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "scientific_signature": signature,
            "calibration_signature": calibration_signature,
            "stars_completed": summary["completed_stars"],
            "stars_total": len(rows),
            "searches_total": total_units,
            "errors": len(summary["errors"]),
        },
    )
    print(json.dumps(summary, indent=2), flush=True)
    return 1 if summary["errors"] else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", required=True)
    parser.add_argument("--output-dir", default="results/p3/locked500")
    parser.add_argument("--author", default="SPOC")
    parser.add_argument("--cadence-seconds", type=float, default=120.0)
    parser.add_argument("--min-period", type=float, default=0.5)
    parser.add_argument("--max-period", type=float, default=20.0)
    parser.add_argument("--mask-width", type=float, default=1.5)
    parser.add_argument("--seed", type=int, default=20260806)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--download-workers", type=int, default=3)
    parser.add_argument("--prefetch", type=int, default=24)
    parser.add_argument("--max-targets", type=int)
    parser.add_argument("--dip-registry")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Diagnostic smoke runs only; release evidence requires a clean worktree.",
    )
    args = parser.parse_args()
    if args.workers < 1 or args.download_workers < 1 or args.prefetch < args.workers:
        parser.error("workers/download-workers must be positive and prefetch >= workers")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
