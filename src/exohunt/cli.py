"""Command-line interface for the exohunt starter."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
from dataclasses import asdict
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from filelock import FileLock, Timeout

from .catalogs import check_tic
from .benchmarks import BENCHMARKS, compare_period
# The campaign concern lives in `campaign`. Its entry points and support
# helpers are re-exported here because tests and historical callers import --
# and monkeypatch -- them on this module; `campaign` resolves its CLI-side
# collaborators through this module at call time, so those seams stay live.
from .campaign import (
    LEGACY_COMMON_MODE_REASON,
    LEGACY_COMMON_MODE_REASONS,
    _analyze_downloaded_batch_target,
    _batch_error_row,
    _batch_hunt,
    _batch_target_spec,
    _campaign_counts,
    _campaign_settings,
    _download_batch_target,
    _is_transient_search_error,
    _legacy_checkpoint_matches,
    _load_reusable_report,
    _performance_snapshot,
    _publish_followup_queue,
    _quarantine_invalid_common_mode,
    _read_target_rows,
    _result_row_from_report,
    _run_batch_hunt,
    _vetting_coverage,
)
from .context import query_cross_mission_context
from .config import CURRENT_CONFIG
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
from .detrending import DEFAULT_DETRENDING
from .paths import resolve_cache_dir
from .photometry import (
    AUTHOR_PREFERENCE,
    MIN_USEFUL_CADENCE_SECONDS,
    _available_products,
    _configured_lightkurve,
    _download_light_curve,
    _preferred_exposure,
    _thread_safe_lightkurve_download,
    resolve_light_curve_source,
)
from .pixel import difference_image, target_pixel_from_sky_grid
from .reporting import create_campaign_report, create_candidate_packet
from .search import build_search_grid, grid_rail_flags
from .screening import (
    CATALOG_PERIOD_REJECTION_REASON,
    _adjudicate_catalog_relation,
    _catalog_ephemerides,
    _classify_screening_result,
    _known_transiting_periods,
    _screening_flags,
)
from .metrics import (
    current_stats,
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
# Campaign target-list construction lives in `targets`. Its command entry
# points and ranking helpers are re-exported here because the parser wires them
# and tests import them on this module; `targets` resolves `_atomic_write_json`
# through this module at call time.
from .targets import (
    _available_lightcurve_sectors,
    _compact_sector_subset,
    _latest_sector_subset,
    _make_blank_targets,
    _make_sector_targets,
    _make_targets,
    _optional_float,
    _read_commented_csv,
    _small_planet_merit,
    _small_planet_selection_tier,
)
from .population import (
    cohort_key,
    encode_star_bins,
    registry_windows,
    star_bin_dips,
)
from .progress import TRACKER
from .tce import check_tces
from .vetoes import evaluate_t3_vetoes

# This command writes PNG files and must also work on headless/portable Python
# runtimes where Tk is not installed.
os.environ.setdefault("MPLBACKEND", "Agg")

_PLOT_LOCK = threading.Lock()


def _dip_registry_windows(
    args: argparse.Namespace,
    cohort: str,
) -> list[tuple[float, float]]:
    """Load registered systematic windows for one cohort, if a snapshot exists.

    MASTER_PLAN section 3.6 versions the window list "like any catalog
    snapshot", so it is supplied to a run rather than discovered by it: the
    campaign that builds a registry cannot also have consumed it. A missing,
    unreadable, or cohort-less snapshot yields no windows, which leaves the
    veto inert -- never silently permissive, because the report records that
    no registry was applied.
    """

    setting = getattr(args, "dip_registry", None)
    if not setting:
        return []
    path = Path(setting)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(
            f"warning: could not read dip registry {path}; "
            "continuing with no absolute-time window veto",
            file=sys.stderr,
        )
        return []
    if not isinstance(payload, dict):
        return []
    # Accept either a single registry or a {cohort_key: registry} mapping.
    cohorts = payload.get("cohorts")
    if isinstance(cohorts, dict):
        return registry_windows(cohorts.get(cohort))
    if payload.get("cohort") in (None, cohort):
        return registry_windows(payload)
    return []


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

    from .paths import workspace_cache_dir

    return workspace_cache_dir(cache_dir, workspace_root=workspace_root)


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
        "catalog_masking": asdict(CURRENT_CONFIG.catalog_masking),
        # Round-trip through JSON so tuple-valued config fields compare equal
        # after reports are serialized and loaded for checkpoint reuse.
        "search": json.loads(json.dumps(asdict(CURRENT_CONFIG.search))),
        "vetoes": json.loads(json.dumps(asdict(CURRENT_CONFIG.vetoes))),
        # Detrending decides which cadences BLS ever sees, so it belongs to a
        # result's scientific identity. Without it here a resumed campaign would
        # silently reuse reports produced under a different segmentation rule
        # and mix two reductions inside one catalog -- exactly what would have
        # happened to the 1,886 targets already searched before the edge-safe
        # change.
        "detrending": asdict(DEFAULT_DETRENDING),
        "data_pipeline_version": (
            "tesscut-bgsub-commonmode-quarantined-v4"
            if args.author == "TESScut"
            else "processed-lc-v3-edge-safe"
        ),
    }


def _artifact_stem(target: str, tic_id: int, sectors: list[int]) -> str:
    identity = target if str(tic_id) in target else f"TIC {tic_id} {target}"
    return _safe_name(identity + _sector_suffix(sectors) + "_residual")


def _ledger_import(args: argparse.Namespace) -> int:
    from . import ledger
    from .importer import import_workspace, parity_check

    conn = ledger.connect(args.db)
    try:
        report = import_workspace(
            conn,
            args.workspace,
            include_orphan_reports=not args.skip_orphan_reports,
        )
        print(json.dumps(report, indent=2))
        if args.parity:
            parity = parity_check(conn, args.workspace)
            print(
                json.dumps(
                    {
                        "parity_match": parity["match"],
                        "exporter_total": parity["exporter_total"],
                        "ledger_total": parity["ledger_total"],
                        "differences": parity["differences"],
                        "star_status_differences": parity[
                            "star_status_differences"
                        ],
                        "star_payload_differences": parity[
                            "star_payload_differences"
                        ],
                    },
                    indent=2,
                )
            )
            if not parity["match"]:
                print(
                    "Parity gate FAILED: ledger projection differs from the "
                    "exporter.",
                    file=sys.stderr,
                )
                return 1
            print("Parity gate passed: ledger projection matches the exporter.")
        return 0
    finally:
        conn.close()


def _ledger_status(args: argparse.Namespace) -> int:
    from . import ledger
    from .paths import default_db_path

    conn = ledger.connect(args.db)
    try:
        payload = {
            "db_path": str(args.db or default_db_path()),
            "stars": conn.execute("SELECT COUNT(*) FROM star").fetchone()[0],
            "evidence_rows": conn.execute(
                "SELECT COUNT(*) FROM evidence"
            ).fetchone()[0],
            "current_status_counts": ledger.status_counts(conn),
            "conclusions_logged": ledger.evidence_counts(conn),
            "signatures": {
                row["signature"] or "none": row["n"]
                for row in conn.execute(
                    "SELECT signature, COUNT(*) AS n FROM evidence "
                    "GROUP BY signature ORDER BY n DESC"
                )
            },
            "coordinator_lease": ledger.lease_status(conn, "coordinator"),
        }
        print(json.dumps(payload, indent=2))
        return 0
    finally:
        conn.close()


def _repair_checkpoints(args: argparse.Namespace) -> int:
    from .checkpoints import repair_stale_checkpoints

    report = repair_stale_checkpoints(
        args.results_root,
        stale_after_minutes=float(args.stale_minutes),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(report, indent=2))
    if report.get("refused"):
        print(
            "Refused: a live coordinator holds the machine lock.",
            file=sys.stderr,
        )
        return 1
    repaired = len(report.get("repaired", []))
    print(
        f"Repaired {repaired} stale checkpoint(s); liveness belongs to "
        "processes, not files."
    )
    return 0


def _storage_prune(args: argparse.Namespace) -> int:
    """Apply the same bounded retention policy outside a running campaign."""

    if not np.isfinite(float(args.cache_max_gb)) or float(args.cache_max_gb) <= 0:
        raise ValueError("--cache-max-gb must be a finite number greater than zero.")
    cache_dir = resolve_cache_dir(args.cache_dir, workspace_root=Path.cwd())
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
        unmaskable_masks = [
            record
            for record in masks
            if record.get("mask_status") != "masked"
        ]
        if unmaskable_masks:
            labels = ", ".join(
                str(record.get("label") or record.get("period_days"))
                for record in unmaskable_masks
            )
            raise RuntimeError(
                "Sector coherence cannot be measured after an untrustworthy "
                f"catalog mask; unmaskable: {labels}"
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

    signal = source_report.get("strongest_residual_signal")
    if not isinstance(signal, dict):
        signal = source_report.get("candidate_signal")
    data_sectors = data.get("requested_sectors", [])
    sectors = _context_sector_values(data_sectors)
    context = query_cross_mission_context(
        int(tic_id),
        mast_radius_arcsec=args.mast_radius_arcsec,
        neighbor_radius_arcsec=args.neighbor_radius_arcsec,
        signal=signal if isinstance(signal, dict) else None,
        sectors=sectors,
    )
    context["source_report"] = str(source_report_path)
    context["signal_under_review"] = signal if isinstance(signal, dict) else None
    context["initial_scan_evidence"] = _compact_initial_scan_evidence(
        source_report,
        source_report_path,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"TIC_{int(tic_id)}_cross_mission_context.json"
    _atomic_write_json(report_path, context)
    print(json.dumps(context, indent=2))
    print(f"\nSaved {report_path}")
    return 0


def _context_sector_values(value: object) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = str(value).replace(",", ";").split(";")
    sectors: set[int] = set()
    for raw in raw_values:
        try:
            sector = int(str(raw).strip())
        except ValueError:
            continue
        if sector > 0:
            sectors.add(sector)
    return sorted(sectors)


def _compact_initial_scan_evidence(
    report: dict[str, object],
    report_path: str | Path,
) -> dict[str, object]:
    """Retain the important first-pass evidence without copying large arrays."""

    data = report.get("data")
    data = data if isinstance(data, dict) else {}
    signal = report.get("strongest_residual_signal")
    signal = signal if isinstance(signal, dict) else {}
    triage = report.get("automated_triage")
    triage = triage if isinstance(triage, dict) else {}
    deeper = report.get("deeper_vetting")
    deeper = deeper if isinstance(deeper, dict) else {}
    sensitivity = report.get("sensitivity_probe")
    sensitivity = sensitivity if isinstance(sensitivity, dict) else {}
    catalog = report.get("catalog_checked")
    catalog = catalog if isinstance(catalog, dict) else {}
    return {
        "source_report": str(report_path),
        "tic_id": data.get("tic_id"),
        "searched_sectors": _context_sector_values(
            data.get("requested_sectors")
        ),
        "search_configuration": report.get("search_configuration"),
        "observation_window": report.get("observation_window"),
        "search_mode": report.get("search_mode"),
        "strongest_signal": {
            key: signal.get(key)
            for key in (
                "period_days",
                "transit_time",
                "duration_hours",
                "depth_ppm",
                "depth_snr",
                "observed_transits",
                "odd_even_depth_difference_sigma",
                "secondary_snr",
            )
        },
        "automated_triage": triage,
        "screening_flags": report.get("screening_flags"),
        "deeper_vetting": {
            key: deeper.get(key)
            for key in (
                "flags",
                "red_noise_adjusted_snr",
                "event_coverage_fraction",
                "positive_depth_event_fraction",
                "out_of_event_baseline_fraction",
            )
        },
        "sensitivity_probe": sensitivity,
        "initial_nasa_catalog_snapshot": catalog,
        "known_signal_masks": report.get("known_signal_masks"),
        "relations_to_masked_periods": report.get(
            "relations_to_masked_periods"
        ),
    }


def _queue_initial_scan_evidence(
    queue_row: dict[str, object],
) -> list[dict[str, object]]:
    supplied = queue_row.get("initial_scan_evidence")
    if isinstance(supplied, list):
        return [row for row in supplied if isinstance(row, dict)]
    if isinstance(supplied, dict):
        return [supplied]
    raw_paths = queue_row.get("source_reports")
    if not isinstance(raw_paths, list):
        raw_paths = [queue_row.get("report")]
    evidence: list[dict[str, object]] = []
    for raw_path in raw_paths:
        if not raw_path:
            continue
        path = Path(str(raw_path))
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(report, dict):
            evidence.append(_compact_initial_scan_evidence(report, path))
    return evidence


def _context_queue_result(
    queue_row: dict[str, object],
    *,
    context_path: Path | None,
    context: dict[str, object] | None,
    run_state: str,
    error: str = "",
) -> dict[str, object]:
    mast = context.get("mast_holdings", {}) if context else {}
    mast = mast if isinstance(mast, dict) else {}
    tess = mast.get("tess", {})
    tess = tess if isinstance(tess, dict) else {}
    neighbors = context.get("neighbor_context", {}) if context else {}
    neighbors = neighbors if isinstance(neighbors, dict) else {}
    collections = mast.get("collection_counts", {})
    collections = collections if isinstance(collections, dict) else {}
    classification = context.get("context_classification", {}) if context else {}
    classification = classification if isinstance(classification, dict) else {}
    source_incomplete = (
        classification.get("disposition") == "context_incomplete"
    )
    source_error = ""
    if source_incomplete:
        states = classification.get("source_states", {})
        failed_sources = [
            str(name)
            for name, state in (
                states.items() if isinstance(states, dict) else []
            )
            if state == "error"
        ]
        source_error = (
            "authoritative metadata source(s) incomplete: "
            + ", ".join(failed_sources or ["unknown source"])
        )
    return {
        "tic_id": int(queue_row["tic_id"]),
        "target": queue_row.get("target"),
        "followup_priority": int(queue_row.get("followup_priority", 0)),
        "vetting_tier": queue_row.get("vetting_tier"),
        "status": "error" if error or source_incomplete else "completed",
        "run_state": run_state,
        "context_report": str(context_path) if context_path else "",
        "error": error or source_error,
        "mast_observation_records": int(mast.get("observation_records", 0)),
        "mast_collection_counts": json.dumps(collections, sort_keys=True),
        "tess_sectors": ",".join(str(value) for value in tess.get("all_sectors", [])),
        "alternate_tess_reductions": ",".join(
            str(value) for value in tess.get("alternate_reductions", [])
        ),
        "crowding_risk": neighbors.get("crowding_risk", ""),
        "context_disposition": classification.get("disposition", ""),
        "followup_lane": classification.get("followup_lane", ""),
        "context_followup_priority": int(
            classification.get("followup_priority", 0)
        ),
        "known_binary_host": bool(
            classification.get("known_binary_host", False)
        ),
        "evidence_source_states": json.dumps(
            classification.get("source_states", {}),
            sort_keys=True,
        ),
        "exact_period_match_count": len(
            classification.get("exact_period_matches", [])
        ),
        "recommended_action_count": len(
            context.get("recommended_actions", []) if context else []
        ),
    }


def _run_context_vet_queue(args: argparse.Namespace) -> int:
    """Vet a saved follow-up queue using compact, metadata-only mission queries."""

    queue_path = Path(args.queue)
    queue_payload = json.loads(queue_path.read_text(encoding="utf-8"))
    raw_targets = queue_payload.get("targets", [])
    if not isinstance(raw_targets, list) or not raw_targets:
        raise RuntimeError("Context-vetting queue contains no targets.")

    targets: list[dict[str, object]] = []
    seen_tic_ids: set[int] = set()
    for raw in raw_targets:
        if not isinstance(raw, dict) or not raw.get("tic_id"):
            raise RuntimeError("Every context-vetting queue row needs a TIC ID.")
        tic_id = int(raw["tic_id"])
        if tic_id in seen_tic_ids:
            raise RuntimeError(f"Duplicate TIC ID in context-vetting queue: {tic_id}.")
        seen_tic_ids.add(tic_id)
        targets.append(raw)
    if args.max_targets is not None:
        if int(args.max_targets) <= 0:
            raise ValueError("--max-targets must be greater than zero.")
        targets = targets[: int(args.max_targets)]

    workers = int(args.workers)
    if workers <= 0 or workers > 4:
        raise ValueError("Use between 1 and 4 context-vetting workers.")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    progress_path = output_dir / "context_vet_progress.json"
    results_by_tic: dict[int, dict[str, object]] = {}
    pending: deque[dict[str, object]] = deque()

    for queue_row in targets:
        tic_id = int(queue_row["tic_id"])
        context_path = output_dir / f"TIC_{tic_id}_cross_mission_context.json"
        if context_path.exists() and not args.force:
            try:
                context = json.loads(context_path.read_text(encoding="utf-8"))
                context_tic = context.get("tic", {})
                if (
                    not isinstance(context_tic, dict)
                    or int(context_tic.get("tic_id", 0)) != tic_id
                    or int(context.get("schema_version", 0)) < 2
                    or (
                        isinstance(
                            context.get("context_classification"),
                            dict,
                        )
                        and context["context_classification"].get(
                            "disposition"
                        )
                        == "context_incomplete"
                    )
                ):
                    raise ValueError("context report TIC ID does not match")
                results_by_tic[tic_id] = _context_queue_result(
                    queue_row,
                    context_path=context_path,
                    context=context,
                    run_state="reused",
                )
                continue
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        pending.append(queue_row)

    def ordered_results() -> list[dict[str, object]]:
        return [
            results_by_tic[int(row["tic_id"])]
            for row in targets
            if int(row["tic_id"]) in results_by_tic
        ]

    def publish(state: str) -> None:
        results = ordered_results()
        errors = sum(row["status"] == "error" for row in results)
        _atomic_write_json(
            progress_path,
            {
                "schema_version": 1,
                "state": state,
                "warning": (
                    "This workflow queries catalogs and observation metadata only. "
                    "It downloads zero telescope science products."
                ),
                "queue": str(queue_path),
                "output_dir": str(output_dir),
                "started_at_utc": started_at,
                "updated_at_utc": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat(),
                "total_targets": len(targets),
                "completed_targets": len(results),
                "counts": {
                    "completed": len(results) - errors,
                    "error": errors,
                    "remaining": len(targets) - len(results),
                },
                "runtime": {
                    "workers": workers,
                    "science_products_downloaded": 0,
                },
                "results": results,
            },
        )

    publish("running")
    futures: dict[Future[dict[str, object]], dict[str, object]] = {}
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="exohunt-context"
    ) as executor:
        while pending or futures:
            while pending and len(futures) < workers:
                queue_row = pending.popleft()
                future = executor.submit(
                    query_cross_mission_context,
                    int(queue_row["tic_id"]),
                    mast_radius_arcsec=args.mast_radius_arcsec,
                    neighbor_radius_arcsec=args.neighbor_radius_arcsec,
                    signal={
                        key: queue_row.get(key)
                        for key in (
                            "period_days",
                            "depth_ppm",
                            "depth_snr",
                            "observed_transits",
                        )
                    },
                    sectors=_context_sector_values(
                        queue_row.get("sectors")
                    ),
                )
                futures[future] = queue_row
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                queue_row = futures.pop(future)
                tic_id = int(queue_row["tic_id"])
                context_path = output_dir / f"TIC_{tic_id}_cross_mission_context.json"
                try:
                    context = future.result()
                    context["source_queue"] = str(queue_path)
                    context["source_report"] = queue_row.get("report")
                    context["source_reports"] = queue_row.get(
                        "source_reports",
                        [queue_row.get("report")],
                    )
                    context["signal_under_review"] = {
                        key: queue_row.get(key)
                        for key in (
                            "period_days",
                            "depth_ppm",
                            "depth_snr",
                            "observed_transits",
                        )
                    }
                    context["initial_scan_evidence"] = (
                        _queue_initial_scan_evidence(queue_row)
                    )
                    _atomic_write_json(context_path, context)
                    result = _context_queue_result(
                        queue_row,
                        context_path=context_path,
                        context=context,
                        run_state="completed",
                    )
                except Exception as error:
                    result = _context_queue_result(
                        queue_row,
                        context_path=None,
                        context=None,
                        run_state="error",
                        error=str(error),
                    )
                results_by_tic[tic_id] = result
                publish("running")

    results = ordered_results()
    errors = sum(row["status"] == "error" for row in results)
    final_state = "completed" if errors == 0 else "retry_pending"
    publish(final_state)
    summary = json.loads(progress_path.read_text(encoding="utf-8"))
    _atomic_write_json(output_dir / "context_vet_summary.json", summary)
    fieldnames = [
        "tic_id",
        "target",
        "followup_priority",
        "vetting_tier",
        "status",
        "run_state",
        "context_report",
        "error",
        "mast_observation_records",
        "mast_collection_counts",
        "tess_sectors",
        "alternate_tess_reductions",
        "crowding_risk",
        "context_disposition",
        "followup_lane",
        "context_followup_priority",
        "known_binary_host",
        "evidence_source_states",
        "exact_period_match_count",
        "recommended_action_count",
    ]
    temporary = output_dir / "context_vet_summary.csv.tmp"
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    _replace_with_retry(temporary, output_dir / "context_vet_summary.csv")
    print(
        f"Cross-mission metadata vetting: {len(results) - errors} complete, "
        f"{errors} error(s), {len(results)} total."
    )
    return 0 if errors == 0 else 1


def _context_vet_queue(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(output_dir / ".context-vet.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as error:
        raise RuntimeError(
            f"Another context-vetting worker already owns {output_dir}."
        ) from error
    try:
        return _run_context_vet_queue(args)
    finally:
        lock.release()


def build_history_context_queue(
    campaign_root: str | Path,
    output_path: str | Path,
    *,
    minimum_priority: int = 50,
) -> dict[str, object]:
    """Build one deduplicated survivor queue from every saved campaign.

    Existing JSON reports are summarized, not re-analyzed, so old stars inherit
    the upgraded metadata rules without another TESS download.
    """

    root = Path(campaign_root)
    if not root.exists():
        raise RuntimeError(f"Campaign root does not exist: {root}")
    if minimum_priority < 0 or minimum_priority > 100:
        raise ValueError("Minimum priority must be between 0 and 100.")
    grouped: dict[int, list[dict[str, object]]] = {}
    checkpoints_read = 0
    for campaign_dir in sorted(
        {path.parent for path in root.rglob("batch_progress.json")}
    ):
        progress_path = campaign_dir / "batch_progress.json"
        try:
            payload = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        checkpoints_read += 1
        for row in payload.get("results", []):
            if not isinstance(row, dict) or not row.get("tic_id"):
                continue
            if row.get("status") == "error":
                continue
            priority = int(row.get("followup_priority", 0))
            if priority < minimum_priority:
                continue
            grouped.setdefault(int(row["tic_id"]), []).append(
                {
                    **row,
                    "campaign_checkpoint": str(progress_path),
                }
            )

    targets: list[dict[str, object]] = []
    for tic_id, rows in grouped.items():
        ordered = sorted(
            rows,
            key=lambda row: (
                -int(row.get("followup_priority", 0)),
                str(row.get("completed_at_utc") or ""),
            ),
        )
        primary = dict(ordered[0])
        reports = list(
            dict.fromkeys(
                str(row["report"])
                for row in ordered
                if row.get("report")
            )
        )
        primary["tic_id"] = tic_id
        primary["source_reports"] = reports
        primary["prior_scan_count"] = len(ordered)
        primary["initial_scan_evidence"] = _queue_initial_scan_evidence(
            {"source_reports": reports}
        )
        primary["campaign_checkpoints"] = list(
            dict.fromkeys(
                str(row["campaign_checkpoint"]) for row in ordered
            )
        )
        targets.append(primary)
    targets.sort(
        key=lambda row: (
            -int(row.get("followup_priority", 0)),
            int(row["tic_id"]),
        )
    )
    payload = {
        "schema_version": 2,
        "generated_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "campaign_root": str(root),
        "checkpoints_read": checkpoints_read,
        "minimum_priority": minimum_priority,
        "warning": (
            "These are unresolved automated leads, not planet candidates. "
            "The queue preserves first-pass evidence and adds metadata-only "
            "known-object vetting without redownloading TESS science products."
        ),
        "targets": targets,
    }
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(destination, payload)
    csv_path = destination.with_suffix(".csv")
    fieldnames = [
        "tic_id",
        "target",
        "sectors",
        "screening_class",
        "followup_priority",
        "vetting_tier",
        "period_days",
        "depth_ppm",
        "depth_snr",
        "observed_transits",
        "prior_scan_count",
        "report",
    ]
    temporary = csv_path.with_name(csv_path.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(
            {key: row.get(key) for key in fieldnames}
            for row in targets
        )
    _replace_with_retry(temporary, csv_path)
    return payload


def _common_mode_screen(args: argparse.Namespace) -> int:
    """Flag signals whose ephemeris is shared by many unrelated targets."""

    from .commonmode import screen_campaign_root

    payload = screen_campaign_root(args.campaign_root, workspace=".")
    if not payload["verdicts"]:
        print("No searched campaign carried a fitted ephemeris to screen.")
        return 1

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(destination, payload)

    csv_path = destination.with_suffix(".csv")
    fieldnames = [
        "tic_id",
        "campaign",
        "verdict",
        "shared_targets",
        "expected_shared_targets",
        "enrichment",
        "shared_fraction",
        "period_group_targets",
        "cameras_spanned",
        "detectors_spanned",
        "sky_spread_deg",
        "shared_epoch_btjd",
    ]
    temporary = csv_path.with_name(csv_path.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for tic_id, verdict in payload["verdicts"].items():
            writer.writerow({"tic_id": tic_id, **verdict})
    _replace_with_retry(temporary, csv_path)

    counts = payload["counts"]
    total = payload["screened_targets"]
    print(f"Screened {total} target(s) across {len(payload['campaigns'])} campaign(s).")
    for verdict, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {verdict}: {count} ({100 * count / total:.1f}%)")
    print(f"Wrote {destination} and {csv_path}")
    print(
        "A shared ephemeris is evidence against an astrophysical origin; "
        "surviving signals are still not planet candidates."
    )
    return 0


def _build_context_queue(args: argparse.Namespace) -> int:
    payload = build_history_context_queue(
        args.campaign_root,
        args.output,
        minimum_priority=int(args.minimum_priority),
    )
    print(
        f"Saved {len(payload['targets'])} deduplicated historical lead(s) "
        f"from {payload['checkpoints_read']} campaign checkpoint(s) to "
        f"{args.output}."
    )
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
    unmaskable_records = [
        record
        for record in mask_records
        if record.get("mask_status") != "masked"
    ]
    if unmaskable_records:
        labels = ", ".join(
            str(record.get("label") or record.get("period_days"))
            for record in unmaskable_records
        )
        raise RuntimeError(
            "Cannot run residual injection recovery because catalogued signals "
            f"could not be masked safely: {labels}"
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
    TRACKER.stage(tic_id, "masking")
    if ephemerides:
        cleaned_time, cleaned_flux, mask_records = mask_periodic_events(
            time, flux, ephemerides, width_factor=args.mask_width
        )
    else:
        cleaned_time, cleaned_flux, mask_records = time, flux, []
    known_periods = _known_transiting_periods(catalog)
    masked_records = [
        record
        for record in mask_records
        if record.get("mask_status") == "masked"
    ]
    maskable_periods = [
        float(record["period_days"]) for record in masked_records
    ]
    unmaskable_periods = [
        period
        for period in known_periods
        if not any(
            abs(period - maskable) / period < 0.01
            for maskable in maskable_periods
        )
    ]
    if unmaskable_periods and not allow_no_known:
        values = ", ".join(f"{period:.8g}" for period in unmaskable_periods)
        raise RuntimeError(
            "Known transiting signals lack a complete or sufficiently precise "
            "catalog ephemeris and could not be demonstrably masked from the "
            f"observed cadences: {values}"
        )
    searched_sectors = _sector_values(
        metadata.get("downloaded_sectors") or args.sector
    )
    TRACKER.stage(tic_id, "searching")
    search_grid_plan = build_search_grid(
        baseline_days=float(
            np.nanmax(cleaned_time) - np.nanmin(cleaned_time)
        ),
        single_sector=len(searched_sectors) <= 1,
        requested_min_period_days=float(args.min_period),
        requested_max_period_days=float(args.max_period),
        stellar_radius_solar=_optional_float(
            metadata.get("stellar_radius_solar")
        ),
        stellar_mass_solar=_optional_float(
            metadata.get("stellar_mass_solar")
        ),
    )
    result, arrays = search_transits(
        cleaned_time,
        cleaned_flux,
        min_period_days=search_grid_plan.period.min_period_days,
        max_period_days=search_grid_plan.period.max_search_days,
        durations_hours=search_grid_plan.duration_hours,
    )
    searched_duration_grid = arrays.get(
        "duration_grid_hours",
        search_grid_plan.duration_hours,
    )
    requested_duration_grid = arrays.get(
        "requested_duration_grid_hours",
        search_grid_plan.duration_hours,
    )
    grid_flags = grid_rail_flags(
        period_days=result.period_days,
        duration_hours=result.duration_hours,
        searched_periods_days=arrays["period_grid"],
        searched_durations_hours=searched_duration_grid,
    )
    best_period_in_overscan = search_grid_plan.period.in_overscan(
        result.period_days
    )
    alias_checks = harmonic_diagnostics(
        arrays["period_grid"], arrays["power"], result.period_days
    )
    known_period_records = [dict(record) for record in mask_records]
    represented_periods = [
        float(record["period_days"]) for record in mask_records
    ]
    known_period_records.extend(
        {
            "label": f"catalogued transit period {period:.8g} d",
            "period_days": period,
            "mask_status": "unmasked_incomplete_ephemeris",
        }
        for period in unmaskable_periods
        if not any(
            abs(period - represented) / period < 0.01
            for represented in represented_periods
        )
    )
    known_relations = []
    for event in known_period_records:
        relation = compare_period(
            result.period_days,
            float(event["period_days"]),
            tolerance_fraction=0.05,
        )
        if relation["status"] != "miss":
            known_relations.append(
                {
                    "known_signal": event["label"],
                    "mask_status": event["mask_status"],
                    **relation,
                    **_adjudicate_catalog_relation(
                        relation,
                        event,
                        recovered_period_days=result.period_days,
                        recovered_transit_time_btjd=result.transit_time,
                        recovered_duration_hours=result.duration_hours,
                        start_btjd=float(np.nanmin(time)),
                        end_btjd=float(np.nanmax(time)),
                    ),
                }
            )

    minimum_supported_events = (
        CURRENT_CONFIG.search.min_transits_single_sector
        if search_grid_plan.single_sector
        else CURRENT_CONFIG.search.min_transits_multisector
    )
    t3_density = (
        search_grid_plan.stellar_density_solar
        if search_grid_plan.density_source
        == "catalog_stellar_mass_and_radius"
        else None
    )
    # T4 contribution and consumption (MASTER_PLAN 3.6). The star records its
    # own absolute-time bin flags so the cohort registry stays rebuildable
    # from durable reports alone, and consumes a published registry snapshot
    # if one was supplied. A first pass over a fresh cohort has no snapshot,
    # so the veto is inert and the report says so explicitly.
    population_cohort = cohort_key(
        _sector_values(args.sector)[0] if _sector_values(args.sector) else None,
        metadata.get("camera"),
        metadata.get("ccd"),
    )
    population_bins = encode_star_bins(
        star_bin_dips(cleaned_time, cleaned_flux)
    )
    dip_windows = _dip_registry_windows(args, population_cohort)
    TRACKER.stage(tic_id, "vetting")
    t3_vetoes = evaluate_t3_vetoes(
        cleaned_time,
        cleaned_flux,
        dip_windows=dip_windows,
        dip_registry_scope=population_cohort,
        period_days=result.period_days,
        transit_time=result.transit_time,
        duration_hours=result.duration_hours,
        depth_ppm=result.depth_ppm,
        density_solar=t3_density,
        stellar_radius_solar=_optional_float(
            metadata.get("stellar_radius_solar")
        ),
        minimum_supported_events=minimum_supported_events,
    )
    screening_flags = {
        **_screening_flags(result),
        "odd_even_mismatch_over_3_sigma": (
            t3_vetoes["checks"]["odd_even"]["verdict"] == "kill"
        ),
        "secondary_eclipse_over_3_sigma": (
            t3_vetoes["checks"]["full_phase_secondary"]["verdict"]
            == "kill"
        ),
    }
    strong_harmonic_ambiguity = any(
        float(check["relative_power"]) >= 0.8 for check in alias_checks
    )
    rejection_reasons: list[str] = []
    if screening_flags["white_noise_depth_snr_below_7_1"]:
        rejection_reasons.append("white-noise BLS depth S/N is below 7.1")
    if screening_flags["fewer_than_two_observed_transits"]:
        rejection_reasons.append("fewer than two transit events are represented")
    if screening_flags["transit_duty_cycle_over_15_percent"]:
        rejection_reasons.append("the fitted transit duty cycle exceeds 15 percent")
    if screening_flags["transit_depth_over_5_percent"]:
        rejection_reasons.append("the fitted transit depth exceeds 5 percent")
    if best_period_in_overscan:
        rejection_reasons.append(
            "the best-fit period is in the search-grid overscan zone"
        )
    if grid_flags["grid_rail"]:
        rejection_reasons.append(
            "the best-fit period or duration is pinned to a search-grid rail"
        )
    if strong_harmonic_ambiguity:
        rejection_reasons.append("a simple harmonic retains at least 80% of the peak power")
    if unmaskable_periods:
        rejection_reasons.append(
            "one or more known transiting signals lacked a complete or "
            "sufficiently precise catalog ephemeris and could not be "
            "demonstrably masked from the observed cadences; this is an "
            "unmasked recovery-only scan"
        )
    if any(
        bool(relation["catalog_match_rejects"])
        for relation in known_relations
    ):
        rejection_reasons.append(CATALOG_PERIOD_REJECTION_REASON)
    rejection_reasons.extend(t3_vetoes["rejection_reasons"])
    deeper_vetting = signal_vetting_diagnostics(
        cleaned_time,
        cleaned_flux,
        result,
    )
    classification = _classify_screening_result(
        result,
        rejection_reasons,
        deeper_vetting,
        t3_vetoes,
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
        "search_mode": (
            "partially masked known-signal recovery"
            if masked_records and unmaskable_periods
            else "catalog-masked residual"
            if masked_records
            else "unmasked known-signal recovery"
            if unmaskable_periods
            else "zero-known-planet star"
        ),
        "catalog_checked": catalog,
        "known_signal_masks": mask_records,
        "known_signal_mask_limitations": {
            "unmaskable_periods_days": unmaskable_periods,
            "unmaskable_events": [
                {
                    "label": record.get("label"),
                    "period_days": record.get("period_days"),
                    "mask_status": record.get("mask_status"),
                    "mask_reason": record.get("mask_reason"),
                    "phase_uncertainty_days": record.get(
                        "phase_uncertainty_days"
                    ),
                    "maximum_maskable_phase_uncertainty_days": record.get(
                        "maximum_maskable_phase_uncertainty_days"
                    ),
                }
                for record in mask_records
                if record.get("mask_status") != "masked"
            ],
            "reason": (
                "catalog signals could not be demonstrably masked from the "
                "available ephemeris and observed cadences"
                if unmaskable_periods
                else None
            ),
            "promotion_allowed": not bool(unmaskable_periods),
        },
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
            **search_grid_plan.to_dict(),
            "requested_duration_grid_hours": [
                float(value) for value in requested_duration_grid
            ],
            "duration_grid_hours": [
                float(value) for value in searched_duration_grid
            ],
            "best_period_in_overscan": best_period_in_overscan,
            **grid_flags,
        },
        "top_period_peaks": independent_period_peaks(
            arrays["period_grid"], arrays["power"]
        ),
        "harmonic_checks": alias_checks,
        "relations_to_known_periods": known_relations,
        "catalog_epoch_agreement": [
            relation
            for relation in known_relations
            if not str(relation["epoch_verdict"]).startswith(
                "not_evaluated_"
            )
        ],
        # Retained for backward-compatible consumers; each row now states
        # whether the corresponding catalog period was actually masked.
        "relations_to_masked_periods": known_relations,
        "screening_flags": {
            **screening_flags,
            "harmonic_ambiguity_over_0_8": strong_harmonic_ambiguity,
        },
        "t3_vetoes": t3_vetoes,
        "population_bins": {
            "cohort": population_cohort,
            **population_bins,
            "scope": (
                "This star's own absolute-time dip flags. The cohort "
                "registry is derived from these across every star observed "
                "together; one star's flags say nothing on their own."
            ),
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
    TRACKER.stage(tic_id, "writing")
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
        "--author",
        default="auto",
        choices=["auto", "SPOC", "TESS-SPOC", "QLP", "TESScut"],
        help=(
            "auto prefers SPOC, then TESS-SPOC, then QLP, and falls back to a "
            "local TESScut extraction only when no processed light curve exists."
        ),
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
        "--author",
        default="auto",
        choices=["auto", "SPOC", "TESS-SPOC", "QLP", "TESScut"],
        help=(
            "auto prefers SPOC, then TESS-SPOC, then QLP, and falls back to a "
            "local TESScut extraction only when no processed light curve exists."
        ),
    )
    hunt.add_argument("--cadence-seconds", type=float, default=120.0)
    hunt.add_argument(
        "--min-period",
        type=float,
        default=0.5,
        help="Minimum searched period, subject to the configured physical floor.",
    )
    hunt.add_argument(
        "--max-period",
        type=float,
        default=30.0,
        help=(
            "Maximum reportable period; the search adds a diagnostic overscan "
            "zone whose fits cannot pass triage."
        ),
    )
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
        "--author",
        default="auto",
        choices=["auto", "SPOC", "TESS-SPOC", "QLP", "TESScut"],
        help=(
            "auto prefers SPOC, then TESS-SPOC, then QLP, and falls back to a "
            "local TESScut extraction only when no processed light curve exists."
        ),
    )
    batch.add_argument("--cadence-seconds", type=float, default=120.0)
    batch.add_argument(
        "--min-period",
        type=float,
        default=0.5,
        help="Minimum searched period, subject to the configured physical floor.",
    )
    batch.add_argument(
        "--max-period",
        type=float,
        default=20.0,
        help=(
            "Maximum reportable period; the search adds a diagnostic overscan "
            "zone whose fits cannot pass triage."
        ),
    )
    batch.add_argument("--mask-width", type=float, default=1.5)
    batch.add_argument("--allow-no-known", action="store_true")
    batch.add_argument(
        "--allow-sleep",
        action="store_true",
        help=(
            "Let the computer sleep during the campaign. By default an "
            "unattended run holds the system awake so it is not stranded "
            "part-way through the cohort."
        ),
    )
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
        "--download-workers",
        type=int,
        help=(
            "Download this many targets concurrently (default: min(2, --workers); "
            "use 3 cautiously for a download-bound TESScut campaign)."
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
        default=os.environ.get("EXOHUNT_CACHE_DIR"),
        help=(
            "FITS cache to bound. Defaults to the active cache location "
            "(EXOHUNT_CACHE_DIR or the local state root outside OneDrive)."
        ),
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

    context_queue = subparsers.add_parser(
        "context-vet-queue",
        help=(
            "Process a deep-followup queue with compact TIC, NASA catalog, MAST "
            "mission-coverage, and nearby-source metadata queries."
        ),
    )
    context_queue.add_argument("--queue", required=True)
    context_queue.add_argument(
        "--output-dir", default="results/context_vet_queue"
    )
    context_queue.add_argument("--max-targets", type=int)
    context_queue.add_argument("--workers", type=int, default=2)
    context_queue.add_argument("--force", action="store_true")
    context_queue.add_argument(
        "--mast-radius-arcsec", type=float, default=3.0
    )
    context_queue.add_argument(
        "--neighbor-radius-arcsec", type=float, default=42.0
    )
    context_queue.set_defaults(func=_context_vet_queue)

    history_queue = subparsers.add_parser(
        "build-context-queue",
        help=(
            "Build one deduplicated metadata-vetting queue from every saved "
            "campaign survivor/single-event lead without redownloading TESS data."
        ),
    )
    history_queue.add_argument(
        "--campaign-root",
        default="results/campaign",
    )
    history_queue.add_argument(
        "--output",
        default="results/vetting/all_campaigns/context_queue.json",
    )
    history_queue.add_argument(
        "--minimum-priority",
        type=int,
        default=50,
    )
    history_queue.set_defaults(func=_build_context_queue)

    common_mode = subparsers.add_parser(
        "common-mode-screen",
        help=(
            "Flag signals whose fitted ephemeris is shared by many unrelated "
            "targets, which indicates an observatory systematic rather than a star."
        ),
    )
    common_mode.add_argument(
        "--campaign-root",
        default="results/campaign",
    )
    common_mode.add_argument(
        "--output",
        default="results/vetting/common_mode/common_mode_screen.json",
    )
    common_mode.set_defaults(func=_common_mode_screen)

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

    repair = subparsers.add_parser(
        "repair-checkpoints",
        help=(
            "Mark stale running/finalizing checkpoints as interrupted when no "
            "live coordinator holds the machine lock."
        ),
    )
    repair.add_argument("--results-root", default="results")
    repair.add_argument(
        "--stale-minutes",
        type=float,
        default=10.0,
        help="Idle time after which a live-state checkpoint counts as orphaned.",
    )
    repair.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be repaired without writing anything.",
    )
    repair.set_defaults(func=_repair_checkpoints)

    ledger_import = subparsers.add_parser(
        "ledger-import",
        help=(
            "Import the file-based campaign history into the evidence ledger "
            "and optionally verify parity against the dashboard exporter."
        ),
    )
    ledger_import.add_argument("--workspace", default=".")
    ledger_import.add_argument(
        "--db",
        default=None,
        help="Ledger database path (default: the local state root).",
    )
    ledger_import.add_argument(
        "--skip-orphan-reports",
        action="store_true",
        help="Skip per-target reports of campaigns that never wrote a summary.",
    )
    ledger_import.add_argument(
        "--parity",
        action="store_true",
        help=(
            "After importing, compare the ledger projection against a fresh "
            "exporter run; non-zero exit on any difference."
        ),
    )
    ledger_import.set_defaults(func=_ledger_import)

    ledger_status = subparsers.add_parser(
        "ledger-status",
        help="Show ledger contents: current states, logged conclusions, leases.",
    )
    ledger_status.add_argument("--db", default=None)
    ledger_status.set_defaults(func=_ledger_status)
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
