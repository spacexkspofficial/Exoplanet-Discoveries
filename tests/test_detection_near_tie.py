"""Near-tied BLS peak selection (owner decision 1, correction 64).

Correction 64: 14 of 53 lost known planets had the true period among the
recorded BLS peaks and were not selected -- `pi Men c` at rank 4 and
`WASP-169 b` at rank 3, both at relative power 0.9999999. At that separation
the screening statistic has saturated and `np.nanargmax` is choosing on grid
order, so the answer is decided by floating-point noise.

The property that matters most here is the *negative* one: where peaks are not
tied, selection must be exactly what a bare `argmax` would have given. A
tie-break that quietly moves ordinary targets would be a far worse defect than
the one it fixes.
"""

from __future__ import annotations

import numpy as np

from exohunt.detection import (
    _event_depth_consistency,
    _near_tied_candidates,
    search_transits,
)


def _synthetic_transit(
    period: float, *, depth: float = 0.01, duration_hours: float = 3.0
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(20260809)
    time = np.arange(0.0, 27.0, 2.0 / 60.0 / 24.0)
    flux = 1.0 + rng.normal(0.0, 3e-4, time.size)
    phase = (time + 0.5 * period) % period - 0.5 * period
    flux[np.abs(phase) < (duration_hours / 24.0) / 2] -= depth
    return time, flux


def test_an_untied_periodogram_selects_exactly_the_argmax() -> None:
    periods = np.array([1.0, 2.0, 3.0, 4.0])
    powers = np.array([1.0, 5.0, 2.0, 3.0])
    assert _near_tied_candidates(
        periods,
        powers,
        1,
        relative_tolerance=1e-3,
        max_candidates=5,
        separation_fraction=0.02,
    ) == [1]


def test_correction_64s_separation_is_recognised_as_a_tie() -> None:
    # relative power 0.9999999 -- the pi Men c / WASP-169 b case.
    periods = np.array([2.0, 6.3, 12.7])
    powers = np.array([10.0, 9.999999, 4.0])
    candidates = _near_tied_candidates(
        periods,
        powers,
        0,
        relative_tolerance=1e-3,
        max_candidates=5,
        separation_fraction=0.02,
    )
    assert set(candidates) == {0, 1}


def test_adjacent_grid_points_of_one_peak_are_not_competing_hypotheses() -> None:
    # Three samples across a single peak: same hypothesis, sampled thrice.
    periods = np.array([5.000, 5.001, 5.002])
    powers = np.array([9.9999, 10.0, 9.9998])
    candidates = _near_tied_candidates(
        periods,
        powers,
        1,
        relative_tolerance=1e-3,
        max_candidates=5,
        separation_fraction=0.02,
    )
    assert candidates == [1]


def test_disabling_the_tolerance_restores_bare_argmax() -> None:
    periods = np.array([2.0, 6.3])
    powers = np.array([10.0, 9.999999])
    assert _near_tied_candidates(
        periods,
        powers,
        0,
        relative_tolerance=0.0,
        max_candidates=5,
        separation_fraction=0.02,
    ) == [0]


def test_consistency_prefers_a_real_repeating_depth_over_a_scrambled_fold() -> None:
    period = 4.0
    time, flux = _synthetic_transit(period)
    # `_synthetic_transit` puts events at t = 0 mod period.
    true_fold = _event_depth_consistency(time, flux, period, 0.0, 3.0 / 24.0)
    # An unrelated period folds different cadences together every epoch, so the
    # "depth" is noise and its median can even come out negative.
    wrong_fold = _event_depth_consistency(time, flux, period * 1.37, 0.0, 3.0 / 24.0)
    assert np.isfinite(true_fold)
    assert true_fold > wrong_fold


def test_the_tie_break_does_not_reward_merely_having_more_events() -> None:
    """The half-period alias must lose despite folding twice as many epochs.

    This is the property that keeps the fix from making the ~1 d grid-rail
    pinning worse. A tie-break on event *count* would systematically prefer
    short periods; measured here, the true period scores ~888 and its
    half-period alias ~2.5 with double the events, because alternate epochs of
    the alias are empty and the per-epoch depths disagree.
    """

    period = 4.0
    time, flux = _synthetic_transit(period)
    true_fold = _event_depth_consistency(time, flux, period, 0.0, 3.0 / 24.0)
    half_fold = _event_depth_consistency(time, flux, period / 2, 0.0, 3.0 / 24.0)
    assert true_fold > half_fold


def test_a_fold_with_no_usable_events_can_never_win_a_tie() -> None:
    time = np.linspace(0.0, 1.0, 50)
    flux = np.ones_like(time)
    # Period longer than the baseline: no repeated events at all.
    assert _event_depth_consistency(time, flux, 100.0, 0.5, 0.1) == float("-inf")


def test_a_signal_free_light_curve_does_not_crash_the_tie_break() -> None:
    # Noise but no transit. A *perfectly* flat curve is refused upstream by
    # `_point_noise`, which is correct and not what this test is about: the
    # question is whether a periodogram with no real peak -- where near-ties
    # are common -- survives the new selection path.
    rng = np.random.default_rng(11)
    time = np.arange(0.0, 20.0, 2.0 / 60.0 / 24.0)
    flux = 1.0 + rng.normal(0.0, 3e-4, time.size)
    result, arrays = search_transits(time, flux, min_period_days=0.5, max_period_days=8.0)
    assert np.isfinite(result.period_days)
    # `near_tie` is present only when peaks actually tied; either way the key
    # must exist so its absence is never confused with "not checked".
    assert "near_tie" in arrays


def test_a_clean_transit_is_recovered_with_the_tie_break_active() -> None:
    period = 3.5
    time, flux = _synthetic_transit(period, depth=0.02)
    result, arrays = search_transits(
        time, flux, min_period_days=0.5, max_period_days=8.0
    )
    # Allow the usual alias family; what must not happen is a crash or a wild
    # answer introduced by the new selection path.
    ratio = result.period_days / period
    assert min(abs(ratio - r) for r in (0.5, 1.0, 2.0)) < 0.05


def test_the_tie_break_is_inert_when_the_tolerance_is_zero() -> None:
    period = 3.5
    time, flux = _synthetic_transit(period, depth=0.02)
    with_break, _ = search_transits(
        time, flux, min_period_days=0.5, max_period_days=8.0
    )
    without_break, arrays = search_transits(
        time,
        flux,
        min_period_days=0.5,
        max_period_days=8.0,
        near_tie_relative_power=0.0,
    )
    assert arrays["near_tie"] is None
    # On a clean, well-separated signal the two paths must agree exactly.
    assert with_break.period_days == without_break.period_days


def test_more_than_five_peaks_are_retained_for_diagnosis() -> None:
    from exohunt.detection import independent_period_peaks

    periods = np.geomspace(1.0, 20.0, 500)
    rng = np.random.default_rng(7)
    powers = rng.uniform(0.0, 1.0, periods.size)
    peaks = independent_period_peaks(periods, powers)
    # Correction 64 could not tell "absent from the periodogram" from "deeper
    # than the five peaks we kept". Twenty makes that answerable.
    assert len(peaks) > 5
