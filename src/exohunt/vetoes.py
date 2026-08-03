"""Cheap physical vetoes (T3): pure functions over a signal and its star.

Every function returns a dictionary with a ``verdict`` and the numbers that
produced it; nothing here mutates state or claims more than its evidence.
Verdicts kill *signals*, never stars, and every kill is reversible by
construction because the signal and the gate values are recorded in the
evidence ledger.

Two of these tests exist because named failures demanded them:

* :func:`full_phase_secondary_scan` scans **all** phases because eccentric
  binaries put secondaries far from phase 0.5, where the historical screen
  looked -- and because TIC 181014443's secondary was 2.3 sigma in one
  sector but 5.9 sigma stacked, the kill belongs on the deepest fold
  available.
* :func:`odd_even_difference` replaces medians-of-per-event-medians (which
  returned None exactly when events were scarce) with a two-depth folded
  comparison that works at 3+1 events.
"""

from __future__ import annotations

import math

import numpy as np

from .config import CURRENT_CONFIG, VetoConfig
from .search import expected_duration_hours

SOLAR_RADII_PER_JUPITER_RADIUS = 0.10045  # IAU nominal values
MEDIAN_STANDARD_ERROR_FACTOR = math.sqrt(math.pi / 2.0)
DURATION_DENSITY_REJECTION_REASON = (
    "the fitted transit duration is physically inconsistent with the catalog "
    "stellar density"
)
DIP_WINDOW_REJECTION_REASON = (
    "too few transit events survive once events inside registered "
    "absolute-time systematic windows are discounted"
)
DEPTH_EB_LANE_REASON = (
    "the implied companion radius exceeds the configured planet-lane ceiling"
)
ODD_EVEN_REJECTION_REASON = (
    "odd and even transit depths differ by more than 3 sigma"
)
SECONDARY_REJECTION_REASON = (
    "a secondary eclipse is detected above 3 sigma"
)
EVENT_SUPPORT_REJECTION_REASON = (
    "fewer than the required transit events have in-transit cadences and "
    "two-sided local baselines"
)
DURATION_DENSITY_REVIEW_FLAG = (
    "the fitted transit duration is strained relative to the catalog stellar "
    "density"
)


def _point_scatter(values: np.ndarray) -> float:
    center = np.nanmedian(values)
    return float(1.4826 * np.nanmedian(np.abs(values - center)))


def duration_density_consistency(
    *,
    period_days: float,
    duration_hours: float,
    density_solar: float | None,
    config: VetoConfig | None = None,
) -> dict[str, object]:
    """Compare the fitted duration with the b=0 expectation from density.

    A duration far above the physical ceiling means a giant host, a blend,
    or a junk fit; far below means a grazing geometry or noise. Cheap,
    single-sector, and requires no pixels -- the strongest test the pipeline
    was not running.
    """

    cfg = config or CURRENT_CONFIG.vetoes
    if density_solar is None or density_solar <= 0:
        return {
            "verdict": "not_evaluable",
            "reason": "stellar density unavailable",
            "ratio": None,
            "expected_hours": None,
        }
    expected = expected_duration_hours(period_days, density_solar)
    ratio = duration_hours / expected
    flag_low, flag_high = cfg.duration_density_flag_span
    kill_low, kill_high = cfg.duration_density_kill_span
    if ratio < kill_low or ratio > kill_high:
        verdict = "kill"
    elif ratio < flag_low or ratio > flag_high:
        verdict = "flag"
    else:
        verdict = "pass"
    return {
        "verdict": verdict,
        "ratio": round(ratio, 3),
        "expected_hours": round(expected, 3),
        "duration_hours": float(duration_hours),
        "density_solar": float(density_solar),
    }


def depth_physicality(
    *,
    depth_ppm: float,
    stellar_radius_solar: float | None,
    config: VetoConfig | None = None,
) -> dict[str, object]:
    """Convert depth to an implied companion radius; route EBs to their lane."""

    cfg = config or CURRENT_CONFIG.vetoes
    if not stellar_radius_solar or stellar_radius_solar <= 0:
        return {
            "verdict": "not_evaluable",
            "reason": "stellar radius unavailable",
            "implied_radius_rjup": None,
        }
    if depth_ppm <= 0:
        return {
            "verdict": "kill",
            "reason": "non-positive depth",
            "implied_radius_rjup": None,
        }
    radius_ratio = float(np.sqrt(depth_ppm / 1e6))
    implied_rjup = (
        radius_ratio * stellar_radius_solar / SOLAR_RADII_PER_JUPITER_RADIUS
    )
    verdict = (
        "eb_lane" if implied_rjup > cfg.max_companion_radius_rjup else "pass"
    )
    return {
        "verdict": verdict,
        "implied_radius_rjup": round(implied_rjup, 3),
        "radius_ratio": round(radius_ratio, 5),
        "max_companion_radius_rjup": cfg.max_companion_radius_rjup,
    }


