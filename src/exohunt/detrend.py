"""Biweight detrending with support-weighted edges (T1 preparation).

Successor to the Savitzky-Golay path in :mod:`exohunt.detrending`, per
MASTER_PLAN.md section 2.2. Three ideas:

* **Time-windowed biweight** (wotan) replaces Savitzky-Golay as the default
  trend estimator: robust to outliers, tracks stellar variability, and erodes
  transit depth only mildly when the window is at least ~3x the transit
  duration.
* **Two prepared fluxes per star**: a short-window flux for the periodic
  search and a long-window flux for the monotransit detector, whose events
  (up to ~a day) a short window would flatten away. One extra pass over the
  same array.
* **Support-weighted edges** instead of the hard half-window guard that cost
  33% of cadences. Each cadence records the populated fraction ``f`` of its
  trend window inside its own segment: ``f = 1`` deep inside, falling toward
  ``0.5`` at a clean edge. Cadences below a support floor are dropped;
  cadences between floor and 1 are kept with their uncertainty inflated by
  ``(1/f)**alpha``, so a search *sees* edge cadences but cannot be driven by
  them. The acceptance criteria for replacing the hard guard are measured,
  not asserted -- see MASTER_PLAN.md section 2.3.

Segmentation at real interruptions (gaps of at least
``DetrendConfig.segment_gap_days``) is inherited unchanged from the measured
Sector 100 work: the 3.8 h and 5.2 h interruptions must create boundaries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from .config import CURRENT_CONFIG, DetrendConfig

METHOD_TAG = "biweight_support_weighted_v1"


@dataclass(frozen=True, slots=True)
class PreparedFlux:
    """One detrended light curve ready for a search stage."""

    time: np.ndarray
    flux: np.ndarray
    trend: np.ndarray
    # Multiply per-point uncertainties by this before any search statistic:
    # 1.0 deep inside a segment, growing toward the support floor at edges.
    uncertainty_scale: np.ndarray
    support_fraction: np.ndarray
    segment_count: int
    metadata: dict[str, Any]


def _clean(time: np.ndarray, flux: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    t = np.asarray(time, dtype=float)
    y = np.asarray(flux, dtype=float)
    keep = np.isfinite(t) & np.isfinite(y)
    t, y = t[keep], y[keep]
    if t.size < 100:
        raise ValueError("At least 100 finite measurements are required.")
    order = np.argsort(t)
    t, y = t[order], y[order]
    median = float(np.nanmedian(y))
    if not np.isfinite(median) or median == 0:
        raise ValueError("Flux cannot be normalized: invalid median.")
    return t, y / median


def segment_boundaries(
    time: np.ndarray, gap_days: float
) -> list[tuple[int, int]]:
    """Half-open index ranges split at gaps of at least ``gap_days``."""

    if time.size == 0:
        return []
    split_after = np.flatnonzero(np.diff(time) > gap_days)
    starts = [0, *(int(index) + 1 for index in split_after)]
    stops = [*starts[1:], time.size]
    return list(zip(starts, stops))


def support_fractions(
    time: np.ndarray,
    *,
    window_days: float,
    gap_days: float,
) -> tuple[np.ndarray, int]:
    """Fraction of each cadence's trend window populated within its segment.

    The denominator is the sample count a fully-populated window would hold at
    the segment's median cadence, so small internal gaps also lower support
    (they starve the trend estimate exactly like an edge does).
    """

    if window_days <= 0 or gap_days <= 0:
        raise ValueError("Window and gap thresholds must be positive.")
    t = np.asarray(time, dtype=float)
    fractions = np.zeros(t.size, dtype=float)
    segments = segment_boundaries(t, gap_days)
    half = window_days / 2.0
    for start, stop in segments:
        segment = t[start:stop]
        if segment.size == 1:
            fractions[start] = 0.0
            continue
        cadence = float(np.median(np.diff(segment)))
        if not np.isfinite(cadence) or cadence <= 0:
            continue
        expected = window_days / cadence
        left = np.searchsorted(segment, segment - half, side="left")
        right = np.searchsorted(segment, segment + half, side="right")
        fractions[start:stop] = np.minimum(1.0, (right - left) / expected)
    return fractions, len(segments)


def transit_mask(
    time: np.ndarray,
    events: list[dict[str, float]],
    *,
    width_factor: float = 1.5,
) -> np.ndarray:
    """In-transit cadence mask for known or newly-found ephemerides.

    Each event needs ``period_days``, ``transit_time``, and
    ``duration_hours``. The width factor widens the window for ephemeris and
    duration error, matching :func:`exohunt.detection.mask_periodic_events`.
    """

    t = np.asarray(time, dtype=float)
    mask = np.zeros(t.size, dtype=bool)
    for event in events:
        period = float(event["period_days"])
        epoch = float(event["transit_time"])
        duration_days = float(event["duration_hours"]) / 24.0
        if period <= 0 or duration_days <= 0:
            continue
        phase = ((t - epoch + period / 2) % period) - period / 2
        mask |= np.abs(phase) <= duration_days * width_factor / 2
    return mask


def prepare_flux(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    window_days: float,
    config: DetrendConfig | None = None,
    mask_events: list[dict[str, float]] | None = None,
) -> PreparedFlux:
    """Detrend one normalized light curve with support-weighted edges.

    ``mask_events`` excludes known in-transit cadences from the *trend
    estimate* (they are still detrended and returned). This is the second
    pass of the two-pass discipline: under strong stellar variability a
    single blind pass erodes transit depth, because the window's scatter is
    dominated by the variability slope and in-transit points stop looking
    like outliers to the robust estimator. After a first search finds a
    signal, re-preparing with the signal masked restores its depth; a depth
    that moves materially between passes is flagged ``detrend_sensitive``.
    """

    from wotan import flatten as wotan_flatten

    cfg = config or CURRENT_CONFIG.detrend
    t, y = _clean(time, flux)
    mask = (
        transit_mask(t, mask_events)
        if mask_events
        else np.zeros(t.size, dtype=bool)
    )
    detrended, trend = wotan_flatten(
        t,
        y,
        method="biweight",
        window_length=window_days,
        break_tolerance=cfg.segment_gap_days,
        edge_cutoff=0.0,
        mask=mask,
        return_trend=True,
    )
    fractions, segment_count = support_fractions(
        t, window_days=window_days, gap_days=cfg.segment_gap_days
    )
    keep = (
        np.isfinite(detrended)
        & np.isfinite(trend)
        & (fractions >= cfg.edge_support_floor)
    )
    kept = int(np.count_nonzero(keep))
    if kept < 100:
        raise RuntimeError(
            "Fewer than 100 supported cadences remain after detrending."
        )
    kept_fractions = fractions[keep]
    scale = (1.0 / np.maximum(kept_fractions, cfg.edge_support_floor)) ** (
        cfg.edge_weight_alpha
    )
    metadata = {
        "method": METHOD_TAG,
        "window_days": float(window_days),
        "config": asdict(cfg),
        "masked_event_count": len(mask_events or []),
        "masked_cadences": int(np.count_nonzero(mask)),
        "segment_count": segment_count,
        "input_cadences": int(t.size),
        "retained_cadences": kept,
        "retention_fraction": round(kept / t.size, 4),
        "dropped_below_support_floor": int(
            np.count_nonzero(
                np.isfinite(detrended) & (fractions < cfg.edge_support_floor)
            )
        ),
        "median_uncertainty_scale": round(float(np.median(scale)), 4),
    }
    return PreparedFlux(
        time=t[keep],
        flux=np.asarray(detrended, dtype=float)[keep],
        trend=np.asarray(trend, dtype=float)[keep],
        uncertainty_scale=scale,
        support_fraction=kept_fractions,
        segment_count=segment_count,
        metadata=metadata,
    )


def prepare_fluxes(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    config: DetrendConfig | None = None,
) -> dict[str, PreparedFlux]:
    """Return the short-window search flux and the long-window event flux."""

    cfg = config or CURRENT_CONFIG.detrend
    return {
        "short": prepare_flux(
            time, flux, window_days=cfg.short_window_days, config=cfg
        ),
        "long": prepare_flux(
            time, flux, window_days=cfg.long_window_days, config=cfg
        ),
    }
