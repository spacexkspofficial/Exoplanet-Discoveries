"""Measure segment-edge trend-model bias on cached SPOC photometry.

`P2_EDGE_DIAGNOSTIC.md` requires that any next edge-recovery design "measure or
avoid trend-model bias itself". This script measures it, for both the shipping
Savitzky-Golay estimator and the built-but-unwired biweight, on the same real
light curves and with the same paired construction.

It is strictly offline: light curves are read from cached FITS files by path
and no MAST search or download is ever issued, so it is a diagnostic rather
than a science download. Point ``--cache-dir`` at the Lightkurve cache root.

Note that ``EXOHUNT_CACHE_DIR`` is frequently absent from a non-login shell's
environment even when it is set as a user variable; this script therefore
requires the directory explicitly rather than resolving a default that might
silently be empty.
"""

from __future__ import annotations

import argparse
import json
import sys
import time as clock
import warnings
from pathlib import Path
from typing import Any

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from exohunt.config import CURRENT_CONFIG  # noqa: E402
from exohunt.detrend import segment_boundaries  # noqa: E402
from exohunt.detrending import (  # noqa: E402
    DEFAULT_DETRENDING,
    build_detrending_plan,
    edge_safe_mask,
)
from exohunt.edge_bias import (  # noqa: E402
    DEFAULT_SEED,
    biweight_trend_estimator,
    concatenate_samples,
    full_support_floor_ppm,
    guard_retention,
    measure_segment_edge_bias,
    profile_by_offset,
    savgol_trend_estimator,
    sufficient_guard_cadences,
)


def cached_light_curve_paths(cache_dir: Path) -> list[Path]:
    """Every cached SPOC light-curve FITS file, in a stable order."""

    return sorted(cache_dir.rglob("*_lc.fits"))


def load_normalized(path: Path):
    """Reproduce the shipping preparation up to, but not including, flattening.

    `photometry.py` stitches downloaded products through
    ``remove_nans().normalize().remove_outliers(sigma_upper=4, sigma_lower=20)``
    and hands the result to ``flatten_edge_safe``. A single cached sector file
    is that same pipeline with a one-product collection, so the flux measured
    here is the flux the detrender actually receives.
    """

    import lightkurve as lk

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        curve = lk.read(str(path), quality_bitmask="default")
    detrend_config = CURRENT_CONFIG.detrend
    return (
        curve.remove_nans()
        .normalize()
        .remove_outliers(
            sigma_upper=detrend_config.outlier_sigma_upper,
            sigma_lower=detrend_config.outlier_sigma_lower,
        )
    )


def longest_segment(
    time: np.ndarray, flux: np.ndarray, gap_days: float
) -> tuple[np.ndarray, np.ndarray]:
    """The longest uninterrupted stretch, which is where edges are synthesized.

    Segments are split at the same 0.10 d threshold production uses, so a
    synthesized edge sits inside genuinely contiguous data rather than
    straddling a real interruption whose edge effects are already present.
    """

    bounds = segment_boundaries(time, gap_days)
    if not bounds:
        raise ValueError("No segments found.")
    start, stop = max(bounds, key=lambda pair: pair[1] - pair[0])
    return time[start:stop], flux[start:stop]


