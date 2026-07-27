"""Physical grids and alias adjudication."""

from __future__ import annotations

import numpy as np
import pytest

from exohunt.detection import inject_box_transit
from exohunt.search import (
    adjudicate_alias,
    duration_grid_hours,
    expected_duration_hours,
    period_grid,
    stellar_density_solar,
)

RNG = np.random.default_rng(42)


def test_expected_duration_matches_the_scaling_relation() -> None:
    assert expected_duration_hours(365.25, 1.0) == pytest.approx(13.0)
    # One-day period around the Sun: ~1.8 hours.
    assert expected_duration_hours(1.0, 1.0) == pytest.approx(1.82, abs=0.05)


def test_density_from_radius_and_mass() -> None:
    assert stellar_density_solar(1.0, 1.0) == pytest.approx(1.0)
    assert stellar_density_solar(0.3, 0.3) == pytest.approx(11.11, abs=0.01)
    assert stellar_density_solar(None, 1.0) is None
    assert stellar_density_solar(0.0, 1.0) is None


def test_duration_grid_scales_with_stellar_density() -> None:
    m_dwarf = duration_grid_hours(
        min_period_days=0.5,
        max_period_days=10.0,
        density_solar=stellar_density_solar(0.3, 0.3),
    )
    giant = duration_grid_hours(
        min_period_days=0.5,
        max_period_days=10.0,
        density_solar=stellar_density_solar(10.0, 1.5),
    )
    # An M dwarf cannot produce the six-hour boxes that railed 4,401 fits.
    assert m_dwarf.max() < 3.0
    assert m_dwarf.min() >= 0.5
    # A giant's grid reaches the physical ceiling instead.
    assert giant.max() == pytest.approx(12.0)
    assert np.all(np.diff(m_dwarf) > 0)


def test_duration_grid_falls_back_to_solar_density() -> None:
    fallback = duration_grid_hours(
        min_period_days=0.5, max_period_days=10.0, density_solar=None
    )
    solar = duration_grid_hours(
        min_period_days=0.5, max_period_days=10.0, density_solar=1.0
    )
    assert np.allclose(fallback, solar)


def test_period_grid_bounds_and_overscan() -> None:
    grid = period_grid(baseline_days=27.0, single_sector=False)
    assert grid.max_report_days == pytest.approx(9.0)
    assert grid.max_search_days == pytest.approx(9.72)
    assert grid.in_overscan(9.5)
    assert not grid.in_overscan(8.9)
    single = period_grid(baseline_days=27.0, single_sector=True)
    assert single.max_report_days == pytest.approx(13.5)


def _quiet_curve_with_transits(
    period: float, t0: float, duration_hours: float, depth_ppm: float
) -> tuple[np.ndarray, np.ndarray]:
    time = np.arange(0.0, 27.0, 10.0 / (24 * 60))
    flux = 1.0 + RNG.normal(0.0, 400e-6, time.size)
    injected, _, _ = inject_box_transit(
        time,
        flux,
        period_days=period,
        transit_time=t0,
        duration_hours=duration_hours,
        depth_ppm=depth_ppm,
    )
    return time, injected


def test_alias_adjudication_recovers_the_true_period_from_half() -> None:
    """The TOI-700 c failure mode: a true period reported at exactly half."""

    time, flux = _quiet_curve_with_transits(4.0, 2.0, 3.0, 8_000.0)
    verdict = adjudicate_alias(
        time,
        flux,
        period_days=2.0,  # reported at half the truth
        transit_time=2.0,
        duration_hours=3.0,
    )
    assert verdict["adjudicated"]
    assert verdict["changed"]
    assert verdict["chosen_period_days"] == pytest.approx(4.0)


def test_alias_adjudication_keeps_a_correct_period() -> None:
    time, flux = _quiet_curve_with_transits(4.0, 2.0, 3.0, 8_000.0)
    verdict = adjudicate_alias(
        time,
        flux,
        period_days=4.0,
        transit_time=2.0,
        duration_hours=3.0,
    )
    assert verdict["adjudicated"]
    assert not verdict["changed"]
    assert verdict["chosen_period_days"] == pytest.approx(4.0)
    # The double-period candidate is punished by its equal-depth signal at
    # phase 0.5 (the events it skipped).
    double = next(
        row
        for row in verdict["candidates"]
        if row["period_days"] == pytest.approx(8.0)
    )
    assert double["half_phase_depth_ratio"] > 0.7
    assert double["score"] < verdict["candidates"][0]["score"]


def test_alias_adjudication_declines_when_nothing_is_testable() -> None:
    time = np.arange(0.0, 3.0, 10.0 / (24 * 60))
    flux = 1.0 + RNG.normal(0.0, 400e-6, time.size)
    verdict = adjudicate_alias(
        time,
        flux,
        # Even the one-third alias exceeds two cycles of the 3-day span.
        period_days=5.8,
        transit_time=0.5,
        duration_hours=3.0,
    )
    assert not verdict["adjudicated"]
    assert verdict["chosen_period_days"] == pytest.approx(5.8)
