"""Command-line interface for the exohunt starter."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import threading
import time
import warnings
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from filelock import FileLock, Timeout

from .catalogs import check_tic, curated_cool_single_hosts, known_planet_host_tic_ids
from .benchmarks import BENCHMARKS, compare_period
from .context import query_cross_mission_context
from .detection import (
    binned_phase_curve,
    evaluate_ephemeris,
    fixed_ephemeris_injection_sensitivity,
    harmonic_diagnostics,
    independent_period_peaks,
    inject_box_transit,
    mask_periodic_events,
    phase_fold,
    search_transits,
    signal_vetting_diagnostics,
)
from .pixel import difference_image, target_pixel_from_sky_grid
from .reporting import create_campaign_report, create_candidate_packet
from .metrics import (
    current_stats,
    read_events,
    record_campaign,
    record_outcome,
    record_validation,
)
from .retention import (
    directory_size_bytes,
    prune_fits_cache,
    prune_historical_rejected_plots,
    prune_rejected_plots,
)
from .tce import check_tces

# This command writes PNG files and must also work on headless/portable Python
# runtimes where Tk is not installed.
os.environ.setdefault("MPLBACKEND", "Agg")

_PLOT_LOCK = threading.Lock()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "target"


def _sector_values(sector: int | list[int] | None) -> list[int]:
    if sector is None:
        return []
    if isinstance(sector, int):
        return [sector]
    return sorted(set(int(value) for value in sector))


def _sector_suffix(sector: int | list[int] | None) -> str:
    values = _sector_values(sector)
    return "" if not values else "_s" + "-".join(str(value) for value in values)


def _workspace_cache_dir(
    cache_dir: str | Path,
    *,
    workspace_root: str | Path = ".",
) -> Path:
    """Resolve a cache only when it is a child of this project's data directory."""

    workspace = Path(workspace_root).resolve()
    data_root = (workspace / "data").resolve()
    raw = Path(cache_dir)
    resolved = (raw if raw.is_absolute() else workspace / raw).resolve()
    try:
        relative = resolved.relative_to(data_root)
    except ValueError as exc:
        raise ValueError(
            f"Cache directory must be inside the project data directory: {data_root}"
        ) from exc
    if not relative.parts:
        raise ValueError(
            "Cache directory must be a dedicated child of the project data directory."
        )
    return resolved


def _configured_lightkurve():
    cache_dir = Path(os.environ.get("EXOHUNT_CACHE_DIR", "data/lightkurve")).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    warnings.filterwarnings(
        "ignore", message="Warning: the tpfmodel submodule is not available", category=UserWarning
    )
    import lightkurve as lk

    lk.conf.cache_dir = str(cache_dir)
    return lk, cache_dir


def _thread_safe_lightkurve_download(method, **kwargs):
    """Call a Lightkurve download without its process-global stdout redirect.

    Lightkurve decorates SearchResult downloads by replacing ``sys.stdout`` for
    the entire network request. Two concurrent calls can restore and close each
    other's stream, terminating a parallel batch with "I/O operation on closed
    file." ``functools.wraps`` exposes the original method, which is safe to
    call concurrently (its progress text may interleave, but no stream closes).
    """

    original = getattr(method, "__wrapped__", None)
    owner = getattr(method, "__self__", None)
    if original is not None and owner is not None:
        return original(owner, **kwargs)
    return method(**kwargs)


def _download_light_curve(
    target: str,
    sector: int | list[int] | None,
    author: str,
    cadence_seconds: float | None = 120.0,
):
    lk, cache_dir = _configured_lightkurve()
    sectors = _sector_values(sector)
    if author == "TESScut":
        if len(sectors) != 1:
            raise ValueError("TESScut searches require exactly one TESS sector.")
        search = lk.search_tesscut(target, sector=sectors[0])
        if len(search) == 0:
            raise RuntimeError(f"No public TESScut data found for {target!r} in Sector {sectors[0]}.")
        tpf = _thread_safe_lightkurve_download(
            search.download,
            cutout_size=11,
            quality_bitmask="default",
            download_dir=str(cache_dir),
        )
        if tpf is None:
            raise RuntimeError("MAST returned no downloadable TESScut target-pixel file.")
        aperture_mask = tpf.create_threshold_mask(threshold=3, reference_pixel="center")
        if int(np.count_nonzero(aperture_mask)) == 0:
            aperture_mask = np.zeros(tpf.flux.shape[1:], dtype=bool)
            center_row = aperture_mask.shape[0] // 2
            center_column = aperture_mask.shape[1] // 2
            aperture_mask[
                max(0, center_row - 1) : center_row + 2,
                max(0, center_column - 1) : center_column + 2,
            ] = True
        aperture_pixels = int(np.count_nonzero(aperture_mask))
        background_mask = ~aperture_mask
        background_pixels = int(np.count_nonzero(background_mask))
        raw_lc = tpf.to_lightcurve(aperture_mask=aperture_mask)
        background_per_pixel = np.nanmedian(tpf.flux[:, background_mask], axis=1)
        corrected_lc = raw_lc.copy()
        corrected_lc.flux = raw_lc.flux - background_per_pixel * aperture_pixels
        corrected_flux = np.asarray(corrected_lc.flux.value, dtype=float)
        median_flux = float(np.nanmedian(corrected_flux))
        relative_scatter = float(np.nanstd(corrected_flux) / median_flux)
        if not np.isfinite(median_flux) or median_flux <= 0:
            raise RuntimeError("TESScut background subtraction left non-positive target flux.")
        if not np.isfinite(relative_scatter) or relative_scatter > 0.5:
            raise RuntimeError(
                "TESScut extraction remains background-dominated after subtraction "
                f"(relative scatter {relative_scatter:.3f})."
            )
        normalized = (
            corrected_lc.remove_nans()
            .normalize()
            .remove_outliers(sigma_upper=4.0, sigma_lower=20.0)
        )
        cadence_days = float(np.nanmedian(np.diff(normalized.time.value)))
        window = max(101, int(round(2.0 / cadence_days)))
        if window % 2 == 0:
            window += 1
        flattened = normalized.flatten(window_length=window, break_tolerance=5)
        tic_match = re.search(r"\b(\d+)\b", target)
        metadata = {
            "target": target,
            "tic_id": int(tic_match.group(1)) if tic_match else None,
            "requested_sectors": sectors,
            "downloaded_sectors": sectors,
            "author": author,
            "requested_cadence_seconds": cadence_seconds,
            "downloaded_products": 1,
            "cadence_minutes": cadence_days * 24 * 60,
            "flatten_window_cadences": window,
            "tesscut_size_pixels": 11,
            "aperture_pixels": aperture_pixels,
            "background_pixels": background_pixels,
            "background_subtracted": True,
            "pre_normalization_relative_scatter": relative_scatter,
            "extraction_version": "tesscut-bgsub-v1",
        }
        return flattened.time.value, flattened.flux.value, metadata

    kwargs: dict[str, object] = {"mission": "TESS", "author": author}
    if sectors:
        kwargs["sector"] = sectors
    if cadence_seconds is not None:
        kwargs["exptime"] = cadence_seconds
    search = lk.search_lightcurve(target, **kwargs)
    if len(search) == 0:
        raise RuntimeError(
            f"No {author} TESS light curve found for {target!r}"
            + (f" in sectors {sectors}." if sectors else ".")
            + " Try --author TESS-SPOC or --author QLP."
        )
    collection = _thread_safe_lightkurve_download(
        search.download_all,
        quality_bitmask="default", download_dir=str(cache_dir)
    )
    if collection is None or len(collection) == 0:
        raise RuntimeError("MAST returned no downloadable light curves.")
    normalized = collection.stitch(
        corrector_func=lambda lc: lc.remove_nans().normalize().remove_outliers(
            sigma_upper=4.0, sigma_lower=20.0
        )
    )
    cadence_days = float(np.nanmedian(np.diff(normalized.time.value)))
    window = max(101, int(round(2.0 / cadence_days)))
    if window % 2 == 0:
        window += 1
    flattened = normalized.flatten(window_length=window, break_tolerance=5)
    target_name = str(search.table["target_name"][0]).strip()
    tic_id = int(target_name) if target_name.isdigit() else None
    downloaded_sectors = sorted(
        {
            int(match.group(1))
            for mission in search.table["mission"]
            if (match := re.search(r"Sector\s+(\d+)", str(mission)))
        }
    )
    metadata = {
        "target": target,
        "tic_id": tic_id,
        "requested_sectors": sectors,
        "downloaded_sectors": downloaded_sectors,
        "author": author,
        "requested_cadence_seconds": cadence_seconds,
        "downloaded_products": len(collection),
        "cadence_minutes": cadence_days * 24 * 60,
        "flatten_window_cadences": window,
    }
    return flattened.time.value, flattened.flux.value, metadata


def _plot_result(result, arrays: dict[str, np.ndarray], destination: Path) -> None:
    matplotlib_cache = Path("data/matplotlib").resolve()
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))
    os.environ.setdefault("MPLBACKEND", "Agg")
    # pyplot owns process-global state. Analysis may run concurrently, but
    # rendering remains serialized so one target cannot corrupt another plot.
    with _PLOT_LOCK:
        import matplotlib.pyplot as plt

        phase, folded_flux = phase_fold(
            arrays["time"], arrays["flux"], result.period_days, result.transit_time
        )
        fig, axes = plt.subplots(3, 1, figsize=(10, 9), constrained_layout=True)
        axes[0].scatter(arrays["time"], arrays["flux"], s=2, alpha=0.55)
        axes[0].set(
            xlabel="Time (BTJD)",
            ylabel="Normalized flux",
            title="Detrended light curve",
        )
        axes[1].plot(arrays["period_grid"], arrays["power"], lw=0.8)
        axes[1].axvline(result.period_days, color="tab:red", ls="--", lw=1)
        axes[1].set(xlabel="Period (days)", ylabel="BLS power", title="Period search")
        axes[2].scatter(phase, folded_flux, s=3, alpha=0.4)
        axes[2].set(
            xlabel="Orbital phase",
            ylabel="Normalized flux",
            title=f"Strongest signal folded at {result.period_days:.6f} days",
            xlim=(-0.2, 0.2),
        )
        fig.savefig(destination, dpi=160)
        plt.close(fig)