def aggregate_across_stars(
    per_star: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Median each profile column across stars, keyed by offset bin.

    Pooling every cadence into one RMS is not usable here: cached Sector 100
    small stars span roughly 1,400 to 86,000 ppm of point-to-point scatter, so
    a pooled RMS describes the noisiest star in the sample rather than the
    estimator. Taking a per-star profile first and a median across stars after
    gives every star one vote.
    """

    by_offset: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for rows in per_star:
        for row in rows:
            by_offset.setdefault(
                (row["offset_low"], row["offset_high"]), []
            ).append(row)

    numeric = (
        "observed_rms_ppm",
        "white_null_rms_ppm",
        "excess_bias_ppm",
        "tracked_trend_excess_ppm",
        "residual_structure_ppm",
        "ratio_observed_to_white_null",
    )
    aggregated: list[dict[str, Any]] = []
    for (low, high), rows in sorted(by_offset.items()):
        entry: dict[str, Any] = {
            "offset_low": low,
            "offset_high": high,
            "support_fraction": rows[0]["support_fraction"],
            "stars": len(rows),
        }
        for column in numeric:
            values = [
                row[column]
                for row in rows
                if row.get(column) is not None
                and np.isfinite(row[column])
            ]
            entry[f"median_{column}"] = (
                round(float(np.median(values)), 3) if values else None
            )
        aggregated.append(entry)
    return aggregated


def point_to_point_ppm(flux: np.ndarray, trend: np.ndarray) -> float:
    """White-noise amplitude, immune to whatever the trend failed to remove."""

    residual = flux / np.where(np.abs(trend) > 0, trend, np.nan)
    finite = residual[np.isfinite(residual)]
    if finite.size < 3:
        return float("nan")
    return float(np.std(np.diff(finite)) / np.sqrt(2.0) * 1.0e6)


def measure_one(
    path: Path,
    *,
    truncation_points: int,
    seed: int,
    estimators: tuple[str, ...],
) -> dict[str, Any]:
    """Measure one star with each requested estimator on its longest segment."""

    curve = load_normalized(path)
    time = np.asarray(curve.time.value, dtype=float)
    flux = np.asarray(curve.flux.value, dtype=float)
    finite = np.isfinite(time) & np.isfinite(flux)
    time, flux = time[finite], flux[finite]
    if time.size < 500:
        raise ValueError("Too few finite cadences.")

    plan = build_detrending_plan(time, DEFAULT_DETRENDING)
    segment_time, segment_flux = longest_segment(
        time, flux, DEFAULT_DETRENDING.segment_gap_days
    )

    detrend_config = CURRENT_CONFIG.detrend
    result: dict[str, Any] = {
        "path": str(path),
        "cadences": int(time.size),
        "segment_cadences": int(segment_time.size),
        "cadence_days": plan.cadence_days,
        "savgol_window_cadences": plan.window_cadences,
        "samples": {},
    }
    for name in estimators:
        if name == "savgol":
            # The production guard removes exactly this many cadences at each
            # edge, so the measured offsets span precisely what it discards.
            half = plan.window_cadences // 2
            estimator = savgol_trend_estimator(
                plan.window_cadences,
                break_tolerance=plan.break_tolerance_cadences,
            )
        elif name == "biweight":
            half = int(
                round(detrend_config.short_window_days / plan.cadence_days / 2)
            )
            estimator = biweight_trend_estimator(
                detrend_config.short_window_days,
                break_tolerance_days=detrend_config.segment_gap_days,
            )
        else:
            raise ValueError(f"Unknown estimator {name!r}.")
        result["samples"][name] = measure_segment_edge_bias(
            segment_time,
            segment_flux,
            estimator=estimator,
            half_window_cadences=half,
            truncation_points=truncation_points,
            seed=seed,
        )
        result.setdefault("point_to_point_ppm", {})[name] = round(
            point_to_point_ppm(segment_flux, estimator(segment_time, segment_flux)),
            1,
        )
    return result


def measure_retention(
    path: Path, guard_grid: list[int]
) -> dict[str, Any]:
    """Retention a guard would leave, on one star's prepared time axis.

    Cheap by comparison with the bias pass: no trend is fitted at all. The
    production entry is computed from the unmodified plan, so it should
    reproduce the documented 0.669 median and is the check that this pass and
    the shipping detrender agree.
    """

    curve = load_normalized(path)
    time = np.asarray(curve.time.value, dtype=float)
    time = time[np.isfinite(time)]
    if time.size < 500:
        raise ValueError("Too few finite cadences.")
    plan = build_detrending_plan(time, DEFAULT_DETRENDING)
    keep, segments = edge_safe_mask(time, plan)
    return {
        "path": str(path),
        "cadences": int(time.size),
        "segments": int(segments),
        "production_guard_days": plan.edge_guard_days,
        "production_retention": round(
            float(np.count_nonzero(keep) / keep.size), 5
        ),
        "retention_by_guard": {
            str(guard): round(guard_retention(time, plan, guard), 5)
            for guard in guard_grid
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-stars", type=int, default=40)
    parser.add_argument("--truncation-points", type=int, default=32)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--estimator",
        action="append",
        choices=["savgol", "biweight"],
        help="Repeatable; defaults to both.",
    )
    parser.add_argument(
        "--retention-only",
        action="store_true",
        help=(
            "Skip the bias fits and measure only what each guard width costs "
            "in retention. Minutes rather than tens of minutes."
        ),
    )
    parser.add_argument(
        "--guard-cadences",
        type=int,
        action="append",
        help=(
            "Repeatable guard width for the retention pass. Defaults to a "
            "grid spanning zero to the production half-window."
        ),
    )
    parser.add_argument(
        "--quiet-ppm",
        type=float,
        default=10000.0,
        help=(
            "Point-to-point scatter below which a star is summarized in the "
            "'quiet' subset, whose absolute ppm biases are comparable with "
            "transit depths. Defaults to 10000 ppm."
        ),
    )
    parser.add_argument(
        "--depth-tolerance-ppm",
        type=float,
        action="append",
        help=(
            "Repeatable bias tolerance for the sufficient-guard summary. "
            "Defaults to 100, 500 and 1000 ppm."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cache_dir = args.cache_dir.expanduser()
    if not cache_dir.is_dir():
        raise SystemExit(f"Cache directory not found: {cache_dir}")
    estimators = tuple(args.estimator or ["savgol", "biweight"])
    tolerances = args.depth_tolerance_ppm or [100.0, 500.0, 1000.0]

    paths = cached_light_curve_paths(cache_dir)
    if not paths:
        raise SystemExit(f"No cached *_lc.fits files under {cache_dir}")
    # A deterministic stride over the sorted cache spreads the sample across
    # the whole cached population rather than favouring one TIC range.
    stride = max(1, len(paths) // args.max_stars)
    selected = paths[::stride][: args.max_stars]

    started = clock.monotonic()

    if args.retention_only:
        guard_grid = sorted(
            set(
                args.guard_cadences
                or [0, 100, 200, 297, 300, 400, 500, 600, 626, 700, 720]
            )
        )
        measured: list[dict[str, Any]] = []
        failures = []
        for index, path in enumerate(selected, start=1):
            try:
                measured.append(measure_retention(path, guard_grid))
            except Exception as error:  # noqa: BLE001 - recorded, not swallowed
                failures.append({"path": str(path), "error": repr(error)})
                print(f"[{index}/{len(selected)}] FAILED {path.name}: {error}")
                continue
            print(f"[{index}/{len(selected)}] {path.name}")
        report = {
            "mode": "retention",
            "cache_dir": str(cache_dir),
            "measured_stars": len(measured),
            "failures": failures,
            "guard_cadences": guard_grid,
            "runtime_seconds": round(clock.monotonic() - started, 1),
            "median_production_retention": round(
                float(
                    np.median([row["production_retention"] for row in measured])
                ),
                5,
            )
            if measured
            else None,
            "median_retention_by_guard": {
                str(guard): round(
                    float(
                        np.median(
                            [
                                row["retention_by_guard"][str(guard)]
                                for row in measured
                            ]
                        )
                    ),
                    5,
                )
                for guard in guard_grid
            }
            if measured
            else {},
            "stars": measured,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nWrote {args.output}")
        print(
            "median production retention: "
            f"{report['median_production_retention']}"
        )
        for guard, value in report["median_retention_by_guard"].items():
            print(f"  guard {guard:>4} cadences -> retention {value}")
        return 0

    pooled: dict[str, list[Any]] = {name: [] for name in estimators}
    stars: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, path in enumerate(selected, start=1):
        try:
            measured = measure_one(
                path,
                truncation_points=args.truncation_points,
                seed=args.seed,
                estimators=estimators,
            )
        except Exception as error:  # noqa: BLE001 - recorded, not swallowed
            failures.append({"path": str(path), "error": repr(error)})
            print(f"[{index}/{len(selected)}] FAILED {path.name}: {error}")
            continue
        for name, samples in measured["samples"].items():
            pooled[name].append(samples)
        stars.append(
            {
                key: value
                for key, value in measured.items()
                if key != "samples"
            }
        )
        print(
            f"[{index}/{len(selected)}] {path.name} "
            f"segment={measured['segment_cadences']} cadences"
        )

    report: dict[str, Any] = {
        "cache_dir": str(cache_dir),
        "cached_files": len(paths),
        "requested_stars": args.max_stars,
        "measured_stars": len(stars),
        "failures": failures,
        "truncation_points": args.truncation_points,
        "seed": args.seed,
        "runtime_seconds": round(clock.monotonic() - started, 1),
        "estimators": {},
        "stars": stars,
    }
    def summarize(subset: list[Any]) -> dict[str, Any] | None:
        """Aggregate one estimator over a set of per-star sample blocks."""

        if not subset:
            return None
        samples = concatenate_samples(subset)
        # Floors are taken per star and medianed, for the same reason the
        # profile is: one very noisy star would otherwise set the floor for
        # the whole sample.
        floors = [full_support_floor_ppm(one) for one in subset]
        floors = [value for value in floors if np.isfinite(value)]
        zero_fractions = []
        for one in subset:
            deepest = one.observed_ppm[one.offset == one.offset.max()]
            zero_fractions.append(
                float(np.count_nonzero(deepest == 0.0) / max(deepest.size, 1))
            )
        median_guards: dict[str, Any] = {}
        for tolerance in tolerances:
            widths = [
                width
                for one in subset
                if (
                    width := sufficient_guard_cadences(
                        one, tolerance_ppm=tolerance
                    )
                )
                is not None
            ]
            median_guards[str(tolerance)] = (
                int(np.median(widths)) if widths else None
            )
        return {
            "stars": len(subset),
            "half_window_cadences": samples.half_window_cadences,
            "total_samples": int(samples.offset.size),
            # Read every bias below against this floor: it is what the
            # instrument returns where the true answer is zero.
            "median_full_support_floor_ppm": (
                round(float(np.median(floors)), 3) if floors else None
            ),
            "median_full_support_exact_zero_fraction": round(
                float(np.median(zero_fractions)), 4
            ),
            "median_profile_across_stars": aggregate_across_stars(
                [profile_by_offset(one) for one in subset]
            ),
            "median_sufficient_guard_cadences": median_guards,
        }

    for name in estimators:
        if not pooled[name]:
            continue
        # A stride over the whole cache is noise-dominated: cached Sector 100
        # targets reach 42% point-to-point scatter, where every edge error is
        # variance and an absolute ppm bias means little. The quiet subset is
        # the one whose ppm numbers can be compared against a transit depth.
        quiet = [
            block
            for block, star in zip(pooled[name], stars)
            if np.isfinite(star.get("point_to_point_ppm", {}).get(name, np.nan))
            and star["point_to_point_ppm"][name] <= args.quiet_ppm
        ]
        report["estimators"][name] = {
            "all_stars": summarize(pooled[name]),
            "quiet_stars": summarize(quiet),
            "quiet_threshold_ppm": args.quiet_ppm,
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWrote {args.output}")
    for name, block in report["estimators"].items():
        for label in ("all_stars", "quiet_stars"):
            entry = block[label]
            if entry is None:
                print(f"{name} [{label}]: no stars")
                continue
            print(
                f"{name} [{label}]: {entry['stars']} stars, "
                f"half-window {entry['half_window_cadences']}, "
                f"floor {entry['median_full_support_floor_ppm']} ppm, "
                f"guard {entry['median_sufficient_guard_cadences']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
