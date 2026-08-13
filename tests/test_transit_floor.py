"""The observed-transit floor (correction 80).

The strongest BLS peak was routinely a fold whose transits land in the data
gaps. One baseline exemplar, TIC 165501611, reported a depth of 215,028 ppm at
S/N 113.6 with `observed_transits: 0` -- a signal of zero transits at S/N 113,
measured against nothing. Stars share a gap structure rather than a star, so
those fits piled onto shared instants and drove `epoch_enrichment` to 5.03
against a ceiling of 2.0.

`fewer_than_two_observed_transits` was already a triage veto, so the search was
reporting as its strongest signal something the next stage always discarded.
"""

from __future__ import annotations

import numpy as np

from exohunt.config import CURRENT_CONFIG
from exohunt.detection import _ranked_distinct_peaks, search_transits


def _gapped_light_curve(
    *,
    period: float = 3.0,
    depth: float = 0.01,
    duration_hours: float = 2.5,
    seed: int = 11,
) -> tuple[np.ndarray, np.ndarray]:
    """Two observing blocks with a wide gap, and a real transiting signal."""

    rng = np.random.default_rng(seed)
    cadence = 2.0 / 60.0 / 24.0
    first = np.arange(0.0, 12.0, cadence)
    second = np.arange(20.0, 32.0, cadence)
    time = np.concatenate([first, second])
    flux = 1.0 + rng.normal(0.0, 2e-4, time.size)
    phase = ((time - 1.5 + period / 2.0) % period) - period / 2.0
    flux[np.abs(phase) < (duration_hours / 24.0) / 2.0] -= depth
    return time, flux


def test_the_floor_is_recorded_even_when_it_was_met_outright() -> None:
    """"Met outright", "replaced" and "unmeetable" are three different states.

    A block that appears only on failure cannot be distinguished from one that
    never ran -- the defect this whole ledger keeps finding.
    """

    time, flux = _gapped_light_curve()
    _, arrays = search_transits(time, flux, min_period_days=1.0, max_period_days=8.0)

    floor = arrays["transit_floor"]
    assert floor["minimum_observed_transits"] == 2
    assert floor["satisfied"] is True
    assert floor["applied"] is False


def test_a_real_repeating_signal_is_not_disturbed_by_the_floor() -> None:
    """The floor must not move a fit that is already witnessed by the data."""

    time, flux = _gapped_light_curve(period=3.0)
    with_floor, _ = search_transits(
        time, flux, min_period_days=1.0, max_period_days=8.0
    )
    without_floor, _ = search_transits(
        time,
        flux,
        min_period_days=1.0,
        max_period_days=8.0,
        minimum_observed_transits=0,
    )

    assert with_floor.observed_transits >= 2
    assert with_floor.period_days == without_floor.period_days


def test_disabling_the_floor_is_possible_so_it_can_be_priced() -> None:
    """Every kernel change here has to be A/B-able against a calibration."""

    time, flux = _gapped_light_curve()
    _, arrays = search_transits(
        time,
        flux,
        min_period_days=1.0,
        max_period_days=8.0,
        minimum_observed_transits=0,
    )

    floor = arrays["transit_floor"]
    assert floor["minimum_observed_transits"] == 0
    assert floor["applied"] is False
    # Not True. A disabled check reporting "satisfied" is how a zero-transit
    # fold looked acceptable for as long as it did.
    assert floor["satisfied"] is None


def test_the_reported_fit_always_meets_the_floor_or_says_it_could_not() -> None:
    """The load-bearing guarantee, stated as an invariant.

    Either the reported ephemeris is witnessed by two events, or the report
    carries a reason it is not. Silence is the failure mode being removed.
    """

    for seed in range(6):
        time, flux = _gapped_light_curve(seed=seed)
        result, arrays = search_transits(
            time, flux, min_period_days=1.0, max_period_days=8.0
        )
        floor = arrays["transit_floor"]
        if result.observed_transits >= 2:
            assert floor["satisfied"] is True
        else:
            assert floor["satisfied"] is False
            assert "not_applied_reason" in floor


def test_ranked_peaks_are_distinct_hypotheses_not_one_peak_resampled() -> None:
    """Adjacent grid points of a single peak are the same hypothesis twice."""

    periods = np.array([1.000, 1.001, 1.002, 2.000, 2.001, 4.000])
    powers = np.array([9.0, 8.9, 8.8, 7.0, 6.9, 5.0])

    peaks = _ranked_distinct_peaks(
        periods, powers, separation_fraction=0.02, limit=10
    )

    assert [round(float(periods[i]), 3) for i in peaks] == [1.0, 2.0, 4.0]


def test_a_bank_of_all_nan_power_yields_no_peaks_rather_than_an_index() -> None:
    periods = np.array([1.0, 2.0, 3.0])
    powers = np.array([np.nan, np.nan, np.nan])

    assert _ranked_distinct_peaks(
        periods, powers, separation_fraction=0.02, limit=5
    ) == []


def test_the_floor_is_two_events_and_matches_the_triage_veto() -> None:
    """The kernel floor and `fewer_than_two_observed_transits` must agree.

    If these ever diverge the search will once again report as its strongest
    signal something the next stage always discards.
    """

    assert CURRENT_CONFIG.search.minimum_observed_transits == 2