def _in_transit_depths(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_days: float,
) -> tuple[np.ndarray, np.ndarray, float]:
    t = np.asarray(time, dtype=float)
    y = np.asarray(flux, dtype=float)
    finite = np.isfinite(t) & np.isfinite(y)
    t = t[finite]
    y = y[finite]
    phase = ((t - transit_time + period_days / 2) % period_days) - period_days / 2
    in_transit = np.abs(phase) <= duration_days / 2
    out = np.abs(phase) >= duration_days
    baseline = float(np.nanmedian(y[out]))
    events = np.rint((t - transit_time) / period_days).astype(int)
    return (
        events[in_transit],
        baseline - y[in_transit],
        baseline,
    )


def odd_even_difference(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_hours: float,
    config: VetoConfig | None = None,
) -> dict[str, object]:
    """Two-depth folded odd/even comparison.

    All odd-event in-transit points against all even-event points, one depth
    each: works whenever both parity classes have a handful of cadences,
    instead of requiring two fully-sampled events per class.
    """

    cfg = config or CURRENT_CONFIG.vetoes
    events, depths, _ = _in_transit_depths(
        time,
        flux,
        period_days=period_days,
        transit_time=transit_time,
        duration_days=duration_hours / 24.0,
    )
    if depths.size and not np.all(np.isfinite(depths)):
        return {
            "verdict": "not_evaluable",
            "reason": "no scatter",
            "sigma": None,
        }
    odd = depths[events % 2 != 0]
    even = depths[events % 2 == 0]
    if odd.size < cfg.min_cadences_per_event or even.size < cfg.min_cadences_per_event:
        return {
            "verdict": "not_evaluable",
            "reason": "too few in-transit cadences in one parity class",
            "sigma": None,
        }
    depth_odd = float(np.nanmedian(odd))
    depth_even = float(np.nanmedian(even))
    error = float(
        np.hypot(
            MEDIAN_STANDARD_ERROR_FACTOR
            * _point_scatter(odd)
            / np.sqrt(odd.size),
            MEDIAN_STANDARD_ERROR_FACTOR
            * _point_scatter(even)
            / np.sqrt(even.size),
        )
    )
    if not np.isfinite(error) or error <= 0:
        return {"verdict": "not_evaluable", "reason": "no scatter", "sigma": None}
    sigma = abs(depth_odd - depth_even) / error
    return {
        "verdict": "kill" if sigma > cfg.odd_even_kill_sigma else "pass",
        "sigma": round(float(sigma), 3),
        "depth_odd_ppm": round(depth_odd * 1e6, 1),
        "depth_even_ppm": round(depth_even * 1e6, 1),
        "odd_cadences": int(odd.size),
        "even_cadences": int(even.size),
    }


def full_phase_secondary_scan(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_hours: float,
    config: VetoConfig | None = None,
) -> dict[str, object]:
    """Strongest dip anywhere outside the primary, not only at phase 0.5."""

    cfg = config or CURRENT_CONFIG.vetoes
    t = np.asarray(time, dtype=float)
    y = np.asarray(flux, dtype=float)
    finite = np.isfinite(t) & np.isfinite(y)
    t = t[finite]
    y = y[finite]
    duration_days = duration_hours / 24.0
    phase = ((t - transit_time + period_days / 2) % period_days) - period_days / 2
    exclusion = cfg.secondary_exclusion_durations * duration_days
    searchable = np.abs(phase) > exclusion
    if np.count_nonzero(searchable) < 40:
        return {"verdict": "not_evaluable", "reason": "too few cadences", "snr": None}
    baseline = float(np.nanmedian(y[searchable]))
    scatter = _point_scatter(y[searchable])
    if not np.isfinite(scatter) or scatter <= 0:
        return {"verdict": "not_evaluable", "reason": "no scatter", "snr": None}
    step = duration_days / 2.0
    centers = np.arange(
        -period_days / 2 + duration_days / 2,
        period_days / 2 - duration_days / 2 + step / 2,
        step,
    )
    best: dict[str, object] | None = None
    tested_windows = 0
    for center in centers:
        if abs(center) <= exclusion + duration_days / 2:
            continue
        window = np.abs(phase - center) <= duration_days / 2
        n = int(np.count_nonzero(window))
        if n < 3:
            continue
        tested_windows += 1
        depth = baseline - float(np.nanmedian(y[window]))
        snr = depth / (
            MEDIAN_STANDARD_ERROR_FACTOR * scatter / np.sqrt(n)
        )
        if best is None or snr > float(best["snr"]):
            best = {
                "phase_days": round(float(center), 5),
                "phase_fraction": round(float(center / period_days), 4),
                "depth_ppm": round(depth * 1e6, 1),
                "snr": round(float(snr), 3),
                "cadences": n,
            }
    if best is None:
        return {"verdict": "not_evaluable", "reason": "no scannable window", "snr": None}
    local_tail = 0.5 * math.erfc(float(best["snr"]) / math.sqrt(2.0))
    family_false_alarm_probability = min(1.0, tested_windows * local_tail)
    configured_tail = 0.5 * math.erfc(
        cfg.secondary_kill_sigma / math.sqrt(2.0)
    )
    verdict = (
        "kill"
        if family_false_alarm_probability < configured_tail
        else "pass"
    )
    return {
        "verdict": verdict,
        **best,
        "tested_phase_windows": tested_windows,
        "family_wise_false_alarm_probability": round(
            family_false_alarm_probability,
            8,
        ),
        "global_sigma_threshold": cfg.secondary_kill_sigma,
    }


