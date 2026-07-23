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