def _analyze(args: argparse.Namespace) -> int:
    time, flux, metadata = _download_light_curve(
        args.target, args.sector, args.author, args.cadence_seconds
    )
    result, arrays = search_transits(
        time,
        flux,
        min_period_days=args.min_period,
        max_period_days=args.max_period,
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(args.target + _sector_suffix(args.sector))
    report_path = output_dir / f"{stem}.json"
    plot_path = output_dir / f"{stem}.png"
    report = {
        "warning": "Automated screening result only; this is not a validated planet candidate.",
        "data": metadata,
        "strongest_signal": result.to_dict(),
        "search_grid": {
            "period_samples": int(len(arrays["period_grid"])),
            "effective_frequency_factor": float(arrays["effective_frequency_factor"]),
            "capped_for_long_baseline": bool(arrays["period_grid_was_capped"]),
        },
        "top_period_peaks": independent_period_peaks(
            arrays["period_grid"], arrays["power"]
        ),
        "harmonic_checks": harmonic_diagnostics(
            arrays["period_grid"], arrays["power"], result.period_days
        ),
        "screening_flags": _screening_flags(result),
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _plot_result(result, arrays, plot_path)
    args.generated_report_path = str(report_path)
    args.generated_plot_path = str(plot_path)
    if not getattr(args, "quiet", False):
        print(json.dumps(report, indent=2))
        print(f"\nSaved {report_path} and {plot_path}")
    return 0


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


def _read_commented_csv(path: Path) -> list[dict[str, str]]:
    lines = [
        line
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return list(csv.DictReader(lines))


def _atomic_write_json(path: Path, payload: object) -> None:
    """Publish JSON without exposing a partially written file to the dashboard."""

    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _replace_with_retry(temporary, path)


def _replace_with_retry(source: Path, destination: Path) -> None:
    """Handle short Windows/OneDrive locks without exposing partial files."""

    for attempt in range(8):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (2**attempt))


def _scientific_settings(args: argparse.Namespace) -> dict[str, object]:
    return {
        "author": args.author,
        "cadence_seconds": args.cadence_seconds,
        "period_range_days": [args.min_period, args.max_period],
        "mask_width": args.mask_width,
        "allow_no_known": args.allow_no_known,
        "data_pipeline_version": (
            "tesscut-bgsub-commonmode-quarantined-v4"
            if args.author == "TESScut"
            else "processed-lc-v2"
        ),
    }


def _campaign_settings(args: argparse.Namespace) -> dict[str, object]:
    workers = max(1, int(getattr(args, "workers", 1)))
    prefetch = getattr(args, "prefetch", None)
    prefetch = max(workers, int(prefetch) if prefetch is not None else workers * 2)
    return {
        **_scientific_settings(args),
        "execution": {
            "analysis_workers": workers,
            "download_workers": min(2, workers),
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
    settings = progress.get("settings")
    if not isinstance(settings, dict):
        return False
    expected = _scientific_settings(args)
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


def _artifact_stem(target: str, tic_id: int, sectors: list[int]) -> str:
    identity = target if str(tic_id) in target else f"TIC {tic_id} {target}"
    return _safe_name(identity + _sector_suffix(sectors) + "_residual")


def _load_reusable_report(
    report_path: Path,
    *,
    target: str,
    tic_id: int,
    sectors: list[int],
    args: argparse.Namespace,
    allow_legacy: bool,
) -> dict[str, object] | None:
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
        or _sector_values(data.get("requested_sectors")) != _sector_values(sectors)
        or str(data.get("author")) != str(args.author)
        or float(data.get("requested_cadence_seconds") or -1)
        != float(args.cadence_seconds)
    ):
        return None
    configuration = report.get("search_configuration")
    if configuration is None:
        configuration_matches = allow_legacy
    else:
        configuration_matches = configuration == _scientific_settings(args)
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


def _publish_followup_queue(
    output_dir: Path,
    results: list[dict[str, object]],
) -> None:
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
    _atomic_write_json(output_dir / "deep_followup_queue.json", payload)
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
    _replace_with_retry(temporary, output_dir / "deep_followup_queue.csv")


def _batch_hunt(args: argparse.Namespace) -> int:
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
        return _run_batch_hunt(args)
    finally:
        lock.release()


def _batch_target_spec(
    index: int,
    row: dict[str, str],
    output_dir: Path,
) -> dict[str, object]:
    target = row["target"]
    tic_id = int(row["tic_id"])
    sectors = [int(value) for value in row["sectors"].split(";") if value]
    stem = _artifact_stem(target, tic_id, sectors)
    return {
        "index": index,
        "target": target,
        "tic_id": tic_id,
        "sectors": sectors,
        "expected_report": output_dir / f"{stem}.json",
    }


def _download_batch_target(
    spec: dict[str, object],
    args: argparse.Namespace,
) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    for attempt in range(1, 4):
        try:
            return _download_light_curve(
                str(spec["target"]),
                list(spec["sectors"]),
                args.author,
                args.cadence_seconds,
            )
        except Exception as exc:
            if attempt >= 3 or not _is_transient_search_error(exc):
                raise
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
    time_values, flux_values, metadata = downloaded
    for attempt in range(1, 4):
        try:
            _hunt_from_light_curve(hunt_args, time_values, flux_values, metadata)
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


def _run_batch_hunt(args: argparse.Namespace) -> int:
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
    download_workers = min(2, workers)
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
    cache_dir = _workspace_cache_dir(
        os.environ.get("EXOHUNT_CACHE_DIR", "data/lightkurve"),
        workspace_root=workspace_root,
    )
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
                non_cache_bytes = max(
                    0, workspace_after_initial - int(report["bytes_after"])
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


def _storage_prune(args: argparse.Namespace) -> int:
    """Apply the same bounded retention policy outside a running campaign."""

    if not np.isfinite(float(args.cache_max_gb)) or float(args.cache_max_gb) <= 0:
        raise ValueError("--cache-max-gb must be a finite number greater than zero.")
    cache_dir = _workspace_cache_dir(args.cache_dir, workspace_root=Path.cwd())
    cache_report = prune_fits_cache(
        cache_dir,
        max_bytes=int(float(args.cache_max_gb) * 1_000_000_000),
        dry_run=args.dry_run,
    )
    if args.keep_rejected_plots:
        plot_report: dict[str, object] = {
            "root": str(Path(args.results_dir).resolve()),
            "dry_run": args.dry_run,
            "files_deleted": 0,
            "bytes_deleted": 0,
            "skipped_by_request": True,
        }
    else:
        plot_report = prune_historical_rejected_plots(
            args.results_dir,
            workspace_root=Path.cwd(),
            dry_run=args.dry_run,
        )
        plot_report.pop("deleted_paths", None)

    report = {
        "dry_run": args.dry_run,
        "fits_cache": cache_report,
        "rejected_plots": plot_report,
        "preserved": [
            "metrics ledger and current statistics",
            "campaign JSON/CSV summaries and checkpoints",
            "per-target JSON diagnostics",
            "survivor and validation plots",
        ],
    }
    if not args.dry_run:
        manifest = Path(args.results_dir) / "storage_retention.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(manifest, report)
        report["manifest"] = str(manifest)
    print(json.dumps(report, indent=2))
    return 0


def _plot_pixel_result(
    images: dict[str, object],
    target_row: float,
    target_column: float,
    destination: Path,
) -> None:
    matplotlib_cache = Path("data/matplotlib").resolve()
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(13, 4), constrained_layout=True)
    panels = (
        ("out_image", "Out of transit", "viridis"),
        ("in_image", "In transit", "viridis"),
        ("difference_image", "Lost-light difference", "magma"),
    )
    for axis, (key, title, cmap) in zip(axes, panels):
        image_data = np.asarray(images[key], dtype=float)
        shown = axis.imshow(image_data, origin="lower", cmap=cmap)
        axis.scatter(target_column, target_row, marker="x", s=90, c="cyan", label="catalog target")
        if key == "difference_image":
            axis.scatter(
                float(images["centroid_column"]),
                float(images["centroid_row"]),
                marker="+",
                s=120,
                c="lime",
                label="lost-light centroid",
            )
        axis.set(title=title, xlabel="Pixel column", ylabel="Pixel row")
        axis.legend(loc="best", fontsize=8)
        fig.colorbar(shown, ax=axis, fraction=0.046)
    fig.savefig(destination, dpi=170)
    plt.close(fig)


def _plot_sector_vet(rows: list[dict[str, object]], destination: Path) -> None:
    matplotlib_cache = Path("data/matplotlib").resolve()
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))
    import matplotlib.pyplot as plt

    sectors = [str(row["sector"]) for row in rows]
    snr = [float(row["depth_snr"]) for row in rows]
    colors = ["#1B998B" if row["supports_signal"] else "#C44536" for row in rows]
    fig, axis = plt.subplots(figsize=(max(6, len(rows) * 1.3), 4.3))
    bars = axis.bar(sectors, snr, color=colors)
    axis.axhline(3.0, color="black", linestyle="--", linewidth=1, label="sector support gate")
    axis.set(
        xlabel="TESS sector",
        ylabel="Fixed-ephemeris depth S/N",
        title="Independent sector support",
    )
    axis.legend(loc="best")
    for bar, row in zip(bars, rows):
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            max(float(row["depth_snr"]), 0) + 0.15,
            f"{float(row['depth_ppm']):.0f} ppm",
            ha="center",
            va="bottom",
            fontsize=8,
        )
    fig.tight_layout()
    fig.savefig(destination, dpi=170)
    plt.close(fig)