def per_event_support(
    time: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_hours: float,
    config: VetoConfig | None = None,
) -> dict[str, object]:
    """Count transit events with real sampling and a two-sided local baseline.

    Only supported events count toward the minimum-transit rule; an
    "event" that is one cadence beside a gap is exactly how edge artifacts
    became detections.
    """

    cfg = config or CURRENT_CONFIG.vetoes
    t = np.asarray(time, dtype=float)
    t = np.sort(t[np.isfinite(t)])
    if t.size == 0:
        return {
            "supported_events": 0,
            "predicted_events": 0,
            "events": [],
        }
    duration_days = duration_hours / 24.0
    first = int(np.ceil((t[0] - transit_time) / period_days))
    last = int(np.floor((t[-1] - transit_time) / period_days))
    events: list[dict[str, object]] = []
    supported = 0
    for number in range(first, last + 1):
        center = transit_time + number * period_days
        in_window = int(
            np.count_nonzero(np.abs(t - center) <= duration_days / 2)
        )
        before = int(
            np.count_nonzero(
                (t >= center - 3 * duration_days) & (t <= center - duration_days)
            )
        )
        after = int(
            np.count_nonzero(
                (t >= center + duration_days) & (t <= center + 3 * duration_days)
            )
        )
        ok = (
            in_window >= cfg.min_cadences_per_event
            and before >= cfg.min_cadences_per_event
            and after >= cfg.min_cadences_per_event
        )
        supported += int(ok)
        events.append(
            {
                "event": number,
                "center": round(float(center), 5),
                "in_transit_cadences": in_window,
                "baseline_before": before,
                "baseline_after": after,
                "supported": ok,
            }
        )
    return {
        "supported_events": supported,
        "predicted_events": len(events),
        "events": events,
    }


def dip_window_veto(
    event_centers: list[float],
    windows: list[tuple[float, float]],
) -> dict[str, object]:
    """Discount transit events whose centres fall in registered systematic
    windows (see :func:`exohunt.population.build_dip_registry`)."""

    flagged = [
        center
        for center in event_centers
        if any(start <= center <= stop for start, stop in windows)
    ]
    clean = len(event_centers) - len(flagged)
    return {
        "events_total": len(event_centers),
        "events_in_systematic_windows": len(flagged),
        "events_clean": clean,
        "flagged_centers": [round(float(value), 5) for value in flagged],
    }


