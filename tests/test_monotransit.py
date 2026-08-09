"""Monotransit detector (MASTER_PLAN 3.4, lane 6.2).

The load-bearing tests here are the negative ones. A monotransit has no repeat
to confirm it, so the detector's value is entirely in what it *refuses*:
section 6.2 says false-alarm control is the whole game, and correction 68
measured the periodic search's inverted survivor rate at 4.2x over budget on
this very cohort.
"""

from __future__ import annotations

import numpy as np

from exohunt.monotransit import (
    DEFAULT_SIGNIFICANCE_THRESHOLD,
    duration_grid_hours,
    limb_darkened_template,
    search_monotransits,
)


def _light_curve(
    *,
    days: float = 25.0,
    cadence_minutes: float = 2.0,
    noise: float = 3e-4,
    seed: int = 20260809,
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    time = np.arange(0.0, days, cadence_minutes / 60.0 / 24.0)
    return time, 1.0 + rng.normal(0.0, noise, time.size)


def _inject(
    time: np.ndarray,
    flux: np.ndarray,
    *,
    centre: float,
    duration_hours: float,
    depth: float,
) -> np.ndarray:
    out = flux.copy()
    offsets = (time - centre) / (duration_hours / 24.0)
    out -= depth * limb_darkened_template(offsets)
    return out


def test_the_template_is_rounded_not_a_box() -> None:
    """A box matches steps and cosmic rays; limb darkening is the shape prior."""

    offsets = np.linspace(-0.5, 0.5, 101)
    shape = limb_darkened_template(offsets)
    assert shape.max() == 1.0
    assert shape[0] < 0.5 and shape[-1] < 0.5  # tapers at contact points
    assert shape[50] == 1.0                     # deepest at centre
    # Strictly rounded: the midpoint between centre and limb is between them.
    assert shape[25] < shape[50] and shape[25] > shape[0]


def test_the_template_is_zero_outside_the_event() -> None:
    shape = limb_darkened_template(np.array([-2.0, -0.6, 0.0, 0.6, 2.0]))
    assert shape[0] == 0.0 and shape[1] == 0.0
    assert shape[3] == 0.0 and shape[4] == 0.0
    assert shape[2] == 1.0


def test_the_duration_grid_covers_the_specified_span() -> None:
    grid = duration_grid_hours()
    assert grid[0] == 1.5    # MASTER_PLAN 3.4
    assert grid[-1] == 24.0
    assert np.all(np.diff(grid) > 0)


def test_a_clean_injected_single_event_is_recovered() -> None:
    time, flux = _light_curve()
    flux = _inject(time, flux, centre=12.0, duration_hours=6.0, depth=0.01)
    result = search_monotransits(time, flux)
    assert result["survivor_count"] >= 1
    best = result["events"][0]
    assert abs(best["time_btjd"] - 12.0) < 0.25
    assert best["depth"] > 0.005
    assert best["significance"] >= DEFAULT_SIGNIFICANCE_THRESHOLD


def test_pure_noise_produces_no_survivor() -> None:
    """The false-alarm floor. Nothing was injected, so nothing may survive."""

    time, flux = _light_curve(seed=7)
    result = search_monotransits(time, flux)
    assert result["survivor_count"] == 0


def test_inverted_flux_produces_no_survivor() -> None:
    """The null section 6.2 cares about: a dimming inverted is a brightening.

    This is the same construction the calibration uses -- a survivor here is a
    false alarm by definition.
    """

    time, flux = _light_curve()
    flux = _inject(time, flux, centre=12.0, duration_hours=6.0, depth=0.01)
    inverted = 2.0 - flux
    result = search_monotransits(time, inverted)
    assert result["survivor_count"] == 0


def test_an_event_against_a_gap_wall_is_vetoed() -> None:
    """Section 3.4 lists this veto explicitly: gap walls mimic ingress."""

    time, flux = _light_curve(days=25.0)
    # Remove a chunk so the "event" sits hard against the interruption.
    keep = (time < 12.0) | (time > 12.6)
    time, flux = time[keep], flux[keep]
    flux = _inject(time, flux, centre=11.95, duration_hours=6.0, depth=0.01)
    result = search_monotransits(time, flux)
    flagged = [
        event
        for event in result["events"]
        if "adjacent_to_segment_edge_or_gap" in event["vetoes"]
        or "no_two_sided_local_baseline" in event["vetoes"]
    ]
    assert flagged, "an event on a gap wall must be vetoed, not reported"


def test_the_detector_never_reports_a_period() -> None:
    """The claim ceiling. A single event cannot constrain an orbit."""

    time, flux = _light_curve()
    flux = _inject(time, flux, centre=12.0, duration_hours=6.0, depth=0.01)
    result = search_monotransits(time, flux)
    assert result["claim_ceiling"] == "single_event_lead"
    blob = repr(result).lower()
    assert "period_days" not in blob
    for event in result["events"]:
        assert "period" not in " ".join(event.keys()).lower()


def test_the_result_declares_its_threshold_uncalibrated() -> None:
    """Until an inverted false-event rate exists, this must not read as ready.

    Correction 57's shape: an unmeasured check that presents as a clean pass is
    indistinguishable from a real one in aggregate.
    """

    time, flux = _light_curve()
    result = search_monotransits(time, flux)
    assert result["threshold_calibrated"] is False
    assert "0.3 false events/star" in result["threshold_note"]


def test_a_deeper_event_is_more_significant() -> None:
    time, flux = _light_curve()
    shallow = search_monotransits(
        time, _inject(time, flux, centre=12.0, duration_hours=6.0, depth=0.004)
    )
    deep = search_monotransits(
        time, _inject(time, flux, centre=12.0, duration_hours=6.0, depth=0.02)
    )
    assert deep["events"][0]["significance"] > shallow["events"][0]["significance"]
