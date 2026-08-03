"""Cheap physical vetoes: duration-density, depths, odd/even, secondaries."""

from __future__ import annotations

import numpy as np
import pytest

from exohunt.detection import inject_box_transit
from exohunt.vetoes import (
    DEPTH_EB_LANE_REASON,
    EVENT_SUPPORT_REJECTION_REASON,
    depth_physicality,
    dip_window_veto,
    duration_density_consistency,
    evaluate_t3_vetoes,
    full_phase_secondary_scan,
    odd_even_difference,
    per_event_support,
)

RNG = np.random.default_rng(7)


def _cadence_days() -> float:
    return 10.0 / (24 * 60)


def test_duration_density_consistency_verdicts() -> None:
    # A plausible planet: 2 h at P=3 d around a Sun-like star (~2.6 h b=0).
    ok = duration_density_consistency(
        period_days=3.0, duration_hours=2.0, density_solar=1.0
    )
    assert ok["verdict"] == "pass"
    # Same star, 6 h fit: physically strained -- flagged, not killed.
    strained = duration_density_consistency(
        period_days=3.0, duration_hours=6.0, density_solar=1.0
    )
    assert strained["verdict"] == "flag"
    # A 6-hour box at P=1 d around an M dwarf is impossible: kill.
    impossible = duration_density_consistency(
        period_days=1.0, duration_hours=6.0, density_solar=11.1
    )
    assert impossible["verdict"] == "kill"
    assert impossible["ratio"] > 5
    # No stellar parameters: say so, decide nothing.
    unknown = duration_density_consistency(
        period_days=3.0, duration_hours=2.0, density_solar=None
    )
    assert unknown["verdict"] == "not_evaluable"


def test_depth_physicality_routes_stellar_companions_to_the_eb_lane() -> None:
    planet = depth_physicality(depth_ppm=20_000.0, stellar_radius_solar=1.0)
    assert planet["verdict"] == "pass"
    assert planet["implied_radius_rjup"] == pytest.approx(1.41, abs=0.02)
    binary = depth_physicality(depth_ppm=50_000.0, stellar_radius_solar=1.5)
    assert binary["verdict"] == "eb_lane"
    assert binary["implied_radius_rjup"] > 3.0
    assert (
        depth_physicality(depth_ppm=1_000.0, stellar_radius_solar=None)[
            "verdict"
        ]
        == "not_evaluable"
    )


def _alternating_eb(
    depth_odd_ppm: float, depth_even_ppm: float
) -> tuple[np.ndarray, np.ndarray]:
    """Eclipses alternating in depth: an EB folded at half its true period."""

    time = np.arange(0.0, 27.0, _cadence_days())
    flux = 1.0 + RNG.normal(0.0, 400e-6, time.size)
    # Even events (0, 2, ...) from the first series; odd from the second.
    flux, _, _ = inject_box_transit(
        time, flux, period_days=4.0, transit_time=1.0,
        duration_hours=2.4, depth_ppm=depth_even_ppm,
    )
    flux, _, _ = inject_box_transit(
        time, flux, period_days=4.0, transit_time=3.0,
        duration_hours=2.4, depth_ppm=depth_odd_ppm,
    )
    return time, flux


def test_odd_even_difference_kills_alternating_depths() -> None:
    time, flux = _alternating_eb(8_000.0, 4_000.0)
    verdict = odd_even_difference(
        time, flux, period_days=2.0, transit_time=1.0, duration_hours=2.4
    )
    assert verdict["verdict"] == "kill"
    assert verdict["sigma"] > 3


def test_odd_even_difference_passes_equal_depths() -> None:
    time, flux = _alternating_eb(5_000.0, 5_000.0)
    verdict = odd_even_difference(
        time, flux, period_days=2.0, transit_time=1.0, duration_hours=2.4
    )
    assert verdict["verdict"] == "pass"
    assert verdict["sigma"] < 3


def test_full_phase_secondary_scan_finds_eccentric_secondaries() -> None:
    """The historical screen looked only at phase 0.5; eccentric binaries
    put secondaries elsewhere. This one is at phase 0.3."""

    time = np.arange(0.0, 27.0, _cadence_days())
    flux = 1.0 + RNG.normal(0.0, 400e-6, time.size)
    flux, _, _ = inject_box_transit(
        time, flux, period_days=3.0, transit_time=1.0,
        duration_hours=2.4, depth_ppm=10_000.0,
    )
    flux, _, _ = inject_box_transit(
        time, flux, period_days=3.0, transit_time=1.9,
        duration_hours=2.4, depth_ppm=4_000.0,
    )
    verdict = full_phase_secondary_scan(
        time, flux, period_days=3.0, transit_time=1.0, duration_hours=2.4
    )
    assert verdict["verdict"] == "kill"
    assert verdict["snr"] > 3
    assert verdict["phase_fraction"] == pytest.approx(0.3, abs=0.05)
    assert verdict["tested_phase_windows"] > 1
    assert verdict["family_wise_false_alarm_probability"] < 0.00135


