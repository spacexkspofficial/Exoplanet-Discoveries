"""Difference-image calculations for target-pixel vetting."""

from __future__ import annotations

import numpy as np


def transit_cadence_masks(
    time: np.ndarray,
    period_days: float,
    transit_time: float,
    duration_hours: float,
    *,
    out_inner_factor: float = 1.5,
    out_outer_factor: float = 5.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Return in-transit and nearby out-of-transit cadence masks."""

    t = np.asarray(time, dtype=float)
    duration_days = duration_hours / 24.0
    if period_days <= 0 or duration_days <= 0:
        raise ValueError("Period and duration must be positive.")
    distance = np.abs(((t - transit_time + period_days / 2) % period_days) - period_days / 2)
    in_transit = distance <= duration_days / 2
    out_transit = (
        (distance >= duration_days * out_inner_factor)
        & (distance <= duration_days * out_outer_factor)
    )
    return in_transit, out_transit


def difference_image(
    time: np.ndarray,
    flux_cube: np.ndarray,
    period_days: float,
    transit_time: float,
    duration_hours: float,
) -> dict[str, object]:
    """Calculate median in/out images and the centroid of lost light."""

    cube = np.asarray(flux_cube, dtype=float)
    if cube.ndim != 3 or cube.shape[0] != len(time):
        raise ValueError("Flux cube must have shape (cadence, row, column).")
    in_mask, out_mask = transit_cadence_masks(
        time, period_days, transit_time, duration_hours
    )
    n_in = int(np.count_nonzero(in_mask))
    n_out = int(np.count_nonzero(out_mask))
    if n_in < 3:
        raise ValueError("Fewer than three in-transit target-pixel cadences are available.")
    if n_out < 10:
        raise ValueError("Fewer than ten nearby out-of-transit cadences are available.")
    in_image = np.nanmedian(cube[in_mask], axis=0)
    out_image = np.nanmedian(cube[out_mask], axis=0)
    lost_light = out_image - in_image
    background = float(np.nanmedian(lost_light))
    weights = np.clip(np.nan_to_num(lost_light - background, nan=0.0), 0.0, None)
    total = float(np.sum(weights))
    if total <= 0:
        centroid_row = float("nan")
        centroid_column = float("nan")
    else:
        rows, columns = np.indices(weights.shape)
        centroid_row = float(np.sum(rows * weights) / total)
        centroid_column = float(np.sum(columns * weights) / total)
    return {
        "in_image": in_image,
        "out_image": out_image,
        "difference_image": lost_light,
        "centroid_row": centroid_row,
        "centroid_column": centroid_column,
        "in_transit_cadences": n_in,
        "out_of_transit_cadences": n_out,
    }


def target_pixel_from_sky_grid(
    ra_grid: np.ndarray,
    dec_grid: np.ndarray,
    target_ra: float,
    target_dec: float,
) -> tuple[float, float]:
    """Locate target sky coordinates in a per-pixel RA/Dec grid."""

    ra = np.asarray(ra_grid, dtype=float)
    dec = np.asarray(dec_grid, dtype=float)
    distance2 = ((ra - target_ra) * np.cos(np.deg2rad(target_dec))) ** 2 + (
        dec - target_dec
    ) ** 2
    index = int(np.nanargmin(distance2))
    row, column = np.unravel_index(index, distance2.shape)
    return float(row), float(column)


# --------------------------------------------------------------------------
# Pixel vetting v2 (MASTER_PLAN.md section 4.4)
#
# Version 1 asks whether one difference-image centroid, from one sector, in one
# aperture, lands within a pixel of the target. That is a point estimate with
# no uncertainty attached, and it cannot separate "on target" from "the blend
# happens to sit close enough for a single centroid to fall inside tolerance".
# The three upgrades below are the cheap ones the plan puts ahead of PRF
# fitting, in its order of value.
# --------------------------------------------------------------------------


def _aperture_mask(
    shape: tuple[int, int], row: float, column: float, radius: float
) -> np.ndarray:
    rows, columns = np.indices(shape)
    return ((rows - row) ** 2 + (columns - column) ** 2) <= radius**2


def _fold_depth(
    series: np.ndarray, in_mask: np.ndarray, out_mask: np.ndarray
) -> float | None:
    """Fractional transit depth of one summed light curve."""

    in_flux = float(np.nanmedian(series[in_mask]))
    out_flux = float(np.nanmedian(series[out_mask]))
    if not np.isfinite(in_flux) or not np.isfinite(out_flux) or out_flux <= 0:
        return None
    return (out_flux - in_flux) / out_flux


def aperture_depth_curve(
    time: np.ndarray,
    flux_cube: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_hours: float,
    center_row: float,
    center_column: float,
    config=None,
) -> dict[str, object]:
    """Measure transit depth in a growing aperture around the target.

    The discriminator, per section 4.4: depth **rising** with aperture radius
    means the mask is admitting flux from a contaminating neighbour, because
    the eclipsing source sits outside the small aperture and enters the large
    one. Depth **falling** is ordinary dilution of a genuine on-target signal
    by the extra background and neighbour flux the wider mask collects.

    This costs nothing beyond the pixel data the difference image already
    needed, which is why the plan ranks it above PRF fitting.
    """

    from .config import CURRENT_PIXEL_VET

    settings = config or CURRENT_PIXEL_VET
    cube = np.asarray(flux_cube, dtype=float)
    if cube.ndim != 3 or cube.shape[0] != len(time):
        raise ValueError("Flux cube must have shape (cadence, row, column).")
    in_mask, out_mask = transit_cadence_masks(
        time, period_days, transit_time, duration_hours
    )
    if int(np.count_nonzero(in_mask)) < settings.minimum_in_transit_cadences:
        raise ValueError("Too few in-transit cadences for an aperture curve.")
    if int(np.count_nonzero(out_mask)) < settings.minimum_out_of_transit_cadences:
        raise ValueError("Too few out-of-transit cadences for an aperture curve.")

    radii: list[float] = []
    depths: list[float | None] = []
    for radius in settings.aperture_radii_pixels:
        mask = _aperture_mask(cube.shape[1:], center_row, center_column, radius)
        if not mask.any():
            radii.append(float(radius))
            depths.append(None)
            continue
        series = np.nansum(cube[:, mask], axis=1)
        radii.append(float(radius))
        depths.append(_fold_depth(series, in_mask, out_mask))

    measured = [(r, d) for r, d in zip(radii, depths) if d is not None]
    if len(measured) < 2:
        return {
            "radii_pixels": radii,
            "depths": depths,
            "growth_fraction": None,
            "verdict": "not_evaluable",
            "reason": "fewer than two apertures produced a finite depth",
        }

    inner_depth = measured[0][1]
    outer_depth = measured[-1][1]
    if inner_depth is None or inner_depth <= 0:
        # A non-positive innermost depth means the target's own aperture shows
        # no dimming at all, so growth is not a ratio anybody should quote.
        return {
            "radii_pixels": radii,
            "depths": depths,
            "growth_fraction": None,
            "verdict": "no_depth_in_target_aperture",
            "reason": (
                "the smallest aperture shows no dimming, so whatever the wider "
                "mask sees belongs to something else inside it"
            ),
        }

    # Normalized by the larger of the two depths, not by the inner one. When
    # the contaminant is well separated the inner aperture sees almost no
    # dimming, and dividing by that near-zero denominator produced growth
    # ratios in the tens -- a number that is unstable, unbounded, and
    # impossible to set a threshold against. This form is bounded in [-1, 1]:
    # +1 is "all of the signal is outside the target aperture", 0 is "the same
    # depth either way", and negative is ordinary dilution.
    growth = (outer_depth - inner_depth) / max(abs(inner_depth), abs(outer_depth))
    if growth >= settings.aperture_growth_kill_fraction:
        verdict = "contaminating_neighbour"
    elif growth >= settings.aperture_growth_flag_fraction:
        verdict = "flagged_ambiguous_growth"
    else:
        verdict = "consistent_with_on_target"
    return {
        "radii_pixels": radii,
        "depths": depths,
        "inner_depth": inner_depth,
        "outer_depth": outer_depth,
        "growth_fraction": growth,
        "verdict": verdict,
        "policy_version": settings.policy_version,
    }


def bootstrap_centroid(
    time: np.ndarray,
    flux_cube: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_hours: float,
    config=None,
    seed: int = 0,
) -> dict[str, object]:
    """Difference-image centroid with an uncertainty, not just a position.

    The centroid's dominant sensitivity is to which cadences are called in-
    and out-of-transit, so that is what is resampled. "0.6 pixels from the
    target" means something entirely different at +/- 0.05 pixels than at
    +/- 0.8, and version 1 reported both identically.

    Deterministic in ``seed``, so a re-run reproduces the interval exactly.
    """

    from .config import CURRENT_PIXEL_VET

    settings = config or CURRENT_PIXEL_VET
    cube = np.asarray(flux_cube, dtype=float)
    in_mask, out_mask = transit_cadence_masks(
        time, period_days, transit_time, duration_hours
    )
    in_index = np.flatnonzero(in_mask)
    out_index = np.flatnonzero(out_mask)
    if in_index.size < settings.minimum_in_transit_cadences:
        raise ValueError("Too few in-transit cadences to bootstrap a centroid.")
    if out_index.size < settings.minimum_out_of_transit_cadences:
        raise ValueError("Too few out-of-transit cadences to bootstrap a centroid.")

    rows, columns = np.indices(cube.shape[1:])
    generator = np.random.default_rng(seed)
    sampled_rows: list[float] = []
    sampled_columns: list[float] = []
    for _ in range(int(settings.bootstrap_samples)):
        pick_in = generator.choice(in_index, size=in_index.size, replace=True)
        pick_out = generator.choice(out_index, size=out_index.size, replace=True)
        lost = np.nanmedian(cube[pick_out], axis=0) - np.nanmedian(cube[pick_in], axis=0)
        weights = np.clip(
            np.nan_to_num(lost - float(np.nanmedian(lost)), nan=0.0), 0.0, None
        )
        total = float(np.sum(weights))
        if total <= 0:
            continue
        sampled_rows.append(float(np.sum(rows * weights) / total))
        sampled_columns.append(float(np.sum(columns * weights) / total))

    if len(sampled_rows) < 2:
        return {
            "centroid_row": float("nan"),
            "centroid_column": float("nan"),
            "row_uncertainty": None,
            "column_uncertainty": None,
            "samples": len(sampled_rows),
            "verdict": "not_evaluable",
            "reason": "no resample produced positive lost light",
        }
    return {
        "centroid_row": float(np.mean(sampled_rows)),
        "centroid_column": float(np.mean(sampled_columns)),
        "row_uncertainty": float(np.std(sampled_rows, ddof=1)),
        "column_uncertainty": float(np.std(sampled_columns, ddof=1)),
        "samples": len(sampled_rows),
        "verdict": "measured",
        "policy_version": settings.policy_version,
    }


def localization_offset(
    centroid: dict[str, object],
    target_row: float,
    target_column: float,
    *,
    config=None,
) -> dict[str, object]:
    """Offset of the lost light from the target, in units of its own error."""

    from .config import CURRENT_PIXEL_VET

    settings = config or CURRENT_PIXEL_VET
    row = centroid.get("centroid_row")
    column = centroid.get("centroid_column")
    if (
        row is None
        or column is None
        or not np.isfinite(float(row))
        or not np.isfinite(float(column))
    ):
        return {"offset_pixels": None, "verdict": "not_evaluable"}
    d_row = float(row) - float(target_row)
    d_column = float(column) - float(target_column)
    offset = float(np.hypot(d_row, d_column))
    row_error = centroid.get("row_uncertainty")
    column_error = centroid.get("column_uncertainty")
    if not row_error or not column_error:
        return {
            "offset_pixels": offset,
            "offset_uncertainty": None,
            "significance": None,
            "verdict": "measured_without_uncertainty",
        }
    # Project the per-axis errors onto the offset direction: the same distance
    # along a well-constrained axis is more significant than along a poorly
    # constrained one, and a scalar distance hides that entirely.
    if offset > 0:
        error = float(
            np.hypot(
                d_row / offset * float(row_error),
                d_column / offset * float(column_error),
            )
        )
    else:
        error = float(np.hypot(float(row_error), float(column_error)))
    significance = offset / error if error > 0 else float("inf")
    return {
        "offset_pixels": offset,
        "offset_uncertainty": error,
        "significance": significance,
        "verdict": (
            "off_target"
            if significance >= settings.centroid_offset_sigma
            else "consistent_with_on_target"
        ),
        "policy_version": settings.policy_version,
    }


def sector_centroid_consistency(
    per_sector: list[dict[str, object]], *, config=None
) -> dict[str, object]:
    """Do the per-sector centroids agree with each other?

    Section 4.4's second upgrade. A blend can put every individual sector's
    centroid inside the one-pixel tolerance while the centroids disagree with
    each *other* far beyond their own errors: a different camera, roll angle
    and scattered-light environment move a blend and do not move a genuine
    on-target signal. Reported as the reduced chi-square about the weighted
    mean, per axis.
    """

    from .config import CURRENT_PIXEL_VET

    settings = config or CURRENT_PIXEL_VET
    usable = [
        entry
        for entry in per_sector
        if entry.get("row_uncertainty")
        and entry.get("column_uncertainty")
        and np.isfinite(float(entry.get("centroid_row", np.nan)))
        and np.isfinite(float(entry.get("centroid_column", np.nan)))
    ]
    if len(usable) < settings.minimum_sectors_for_consistency:
        return {
            "sectors": len(usable),
            "verdict": "not_evaluable",
            "reason": (
                "consistency needs at least "
                f"{settings.minimum_sectors_for_consistency} sectors with "
                "measured uncertainties"
            ),
        }

    def reduced_chi2(values: list[float], errors: list[float]) -> tuple[float, float]:
        weights = np.array([1.0 / (error**2) for error in errors], dtype=float)
        mean = float(np.sum(np.array(values) * weights) / np.sum(weights))
        chi2 = float(np.sum(weights * (np.array(values) - mean) ** 2))
        return mean, chi2 / max(1, len(values) - 1)

    row_mean, row_chi2 = reduced_chi2(
        [float(entry["centroid_row"]) for entry in usable],
        [float(entry["row_uncertainty"]) for entry in usable],
    )
    column_mean, column_chi2 = reduced_chi2(
        [float(entry["centroid_column"]) for entry in usable],
        [float(entry["column_uncertainty"]) for entry in usable],
    )
    worst = max(row_chi2, column_chi2)
    return {
        "sectors": len(usable),
        "weighted_mean_row": row_mean,
        "weighted_mean_column": column_mean,
        "row_reduced_chi2": row_chi2,
        "column_reduced_chi2": column_chi2,
        "worst_reduced_chi2": worst,
        "verdict": (
            "centroid_wanders_between_sectors"
            if worst > settings.sector_consistency_max_chi2
            else "consistent_across_sectors"
        ),
        "policy_version": settings.policy_version,
    }


def neighbour_transit_extraction(
    time: np.ndarray,
    flux_cube: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_hours: float,
    candidates: list[dict[str, object]],
    radius_pixels: float = 1.0,
    config=None,
) -> dict[str, object]:
    """Extract the fixed ephemeris at each candidate host and rank them.

    Section 1.3's decisive test and section 4.4's highest-value upgrade. The
    question is not "is the centroid near the target" but "which of the
    objects in this pixel is actually dimming". Each ranked counterpart from
    the identity graph gets its own small aperture, and the deepest coherent
    signal names the host.

    A neighbour winning here is a *reassignment*, not a warning -- which is
    why the identity graph keeps every plausible counterpart rather than
    resolving to one.
    """

    from .config import CURRENT_PIXEL_VET

    settings = config or CURRENT_PIXEL_VET
    cube = np.asarray(flux_cube, dtype=float)
    in_mask, out_mask = transit_cadence_masks(
        time, period_days, transit_time, duration_hours
    )
    if int(np.count_nonzero(in_mask)) < settings.minimum_in_transit_cadences:
        raise ValueError("Too few in-transit cadences for neighbour extraction.")

    measured: list[dict[str, object]] = []
    for candidate in candidates:
        row = candidate.get("row")
        column = candidate.get("column")
        if row is None or column is None:
            continue
        mask = _aperture_mask(cube.shape[1:], float(row), float(column), radius_pixels)
        if not mask.any():
            continue
        series = np.nansum(cube[:, mask], axis=1)
        depth = _fold_depth(series, in_mask, out_mask)
        scatter = float(np.nanstd(series[out_mask]))
        baseline = float(np.nanmedian(series[out_mask]))
        measured.append(
            {
                "identifier": candidate.get("identifier"),
                "row": float(row),
                "column": float(column),
                "depth": depth,
                "depth_snr": (
                    (depth * baseline) / scatter
                    if depth is not None and scatter > 0
                    else None
                ),
                "is_target": bool(candidate.get("is_target", False)),
            }
        )

    ranked = sorted(
        measured,
        key=lambda item: (item["depth"] is None, -(item["depth"] or 0.0)),
    )
    target = next((item for item in ranked if item["is_target"]), None)
    best = ranked[0] if ranked else None
    reassigned = bool(
        best is not None
        and target is not None
        and not best["is_target"]
        and best["depth"] is not None
        and target["depth"] is not None
        and best["depth"] > target["depth"]
    )
    return {
        "candidates": ranked,
        "best_host": best["identifier"] if best else None,
        "target_depth": target["depth"] if target else None,
        "verdict": (
            "signal_belongs_to_neighbour" if reassigned else "target_is_best_host"
        ),
        "policy_version": settings.policy_version,
    }