def _sector_vet(args: argparse.Namespace) -> int:
    source_path = Path(args.report)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    signal = source.get("strongest_residual_signal")
    if not signal:
        raise RuntimeError("Report does not contain a residual signal.")
    metadata = source["data"]
    target = str(metadata["target"])
    tic_id = int(metadata["tic_id"])
    sectors = args.sector or metadata.get("downloaded_sectors") or metadata.get(
        "requested_sectors"
    )
    sectors = _sector_values(sectors)
    if not sectors:
        raise RuntimeError("No sectors were supplied or recorded in the source report.")
    catalog = check_tic(tic_id)
    ephemerides = _catalog_ephemerides(catalog)
    known_periods = _known_transiting_periods(catalog)
    maskable_periods = [float(event["period_days"]) for event in ephemerides]
    unmaskable = [
        period
        for period in known_periods
        if not any(abs(period - maskable) / period < 0.01 for maskable in maskable_periods)
    ]
    if unmaskable:
        raise RuntimeError("Known transiting signals cannot all be masked safely.")

    rows: list[dict[str, object]] = []
    for sector in sectors:
        time, flux, sector_metadata = _download_light_curve(
            target, [sector], args.author, args.cadence_seconds
        )
        cleaned_time, cleaned_flux, masks = mask_periodic_events(
            time, flux, ephemerides, width_factor=args.mask_width
        )
        measured = evaluate_ephemeris(
            cleaned_time,
            cleaned_flux,
            period_days=float(signal["period_days"]),
            transit_time=float(signal["transit_time"]),
            duration_hours=float(signal["duration_hours"]),
        )
        supports = bool(
            measured["sampled"]
            and int(measured["sampled_transit_events"]) >= 1
            and float(measured["depth_ppm"]) > 0
            and float(measured["depth_snr"]) >= args.min_sector_snr
        )
        row = {
            "sector": sector,
            **measured,
            "supports_signal": supports,
            "downloaded_products": sector_metadata["downloaded_products"],
            "known_masked_events": len(masks),
        }
        rows.append(row)
        print(
            f"Sector {sector}: {measured['depth_ppm']:.1f} ppm, "
            f"S/N {measured['depth_snr']:.2f}, "
            + ("supports" if supports else "does not support")
        )
    supported = sum(bool(row["supports_signal"]) for row in rows)
    report = {
        "warning": "Fixed-ephemeris sector coherence is a screening test, not confirmation.",
        "source_report": str(source_path),
        "target": target,
        "tic_id": tic_id,
        "candidate_signal": signal,
        "settings": {"minimum_sector_snr": args.min_sector_snr},
        "sectors": rows,
        "supported_sector_count": supported,
        "passes_distinct_sector_gate": supported >= args.min_supporting_sectors,
        "minimum_supporting_sectors": args.min_supporting_sectors,
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(f"TIC_{tic_id}_sector_vet")
    report_path = output_dir / f"{stem}.json"
    plot_path = output_dir / f"{stem}.png"
    report["plot"] = str(plot_path)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _plot_sector_vet(rows, plot_path)
    print(f"\nSaved {report_path} and {plot_path}")
    return 0


def _tce_check(args: argparse.Namespace) -> int:
    source_path = Path(args.report)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    signal = source.get("strongest_residual_signal")
    if not signal:
        raise RuntimeError("Report does not contain a residual signal.")
    metadata = source["data"]
    tic_id = int(metadata["tic_id"])
    sectors = args.sector or metadata.get("downloaded_sectors") or metadata.get(
        "requested_sectors"
    )
    result = check_tces(tic_id, _sector_values(sectors), float(signal["period_days"]))
    result["source_report"] = str(source_path)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / _safe_name(f"TIC_{tic_id}_tce_check.json")
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if result["matching_tces"]:
        first = result["matching_tces"][0]
        record_outcome(
            "known_tce_rediscovery",
            tic_id=tic_id,
            label=str(first["tce_id"]),
            notes=(
                f"Candidate {signal['period_days']:.8f} d matched public TCE "
                f"{first['period_days']:.8f} d"
            ),
            source=str(destination),
        )
    print(json.dumps(result, indent=2))
    print(f"\nSaved {destination}")
    return 0


def _context_vet(args: argparse.Namespace) -> int:
    """Collect compact cross-mission metadata without downloading science data."""

    source_report_path = Path(args.report)
    source_report = json.loads(source_report_path.read_text(encoding="utf-8"))
    data = source_report.get("data")
    data = data if isinstance(data, dict) else {}
    tic_id = args.tic or data.get("tic_id")
    if not tic_id:
        target = str(data.get("target") or source_report.get("target") or "")
        match = re.search(r"\b(\d+)\b", target)
        tic_id = int(match.group(1)) if match else None
    if not tic_id:
        raise RuntimeError("Could not infer a TIC ID; provide one with --tic.")

    context = query_cross_mission_context(
        int(tic_id),
        mast_radius_arcsec=args.mast_radius_arcsec,
        neighbor_radius_arcsec=args.neighbor_radius_arcsec,
    )
    signal = source_report.get("strongest_residual_signal")
    if not isinstance(signal, dict):
        signal = source_report.get("candidate_signal")
    context["source_report"] = str(source_report_path)
    context["signal_under_review"] = signal if isinstance(signal, dict) else None

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"TIC_{int(tic_id)}_cross_mission_context.json"
    _atomic_write_json(report_path, context)
    print(json.dumps(context, indent=2))
    print(f"\nSaved {report_path}")
    return 0


def _pixel_vet(args: argparse.Namespace) -> int:
    source_report_path = Path(args.report)
    source = json.loads(source_report_path.read_text(encoding="utf-8"))
    signal = source.get("strongest_residual_signal") or source.get("strongest_signal")
    if not signal:
        raise RuntimeError("Report does not contain a strongest signal.")
    metadata = source["data"]
    target = metadata["target"]
    lk, cache_dir = _configured_lightkurve()
    if args.author == "TESScut":
        search = lk.search_tesscut(target, sector=args.sector)
    else:
        search = lk.search_targetpixelfile(
            target,
            mission="TESS",
            author=args.author,
            sector=args.sector,
            exptime=args.cadence_seconds,
        )
    if len(search) == 0:
        raise RuntimeError(
            f"No {args.author} target-pixel file found for {target} in Sector {args.sector}."
        )
    download_kwargs: dict[str, object] = {
        "quality_bitmask": "default",
        "download_dir": str(cache_dir),
    }
    if args.author == "TESScut":
        download_kwargs["cutout_size"] = 11
    tpf = search.download(**download_kwargs)
    images = difference_image(
        tpf.time.value,
        tpf.flux.value,
        float(signal["period_days"]),
        float(signal["transit_time"]),
        float(signal["duration_hours"]),
    )
    middle = len(tpf.time) // 2
    try:
        ra_grid, dec_grid = tpf.get_coordinates(cadence=middle)
        target_row, target_column = target_pixel_from_sky_grid(
            ra_grid, dec_grid, float(tpf.ra), float(tpf.dec)
        )
    except Exception:
        target_row = (tpf.flux.shape[1] - 1) / 2
        target_column = (tpf.flux.shape[2] - 1) / 2
    centroid_row = float(images["centroid_row"])
    centroid_column = float(images["centroid_column"])
    offset_pixels = float(
        np.hypot(centroid_row - target_row, centroid_column - target_column)
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(f"{target}_s{args.sector}_pixel")
    plot_path = output_dir / f"{stem}.png"
    report_path = output_dir / f"{stem}.json"
    _plot_pixel_result(images, target_row, target_column, plot_path)
    report = {
        "warning": (
            "Difference-image centroiding is a screening check. TESS pixels are large, "
            "so an apparently on-target signal still needs catalog and follow-up checks."
        ),
        "source_report": str(source_report_path),
        "target": target,
        "sector": args.sector,
        "candidate_signal": signal,
        "in_transit_cadences": images["in_transit_cadences"],
        "out_of_transit_cadences": images["out_of_transit_cadences"],
        "target_pixel": {"row": target_row, "column": target_column},
        "lost_light_centroid": {"row": centroid_row, "column": centroid_column},
        "centroid_offset_pixels": offset_pixels,
        "centroid_offset_arcsec_approx": offset_pixels * 21.0,
        "on_target_within_one_pixel": offset_pixels <= 1.0,
        "plot": str(plot_path),
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nSaved {report_path} and {plot_path}")
    return 0


def _plot_completeness(
    rows: list[dict[str, object]],
    periods: list[float],
    depths: list[float],
    destination: Path,
) -> None:
    matplotlib_cache = Path("data/matplotlib").resolve()
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))
    import matplotlib.pyplot as plt

    matrix = np.zeros((len(depths), len(periods)), dtype=float)
    labels = np.full(matrix.shape, "miss", dtype=object)
    score = {"exact": 1.0, "harmonic_alias": 0.5, "miss": 0.0}
    short = {"exact": "exact", "harmonic_alias": "alias", "miss": "miss"}
    for row in rows:
        y = depths.index(float(row["injected_depth_ppm"]))
        x = periods.index(float(row["injected_period_days"]))
        status = str(row["recovery_status"])
        matrix[y, x] = score[status]
        labels[y, x] = short[status]
    fig, axis = plt.subplots(figsize=(max(6, len(periods) * 1.5), max(4, len(depths))))
    image_plot = axis.imshow(matrix, origin="lower", vmin=0, vmax=1, cmap="RdYlGn")
    axis.set_xticks(range(len(periods)), [f"{value:g}" for value in periods])
    axis.set_yticks(range(len(depths)), [f"{value:g}" for value in depths])
    axis.set(
        xlabel="Injected period (days)",
        ylabel="Injected depth (ppm)",
        title="Injection-recovery",
    )
    for row_index in range(len(depths)):
        for column_index in range(len(periods)):
            axis.text(column_index, row_index, labels[row_index, column_index], ha="center", va="center")
    fig.colorbar(image_plot, ax=axis, ticks=[0, 0.5, 1], label="0 miss / 0.5 alias / 1 exact")
    fig.tight_layout()
    fig.savefig(destination, dpi=170)
    plt.close(fig)


def _inject_recover(args: argparse.Namespace) -> int:
    time, flux, metadata = _download_light_curve(
        args.target, args.sector, args.author, args.cadence_seconds
    )
    tic_id = args.tic or metadata.get("tic_id")
    if not tic_id:
        raise RuntimeError("Could not infer a TIC ID; provide one with --tic.")
    catalog = check_tic(int(tic_id))
    ephemerides = _catalog_ephemerides(catalog)
    cleaned_time, cleaned_flux, mask_records = mask_periodic_events(
        time, flux, ephemerides, width_factor=args.mask_width
    )
    periods = sorted(set(float(value) for value in args.periods))
    depths = sorted(set(float(value) for value in args.depths))
    rng = np.random.default_rng(args.seed)
    epochs = {
        period: float(np.nanmin(cleaned_time) + rng.uniform(0.1, 0.9) * period)
        for period in periods
    }
    rows: list[dict[str, object]] = []
    total = len(periods) * len(depths)
    run_number = 0
    for depth in depths:
        for period in periods:
            run_number += 1
            duration_hours = (
                args.duration_hours
                if args.duration_hours is not None
                else float(np.clip(2.0 * (period / 5.0) ** (1.0 / 3.0), 0.75, 4.0))
            )
            injected_flux, _, injected_events = inject_box_transit(
                cleaned_time,
                cleaned_flux,
                period_days=period,
                transit_time=epochs[period],
                duration_hours=duration_hours,
                depth_ppm=depth,
            )
            result, arrays = search_transits(
                cleaned_time,
                injected_flux,
                min_period_days=args.min_period,
                max_period_days=args.max_period,
                max_period_grid_size=args.max_grid_size,
            )
            comparison = compare_period(result.period_days, period)
            if result.depth_snr < 7.1 or result.observed_transits < 2:
                recovery_status = "miss"
            else:
                recovery_status = str(comparison["status"])
            if recovery_status not in {"exact", "harmonic_alias"}:
                recovery_status = "miss"
            row = {
                "injected_period_days": period,
                "injected_depth_ppm": depth,
                "injected_duration_hours": duration_hours,
                "injected_transit_time": epochs[period],
                "sampled_injected_events": injected_events,
                "recovered_period_days": result.period_days,
                "recovered_depth_ppm": result.depth_ppm,
                "recovered_depth_snr": result.depth_snr,
                "recovered_observed_transits": result.observed_transits,
                "recovery_status": recovery_status,
                "period_relation": comparison["relation"],
                "period_grid_samples": int(len(arrays["period_grid"])),
                "period_grid_capped": bool(arrays["period_grid_was_capped"]),
            }
            rows.append(row)
            print(
                f"[{run_number}/{total}] P={period:g} d, depth={depth:g} ppm: "
                f"{recovery_status} (found {result.period_days:.5f} d, S/N {result.depth_snr:.2f})"
            )

    counts = {
        status: sum(row["recovery_status"] == status for row in rows)
        for status in ("exact", "harmonic_alias", "miss")
    }
    summary = {
        "warning": (
            "This is a small deterministic injection grid, not a publication-grade "
            "completeness calculation. More phases and realistic transit shapes are needed."
        ),
        "data": metadata,
        "catalog_masks": mask_records,
        "settings": {
            "seed": args.seed,
            "periods_days": periods,
            "depths_ppm": depths,
            "period_search_days": [args.min_period, args.max_period],
            "max_period_grid_size": args.max_grid_size,
        },
        "counts": counts,
        "exact_recovery_fraction": counts["exact"] / len(rows),
        "exact_or_harmonic_fraction": (counts["exact"] + counts["harmonic_alias"]) / len(rows),
        "results": rows,
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(args.target + _sector_suffix(args.sector) + "_injections")
    json_path = output_dir / f"{stem}.json"
    csv_path = output_dir / f"{stem}.csv"
    plot_path = output_dir / f"{stem}.png"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    _plot_completeness(rows, periods, depths, plot_path)
    print(f"\nSaved {json_path}, {csv_path}, and {plot_path}")
    return 0


def _catalog(args: argparse.Namespace) -> int:
    result = check_tic(args.tic)
    print(json.dumps(result, indent=2))
    if not result["tois"] and not result["confirmed_planets"]:
        print(
            "\nNo match in these two tables. This does NOT prove novelty; also check "
            "ExoFOP, TESS TCE/DV products, SIMBAD, ADS, and nearby-source contamination."
        )
    return 0


def _candidate_packet(args: argparse.Namespace) -> int:
    outputs = create_candidate_packet(
        args.report,
        output_dir=args.output_dir,
        pdf_output_dir=args.pdf_output_dir,
        pixel_report_path=args.pixel_report,
        sector_vet_report_path=args.sector_vet_report,
        tce_check_report_path=args.tce_check_report,
        submitter=args.submitter,
        contact_email=args.contact_email,
        allow_rejected=args.allow_rejected,
    )
    source = json.loads(Path(args.report).read_text(encoding="utf-8"))
    tic_id = int(source["data"]["tic_id"])
    record_outcome(
        "candidate_packet_created",
        tic_id=tic_id,
        label=f"TIC {tic_id} candidate evidence packet",
        notes=outputs["pdf"],
        source=str(args.report),
    )
    print(json.dumps(outputs, indent=2))
    return 0


def _campaign_report(args: argparse.Namespace) -> int:
    outputs = create_campaign_report(
        args.summary,
        target_manifest_path=args.target_manifest,
        output_dir=args.output_dir,
        pdf_output_dir=args.pdf_output_dir,
    )
    print(json.dumps(outputs, indent=2))
    return 0


def _metrics_summary(args: argparse.Namespace) -> int:
    print(json.dumps(current_stats(), indent=2))
    return 0


def _log_outcome(args: argparse.Namespace) -> int:
    added, stats = record_outcome(
        args.kind,
        tic_id=args.tic,
        label=args.label,
        notes=args.notes,
        source=args.source,
    )
    print(json.dumps({"event_added": added, "current_stats": stats}, indent=2))
    return 0


def _available_lightcurve_sectors(
    tic_id: int, cadence_seconds: float, author: str = "SPOC"
) -> list[int]:
    lk, _ = _configured_lightkurve()
    search = lk.search_lightcurve(
        f"TIC {tic_id}", mission="TESS", author=author, exptime=cadence_seconds
    )
    if len(search) == 0 or "mission" not in search.table.colnames:
        return []
    return sorted(
        {
            int(match.group(1))
            for mission in search.table["mission"]
            if (match := re.search(r"Sector\s+(\d+)", str(mission)))
        }
    )


def _compact_sector_subset(sectors: list[int], count: int) -> list[int]:
    """Choose a deterministic compact observing window from available sectors."""

    if count <= 0:
        raise ValueError("Sector count must be positive.")
    values = sorted(set(sectors))
    if len(values) <= count:
        return values
    windows = [values[index : index + count] for index in range(len(values) - count + 1)]
    return min(windows, key=lambda window: (window[-1] - window[0], window[0]))


def _latest_sector_subset(sectors: list[int], count: int) -> list[int]:
    """Choose the most recently numbered available sectors."""

    if count <= 0:
        raise ValueError("Sector count must be positive.")
    return sorted(set(sectors))[-count:]


def _make_targets(args: argparse.Namespace) -> int:
    criteria = {
        "dispositions": ["CP", "KP"],
        "unique_transiting_periods_across_all_toi_and_confirmed_rows": 1,
        "max_tmag": args.max_tmag,
        "max_teff_k": args.max_teff,
        "max_stellar_radius_solar": args.max_stellar_radius,
        "max_distance_pc": args.max_distance,
        "known_period_range_days": [args.known_min_period, args.known_max_period],
        "minimum_available_sectors": args.min_sectors,
        "selected_sectors_per_target": args.sectors_per_target,
        "cadence_seconds": args.cadence_seconds,
        "lightcurve_author": args.author,
        "minimum_latest_sector": args.min_latest_sector,
        "sector_strategy": args.sector_strategy,
        "ordering": "TESS magnitude ascending, then TIC ID",
    }
    catalog_rows = curated_cool_single_hosts(
        max_tmag=args.max_tmag,
        max_teff=args.max_teff,
        max_stellar_radius=args.max_stellar_radius,
        max_distance_pc=args.max_distance,
        min_period_days=args.known_min_period,
        max_period_days=args.known_max_period,
    )
    selected: list[dict[str, object]] = []
    checked = 0
    for row in catalog_rows[: args.pool_size]:
        checked += 1
        tic_id = int(float(row["tid"]))
        full_catalog = check_tic(tic_id)
        unique_periods = _known_transiting_periods(full_catalog)
        if len(unique_periods) != 1:
            continue
        sectors = _available_lightcurve_sectors(tic_id, args.cadence_seconds, args.author)
        if len(sectors) < args.min_sectors:
            continue
        if args.min_latest_sector is not None and max(sectors) < args.min_latest_sector:
            continue
        chosen = (
            _latest_sector_subset(sectors, args.sectors_per_target)
            if args.sector_strategy == "latest"
            else _compact_sector_subset(sectors, args.sectors_per_target)
        )
        selected.append(
            {
                "target": f"TIC {tic_id}",
                "tic_id": tic_id,
                "toi": row["toi"],
                "disposition": row["tfopwg_disp"],
                "tmag": row["st_tmag"],
                "teff_k": row["st_teff"],
                "stellar_radius_solar": row["st_rad"],
                "distance_pc": row["st_dist"],
                "known_period_days": row["pl_orbper"],
                "unique_transiting_signal_count": len(unique_periods),
                "available_sector_count": len(sectors),
                "sectors": ";".join(str(value) for value in chosen),
            }
        )
        print(
            f"selected TIC {tic_id} / TOI-{row['toi']}: sectors "
            + ",".join(str(value) for value in chosen)
        )
        if len(selected) >= args.limit:
            break
    if not selected:
        raise RuntimeError("No targets satisfied both the catalog and sector criteria.")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(selected[0].keys()))
        writer.writeheader()
        writer.writerows(selected)
    manifest = {
        "created_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": "NASA Exoplanet Archive TOI table plus MAST SPOC availability",
        "criteria": criteria,
        "catalog_rows_returned": len(catalog_rows),
        "catalog_rows_sector_checked": checked,
        "selected_count": len(selected),
        "targets": selected,
    }
    manifest_path = output_path.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nSaved {output_path} and {manifest_path}")
    return 0


def _optional_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _small_planet_selection_tier(
    *,
    luminosity_class: str,
    stellar_radius_solar: float | None,
    teff_k: float | None,
    max_stellar_radius_solar: float,
    max_teff_k: float,
) -> int:
    """Rank target suitability without pretending this is survey completeness."""

    lumclass = luminosity_class.strip().upper()
    radius_is_small = (
        stellar_radius_solar is not None
        and 0.1 <= stellar_radius_solar <= max_stellar_radius_solar
    )
    temperature_is_supported = (
        teff_k is not None and 2500.0 <= teff_k <= max_teff_k
    )
    if lumclass == "DWARF":
        if (
            stellar_radius_solar is not None
            and 0.1 <= stellar_radius_solar <= 1.5
            and teff_k is not None
            and 2500.0 <= teff_k <= min(max_teff_k, 6500.0)
        ):
            return 0
        if radius_is_small and temperature_is_supported:
            return 1
        return 2
    if lumclass not in {"GIANT", "SUBGIANT"} and radius_is_small:
        return 3
    if lumclass == "SUBGIANT":
        return 4
    if lumclass == "GIANT":
        return 6
    return 5


def _small_planet_merit(
    *,
    stellar_radius_solar: float | None,
    tmag: float,
) -> float:
    """Deterministic depth/brightness heuristic; lower is more favorable."""

    if stellar_radius_solar is None or stellar_radius_solar <= 0:
        return 999.0
    return 2.0 * float(np.log10(stellar_radius_solar)) + 0.2 * tmag


def _make_sector_targets(args: argparse.Namespace) -> int:
    """Build a large, local-only campaign from an official TESS target list."""

    source_path = Path(args.target_list)
    source_rows = _read_commented_csv(source_path)
    excluded_tic_ids = {
        int(tic_id)
        for event in read_events(args.exclude_ledger)
        if event.get("kind") == "campaign_completed"
        for tic_id in event.get("tic_ids", [])
    }
    for exclude_path_text in args.exclude_list:
        exclude_path = Path(exclude_path_text)
        with exclude_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                value = row.get("tic_id") or row.get("TICID") or row.get("target")
                match = re.search(r"\d+", str(value or ""))
                if match:
                    excluded_tic_ids.add(int(match.group()))

    groups: dict[tuple[int, int], list[dict[str, object]]] = {}
    seen: set[int] = set()
    for row in source_rows:
        tic_id = int(row["TICID"])
        tmag = float(row["Tmag"])
        if (
            tic_id in seen
            or tic_id in excluded_tic_ids
            or tmag < args.min_tmag
            or tmag > args.max_tmag
        ):
            continue
        seen.add(tic_id)
        camera = int(row["Camera"])
        ccd = int(row["CCD"])
        groups.setdefault((camera, ccd), []).append(
            {
                "target": f"TIC {tic_id}",
                "tic_id": tic_id,
                "sector": args.sector,
                "sectors": str(args.sector),
                "tmag": tmag,
                "ra_deg": _optional_float(row.get("RA")),
                "dec_deg": _optional_float(row.get("Dec")),
                "camera": camera,
                "ccd": ccd,
            }
        )
    prefer_small_stars = bool(getattr(args, "prefer_small_stars", False))
    if prefer_small_stars:
        from astroquery.mast import Catalogs

        candidates = [row for values in groups.values() for row in values]
        tic_ids = [int(row["tic_id"]) for row in candidates]
        metadata_by_tic: dict[int, object] = {}
        batch_size = max(1, int(getattr(args, "tic_query_batch_size", 500)))
        for start in range(0, len(tic_ids), batch_size):
            table = Catalogs.query_criteria(
                catalog="TIC", ID=tic_ids[start : start + batch_size]
            )
            for metadata in table:
                metadata_by_tic[int(str(metadata["ID"]))] = metadata
        max_radius = float(getattr(args, "max_stellar_radius", 2.0))
        max_teff = float(getattr(args, "max_teff", 7000.0))
        for row in candidates:
            metadata = metadata_by_tic.get(int(row["tic_id"]))
            if metadata is None:
                luminosity_class = "UNKNOWN"
                radius = None
                teff = None
                distance = None
            else:
                raw_lumclass = str(metadata["lumclass"]).strip().upper()
                luminosity_class = (
                    raw_lumclass
                    if raw_lumclass not in {"", "--", "NAN", "NONE"}
                    else "UNKNOWN"
                )
                radius = _optional_float(metadata["rad"])
                teff = _optional_float(metadata["Teff"])
                distance = _optional_float(metadata["d"])
            row.update(
                {
                    "teff_k": teff,
                    "stellar_radius_solar": radius,
                    "distance_pc": distance,
                    "luminosity_class": luminosity_class,
                    "stellar_selection_tier": _small_planet_selection_tier(
                        luminosity_class=luminosity_class,
                        stellar_radius_solar=radius,
                        teff_k=teff,
                        max_stellar_radius_solar=max_radius,
                        max_teff_k=max_teff,
                    ),
                    "small_planet_merit": round(
                        _small_planet_merit(
                            stellar_radius_solar=radius,
                            tmag=float(row["tmag"]),
                        ),
                        6,
                    ),
                }
            )
    for values in groups.values():
        values.sort(
            key=lambda row: (
                int(row.get("stellar_selection_tier", 0)),
                float(row.get("small_planet_merit", 0.0)),
                float(row["tmag"]),
                int(row["tic_id"]),
            )
        )

    selected: list[dict[str, object]] = []
    if prefer_small_stars:
        # Reserve one quarter of an equal-share allocation for every detector, then
        # spend the remaining sample on the strongest host-star merit globally.
        # This preserves broad detector coverage without forcing weak giant-star
        # targets into the list merely because one CCD has fewer suitable dwarfs.
        per_group_quota = max(1, args.limit // (max(1, len(groups)) * 4))
        selected_ids: set[int] = set()
        for rank in range(per_group_quota):
            for key in sorted(groups):
                if rank < len(groups[key]) and len(selected) < args.limit:
                    row = groups[key][rank]
                    selected.append(row)
                    selected_ids.add(int(row["tic_id"]))
        remaining = sorted(
            (
                row
                for values in groups.values()
                for row in values
                if int(row["tic_id"]) not in selected_ids
            ),
            key=lambda row: (
                int(row.get("stellar_selection_tier", 0)),
                float(row.get("small_planet_merit", 0.0)),
                float(row["tmag"]),
                int(row["tic_id"]),
            ),
        )
        selected.extend(remaining[: max(0, args.limit - len(selected))])
    else:
        rank = 0
        while len(selected) < args.limit:
            added = False
            for key in sorted(groups):
                if rank < len(groups[key]):
                    selected.append(groups[key][rank])
                    added = True
                    if len(selected) == args.limit:
                        break
            if not added:
                break
            rank += 1
    if len(selected) < args.limit:
        raise RuntimeError(
            f"Only {len(selected)} unsearched targets met the magnitude criteria; "
            f"{args.limit} were requested."
        )
    for index, row in enumerate(selected, start=1):
        row["selection_rank"] = index

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(selected[0]))
        writer.writeheader()
        writer.writerows(selected)
    manifest = {
        "created_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_target_list": str(source_path),
        "source_target_list_sha256": hashlib.sha256(
            source_path.read_bytes()
        ).hexdigest(),
        "output_csv_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "sector": args.sector,
        "criteria": {
            "tmag_range": [args.min_tmag, args.max_tmag],
            "prefer_small_stars": prefer_small_stars,
            "max_stellar_radius_solar": (
                float(args.max_stellar_radius) if prefer_small_stars else None
            ),
            "max_teff_k": float(args.max_teff) if prefer_small_stars else None,
            "excluded_completed_campaign_tic_ids": len(excluded_tic_ids),
            "exclude_ledger": str(args.exclude_ledger),
            "exclude_lists": [
                str(Path(value)) for value in args.exclude_list
            ],
            "ordering": (
                (
                    "quarter-share camera/CCD quota followed by a global fill; "
                    "small-planet stellar tier then approximate radius/brightness "
                    "merit then TESS magnitude and TIC ID"
                )
                if prefer_small_stars
                else (
                    "round-robin across camera/CCD groups; within each group, "
                    "TESS magnitude ascending then TIC ID"
                )
            ),
            "small_planet_merit_warning": (
                "The deterministic radius/brightness ranking improves target "
                "triage but is not an occurrence-rate model or completeness result."
                if prefer_small_stars
                else None
            ),
            "catalog_handling": (
                "NASA TOI and confirmed-planet rows are checked per target during "
                "batch-hunt; known ephemerides are masked before the residual search"
            ),
        },
        "source_rows": len(source_rows),
        "eligible_rows": sum(len(values) for values in groups.values()),
        "selected_count": len(selected),
        "targets": selected,
        "warning": (
            "Catalog absence and automated transit screening are not proof of a "
            "new planet; pixel, neighbor, TCE, literature, and multi-sector checks "
            "remain required."
        ),
    }
    _atomic_write_json(output_path.with_suffix(".json"), manifest)
    print(f"Selected {len(selected)} Sector {args.sector} stars for the campaign.")
    print(f"Saved {output_path} and {output_path.with_suffix('.json')}")
    return 0


def _make_blank_targets(args: argparse.Namespace) -> int:
    """Select small Sector target-list stars with no catalogued planet host entry."""

    from astroquery.mast import Catalogs

    target_list_path = Path(args.target_list)
    target_rows = _read_commented_csv(target_list_path)
    excluded_tic_ids: set[int] = set()
    for exclude_path_text in args.exclude_list:
        exclude_path = Path(exclude_path_text)
        with exclude_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                value = row.get("tic_id") or row.get("TICID") or row.get("target")
                if value is None:
                    continue
                match = re.search(r"\d+", str(value))
                if match:
                    excluded_tic_ids.add(int(match.group()))
    eligible_rows = sorted(
        (
            row
            for row in target_rows
            if args.min_tmag <= float(row["Tmag"]) <= args.max_tmag
        ),
        key=lambda row: (float(row["Tmag"]), int(row["TICID"])),
    )[: args.pool_size]
    if not eligible_rows:
        raise RuntimeError("No target-list rows satisfy the magnitude and pool criteria.")

    source_by_tic = {int(row["TICID"]): row for row in eligible_rows}
    tic_rows: dict[int, object] = {}
    tic_ids = list(source_by_tic)
    for start in range(0, len(tic_ids), 50):
        table = Catalogs.query_criteria(catalog="Tic", ID=tic_ids[start : start + 50])
        for row in table:
            tic_rows[int(row["ID"])] = row

    filtered: list[dict[str, object]] = []
    for tic_id, row in tic_rows.items():
        teff = _optional_float(row["Teff"])
        radius = _optional_float(row["rad"])
        distance = _optional_float(row["d"])
        tmag = _optional_float(row["Tmag"])
        if None in {teff, radius, distance, tmag}:
            continue
        if (
            teff > args.max_teff
            or radius > args.max_stellar_radius
            or distance > args.max_distance
        ):
            continue
        source = source_by_tic[tic_id]
        filtered.append(
            {
                "target": f"TIC {tic_id}",
                "tic_id": tic_id,
                "sector": args.sector,
                "sectors": str(args.sector),
                "tmag": tmag,
                "teff_k": teff,
                "stellar_radius_solar": radius,
                "distance_pc": distance,
                "ra_deg": _optional_float(row["ra"]),
                "dec_deg": _optional_float(row["dec"]),
                "camera": source.get("Camera"),
                "ccd": source.get("CCD"),
                "known_planet_host_rows": 0,
            }
        )
    known = known_planet_host_tic_ids([int(row["tic_id"]) for row in filtered])
    selected = sorted(
        (
            row
            for row in filtered
            if int(row["tic_id"]) not in known
            and int(row["tic_id"]) not in excluded_tic_ids
        ),
        key=lambda row: (float(row["tmag"]), int(row["tic_id"])),
    )[: args.limit]
    if not selected:
        raise RuntimeError("No zero-catalogued-planet stars satisfied all target criteria.")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(selected[0]))
        writer.writeheader()
        writer.writerows(selected)
    manifest = {
        "created_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_target_list": str(target_list_path),
        "sector": args.sector,
        "criteria": {
            "no_nasa_toi_or_confirmed_planet_host_row": True,
            "tmag_range": [args.min_tmag, args.max_tmag],
            "max_teff_k": args.max_teff,
            "max_stellar_radius_solar": args.max_stellar_radius,
            "max_distance_pc": args.max_distance,
            "input_pool_size": args.pool_size,
            "excluded_target_lists": args.exclude_list,
            "excluded_tic_ids": len(excluded_tic_ids),
            "ordering": "TESS magnitude ascending, then TIC ID",
        },
        "target_list_rows": len(target_rows),
        "tic_rows_queried": len(eligible_rows),
        "stellar_rows_passing": len(filtered),
        "known_hosts_excluded": len(known),
        "selected_count": len(selected),
        "targets": selected,
        "warning": (
            "NASA catalog absence is not proof of novelty; live ExoFOP, TCE, "
            "literature, neighbor, and pixel checks remain required."
        ),
    }
    manifest_path = output_path.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Selected {len(selected)} zero-catalogued-planet Sector {args.sector} stars.")
    print(f"Saved {output_path} and {manifest_path}")
    return 0


