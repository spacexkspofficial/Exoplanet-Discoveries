"""Physical grids and alias adjudication."""

from __future__ import annotations

import numpy as np
import pytest

from exohunt.detection import inject_box_transit
from exohunt.search import (
    adjudicate_alias,
    build_search_grid,
    duration_grid_hours,
    expected_duration_hours,
    grid_rail_flags,
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


def test_requested_ceiling_is_report_boundary_not_search_rail() -> None:
    grid = period_grid(
        baseline_days=27.0,
        single_sector=True,
        requested_min_period_days=1.0,
        requested_max_period_days=10.0,
    )

    assert grid.min_period_days == 1.0
    assert grid.max_report_days == 10.0
    assert grid.max_search_days == pytest.approx(10.8)
    assert grid.in_overscan(10.4)


def test_complete_search_grid_records_density_and_fallback() -> None:
    physical = build_search_grid(
        baseline_days=27.0,
        single_sector=True,
        requested_min_period_days=0.5,
        requested_max_period_days=20.0,
        stellar_radius_solar=0.3,
        stellar_mass_solar=0.3,
    )
    fallback = build_search_grid(
        baseline_days=27.0,
        single_sector=True,
        requested_min_period_days=0.5,
        requested_max_period_days=20.0,
        stellar_radius_solar=0.3,
        stellar_mass_solar=None,
    )

    assert physical.stellar_density_solar == pytest.approx(11.11, abs=0.01)
    assert physical.density_source == "catalog_stellar_mass_and_radius"
    assert physical.duration_hours.max() < 3.0
    assert fallback.stellar_density_solar == 1.0
    assert fallback.density_source.startswith("solar_density_fallback")
    assert fallback.duration_hours.max() > physical.duration_hours.max()


def test_grid_rail_flags_detect_either_fit_boundary() -> None:
    periods = np.array([0.5, 1.0, 1.5])
    durations = np.array([0.5, 1.0, 2.0])

    interior = grid_rail_flags(
        period_days=1.0,
        duration_hours=1.0,
        searched_periods_days=periods,
        searched_durations_hours=durations,
    )
    duration_rail = grid_rail_flags(
        period_days=1.0,
        duration_hours=2.0,
        searched_periods_days=periods,
        searched_durations_hours=durations,
    )

    assert interior == {
        "period_at_grid_rail": False,
        "duration_at_grid_rail": False,
        "grid_rail": False,
    }
    assert duration_rail["duration_at_grid_rail"] is True
    assert duration_rail["grid_rail"] is True


def test_grid_rail_flags_use_effective_quantized_duration_endpoint() -> None:
    flags = grid_rail_flags(
        period_days=1.0,
        duration_hours=1.95,
        searched_periods_days=np.array([0.5, 1.0, 1.5]),
        searched_durations_hours=np.array([0.5, 0.6, 1.95]),
    )

    assert flags["duration_at_grid_rail"] is True
    assert flags["grid_rail"] is True


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


def test_a_triple_period_alias_is_punished_by_its_skipped_transits() -> None:
    """The blind spot: phase 0.5 cannot see a 3x alias.

    At three times the true period, phase 0.5 lands at 1.5 P -- between
    transits -- so the half-phase test finds nothing and the candidate escapes
    unpunished. Measured cost on the 341-star known-planet cohort when the
    ladder was first wired in with only that test: 8 exact periods converted
    into triple-period aliases against 3 aliases fixed, taking exact
    recoveries 288 -> 283. The headline `recovered` count did not move at all,
    because a harmonic alias still scores as recovered.
    """

    time, flux = _quiet_curve_with_transits(3.0, 2.0, 3.0, 8_000.0)
    verdict = adjudicate_alias(
        time, flux, period_days=3.0, transit_time=2.0, duration_hours=3.0
    )
    triple = next(
        row
        for row in verdict["candidates"]
        if row["period_days"] == pytest.approx(9.0)
    )
    # The old test is blind here, and that is the point of this one.
    assert triple["half_phase_depth_ratio"] < 0.1
    # The generalized sub-phase probe sees the skipped transits at 1/3 and 2/3.
    assert triple["max_sub_phase_depth_ratio"] > 0.9
    best = max(verdict["candidates"], key=lambda row: row["score"])
    assert best["period_days"] == pytest.approx(3.0)
    assert triple["score"] < best["score"]


def test_alias_adjudication_recovers_the_true_period_from_one_third() -> None:
    """31 of 341 known planets were reported at exactly P/3 and none recovered."""

    time, flux = _quiet_curve_with_transits(3.0, 2.0, 3.0, 8_000.0)
    verdict = adjudicate_alias(
        time,
        flux,
        period_days=1.0,  # reported at one third of the truth
        transit_time=2.0,
        duration_hours=3.0,
    )
    assert verdict["adjudicated"]
    assert verdict["changed"]
    assert verdict["chosen_period_days"] == pytest.approx(3.0)


def test_the_ratio_field_is_the_alias_ratio_not_a_depth_fraction() -> None:
    """Regression: the sub-phase loop shadowed the enclosing `ratio`.

    That silently rewrote every row's `ratio` with a depth fraction, which
    broke the lookup of the reported ephemeris and reported `changed` for
    periods the ladder had actually left alone.
    """

    time, flux = _quiet_curve_with_transits(3.0, 2.0, 3.0, 8_000.0)
    verdict = adjudicate_alias(
        time, flux, period_days=3.0, transit_time=2.0, duration_hours=3.0
    )
    ratios = {round(float(row["ratio"]), 3) for row in verdict["candidates"]}
    assert 1.0 in ratios, "the reported ephemeris must appear at ratio 1.0"
    for row in verdict["candidates"]:
        assert row["period_days"] == pytest.approx(3.0 * float(row["ratio"]))
    assert verdict["changed"] is False


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