def evaluate_t3_vetoes(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_hours: float,
    depth_ppm: float,
    density_solar: float | None,
    stellar_radius_solar: float | None,
    minimum_supported_events: int,
    config: VetoConfig | None = None,
    dip_windows: list[tuple[float, float]] | None = None,
    dip_registry_scope: str | None = None,
) -> dict[str, object]:
    """Evaluate the complete single-target T3 gate and retain every check.

    Missing stellar parameters make only the corresponding physical checks
    non-evaluable. Folded light-curve checks still run, and an event counts
    toward the periodic-signal minimum only when it has both in-transit
    sampling and a two-sided local baseline.
    """

    if minimum_supported_events <= 0:
        raise ValueError("minimum_supported_events must be positive")
    cfg = config or CURRENT_CONFIG.vetoes
    duration = duration_density_consistency(
        period_days=period_days,
        duration_hours=duration_hours,
        density_solar=density_solar,
        config=cfg,
    )
    depth = depth_physicality(
        depth_ppm=depth_ppm,
        stellar_radius_solar=stellar_radius_solar,
        config=cfg,
    )
    odd_even = odd_even_difference(
        time,
        flux,
        period_days=period_days,
        transit_time=transit_time,
        duration_hours=duration_hours,
        config=cfg,
    )
    secondary = full_phase_secondary_scan(
        time,
        flux,
        period_days=period_days,
        transit_time=transit_time,
        duration_hours=duration_hours,
        config=cfg,
    )
    support = per_event_support(
        time,
        period_days=period_days,
        transit_time=transit_time,
        duration_hours=duration_hours,
        config=cfg,
    )

    # T4 feeds back into T3 here (MASTER_PLAN 3.6). An event whose centre sits
    # in a registered absolute-time window is *discounted*, not merely noted:
    # many unrelated stars dimmed together at that timestamp, so it is the
    # observatory's event and cannot be one of this star's transits. Windows
    # arrive as a published snapshot -- a first campaign over a fresh cohort
    # has none, and the veto is then inert rather than silently permissive.
    dip = _dip_window_evidence(
        support,
        windows=dip_windows,
        scope=dip_registry_scope,
        enabled=cfg.dip_registry_veto,
    )
    effective_supported = int(dip["supported_events_after_veto"])

    rejection_reasons: list[str] = []
    if duration["verdict"] == "kill":
        rejection_reasons.append(DURATION_DENSITY_REJECTION_REASON)
    if depth["verdict"] == "eb_lane":
        rejection_reasons.append(DEPTH_EB_LANE_REASON)
    if odd_even["verdict"] == "kill":
        rejection_reasons.append(ODD_EVEN_REJECTION_REASON)
    if secondary["verdict"] == "kill":
        rejection_reasons.append(SECONDARY_REJECTION_REASON)
    if int(support["supported_events"]) < minimum_supported_events:
        rejection_reasons.append(EVENT_SUPPORT_REJECTION_REASON)
    elif effective_supported < minimum_supported_events:
        # Support was sufficient until the registry discounted events, so the
        # reason names the registry rather than hiding behind plain
        # insufficient sampling.
        rejection_reasons.append(DIP_WINDOW_REJECTION_REASON)

    review_flags = (
        [DURATION_DENSITY_REVIEW_FLAG]
        if duration["verdict"] == "flag"
        else []
    )
    return {
        "schema_version": 1,
        "passes": not rejection_reasons,
        "routes_to_eb_lane": depth["verdict"] == "eb_lane",
        "minimum_supported_events": minimum_supported_events,
        "rejection_reasons": rejection_reasons,
        "review_flags": review_flags,
        "checks": {
            "duration_density": duration,
            "depth_physicality": depth,
            "odd_even": odd_even,
            "full_phase_secondary": secondary,
            "event_support": support,
            "dip_window": dip,
        },
    }


def _dip_window_evidence(
    support: dict[str, object],
    *,
    windows: list[tuple[float, float]] | None,
    scope: str | None,
    enabled: bool,
) -> dict[str, object]:
    """Discount supported events that land in registered systematic windows.

    Always returns a block, so a report records *why* the screen did or did
    not apply. "No registry was available" and "a registry was applied and
    found nothing" are different facts and must not read the same.
    """

    events = support.get("events") or []
    supported_centers = [
        float(event["center"])
        for event in events
        if isinstance(event, dict) and event.get("supported")
    ]
    baseline = int(support.get("supported_events") or 0)
    if not enabled:
        state = "disabled_by_config"
    elif not windows:
        state = "no_registry_available"
    else:
        state = "applied"
    if state != "applied":
        return {
            "state": state,
            "cohort": scope,
            "registered_windows": 0,
            "events_total": len(supported_centers),
            "events_in_systematic_windows": 0,
            "events_clean": len(supported_centers),
            "flagged_centers": [],
            "supported_events_before_veto": baseline,
            "supported_events_after_veto": baseline,
            "note": (
                "No absolute-time window list was applied. This is not "
                "evidence that the events are astrophysical."
            ),
        }
    veto = dip_window_veto(supported_centers, list(windows))
    return {
        "state": state,
        "cohort": scope,
        "registered_windows": len(windows),
        **veto,
        "supported_events_before_veto": baseline,
        "supported_events_after_veto": int(veto["events_clean"]),
        "note": (
            "Events centred inside a registered window are discounted "
            "because many unrelated stars dimmed together at that absolute "
            "time. Surviving the screen is not evidence of a planet."
        ),
    }