def _catalog_ephemerides(catalog: dict[str, object]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for row in catalog["tois"]:
        period = _optional_float(row.get("pl_orbper"))
        epoch = _optional_float(row.get("pl_tranmid"))
        duration = _optional_float(row.get("pl_trandurh"))
        if period and epoch and duration:
            events.append(
                {
                    "label": f"TOI-{row.get('toi')}",
                    "source": "NASA Exoplanet Archive TOI table",
                    "disposition": row.get("tfopwg_disp"),
                    "period_days": period,
                    "epoch_bjd": epoch,
                    "duration_hours": duration,
                }
            )
    for row in catalog["confirmed_planets"]:
        period = _optional_float(row.get("pl_orbper"))
        epoch = _optional_float(row.get("pl_tranmid"))
        duration = _optional_float(row.get("pl_trandur"))
        duplicate = period is not None and any(
            abs(float(event["period_days"]) - period) / period < 0.001
            for event in events
        )
        if row.get("tran_flag") == "1" and period and epoch and duration and not duplicate:
            events.append(
                {
                    "label": row.get("pl_name"),
                    "source": "NASA Exoplanet Archive confirmed planets table",
                    "disposition": "confirmed",
                    "period_days": period,
                    "epoch_bjd": epoch,
                    "duration_hours": duration,
                }
            )
    return events


def _known_transiting_periods(catalog: dict[str, object]) -> list[float]:
    """Count known transit periods even when a row lacks mask parameters.

    A missing duration must not make a multi-planet system appear to be a
    single-planet system. TOI rows explicitly marked false positive are not
    counted; all transiting confirmed-planet rows are counted.
    """

    periods: list[float] = []
    for row in catalog["tois"]:
        disposition = str(row.get("tfopwg_disp") or "").upper()
        period = _optional_float(row.get("pl_orbper"))
        if period and disposition not in {"FP", "FA"}:
            periods.append(period)
    for row in catalog["confirmed_planets"]:
        period = _optional_float(row.get("pl_orbper"))
        if period and str(row.get("tran_flag")) == "1":
            periods.append(period)
    unique: list[float] = []
    for period in sorted(periods):
        if not any(abs(period - known) / known < 0.01 for known in unique):
            unique.append(period)
    return unique


def _screening_flags(result) -> dict[str, bool]:
    duty_cycle = result.duration_hours / (result.period_days * 24.0)
    return {
        "white_noise_depth_snr_below_7_1": result.depth_snr < 7.1,
        "fewer_than_two_observed_transits": result.observed_transits < 2,
        "odd_even_mismatch_over_3_sigma": (
            result.odd_even_depth_difference_sigma is not None
            and result.odd_even_depth_difference_sigma > 3
        ),
        "secondary_eclipse_over_3_sigma": (
            result.secondary_snr is not None and result.secondary_snr > 3
        ),
        "transit_duty_cycle_over_15_percent": duty_cycle > 0.15,
        "transit_depth_over_5_percent": result.depth_ppm > 50_000,
    }


def _classify_screening_result(
    result,
    rejection_reasons: list[str],
    deeper_vetting: dict[str, object] | None = None,
) -> dict[str, object]:
    """Assign a follow-up class without claiming that any star is planet-free."""

    reasons = set(rejection_reasons)
    deeper_flags = [
        str(value)
        for value in (
            deeper_vetting.get("flags", [])
            if isinstance(deeper_vetting, dict)
            else []
        )
    ]
    recommended_sources = [
        "alternate TESS reduction (SPOC, QLP, or TGLC)",
        "additional TESS sectors",
        "Gaia DR3 neighbor and astrometry context",
        "Kepler or K2 light curves when sky coverage overlaps",
        "ZTF or ASAS-SN variability context when available",
    ]
    if not rejection_reasons:
        screening_class = "automated_survivor"
        if deeper_flags:
            vetting_tier = "needs_manual_review"
            priority = min(79, 55 + int(max(0.0, result.depth_snr - 7.1) / 4.0))
        else:
            vetting_tier = (
                "legacy_unmeasured"
                if deeper_vetting is None
                else "high_priority_followup"
            )
            priority = min(99, 75 + int(max(0.0, result.depth_snr - 7.1) / 2.0))
        followup = [
            "localize the signal in target pixels",
            "check nearby stars and official TCE records",
            "test independent TESS sectors when available",
            "compare an independently reduced TESS light curve",
        ]
    elif (
        "fewer than two transit events are represented" in reasons
        and result.depth_snr >= 7.1
        and not reasons.intersection(
            {
                "odd and even transit depths differ by more than 3 sigma",
                "a secondary eclipse is detected above 3 sigma",
                "the fitted transit duty cycle exceeds 15 percent",
                "the fitted transit depth exceeds 5 percent",
            }
        )
    ):
        screening_class = "single_event_lead"
        if deeper_flags:
            vetting_tier = "fragile_single_event"
            priority = min(69, 50 + int(max(0.0, result.depth_snr - 7.1) / 5.0))
        else:
            vetting_tier = (
                "legacy_unmeasured"
                if deeper_vetting is None
                else "supported_single_event"
            )
            priority = min(94, 65 + int(max(0.0, result.depth_snr - 7.1) / 3.0))
        followup = [
            "search earlier or later TESS sectors for another event",
            "inspect target-pixel localization and nearby sources",
            "fit a single-transit model before assigning an orbital period",
            "check cross-mission coverage for a longer time baseline",
        ]
    elif reasons == {"white-noise BLS depth S/N is below 7.1"}:
        screening_class = "no_transit_detected"
        vetting_tier = "deprioritized_for_this_window"
        priority = 5
        followup = [
            "deprioritize for this exact TESS window",
            "retain for longer-baseline or non-transit surveys",
        ]
    else:
        screening_class = "screened_rejected"
        vetting_tier = "strongest_signal_rejected"
        priority = 15
        followup = [
            "do not promote the strongest signal",
            "retain the star for possible weaker-signal or other-method searches",
        ]
    return {
        "screening_class": screening_class,
        "followup_priority": priority,
        "followup_reasons": followup,
        "vetting_tier": vetting_tier,
        "deeper_vetting_flags": deeper_flags,
        "recommended_data_sources": recommended_sources,
        "planet_free": False,
        "scope_warning": (
            "Classification applies only to detectable transits in the searched "
            "TESS sectors and period range."
        ),
    }


def _sensitivity_depth_at_period(
    sensitivity: dict[str, object] | None,
    period_days: float,
) -> float | None:
    if not isinstance(sensitivity, dict):
        return None
    rows = sensitivity.get("periods")
    if not isinstance(rows, list):
        return None
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and abs(float(row.get("period_days", -1)) - period_days) < 1e-6
    ]
    if not matches:
        return None
    value = matches[0].get("minimum_recovered_depth_ppm")
    return None if value is None else float(value)