def test_full_phase_secondary_scan_passes_a_clean_transit() -> None:
    time = np.arange(0.0, 27.0, _cadence_days())
    flux = 1.0 + RNG.normal(0.0, 400e-6, time.size)
    flux, _, _ = inject_box_transit(
        time, flux, period_days=3.0, transit_time=1.0,
        duration_hours=2.4, depth_ppm=10_000.0,
    )
    verdict = full_phase_secondary_scan(
        time, flux, period_days=3.0, transit_time=1.0, duration_hours=2.4
    )
    assert verdict["verdict"] == "pass"


def test_full_phase_secondary_scan_controls_the_look_elsewhere_effect() -> None:
    rng = np.random.default_rng(20260728)
    time = np.arange(0.0, 27.0, _cadence_days())
    killed = 0
    draws = 500
    for _ in range(draws):
        flux = 1.0 + rng.normal(0.0, 400e-6, time.size)
        verdict = full_phase_secondary_scan(
            time,
            flux,
            period_days=3.0,
            transit_time=1.0,
            duration_hours=2.4,
        )
        killed += verdict["verdict"] == "kill"

    assert killed / draws <= 0.01


def test_per_event_support_discounts_gap_adjacent_events() -> None:
    time = np.arange(0.0, 27.0, _cadence_days())
    # Remove the window and trailing baseline of the event at t=13.0.
    keep = ~((time > 12.8) & (time < 13.6))
    time = time[keep]
    support = per_event_support(
        time, period_days=6.5, transit_time=0.0, duration_hours=2.4
    )
    events = {row["event"]: row["supported"] for row in support["events"]}
    assert events[1] and events[3]  # t = 6.5 and 19.5: fully supported
    assert not events[2]  # t = 13.0: inside the gap
    assert support["supported_events"] == support["predicted_events"] - 2
    # (event 0 at t=0.0 also fails: no leading baseline before the start)
    assert not events[0]


def test_dip_window_veto_counts_events_in_registered_windows() -> None:
    verdict = dip_window_veto(
        [4074.4, 4080.8, 4085.1],
        windows=[(4074.3, 4074.5), (4080.7, 4080.9)],
    )
    assert verdict["events_in_systematic_windows"] == 2
    assert verdict["events_clean"] == 1
    assert verdict["flagged_centers"] == [4074.4, 4080.8]


def test_complete_t3_gate_routes_large_companion_to_eb_lane() -> None:
    time = np.arange(0.0, 27.0, _cadence_days())
    flux = 1.0 + RNG.normal(0.0, 400e-6, time.size)
    flux, _, _ = inject_box_transit(
        time,
        flux,
        period_days=3.0,
        transit_time=1.0,
        duration_hours=2.4,
        depth_ppm=50_000.0,
    )

    verdict = evaluate_t3_vetoes(
        time,
        flux,
        period_days=3.0,
        transit_time=1.0,
        duration_hours=2.4,
        depth_ppm=50_000.0,
        density_solar=1.0,
        stellar_radius_solar=1.5,
        minimum_supported_events=2,
    )

    assert verdict["passes"] is False
    assert verdict["routes_to_eb_lane"] is True
    assert DEPTH_EB_LANE_REASON in verdict["rejection_reasons"]
    assert verdict["checks"]["depth_physicality"]["verdict"] == "eb_lane"
    assert verdict["checks"]["event_support"]["supported_events"] >= 2


def test_complete_t3_gate_rejects_insufficient_two_sided_event_support() -> None:
    time = np.arange(0.0, 27.0, _cadence_days())
    time = time[~((time > 12.8) & (time < 13.6))]
    flux = 1.0 + RNG.normal(0.0, 400e-6, time.size)

    verdict = evaluate_t3_vetoes(
        time,
        flux,
        period_days=13.0,
        transit_time=0.0,
        duration_hours=2.4,
        depth_ppm=1_000.0,
        density_solar=None,
        stellar_radius_solar=None,
        minimum_supported_events=2,
    )

    assert verdict["passes"] is False
    assert EVENT_SUPPORT_REJECTION_REASON in verdict["rejection_reasons"]
    assert verdict["checks"]["event_support"]["supported_events"] < 2


def test_complete_t3_gate_requires_a_positive_event_minimum() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        evaluate_t3_vetoes(
            np.arange(100.0),
            np.ones(100),
            period_days=3.0,
            transit_time=1.0,
            duration_hours=2.4,
            depth_ppm=1_000.0,
            density_solar=None,
            stellar_radius_solar=None,
            minimum_supported_events=0,
        )


def test_t3_folded_checks_ignore_nonfinite_cadences() -> None:
    time = np.arange(0.0, 27.0, _cadence_days())
    flux = 1.0 + RNG.normal(0.0, 400e-6, time.size)
    flux[::97] = np.nan
    verdict = evaluate_t3_vetoes(
        time,
        flux,
        period_days=3.0,
        transit_time=1.0,
        duration_hours=2.4,
        depth_ppm=1_000.0,
        density_solar=None,
        stellar_radius_solar=None,
        minimum_supported_events=2,
    )

    assert verdict["checks"]["odd_even"]["verdict"] in {
        "pass",
        "kill",
        "not_evaluable",
    }
    assert np.isfinite(verdict["checks"]["full_phase_secondary"]["snr"])
