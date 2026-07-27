"""Support-weighted biweight detrending: retention, edges, and depth safety."""

from __future__ import annotations

import numpy as np
import pytest

from exohunt.config import CURRENT_CONFIG
from exohunt.detection import evaluate_ephemeris, inject_box_transit
from exohunt.detrend import (
    prepare_flux,
    prepare_fluxes,
    segment_boundaries,
    support_fractions,
)

RNG = np.random.default_rng(20260727)


def _sector_like_time() -> np.ndarray:
    """A 27-day sector at 10-minute cadence with the measured gap anatomy:

    a one-day perigee downlink mid-sector, a 3.8-hour interruption (the
    BTJD 4080.708 class), and 13-minute interruptions that must *not* split
    segments (below the 0.10-day threshold).
    """

    time = np.arange(0.0, 27.0, 10.0 / (24 * 60))
    keep = np.ones(time.size, dtype=bool)
    keep &= ~((time > 13.0) & (time < 14.0))  # downlink
    keep &= ~((time > 6.70) & (time < 6.70 + 3.8 / 24.0))  # 3.8 h gap
    for start in (3.0, 9.5, 20.0):  # routine 13-minute interruptions
        keep &= ~((time > start) & (time < start + 13.0 / (24 * 60)))
    return time[keep]


def _noise(size: int, sigma: float = 400e-6) -> np.ndarray:
    return RNG.normal(0.0, sigma, size)


def test_segments_split_only_at_real_gaps() -> None:
    time = _sector_like_time()
    segments = segment_boundaries(time, CURRENT_CONFIG.detrend.segment_gap_days)
    assert len(segments) == 3  # downlink and the 3.8 h gap, nothing else


def test_support_is_full_interior_and_half_at_edges() -> None:
    time = _sector_like_time()
    fractions, segment_count = support_fractions(
        time, window_days=1.0, gap_days=CURRENT_CONFIG.detrend.segment_gap_days
    )
    assert segment_count == 3
    interior = (time > 3.5) & (time < 5.5)
    assert np.all(fractions[interior] > 0.95)
    assert 0.4 < fractions[0] < 0.6  # clean segment start: half a window
    assert 0.4 < fractions[-1] < 0.6


def test_retention_recovers_the_edge_guard_cost() -> None:
    """The hard half-window guard retained 67% of cadences on real data.

    Support weighting must retain at least 85% on the same gap anatomy --
    the number that says we got the discarded third back.
    """

    time = _sector_like_time()
    flux = 1.0 + _noise(time.size)
    prepared = prepare_flux(time, flux, window_days=1.0)
    assert prepared.metadata["retention_fraction"] >= 0.85


def test_uncertainty_inflates_toward_edges_and_not_interior() -> None:
    time = _sector_like_time()
    flux = 1.0 + _noise(time.size)
    prepared = prepare_flux(time, flux, window_days=1.0)
    interior = (prepared.time > 3.5) & (prepared.time < 5.5)
    assert np.all(prepared.uncertainty_scale[interior] < 1.1)
    edge = prepared.time < (time[0] + 0.2)
    assert edge.any()
    assert np.all(prepared.uncertainty_scale[edge] > 1.2)
    assert np.all(
        prepared.uncertainty_scale
        <= (1.0 / CURRENT_CONFIG.detrend.edge_support_floor) + 1e-9
    )


def test_two_pass_masking_restores_depth_under_variability() -> None:
    """The measured reason the two-pass and escalation rules exist.

    Under a 2% variability signal the window's scatter is dominated by the
    variability slope, so in-transit points stop looking like outliers and a
    single blind biweight pass eats real depth (measured: ~30% loss at the
    active-star window; ~50% at the default window). The second pass -- the
    same detrend with the found ephemeris masked out of the trend estimate,
    at the active-star escalation window -- restores the depth to ~10%.
    That residual systematic is real, is recorded here, and is what the P3
    injection-recovery layer measures at scale; the gap between the two
    passes is what the `detrend_sensitive` flag reports.
    """

    time = _sector_like_time()
    variability = 1.0 + 0.02 * np.sin(2 * np.pi * time / 2.5)
    flux = variability + _noise(time.size)
    ephemeris = {
        "period_days": 3.7,
        "transit_time": 1.85,
        "duration_hours": 2.4,
    }
    injected, _, _ = inject_box_transit(
        time, flux, depth_ppm=5_000.0, **ephemeris
    )
    window = CURRENT_CONFIG.detrend.active_window_days

    blind = prepare_flux(time, injected, window_days=window)
    blind_depth = evaluate_ephemeris(blind.time, blind.flux, **ephemeris)
    assert blind_depth["sampled"]
    # Documented erosion: the blind pass keeps the signal findable but
    # measurably shallow. If this assertion ever fails upward, the erosion
    # is gone and the two-pass rule can be revisited.
    assert 0.25 * 5_000.0 < blind_depth["depth_ppm"] < 0.85 * 5_000.0

    masked = prepare_flux(
        time, injected, window_days=window, mask_events=[ephemeris]
    )
    masked_depth = evaluate_ephemeris(masked.time, masked.flux, **ephemeris)
    assert masked_depth["sampled"]
    assert masked_depth["depth_ppm"] == pytest.approx(5_000.0, rel=0.15)
    assert masked_depth["depth_ppm"] > blind_depth["depth_ppm"]
    assert masked.metadata["masked_cadences"] > 0


