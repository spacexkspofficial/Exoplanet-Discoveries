"""Population-level screens (T4): the absolute-time dip registry.

The shared-ephemeris screen (:mod:`exohunt.commonmode`) catches artifacts
after they alias into periods. This registry catches them earlier, in
absolute time: bin every prepared light curve in a cohort onto one time
axis, and any bin where an improbable fraction of unrelated stars dip
together is a systematic window -- scattered light, a momentum dump, an
edge artifact. Individual transit events falling in registered windows are
discounted before any period is fitted (see
:func:`exohunt.vetoes.dip_window_veto`).

A free by-product is the empirical per-sector map of shared instrumental
events the research review wanted from external documentation: here it is
measured from the cohort itself.
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from .config import CURRENT_CONFIG, PopulationConfig


def _star_bin_dips(
    time: np.ndarray,
    flux: np.ndarray,
    edges: np.ndarray,
    sigma_threshold: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-bin (has_data, dips) flags for one star's normalized flux."""

    t = np.asarray(time, dtype=float)
    y = np.asarray(flux, dtype=float)
    finite = np.isfinite(t) & np.isfinite(y)
    t, y = t[finite], y[finite]
    bins = len(edges) - 1
    has_data = np.zeros(bins, dtype=bool)
    dips = np.zeros(bins, dtype=bool)
    if t.size < 10:
        return has_data, dips
    center = float(np.nanmedian(y))
    scatter = float(1.4826 * np.nanmedian(np.abs(y - center)))
    if not np.isfinite(scatter) or scatter <= 0:
        return has_data, dips
    indices = np.searchsorted(edges, t, side="right") - 1
    valid = (indices >= 0) & (indices < bins)
    for index in np.unique(indices[valid]):
        values = y[indices == index]
        if values.size < 2:
            continue
        has_data[index] = True
        depth = center - float(np.nanmedian(values))
        significance = depth / (scatter / np.sqrt(values.size))
        dips[index] = significance > sigma_threshold
    return has_data, dips


def build_dip_registry(
    curves: Iterable[tuple[int, np.ndarray, np.ndarray]],
    *,
    config: PopulationConfig | None = None,
) -> dict[str, Any]:
    """Build the shared-dip window registry for one cohort.

    ``curves`` yields ``(tic_id, time, normalized_flux)`` for every prepared
    star observed together (one sector-camera cohort; pooling stars never
    observed together would dilute real windows and manufacture fake ones,
    the same rule the shared-ephemeris screen follows).
    """

    cfg = config or CURRENT_CONFIG.population
    bin_days = cfg.dip_bin_minutes / (24.0 * 60.0)
    star_flags: list[tuple[int, np.ndarray, np.ndarray]] = []
    t_min = np.inf
    t_max = -np.inf
    materialized: list[tuple[int, np.ndarray, np.ndarray]] = []
    for tic_id, time, flux in curves:
        t = np.asarray(time, dtype=float)
        finite = t[np.isfinite(t)]
        if finite.size:
            t_min = min(t_min, float(finite.min()))
            t_max = max(t_max, float(finite.max()))
        materialized.append((tic_id, t, np.asarray(flux, dtype=float)))
    if not np.isfinite(t_min) or t_max <= t_min:
        return {
            "schema_version": 1,
            "windows": [],
            "stars": 0,
            "bins": 0,
            "settings": _settings(cfg),
        }
    edges = np.arange(t_min, t_max + bin_days, bin_days)
    for tic_id, t, y in materialized:
        has_data, dips = _star_bin_dips(t, y, edges, cfg.dip_star_sigma)
        star_flags.append((tic_id, has_data, dips))

    bins = len(edges) - 1
    stars_in_bin = np.zeros(bins, dtype=int)
    dips_in_bin = np.zeros(bins, dtype=int)
    for _, has_data, dips in star_flags:
        stars_in_bin += has_data
        dips_in_bin += dips
    with np.errstate(divide="ignore", invalid="ignore"):
        fraction = np.where(
            stars_in_bin > 0, dips_in_bin / np.maximum(stars_in_bin, 1), 0.0
        )
    flagged = (
        (stars_in_bin >= cfg.dip_min_stars)
        & (fraction >= cfg.dip_min_fraction)
    )

    windows: list[dict[str, Any]] = []
    index = 0
    while index < bins:
        if not flagged[index]:
            index += 1
            continue
        start = index
        while index < bins and flagged[index]:
            index += 1
        stop = index - 1
        windows.append(
            {
                "start": round(float(edges[start]), 5),
                "stop": round(float(edges[stop + 1]), 5),
                "peak_fraction": round(
                    float(np.max(fraction[start : stop + 1])), 4
                ),
                "stars_evaluated": int(
                    np.max(stars_in_bin[start : stop + 1])
                ),
            }
        )
    return {
        "schema_version": 1,
        "windows": windows,
        "window_spans": [
            (window["start"], window["stop"]) for window in windows
        ],
        "stars": len(star_flags),
        "bins": bins,
        "time_range": [round(float(t_min), 5), round(float(t_max), 5)],
        "settings": _settings(cfg),
        "warning": (
            "A registered window says many unrelated stars dimmed together "
            "at this absolute time; individual events there are discounted. "
            "It does not certify events outside the windows as astrophysical."
        ),
    }


def _settings(cfg: PopulationConfig) -> dict[str, float | int]:
    return {
        "dip_bin_minutes": cfg.dip_bin_minutes,
        "dip_star_sigma": cfg.dip_star_sigma,
        "dip_min_fraction": cfg.dip_min_fraction,
        "dip_min_stars": cfg.dip_min_stars,
    }