def _hunt(args: argparse.Namespace) -> int:
    time, flux, metadata = _download_light_curve(
        args.target, args.sector, args.author, args.cadence_seconds
    )
    return _hunt_from_light_curve(args, time, flux, metadata)


def _hunt_from_light_curve(
    args: argparse.Namespace,
    time: np.ndarray,
    flux: np.ndarray,
    metadata: dict[str, object],
) -> int:
    tic_id = args.tic or metadata.get("tic_id")
    if not tic_id:
        raise RuntimeError("Could not infer a TIC ID; provide one with --tic.")
    catalog = check_tic(int(tic_id))
    ephemerides = _catalog_ephemerides(catalog)
    allow_no_known = bool(getattr(args, "allow_no_known", False))
    if not ephemerides and not allow_no_known:
        raise RuntimeError(
            "No catalogued TOI/confirmed transit ephemerides were available to mask."
        )
    known_periods = _known_transiting_periods(catalog)
    maskable_periods = [float(event["period_days"]) for event in ephemerides]
    unmaskable_periods = [
        period
        for period in known_periods
        if not any(abs(period - maskable) / period < 0.01 for maskable in maskable_periods)
    ]
    if unmaskable_periods:
        values = ", ".join(f"{period:.8g}" for period in unmaskable_periods)
        raise RuntimeError(
            "Known transiting signals lack a complete period/epoch/duration mask: " + values
        )
    if ephemerides:
        cleaned_time, cleaned_flux, mask_records = mask_periodic_events(
            time, flux, ephemerides, width_factor=args.mask_width
        )
    else:
        cleaned_time, cleaned_flux, mask_records = time, flux, []
    result, arrays = search_transits(
        cleaned_time,
        cleaned_flux,
        min_period_days=args.min_period,
        max_period_days=args.max_period,
    )
    alias_checks = harmonic_diagnostics(
        arrays["period_grid"], arrays["power"], result.period_days
    )
    known_relations = []
    for event in ephemerides:
        relation = compare_period(
            result.period_days,
            float(event["period_days"]),
            tolerance_fraction=0.05,
        )
        if relation["status"] != "miss":
            known_relations.append({"known_signal": event["label"], **relation})

    screening_flags = _screening_flags(result)
    strong_harmonic_ambiguity = any(
        float(check["relative_power"]) >= 0.8 for check in alias_checks
    )
    rejection_reasons: list[str] = []
    if screening_flags["white_noise_depth_snr_below_7_1"]:
        rejection_reasons.append("white-noise BLS depth S/N is below 7.1")
    if screening_flags["fewer_than_two_observed_transits"]:
        rejection_reasons.append("fewer than two transit events are represented")
    if screening_flags["odd_even_mismatch_over_3_sigma"]:
        rejection_reasons.append("odd and even transit depths differ by more than 3 sigma")
    if screening_flags["secondary_eclipse_over_3_sigma"]:
        rejection_reasons.append("a secondary eclipse is detected above 3 sigma")
    if screening_flags["transit_duty_cycle_over_15_percent"]:
        rejection_reasons.append("the fitted transit duty cycle exceeds 15 percent")
    if screening_flags["transit_depth_over_5_percent"]:
        rejection_reasons.append("the fitted transit depth exceeds 5 percent")
    if strong_harmonic_ambiguity:
        rejection_reasons.append("a simple harmonic retains at least 80% of the peak power")
    if known_relations:
        rejection_reasons.append(
            "the residual period is within 5% of a masked period or simple harmonic"
        )
    deeper_vetting = signal_vetting_diagnostics(
        cleaned_time,
        cleaned_flux,
        result,
    )
    classification = _classify_screening_result(
        result,
        rejection_reasons,
        deeper_vetting,
    )
    sensitivity = fixed_ephemeris_injection_sensitivity(cleaned_time, cleaned_flux)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _artifact_stem(str(args.target), int(tic_id), _sector_values(args.sector))
    report_path = output_dir / f"{stem}.json"
    plot_path = output_dir / f"{stem}.png"
    report = {
        "warning": (
            "Residual automated screening only. Catalog masking and a BLS peak do not "
            "establish a new planet candidate."
        ),
        "data": metadata,
        "search_configuration": _scientific_settings(args),
        "observation_window": {
            "start_btjd": float(np.nanmin(time)),
            "end_btjd": float(np.nanmax(time)),
            "measurements": int(len(time)),
        },
        "search_mode": "catalog-masked residual" if ephemerides else "zero-known-planet star",
        "catalog_checked": catalog,
        "known_signal_masks": mask_records,
        "mask_summary": {
            "original_measurements": int(len(time)),
            "remaining_measurements": int(len(cleaned_time)),
            "removed_fraction": 1.0 - len(cleaned_time) / len(time),
            "width_factor": args.mask_width,
        },
        "phase_curve": binned_phase_curve(
            cleaned_time,
            cleaned_flux,
            result.period_days,
            result.transit_time,
        ),
        "strongest_residual_signal": result.to_dict(),
        "search_grid": {
            "period_samples": int(len(arrays["period_grid"])),
            "effective_frequency_factor": float(arrays["effective_frequency_factor"]),
            "capped_for_long_baseline": bool(arrays["period_grid_was_capped"]),
        },
        "top_period_peaks": independent_period_peaks(
            arrays["period_grid"], arrays["power"]
        ),
        "harmonic_checks": alias_checks,
        "relations_to_masked_periods": known_relations,
        "screening_flags": {
            **screening_flags,
            "harmonic_ambiguity_over_0_8": strong_harmonic_ambiguity,
        },
        "sensitivity_probe": sensitivity,
        "deeper_vetting": deeper_vetting,
        "automated_triage": {
            "passes": not rejection_reasons,
            "rejection_reasons": rejection_reasons,
            "warning": "Passing this gate would still not establish a planet candidate.",
        },
        "followup_classification": classification,
    }
    temporary_plot = plot_path.with_name(plot_path.stem + ".tmp.png")
    _plot_result(result, arrays, temporary_plot)
    _replace_with_retry(temporary_plot, plot_path)
    # The report is the completion marker and is published only after its plot
    # is durable. Resume validation requires both artifacts.
    _atomic_write_json(report_path, report)
    args.generated_report_path = str(report_path)
    args.generated_plot_path = str(plot_path)
    if not getattr(args, "quiet", False):
        print(json.dumps(report, indent=2))
        print(f"\nSaved {report_path} and {plot_path}")
    return 0