def test_transit_depth_survives_single_pass_on_quiet_star() -> None:
    """On a quiet star the dip is a strong outlier and survives one pass."""

    time = _sector_like_time()
    flux = 1.0 + _noise(time.size)
    ephemeris = {
        "period_days": 3.7,
        "transit_time": 1.85,
        "duration_hours": 2.4,
    }
    injected, _, _ = inject_box_transit(
        time, flux, depth_ppm=5_000.0, **ephemeris
    )
    prepared = prepare_flux(time, injected, window_days=1.0)
    measured = evaluate_ephemeris(prepared.time, prepared.flux, **ephemeris)
    assert measured["sampled"]
    assert measured["depth_ppm"] == pytest.approx(5_000.0, rel=0.10)


def test_edge_transit_remains_measurable_instead_of_discarded() -> None:
    """Events inside the old guard zone stay searchable with inflated dy.

    The hard guard removed the half-window after every segment start; an
    ephemeris whose only events landed there was undetectable by
    construction. Support weighting keeps those cadences.
    """

    time = _sector_like_time()
    flux = 1.0 + _noise(time.size)
    # Transits at 0.25 d after each of the first two segment starts.
    injected, _, _ = inject_box_transit(
        time,
        flux,
        period_days=6.45,
        transit_time=0.25,
        duration_hours=3.0,
        depth_ppm=8_000.0,
    )
    prepared = prepare_flux(time, injected, window_days=1.0)
    # The event region survived preparation...
    first_event = np.abs(prepared.time - 0.25) < (1.5 / 24.0)
    assert np.count_nonzero(first_event) >= 5
    # ...is measurable at close to the injected depth...
    measured = evaluate_ephemeris(
        prepared.time,
        prepared.flux,
        period_days=6.45,
        transit_time=0.25,
        duration_hours=3.0,
    )
    assert measured["sampled"]
    assert measured["depth_ppm"] == pytest.approx(8_000.0, rel=0.25)
    # ...and carries an honest uncertainty penalty relative to the interior.
    assert float(np.median(prepared.uncertainty_scale[first_event])) > 1.1


def test_prepare_fluxes_returns_both_windows() -> None:
    time = _sector_like_time()
    flux = 1.0 + _noise(time.size)
    both = prepare_fluxes(time, flux)
    assert set(both) == {"short", "long"}
    assert (
        both["short"].metadata["window_days"]
        == CURRENT_CONFIG.detrend.short_window_days
    )
    assert (
        both["long"].metadata["window_days"]
        == CURRENT_CONFIG.detrend.long_window_days
    )
    # The long window keeps more cadences supported (wider windows are less
    # edge-starved proportionally is false -- edges scale with window -- so
    # simply require both to publish their own honest retention numbers).
    assert 0.0 < both["long"].metadata["retention_fraction"] <= 1.0
    assert 0.0 < both["short"].metadata["retention_fraction"] <= 1.0


def test_configuration_is_recorded_in_metadata() -> None:
    time = _sector_like_time()
    flux = 1.0 + _noise(time.size)
    prepared = prepare_flux(time, flux, window_days=1.0)
    assert prepared.metadata["method"] == "biweight_support_weighted_v1"
    assert prepared.metadata["config"]["edge_support_floor"] == (
        CURRENT_CONFIG.detrend.edge_support_floor
    )
    assert prepared.metadata["segment_count"] == 3
