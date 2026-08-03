"""Search-stage policy: physical grids, overscan, and alias adjudication (T2).

Three measured problems this module removes:

* **Duration rails.** 4,401 fits pinned to the edges of the fixed
  0.25-6.0 h duration grid, with 6.0 h the modal "duration". The grid now
  derives from stellar density and endpoint fits cannot pass triage. The
  fixed 6-hour pile-up is gone, but the locked shipping A/B found that
  star-specific physical endpoints remain a dominant fit class.
* **Period rails.** Survivors piled up at the search ceiling (a truncated
  13.70 d spacecraft peak). The grid now extends past the *reporting*
  ceiling; a best fit in the overscan region is a diagnostic, never a
  survivor, so the reporting boundary is not a grid boundary.
* **Alias errors.** TOI-700 c was recovered at exactly half its true period
  from one sector. Every reported ephemeris is adjudicated against its alias
  ladder before it is allowed to mean anything.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import CURRENT_CONFIG, SearchConfig
from .detection import evaluate_ephemeris

SOLAR_DENSITY_EXPECTED_HOURS_AT_ONE_YEAR = 13.0
DAYS_PER_YEAR = 365.25


def stellar_density_solar(
    stellar_radius_solar: float | None,
    stellar_mass_solar: float | None,
) -> float | None:
    """Mean stellar density in solar units, when both parameters exist."""

    if not stellar_radius_solar or not stellar_mass_solar:
        return None
    if stellar_radius_solar <= 0 or stellar_mass_solar <= 0:
        return None
    return float(stellar_mass_solar / stellar_radius_solar**3)


def expected_duration_hours(
    period_days: float, density_solar: float
) -> float:
    """Central-transit duration for a circular orbit (Seager & Mallen-Ornelas).

    T0 ~= 13 h x (P / 1 yr)^(1/3) x (rho / rho_sun)^(-1/3).
    """

    if period_days <= 0 or density_solar <= 0:
        raise ValueError("Period and density must be positive.")
    return (
        SOLAR_DENSITY_EXPECTED_HOURS_AT_ONE_YEAR
        * (period_days / DAYS_PER_YEAR) ** (1.0 / 3.0)
        * density_solar ** (-1.0 / 3.0)
    )


def duration_grid_hours(
    *,
    min_period_days: float,
    max_period_days: float,
    density_solar: float | None,
    config: SearchConfig | None = None,
) -> np.ndarray:
    """Log-spaced duration grid spanning what this star can physically show.

    Falls back to solar density when stellar parameters are missing -- the
    fallback is named, recorded, and deliberately wider than any dwarf-star
    truth, never a bare hard-coded list.
    """

    cfg = config or CURRENT_CONFIG.search
    density = density_solar if density_solar and density_solar > 0 else 1.0
    low = cfg.duration_grid_span[0] * expected_duration_hours(
        min_period_days, density
    )
    high = cfg.duration_grid_span[1] * expected_duration_hours(
        max_period_days, density
    )
    low = max(cfg.duration_min_hours, low)
    high = min(cfg.duration_max_hours, max(high, low * 1.5))
    return np.geomspace(low, high, cfg.duration_grid_points)


@dataclass(frozen=True, slots=True)
class PeriodGrid:
    """Search bounds with an overscan zone past the reporting ceiling."""

    min_period_days: float
    max_report_days: float
    max_search_days: float

    def in_overscan(self, period_days: float) -> bool:
        return period_days > self.max_report_days

    def at_lower_rail(self, period_days: float, tolerance: float = 0.001) -> bool:
        return period_days <= self.min_period_days * (1 + tolerance)


@dataclass(frozen=True, slots=True)
class SearchGridPlan:
    """Complete physical grid plan for one light curve."""

    period: PeriodGrid
    duration_hours: np.ndarray
    stellar_density_solar: float
    density_source: str
    single_sector: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "minimum_period_days": self.period.min_period_days,
            "maximum_reportable_period_days": self.period.max_report_days,
            "maximum_searched_period_days": self.period.max_search_days,
            "period_overscan_fraction": (
                self.period.max_search_days / self.period.max_report_days
                - 1.0
            ),
            "duration_grid_hours": [
                float(value) for value in self.duration_hours
            ],
            "stellar_density_solar": self.stellar_density_solar,
            "density_source": self.density_source,
            "single_sector": self.single_sector,
        }


def period_grid(
    *,
    baseline_days: float,
    single_sector: bool,
    requested_min_period_days: float | None = None,
    requested_max_period_days: float | None = None,
    config: SearchConfig | None = None,
) -> PeriodGrid:
    """Period bounds from user rails, baseline, and the transit-count rule."""

    cfg = config or CURRENT_CONFIG.search
    if baseline_days <= 0:
        raise ValueError("Baseline must be positive.")
    minimum = (
        cfg.min_period_days
        if requested_min_period_days is None
        else max(cfg.min_period_days, float(requested_min_period_days))
    )
    if minimum <= 0:
        raise ValueError("Minimum period must be positive.")
    requested_maximum = (
        None
        if requested_max_period_days is None
        else float(requested_max_period_days)
    )
    if requested_maximum is not None and requested_maximum <= minimum:
        raise ValueError("Requested period bounds must satisfy min < max.")
    min_transits = (
        cfg.min_transits_single_sector
        if single_sector
        else cfg.min_transits_multisector
    )
    baseline_maximum = max(minimum * 2, baseline_days / min_transits)
    max_report = (
        baseline_maximum
        if requested_maximum is None
        else min(baseline_maximum, requested_maximum)
    )
    return PeriodGrid(
        min_period_days=minimum,
        max_report_days=max_report,
        max_search_days=max_report * (1.0 + cfg.period_overscan_fraction),
    )


def build_search_grid(
    *,
    baseline_days: float,
    single_sector: bool,
    requested_min_period_days: float,
    requested_max_period_days: float,
    stellar_radius_solar: float | None,
    stellar_mass_solar: float | None,
    config: SearchConfig | None = None,
) -> SearchGridPlan:
    """Build and label the physical BLS grid used by the shipping path."""

    cfg = config or CURRENT_CONFIG.search
    periods = period_grid(
        baseline_days=baseline_days,
        single_sector=single_sector,
        requested_min_period_days=requested_min_period_days,
        requested_max_period_days=requested_max_period_days,
        config=cfg,
    )
    density = stellar_density_solar(
        stellar_radius_solar,
        stellar_mass_solar,
    )
    density_source = (
        "catalog_stellar_mass_and_radius"
        if density is not None
        else "solar_density_fallback_missing_stellar_mass_or_radius"
    )
    effective_density = density if density is not None else 1.0
    durations = duration_grid_hours(
        min_period_days=periods.min_period_days,
        max_period_days=periods.max_search_days,
        density_solar=effective_density,
        config=cfg,
    )
    return SearchGridPlan(
        period=periods,
        duration_hours=durations,
        stellar_density_solar=effective_density,
        density_source=density_source,
        single_sector=single_sector,
    )


def grid_rail_flags(
    *,
    period_days: float,
    duration_hours: float,
    searched_periods_days: np.ndarray,
    searched_durations_hours: np.ndarray,
) -> dict[str, bool]:
    """Return whether the best BLS fit is pinned to either grid boundary."""

    periods = np.asarray(searched_periods_days, dtype=float)
    durations = np.asarray(searched_durations_hours, dtype=float)
    if periods.size == 0 or durations.size == 0:
        raise ValueError("Grid-rail checks require non-empty grids.")
    period_at_rail = bool(
        np.isclose(period_days, periods[0], rtol=1e-6, atol=1e-10)
        or np.isclose(period_days, periods[-1], rtol=1e-6, atol=1e-10)
    )
    duration_at_rail = bool(
        np.isclose(duration_hours, durations[0], rtol=1e-6, atol=1e-10)
        or np.isclose(duration_hours, durations[-1], rtol=1e-6, atol=1e-10)
    )
    return {
        "period_at_grid_rail": period_at_rail,
        "duration_at_grid_rail": duration_at_rail,
        "grid_rail": period_at_rail or duration_at_rail,
    }


def _significant_event_fraction(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_days: float,
    event_sigma: float,
) -> float:
    """Fraction of predicted, sampled windows with a *significant* dip.

    Sign alone is not evidence: an empty window's median is below baseline
    half the time by chance, which let a half-period fold look 75% populated.
    A window votes "present" only when its depth clears ``event_sigma``
    standard errors of its own sampling.
    """

    t = np.asarray(time, dtype=float)
    y = np.asarray(flux, dtype=float)
    first = int(np.ceil((t[0] - transit_time) / period_days))
    last = int(np.floor((t[-1] - transit_time) / period_days))
    if last < first:
        return 0.0
    phase = ((t - transit_time + period_days / 2) % period_days) - period_days / 2
    out_of_transit = np.abs(phase) >= duration_days
    if np.count_nonzero(out_of_transit) < 20:
        return 0.0
    baseline_values = y[out_of_transit]
    baseline = float(np.nanmedian(baseline_values))
    scatter = float(
        1.4826 * np.nanmedian(np.abs(baseline_values - baseline))
    )
    if not np.isfinite(scatter) or scatter <= 0:
        return 0.0
    sampled = 0
    significant = 0
    for number in range(first, last + 1):
        center = transit_time + number * period_days
        window = np.abs(t - center) <= duration_days / 2
        count = int(np.count_nonzero(window))
        if count < 2:
            continue
        sampled += 1
        depth = baseline - float(np.nanmedian(y[window]))
        if depth > event_sigma * scatter / np.sqrt(count):
            significant += 1
    return significant / sampled if sampled else 0.0


def adjudicate_alias(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    period_days: float,
    transit_time: float,
    duration_hours: float,
    config: SearchConfig | None = None,
) -> dict[str, object]:
    """Choose the best member of a reported ephemeris's alias ladder.

    For every ratio in the ladder, three folded measurements decide:

    * the stacked depth significance at that period;
    * the fraction of predicted, sampled events actually showing a dip (a
      true period reported at half shows dips in only half its windows);
    * an equal-depth signal at phase 0.5 (a true period reported at double
      puts the skipped events there).

    The reported ephemeris is only re-labelled, never silently replaced:
    the decision, the scores, and the runner-up are all returned for the
    evidence record.
    """

    cfg = config or CURRENT_CONFIG.search
    t = np.asarray(time, dtype=float)
    span = float(t[-1] - t[0]) if t.size else 0.0
    duration_days = duration_hours / 24.0
    rows: list[dict[str, float | bool]] = []
    for ratio in cfg.alias_ratios:
        candidate = period_days * ratio
        if candidate <= 0 or candidate < 2 * duration_days:
            continue
        if candidate > span / 2:
            # Fewer than two full cycles: not adjudicable, not a candidate.
            continue
        primary = evaluate_ephemeris(
            t,
            flux,
            period_days=candidate,
            transit_time=transit_time,
            duration_hours=duration_hours,
        )
        if not primary["sampled"]:
            continue
        offset = evaluate_ephemeris(
            t,
            flux,
            period_days=candidate,
            transit_time=transit_time + candidate / 2,
            duration_hours=duration_hours,
        )
        primary_depth = float(primary["depth_ppm"])
        offset_depth = float(offset["depth_ppm"]) if offset["sampled"] else 0.0
        half_phase_ratio = (
            max(0.0, min(1.0, offset_depth / primary_depth))
            if primary_depth > 0
            else 1.0
        )
        event_fraction = _significant_event_fraction(
            t,
            flux,
            period_days=candidate,
            transit_time=transit_time,
            duration_days=duration_days,
            event_sigma=cfg.alias_event_sigma,
        )
        score = (
            max(0.0, float(primary["depth_snr"]))
            * event_fraction
            * (1.0 - half_phase_ratio)
        )
        rows.append(
            {
                "ratio": ratio,
                "period_days": candidate,
                "depth_snr": round(float(primary["depth_snr"]), 3),
                "depth_ppm": round(primary_depth, 1),
                "significant_event_fraction": round(event_fraction, 3),
                "half_phase_depth_ratio": round(half_phase_ratio, 3),
                "score": round(score, 3),
            }
        )
    if not rows:
        return {
            "adjudicated": False,
            "chosen_period_days": period_days,
            "changed": False,
            "candidates": [],
        }
    rows.sort(key=lambda row: float(row["score"]), reverse=True)
    reported = next(
        (row for row in rows if abs(float(row["ratio"]) - 1.0) < 1e-9), None
    )
    chosen = rows[0]
    # Hysteresis: an alias replaces the reported ephemeris only when it wins
    # by a clear margin, not on a statistical tie.
    if (
        reported is not None
        and chosen is not reported
        and float(chosen["score"])
        <= cfg.alias_change_margin * float(reported["score"])
    ):
        chosen = reported
    return {
        "adjudicated": True,
        "chosen_period_days": float(chosen["period_days"]),
        "chosen_ratio": float(chosen["ratio"]),
        "changed": abs(float(chosen["ratio"]) - 1.0) > 1e-9,
        "candidates": rows,
        "runner_up_period_days": (
            float(rows[1]["period_days"]) if len(rows) > 1 else None
        ),
    }