def _validate(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for benchmark in BENCHMARKS:
        target = str(benchmark["target"])
        sector = int(benchmark["sector"])
        try:
            time, flux, metadata = _download_light_curve(
                target, sector, "SPOC", args.cadence_seconds
            )
            result, arrays = search_transits(
                time,
                flux,
                min_period_days=float(benchmark["min_period_days"]),
                max_period_days=float(benchmark["max_period_days"]),
            )
            comparison = compare_period(
                result.period_days, float(benchmark["expected_period_days"])
            )
            stem = _safe_name(f"{target}_s{sector}")
            plot_path = output_dir / f"{stem}.png"
            report_path = output_dir / f"{stem}.json"
            _plot_result(result, arrays, plot_path)
            report = {
                "warning": "Known-planet benchmark; screening metrics are approximate.",
                "benchmark": benchmark,
                "data": metadata,
                "strongest_signal": result.to_dict(),
                "comparison": comparison,
            }
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            row = {
                "target": target,
                "planet": benchmark["planet"],
                "purpose": benchmark["purpose"],
                "expected_period_days": benchmark["expected_period_days"],
                "recovered_period_days": result.period_days,
                "recovered_depth_ppm": result.depth_ppm,
                **comparison,
                "report": str(report_path),
                "plot": str(plot_path),
            }
        except Exception as exc:
            row = {
                "target": target,
                "planet": benchmark["planet"],
                "status": "error",
                "error": str(exc),
            }
        rows.append(row)
        print(
            f"{row['planet']}: {row['status']}"
            + (
                f" ({float(row['recovered_period_days']):.8f} d)"
                if "recovered_period_days" in row
                else f" ({row.get('error', 'unknown error')})"
            )
        )

    counts = {
        status: sum(row.get("status") == status for row in rows)
        for status in ("exact", "harmonic_alias", "miss", "error")
    }
    summary = {
        "source": "NASA Exoplanet Archive default solutions, queried 2026-07-22",
        "cadence_seconds": args.cadence_seconds,
        "counts": counts,
        "benchmarks": rows,
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    record_validation(summary_path)
    print(f"\nSaved benchmark summary to {summary_path}")
    return 1 if counts["miss"] or counts["error"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="exohunt", description="Download and screen public TESS light curves."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze", help="Run a BLS search on one target.")
    analyze.add_argument("--target", required=True, help="Name, coordinates, or 'TIC 123'.")
    analyze.add_argument(
        "--sector",
        type=int,
        nargs="+",
        help="Download one or more selected TESS sectors, e.g. --sector 1 3 4.",
    )
    analyze.add_argument(
        "--author", default="SPOC", choices=["SPOC", "TESS-SPOC", "QLP", "TESScut"]
    )
    analyze.add_argument(
        "--cadence-seconds",
        type=float,
        default=120.0,
        help="Select one exposure time and avoid mixing duplicate cadences (default: 120).",
    )
    analyze.add_argument("--min-period", type=float, default=0.5)
    analyze.add_argument("--max-period", type=float, default=13.5)
    analyze.add_argument("--output-dir", default="results")
    analyze.set_defaults(func=_analyze)

    catalog = subparsers.add_parser(
        "catalog-check", help="Check a TIC ID against NASA's TOI and confirmed tables."
    )
    catalog.add_argument("--tic", type=int, required=True)
    catalog.set_defaults(func=_catalog)

    make_targets = subparsers.add_parser(
        "make-targets", help="Build a reproducible pilot target list from NASA and MAST."
    )
    make_targets.add_argument("--output", default="targets/pilot_cool_single_hosts.csv")
    make_targets.add_argument("--limit", type=int, default=3)
    make_targets.add_argument("--pool-size", type=int, default=20)
    make_targets.add_argument("--max-tmag", type=float, default=11.5)
    make_targets.add_argument("--max-teff", type=float, default=4200.0)
    make_targets.add_argument("--max-stellar-radius", type=float, default=0.8)
    make_targets.add_argument("--max-distance", type=float, default=100.0)
    make_targets.add_argument("--known-min-period", type=float, default=0.5)
    make_targets.add_argument("--known-max-period", type=float, default=20.0)
    make_targets.add_argument("--min-sectors", type=int, default=2)
    make_targets.add_argument("--sectors-per-target", type=int, default=3)
    make_targets.add_argument("--cadence-seconds", type=float, default=120.0)
    make_targets.add_argument(
        "--author", choices=["SPOC", "TESS-SPOC", "QLP"], default="SPOC"
    )
    make_targets.add_argument(
        "--min-latest-sector",
        type=int,
        help="Require at least one available light curve at or after this sector.",
    )
    make_targets.add_argument(
        "--sector-strategy",
        choices=["compact", "latest"],
        default="compact",
        help="Choose a compact observing window or the newest available sectors.",
    )
    make_targets.set_defaults(func=_make_targets)

    sector_targets = subparsers.add_parser(
        "make-sector-targets",
        help="Build a large balanced campaign from an official TESS sector target list.",
    )
    sector_targets.add_argument("--target-list", required=True)
    sector_targets.add_argument("--sector", type=int, required=True)
    sector_targets.add_argument("--output", default="targets/sector_campaign.csv")
    sector_targets.add_argument("--limit", type=int, default=1000)
    sector_targets.add_argument("--min-tmag", type=float, default=7.0)
    sector_targets.add_argument("--max-tmag", type=float, default=12.0)
    sector_targets.add_argument(
        "--prefer-small-stars",
        action="store_true",
        help=(
            "Query compact TIC stellar metadata and rank dwarfs/smaller hosts "
            "ahead of giants within each camera/CCD group."
        ),
    )
    sector_targets.add_argument(
        "--max-stellar-radius",
        type=float,
        default=2.0,
        help="Preferred-host radius ceiling in solar radii (default: 2.0).",
    )
    sector_targets.add_argument(
        "--max-teff",
        type=float,
        default=7000.0,
        help="Preferred-host effective-temperature ceiling in kelvin (default: 7000).",
    )
    sector_targets.add_argument(
        "--tic-query-batch-size",
        type=int,
        default=500,
        help="TIC metadata IDs per catalog request (default: 500).",
    )
    sector_targets.add_argument(
        "--exclude-list",
        action="append",
        default=[],
        help="Additional campaign CSV to exclude; repeat for multiple lists.",
    )
    sector_targets.add_argument(
        "--exclude-ledger",
        default="metrics/events.jsonl",
        help="Ledger whose completed campaign TIC IDs should be excluded.",
    )
    sector_targets.set_defaults(func=_make_sector_targets)

    blank_targets = subparsers.add_parser(
        "make-blank-targets",
        help="Select small stars with no catalogued planets from a TESS sector target list.",
    )
    blank_targets.add_argument("--target-list", required=True)
    blank_targets.add_argument("--sector", type=int, required=True)
    blank_targets.add_argument("--output", default="targets/blank_sector_targets.csv")
    blank_targets.add_argument("--limit", type=int, default=10)
    blank_targets.add_argument("--pool-size", type=int, default=500)
    blank_targets.add_argument(
        "--exclude-list",
        action="append",
        default=[],
        help="CSV target list to exclude; repeat for multiple previous batches.",
    )
    blank_targets.add_argument("--min-tmag", type=float, default=7.0)
    blank_targets.add_argument("--max-tmag", type=float, default=11.0)
    blank_targets.add_argument("--max-teff", type=float, default=5000.0)
    blank_targets.add_argument("--max-stellar-radius", type=float, default=1.0)
    blank_targets.add_argument("--max-distance", type=float, default=200.0)
    blank_targets.set_defaults(func=_make_blank_targets)

    validate = subparsers.add_parser(
        "validate", help="Recover a curated set of known planets end to end."
    )
    validate.add_argument("--output-dir", default="results/validation")
    validate.add_argument("--cadence-seconds", type=float, default=120.0)
    validate.set_defaults(func=_validate)

    hunt = subparsers.add_parser(
        "hunt", help="Mask catalogued transits and search selected sectors for residual signals."
    )
    hunt.add_argument("--target", required=True, help="Name or 'TIC 123'.")
    hunt.add_argument("--tic", type=int, help="TIC ID if it cannot be inferred.")
    hunt.add_argument("--sector", type=int, nargs="+", required=True)
    hunt.add_argument(
        "--author", default="SPOC", choices=["SPOC", "TESS-SPOC", "QLP", "TESScut"]
    )
    hunt.add_argument("--cadence-seconds", type=float, default=120.0)
    hunt.add_argument("--min-period", type=float, default=0.5)
    hunt.add_argument("--max-period", type=float, default=30.0)
    hunt.add_argument(
        "--mask-width",
        type=float,
        default=1.5,
        help="Multiply catalog transit durations by this safety factor.",
    )
    hunt.add_argument("--output-dir", default="results/hunt")
    hunt.add_argument(
        "--allow-no-known",
        action="store_true",
        help="Search a star with no catalogued TOI/confirmed transit instead of requiring a mask.",
    )
    hunt.set_defaults(func=_hunt)

    batch = subparsers.add_parser(
        "batch-hunt", help="Run residual searches for every row in a target CSV."
    )
    batch.add_argument("--targets", required=True)
    batch.add_argument("--output-dir", default="results/campaign")
    batch.add_argument("--max-targets", type=int)
    batch.add_argument("--force", action="store_true", help="Re-run existing target reports.")
    batch.add_argument(
        "--author", default="SPOC", choices=["SPOC", "TESS-SPOC", "QLP", "TESScut"]
    )
    batch.add_argument("--cadence-seconds", type=float, default=120.0)
    batch.add_argument("--min-period", type=float, default=0.5)
    batch.add_argument("--max-period", type=float, default=20.0)
    batch.add_argument("--mask-width", type=float, default=1.5)
    batch.add_argument("--allow-no-known", action="store_true")
    batch.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Analyze this many targets concurrently while retaining one checkpoint "
            "writer (default: 1; recommended maximum for TESScut: 3-4)."
        ),
    )
    batch.add_argument(
        "--prefetch",
        type=int,
        help=(
            "Bound the number of downloaded, downloading, or analyzing targets. "
            "Defaults to twice --workers."
        ),
    )
    batch.add_argument(
        "--cache-max-gb",
        type=float,
        default=2.0,
        help=(
            "Keep at most this many decimal GB of re-downloadable FITS/FIT/ZIP "
            "cache (default: 2)."
        ),
    )
    batch.add_argument(
        "--workspace-max-gb",
        type=float,
        help=(
            "Hard ceiling for the entire project workspace. The rolling download "
            "cache is reduced as needed to preserve 0.5 GB of headroom."
        ),
    )
    batch.add_argument(
        "--retain-rejected-plots",
        action="store_true",
        help="Keep PNG diagnostics for rejected targets instead of retaining only their JSON.",
    )
    batch.set_defaults(func=_batch_hunt)

    storage_prune = subparsers.add_parser(
        "storage-prune",
        help="Bound the FITS cache and remove plots for rejected campaign targets.",
    )
    storage_prune.add_argument(
        "--cache-dir",
        default=os.environ.get("EXOHUNT_CACHE_DIR", "data/lightkurve"),
    )
    storage_prune.add_argument("--cache-max-gb", type=float, default=2.0)
    storage_prune.add_argument("--results-dir", default="results")
    storage_prune.add_argument(
        "--keep-rejected-plots",
        action="store_true",
        help="Prune only the FITS cache.",
    )
    storage_prune.add_argument(
        "--dry-run",
        action="store_true",
        help="Report exactly what would be removed without deleting anything.",
    )
    storage_prune.set_defaults(func=_storage_prune)

    pixel = subparsers.add_parser(
        "pixel-vet", help="Create a target-pixel difference image for a signal report."
    )
    pixel.add_argument("--report", required=True)
    pixel.add_argument("--sector", type=int, required=True)
    pixel.add_argument("--author", default="SPOC")
    pixel.add_argument("--cadence-seconds", type=float, default=120.0)
    pixel.add_argument("--output-dir", default="results/pixel")
    pixel.set_defaults(func=_pixel_vet)

    sector_vet = subparsers.add_parser(
        "sector-vet",
        help="Test whether a residual ephemeris is independently supported by multiple sectors.",
    )
    sector_vet.add_argument("--report", required=True)
    sector_vet.add_argument("--sector", type=int, nargs="+")
    sector_vet.add_argument("--author", default="SPOC")
    sector_vet.add_argument("--cadence-seconds", type=float, default=120.0)
    sector_vet.add_argument("--mask-width", type=float, default=1.5)
    sector_vet.add_argument("--min-sector-snr", type=float, default=3.0)
    sector_vet.add_argument("--min-supporting-sectors", type=int, default=2)
    sector_vet.add_argument("--output-dir", default="results/sector_vet")
    sector_vet.set_defaults(func=_sector_vet)

    tce_check = subparsers.add_parser(
        "tce-check", help="Compare a residual signal with public MAST TESS TCE tables."
    )
    tce_check.add_argument("--report", required=True)
    tce_check.add_argument("--sector", type=int, nargs="+")
    tce_check.add_argument("--output-dir", default="results/tce_checks")
    tce_check.set_defaults(func=_tce_check)

    context_vet = subparsers.add_parser(
        "context-vet",
        help=(
            "Collect metadata-only TIC, NASA catalog, MAST mission-coverage, "
            "and nearby-source context for a signal report."
        ),
    )
    context_vet.add_argument("--report", required=True)
    context_vet.add_argument(
        "--tic",
        type=int,
        help="TIC ID if it cannot be inferred from the report.",
    )
    context_vet.add_argument(
        "--mast-radius-arcsec",
        type=float,
        default=3.0,
        help="MAST observation-match radius (default: 3 arcsec).",
    )
    context_vet.add_argument(
        "--neighbor-radius-arcsec",
        type=float,
        default=42.0,
        help="TIC/Gaia-crossmatch crowding radius (default: 42 arcsec).",
    )
    context_vet.add_argument("--output-dir", default="results/context_vet")
    context_vet.set_defaults(func=_context_vet)

    inject = subparsers.add_parser(
        "inject-recover",
        help="Measure transit recovery in a real, catalog-masked light curve.",
    )
    inject.add_argument("--target", required=True, help="Name or 'TIC 123'.")
    inject.add_argument("--tic", type=int, help="TIC ID if it cannot be inferred.")
    inject.add_argument("--sector", type=int, nargs="+", required=True)
    inject.add_argument(
        "--periods",
        type=float,
        nargs="+",
        default=[1.0, 5.0, 12.0],
        help="Injected orbital periods in days (default: 1, 5, 12).",
    )
    inject.add_argument(
        "--depths",
        type=float,
        nargs="+",
        default=[100.0, 300.0, 1000.0],
        help="Injected transit depths in ppm (default: 100, 300, 1000).",
    )
    inject.add_argument(
        "--duration-hours",
        type=float,
        help="Fixed injected duration; otherwise scale duration with period.",
    )
    inject.add_argument("--seed", type=int, default=42)
    inject.add_argument(
        "--author", default="SPOC", choices=["SPOC", "TESS-SPOC", "QLP"]
    )
    inject.add_argument("--cadence-seconds", type=float, default=120.0)
    inject.add_argument("--min-period", type=float, default=0.5)
    inject.add_argument("--max-period", type=float, default=20.0)
    inject.add_argument(
        "--max-grid-size",
        type=int,
        default=100_000,
        help="Cap the BLS trial-period grid (default: 100000).",
    )
    inject.add_argument("--mask-width", type=float, default=1.5)
    inject.add_argument("--output-dir", default="results/completeness")
    inject.set_defaults(func=_inject_recover)

    packet = subparsers.add_parser(
        "candidate-packet",
        help="Create a review packet and ExoFOP parameter worksheet for a survivor.",
    )
    packet.add_argument("--report", required=True, help="Residual-search JSON report.")
    packet.add_argument("--pixel-report", help="Optional pixel-vet JSON report.")
    packet.add_argument("--sector-vet-report", help="Optional sector-vet JSON report.")
    packet.add_argument("--tce-check-report", help="Optional public-TCE check JSON report.")
    packet.add_argument("--submitter", default="[fill before sharing]")
    packet.add_argument("--contact-email", default="[fill before sharing]")
    packet.add_argument("--output-dir", default="output/candidate_packets")
    packet.add_argument("--pdf-output-dir", default="output/pdf")
    packet.add_argument(
        "--allow-rejected",
        action="store_true",
        help="Create a clearly marked draft for pipeline testing even if triage failed.",
    )
    packet.set_defaults(func=_candidate_packet)

    campaign_report = subparsers.add_parser(
        "campaign-report", help="Create Markdown and PDF reports from a batch summary."
    )
    campaign_report.add_argument("--summary", required=True)
    campaign_report.add_argument("--target-manifest")
    campaign_report.add_argument("--output-dir", default="output/reports")
    campaign_report.add_argument("--pdf-output-dir", default="output/pdf")
    campaign_report.set_defaults(func=_campaign_report)

    metrics_summary = subparsers.add_parser(
        "metrics-summary", help="Show cumulative search and outcome statistics."
    )
    metrics_summary.set_defaults(func=_metrics_summary)

    outcome = subparsers.add_parser(
        "log-outcome", help="Append a vetted candidate, confirmation, or false-positive outcome."
    )
    outcome.add_argument(
        "--kind",
        required=True,
        choices=["vetted_candidate", "confirmed_planet", "false_positive", "rediscovery"],
    )
    outcome.add_argument("--tic", type=int, required=True)
    outcome.add_argument("--label", required=True)
    outcome.add_argument("--notes", default="")
    outcome.add_argument("--source", default="manual")
    outcome.set_defaults(func=_log_outcome)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
